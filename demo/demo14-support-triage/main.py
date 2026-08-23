"""demo14 多渠道客服分流 — 主流程（第 05 章 #14 / 附錄 F p11-p12）。

同時監控 Gmail / Intercom / WhatsApp / Instagram，把每一則進件依 apxF_p12 決策樹分流：

    訂單狀態      -> 查訂單系統 -> 自動回覆精確物流更新
    常見問題      -> 查知識庫   -> 自動回覆已審核的標準解答
    退款 / 爭議   -> DO NOT PROCESS -> 只給 Holding Response 草稿並升級 Slack
    負面/VIP/法務 -> Immediate Flag -> 高優先升級 Slack

兩條不可協商的安全鐵律（違反其一，這個模組就不該上線）：

    1. never_claim_human：自動回覆一律附 AI 身分揭露，且送出前掃描冒充真人的措辭，
       命中就取消自動回覆並改升級真人。
    2. 退款/爭議 DO NOT PROCESS：這條規則不吃 config。把 safety 旗標改成 false 也會
       被強制覆寫回 true 並發出琥珀警示（比照 demo10 的 stop_on_reply）。

用法：
    python main.py --mock                      # 零憑證、零網路
    python main.py --mock --notify telegram    # 把升級卡片推到 Telegram
    python main.py --live --dry-run            # 串真實渠道但不送出任何東西
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel, parse_level  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import LLMClient, LLMError  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402
from triage import (  # noqa: E402
    ACTION_AUTO_REPLY,
    CATEGORY_FAQ,
    CATEGORY_ORDER_STATUS,
    CATEGORY_REFUND_DISPUTE,
    HANDLED_FILE_DEFAULT,
    KB_MAX_ENTRIES,
    KB_MIN_ENTRIES,
    NEVER_AUTO_REPLY,
    PRIORITY_HIGH,
    REPLY_MAX_WORDS,
    SupportFetchError,
    build_faq_reply,
    build_holding_response,
    build_order_reply,
    classify,
    collect_messages,
    filter_new_messages,
    find_impersonation,
    format_escalation_payload,
    has_disclosure,
    load_handled_keys,
    load_knowledge_base,
    load_orders,
    lookup_order,
    match_faq,
    message_key,
    save_handled_keys,
    state_path,
)

MODULE_NAME = "demo14-support-triage"

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出
CONTEXT_NOTE = (
    "這是即時客服現場。客戶正在等回覆，任何一句被截圖都可能變成公司對外的承諾。"
    "拿不準就交給真人，不要為了看起來有用而猜。"
)


def load_prompt(name: str) -> str:
    """讀取 prompts/<name>.md。提示詞是資產，一律獨立成檔不內嵌。"""
    path = MODULE_DIR / "prompts" / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"找不到提示詞檔：{path}")
    return path.read_text(encoding="utf-8")


# ── 安全鐵律：config 改不掉的兩件事 ────────────────────────────────────────
def enforce_hard_rules(safety: dict[str, Any], diagnostics: Diagnostics) -> list[str]:
    """強制兩條安全鐵律為 true，被改掉就覆寫並發琥珀警示。

    為什麼不讓 config 關掉：這兩條不是「偏好設定」，是這個模組能不能合法上線的前提。
    做成可設定的旗標，等於把「哪天有人為了衝回覆率關掉它」變成一個選項。
    """
    warnings: list[str] = []
    rules = (
        ("refund_dispute_do_not_process", "退款/爭議一律不自動回覆", "退款與爭議必須由真人處理（apxF_p12：DO NOT PROCESS）"),
        ("never_claim_human", "自動回覆必須揭露 AI 身分", "AI 冒充真人涉及消費者保護與平台政策風險"),
    )
    for key, label, why in rules:
        if safety.get(key, True) is not True:
            message = f"config.safety.{key} 被設為 {safety.get(key)!r}，已強制覆寫為 true（{label}）"
            warnings.append(message)
            diagnostics.amber(symptom=message, fix=f"把 config.yaml 改回 {key}: true。{why}")
        safety[key] = True
    return warnings


def build_gate(runtime_cfg: dict[str, Any], diagnostics: Diagnostics) -> AutonomyGate:
    """建立自主權閘門。設定有誤一律往安全側倒（退回 DRAFT），絕不放寬。"""
    try:
        level = parse_level(runtime_cfg.get("autonomy", "draft"))
    except AutonomyError as exc:
        diagnostics.amber(symptom=f"自主權設定無法解析：{exc}", fix="runtime.autonomy 僅接受 read_only / draft / supervised_auto，本次退回 draft")
        level = AutonomyLevel.DRAFT
    try:
        return AutonomyGate(
            level=level,
            approved_senders=list(runtime_cfg.get("approved_senders") or []),
            days_in_draft=int(runtime_cfg.get("days_in_draft", 0)),
        )
    except AutonomyError as exc:
        diagnostics.amber(symptom=f"自主權設定違規：{exc}", fix="已強制退回 DRAFT，補齊 approved_senders 後再調整")
        return AutonomyGate(level=AutonomyLevel.DRAFT)


def check_knowledge_base(entries: list[dict[str, Any]], diagnostics: Diagnostics) -> None:
    """檢查知識庫規模。書中規格是 30-50 條，太少會讓自動回覆率低到沒有價值。"""
    if len(entries) < KB_MIN_ENTRIES:
        diagnostics.amber(
            symptom=f"知識庫只有 {len(entries)} 條，低於書中建議的 {KB_MIN_ENTRIES} 條",
            fix="從過去 3 個月的客服紀錄整理出最常見的問答補齊，否則多數訊息會落到升級路徑",
        )
    elif len(entries) > KB_MAX_ENTRIES:
        diagnostics.amber(
            symptom=f"知識庫有 {len(entries)} 條，超過書中建議上限 {KB_MAX_ENTRIES} 條",
            fix="關鍵字重疊會讓命中變得難以解釋；合併語意相近的條目",
        )


# ── 單則訊息的處置 ──────────────────────────────────────────────────────────
def _compose_reply(
    verdict: dict[str, Any],
    message: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any] | None:
    """依分類組出自動回覆內容。內容只能來自知識庫或訂單系統。"""
    persona = context["persona"]
    greeting = str(persona.get("greeting", "")).replace("{NAME}", str(message.get("customer_name", "")))
    disclosure = str(persona.get("ai_disclosure", ""))
    max_words = int(context["reply_max_words"])
    if verdict["category"] == CATEGORY_ORDER_STATUS:
        order = lookup_order(verdict.get("order_id"), context["orders"])
        return build_order_reply(order, greeting, disclosure, max_words) if order else None
    if verdict["category"] == CATEGORY_FAQ:
        text = f"{message.get('subject', '')} {message.get('text', '')}"
        entry, _ = match_faq(text, context["knowledge_base"], context["kb_min_score"])
        return build_faq_reply(entry, greeting, disclosure, max_words) if entry else None
    return None


def _guard_reply(reply: dict[str, Any], persona: dict[str, Any]) -> list[str]:
    """never_claim_human 程式層檢查。回傳違規清單（空清單代表可以送）。"""
    violations: list[str] = []
    banned = [str(item) for item in persona.get("human_impersonation_phrases") or []]
    hits = find_impersonation(reply["text"], banned)
    if hits:
        violations.append(f"回覆中出現冒充真人的措辭：{'、'.join(hits)}")
    if not has_disclosure(reply["text"], str(persona.get("ai_disclosure", ""))):
        violations.append("回覆缺少 AI 身分揭露句")
    return violations


def _handle_auto_reply(
    message: dict[str, Any], verdict: dict[str, Any], context: dict[str, Any], diagnostics: Diagnostics
) -> dict[str, Any]:
    """處理可自動回覆的訊息。任何一關沒過就轉成升級，寧可慢也不亂講。"""
    reply = _compose_reply(verdict, message, context)
    if reply is None:
        verdict["reason"] += "；找不到可引用的答案來源，改走升級路徑"
        return _handle_escalation(message, verdict, context, diagnostics)
    violations = _guard_reply(reply, context["persona"])
    if violations:
        diagnostics.amber(
            symptom=f"[never_claim_human] {message_key(message)} 的自動回覆被攔下：{'；'.join(violations)}",
            fix="檢查 knowledge_base 答案與 persona.ai_disclosure 設定；本則已改為升級真人",
        )
        verdict["reason"] += "；違反 never_claim_human，已取消自動回覆"
        return _handle_escalation(message, verdict, context, diagnostics, extra_note="；".join(violations))
    gate: AutonomyGate = context["gate"]
    recipient = str(message.get("customer_contact", ""))
    return {
        "key": message_key(message),
        "channel": message.get("channel", ""),
        "customer_name": message.get("customer_name", ""),
        "category": verdict["category"],
        "reply": reply["text"],
        "source": reply["source"],
        "is_trimmed": reply["is_trimmed"],
        "autonomy": gate.effective_level(recipient).value,
        "can_send": gate.can_send(recipient),
        "reason": verdict["reason"],
    }


def _handle_escalation(
    message: dict[str, Any],
    verdict: dict[str, Any],
    context: dict[str, Any],
    diagnostics: Diagnostics,
    extra_note: str = "",
) -> dict[str, Any]:
    """處理必須交給真人的訊息，組出 Slack 升級卡片與（退款專用的）Holding Response。"""
    holding = ""
    if verdict["category"] == CATEGORY_REFUND_DISPUTE:
        holding = build_holding_response(
            str(context["persona"].get("holding_response_template", "")),
            message,
            str(context["persona"].get("ai_disclosure", "")),
        )
        hits = find_impersonation(holding, [str(i) for i in context["persona"].get("human_impersonation_phrases") or []])
        if hits:
            diagnostics.amber(
                symptom=f"[never_claim_human] Holding Response 模板含冒充真人措辭：{'、'.join(hits)}",
                fix="修改 config.persona.holding_response_template；本則不附 Holding Response 文字",
            )
            holding = ""
    return {
        "key": message_key(message),
        "channel": message.get("channel", ""),
        "customer_name": message.get("customer_name", ""),
        "category": verdict["category"],
        "priority": verdict["priority"],
        "signals": verdict["signals"],
        "is_vip": verdict["is_vip"],
        "refund_amount": verdict["refund_amount"],
        "payload": format_escalation_payload(message, verdict),
        "holding_response": holding,
        "reason": verdict["reason"] + (f"；{extra_note}" if extra_note else ""),
        "advisory": verdict.get("advisory", ""),
    }


def triage_messages(
    messages: list[dict[str, Any]], context: dict[str, Any], diagnostics: Diagnostics
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """跑完決策樹，回傳 (自動回覆清單, 升級清單)。"""
    replies: list[dict[str, Any]] = []
    escalations: list[dict[str, Any]] = []
    for message in messages:
        verdict = classify(message, context["rules"], context["knowledge_base"], context["kb_min_score"], context["orders"])
        verdict["advisory"] = _advisory_intent(message, context)
        if verdict["action"] == ACTION_AUTO_REPLY and verdict["category"] not in NEVER_AUTO_REPLY:
            outcome = _handle_auto_reply(message, verdict, context, diagnostics)
            (replies if "reply" in outcome else escalations).append(outcome)
        else:
            escalations.append(_handle_escalation(message, verdict, context, diagnostics))
    return replies, escalations


def _advisory_intent(message: dict[str, Any], context: dict[str, Any]) -> str:
    """向 LLM 索取第二意見。**只記錄，不影響路由**（理由見 triage.py 檔頭設計決定 1）。"""
    llm: LLMClient = context["llm"]
    user = (
        f"渠道：{message.get('channel', '')}\n"
        f"主旨：{message.get('subject', '')}\n"
        f"內文：{message.get('text', '')}"
    )
    try:
        return str(llm.complete(system=context["advisory_prompt"], user=user, max_tokens=200)).strip()
    except LLMError as exc:
        # 顧問意見掛掉不影響分流結果，因此不升級為警報，只留痕跡
        return f"（LLM 顧問意見不可用：{exc}）"


# ── 發送 ────────────────────────────────────────────────────────────────────
def dispatch(
    notifier: Notifier,
    replies: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
    is_dry_run: bool,
) -> dict[str, Any]:
    """發送：升級卡片逐則送 Slack 值班頻道，自動回覆依自主權決定送出或留草稿。"""
    escalations_sent = 0
    for item in sorted(escalations, key=lambda row: row["priority"] != PRIORITY_HIGH):
        text = item["payload"] + (f"\n> Holding Response（待真人確認後送出）：{item['holding_response']}" if item["holding_response"] else "")
        if is_dry_run:
            continue
        if notifier.send(text, subject=f"[{item['priority']}] 客服升級｜{item['channel']}"):
            escalations_sent += 1
    replies_sent = 0
    for item in replies:
        if is_dry_run or not item["can_send"]:
            continue
        if notifier.send(item["reply"], subject=f"客服自動回覆｜{item['channel']}"):
            replies_sent += 1
    return {
        "escalations_sent": escalations_sent,
        "escalations_pending": len(escalations) - escalations_sent,
        "replies_sent": replies_sent,
        "replies_drafted": len(replies) - replies_sent,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（介面依 CONTRACT.md §6）。"""
    parser = argparse.ArgumentParser(description="demo14 多渠道客服分流")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式，零憑證零網路（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實渠道 API（與 --mock 互斥）")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不實際發送、不寫入去重狀態")
    parser.add_argument("--notify", choices=list(Notifier.SUPPORTED), default="console", help="通知管道（升級卡片的去處）")
    parser.add_argument("--config", default=str(MODULE_DIR / "config.yaml"), help="設定檔路徑")
    parser.add_argument("--state-file", default=None, help="去重狀態檔路徑（預設同目錄 .handled.json）")
    return parser


