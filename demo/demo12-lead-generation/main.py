"""模組 #12 — 潛在客戶生成管線（主流程）。

原著數據（ch05_p05 / apxF_p06）：
    部署 1 Day｜每週回收 20+ 小時｜內部價值 $5,200/mo（管線容量 3-4x）
    售價 $900 首付 + $220/月（附錄 F；第 05 章另有一組較高定價，見 README）

核心價值：銷售團隊 40% 的時間花在永遠不會購買的人身上。這不是管線問題，
是「資格審查」問題。本模組把資格審查從主觀感受換成可稽核的 0-100 分。

流程：
    每日抓取觸發事件（募資 / 高管異動 / 擴編）
        → ICP 評分 0-100（產業 35 / 規模 25 / 觸發事件 25 / 技術特徵 15）
        → 法遵閘門（來源合法性、抑制名單、同意法域、退訂與寄件人識別）
        → 合格者進入三階段外聯（Day 0 破冰 / Day 4 佐證 / Day 9 收尾）

三個安全設計：
    1. **評分與發送權限分離**。評分不觸碰對方（READ_ONLY 安全），任何線索都能評；
       能不能寄是另一道獨立閘門。高分不等於可以寄。
    2. **`require_unsubscribe` 設定檔改不掉**，且退訂/識別區塊由程式串接，
       不交給 LLM 生成——法定必要資訊不能依賴模型「記得寫」。
    3. **自主權預設 DRAFT**。SUPERVISED_AUTO 必須配白名單，未命中一律降級；
       寄件人識別不齊備時，即使命中白名單也一律降級為草稿。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, tzinfo
from decimal import Decimal
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient, LLMError  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402
from lead_scorer import (  # noqa: E402
    ACTION_BLOCK,
    ACTION_ENRICH,
    ACTION_REJECT,
    BAND_COLD,
    BAND_HOT,
    BAND_WARM,
    CadenceStep,
    ComplianceBlocked,
    ComplianceGate,
    LeadScorer,
    OutreachPlanner,
    OutreachState,
    ScoreCard,
    ScoringError,
    build_cadence,
    build_compliance_gate,
    build_scorer,
    parse_iso,
    resolve_timezone,
    sum_pipeline_value,
)

MODULE_LABEL = "demo12-lead-generation"
LIVE_REQUIRED_ENV: tuple[str, ...] = ()
_SUMMARY_PREVIEW_WIDTH = 40
_TRUNCATION_SUFFIX = "…"

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出
CONTEXT_NOTE = (
    "這是冷開發（cold outreach）的第一次接觸。收件人從未與我們互動過，"
    "不要假裝熟識、不要杜撰任何數字或客戶名稱、不要製造期限壓力。"
    "所有事實只能取自輸入 JSON；結尾的寄件人識別與退訂區塊由系統附加，不要自行改寫。"
)


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_LABEL,
        description="潛在客戶生成管線：ICP 0-100 評分 + 法遵閘門 + 三階段外聯",
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
        help="外聯進度狀態檔路徑（預設取 config.yaml 的 safety.state_file）",
    )
    return parser


def _resolve_path(value: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，杜絕硬編碼使用者路徑。"""
    path = Path(value)
    return path if path.is_absolute() else MODULE_DIR / path


def _load_json(value: str | Path, expected: str) -> list:
    """讀取 mock JSON 清單，格式錯誤要明確報錯。"""
    path = _resolve_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoringError(f"{expected} 無法讀取或解析：{path}") from exc
    if not isinstance(payload, list):
        raise ScoringError(f"{expected} 應為 JSON 陣列：{path}")
    return payload


def _load_suppression(value: str | Path) -> list[str]:
    """讀取抑制名單。支援純字串清單與 {entry, reason, added_at} 物件清單。"""
    entries: list[str] = []
    for item in _load_json(value, "suppression_list"):
        raw = item.get("entry") if isinstance(item, dict) else item
        text = str(raw or "").strip()
        if text:
            entries.append(text)
    return entries


def _read_prompt(relative: str) -> str:
    """讀取提示詞檔（提示詞是資產，一律獨立成 .md，不內嵌在 .py）。"""
    path = _resolve_path(relative)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScoringError(f"提示詞檔無法讀取：{path}") from exc


def _require_live_env(diagnostics: Diagnostics) -> None:
    """--live 缺憑證要明確報錯退出，絕不靜默降級回 mock。"""
    missing = [key for key in LIVE_REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="金鑰未設定或未載入目前的 shell session",
            fix=f"設定 {', '.join(missing)} 後重跑，或改用 --mock 離線驗證",
        )


