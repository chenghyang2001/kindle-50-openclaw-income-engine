"""demo20 — 供應商通訊與訂單追蹤（主流程）。

每日排程呼叫一次：讀 ERP 未結採購單 → 讀供應商信箱 → 把回覆套進
「下單 → 確認 → 出貨 → 到貨」狀態機 → 找出逾期的 PO → 產出催辦信草稿
與每月供應商計分卡。

本模組的三條紀律：

1. **逾期偵測是核心價值**。採購訂單寄出後就被遺忘，直到生產線停工才發現，
   是中小企業最貴的一種沉默。每張逾期 PO 都必須現形。
2. **供應商回覆解析失敗必須警報**，不可靜默當成「這張 PO 沒有更新」。
3. **催辦信預設是草稿**。對外寄給供應商的信，語氣直接影響商業關係與下次議價，
   一定要人看過才送。要開自動送出必須填白名單，且書中鐵律要求先跑滿 14 天草稿。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

import orders as order_lib  # noqa: E402
from orders import (  # noqa: E402
    ALERT_LABEL,
    ALERT_MARK,
    OVERDUE_ALERTS,
    Alert,
    OrderError,
    OrderTrack,
    SupplierEvent,
)
from supplier_mail import (  # noqa: E402
    MailboxError,
    ParseFailure,
    fetch_imap_messages,
    load_messages,
    parse_messages,
)

MODULE_ID = "20"
MODULE_NAME = "供應商通訊與訂單追蹤"
NOTIFY_CHANNELS = ("console", "telegram", "gmail", "line", "whatsapp")
DEFAULT_CONFIG = _DEMO_DIR / "config.yaml"
CHASER_PROMPT = _DEMO_DIR / "prompts" / "chaser_email.md"
SCORECARD_PROMPT = _DEMO_DIR / "prompts" / "scorecard.md"
CHASER_FIXTURE = _DEMO_DIR / "mock" / "chaser_email_fixture.md"
SCORECARD_FIXTURE = _DEMO_DIR / "mock" / "scorecard_fixture.md"
CONTEXT_NOTE = "讀者是中小企業採購。催貨信會直接寄給供應商，語氣影響下次議價。"

DISPATCH_SENT = "sent"
DISPATCH_DRAFT = "draft"
DISPATCH_DRY_RUN = "dry_run"

# 催辦信草稿的分隔線：--- PO-20260812 ---
CHASER_BLOCK_PATTERN = re.compile(r"^---\s*(PO-\S+?)\s*---\s*$", re.MULTILINE)

LIVE_REQUIRED_ENV = [
    "ERP_API_TOKEN",
    "SUPPLIER_MAILBOX_USER",
    "SUPPLIER_MAILBOX_PASSWORD",
]


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT.md §6）"""
    parser = argparse.ArgumentParser(
        prog="demo20-vendor-tracking", description="供應商通訊與訂單追蹤"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True,
                      help="離線模式：讀本地 ERP／信箱快照，不觸網、不呼叫 API（預設）")
    mode.add_argument("--live", dest="mock", action="store_false",
                      help="串接真實 ERP API、IMAP 信箱與 Claude API")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="跑完整流程但不發送任何信件、也不更新催辦狀態檔")
    parser.add_argument("--notify", choices=NOTIFY_CHANNELS, default=None,
                        help="通知管道，未指定時採用 config.yaml 的 runtime.notify_channel")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="設定檔路徑（預設為本目錄的 config.yaml）")
    parser.add_argument("--state-file", dest="state_file", default=None,
                        help="催辦狀態檔路徑，未指定時採用 config.yaml 的 tracking.state_file")
    return parser


@dataclass
class RunContext:
    """一次執行所需的環境：設定、時間、閘門、通道、狀態檔。"""

    args: argparse.Namespace
    diagnostics: Diagnostics
    config: dict
    tracking: dict
    runtime: dict
    tz: tzinfo
    tz_warning: str | None
    now: datetime
    gate: AutonomyGate
    client: LLMClient
    channel: str
    state_path: Path
    state: dict

    @property
    def is_mock(self) -> bool:
        """是否為離線模式"""
        return bool(self.args.mock)

    @property
    def is_dry_run(self) -> bool:
        """是否只跑流程不送出、不寫狀態檔"""
        return bool(self.args.dry_run)

    @property
    def period(self) -> str:
        """計分卡期間標籤"""
        return str(self.tracking.get("scorecard_period", ""))


