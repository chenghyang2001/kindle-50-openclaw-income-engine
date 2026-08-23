"""demo07 預約排程器主流程（書中原型：WhatsApp 自動排程器，本專案改用 Telegram）。

把「客戶詢問 -> 手動查日曆 -> 給選項 -> 等回覆 -> 確認 -> 處理改期」這條
每次 5-12 則訊息的來回，壓成一條狀態機路徑：

    INQUIRY -> SLOTS_OFFERED -> SLOT_SELECTED -> CONFIRMED
                    ^                 |
                    +---- 樂觀鎖衝突 --+          CONFIRMED -> RESCHEDULE_REQUESTED -> SLOTS_OFFERED

用法：
    python main.py --mock              # 零憑證零網路，重播 mock/conversations.json
    python main.py --mock --dry-run    # 跑完流程但不發通知、不寫狀態檔
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

from calendar_client import (  # noqa: E402
    CalendarClient,
    CalendarConflictError,
    Slot,
    resolve_timezone,
)
from state_machine import (  # noqa: E402
    BookingEvent,
    ConversationStateMachine,
    ConversationStore,
)

MODULE_NAME = "demo07-booking-scheduler"
MEETING_MODE = "線上會議（連結於確認訊息內附上）"


@dataclass
class SchedulerContext:
    """一次執行共用的相依物件。集中傳遞，避免每個 handler 都吃 8 個參數。"""

    config: dict[str, Any]
    calendar: CalendarClient
    gate: AutonomyGate
    diagnostics: Diagnostics
    llm: LLMClient
    prompt: str
    now: datetime
    slots_to_offer: int
    timezone_label: str
    is_mock: bool


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6）"""
    parser = argparse.ArgumentParser(description="demo07 預約排程器")
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
        help="對話狀態檔路徑，預設取自 config 的 state.store_file（相對於模組目錄）。"
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
) -> SchedulerContext:
    """組出 SchedulerContext：時區、日曆、自主權閘門、LLM、提示詞。"""
    scheduling = config["scheduling"]
    tz, tz_warning = resolve_timezone(
        scheduling["timezone"], int(scheduling.get("fallback_utc_offset_hours", 0))
    )
    if tz_warning:
        diagnostics.amber(tz_warning, "pip install tzdata（Windows 環境建議安裝）")
    is_mock = bool(args.mock) and not bool(args.live)
    calendar = CalendarClient(
        calendar_path=_resolve_path(config["mock"]["calendar_file"]),
        tz=tz,
        slot_duration_minutes=int(scheduling["slot_duration_minutes"]),
        business_hours=scheduling["business_hours"],
        min_lead_time_minutes=int(scheduling.get("min_lead_time_minutes", 0)),
        horizon_days=int(scheduling.get("horizon_days", 14)),
        persist=not is_mock,
    )
    now = datetime.fromisoformat(scheduling["reference_now"]).replace(tzinfo=tz)
    return SchedulerContext(
        config=config,
        calendar=calendar,
        gate=_build_gate(config.get("runtime", {}), diagnostics),
        diagnostics=diagnostics,
        llm=LLMClient(mock=is_mock, context_note="預約排程對話，僅談時間，不談價格"),
        prompt=_resolve_path(config["prompts"]["conversation_file"]).read_text(encoding="utf-8"),
        now=now if is_mock else datetime.now(tz),
        slots_to_offer=int(scheduling.get("slots_to_offer", 3)),
        timezone_label=scheduling["timezone"],
        is_mock=is_mock,
    )


