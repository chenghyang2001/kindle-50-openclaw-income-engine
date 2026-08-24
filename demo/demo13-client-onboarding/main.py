"""demo13 客戶入職工作流主流程（第 05 章 #13 / 附錄 F p10）。

把「簽約後 1-3 天才發歡迎信 -> 手動來回約 Kick-off -> 忘記 30 天回顧」這條
每位客戶吃掉 8 小時、且會讓 90 天流失率增加 3 倍的流程，換成一條
**冪等、可中斷、可續跑**的 Day 0 → Day 30 有序管線：

    CRM webhook（Closed Won）
        -> Day 0  歡迎包（個人化歡迎信 + Slack #new-clients 通知）
        -> Day 0  Calendly 啟動會議連結
        -> Day 3  啟動會議
        -> Day 7  主動關心
        -> Day 30 成效回顧
        -> 結案

每一輪執行都可以安全重跑：狀態檔記錄「哪個階段確實交付過」，
CRM webhook 重送、排程重跑、同批次重複觸發都不會讓客戶收到第二封歡迎信。

用法：
    python main.py --mock                       # 零憑證零網路，重播 mock/clients.json
    python main.py --mock --dry-run             # 跑完流程但不發通知、不寫狀態檔
    python main.py --mock --state-file /tmp/s.json
    python main.py --live --notify telegram
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

from onboarding import (  # noqa: E402
    StageOutcome,
    StageResult,
    StageSpec,
    build_template_context,
    days_elapsed,
    financial_summary,
    load_sequence,
    missing_required,
    render_stage_messages,
    seconds_since,
)
from state_machine import (  # noqa: E402
    ClientOnboarding,
    OnboardingEvent,
    OnboardingState,
    OnboardingStore,
)

MODULE_NAME = "demo13-client-onboarding"


@dataclass
class OnboardingContext:
    """一次執行共用的相依物件。集中傳遞，避免每個 handler 都吃 8 個參數。"""

    config: dict[str, Any]
    stages: tuple[StageSpec, ...]
    templates: dict[str, str]
    gate: AutonomyGate
    diagnostics: Diagnostics
    llm: LLMClient
    prompts: dict[str, str]
    now: datetime
    is_mock: bool


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6）"""
    parser = argparse.ArgumentParser(description="demo13 客戶入職工作流")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實 API 與通知通道")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不發送、不寫狀態檔")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="通知通道，預設 console",
    )
    parser.add_argument("--config", default=None, help="設定檔路徑，預設同目錄 config.yaml")
    parser.add_argument(
        "--state-file",
        default=None,
        help="入職狀態檔路徑，預設取自 config 的 state.store_file（相對於模組目錄）。"
        "這個檔案是冪等性的憑據，刪掉它所有客戶都會被重寄歡迎信；"
        "測試與 CI 請指到暫存目錄，避免污染工作樹。",
    )
    return parser


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #
def _resolve_path(raw: str) -> Path:
    """把設定檔中的相對路徑接到本模組目錄下（禁止硬編碼使用者路徑）"""
    path = Path(raw)
    return path if path.is_absolute() else MODULE_DIR / path


def _build_gate(runtime: dict[str, Any], diagnostics: Diagnostics) -> AutonomyGate:
    """依 runtime 設定建立自主權閘門，並把警告轉成 amber。"""
    gate = AutonomyGate(
        level=AutonomyLevel(runtime.get("autonomy", "draft")),
        approved_senders=runtime.get("approved_senders") or [],
        days_in_draft=int(runtime.get("days_in_draft", 0)),
    )
    for warning in gate.warnings:
        diagnostics.amber(warning, "延長草稿觀察期或補齊 approved_senders 白名單")
    return gate


def _build_context(
    config: dict[str, Any], args: argparse.Namespace, diagnostics: Diagnostics
) -> OnboardingContext:
    """組出 OnboardingContext：階段序列、樣板、自主權閘門、LLM、提示詞、基準時間。"""
    is_mock = bool(args.mock) and not bool(args.live)
    reference_now = config.get("onboarding", {}).get("reference_now")
    prompts = {
        key: _resolve_path(path).read_text(encoding="utf-8")
        for key, path in config.get("prompts", {}).items()
    }
    return OnboardingContext(
        config=config,
        stages=load_sequence(config),
        templates=dict(config.get("templates", {})),
        gate=_build_gate(config.get("runtime", {}), diagnostics),
        diagnostics=diagnostics,
        llm=LLMClient(mock=is_mock, context_note="客戶入職信件，只潤飾語氣，不得新增事實"),
        prompts=prompts,
        now=datetime.fromisoformat(reference_now) if is_mock else datetime.now(),
        is_mock=is_mock,
    )