def _resolve_state_file(args: argparse.Namespace, triage_cfg: dict[str, Any]) -> Path:
    """決定去重狀態檔位置。未指定時以本檔目錄推算，禁止硬編碼。"""
    if args.state_file:
        return Path(args.state_file).expanduser().resolve()
    return state_path(str(triage_cfg.get("handled_state_file", HANDLED_FILE_DEFAULT)))


def _build_context(config: dict[str, Any], args: argparse.Namespace, diagnostics: Diagnostics) -> dict[str, Any]:
    """把設定檔攤平成流程用的 context，順便把知識庫與訂單資料讀進來。"""
    triage_cfg = config.get("triage") or {}
    knowledge_base = load_knowledge_base(str((config.get("knowledge_base") or {}).get("file", "")))
    check_knowledge_base(knowledge_base, diagnostics)
    rules = dict(triage_cfg.get("signals") or {})
    rules["vip_contacts"] = list(triage_cfg.get("vip_contacts") or [])
    rules["order_id_pattern"] = str(triage_cfg.get("order_id_pattern", r"\b([A-Z]{2}-\d{4,6})\b"))
    rules["refund_amount_threshold"] = triage_cfg.get("refund_amount_threshold", 0)
    return {
        "persona": config.get("persona") or {},
        "rules": rules,
        "knowledge_base": knowledge_base,
        "kb_min_score": int((config.get("knowledge_base") or {}).get("match_min_score", 2)),
        "orders": load_orders(str((config.get("orders") or {}).get("mock_file", ""))),
        "reply_max_words": int(triage_cfg.get("reply_max_words", REPLY_MAX_WORDS)),
        "gate": build_gate(config.get("runtime") or {}, diagnostics),
        "llm": LLMClient(mock=not args.live, context_note=CONTEXT_NOTE),
        "advisory_prompt": f"{load_prompt('persona')}\n\n---\n\n{load_prompt('intent_advisory')}",
    }


