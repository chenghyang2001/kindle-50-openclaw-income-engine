"""demo23 — 董事會級財務報表自動化（Level 3 模組 #23）。

月結 close 之後 **T+1** 交出一份董事會品質的財務包：自動從唯讀會計 API 取數、
算出變異數、由 AI 寫出管理階層解讀、更新 12 個月三情境滾動預測，
交財務總監審核（SLA < 2 小時），核准後才於 T+3 對董事會發布。

**這個模組的靈魂是三條財務鐵律**，全部在程式層強制，不是寫在文件裡的期許：

1. **資料源一律唯讀**：Xero / QuickBooks / Sage / BambooHR 的 scope 在取數之前
   逐一驗證；出現任何寫入字樣即紅色警報中止。`--live` 的 HTTP 層再擋一次非 GET。
2. **未經財務總監核准不得發送給董事會**：核准綁定「這一份數字」的指紋，
   數字一改核准立即失效；未核准時報表一律標示「草稿・待財務總監審核」，
   收件人只有財務總監，董事會信箱拿不到任何東西。所有審核動作寫入稽核 JSONL。
3. **金額一律 `decimal.Decimal`，全檔禁 float，不同幣別不可混加。**

用法：

    python main.py --mock                         # 零憑證、零網路跑完（產出草稿）
    python main.py --mock --approve-as fd@example.com   # 模擬財務總監核准後再跑
    python main.py --mock --dry-run               # 產出但不發送、不寫入核准狀態
    python main.py --mock --state-file ./state/approval.json --audit-file ./audit/log.jsonl
    python main.py --live                         # 串真實唯讀 API（缺憑證會明確報錯退出）
"""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODULE_DIR = Path(__file__).resolve().parent
# demo/ 進 sys.path 才能匯入 _shared；demo23 自己也要進，
# 這樣 pytest 從別的目錄呼叫時仍找得到 sources / board_pack / forecaster。
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

import audit as audit_mod  # noqa: E402
import board_pack as pack_mod  # noqa: E402
import forecaster  # noqa: E402
import sources  # noqa: E402
from audit import AuditLog, content_fingerprint, resolve_audit_path  # noqa: E402

MODULE_NAME = "demo23-financial-reporting"

#: 第 04 章：附在 system prompt 尾端可減少約 40% 不相關輸出。
CONTEXT_NOTE = (
    "這是要送進董事會議事錄的財務報告，讀者是董事與投資人。"
    "只使用輸入 JSON 中實際存在的數字，缺失或失敗的資料源一律據實說明，"
    "禁止推估、補值或以行業經驗填補任何金額。"
)

#: 提示詞讀不到時的最低限度後備（表格本體仍有價值，不因少一段敘述就交不出報表）。
FALLBACK_PROMPTS = {
    "executive_summary": "你是財務長。用繁體中文寫 3 條關鍵財務標題與變異數摘要，只引用輸入中的數字。",
    "variance_narrative": "你是財務長。逐條解釋重大變異數成因，並指出時間差何時逆轉。",
}