@dataclass
class Findings:
    """一次執行的分析結果"""

    tracks: list[OrderTrack]
    alerts: list[Alert]
    events: list[SupplierEvent]
    failures: list[ParseFailure]


def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律相對於本 demo 目錄解析，避免受呼叫端的工作目錄影響"""
    path = Path(raw)
    return path if path.is_absolute() else (_DEMO_DIR / path)


def _build_gate(runtime: dict, diagnostics: Diagnostics) -> AutonomyGate:
    """依 config 的 runtime 區塊建立自主權閘門，並把警告轉成 AMBER"""
    raw_level = str(runtime.get("autonomy", "draft"))
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        diagnostics.red(
            symptom=f"runtime.autonomy 值不合法：{raw_level!r}",
            cause="設定檔寫了不存在的自主權層級",
            fix="改為 read_only / draft / supervised_auto 其中之一",
        )
        raise

    gate = AutonomyGate(
        level=level,
        approved_senders=list(runtime.get("approved_senders") or []),
        days_in_draft=int(runtime.get("days_in_draft", 0) or 0),
    )
    for warning in gate.warnings:
        diagnostics.amber(symptom=warning, fix="維持 draft 直到滿 14 天且客戶已簽核")
    return gate


def _require_env(name: str, diagnostics: Diagnostics) -> str:
    """讀取必要的環境變數；缺少走紅色警報，不用空字串靜默帶過"""
    value = os.environ.get(name)
    if not value:
        diagnostics.red(
            symptom=f"缺少環境變數 {name}",
            cause="live 模式需要 ERP／信箱憑證，設定檔中不得寫入明碼",
            fix=f'PowerShell 執行：setx {name} "<VALUE>" 後重開終端機',
        )
        raise OrderError(f"缺少環境變數 {name}")
    return value


def _resolve_now(tracking: dict, tz: tzinfo, is_mock: bool) -> datetime:
    """決定「現在」是什麼時候。

    mock 模式用設定檔中的固定時刻，逾期時數才會可重現；live 模式才用真實時間。
    """
    if not is_mock:
        return datetime.now(tz)
    raw = tracking.get("mock_now")
    if not raw:
        raise OrderError("mock 模式需要 tracking.mock_now，否則逾期判定無法重現")
    return order_lib.parse_timestamp(raw, tz)


def _load_state(path: Path, diagnostics: Diagnostics) -> dict:
    """讀取催辦狀態檔；損毀時走紅色警報，不靜默當成首次執行。"""
    try:
        return order_lib.load_state(path)
    except OrderError as exc:
        diagnostics.red(
            symptom=f"催辦狀態檔無法讀取：{exc}",
            cause="狀態檔在寫入過程中被中斷，或被手動改壞",
            fix=f"刪除或還原 {path} 後重跑（首次執行會視為沒有任何催辦紀錄）",
        )
        raise


def prepare(args: argparse.Namespace) -> RunContext:
    """組出執行環境：設定檔、時區、基準時刻、自主權閘門、狀態檔。"""
    diagnostics = Diagnostics(f"demo{MODULE_ID}-vendor-tracking", exit_on_red=False)
    config = load_config(_resolve_path(args.config),
                         required_env=None if args.mock else LIVE_REQUIRED_ENV)
    tracking = dict(config.get("tracking") or {})
    runtime = dict(config.get("runtime") or {})

    tz, tz_warning = order_lib.resolve_timezone(
        str(runtime.get("timezone", "UTC")), float(runtime.get("utc_offset_hours", 0) or 0)
    )
    if tz_warning:
        diagnostics.amber(symptom=tz_warning, fix="python -m pip install tzdata 後重跑")

    state_path = _resolve_path(args.state_file or tracking.get("state_file", "state/orders.json"))
    return RunContext(
        args=args, diagnostics=diagnostics, config=config, tracking=tracking, runtime=runtime,
        tz=tz, tz_warning=tz_warning, now=_resolve_now(tracking, tz, bool(args.mock)),
        gate=_build_gate(runtime, diagnostics),
        client=LLMClient(mock=bool(args.mock), context_note=CONTEXT_NOTE),
        channel=args.notify or str(runtime.get("notify_channel", "console")),
        state_path=state_path, state=_load_state(state_path, diagnostics),
    )


# ── 取得資料 ─────────────────────────────────────────────────────────
def _load_sources(ctx: RunContext) -> tuple[list[order_lib.PurchaseOrder], list[dict]]:
    """取得採購單與供應商郵件（mock 讀本地檔，live 走 ERP API + IMAP）。"""
    registry = order_lib.build_supplier_registry(ctx.config.get("suppliers") or [])
    zero_decimal = ctx.tracking.get("zero_decimal_currencies") or []
    if ctx.is_mock:
        return _load_mock_sources(ctx, registry, zero_decimal)
    return _load_live_sources(ctx, registry, zero_decimal)


def _load_mock_sources(ctx: RunContext, registry: dict, zero_decimal: list
                       ) -> tuple[list[order_lib.PurchaseOrder], list[dict]]:
    """離線模式：讀本目錄下的 ERP 匯出檔與信箱快照，零憑證零網路。"""
    purchase_orders = order_lib.load_orders(
        _resolve_path(ctx.tracking.get("erp_orders_file", "mock/purchase_orders.json")),
        registry, ctx.tz, zero_decimal,
    )
    messages = load_messages(
        _resolve_path(ctx.tracking.get("mailbox_file", "mock/supplier_replies.json"))
    )
    return purchase_orders, messages


def _load_live_sources(ctx: RunContext, registry: dict, zero_decimal: list
                       ) -> tuple[list[order_lib.PurchaseOrder], list[dict]]:
    """真實模式：ERP REST API 取未結 PO，IMAP 取 orders@ 信箱最近的信。"""
    timeout = int(ctx.tracking.get("request_timeout_seconds", 20))
    records = order_lib.fetch_erp_orders(
        str(ctx.tracking.get("erp_api_url", "")),
        _require_env("ERP_API_TOKEN", ctx.diagnostics), timeout,
    )
    messages = fetch_imap_messages(
        host=str(ctx.tracking.get("imap_host", "")),
        port=int(ctx.tracking.get("imap_port", 993)),
        user=_require_env("SUPPLIER_MAILBOX_USER", ctx.diagnostics),
        password=_require_env("SUPPLIER_MAILBOX_PASSWORD", ctx.diagnostics),
        folder=str(ctx.tracking.get("imap_folder", "INBOX")),
        limit=int(ctx.tracking.get("imap_fetch_limit", 50)),
        timeout=timeout,
    )
    return [
        order_lib.build_order(record, registry, ctx.tz, zero_decimal) for record in records
    ], messages


def _report_parse_failures(diagnostics: Diagnostics, failures: list[ParseFailure]) -> None:
    """每一封讀不懂的供應商回覆都要轉成 AMBER。

    這是本模組最容易出現的無聲故障：把「讀不懂」當成「沒有更新」，
    系統就會每天回報一切正常，直到料沒到、產線停了才被發現。
    """
    for failure in failures:
        diagnostics.amber(
            symptom=f"郵件 {failure.email_id}（{failure.subject}）解析失敗：{failure.reason}",
            fix="人工開信確認後手動更新該 PO 狀態；若為新範本請補進分類關鍵字",
        )


def _report_anomalies(diagnostics: Diagnostics, tracks: list[OrderTrack]) -> None:
    """狀態機的倒退事件也要現形，不可靜默忽略"""
    for track in tracks:
        for anomaly in track.anomalies:
            diagnostics.amber(
                symptom=f"{track.order.po_number} 狀態異常：{anomaly}",
                fix="確認供應商是否重寄舊信，或郵件時間戳有誤",
            )


def analyse(ctx: RunContext) -> Findings:
    """讀資料 → 解析郵件 → 套狀態機 → 判定逾期。"""
    purchase_orders, messages = _load_sources(ctx)
    if not purchase_orders:
        ctx.diagnostics.red(
            symptom="沒有任何未結採購單",
            cause="ERP 匯出檔為空，或 API 篩選條件把所有 PO 都排除了",
            fix="確認 tracking.erp_orders_file / tracking.erp_api_url 的內容",
        )
    events, failures = parse_messages(messages, purchase_orders, ctx.tz)
    _report_parse_failures(ctx.diagnostics, failures)
    tracks = order_lib.build_tracks(purchase_orders, events)
    _report_anomalies(ctx.diagnostics, tracks)
    return Findings(
        tracks=tracks,
        alerts=order_lib.evaluate_all(tracks, ctx.now, ctx.tracking),
        events=events,
        failures=failures,
    )


# ── 催辦信 ───────────────────────────────────────────────────────────
def _build_chaser_input(alerts: list[Alert], tracks_by_po: dict[str, OrderTrack]) -> str:
    """組出餵給提示詞的結構化資料（每張 PO 一段）"""
    blocks: list[str] = []
    for alert in alerts:
        track = tracks_by_po[alert.po_number]
        order = track.order
        blocks.append(
            f"PO: {order.po_number}\n"
            f"SUPPLIER: {order.supplier.name}\n"
            f"CONTACT: {order.supplier.contact}\n"
            f"ITEM: {order.description}\n"
            f"AMOUNT: {order.currency} {order.amount}\n"
            f"STAGE: {order_lib.STAGE_LABEL[track.stage]}\n"
            f"ALERT: {ALERT_LABEL[alert.kind]}\n"
            f"DETAIL: {alert.detail}"
        )
    return "\n\n".join(blocks) if blocks else "（本次沒有需要催辦的 PO）"


def split_chaser_blocks(text: str) -> dict[str, str]:
    """把 LLM 回應依 `--- PO-XXXX ---` 切成 {po_number: 內文}。"""
    parts = CHASER_BLOCK_PATTERN.split(text)
    blocks: dict[str, str] = {}
    # split 後結構固定為 [前言, PO, 內文, PO, 內文, ...]
    for index in range(1, len(parts) - 1, 2):
        blocks[parts[index].strip().upper()] = parts[index + 1].strip()
    return blocks


def _fallback_body(alert: Alert, track: OrderTrack) -> str:
    """LLM 沒有替某張 PO 產出草稿時的保底內容（寧可制式，也不能無聲漏催）。"""
    order = track.order
    return (
        f"Dear {order.supplier.name},\n\n"
        f"We are following up on purchase order {order.po_number} "
        f"({order.description}, {order.currency} {order.amount}).\n"
        f"Status: {ALERT_LABEL[alert.kind]} — {alert.detail}\n\n"
        "Could you confirm the current status and the committed delivery date?\n\n"
        "Best regards,\nProcurement"
    )


def _draft_chasers(ctx: RunContext, alerts: list[Alert],
                   tracks_by_po: dict[str, OrderTrack]) -> list[dict[str, Any]]:
    """呼叫 LLM 一次產出所有催辦信草稿，再依 PO 拆回各自的信件。"""
    if not alerts:
        return []
    raw = ctx.client.complete(
        system=CHASER_PROMPT.read_text(encoding="utf-8"),
        user=_build_chaser_input(alerts, tracks_by_po),
        max_tokens=1600,
        fixture=CHASER_FIXTURE if ctx.is_mock else None,
    )
    blocks = split_chaser_blocks(raw)
    return [_one_draft(ctx, alert, tracks_by_po[alert.po_number], blocks) for alert in alerts]


def _one_draft(ctx: RunContext, alert: Alert, track: OrderTrack,
               blocks: dict[str, str]) -> dict[str, Any]:
    """組出單封催辦信草稿；LLM 漏掉這張 PO 就退回制式範本並警報。"""
    body = blocks.get(alert.po_number)
    if body is None:
        ctx.diagnostics.amber(
            symptom=f"{alert.po_number} 沒有拿到 LLM 草稿，已改用制式範本",
            fix="檢查 prompts/chaser_email.md 是否要求逐張 PO 以分隔線輸出",
        )
        body = _fallback_body(alert, track)
    return {
        "po_number": alert.po_number,
        "supplier_name": alert.supplier_name,
        "recipient": track.order.supplier.contact,
        "subject": f"{ALERT_MARK[alert.kind]} {alert.po_number} — {ALERT_LABEL[alert.kind]}",
        "body": body,
        "alert_kind": alert.kind,
        "is_overdue": alert.kind in OVERDUE_ALERTS,
    }


def _dispatch_chaser(ctx: RunContext, draft: dict[str, Any]) -> dict[str, Any]:
    """依自主權層級決定送出或留為草稿，回傳補上 status 的草稿。"""
    if ctx.is_dry_run:
        status = DISPATCH_DRY_RUN
    elif ctx.gate.can_send(str(draft["recipient"])):
        status = DISPATCH_SENT
    else:
        status = DISPATCH_DRAFT

    # mock 模式只做判定不真的寄信；live 模式才交給 Notifier
    is_transmitted = False
    if status == DISPATCH_SENT and not ctx.is_mock:
        is_transmitted = Notifier(ctx.channel).send(
            str(draft["body"]), subject=str(draft["subject"])
        )
    return {**draft, "status": status, "is_transmitted": is_transmitted}


def _filter_by_cooldown(ctx: RunContext,
                        alerts: list[Alert]) -> tuple[list[Alert], list[dict[str, str]]]:
    """把還在冷卻期內的 PO 從催辦名單移除，避免每日排程重複轟炸供應商。"""
    cooldown = float(ctx.tracking.get("chaser_cooldown_hours", 48))
    due: list[Alert] = []
    suppressed: list[dict[str, str]] = []
    for alert in alerts:
        entry = ctx.state["chasers"].get(alert.po_number)
        if not order_lib.is_within_cooldown(entry, ctx.now, cooldown, ctx.tz):
            due.append(alert)
            continue
        suppressed.append({
            "po_number": alert.po_number,
            "supplier_name": alert.supplier_name,
            "last_chased_at": str(entry.get("last_chased_at")),
            "reason": f"仍在 {cooldown:g} 小時催辦冷卻期內",
        })
    return due, suppressed


def make_chasers(ctx: RunContext,
                 findings: Findings) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """冷卻期過濾 → LLM 產草稿 → 依自主權決定送出或留草稿。"""
    due, suppressed = _filter_by_cooldown(ctx, findings.alerts)
    tracks_by_po = {track.order.po_number: track for track in findings.tracks}
    drafts = _draft_chasers(ctx, due, tracks_by_po)
    return [_dispatch_chaser(ctx, draft) for draft in drafts], suppressed


def _next_chaser_state(ctx: RunContext, chasers: list[dict[str, Any]]) -> dict[str, Any]:
    """把本次實際產生的催辦（含草稿）記進狀態檔的冷卻紀錄。"""
    updated = dict(ctx.state["chasers"])
    for chaser in chasers:
        if chaser["status"] == DISPATCH_DRY_RUN:
            continue
        entry = updated.get(chaser["po_number"])
        count = int(entry.get("count", 0)) + 1 if isinstance(entry, dict) else 1
        updated[chaser["po_number"]] = {"count": count, "last_chased_at": ctx.now.isoformat()}
    return updated


# ── 報告 ─────────────────────────────────────────────────────────────
def _alert_lines(alerts: list[Alert]) -> list[str]:
    """逾期與追蹤區塊的明細行"""
    if not alerts:
        return ["  （本次沒有逾期或需要追蹤的 PO）"]
    return [
        f"  {ALERT_MARK[alert.kind]} {alert.po_number}｜{alert.supplier_name}｜"
        f"{ALERT_LABEL[alert.kind]}\n        {alert.detail}"
        for alert in alerts
    ]


def _chaser_lines(chasers: list[dict[str, Any]]) -> list[str]:
    """催辦信草稿區塊（草稿全文照印，採購要能直接複製寄出）"""
    if not chasers:
        return ["  （本次沒有催辦信）"]
    lines: list[str] = []
    for chaser in chasers:
        lines.append(
            f"  [{chaser['status']}] 收件人 {chaser['recipient']}｜主旨：{chaser['subject']}"
        )
        lines += [f"        {line}" for line in str(chaser["body"]).splitlines()]
    return lines


def _failure_lines(failures: list[ParseFailure]) -> list[str]:
    """解析失敗區塊。標題刻意寫死「不等於沒有更新」，避免被當成雜訊略過。"""
    if not failures:
        return []
    lines = [f"── {len(failures)} 封供應商回覆解析失敗（不等於「沒有更新」，請人工確認）──"]
    lines += [
        f"  x {failure.email_id}｜{failure.sender}｜{failure.subject}\n        {failure.reason}"
        for failure in failures
    ]
    return lines


def _scorecard_lines(scorecard: list[dict[str, Any]], period: str) -> list[str]:
    """計分卡區塊。沒有母體的比率印「—」，不印 0%（那是兩件不同的事）。"""
    lines = [f"── 供應商計分卡（{period}）──"]
    for row in scorecard:
        rate = (f"{row['on_time_rate']}%（{row['on_time']}/{row['delivered']} 到貨）"
                if row["on_time_rate"] is not None else "—（尚無到貨紀錄）")
        ack = (f"{row['avg_acknowledgement_hours']} 小時"
               if row["avg_acknowledgement_hours"] is not None else "—（尚無確認回覆）")
        lines.append(
            f"  {row['supplier_name']}：準時率 {rate}｜平均確認 {ack}｜未結 {row['open_orders']} 張"
        )
    return lines


def _render_scorecard_note(ctx: RunContext, scorecard: list[dict[str, Any]]) -> str:
    """呼叫 LLM 把計分卡數字寫成可以拿去議價的一段話"""
    rows = "\n".join(
        f"- {row['supplier_name']}｜訂單 {row['orders']}｜到貨 {row['delivered']}｜"
        f"準時 {row['on_time']}｜準時率 {row['on_time_rate']}｜"
        f"平均確認時數 {row['avg_acknowledgement_hours']}"
        for row in scorecard
    )
    return ctx.client.complete(
        system=SCORECARD_PROMPT.read_text(encoding="utf-8"),
        user=f"PERIOD: {ctx.period}\nROWS:\n{rows or '- （無資料）'}",
        max_tokens=700,
        fixture=SCORECARD_FIXTURE if ctx.is_mock else None,
    )


def build_report(ctx: RunContext, findings: Findings, chasers: list[dict[str, Any]],
                 suppressed: list[dict[str, str]], scorecard: list[dict[str, Any]],
                 scorecard_note: str) -> str:
    """組出最終要發送給採購／生管的內部報告全文。"""
    overdue = [alert for alert in findings.alerts if alert.kind in OVERDUE_ALERTS]
    outstanding = order_lib.totals_by_currency(findings.tracks, only_open=True)
    blocks = [
        f"【供應商訂單追蹤】{ctx.now:%Y-%m-%d %H:%M %z}\n"
        f"追蹤 {len(findings.tracks)} 張 PO｜{len(overdue)} 張逾期需催辦｜"
        f"{len(findings.alerts) - len(overdue)} 張進入交期前追蹤窗｜"
        f"{len(findings.failures)} 封供應商回覆解析失敗",
        "", "── 逾期與追蹤 ──", *_alert_lines(findings.alerts), "",
        f"── 催辦信草稿（{len(chasers)} 封）──", *_chaser_lines(chasers), "",
    ]
    if suppressed:
        blocks.append(f"── {len(suppressed)} 張 PO 因冷卻期未重複催辦 ──")
        blocks += [f"  - {item['po_number']}｜{item['supplier_name']}｜{item['reason']}"
                   for item in suppressed]
        blocks.append("")
    if findings.failures:
        blocks += [*_failure_lines(findings.failures), ""]
    joined = "／".join(f"{code} {amount}" for code, amount in outstanding.items())
    blocks += ["── 未結採購金額（幣別不混加）──", f"  {joined or '（無未結採購）'}", ""]
    blocks += [*_scorecard_lines(scorecard, ctx.period), "", scorecard_note.strip()]
    return "\n".join(blocks)


# ── 收尾 ─────────────────────────────────────────────────────────────
def _deliver_report(ctx: RunContext, report: str, alert_count: int) -> bool:
    """把內部彙整報告送到指定通道。

    這份報告是給自家採購看的，不是對外信件，因此不受自主權閘門限制；
    真正受閘門管制的是每一封要寄給供應商的催辦信。
    """
    if ctx.is_dry_run:
        return False
    return Notifier(ctx.channel).send(
        report, subject=f"供應商訂單追蹤｜{alert_count} 張需處理"
    )


def _persist(ctx: RunContext, chasers: list[dict[str, Any]], events: list[SupplierEvent]) -> None:
    """寫回催辦狀態檔（dry-run 不寫，避免空跑吃掉真正的催辦冷卻期）"""
    if ctx.is_dry_run:
        return
    order_lib.save_state(
        ctx.state_path,
        _next_chaser_state(ctx, chasers),
        list(ctx.state["processed_email_ids"]) + [event.email_id for event in events],
        ctx.now,
    )


def _build_result(ctx: RunContext, findings: Findings, chasers: list[dict[str, Any]],
                  suppressed: list[dict[str, str]], scorecard: list[dict[str, Any]],
                  report: str, is_notified: bool) -> dict[str, Any]:
    """組出供測試斷言與下游使用的結果 dict（全部欄位皆可 JSON 序列化）"""
    overdue = [alert for alert in findings.alerts if alert.kind in OVERDUE_ALERTS]
    warnings = list(ctx.gate.warnings) + ([ctx.tz_warning] if ctx.tz_warning else [])
    return {
        "module_id": MODULE_ID,
        "module_name": MODULE_NAME,
        "mode": "mock" if ctx.is_mock else "live",
        "dry_run": ctx.is_dry_run,
        "now": ctx.now.isoformat(),
        "timezone": str(ctx.tz),
        "orders": [track.as_dict() for track in findings.tracks],
        "alerts": [alert.as_dict() for alert in findings.alerts],
        "overdue_count": len(overdue),
        "chasers": chasers,
        "suppressed_chasers": suppressed,
        "parse_failures": [failure.as_dict() for failure in findings.failures],
        "scorecard": scorecard,
        "outstanding_by_currency": order_lib.totals_by_currency(findings.tracks, only_open=True),
        "report": report,
        "notified": is_notified,
        "notify_channel": ctx.channel,
        "state_file": str(ctx.state_path),
        "warnings": warnings,
        "amber_count": ctx.diagnostics.amber_count,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程並回傳結果 dict（不做 sys.exit，交給 main() 決定退出碼）"""
    ctx = prepare(args)
    findings = analyse(ctx)
    chasers, suppressed = make_chasers(ctx, findings)
    scorecard = order_lib.build_scorecard(findings.tracks, ctx.period)
    report = build_report(
        ctx, findings, chasers, suppressed, scorecard, _render_scorecard_note(ctx, scorecard)
    )
    is_notified = _deliver_report(ctx, report, len(findings.alerts))
    _persist(ctx, chasers, findings.events)
    if not findings.failures and not any(a.kind in OVERDUE_ALERTS for a in findings.alerts):
        ctx.diagnostics.green(
            f"{len(findings.tracks)} 張 PO 全部在期程內，供應商回覆全數解析成功"
        )
    return _build_result(ctx, findings, chasers, suppressed, scorecard, report, is_notified)


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳退出碼。

    退出碼約定（讓排程器能分辨「壞掉」與「有事要看」）：
        0 = 全部 PO 都在期程內，且供應商回覆全數解析成功
        2 = 流程完成，但有逾期 PO 或有回覆解析失敗（需人工處理）
        1 = 紅色警報或致命錯誤，本次追蹤沒有結果
    """
    args = build_parser().parse_args()
    try:
        result = run(args)
    except RedAlert as exc:
        print(f"紅色警報：{exc}", file=sys.stderr)
        return 1
    except (OrderError, MailboxError, FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    # dry-run 不經 Notifier，報告要自己印出來才看得到
    if result["dry_run"]:
        print(result["report"])
    print(
        f"\n完成：{len(result['orders'])} 張 PO｜{result['overdue_count']} 張逾期｜"
        f"{len(result['parse_failures'])} 封回覆解析失敗｜"
        f"{len(result['chasers'])} 封催辦信（mode={result['mode']}）",
        file=sys.stderr,
    )
    return 2 if (result["overdue_count"] or result["parse_failures"]) else 0


if __name__ == "__main__":
    sys.exit(main())