def _gather(config: dict[str, Any], args: argparse.Namespace, state_file: Path, diagnostics: Diagnostics) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """抓取全渠道訊息並去重。單一渠道失敗只發琥珀，全部失敗才是紅色警報。"""
    triage_cfg = config.get("triage") or {}
    channels = list(config.get("channels") or [])
    if not channels:
        diagnostics.red("沒有任何可監控的渠道", "config.yaml 的 channels 為空", "至少啟用一個渠道並填 mock_file / api_url")
    messages, errors = collect_messages(
        channels,
        is_mock=not args.live,
        env=dict(os.environ),
        delay_seconds=float(triage_cfg.get("fetch_delay_seconds", 1.0)),
    )
    for error in errors:
        diagnostics.amber(symptom=f"渠道抓取失敗：{error}", fix="檢查該渠道的 mock_file 是否存在，或 live 模式的憑證與網路；其他渠道已照常處理")
    if errors and not messages:
        diagnostics.red("所有渠道都抓不到訊息", "; ".join(errors), "先用 --mock 確認流程，再逐一修復渠道憑證")
    fresh, skipped = filter_new_messages(messages, load_handled_keys(state_file))
    return fresh, {"fetched": len(messages), "skipped": len(skipped), "errors": errors, "channels": len(channels)}


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config = load_config(args.config)
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=False)
    forced = enforce_hard_rules(config.setdefault("safety", {}), diagnostics)
    state_file = _resolve_state_file(args, config.get("triage") or {})
    messages, stats = _gather(config, args, state_file, diagnostics)
    context = _build_context(config, args, diagnostics)
    replies, escalations = triage_messages(messages, context, diagnostics)
    notified = dispatch(Notifier(channel=args.notify), replies, escalations, args.dry_run)
    if not args.dry_run and messages:
        save_handled_keys(state_file, load_handled_keys(state_file) | {message_key(item) for item in messages})
    return _build_result(args, config, context, diagnostics, stats, replies, escalations, notified, forced, state_file)