def _build_compliance(config: dict, diagnostics: Diagnostics, warnings: list[str]) -> ComplianceGate:
    """建立法遵閘門，並把硬規則覆寫與識別缺漏轉成 AMBER 警告。"""
    raw = config.get("compliance") or {}
    entries = _load_suppression(str(raw.get("suppression_list_file") or "mock/suppression_list.json"))
    gate = build_compliance_gate(config, entries)
    for message in gate.forced_overrides:
        warnings.append(message)
        diagnostics.amber(
            symptom=message,
            fix="本模組不允許停用退訂機制；請把 config.yaml 改回 require_unsubscribe: true",
        )
    if gate.identity_gaps:
        message = (
            f"寄件人識別不完整，缺少 {', '.join(gate.identity_gaps)}；本次執行一律不自動外送"
        )
        warnings.append(message)
        diagnostics.amber(
            symptom=message,
            fix="補齊 compliance.sender_identity 五個欄位（CAN-SPAM 要求實體地址與可用的退訂管道）",
        )
    return gate


def _build_gate(config: dict, diagnostics: Diagnostics, warnings: list[str]) -> AutonomyGate:
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
        warnings.append(warning)
        diagnostics.amber(symptom=warning, fix="滿 14 天並取得客戶簽核後再開全自動")
    return gate


def _resolve_state_file(args: argparse.Namespace, config: dict) -> Path:
    """決定狀態檔位置：命令列 > config.yaml > 模組預設。"""
    if args.state_file:
        return Path(args.state_file).expanduser().resolve()
    configured = str((config.get("safety") or {}).get("state_file") or "state/outreach_state.json")
    return _resolve_path(configured).resolve()


def _build_context(
    config: dict,
    args: argparse.Namespace,
    diagnostics: Diagnostics,
    is_mock: bool,
) -> dict:
    """把設定組裝成一次執行所需的全部元件。"""
    warnings: list[str] = []
    safety = config.get("safety") or {}
    tz, tz_warning = resolve_timezone(
        str(safety.get("timezone", "Asia/Taipei")),
        int(safety.get("timezone_fallback_offset_hours", 8)),
    )
    if tz_warning:
        warnings.append(tz_warning)
        diagnostics.amber(symptom=tz_warning, fix="安裝 tzdata 套件或改用固定偏移設定")
    compliance = _build_compliance(config, diagnostics, warnings)
    scorer, scorer_warnings = build_scorer(config)
    for message in scorer_warnings:
        warnings.append(message)
        diagnostics.amber(symptom=message, fix="把 scoring.weights 四項調整成總和 100")
    state_file = _resolve_state_file(args, config)
    # 只有「明確指定狀態檔」或「--live 正式執行」才落地，讓 --mock 完全可重現
    persist = (bool(args.state_file) or bool(args.live)) and not args.dry_run
    planner = OutreachPlanner(
        steps=build_cadence(config.get("cadence")),
        tz=tz,
        state=OutreachState(state_file, persist=persist),
        compliance=compliance,
    )
    return {
        "tz": tz,
        "warnings": warnings,
        "compliance": compliance,
        "scorer": scorer,
        "planner": planner,
        "gate": _build_gate(config, diagnostics, warnings),
        "llm": LLMClient(mock=is_mock, context_note=CONTEXT_NOTE),
        "notifier": Notifier(channel=args.notify),
        "dry_run": bool(args.dry_run),
        "state_file": state_file,
    }


def _resolve_now(config: dict, tz: tzinfo, is_mock: bool) -> datetime:
    """決定「現在」：mock 用設定的基準時間，確保結果可重現。"""
    if not is_mock:
        return datetime.now(tz)
    raw = str((config.get("mock") or {}).get("today") or "").strip()
    return parse_iso(raw, tz) if raw else datetime.now(tz)


def _base_entry(lead: dict, card: ScoreCard, reason: str, detail: str) -> dict:
    """所有 bucket 共用的線索摘要。金額一律轉字串保住 Decimal 精度。"""
    return {
        "lead_id": card.lead_id,
        "company": str(lead.get("company") or ""),
        "contact": str(lead.get("contact_name") or ""),
        "email": str(lead.get("email") or ""),
        "region": str(lead.get("region") or ""),
        "score": str(card.total),
        "band": card.band,
        "completeness": str(card.completeness),
        "missing_fields": list(card.missing_fields),
        "reason": reason,
        "detail": detail,
        "estimated_acv_usd": str(lead.get("estimated_acv_usd") or ""),
    }


def _subject(lead: dict, step: CadenceStep) -> str:
    """組出信件主旨。"""
    return f"[外聯 Day {step.day}｜{step.type}] {lead.get('company') or lead.get('id')}"


