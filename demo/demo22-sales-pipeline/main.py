"""模組 #22 — 全漏斗業務自動化（主流程）。

書中數據（apxG_p06–p07、ch07_p05）：
    部署 1 週｜售價 $4,500 setup + $2,000/mo｜內部回收工時原簡報未提供
    客戶端成效：交易週期縮短 30-50%、轉換率 +8-12%、業務行政時間 -60%

流程（Pipeline Orchestrator 統籌五條鏈路）：
    CRM Stage Change Event (Webhook) 或 Cron Velocity Check
        → 套用階段轉移（非法轉移擋下並記錄）
        → SLA 掃描（Enrichment < 2 小時，超時發 AMBER + 稽核，不可靜默）
        → 依 stage_map 觸發對應鏈路
            lead_captured  → Lead Enrichment (#12)
            discovery      → Proposal Engine (#15)
            proposal_sent  → 5-touch Follow-Up (#10)，halt_on_reply
            closed_won     → Onboarding Chain (#13)
            closed_lost    → 90-day Re-Nurture（3 封）

三層安全設計：
    1. `halt_on_reply` 是不可停用的硬規則。設定檔改成 false 也會被強制覆寫，
       並發出 AMBER；且在**每一次實際送出前**再複查一次。
    2. 全域安全閥：所有對外 API 呼叫前必經 `--dry-run` 內部通訊測試。
       `--live` 找不到對應設定指紋的 dry-run 收據就直接 RED 中止。
    3. 自主權預設 DRAFT。SUPERVISED_AUTO 必須配白名單，未命中一律降級為草稿。
       每一個動作（草稿 / 執行 / 中止 / 超時）都寫入 JSONL 稽核軌跡。
"""

from __future__ import annotations

import argparse
import hashlib
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
from audit import (  # noqa: E402
    ACTION_CHAIN_DRAFTED,
    ACTION_CHAIN_EXECUTED,
    ACTION_CHAIN_HALTED,
    ACTION_DRY_RUN_RECEIPT,
    ACTION_EVENT_REJECTED,
    ACTION_RUN_COMPLETED,
    ACTION_RUN_STARTED,
    ACTION_SAFETY_OVERRIDE,
    ACTION_SLA_BREACH,
    DEFAULT_AUDIT_PATH,
    AuditLog,
    body_digest,
)
from pipeline import (  # noqa: E402
    ACTION_RUN,
    DEFAULT_STATE_PATH,
    Chain,
    PipelineError,
    PipelineState,
    SalesPipeline,
    SequenceHalted,
    build_chains,
    build_stage_map,
    collect_forced_overrides,
    parse_iso,
    pipeline_value,
    resolve_timezone,
)

MODULE_LABEL = "demo22-sales-pipeline"
LIVE_REQUIRED_ENV = ("ANTHROPIC_API_KEY", "CRM_API_TOKEN")

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出
CONTEXT_NOTE = (
    "這是 B2B 全漏斗業務自動化的產出。收件人是企業決策者，"
    "不要重複介紹公司、不要用行銷術語、不要施壓，一切數字只能取自輸入資料，"
    "缺資料就寫「待補」，禁止杜撰金額、比率與案例。"
)


# ---------------------------------------------------------------- CLI --
def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6，另加兩個企業級旗標）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_LABEL,
        description="全漏斗業務自動化：階段路由 + SLA 監控 + 5-touch 追蹤（回覆即中止）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實 API")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不實際發送")
    parser.add_argument(
        "--notify", choices=list(Notifier.SUPPORTED), default="console", help="通知管道"
    )
    parser.add_argument(
        "--config", default=str(MODULE_DIR / "config.yaml"), help="設定檔路徑"
    )
    parser.add_argument(
        "--state-file", default=str(DEFAULT_STATE_PATH), help="管線進度與 dry-run 收據檔"
    )
    parser.add_argument(
        "--audit-file", default=str(DEFAULT_AUDIT_PATH), help="JSONL 稽核軌跡檔"
    )
    return parser