def _load_conversations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """讀取待重播的對話。檔案缺失要明確報錯，不可靜默當成沒有客戶。"""
    path = _resolve_path(config["mock"]["conversations_file"])
    if not path.exists():
        raise FileNotFoundError(f"找不到對話檔：{path.resolve()}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"對話檔格式錯誤：{path}（{exc}）") from exc
    return list(data.get("conversations", []))


# --------------------------------------------------------------------------- #
# 回覆生成
# --------------------------------------------------------------------------- #
def _polish(ctx: SchedulerContext, text: str) -> str:
    """live 模式才把樣板回覆交給 LLM 依 prompts/conversation.md 潤飾語氣。"""
    if ctx.is_mock:
        return text
    return ctx.llm.complete(system=ctx.prompt, user=text, max_tokens=400).strip() or text


def _slots_reply(ctx: SchedulerContext, slots: list[Slot], opening: str) -> str:
    """提供時段的訊息：開場 + 編號清單 + 一句「回覆數字」的行動呼籲。"""
    if not slots:
        return f"{opening}目前排程已滿，我明天再回報最新空檔給你，抱歉讓你久等。"
    lines = [f"{index}. {slot.label()}" for index, slot in enumerate(slots, start=1)]
    body = "\n".join(lines)
    return (
        f"{opening}以下是最近可預約的時段（{ctx.timezone_label} 時間）：\n"
        f"{body}\n"
        "哪一個方便？回覆數字就好。"
    )


def _confirmed_reply(booking: dict[str, Any], slot: Slot) -> str:
    """確認訊息四件事：時間、長度、形式、改期出口（第 4 點每次都要講）。"""
    return (
        f"已為你確認 {slot.label()}，共 {slot.duration_minutes} 分鐘。\n"
        f"形式：{MEETING_MODE}\n"
        f"預約編號：{booking['id']}\n"
        "要改期的話，直接在這裡跟我說一聲就行。"
    )


# --------------------------------------------------------------------------- #
# 對話處理
# --------------------------------------------------------------------------- #
def _slots_from_context(machine: ConversationStateMachine) -> list[Slot]:
    """把狀態機 context 內的時段還原成 Slot（狀態檔重載後仍可用）"""
    return [
        Slot(
            start=datetime.fromisoformat(item["start"]),
            end=datetime.fromisoformat(item["end"]),
        )
        for item in machine.context.get("offered_slots", [])
    ]


def _offer_slots(
    ctx: SchedulerContext,
    machine: ConversationStateMachine,
    record: dict[str, Any],
    opening: str,
) -> str:
    """查即時日曆、提供 N 個時段，並記下當下的日曆版本（樂觀鎖的憑據）。"""
    slots = ctx.calendar.available_slots(ctx.now, ctx.slots_to_offer)
    if len(slots) < ctx.slots_to_offer:
        ctx.diagnostics.amber(
            f"{machine.conversation_id}：可用時段只剩 {len(slots)} 個（少於 {ctx.slots_to_offer}）",
            "放寬 business_hours、縮短 slot_duration_minutes 或延長 horizon_days",
        )
    machine.apply(
        BookingEvent.OFFER_SLOTS,
        offered_slots=[slot.to_dict() for slot in slots],
        calendar_version=ctx.calendar.version,
    )
    record["offer_rounds"] += 1
    return _polish(ctx, _slots_reply(ctx, slots, opening))


def _inject_conflict(ctx: SchedulerContext, turn: dict[str, Any], slot: Slot) -> None:
    """模擬「另一位客戶在我們寫入前搶先訂走同一時段」，用來觸發樂觀鎖。"""
    injection = turn.get("conflict_injection")
    if not injection:
        return
    ctx.calendar.create_booking(
        slot=slot,
        customer=injection.get("customer", "（其他客戶）"),
        title=injection.get("title", "臨時預約"),
        expected_version=None,  # 搶先者拿到的是最新版本，所以不比對
    )


def _book_slot(
    ctx: SchedulerContext,
    machine: ConversationStateMachine,
    record: dict[str, Any],
    slot: Slot,
    topic: str,
) -> list[str]:
    """嘗試寫入日曆：成功走 CONFIRM，撞鎖走 SLOT_TAKEN 並重新提供時段。"""
    try:
        booking = ctx.calendar.create_booking(
            slot=slot,
            customer=machine.customer,
            title=topic,
            expected_version=machine.context.get("calendar_version"),
        )
    except CalendarConflictError as exc:
        record["conflicts"] += 1
        ctx.diagnostics.amber(
            f"{machine.conversation_id}：{exc}", "已自動重新提供時段，無需人工介入"
        )
        machine.apply(BookingEvent.SLOT_TAKEN, last_conflict=str(exc))
        opening = "不好意思，這個時段在幾秒前剛被預訂走了。"
        return [_offer_slots(ctx, machine, record, opening)]
    machine.apply(BookingEvent.CONFIRM, booking=booking)
    record["booking"] = booking
    return [_polish(ctx, _confirmed_reply(booking, slot))]


def _handle_select(
    ctx: SchedulerContext,
    machine: ConversationStateMachine,
    record: dict[str, Any],
    turn: dict[str, Any],
    topic: str,
) -> list[str]:
    """客戶回覆數字選定時段。索引越界視為看不懂的回覆，重新提供時段。"""
    slots = _slots_from_context(machine)
    index = int(turn.get("slot_index", 0))
    if not slots or index >= len(slots):
        ctx.diagnostics.amber(
            f"{machine.conversation_id}：選擇的編號 {index + 1} 不在清單內",
            "確認提供時段的訊息與客戶回覆是否對齊",
        )
        return [_offer_slots(ctx, machine, record, "我這邊沒對到你選的編號，重新給你一次：")]
    slot = slots[index]
    machine.apply(BookingEvent.SELECT_SLOT, selected_slot=slot.to_dict())
    _inject_conflict(ctx, turn, slot)
    return _book_slot(ctx, machine, record, slot, topic)


def _handle_reschedule(
    ctx: SchedulerContext, machine: ConversationStateMachine, record: dict[str, Any]
) -> list[str]:
    """改期：先無條件答應並釋出舊時段，再走一次完整的提供時段流程。"""
    machine.apply(BookingEvent.REQUEST_RESCHEDULE)
    booking = machine.context.get("booking")
    if booking:
        ctx.calendar.cancel_booking(booking["id"])
    record["reschedules"] += 1
    record["booking"] = None
    return [
        _polish(ctx, "沒問題，我幫你改。原本的時段已經釋出了。"),
        _offer_slots(ctx, machine, record, ""),
    ]


def _handle_turn(
    ctx: SchedulerContext,
    machine: ConversationStateMachine,
    record: dict[str, Any],
    turn: dict[str, Any],
    topic: str,
) -> list[str]:
    """把一則客戶訊息的意圖派給對應 handler。未知意圖要出聲，不可靜默略過。"""
    intent = turn.get("intent", "")
    if intent == "inquiry":
        machine.apply(BookingEvent.RECEIVE_INQUIRY, topic=topic)
        return [_offer_slots(ctx, machine, record, "感謝你的詢問！")]
    if intent == "select_slot":
        return _handle_select(ctx, machine, record, turn, topic)
    if intent == "reschedule":
        return _handle_reschedule(ctx, machine, record)
    ctx.diagnostics.amber(
        f"{machine.conversation_id}：無法辨識的意圖 {intent!r}", "擴充 _handle_turn 的意圖對照"
    )
    return []


def _process_conversation(
    ctx: SchedulerContext, spec: dict[str, Any], machine: ConversationStateMachine
) -> dict[str, Any]:
    """重播一段對話，回傳可供斷言的結果紀錄。"""
    record: dict[str, Any] = {
        "id": spec["id"],
        "customer": spec.get("customer", ""),
        "handle": spec.get("handle", ""),
        "scenario": spec.get("scenario", ""),
        "replies": [],
        "booking": None,
        "conflicts": 0,
        "reschedules": 0,
        "offer_rounds": 0,
    }
    topic = spec.get("topic", "預約洽談")
    for turn in spec.get("turns", []):
        record["replies"].extend(_handle_turn(ctx, machine, record, turn, topic))
    record["final_state"] = machine.state.value
    level = ctx.gate.effective_level(machine.handle)
    record["autonomy_level"] = level.value
    record["is_auto_sent"] = ctx.gate.can_send(machine.handle)
    return record


# --------------------------------------------------------------------------- #
# 彙整與輸出
# --------------------------------------------------------------------------- #
def _build_summary(config: dict[str, Any], records: list[dict[str, Any]]) -> str:
    """給通知通道用的純文字摘要（不含 HTML，避免通道間差異）。"""
    module = config.get("module", {})
    booked = sum(1 for record in records if record["booking"])
    conflicts = sum(record["conflicts"] for record in records)
    lines = [
        f"[{module.get('id', '07')}] {module.get('name', MODULE_NAME)} 執行結果",
        f"對話數：{len(records)}｜完成預約：{booked}｜時段衝突自動化解：{conflicts}",
        "",
    ]
    for record in records:
        booking = record["booking"]
        when = booking["start"][:16].replace("T", " ") if booking else "未成交"
        lines.append(
            f"- {record['customer']}：{record['final_state']}｜{when}"
            f"｜改期 {record['reschedules']} 次｜自主權 {record['autonomy_level']}"
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
    return notifier.send(summary, subject="預約排程器每日摘要")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config_path = Path(args.config) if args.config else MODULE_DIR / "config.yaml"
    config = load_config(config_path)
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=False)
    ctx = _build_context(config, args, diagnostics)
    # CLI 覆寫優先，沒給才回頭取 config；--state-file 預設 None，不會把 config 值蓋掉
    store_source = args.state_file or config["state"]["store_file"]
    store = ConversationStore(_resolve_path(store_source))
    machines = store.load()
    records: list[dict[str, Any]] = []
    for spec in _load_conversations(config):
        # 重播一律開新狀態機：拿舊的 CONFIRMED 狀態去接 RECEIVE_INQUIRY 會（正確地）拋錯
        machine = ConversationStateMachine(
            conversation_id=spec["id"],
            customer=spec.get("customer", ""),
            handle=spec.get("handle", ""),
        )
        machines[spec["id"]] = machine
        records.append(_process_conversation(ctx, spec, machine))
    summary = _build_summary(config, records)
    state_path = None if args.dry_run else str(store.save(machines))
    return {
        "module": config.get("module", {}).get("id", "07"),
        "is_mock": ctx.is_mock,
        "conversations": records,
        "bookings_created": sum(1 for record in records if record["booking"]),
        "conflicts_resolved": sum(record["conflicts"] for record in records),
        "reschedules": sum(record["reschedules"] for record in records),
        "calendar_version": ctx.calendar.version,
        "amber_count": diagnostics.amber_count,
        "summary": summary,
        "state_file": state_path,
        "is_notified": _notify(args, config, summary, diagnostics),
    }


def _render_report(result: dict[str, Any]) -> str:
    """人看的完整輸出：摘要 + 每段對話的代理程式回覆逐字稿。"""
    blocks = [result["summary"], ""]
    for record in result["conversations"]:
        blocks.append(f"=== {record['id']} {record['customer']}（{record['scenario']}）===")
        for reply in record["replies"]:
            blocks.append(reply)
            blocks.append("")
    blocks.append(
        f"日曆版本：v{result['calendar_version']}｜"
        f"amber：{result['amber_count']}｜狀態檔：{result['state_file'] or '（未寫入）'}"
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