def _compose_message(context: dict, lead: dict, card: ScoreCard, step: CadenceStep) -> str:
    """呼叫 LLM 產生信件內容，並由程式附上法定的識別與退訂區塊。"""
    system = _read_prompt(step.prompt)
    payload = {
        "lead": {
            "company": lead.get("company"),
            "contact_name": lead.get("contact_name"),
            "title": lead.get("title"),
            "industry": lead.get("industry"),
            "employee_count": lead.get("employee_count"),
            "tech_stack": lead.get("tech_stack"),
            "trigger_event": lead.get("trigger_event"),
            "known_pain_point": lead.get("known_pain_point"),
            "source_note": lead.get("source_note"),
        },
        "icp_score": {
            "total": str(card.total),
            "band": card.band,
            "components": [item.as_dict() for item in card.components],
        },
        "step": {"day": step.day, "type": step.type, "max_chars": step.max_chars},
    }
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    body = context["llm"].complete(system=system, user=user, max_tokens=700)
    return f"{body.rstrip()}\n\n{context['compliance'].footer(lead)}"


def _outreach_entry(
    lead: dict,
    card: ScoreCard,
    step: CadenceStep,
    plan: dict,
    body: str,
    level: AutonomyLevel,
) -> dict:
    """組出外聯（已送出 / 待審草稿）的回報項目。"""
    entry = _base_entry(lead, card, plan["reason"], plan["detail"])
    entry.update(
        {
            "stage_day": step.day,
            "stage_type": step.type,
            "max_chars": step.max_chars,
            "due_at": plan["due_at"],
            "autonomy": level.value,
            "crm_push": card.band == BAND_HOT,
            "body": body,
        }
    )
    return entry


def _process_outreach(lead: dict, card: ScoreCard, now: datetime, context: dict) -> tuple[str, dict]:
    """處理一筆合格線索，回傳 ``(bucket, 紀錄)``。"""
    planner: OutreachPlanner = context["planner"]
    plan = planner.plan(lead, now)
    step = plan["step"]
    if step is None:
        return "skipped", _base_entry(lead, card, plan["reason"], plan["detail"])
    try:
        # 第二道閘門：實際送出前再查一次抑制名單與來源合法性
        planner.assert_can_send(lead)
    except ComplianceBlocked as exc:
        return "blocked", _base_entry(lead, card, exc.reason, exc.detail)
    body = _compose_message(context, lead, card, step)
    email = str(lead.get("email") or "")
    gate: AutonomyGate = context["gate"]
    level = gate.effective_level(email)
    entry = _outreach_entry(lead, card, step, plan, body, level)
    # 寄件人識別不齊備時，即使命中白名單也不得自動外送
    can_auto = gate.can_send(email) and context["compliance"].is_identity_complete
    if can_auto and not context["dry_run"]:
        context["notifier"].send(text=body, subject=_subject(lead, step))
        planner.mark_sent(lead, step)
        return "sent", entry
    return "drafted", entry


def _process_leads(leads: list, now: datetime, context: dict) -> dict[str, list]:
    """跑完評分 + 法遵 + 外聯判定，回傳各 bucket。"""
    buckets: dict[str, list] = {
        "sent": [],
        "drafted": [],
        "enrichment_queue": [],
        "rejected": [],
        "blocked": [],
        "skipped": [],
        "scorecards": [],
    }
    scorer: LeadScorer = context["scorer"]
    for lead in leads:
        card = scorer.score(lead, now)
        buckets["scorecards"].append(card.as_dict())
        action, reason, detail = scorer.decide(card, context["compliance"].check(lead))
        if action == ACTION_BLOCK:
            buckets["blocked"].append(_base_entry(lead, card, reason, detail))
        elif action == ACTION_ENRICH:
            buckets["enrichment_queue"].append(_base_entry(lead, card, reason, detail))
        elif action == ACTION_REJECT:
            buckets["rejected"].append(_base_entry(lead, card, reason, detail))
        else:
            bucket, entry = _process_outreach(lead, card, now, context)
            buckets[bucket].append(entry)
    return buckets


def _band_counts(scorecards: list) -> dict[str, int]:
    """統計各分數帶筆數。"""
    counts = {BAND_HOT: 0, BAND_WARM: 0, BAND_COLD: 0}
    for card in scorecards:
        band = str(card.get("band") or "")
        if band in counts:
            counts[band] += 1
    return counts


def _pipeline_value(buckets: dict[str, list]) -> Decimal:
    """合格線索（已送 + 草稿 + 尚未到期）的預估年約總值。"""
    qualified = buckets["sent"] + buckets["drafted"] + buckets["skipped"]
    return sum_pipeline_value(qualified)


