"""模組 #19 — 活動與研討會跟進序列（主流程）。

書中數據：部署 1 Day｜每場活動回收 20 小時｜售價 $900 setup + $240/mo
核心價值：研討會帶來品質最高的名單，多數企業卻在 4 天後才寄出一封通用感謝信。
本模組把「一份名單一封信」換成「三種意向、三條節奏」，並在活動結束 30 分鐘內
把高熱度名單交棒給業務。

流程：
    event.ended Webhook
        → 依出席率與互動訊號分群（hot / warm / cold）
        → 各自走專屬序列（hot 30 分鐘起跑、warm 2 小時、cold 4 小時）
        → hot 群同時推進業務 Slack（活動結束 30 分鐘內）
    任何一步之前，只要與會者已回覆或已退訂，該人的序列立即中止。

安全設計：
    1. `stop_on_reply` 與 `respect_unsubscribe` 是不可停用的硬規則。
       config 改成 false 也會被強制覆寫為 true，並發出 Diagnostics AMBER 警告。
    2. 中止檢查跑兩次：排程判定時一次，**每一封信實際送出前再一次**。
    3. 行銷法遵：缺寄件人識別 / 實體地址 / 退訂連結任一項，全部信件強制降為
       草稿，禁止自動送出。
    4. 自主權預設 DRAFT。SUPERVISED_AUTO 必須配白名單，未命中一律降級為草稿。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, tzinfo
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402
from segmenter import (  # noqa: E402
    ACTION_SEND,
    EventFollowUpSequencer,
    EventState,
    SegmentError,
    SegmentRule,
    SequenceHalted,
    build_segments,
    parse_iso,
    resolve_timezone,
)

MODULE_LABEL = "demo19-event-followup"
LIVE_REQUIRED_ENV = ("ANTHROPIC_API_KEY",)

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出
CONTEXT_NOTE = (
    "這是研討會結束後寄給與會者的跟進信。收件人剛聽完一場 90 分鐘的線上活動，"
    "對主題有基本認識但尚未表達購買意向。不要重複整場議程，不要用行銷術語，"
    "不要假裝彼此很熟。信件必須讓對方一眼看出「這是寫給我的」。"
)

# 法遵三件套：行銷名單信件缺任一項就不得自動送出
COMPLIANCE_FIELDS = ("sender_identity", "physical_address", "unsubscribe_url")


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6）。"""
    parser = argparse.ArgumentParser(
        prog="demo19-event-followup",
        description="活動與研討會跟進序列：依參與狀態分眾，回覆或退訂即中止",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實 Anthropic API")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不實際發送")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="通知管道，預設 console",
    )
    parser.add_argument(
        "--config",
        default=str(MODULE_DIR / "config.yaml"),
        help="設定檔路徑，預設同目錄 config.yaml",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="跟進進度狀態檔路徑；指定後即啟用讀寫（預設 --mock 完全不碰檔案）",
    )
    return parser