def _resolve_path(value: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，杜絕硬編碼使用者路徑。"""
    path = Path(value)
    return path if path.is_absolute() else MODULE_DIR / path


def _load_json_list(value: str | Path, expected: str) -> list:
    """讀取 mock JSON 清單，格式錯誤要明確報錯。"""
    path = _resolve_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{expected} 無法讀取或解析：{path}") from exc
    if not isinstance(payload, list):
        raise PipelineError(f"{expected} 應為 JSON 陣列：{path}")
    return payload


def _read_prompt(relative: str) -> str:
    """讀取提示詞檔（提示詞是資產，一律獨立成 .md，不內嵌在 .py）。"""
    path = _resolve_path(relative)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PipelineError(f"提示詞檔無法讀取：{path}") from exc


# ---------------------------------------------------------------- 前置檢查 --
def _require_live_env(diagnostics: Diagnostics) -> None:
    """--live 缺憑證要明確報錯退出，絕不靜默降級回 mock。"""
    missing = [key for key in LIVE_REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="金鑰未設定或未載入目前的 shell session",
            fix=f"設定 {', '.join(missing)} 後重跑，或改用 --mock 離線驗證",
        )


def config_fingerprint(config: dict) -> str:
    """對「會影響對外呼叫」的設定區段取指紋。

    只涵蓋 integrations 與 pipeline：改了通知文案不必重跑通訊測試，
    但改了端點或階段路由就必須重跑——否則 dry-run 收據等於背書了另一份設定。
    """
    payload = {
        "integrations": config.get("integrations") or {},
        "pipeline": config.get("pipeline") or {},
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def _require_dry_run_receipt(
    state: PipelineState,
    fingerprint: str,
    safety: dict,
    diagnostics: Diagnostics,
) -> None:
    """全域安全閥：--live 正式送出前，必須先有同指紋的 dry-run 收據。"""
    if not bool(safety.get("require_dry_run_receipt", True)):
        return
    if state.dry_run_receipt(fingerprint) is not None:
        return
    diagnostics.red(
        symptom=f"設定指紋 {fingerprint} 尚未通過 --dry-run 內部通訊測試",
        cause="apxG_p03 規定所有對外 API 呼叫前必經 dry-run；本次找不到對應收據",
        fix=f"先執行 `python main.py --live --dry-run --state-file {state.path}` 再重跑",
    )


# ---------------------------------------------------------------- 組裝 --
def _build_pipeline(
    config: dict,
    state: PipelineState,
    diagnostics: Diagnostics,
) -> tuple[SalesPipeline, tzinfo, list[str]]:
    """依設定組出管線狀態機，回傳 (pipeline, 時區, 警告清單)。"""
    safety = config.get("safety") or {}
    warnings: list[str] = []
    tz, tz_warning = resolve_timezone(
        safety.get("timezone", "Asia/Taipei"),
        int(safety.get("timezone_fallback_offset_hours", 8)),
    )
    if tz_warning:
        warnings.append(tz_warning)
        diagnostics.amber(symptom=tz_warning, fix="安裝 tzdata 套件或改用固定偏移設定")
    raw_pipeline = config.get("pipeline") or {}
    chains = build_chains(raw_pipeline.get("chains"))
    overrides = collect_forced_overrides(raw_pipeline.get("chains"))
    for override in overrides:
        diagnostics.amber(
            symptom=override,
            fix="本模組不允許停用追蹤序列的回覆中止；請把 halt_on_reply 改回 true",
        )
    warnings.extend(overrides)
    pipeline = SalesPipeline(
        chains=chains,
        stage_map=build_stage_map(raw_pipeline.get("stage_map"), chains),
        tz=tz,
        state=state,
        forced_overrides=overrides,
    )
    return pipeline, tz, warnings


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
    raw = str((config.get("mock") or {}).get("today") or "").strip()
    return parse_iso(raw, tz) if raw else datetime.now(tz)


def _endpoint_for(integrations: dict, chain_name: str, deal_id: str) -> tuple[str, str]:
    """回傳某鏈路對外呼叫的 (endpoint, method)，供 dry-run 通訊測試列印。"""
    crm_base = str(integrations.get("crm_base_url") or "").rstrip("/")
    crm_path = str(integrations.get("crm_update_path") or "/v1/deals/{deal_id}")
    outreach = str(integrations.get("outreach_service_url") or "")
    table = {
        "enrichment": (f"{crm_base}{crm_path.format(deal_id=deal_id)}", "PATCH"),
        "proposal": (str(integrations.get("proposal_service_url") or ""), "POST"),
        "follow_up": (outreach, "POST"),
        "renurture": (outreach, "POST"),
        "onboarding": (str(integrations.get("onboarding_service_url") or ""), "POST"),
    }
    return table.get(chain_name, ("", "POST"))


# ---------------------------------------------------------------- 執行鏈路 --
def _compose_output(llm: LLMClient, deal: dict, chain: Chain, step: dict) -> str:
    """呼叫 LLM 產生此節點的產出（enrichment 摘要 / 提案綱要 / 信件內容）。"""
    system = _read_prompt(str(step["prompt"]))
    payload = {
        "chain": {"name": chain.name, "label": chain.label},
        "step": {"day": step.get("day"), "type": step.get("type")},
        "deal": {
            "id": deal.get("id"),
            "company": deal.get("company"),
            "contact": deal.get("contact"),
            "industry": deal.get("industry"),
            "stage": deal.get("stage"),
            "amount_usd": str(deal.get("amount_usd")),
            "pain_point": deal.get("pain_point"),
            "discovery_notes": deal.get("discovery_notes"),
            "proposal_sent_at": deal.get("proposal_sent_at"),
            "closed_at": deal.get("closed_at"),
            "lost_reason": deal.get("lost_reason"),
            "enrichment": deal.get("enrichment"),
        },
    }
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    return llm.complete(system=system, user=user, max_tokens=900)


def _register_call(context: dict, chain: Chain, deal: dict, body: str) -> dict:
    """登記一次「將要發生」的對外呼叫（dry-run 通訊測試的核心資料）。"""
    endpoint, method = _endpoint_for(
        context["integrations"], chain.name, str(deal.get("id") or "")
    )
    call = {
        "chain": chain.name,
        "deal_id": str(deal.get("id") or ""),
        "endpoint": endpoint,
        "method": method,
        "recipient": str(deal.get("email") or ""),
        "payload_preview": {
            "stage": str(deal.get("stage") or ""),
            "body": body_digest(body),
            "excerpt": body[:120],
        },
    }
    context["planned_calls"].append(call)
    return call


def _entry(decision: dict, deal: dict, body: str, level: AutonomyLevel) -> dict:
    """把一次產出整理成回報用的 dict。"""
    return {
        "deal_id": decision["deal_id"],
        "company": decision["company"],
        "contact": decision["contact"],
        "email": str(deal.get("email") or ""),
        "stage": decision["stage"],
        "chain": decision["chain"],
        "chain_label": decision["chain_label"],
        "step_day": decision["step"]["day"],
        "step_type": decision["step"]["type"],
        "due_at": decision["due_at"],
        "autonomy": level.value,
        "body": body,
    }


def _halt_entry(decision: dict) -> dict:
    """把中止決策整理成回報用的 dict。"""
    return {
        "deal_id": decision["deal_id"],
        "company": decision["company"],
        "stage": decision["stage"],
        "chain": decision["chain"],
        "reason": decision["reason"],
        "detail": decision["detail"],
    }


def _process_one(decision: dict, deal: dict, context: dict) -> tuple[str, dict]:
    """處理單一「該執行」決策，回傳 (bucket, 紀錄)。

    bucket 為 "executed" / "drafted" / "halted"。
    """
    pipeline: SalesPipeline = context["pipeline"]
    chain = pipeline.chains[decision["chain"]]
    try:
        # 第二道閘門：實際送出前再查一次是否已回覆
        pipeline.assert_can_send(deal, chain)
    except SequenceHalted as exc:
        halted = dict(decision, reason=exc.reason, detail=exc.detail)
        _audit_halt(context, halted, "發送前複查（第二道閘門）")
        return "halted", _halt_entry(halted)
    body = _compose_output(context["llm"], deal, chain, decision["step"])
    call = _register_call(context, chain, deal, body)
    gate: AutonomyGate = context["gate"]
    email = str(deal.get("email") or "")
    level = gate.effective_level(email)
    entry = _entry(decision, deal, body, level)
    if gate.can_send(email) and not context["dry_run"]:
        return "executed", _execute(context, decision, deal, chain, entry, call)
    _audit_action(context, ACTION_CHAIN_DRAFTED, decision, entry, is_approved=False)
    return "drafted", entry


def _execute(
    context: dict,
    decision: dict,
    deal: dict,
    chain: Chain,
    entry: dict,
    call: dict,
) -> dict:
    """白名單命中且非 dry-run：真的送出並記帳。"""
    if chain.is_outbound:
        context["notifier"].send(text=entry["body"], subject=_subject(decision))
    context["pipeline"].mark_sent(deal, chain, chain.step_for_day(entry["step_day"]))
    _audit_action(context, ACTION_CHAIN_EXECUTED, decision, entry, is_approved=True, call=call)
    return entry


def _subject(decision: dict) -> str:
    """組出通知主旨。"""
    return (
        f"[{decision['chain_label']}] {decision['company']} — "
        f"Day {decision['step']['day']} {decision['step']['type']}"
    )


# ---------------------------------------------------------------- 稽核 --
def _audit_action(
    context: dict,
    action: str,
    decision: dict,
    entry: dict,
    is_approved: bool,
    call: dict | None = None,
) -> None:
    """把一次鏈路動作寫入稽核軌跡（正文只記長度與雜湊，不落地）。"""
    detail = {
        "chain": decision["chain"],
        "stage": decision["stage"],
        "step_day": entry["step_day"],
        "step_type": entry["step_type"],
        "autonomy": entry["autonomy"],
        "body": body_digest(entry["body"]),
    }
    if call is not None:
        detail["endpoint"] = call["endpoint"]
        detail["method"] = call["method"]
    context["audit"].record(
        action=action,
        subject=decision["deal_id"],
        rationale=f"{decision['reason']}｜{decision['detail']}",
        is_human_approved=is_approved,
        when=context["now"],
        extra=detail,
    )


def _audit_halt(context: dict, decision: dict, gate_label: str) -> None:
    """把中止決策寫入稽核軌跡。"""
    context["audit"].record(
        action=ACTION_CHAIN_HALTED,
        subject=decision["deal_id"],
        rationale=f"{gate_label}：{decision['reason']}｜{decision['detail']}",
        is_human_approved=False,
        when=context["now"],
        extra={"chain": decision["chain"], "stage": decision["stage"]},
    )


def _audit_preflight(
    context: dict,
    rejected: list[dict],
    breaches: list[dict],
    warnings: list[str],
) -> None:
    """把事件拒絕、SLA 超時、安全覆寫三類前置事實寫入稽核軌跡。"""
    audit: AuditLog = context["audit"]
    for item in rejected:
        audit.record(
            ACTION_EVENT_REJECTED,
            subject=item["deal_id"],
            rationale=f"{item['reason']}：{item['detail']}",
            is_human_approved=False,
            when=context["now"],
            extra=item,
        )
    for breach in breaches:
        audit.record(
            ACTION_SLA_BREACH,
            subject=breach["deal_id"],
            rationale=(
                f"{breach['chain_label']} SLA {breach['sla_minutes']} 分鐘已超時 "
                f"{breach['overdue_minutes']} 分鐘"
            ),
            is_human_approved=False,
            when=context["now"],
            extra=breach,
        )
    for warning in warnings:
        if "halt_on_reply" in warning:
            audit.record(
                ACTION_SAFETY_OVERRIDE,
                subject=MODULE_LABEL,
                rationale=warning,
                is_human_approved=False,
                when=context["now"],
            )


def _alert_preflight(
    diagnostics: Diagnostics,
    rejected: list[dict],
    breaches: list[dict],
) -> list[str]:
    """事件拒絕與 SLA 超時一律發 AMBER——靜默的 SLA 等於沒有 SLA。"""
    warnings: list[str] = []
    for item in rejected:
        message = (
            f"CRM 事件 {item['event_id']} 被拒（{item['reason']}）："
            f"{item['deal_id']} {item['from_stage']} -> {item['to_stage']}"
        )
        warnings.append(message)
        diagnostics.amber(symptom=message, fix=f"檢查 CRM 階段設定；{item['detail']}")
    for breach in breaches:
        message = (
            f"SLA 超時：{breach['deal_id']}（{breach['company']}）{breach['chain_label']} "
            f"逾時 {breach['overdue_minutes']} 分鐘，門檻 {breach['sla_minutes']} 分鐘"
        )
        warnings.append(message)
        diagnostics.amber(
            symptom=message,
            fix=f"立即人工介入 {breach['deal_id']}；截止時間為 {breach['deadline_at']}",
        )
    return warnings


# ---------------------------------------------------------------- 主流程 --
def run(args: argparse.Namespace) -> dict:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config = load_config(_resolve_path(args.config))
    diagnostics = Diagnostics(MODULE_LABEL)
    is_mock = not args.live
    if not is_mock:
        _require_live_env(diagnostics)
    safety = config.get("safety") or {}
    # dry-run 必須落地寫收據，否則 --live 永遠拿不到通行證
    persist = bool(args.dry_run) or bool(args.live)
    state = PipelineState(path=_resolve_path(args.state_file), persist=persist)
    fingerprint = config_fingerprint(config)
    if args.live and not args.dry_run:
        _require_dry_run_receipt(state, fingerprint, safety, diagnostics)
    pipeline, tz, warnings = _build_pipeline(config, state, diagnostics)
    gate, gate_warnings = _build_gate(config, diagnostics)
    warnings.extend(gate_warnings)
    now = _resolve_now(config, tz, is_mock)
    context = _build_context(config, args, pipeline, gate, is_mock, now, fingerprint)
    return _execute_run(config, args, context, state, fingerprint, warnings, diagnostics)


def _build_context(
    config: dict,
    args: argparse.Namespace,
    pipeline: SalesPipeline,
    gate: AutonomyGate,
    is_mock: bool,
    now: datetime,
    fingerprint: str,
) -> dict:
    """組出各處理函式共用的執行環境。"""
    safety = config.get("safety") or {}
    return {
        "pipeline": pipeline,
        "gate": gate,
        "llm": LLMClient(mock=is_mock, context_note=CONTEXT_NOTE),
        "notifier": Notifier(channel=args.notify),
        "integrations": config.get("integrations") or {},
        "dry_run": bool(args.dry_run),
        "now": now,
        "planned_calls": [],
        "audit": AuditLog(
            path=_resolve_path(args.audit_file),
            module_name=MODULE_LABEL,
            run_id=f"{fingerprint}-{now.isoformat()}",
            is_dry_run=bool(args.dry_run),
            enabled=bool(safety.get("audit_enabled", True)),
        ),
    }


def _execute_run(
    config: dict,
    args: argparse.Namespace,
    context: dict,
    state: PipelineState,
    fingerprint: str,
    warnings: list[str],
    diagnostics: Diagnostics,
) -> dict:
    """載入資料 → 套事件 → 掃 SLA → 跑鏈路 → 組結果。"""
    pipeline: SalesPipeline = context["pipeline"]
    now: datetime = context["now"]
    mock_cfg = config.get("mock") or {}
    raw_deals = _load_json_list(mock_cfg.get("deals", "mock/deals.json"), "deals")
    events = _load_json_list(mock_cfg.get("crm_events", "mock/crm_events.json"), "crm_events")
    context["audit"].record(
        ACTION_RUN_STARTED,
        subject=MODULE_LABEL,
        rationale=f"載入 {len(raw_deals)} 筆交易與 {len(events)} 則 CRM 事件",
        is_human_approved=False,
        when=now,
        extra={"fingerprint": fingerprint, "mode": "mock" if not args.live else "live"},
    )
    deals, rejected = pipeline.apply_events(raw_deals, events)
    breaches = pipeline.scan_sla(deals, now)
    warnings.extend(_alert_preflight(diagnostics, rejected, breaches))
    _audit_preflight(context, rejected, breaches, warnings)
    buckets = _run_chains(deals, now, context)
    if args.dry_run:
        _finish_dry_run(context, state, fingerprint, now)
    return _build_result(
        config, args, context, deals, buckets, rejected, breaches, warnings, diagnostics
    )


def _run_chains(deals: list, now: datetime, context: dict) -> dict[str, list]:
    """跑完所有鏈路判定 + 產出，回傳三個 bucket。"""
    buckets: dict[str, list] = {"executed": [], "drafted": [], "halted": []}
    pipeline: SalesPipeline = context["pipeline"]
    index = {str(item.get("id") or ""): item for item in deals}
    for decision in pipeline.plan(deals, now):
        if decision["action"] != ACTION_RUN:
            _audit_halt(context, decision, "排程判定（第一道閘門）")
            buckets["halted"].append(_halt_entry(decision))
            continue
        deal = index[decision["deal_id"]]
        bucket, entry = _process_one(decision, deal, context)
        buckets[bucket].append(entry)
    return buckets


def _finish_dry_run(
    context: dict,
    state: PipelineState,
    fingerprint: str,
    now: datetime,
) -> None:
    """dry-run 收尾：印出將呼叫的端點與內容，並發出通行收據。"""
    print("── dry-run 內部通訊測試（不會實際送出）──")
    for call in context["planned_calls"]:
        print(
            f"  {call['method']} {call['endpoint']}｜deal={call['deal_id']}"
            f"｜chain={call['chain']}｜recipient={call['recipient'] or '(內部)'}"
        )
        print(f"      內容摘要：{call['payload_preview']['excerpt']!r}")
    if not context["planned_calls"]:
        print("  （本次沒有任何對外呼叫）")
    state.record_dry_run(fingerprint, now)
    context["audit"].record(
        ACTION_DRY_RUN_RECEIPT,
        subject=fingerprint,
        rationale=f"dry-run 通訊測試通過，登記 {len(context['planned_calls'])} 筆待發呼叫",
        is_human_approved=False,
        when=now,
        extra={"planned_calls": len(context["planned_calls"]), "state_file": str(state.path)},
    )


def _build_result(
    config: dict,
    args: argparse.Namespace,
    context: dict,
    deals: list,
    buckets: dict[str, list],
    rejected: list[dict],
    breaches: list[dict],
    warnings: list[str],
    diagnostics: Diagnostics,
) -> dict:
    """組出統一的回傳結構（金額以字串輸出，保住 Decimal 精度又可 JSON 序列化）。"""
    module = config.get("module") or {}
    metrics = config.get("metrics") or {}
    result = {
        "module_id": str(module.get("id", "22")),
        "module_name": str(module.get("name", "全漏斗業務自動化")),
        "mode": "mock" if not args.live else "live",
        "dry_run": bool(args.dry_run),
        "notify_channel": args.notify,
        "timezone": str((config.get("safety") or {}).get("timezone", "")),
        "reference_now": context["now"].isoformat(),
        "halt_on_reply": True,
        "total_deals": len(deals),
        "open_pipeline_value_usd": str(pipeline_value(deals)),
        "stage_counts": _stage_counts(deals),
        "executed": buckets["executed"],
        "drafted": buckets["drafted"],
        "halted": buckets["halted"],
        "sla_breaches": breaches,
        "rejected_events": rejected,
        "planned_calls": context["planned_calls"],
        "audit_file": str(context["audit"].path),
        "audit_entries": len(context["audit"].entries),
        "state_file": str(context["pipeline"].state.path),
        "warnings": warnings,
        "amber_count": diagnostics.amber_count,
        "metrics": dict(metrics),
    }
    context["audit"].record(
        ACTION_RUN_COMPLETED,
        subject=MODULE_LABEL,
        rationale=(
            f"執行 {len(buckets['executed'])}｜草稿 {len(buckets['drafted'])}"
            f"｜中止 {len(buckets['halted'])}｜SLA 超時 {len(breaches)}"
        ),
        is_human_approved=False,
        when=context["now"],
        extra={"amber_count": diagnostics.amber_count},
    )
    result["audit_entries"] = len(context["audit"].entries)
    return result


def _stage_counts(deals: list) -> dict[str, int]:
    """統計各階段的交易數（管線健康度報表的最小版本）。"""
    counts: dict[str, int] = {}
    for deal in deals:
        stage = str(deal.get("stage") or "unknown")
        counts[stage] = counts.get(stage, 0) + 1
    return counts


def _summarise(result: dict) -> str:
    """組出給操作者的摘要文字。"""
    lines = [
        f"【{result['module_name']}】{result['reference_now']}（{result['mode']} 模式"
        f"{'／dry-run' if result['dry_run'] else ''}）",
        f"交易 {result['total_deals']} 筆｜在途管線值 ${result['open_pipeline_value_usd']}"
        f"｜自動執行 {len(result['executed'])}｜待審草稿 {len(result['drafted'])}"
        f"｜中止 {len(result['halted'])}",
        f"halt_on_reply：{'啟用（不可停用）' if result['halt_on_reply'] else '異常'}"
        f"｜SLA 超時 {len(result['sla_breaches'])} 筆"
        f"｜事件拒絕 {len(result['rejected_events'])} 則",
    ]
    lines.extend(_summarise_items(result))
    lines.append(f"稽核軌跡：{result['audit_file']}（本次 {result['audit_entries']} 列）")
    return "\n".join(lines)


def _summarise_items(result: dict) -> list[str]:
    """逐項列出各 bucket 的內容。"""
    lines: list[str] = []
    for breach in result["sla_breaches"]:
        lines.append(
            f"  [SLA] {breach['deal_id']}（{breach['company']}）{breach['chain_label']}"
            f" 逾時 {breach['overdue_minutes']} 分"
        )
    for item in result["rejected_events"]:
        lines.append(
            f"  [拒絕] {item['event_id']} {item['deal_id']}："
            f"{item['from_stage']} -> {item['to_stage']}（{item['reason']}）"
        )
    for item in result["executed"]:
        lines.append(
            f"  [已送] {item['deal_id']}（{item['company']}）{item['chain_label']}"
            f" Day {item['step_day']}"
        )
    for item in result["drafted"]:
        lines.append(
            f"  [草稿] {item['deal_id']}（{item['company']}）{item['chain_label']}"
            f" Day {item['step_day']}"
        )
    for item in result["halted"]:
        lines.append(f"  [中止] {item['deal_id']}（{item['company']}）— {item['reason']}")
    return lines


def main() -> int:
    """解析參數 -> run() -> 印出/發送結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (PipelineError, FileNotFoundError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    summary = _summarise(result)
    print(summary)
    # console 管道等同上面的 print，再送一次只會讓輸出重複
    if not args.dry_run and args.notify != "console":
        Notifier(channel=args.notify).send(text=summary, subject="全漏斗管線執行摘要")
    return 0


if __name__ == "__main__":
    sys.exit(main())