def _build_result(
    config: dict,
    args: argparse.Namespace,
    now: datetime,
    leads: list,
    buckets: dict[str, list],
    context: dict,
    diagnostics: Diagnostics,
) -> dict:
    """組出統一的回傳結構（全部 JSON-safe）。"""
    module = config.get("module") or {}
    pricing = config.get("pricing") or {}
    compliance: ComplianceGate = context["compliance"]
    return {
        "module_id": str(module.get("id", "12")),
        "module_name": str(module.get("name", "潛在客戶生成管線")),
        "mode": "live" if args.live else "mock",
        "dry_run": bool(args.dry_run),
        "notify_channel": args.notify,
        "timezone": str((config.get("safety") or {}).get("timezone", "")),
        "reference_now": now.isoformat(),
        "total_leads": len(leads),
        "sent": buckets["sent"],
        "drafted": buckets["drafted"],
        "enrichment_queue": buckets["enrichment_queue"],
        "rejected": buckets["rejected"],
        "blocked": buckets["blocked"],
        "skipped": buckets["skipped"],
        "scorecards": buckets["scorecards"],
        "band_counts": _band_counts(buckets["scorecards"]),
        "pipeline_value_usd": str(_pipeline_value(buckets)),
        "weights": context["scorer"].weights.as_dict(),
        "min_score_for_outreach": str(context["scorer"].min_score),
        "require_unsubscribe": compliance.is_unsubscribe_required,
        "is_sender_identity_complete": compliance.is_identity_complete,
        "suppression_size": compliance.suppression_size,
        "state_file": str(context["state_file"]),
        "is_state_persisted": context["planner"].state.is_persisted,
        "pricing": {
            "source": str(pricing.get("source", "")),
            "setup_usd": str(pricing.get("setup_usd", "")),
            "monthly_usd": str(pricing.get("monthly_usd", "")),
        },
        "warnings": context["warnings"],
        "amber_count": diagnostics.amber_count,
    }


def _first_line(text: str, width: int = _SUMMARY_PREVIEW_WIDTH) -> str:
    """取信件內容第一行的前 width 字元，用於草稿摘要預覽。"""
    stripped = (text or "").strip()
    head = stripped.splitlines()[0] if stripped else ""
    return head if len(head) <= width else head[:width] + _TRUNCATION_SUFFIX


def _summarise(result: dict) -> str:
    """組出給操作者的摘要文字。"""
    counts = result["band_counts"]
    lines = [
        f"【{result['module_name']}】{result['reference_now']}（{result['mode']} 模式）",
        f"線索 {result['total_leads']} 筆｜Hot {counts['hot']}／Warm {counts['warm']}／Cold {counts['cold']}"
        f"｜管線價值 ${result['pipeline_value_usd']}",
        f"自動送出 {len(result['sent'])}｜待審草稿 {len(result['drafted'])}"
        f"｜待補資料 {len(result['enrichment_queue'])}｜不合格 {len(result['rejected'])}"
        f"｜法遵阻擋 {len(result['blocked'])}｜暫不外聯 {len(result['skipped'])}",
        f"退訂機制：{'啟用（不可停用）' if result['require_unsubscribe'] else '異常'}"
        f"｜寄件人識別：{'完整' if result['is_sender_identity_complete'] else '不完整（全部降級草稿）'}",
    ]
    for item in result["sent"]:
        lines.append(f"  [已送] {item['company']} {item['score']} 分 Day {item['stage_day']}")
    for item in result["drafted"]:
        lines.append(
            f"  [草稿] {item['company']} {item['score']} 分 Day {item['stage_day']}"
            f"｜{len(item['body'])} 字元｜{_first_line(item['body'])}"
        )
    for item in result["enrichment_queue"]:
        lines.append(f"  [補資料] {item['company']} — 缺 {'、'.join(item['missing_fields'])}")
    for item in result["rejected"]:
        lines.append(f"  [不合格] {item['company']} {item['score']} 分 — {item['detail']}")
    for item in result["blocked"]:
        lines.append(f"  [法遵阻擋] {item['company']} — {item['reason']}：{item['detail']}")
    for item in result["skipped"]:
        lines.append(f"  [暫不外聯] {item['company']} — {item['detail']}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config = load_config(_resolve_path(args.config))
    diagnostics = Diagnostics(MODULE_LABEL)
    is_mock = not args.live
    if not is_mock:
        _require_live_env(diagnostics)
    context = _build_context(config, args, diagnostics, is_mock)
    leads = _load_json(str((config.get("mock") or {}).get("leads") or "mock/leads.json"), "leads")
    now = _resolve_now(config, context["tz"], is_mock)
    buckets = _process_leads(leads, now, context)
    return _build_result(config, args, now, leads, buckets, context, diagnostics)


def main() -> int:
    """解析參數 -> run() -> 印出/發送結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (ScoringError, AutonomyError, NotifierError, LLMError, FileNotFoundError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    summary = _summarise(result)
    print(summary)
    # console 管道等同上面的 print，再送一次只會讓輸出重複
    if not args.dry_run and args.notify != "console":
        Notifier(channel=args.notify).send(text=summary, subject="潛在客戶管線執行摘要")
    return 0


if __name__ == "__main__":
    sys.exit(main())