def _resolve_path(value: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，杜絕硬編碼使用者路徑。"""
    path = Path(value)
    return path if path.is_absolute() else MODULE_DIR / path


def _load_json(value: str | Path, expected_type: type, label: str) -> object:
    """讀取 JSON 檔並檢查頂層型別，格式錯誤要明確報錯。"""
    path = _resolve_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SegmentError(f"{label} 無法讀取或解析：{path}") from exc
    if not isinstance(payload, expected_type):
        raise SegmentError(f"{label} 的頂層應為 {expected_type.__name__}：{path}")
    return payload


def _require_live_env(diagnostics: Diagnostics) -> None:
    """--live 缺憑證要明確報錯退出，絕不靜默降級回 mock。"""
    missing = [key for key in LIVE_REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="金鑰未設定或未載入目前的 shell session",
            fix=f"設定 {', '.join(missing)} 後重跑，或改用 --mock 離線驗證",
        )


def _enforce_safety_switches(safety: dict, diagnostics: Diagnostics) -> list[str]:
    """強制兩個不可停用的安全開關，設定被改掉就發 AMBER 警告。"""
    warnings: list[str] = []
    for key, fix in (
        ("stop_on_reply", "本模組不允許停用回覆中止；請改回 stop_on_reply: true"),
        ("respect_unsubscribe", "忽視退訂屬法遵事故；請改回 respect_unsubscribe: true"),
    ):
        raw = safety.get(key, True)
        if raw is not True:
            message = f"config.safety.{key} 被設為 {raw!r}，已強制覆寫為 true"
            warnings.append(message)
            diagnostics.amber(symptom=message, fix=fix)
    return warnings


def _check_compliance(compliance: dict, diagnostics: Diagnostics) -> tuple[bool, list[str]]:
    """檢查行銷法遵三件套；缺項則回報並要求全數降為草稿。"""
    missing = [key for key in COMPLIANCE_FIELDS if not str(compliance.get(key) or "").strip()]
    if not missing:
        return True, []
    message = f"compliance 缺少必要欄位：{', '.join(missing)}，本次全部信件強制為草稿"
    diagnostics.amber(
        symptom=message,
        fix="補上 sender_identity / physical_address / unsubscribe_url 後重跑",
    )
    return False, [message]


def _compliance_footer(compliance: dict) -> str:
    """組出行銷信必備的信尾（寄件人識別 + 地址 + 退訂連結）。"""
    lines = [
        "—",
        str(compliance.get("sender_identity") or "").strip(),
        str(compliance.get("physical_address") or "").strip(),
    ]
    reply_to = str(compliance.get("reply_to") or "").strip()
    if reply_to:
        lines.append(f"回信請寄至：{reply_to}")
    unsubscribe = str(compliance.get("unsubscribe_url") or "").strip()
    if unsubscribe:
        lines.append(f"不想再收到活動信件請點此退訂：{unsubscribe}")
    return "\n".join(line for line in lines if line)


def _resolve_state(args: argparse.Namespace, config: dict, event_id: str) -> EventState:
    """決定狀態檔位置與讀寫權限。

    優先序：--state-file > config.state.path > 模組預設路徑。
    只有明確指定 --state-file、或跑 --live 正式模式時才啟用檔案 I/O，
    確保 `python main.py --mock` 永遠零副作用且結果可重現。
    """
    raw_path = args.state_file or (config.get("state") or {}).get("path")
    path = _resolve_path(raw_path) if raw_path else None
    is_enabled = bool(args.state_file) or (bool(args.live) and not args.dry_run)
    return EventState(
        path=path,
        event_id=event_id,
        is_enabled=is_enabled,
        is_writable=is_enabled and not args.dry_run,
    )


def _build_sequencer(
    config: dict,
    event: dict,
    state: EventState,
    diagnostics: Diagnostics,
) -> tuple[EventFollowUpSequencer, tzinfo, list[str]]:
    """依設定組出分眾狀態機，回傳 (sequencer, 時區, 警告清單)。"""
    safety = config.get("safety") or {}
    warnings = _enforce_safety_switches(safety, diagnostics)
    tz, tz_warning = resolve_timezone(
        safety.get("timezone", "Asia/Taipei"),
        int(safety.get("timezone_fallback_offset_hours", 8)),
    )
    if tz_warning:
        warnings.append(tz_warning)
        diagnostics.amber(symptom=tz_warning, fix="安裝 tzdata 套件或改用固定偏移設定")
    segmentation = config.get("segmentation") or {}
    crm = config.get("crm") or {}
    sequencer = EventFollowUpSequencer(
        segments=build_segments(segmentation.get("segments")),
        tz=tz,
        event_ended_at=parse_iso(event.get("ended_at"), tz),
        stop_on_reply=safety.get("stop_on_reply", True),
        respect_unsubscribe=safety.get("respect_unsubscribe", True),
        state=state,
        engagement_signals=segmentation.get("engagement_signals"),
        handover_deadline_minutes=int(crm.get("handover_deadline_minutes", 30)),
    )
    warnings.extend(sequencer.forced_overrides)
    return sequencer, tz, warnings


def _build_gate(config: dict, diagnostics: Diagnostics) -> tuple[AutonomyGate, list[str]]:
    """建立自主權閘門；設定違規時降級為 DRAFT 而非讓程式崩潰。"""
    runtime = config.get("runtime") or {}
    raw_level = str(runtime.get("autonomy") or "draft").strip().lower()
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        diagnostics.amber(
            symptom=f"未知的 autonomy 設定 {raw_level!r}",
            fix="改用 read_only / draft / supervised_auto 其中之一；本次已降級為 draft",
        )
        level = AutonomyLevel.DRAFT
    gate = AutonomyGate(
        level=level,
        approved_senders=list(runtime.get("approved_senders") or []),
        days_in_draft=int(runtime.get("days_in_draft") or 0),
    )
    for warning in gate.warnings:
        diagnostics.amber(symptom=warning, fix="滿 14 天並取得客戶簽核後再開全自動")
    return gate, list(gate.warnings)


def _resolve_now(config: dict, tz: tzinfo, is_mock: bool) -> datetime:
    """決定「現在」：mock 用設定的基準時間，確保結果可重現。"""
    if not is_mock:
        return datetime.now(tz)
    raw = str((config.get("mock") or {}).get("now") or "").strip()
    return parse_iso(raw, tz) if raw else datetime.now(tz)


def _read_prompt(relative: str) -> str:
    """讀取提示詞檔（提示詞是資產，一律獨立成 .md，不內嵌在 .py）。"""
    path = _resolve_path(relative)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SegmentError(f"提示詞檔無法讀取：{path}") from exc


def _compose_message(
    llm: LLMClient,
    attendee: dict,
    event: dict,
    decision: dict,
) -> str:
    """呼叫 LLM 產生這一段跟進的信件內容。"""
    system = _read_prompt(str(decision["step"]["prompt"]))
    payload = {
        "attendee": {
            "name": attendee.get("name"),
            "company": attendee.get("company"),
            "role": attendee.get("role"),
            "attendance_pct": attendee.get("attendance_pct"),
            "watched_minutes": attendee.get("watched_minutes"),
            "questions": attendee.get("questions") or [],
            "downloaded_assets": attendee.get("downloaded_assets") or [],
            "segment": decision["segment"],
        },
        "event": {
            "name": event.get("name"),
            "host": event.get("host"),
            "ended_at": event.get("ended_at"),
            "recording_url": event.get("recording_url"),
            "slides_url": event.get("slides_url"),
            "key_takeaways": event.get("key_takeaways") or [],
            "next_event": event.get("next_event"),
        },
        "step": {
            "offset_minutes": decision["step"]["offset_minutes"],
            "type": decision["step"]["type"],
        },
    }
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    return llm.complete(system=system, user=user, max_tokens=900)


def _rule_for(sequencer: EventFollowUpSequencer, decision: dict) -> SegmentRule:
    """由決策中的 segment key 取回對應的 SegmentRule 物件。"""
    key = str(decision["segment"])
    for rule in sequencer.segments:
        if rule.key == key:
            return rule
    raise SegmentError(f"決策指向不存在的分群：{key}")


def _record(decision: dict, attendee: dict, body: str, autonomy: AutonomyLevel,
            is_compliant: bool) -> dict:
    """把一次成功產出的跟進整理成回報用的 dict。"""
    return {
        "attendee_id": decision["attendee_id"],
        "name": decision["name"],
        "company": decision["company"],
        "email": str(attendee.get("email") or ""),
        "segment": decision["segment"],
        "segment_label": decision["segment_label"],
        "step_type": decision["step"]["type"],
        "step_offset_minutes": decision["step"]["offset_minutes"],
        "due_at": decision["due_at"],
        "autonomy": autonomy.value,
        "is_compliant": is_compliant,
        "body": body,
    }


def _halt_entry(decision: dict) -> dict:
    """把中止決策整理成回報用的 dict。"""
    return {
        "attendee_id": decision["attendee_id"],
        "name": decision["name"],
        "company": decision["company"],
        "segment": decision["segment"],
        "reason": decision["reason"],
        "detail": decision["detail"],
    }


def _handover_entry(
    decision: dict,
    attendee: dict,
    context: dict,
    now: datetime,
) -> dict:
    """組出交棒業務的 Slack 卡片內容，並標記 SLA 是否達標。"""
    sequencer: EventFollowUpSequencer = context["sequencer"]
    deadline = sequencer.handover_deadline
    return {
        "attendee_id": decision["attendee_id"],
        "name": decision["name"],
        "company": decision["company"],
        "email": str(attendee.get("email") or ""),
        "role": str(attendee.get("role") or ""),
        "attendance_pct": attendee.get("attendance_pct"),
        "questions": list(attendee.get("questions") or []),
        "lead_score": attendee.get("lead_score"),
        "slack_channel": context["slack_channel"],
        "deadline_at": deadline.isoformat(),
        "dispatched_at": now.isoformat(),
        "is_within_sla": now <= deadline,
    }


def _maybe_handover(decision: dict, attendee: dict, context: dict, now: datetime) -> None:
    """hot 群在活動結束 30 分鐘內交棒業務；逾時仍要交棒但發 AMBER。"""
    sequencer: EventFollowUpSequencer = context["sequencer"]
    rule = _rule_for(sequencer, decision)
    if not sequencer.should_handover(attendee, rule):
        return
    entry = _handover_entry(decision, attendee, context, now)
    context["handovers"].append(entry)
    sequencer.mark_handover(attendee)
    if entry["is_within_sla"]:
        return
    message = (
        f"{entry['name']}（{entry['company']}）的業務交棒逾時："
        f"應於 {entry['deadline_at']} 前送達 {entry['slack_channel']}"
    )
    context["warnings"].append(message)
    context["diagnostics"].amber(
        symptom=message,
        fix="把排程頻率提高到每 5 分鐘一次，或改用 event.ended Webhook 即時觸發",
    )


def _process_one(decision: dict, attendee: dict, context: dict, now: datetime) -> tuple[str, dict]:
    """處理單一「該發送」決策，回傳 (bucket, 紀錄)。

    bucket 為 "sent" / "drafted" / "halted"。
    """
    sequencer: EventFollowUpSequencer = context["sequencer"]
    try:
        # 第二道閘門：實際送出前再查一次是否已回覆或已退訂
        sequencer.assert_can_send(attendee)
    except SequenceHalted as exc:
        return "halted", {
            "attendee_id": exc.attendee_id,
            "name": decision["name"],
            "company": decision["company"],
            "segment": decision["segment"],
            "reason": exc.reason,
            "detail": exc.detail,
        }
    _maybe_handover(decision, attendee, context, now)
    body = _compose_message(context["llm"], attendee, context["event"], decision)
    body = f"{body}\n\n{context['footer']}" if context["footer"] else body
    email = str(attendee.get("email") or "")
    gate: AutonomyGate = context["gate"]
    level = gate.effective_level(email)
    is_compliant = bool(context["is_compliant"])
    can_send = gate.can_send(email) and is_compliant and not context["dry_run"]
    entry = _record(decision, attendee, body, level, is_compliant)
    if can_send:
        context["notifier"].send(text=body, subject=_subject(decision))
        sequencer.mark_sent(attendee, decision["step"]["offset_minutes"])
        return "sent", entry
    return "drafted", entry


def _subject(decision: dict) -> str:
    """組出信件主旨。"""
    return (
        f"[活動跟進 · {decision['segment']}] {decision['company']}"
        f" — {decision['step']['type']}"
    )


def _run_sequence(attendees: list, now: datetime, context: dict) -> dict[str, list]:
    """跑完整份名單的分群判定 + 產文，回傳三個 bucket。"""
    buckets: dict[str, list] = {"sent": [], "drafted": [], "halted": []}
    sequencer: EventFollowUpSequencer = context["sequencer"]
    index = {str(item.get("id") or ""): item for item in attendees}
    for decision in sequencer.plan(attendees, now):
        if decision["action"] != ACTION_SEND:
            buckets["halted"].append(_halt_entry(decision))
            continue
        attendee = index[decision["attendee_id"]]
        bucket, entry = _process_one(decision, attendee, context, now)
        buckets[bucket].append(entry)
    return buckets


def _segment_counts(sequencer: EventFollowUpSequencer, attendees: list) -> dict[str, int]:
    """統計整份名單的分群人數（含無法分群者）。"""
    counts = {rule.key: 0 for rule in sequencer.segments}
    counts["unclassified"] = 0
    for attendee in attendees:
        try:
            rule = sequencer.classify(attendee)
        except SegmentError:
            rule = None
        counts["unclassified" if rule is None else rule.key] += 1
    return counts


def _build_context(config: dict, args: argparse.Namespace, event: dict,
                   sequencer: EventFollowUpSequencer, gate: AutonomyGate,
                   diagnostics: Diagnostics, is_mock: bool) -> dict:
    """組出跑序列所需的共用物件。"""
    compliance = config.get("compliance") or {}
    is_compliant, compliance_warnings = _check_compliance(compliance, diagnostics)
    return {
        "sequencer": sequencer,
        "gate": gate,
        "diagnostics": diagnostics,
        "event": event,
        "llm": LLMClient(mock=is_mock, context_note=CONTEXT_NOTE),
        "notifier": Notifier(channel=args.notify),
        "footer": _compliance_footer(compliance),
        "is_compliant": is_compliant,
        "slack_channel": str((config.get("crm") or {}).get("slack_channel") or ""),
        "dry_run": bool(args.dry_run),
        "handovers": [],
        "warnings": list(compliance_warnings),
    }


def run(args: argparse.Namespace) -> dict:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config = load_config(_resolve_path(args.config))
    diagnostics = Diagnostics(MODULE_LABEL)
    is_mock = not args.live
    if not is_mock:
        _require_live_env(diagnostics)
    event = _load_json(
        (config.get("event") or {}).get("source", "mock/event.json"), dict, "event"
    )
    state = _resolve_state(args, config, str(event.get("id") or ""))
    sequencer, tz, warnings = _build_sequencer(config, event, state, diagnostics)
    gate, gate_warnings = _build_gate(config, diagnostics)
    warnings.extend(gate_warnings)
    attendees = _load_json(
        (config.get("mock") or {}).get("attendees", "mock/attendees.json"),
        list,
        "attendees",
    )
    context = _build_context(config, args, event, sequencer, gate, diagnostics, is_mock)
    now = _resolve_now(config, tz, is_mock)
    buckets = _run_sequence(attendees, now, context)
    warnings.extend(context["warnings"])
    return _build_result(config, args, now, attendees, buckets, warnings, context)


def _build_result(
    config: dict,
    args: argparse.Namespace,
    now: datetime,
    attendees: list,
    buckets: dict[str, list],
    warnings: list[str],
    context: dict,
) -> dict:
    """組出統一的回傳結構。"""
    module = config.get("module") or {}
    sequencer: EventFollowUpSequencer = context["sequencer"]
    event = context["event"]
    state = sequencer.state
    return {
        "module_id": str(module.get("id", "19")),
        "module_name": str(module.get("name", "活動與研討會跟進序列")),
        "mode": "mock" if not args.live else "live",
        "dry_run": bool(args.dry_run),
        "notify_channel": args.notify,
        "timezone": str((config.get("safety") or {}).get("timezone", "")),
        "reference_now": now.isoformat(),
        "event": {
            "id": str(event.get("id") or ""),
            "name": str(event.get("name") or ""),
            "ended_at": str(event.get("ended_at") or ""),
            "recording_url": str(event.get("recording_url") or ""),
        },
        "stop_on_reply": sequencer.is_stop_on_reply_enabled,
        "respect_unsubscribe": sequencer.is_unsubscribe_respected,
        "is_compliant": bool(context["is_compliant"]),
        "state_file": str(state.path) if state.is_enabled else None,
        "total_attendees": len(attendees),
        "segment_counts": _segment_counts(sequencer, attendees),
        "sent": buckets["sent"],
        "drafted": buckets["drafted"],
        "halted": buckets["halted"],
        "crm_handovers": context["handovers"],
        "handover_deadline_at": sequencer.handover_deadline.isoformat(),
        "warnings": warnings,
        "amber_count": context["diagnostics"].amber_count,
    }


def _summarise(result: dict) -> str:
    """組出給操作者的摘要文字。"""
    counts = result["segment_counts"]
    lines = [
        f"【{result['module_name']}】{result['event']['name']}"
        f"（{result['mode']} 模式，基準時間 {result['reference_now']}）",
        f"與會名單 {result['total_attendees']} 人｜"
        + "｜".join(f"{key} {value}" for key, value in counts.items()),
        f"自動送出 {len(result['sent'])}｜待審草稿 {len(result['drafted'])}"
        f"｜中止 {len(result['halted'])}｜業務交棒 {len(result['crm_handovers'])}",
        f"安全開關：stop_on_reply={result['stop_on_reply']}"
        f"｜respect_unsubscribe={result['respect_unsubscribe']}"
        f"｜法遵={'通過' if result['is_compliant'] else '未通過（全部降為草稿）'}",
    ]
    lines.extend(_summarise_entries(result))
    return "\n".join(lines)


def _summarise_entries(result: dict) -> list[str]:
    """把三個 bucket 與交棒清單攤平成摘要行。"""
    lines: list[str] = []
    for item in result["drafted"]:
        lines.append(
            f"  [草稿] {item['name']}（{item['company']}）"
            f" {item['segment']} / {item['step_type']}"
        )
    for item in result["sent"]:
        lines.append(
            f"  [已送] {item['name']}（{item['company']}）"
            f" {item['segment']} / {item['step_type']}"
        )
    for item in result["halted"]:
        lines.append(f"  [中止] {item['name']}（{item['company']}）— {item['reason']}")
    for item in result["crm_handovers"]:
        flag = "準時" if item["is_within_sla"] else "逾時"
        lines.append(
            f"  [交棒·{flag}] {item['name']} → {item['slack_channel']}"
            f"（截止 {item['deadline_at']}）"
        )
    return lines


def main() -> int:
    """解析參數 -> run() -> 印出/發送結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (SegmentError, FileNotFoundError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    summary = _summarise(result)
    print(summary)
    # console 管道等同上面的 print，再送一次只會讓輸出重複
    if not args.dry_run and args.notify != "console":
        Notifier(channel=args.notify).send(text=summary, subject="活動跟進序列執行摘要")
    return 0


if __name__ == "__main__":
    sys.exit(main())