def _build_result(
    args: argparse.Namespace,
    config: dict[str, Any],
    context: dict[str, Any],
    diagnostics: Diagnostics,
    stats: dict[str, Any],
    replies: list[dict[str, Any]],
    escalations: list[dict[str, Any]],
    notified: dict[str, Any],
    forced: list[str],
    state_file: Path,
) -> dict[str, Any]:
    """組出回傳結構。獨立成函式，避免 run() 超過 30 行。"""
    module_cfg = config.get("module") or {}
    triage_cfg = config.get("triage") or {}
    gate: AutonomyGate = context["gate"]
    return {
        "module_id": str(module_cfg.get("id", "14")),
        "module_name": str(module_cfg.get("name", "多渠道客服分流")),
        "mode": "live" if args.live else "mock",
        "dry_run": bool(args.dry_run),
        "channels_configured": int(stats["channels"]),
        "fetched": int(stats["fetched"]),
        "new": len(replies) + len(escalations),
        "skipped_duplicates": int(stats["skipped"]),
        "fetch_errors": list(stats["errors"]),
        "response_target_minutes": int(triage_cfg.get("response_target_minutes", 2)),
        "knowledge_base_entries": len(context["knowledge_base"]),
        "auto_replied": replies,
        "escalated": escalations,
        "high_priority": [item for item in escalations if item["priority"] == PRIORITY_HIGH],
        "notified": notified,
        "notify_channel": args.notify,
        # 回報「設定的層級」而非 effective_level("*")：白名單制度下沒有一個叫 "*" 的收件人，
        # 拿假收件人去問實際層級，只會永遠得到降級後的 draft，讓報表看起來像沒設定過。
        # 每則回覆各自的實際層級記在 auto_replied[].autonomy。
        "autonomy_level": gate.level.value,
        "never_claim_human": bool(config["safety"]["never_claim_human"]),
        "refund_dispute_do_not_process": bool(config["safety"]["refund_dispute_do_not_process"]),
        "warnings": forced + list(gate.warnings),
        "amber_count": diagnostics.amber_count,
        "state_file": str(state_file),
    }


def _summarise(result: dict[str, Any]) -> str:
    """給操作者看的一行摘要（完整 JSON 在後面，這行是給人看的）。"""
    return (
        f"\n抓取 {result['fetched']} 則｜新進件 {result['new']} 則｜"
        f"自動回覆 {len(result['auto_replied'])} 則（已送 {result['notified']['replies_sent']}／"
        f"待審 {result['notified']['replies_drafted']}）｜"
        f"升級真人 {len(result['escalated'])} 則（高優先 {len(result['high_priority'])}）"
        f"｜自主權：{result['autonomy_level']}"
    )


def main() -> int:
    """解析參數 -> run() -> 印出結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except RedAlert as exc:
        print(f"紅色警報：{exc}", file=sys.stderr)
        return 1
    except (SupportFetchError, NotifierError, LLMError, AutonomyError) as exc:
        print(f"執行失敗：{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"設定或資料錯誤：{exc}", file=sys.stderr)
        return 1
    print(_summarise(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