#: 核准狀態機。
STATUS_APPROVED = "approved"
STATUS_PENDING = "pending"
STATUS_INVALIDATED = "invalidated"


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（契約 §6 的統一介面 + 本模組專屬旗標）。"""
    parser = argparse.ArgumentParser(
        prog="demo23-financial-reporting",
        description="董事會級財務報表：唯讀取數、變異數分析、三情境滾動預測、T+1 財務總監審核閘門。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True,
                      help="離線模式，讀 mock/*.json、不呼叫任何 API（預設）")
    mode.add_argument("--live", dest="mock", action="store_false",
                      help="串接真實唯讀 API；缺憑證會明確報錯退出，不會靜默退回 mock")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="跑完整流程並印出報表，但不發送、也不寫入核准狀態")
    parser.add_argument("--notify", choices=list(Notifier.SUPPORTED), default=None,
                        help="發送通道；未指定時取 config 的 runtime.notify_channel")
    parser.add_argument("--config", default=str(MODULE_DIR / "config.yaml"),
                        help="設定檔路徑（預設為本目錄的 config.yaml）")
    parser.add_argument("--state-file", dest="state_file", default=None,
                        help="核准狀態檔路徑（預設取 config 的 approval.state_file）")
    parser.add_argument("--audit-file", dest="audit_file", default=None,
                        help="稽核軌跡 JSONL 路徑（預設取 config 的 audit.file）")
    parser.add_argument("--approve-as", dest="approve_as", default=None,
                        help="以財務總監身分核准本期報表（必須等於 config 的 approval.fd_email）")
    return parser


# --------------------------------------------------------------------------
# 時間
# --------------------------------------------------------------------------


def resolve_timezone(name: str, fallback_offset_hours: int, diagnostics: Diagnostics) -> tzinfo:
    """用 zoneinfo 解析時區（禁用 pytz）。

    Windows 沒有系統時區資料庫，未安裝 `tzdata` 時 `ZoneInfo("Asia/Taipei")` 會拋
    `ZoneInfoNotFoundError`。本模組只允許 PyYAML + pytest 兩個第三方套件，
    因此改用固定 UTC 偏移後備並記琥珀燈——報表照出，但讓維運知道時區來源退化了。
    """
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        diagnostics.amber(
            f"時區 {name!r} 無法解析（{exc}），改用固定 UTC{fallback_offset_hours:+d} 偏移",
            "安裝 tzdata（pip install tzdata）或改用系統支援的時區名稱",
        )
        return timezone(timedelta(hours=int(fallback_offset_hours)))


def config_decimal(value: Any, field_name: str) -> Decimal:
    """把設定檔中的門檻值（百分比、小時數）轉成 Decimal。

    比金額寬鬆：容許 YAML 寫成裸數字。金額仍走 `sources.to_decimal` 的嚴格路徑
    （拒收 float），因為金額會被累加十幾次，門檻值只被比較一次。
    """
    try:
        return Decimal(str(value).strip())
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError(f"設定值 {field_name} 不是合法數字：{value!r}") from exc


def current_time(tz: tzinfo) -> datetime:
    """取得帶時區的現在時間。測試以 monkeypatch 覆寫本函式注入固定時間。"""
    return datetime.now(tz)


def resolve_period(reporting: dict, now: datetime) -> str:
    """決定報告期間。`auto` 表示「上一個月」（月結後才跑得出本模組的報表）。"""
    raw = str(reporting.get("period", "auto")).strip()
    if raw.lower() != "auto":
        return raw
    first_of_month = now.date().replace(day=1)
    previous = first_of_month - timedelta(days=1)
    return f"{previous.year:04d}-{previous.month:02d}"


def delivery_schedule(period: str, review_days: int, publish_days: int) -> tuple[date, date]:
    """由期間月底推算 T+1 財務總監審核日與 T+3 董事會發布日。"""
    year, month = (int(part) for part in period.split("-", 1))
    month_end = date(year, month, calendar.monthrange(year, month)[1])
    return month_end + timedelta(days=review_days), month_end + timedelta(days=publish_days)


# --------------------------------------------------------------------------
# 前置檢查（全域安全閥）
# --------------------------------------------------------------------------


def ensure_live_env(config: dict, diagnostics: Diagnostics) -> list[str]:
    """`--live` 時檢查必要環境變數；缺任何一個都走紅色警報退出。"""
    required = list((config.get("live") or {}).get("required_env") or [])
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="唯讀會計 API 憑證未設定或未匯入目前的 shell",
            fix=f"設定 {', '.join(missing)} 後重跑；或改用 --mock 離線驗證流程",
        )
    return required


def selftest(config: dict, is_mock: bool, diagnostics: Diagnostics, log: AuditLog) -> dict:
    """apxG_p03 全域安全閥：任何對外 API 呼叫之前先跑內部通訊測試。

    檢查三件事：資料源 scope 全為唯讀、收件人設定完整、`--live` 憑證到位。
    scope 違規在任何模式下都是紅色警報——那代表這份設定有能力寫壞客戶的總帳。
    """
    entries = list(config.get("sources") or [])
    for entry in entries:
        sources.assert_read_only_scope(str(entry.get("id", "未命名資料源")), entry.get("scope"))

    delivery = config.get("delivery") or {}
    recipients = [str(delivery.get("fd_email", ""))] + [str(x) for x in (delivery.get("board_emails") or [])]
    invalid = [item for item in recipients if "@" not in item]
    if invalid:
        diagnostics.red(
            symptom=f"收件人設定不合法：{invalid}",
            cause="delivery.fd_email / delivery.board_emails 未正確設定",
            fix="以 openclaw config set FD_EMAIL=<email>、BOARD_EMAILS=<a@x,b@y> 補齊",
        )

    required_env = ensure_live_env(config, diagnostics) if not is_mock else []
    checks = {
        "sources_read_only": len(entries),
        "recipients_valid": len(recipients),
        "live_env_verified": len(required_env),
        "mode": "mock" if is_mock else "live",
    }
    log.record(audit_mod.EVENT_DRY_RUN_SELFTEST, checks)
    log.record(audit_mod.EVENT_SCOPE_VERIFIED, {"scopes": [str(e.get("scope")) for e in entries]})
    return checks


def build_gate(runtime_cfg: dict, diagnostics: Diagnostics) -> AutonomyGate:
    """依 config 建立自主權閘門；設定有問題一律降級成 DRAFT 並記琥珀燈。"""
    raw_level = str(runtime_cfg.get("autonomy", "draft")).strip().lower()
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        diagnostics.amber(
            f"未知的自主權設定 {raw_level!r}，本次降級為 draft",
            "runtime.autonomy 只接受 read_only / draft / supervised_auto",
        )
        level = AutonomyLevel.DRAFT

    try:
        gate = AutonomyGate(
            level=level,
            approved_senders=list(runtime_cfg.get("approved_senders") or []),
            days_in_draft=int(runtime_cfg.get("days_in_draft", 0)),
        )
    except AutonomyError as exc:
        diagnostics.amber(f"自主權設定違規，本次降級為 draft：{exc}",
                          "supervised_auto 必須提供非空的 approved_senders")
        gate = AutonomyGate(level=AutonomyLevel.DRAFT)

    for warning in gate.warnings:
        diagnostics.amber(warning, "維持 draft 直到連續穩定運行滿 14 天且客戶簽核")
    return gate


# --------------------------------------------------------------------------
# 財務包
# --------------------------------------------------------------------------


def assemble_pack(
    config: dict, is_mock: bool, diagnostics: Diagnostics, log: AuditLog, period: str
) -> pack_mod.BoardPack:
    """取數 → 幣別守衛 → 組出董事會財務包，並把每個資料源的結果寫進稽核軌跡。"""
    reporting = config.get("reporting") or {}
    currency = str(reporting.get("currency", "USD"))

    facts, failures = pack_mod.collect(
        config.get("sources") or [], MODULE_DIR, sources.FETCHERS, diagnostics, is_mock
    )
    kept, rejected = pack_mod.enforce_single_currency(facts, currency, diagnostics)
    failures.extend(rejected)

    for fact in kept:
        log.record(audit_mod.EVENT_SOURCE_READ,
                   {"source_id": fact.source_id, "scope": fact.scope, "currency": fact.currency})
    for failure in failures:
        log.record(audit_mod.EVENT_SOURCE_FAILED, failure.to_dict())

    return pack_mod.build_board_pack(
        facts=kept,
        failures=failures,
        period=period,
        currency=currency,
        material_pct=config_decimal(
            (config.get("variance") or {}).get("material_pct", "5"), "variance.material_pct"
        ),
        min_days_cash=int((config.get("liquidity") or {}).get("min_days_cash", 60)),
    )


def build_rolling_forecast(
    config: dict, pack: pack_mod.BoardPack
) -> forecaster.RollingForecast:
    """以財務包的實際數為基礎，產出三情境 12 個月滾動預測。"""
    forecast_cfg = config.get("forecast") or {}
    pipeline = pack.pipeline
    cashflow = pack.cashflow
    cost_base = pack.totals[pack_mod.TOTAL_COGS].actual + pack.totals[pack_mod.TOTAL_OPEX].actual

    return forecaster.build_forecast(
        period=pack.period,
        currency=pack.currency,
        horizon_months=int(forecast_cfg.get("horizon_months", 12)),
        recurring_revenue=pipeline.monthly_recurring_revenue if pipeline else Decimal("0.00"),
        weighted_pipeline=pipeline.weighted_pipeline_value if pipeline else Decimal("0.00"),
        monthly_cost_base=cost_base,
        opening_cash=cashflow.closing_balance if cashflow else Decimal("0.00"),
        scenarios=forecaster.load_scenarios(forecast_cfg),
    )


def load_prompt(config: dict, key: str, diagnostics: Diagnostics) -> str:
    """讀 prompts/*.md；讀不到就用後備提示詞並記琥珀燈。"""
    rel = (config.get("prompts") or {}).get(key, f"prompts/{key}.md")
    path = MODULE_DIR / str(rel)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.amber(f"讀不到提示詞檔 {path}，改用後備提示詞：{exc}",
                          "確認 prompts/ 是否隨部署一起複製過去")
        return FALLBACK_PROMPTS[key]


def write_narratives(
    payload: str, config: dict, is_mock: bool, diagnostics: Diagnostics
) -> dict[str, str]:
    """呼叫 LLM 產生執行摘要與變異數敘述。mock 模式回傳佔位字串，零成本。"""
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    return {
        key: client.complete(
            system=load_prompt(config, key, diagnostics), user=payload, max_tokens=900
        )
        for key in ("executive_summary", "variance_narrative")
    }


# --------------------------------------------------------------------------
# 審核閘門（本模組的第二條鐵律）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalDecision:
    """本次執行的核准判定結果。"""

    status: str
    reason: str
    approved_by: str | None
    approved_at: str | None
    requested_at: str | None
    is_dispatch_allowed: bool

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構。"""
        return {
            "status": self.status,
            "reason": self.reason,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "requested_at": self.requested_at,
            "is_dispatch_allowed": self.is_dispatch_allowed,
        }


def resolve_state_path(cli_value: str | None, config_value: str) -> Path:
    """決定核准狀態檔位置：`--state-file` > config；相對路徑以模組目錄為基準。"""
    chosen = Path(os.path.expandvars(str(cli_value or config_value))).expanduser()
    return chosen if chosen.is_absolute() else (MODULE_DIR / chosen)


def load_state(path: Path) -> dict:
    """讀核准狀態檔。不存在視為「從未送審」；損毀則明確報錯（不可當成未送審）。"""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"核准狀態檔無法解析：{path}｜{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"核准狀態檔頂層必須是物件：{path}")
    return payload


def save_state(path: Path, state: dict) -> None:
    """寫回核准狀態檔。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"核准狀態檔無法寫入：{path}｜{exc}") from exc


def evaluate_approval(state: dict, period: str, fingerprint: str, is_partial: bool) -> ApprovalDecision:
    """判定本期報表能否對董事會發布。任何一項不符即維持草稿。"""
    requested_at = state.get("requested_at") if state.get("period") == period else None
    stub = dict(approved_by=None, approved_at=None, requested_at=requested_at)

    if is_partial:
        # 財務資料殘缺時，先前針對完整數字給出的核准當然不再成立。
        return ApprovalDecision(STATUS_INVALIDATED, "partial_data", is_dispatch_allowed=False, **stub)
    if not state:
        return ApprovalDecision(STATUS_PENDING, "no_state", is_dispatch_allowed=False, **stub)
    if state.get("period") != period:
        return ApprovalDecision(STATUS_PENDING, "period_mismatch", is_dispatch_allowed=False, **stub)
    if state.get("status") != STATUS_APPROVED:
        return ApprovalDecision(STATUS_PENDING, "awaiting_fd", is_dispatch_allowed=False, **stub)
    if state.get("fingerprint") != fingerprint:
        return ApprovalDecision(STATUS_INVALIDATED, "fingerprint_mismatch",
                                is_dispatch_allowed=False, **stub)

    return ApprovalDecision(
        status=STATUS_APPROVED,
        reason="approved",
        approved_by=str(state.get("approved_by") or ""),
        approved_at=str(state.get("approved_at") or ""),
        requested_at=requested_at,
        is_dispatch_allowed=True,
    )


def grant_approval(
    state_path: Path, approver: str, config: dict, period: str, fingerprint: str,
    now: datetime, log: AuditLog,
) -> dict:
    """記錄財務總監核准。核准人必須等於設定中的財務總監信箱，否則直接拒絕。"""
    fd_email = str((config.get("delivery") or {}).get("fd_email", "")).strip().lower()
    if approver.strip().lower() != fd_email:
        log.record(audit_mod.EVENT_APPROVAL_REJECTED,
                   {"attempted_by": approver, "expected": fd_email, "period": period}, actor=approver)
        raise ValueError(f"核准人 {approver} 不是設定中的財務總監（{fd_email}），拒絕核准")

    previous = load_state(state_path)
    state = {
        "period": period,
        "fingerprint": fingerprint,
        "status": STATUS_APPROVED,
        "approved_by": approver,
        "approved_at": now.isoformat(timespec="seconds"),
        "requested_at": previous.get("requested_at") or now.isoformat(timespec="seconds"),
    }
    save_state(state_path, state)
    log.record(audit_mod.EVENT_APPROVAL_GRANTED,
               {"period": period, "fingerprint": fingerprint}, actor=approver)
    return state


def persist_pending(
    state_path: Path, state: dict, period: str, fingerprint: str, now: datetime, log: AuditLog
) -> str:
    """把「已送審、待核准」寫回狀態檔並回傳送審時間（SLA 的起算點）。

    指紋改變代表這是一份新的數字，SLA 重新起算——否則舊的送審時間會讓一份
    剛產出的報表看起來已經逾期。
    """
    is_same_draft = state.get("period") == period and state.get("fingerprint") == fingerprint
    requested_at = state.get("requested_at") if is_same_draft else None
    requested_at = requested_at or now.isoformat(timespec="seconds")

    save_state(state_path, {
        "period": period,
        "fingerprint": fingerprint,
        "status": STATUS_PENDING,
        "approved_by": None,
        "approved_at": None,
        "requested_at": requested_at,
    })
    log.record(audit_mod.EVENT_APPROVAL_REQUESTED,
               {"period": period, "fingerprint": fingerprint, "requested_at": requested_at})
    return requested_at


def parse_timestamp(raw: Any, tz: tzinfo) -> datetime | None:
    """解析狀態檔中的 ISO 時間字串；沒有時區資訊時補上報表時區。"""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def check_sla(
    decision: ApprovalDecision, now: datetime, tz: tzinfo, sla_hours: Decimal,
    diagnostics: Diagnostics, log: AuditLog,
) -> bool:
    """檢查 T+1 審核 SLA（apxG_p08：`<2 小時`）。逾時發琥珀燈並寫稽核事件。"""
    if decision.is_dispatch_allowed:
        return False
    requested = parse_timestamp(decision.requested_at, tz)
    if requested is None:
        return False

    elapsed_hours = Decimal(str((now - requested).total_seconds())) / Decimal("3600")
    if elapsed_hours <= sla_hours:
        return False

    detail = {"requested_at": decision.requested_at, "elapsed_hours": str(round(elapsed_hours, 2)),
              "sla_hours": str(sla_hours)}
    log.record(audit_mod.EVENT_APPROVAL_SLA_BREACHED, detail)
    diagnostics.amber(
        f"財務總監審核逾時：已等待 {round(elapsed_hours, 2)} 小時（SLA {sla_hours} 小時）",
        "催辦財務總監，或改指派代理審核人；逾時期間報表一律維持草稿",
    )
    return True


def status_banner(decision: ApprovalDecision, watermark: str) -> str:
    """報告最上方的狀態橫幅：未核准一律加浮水印。"""
    if decision.is_dispatch_allowed:
        return f"✅ 已核准發布｜核准人：{decision.approved_by}｜核准時間：{decision.approved_at}"
    reasons = {
        "partial_data": "資料不完整，既有核准已作廢",
        "fingerprint_mismatch": "數字已變動，先前核准失效，需重新審核",
        "period_mismatch": "狀態檔屬於其他期間",
        "awaiting_fd": "等待財務總監核准",
        "no_state": "尚未送審",
    }
    return f"🕐 {watermark}｜原因：{reasons.get(decision.reason, decision.reason)}"


# --------------------------------------------------------------------------
# 發送
# --------------------------------------------------------------------------


def _split_recipients(gate: AutonomyGate, recipients: Iterable[str]) -> tuple[list[str], list[str]]:
    """依自主權閘門把收件人分成「可自動送出」與「須人工審核」。"""
    approved, held = [], []
    for recipient in recipients:
        (approved if gate.can_send(recipient) else held).append(recipient)
    return approved, held


def _delivery_result(delivered: bool, channel: str, reason: str,
                     recipients: list[str], held: list[str]) -> dict:
    return {"delivered": delivered, "channel": channel, "reason": reason,
            "recipients": recipients, "held_recipients": held}


def deliver(
    text: str, subject: str, channel: str, decision: ApprovalDecision, config: dict,
    gate: AutonomyGate, is_dry_run: bool, diagnostics: Diagnostics, log: AuditLog,
) -> dict:
    """未核准 → 只寄草稿給財務總監，董事會信箱一封都不寄（並寫入稽核）。"""
    delivery_cfg = config.get("delivery") or {}
    fd_email = str(delivery_cfg.get("fd_email", ""))
    board = [str(item) for item in (delivery_cfg.get("board_emails") or [])]
    recipients = board if decision.is_dispatch_allowed else [fd_email]
    event = audit_mod.EVENT_DISPATCH if decision.is_dispatch_allowed else audit_mod.EVENT_DISPATCH_BLOCKED

    if not decision.is_dispatch_allowed:
        log.record(event, {"blocked_recipients": board, "reason": decision.reason,
                           "sent_to_fd_for_review": fd_email})
    if is_dry_run:
        diagnostics.green("--dry-run：報表已產出但未發送，核准狀態未變更")
        return _delivery_result(False, channel, "dry-run", [], recipients)

    if channel == "console":
        # 印在本機終端不算「對外發送」，因此不受自主權閘門管制。
        ok = Notifier("console").send(text, subject=subject)
        if decision.is_dispatch_allowed:
            log.record(event, {"channel": channel, "recipients": recipients})
        return _delivery_result(ok, channel, "console-output", recipients, [])

    allowed, held = _split_recipients(gate, recipients)
    if not allowed:
        diagnostics.green("自主權為 draft：報表已產出為草稿，等待人工送出")
        return _delivery_result(False, channel, "autonomy_draft", [], held)

    ok = Notifier(channel).send(text, subject=subject)
    if decision.is_dispatch_allowed:
        log.record(event, {"channel": channel, "recipients": allowed})
    return _delivery_result(ok, channel, "sent" if ok else "notifier-failed", allowed, held)


def build_subject(pack: pack_mod.BoardPack, decision: ApprovalDecision, watermark: str) -> str:
    """通知主旨：狀態放最前面，手機通知列被截斷也讀得到「這還沒核准」。"""
    prefix = "" if decision.is_dispatch_allowed else f"[{watermark}] "
    partial = "⛔資料不完整 " if pack.is_partial else ""
    net = pack.totals[pack_mod.NET_PROFIT]
    return f"{prefix}{partial}董事會財務報告 {pack.period}｜淨利 {pack_mod.money(net.actual, pack.currency)}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _schedule_lines(period: str, approval_cfg: dict, sla_hours: Decimal) -> list[str]:
    """交付時序區塊（T+1 審核 → T+3 董事會發布）。"""
    review_day, publish_day = delivery_schedule(
        period, int(approval_cfg.get("review_offset_days", 1)),
        int(approval_cfg.get("publish_offset_days", 3)),
    )
    return [
        f"交付時序：T+1 財務總監審核 {review_day.isoformat()}（SLA < {sla_hours} 小時）"
        f" → T+3 董事會發布 {publish_day.isoformat()}",
    ]


def run(args: argparse.Namespace) -> dict:
    """執行主流程並回傳結果 dict（供測試斷言）。本函式不呼叫 sys.exit。"""
    diagnostics = Diagnostics(MODULE_NAME)
    config = load_config(Path(args.config).expanduser())
    reporting = config.get("reporting") or {}
    approval_cfg = config.get("approval") or {}

    tz = resolve_timezone(str(reporting.get("timezone", "Asia/Taipei")),
                          int(reporting.get("fallback_utc_offset_hours", 0)), diagnostics)
    now = current_time(tz)
    period = resolve_period(reporting, now)

    audit_path = resolve_audit_path(
        args.audit_file,
        str((config.get("audit") or {}).get("file", "audit/audit-log.jsonl")),
        MODULE_DIR,
    )
    log = AuditLog(audit_path, MODULE_NAME, tz)
    log.record(audit_mod.EVENT_RUN_STARTED,
               {"period": period, "mode": "mock" if args.mock else "live", "dry_run": bool(args.dry_run)})
    selftest(config, bool(args.mock), diagnostics, log)

    pack = assemble_pack(config, bool(args.mock), diagnostics, log, period)
    forecast = build_rolling_forecast(config, pack)
    payload = json.dumps(
        {"board_pack": pack.to_dict(), "forecast": forecast.to_dict()},
        ensure_ascii=False, sort_keys=True, indent=2,
    )
    fingerprint = content_fingerprint(payload)
    log.record(audit_mod.EVENT_PACK_GENERATED,
               {"period": period, "fingerprint": fingerprint, "is_partial": pack.is_partial,
                "material_lines": len(pack.material_lines), "alerts": pack.alerts()})

    decision = _resolve_decision(args, config, approval_cfg, period, fingerprint, pack, now, tz,
                                 diagnostics, log)
    narratives = write_narratives(payload, config, bool(args.mock), diagnostics)
    watermark = str(approval_cfg.get("draft_watermark", "草稿・待財務總監審核"))
    sla_hours = config_decimal(approval_cfg.get("sla_hours", "2"), "approval.sla_hours")

    text = pack_mod.render_board_pack(
        pack=pack,
        status_banner=status_banner(decision, watermark),
        schedule_lines=_schedule_lines(period, approval_cfg, sla_hours),
        executive_summary=narratives["executive_summary"],
        variance_narrative=narratives["variance_narrative"],
        forecast_text=forecaster.render_forecast(forecast),
    )

    runtime_cfg = config.get("runtime") or {}
    # 閘門只建一次：build_gate 會發琥珀燈，重建第二次會讓同一則警告被記兩遍。
    gate = build_gate(runtime_cfg, diagnostics)
    delivery = deliver(
        text=text, subject=build_subject(pack, decision, watermark),
        channel=args.notify or str(runtime_cfg.get("notify_channel", "console")),
        decision=decision, config=config, gate=gate,
        is_dry_run=bool(args.dry_run), diagnostics=diagnostics, log=log,
    )
    log.record(audit_mod.EVENT_RUN_FINISHED,
               {"delivered": delivery["delivered"], "amber_count": diagnostics.amber_count})

    return _build_result(config, args, pack, forecast, decision, delivery, text, narratives,
                         fingerprint, log, diagnostics, gate, period, now)


def _resolve_decision(
    args: argparse.Namespace, config: dict, approval_cfg: dict, period: str, fingerprint: str,
    pack: pack_mod.BoardPack, now: datetime, tz: tzinfo, diagnostics: Diagnostics, log: AuditLog,
) -> ApprovalDecision:
    """核准閘門：可選的核准動作 → 判定 → 未核准時記錄送審與 SLA。"""
    state_path = resolve_state_path(args.state_file, str(approval_cfg.get("state_file", "state/approval.json")))
    if args.approve_as:
        grant_approval(state_path, str(args.approve_as), config, period, fingerprint, now, log)

    state = load_state(state_path)
    decision = evaluate_approval(state, period, fingerprint, pack.is_partial)
    if decision.status == STATUS_INVALIDATED:
        log.record(audit_mod.EVENT_APPROVAL_INVALIDATED, {"period": period, "reason": decision.reason})
        diagnostics.amber(
            f"本期核准已作廢：{decision.reason}",
            "修復資料或重新送審；在此之前董事會收件人不會收到任何內容",
        )

    if not decision.is_dispatch_allowed and not args.dry_run:
        requested_at = persist_pending(state_path, state, period, fingerprint, now, log)
        decision = ApprovalDecision(decision.status, decision.reason, None, None, requested_at, False)

    sla_hours = config_decimal(approval_cfg.get("sla_hours", "2"), "approval.sla_hours")
    check_sla(decision, now, tz, sla_hours, diagnostics, log)
    return decision


def _build_result(
    config: dict, args: argparse.Namespace, pack: pack_mod.BoardPack,
    forecast: forecaster.RollingForecast, decision: ApprovalDecision, delivery: dict,
    text: str, narratives: dict[str, str], fingerprint: str, log: AuditLog,
    diagnostics: Diagnostics, gate: AutonomyGate, period: str, now: datetime,
) -> dict:
    """組出回傳結果。鍵名採用契約 §6 技術債段落建議的六個標準鍵。"""
    module_cfg = config.get("module") or {}
    return {
        "module_id": str(module_cfg.get("id", "23")),
        "module_name": str(module_cfg.get("name", MODULE_NAME)),
        "mode": "mock" if args.mock else "live",
        "dry_run": bool(args.dry_run),
        "warnings": list(gate.warnings),
        "amber_count": diagnostics.amber_count,
        "period": period,
        "generated_at": now.isoformat(timespec="seconds"),
        "currency": pack.currency,
        "is_partial": pack.is_partial,
        "fingerprint": fingerprint,
        "alerts": pack.alerts(),
        "board_pack": pack.to_dict(),
        "forecast": forecast.to_dict(),
        "approval": decision.to_dict(),
        "narratives": narratives,
        "report_text": text,
        "delivery": delivery,
        "audit_file": str(log.path),
        "audit_run_id": log.run_id,
    }


def main() -> int:
    """CLI 進入點。回傳 exit code：草稿（未核准）仍算成功執行，回 0。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (FileNotFoundError, ValueError, sources.SourceError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except sources.ReadOnlyViolation as exc:
        print(f"[RED] 唯讀鐵律違規，已中止：{exc}", file=sys.stderr)
        return 2
    except audit_mod.AuditError as exc:
        print(f"[RED] 稽核軌跡不可用，拒絕產出財務報表：{exc}", file=sys.stderr)
        return 3

    if result["delivery"]["channel"] != "console":
        # console 通道已經由 Notifier 印過，不重複輸出。
        print(result["report_text"])
    if not result["approval"]["is_dispatch_allowed"]:
        print(f"\n注意：本期報表為草稿（{result['approval']['reason']}），未發送給董事會。"
              f"核准指令：python main.py --mock --approve-as <財務總監信箱>", file=sys.stderr)
    if result["is_partial"]:
        print("注意：本期以不完整財務資料產出，不得作為決策依據。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