def _load_triggers(config: dict[str, Any]) -> list[dict[str, Any]]:
    """讀取待處理的 CRM 觸發佇列。檔案缺失要明確報錯，不可靜默當成沒有新客戶。"""
    path = _resolve_path(config["mock"]["clients_file"])
    if not path.exists():
        raise FileNotFoundError(f"找不到 CRM 觸發檔：{path.resolve()}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"CRM 觸發檔格式錯誤：{path}（{exc}）") from exc
    return list(data.get("triggers", []))


def _bootstrap(client: dict[str, Any], trigger: dict[str, Any]) -> ClientOnboarding:
    """狀態檔裡沒有這位客戶時，用 CRM 帶來的既有進度建立狀態機。"""
    crm_state = trigger.get("crm_state") or {}
    raw_state = str(crm_state.get("state", OnboardingState.NEW.value))
    try:
        state = OnboardingState(raw_state)
    except ValueError as exc:
        raise ValueError(
            f"客戶 {client.get('client_id')} 的 CRM 狀態 {raw_state!r} 不在狀態機定義內"
        ) from exc
    return ClientOnboarding(
        client_id=str(client["client_id"]),
        company=str(client.get("company", "")),
        contact_email=str(client.get("contact_email", "")).strip(),
        state=state,
        completed_stages=dict(crm_state.get("completed_stages") or {}),
    )


# --------------------------------------------------------------------------- #
# 階段處理
# --------------------------------------------------------------------------- #
def _result(stage: StageSpec, outcome: StageOutcome, reason: str, **extra: Any) -> StageResult:
    """StageResult 的簡短建構子（階段的三個描述欄位每次都一樣）。"""
    return StageResult(
        stage_key=stage.key,
        stage_name=stage.name,
        stage_day=stage.day,
        outcome=outcome,
        reason=reason,
        **extra,
    )


# 這些樣板是內部團隊用的通知（Slack 頻道訊息、內部會議提醒），不是寫給客戶的信。
# 絕對不能套用「你是客戶經理，正在寫信給客戶」這類提示詞去潤飾——LLM 會把整則
# 內部通知改寫成客戶信，聯絡人 email／客戶經理／自主權層級等內部追蹤欄位會全部消失。
# 這是實測 --live 真實重現過的 bug，見對應的 commit message。
_INTERNAL_ONLY_TEMPLATES = frozenset({"slack_new_client", "kickoff_meeting"})


def _polish(ctx: OnboardingContext, stage: StageSpec, template_name: str, text: str) -> str:
    """live 模式才把套版結果交給 LLM 依 prompts/*.md 潤飾語氣；mock 直接用套版輸出。

    內部專用樣板（見 _INTERNAL_ONLY_TEMPLATES）一律跳過 LLM 潤飾、原樣送出——
    這些是給內部團隊看的固定格式通知，不需要「潤飾語氣」，套用客戶信提示詞
    只會讓 LLM 把內部追蹤欄位整個改寫掉。
    """
    if ctx.is_mock or template_name in _INTERNAL_ONLY_TEMPLATES:
        return text
    prompt_key = "welcome_file" if stage.key == "welcome_pack" else "checkin_file"
    system = ctx.prompts.get(prompt_key, "")
    return ctx.llm.complete(system=system, user=text, max_tokens=800).strip() or text


def _check_welcome_sla(ctx: OnboardingContext, stage: StageSpec, client: dict[str, Any]) -> None:
    """書中承諾：轉為 Closed Won 後 60 秒內寄出歡迎包。超時要看得見，不可默默慢下去。"""
    if stage.key != "welcome_pack":
        return
    sla = int(ctx.config.get("crm", {}).get("welcome_sla_seconds", 60))
    elapsed = seconds_since(str(client.get("closed_won_at", "")), ctx.now)
    if elapsed > sla:
        ctx.diagnostics.amber(
            f"{client.get('client_id')}：歡迎包在成交後 {elapsed} 秒才送出（SLA {sla} 秒）",
            "檢查 CRM webhook 是否進了重試佇列，或把排程間隔縮短到 1 分鐘以內",
        )


def _execute_stage(
    ctx: OnboardingContext,
    machine: ClientOnboarding,
    client: dict[str, Any],
    stage: StageSpec,
) -> StageResult:
    """實際交付一個階段：決定自主權 -> 套版 -> 潤飾 -> 記進冪等帳本。"""
    recipient = str(client.get("contact_email", "")).strip()
    is_auto_sent = ctx.gate.can_send(recipient)
    delivery = "自動送出" if is_auto_sent else "建立草稿待人工審查"
    context = build_template_context(
        ctx.config, ctx.stages, stage, client, delivery, ctx.gate.effective_level(recipient).value
    )
    messages, unresolved = render_stage_messages(ctx.templates, stage, context)
    if unresolved:
        ctx.diagnostics.amber(
            f"{machine.client_id}／{stage.name}：樣板有未替換的欄位 {', '.join(unresolved)}",
            "補齊該階段的 required_fields，或修正 templates 的佔位符名稱",
        )
    _check_welcome_sla(ctx, stage, client)
    at = ctx.now.isoformat(timespec="seconds")
    machine.mark_stage_done(stage.key, stage.event, at, last_owner=context["am_name"])
    return _result(
        stage,
        StageOutcome.SENT,
        reason=f"於 {at} 交付（{delivery}）",
        owner=str(context["am_name"]),
        idempotency_key=machine.idempotency_key(stage.key),
        messages=[_polish(ctx, stage, name, text) for name, text in messages],
        delivery=delivery,
    )


def _advance_stage(
    ctx: OnboardingContext,
    machine: ClientOnboarding,
    client: dict[str, Any],
    stage: StageSpec,
) -> StageResult:
    """判斷單一階段本輪該做什麼：已做過 / 時候未到 / 資料缺漏 / 交付。"""
    if machine.is_stage_done(stage.key):
        return _result(
            stage,
            StageOutcome.SKIPPED_DONE,
            reason=f"已於 {machine.stage_done_at(stage.key)} 交付，本輪冪等跳過",
            idempotency_key=machine.idempotency_key(stage.key),
        )
    elapsed = days_elapsed(str(client.get("closed_won_at", "")), ctx.now)
    if elapsed < stage.day:
        return _result(
            stage, StageOutcome.PENDING, reason=f"排定於 Day {stage.day}，目前 Day {elapsed}"
        )
    gaps = missing_required(client, stage.required_fields)
    if gaps:
        ctx.diagnostics.amber(
            f"{machine.client_id}（{machine.company}）／{stage.name} 卡關：缺少 {', '.join(gaps)}",
            "回 CRM 補齊欄位並指派客戶經理後重跑；本階段與其後所有階段都不會執行",
        )
        return _result(
            stage,
            StageOutcome.BLOCKED,
            reason=f"客戶資料缺漏 {len(gaps)} 項，無法交付",
            missing_fields=gaps,
        )
    return _execute_stage(ctx, machine, client, stage)


def _try_complete(ctx: OnboardingContext, machine: ClientOnboarding) -> None:
    """五個階段全數交付後結案。無法轉移時保持原狀，不強行改狀態。"""
    if not all(machine.is_stage_done(stage.key) for stage in ctx.stages):
        return
    if machine.can_apply(OnboardingEvent.COMPLETE):
        machine.apply(OnboardingEvent.COMPLETE, completed_at=ctx.now.isoformat(timespec="seconds"))


def _run_stages(
    ctx: OnboardingContext, machine: ClientOnboarding, client: dict[str, Any]
) -> list[StageResult]:
    """依序跑完整條入職管線。一旦有階段停下，其後階段一律標示「前置未完成」。

    刻意不跳過卡關階段直接做下一步：入職的價值來自順序本身，
    在客戶還沒收到歡迎信之前寄出第 7 天關心信，比什麼都不寄更傷。
    """
    results: list[StageResult] = []
    halted_reason = ""
    for stage in ctx.stages:
        if halted_reason:
            results.append(_result(stage, StageOutcome.PENDING, reason=halted_reason))
            continue
        result = _advance_stage(ctx, machine, client, stage)
        results.append(result)
        if result.outcome in (StageOutcome.PENDING, StageOutcome.BLOCKED):
            halted_reason = f"前置階段「{stage.name}」尚未完成（{result.reason}）"
    _try_complete(ctx, machine)
    return results


# --------------------------------------------------------------------------- #
# 觸發處理
# --------------------------------------------------------------------------- #
def _new_record(client: dict[str, Any], trigger: dict[str, Any]) -> dict[str, Any]:
    """一筆觸發的結果骨架。"""
    return {
        "client_id": str(client.get("client_id", "")),
        "company": str(client.get("company", "")),
        "scenario": str(trigger.get("scenario", "")),
        "contact_email": str(client.get("contact_email", "")).strip(),
        "closed_won_at": str(client.get("closed_won_at", "")),
        "is_duplicate_trigger": False,
        "skipped_reason": "",
        "stages": [],
        "final_state": "",
        # 預設值讓報表欄位恆定：重複觸發與被 webhook 過濾掉的客戶也有這兩格
        "autonomy_level": "",
        "is_auto_sent": False,
    }


def _guard_deal_stage(
    ctx: OnboardingContext,
    machine: ClientOnboarding,
    trigger: dict[str, Any],
    record: dict[str, Any],
) -> bool:
    """只有交易階段真的是 Closed Won 才啟動入職（webhook 的第一道過濾）。"""
    expected = str(ctx.config.get("crm", {}).get("trigger_deal_stage", "Closed Won"))
    actual = str(trigger.get("deal_stage", ""))
    if actual != expected:
        reason = f"交易階段為 {actual!r}，非 {expected!r}，不啟動入職"
        ctx.diagnostics.amber(f"{machine.client_id}：{reason}", "檢查 CRM webhook 的過濾條件")
        record["skipped_reason"] = reason
        return False
    if machine.state is OnboardingState.NEW:
        machine.apply(OnboardingEvent.DEAL_CLOSED_WON, deal_stage=actual)
    return True


def _process_trigger(
    ctx: OnboardingContext,
    trigger: dict[str, Any],
    known: dict[str, ClientOnboarding],
    seen: set[str],
) -> dict[str, Any]:
    """處理一次 CRM 觸發。重複的 client_id 一律擋在最外層（冪等第一道防線）。"""
    client = dict(trigger.get("client") or {})
    client_id = str(client.get("client_id", "")).strip()
    if not client_id:
        raise ValueError("CRM 觸發缺少 client_id，無法保證冪等性，拒絕處理")
    record = _new_record(client, trigger)
    if client_id in seen:
        record["is_duplicate_trigger"] = True
        record["skipped_reason"] = "本輪已處理過同一個 client_id（CRM webhook 重送）"
        ctx.diagnostics.amber(
            f"{client_id}：{record['skipped_reason']}", "確認 CRM webhook 的重試設定與去重鍵"
        )
        return record
    seen.add(client_id)
    machine = known.get(client_id) or _bootstrap(client, trigger)
    known[client_id] = machine
    if not _guard_deal_stage(ctx, machine, trigger, record):
        record["final_state"] = machine.state.value
        return record
    record["stages"] = [item.to_dict() for item in _run_stages(ctx, machine, client)]
    record["final_state"] = machine.state.value
    record["autonomy_level"] = ctx.gate.effective_level(record["contact_email"]).value
    record["is_auto_sent"] = ctx.gate.can_send(record["contact_email"])
    return record


# --------------------------------------------------------------------------- #
# 彙整與輸出
# --------------------------------------------------------------------------- #
def _count_outcome(records: list[dict[str, Any]], outcome: StageOutcome) -> int:
    """統計所有客戶中某一種階段結果的數量。"""
    return sum(
        1
        for record in records
        for stage in record["stages"]
        if stage["outcome"] == outcome.value
    )


def _blocked_details(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """卡關清單：誰、卡在哪一階段、缺哪些欄位。這份清單就是要人工處理的工單。"""
    return [
        {
            "client_id": record["client_id"],
            "company": record["company"],
            "stage_key": stage["stage_key"],
            "stage_name": stage["stage_name"],
            "missing_fields": stage["missing_fields"],
        }
        for record in records
        for stage in record["stages"]
        if stage["outcome"] == StageOutcome.BLOCKED.value
    ]


def _summary_line(record: dict[str, Any]) -> str:
    """單一客戶在摘要中的那一行。"""
    if record["is_duplicate_trigger"]:
        return f"- {record['client_id']} {record['company']}：重複觸發，已略過"
    sent = sum(1 for stage in record["stages"] if stage["outcome"] == StageOutcome.SENT.value)
    blocked = [
        stage["stage_name"]
        for stage in record["stages"]
        if stage["outcome"] == StageOutcome.BLOCKED.value
    ]
    tail = f"｜卡關：{'、'.join(blocked)}" if blocked else ""
    return (
        f"- {record['client_id']} {record['company']}：狀態 {record['final_state']}"
        f"｜本輪交付 {sent} 個階段{tail}"
    )


def _build_summary(config: dict[str, Any], records: list[dict[str, Any]]) -> str:
    """給通知通道用的純文字摘要（不含 HTML，避免通道間差異）。"""
    module = config.get("module", {})
    lines = [
        f"[{module.get('id', '13')}] {module.get('name', MODULE_NAME)} 執行結果",
        f"觸發數：{len(records)}"
        f"｜本輪交付階段：{_count_outcome(records, StageOutcome.SENT)}"
        f"｜冪等跳過：{_count_outcome(records, StageOutcome.SKIPPED_DONE)}"
        f"｜卡關：{_count_outcome(records, StageOutcome.BLOCKED)}",
        "",
    ]
    lines.extend(_summary_line(record) for record in records)
    blocked = _blocked_details(records)
    if blocked:
        lines.append("")
        lines.append("需人工補件：")
        lines.extend(
            f"  * {item['company']}／{item['stage_name']}：缺 {', '.join(item['missing_fields'])}"
            for item in blocked
        )
    return "\n".join(lines)


def _notify(
    args: argparse.Namespace,
    config: dict[str, Any],
    summary: str,
    diagnostics: Diagnostics,
) -> bool:
    """送出摘要。--dry-run 一律不送，但要說清楚是誰決定不送的。"""
    if args.dry_run:
        diagnostics.green("--dry-run：略過發送")
        return False
    channel = args.notify or config.get("runtime", {}).get("notify_channel", "console")
    notifier = Notifier(channel=channel, config=config.get("channel", {}))
    return notifier.send(summary, subject="客戶入職工作流每日摘要")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config_path = Path(args.config) if args.config else MODULE_DIR / "config.yaml"
    config = load_config(config_path)
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=False)
    ctx = _build_context(config, args, diagnostics)
    # CLI 覆寫優先，沒給才回頭取 config；--state-file 預設 None，不會把 config 值蓋掉
    store = OnboardingStore(_resolve_path(args.state_file or config["state"]["store_file"]))
    known = store.load()
    seen: set[str] = set()
    records = [
        _process_trigger(ctx, trigger, known, seen) for trigger in _load_triggers(config)
    ]
    summary = _build_summary(config, records)
    return {
        "module_id": config.get("module", {}).get("id", "13"),
        "module_name": config.get("module", {}).get("name", MODULE_NAME),
        "mode": "mock" if ctx.is_mock else "live",
        "is_mock": ctx.is_mock,
        "dry_run": bool(args.dry_run),
        "clients": records,
        "stages_delivered": _count_outcome(records, StageOutcome.SENT),
        "stages_skipped_idempotent": _count_outcome(records, StageOutcome.SKIPPED_DONE),
        "stages_pending": _count_outcome(records, StageOutcome.PENDING),
        "stages_blocked": _count_outcome(records, StageOutcome.BLOCKED),
        "blocked_details": _blocked_details(records),
        "duplicate_triggers": sum(1 for record in records if record["is_duplicate_trigger"]),
        "financials": financial_summary(config.get("module", {})),
        "warnings": list(ctx.gate.warnings),
        "amber_count": diagnostics.amber_count,
        "summary": summary,
        "state_file": None if args.dry_run else str(store.save(known)),
        "is_notified": _notify(args, config, summary, diagnostics),
    }


def _render_report(result: dict[str, Any]) -> str:
    """人看的完整輸出：摘要 + 本輪實際送出的每一則訊息 + 財務模型。"""
    blocks = [result["summary"], ""]
    for record in result["clients"]:
        sent = [s for s in record["stages"] if s["outcome"] == StageOutcome.SENT.value]
        if not sent:
            continue
        blocks.append(f"=== {record['client_id']} {record['company']}（{record['scenario']}）===")
        for stage in sent:
            blocks.append(f"--- {stage['stage_name']}｜idem={stage['idempotency_key']} ---")
            blocks.extend(stage["messages"])
            blocks.append("")
    money = result["financials"]
    blocks.append(
        f"客戶端 ROI：設置 ${money['client_setup_price']} + ${money['client_monthly_price']}/月"
        f"｜每月回收價值 ${money['recovered_value_per_month']}"
        f"｜首月淨值 ${money['first_month_net']}｜回收期 {money['payback_months']} 個月"
    )
    blocks.append(
        f"amber：{result['amber_count']}｜重複觸發：{result['duplicate_triggers']}"
        f"｜狀態檔：{result['state_file'] or '（未寫入）'}"
    )
    return "\n".join(blocks)


def main() -> int:
    """解析參數 -> run() -> 印出結果 -> 回傳 exit code"""
    if hasattr(sys.stdout, "reconfigure"):
        # Windows 主控台預設 cp950，繁中輸出會炸；不依賴外部設定 PYTHONUTF8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(_render_report(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
