"""模組 #21：多智能體行銷協同群（The Multi-Agent Marketing Swarm）— 主流程。

執行邏輯逐字對應 apxG_p05 的四步驟：

    ① brand_context.yml
    ② 整合層（CMS / GA4 / Social / CRM API）＋ 強制 --dry-run 內部通訊測試
    ③ 每週日 07:00 生成策略備忘錄（approval_required: true）
    ④ Task Dispatch -> 五條 Agent Action 平行執行

整份 Level 3 只保留**一個**人類監督節點：策略備忘錄。備忘錄沒被具名的人核准，
五個 Sub-agent 仍會產內容，但一律停在草稿，publish_mode 永遠不會是 auto。
這是「未核准不可發布」在程式層的硬條件，不是提示詞裡的請求。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parent
# demo/ 在上一層（_shared 從那裡匯入）；再把本目錄加進來讓 orchestrator/audit 可被匯入
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402

from audit import AuditError, AuditLog  # noqa: E402
from orchestrator import (  # noqa: E402
    STATUS_DISPATCHED,
    AgentSpec,
    Orchestrator,
    SubAgent,
    SwarmError,
    current_memo_slot,
    dispatch_agents,
    format_preflight_report,
    generate_strategy_memo,
    preflight_dry_run,
    resolve_timezone,
)

MODULE_NAME = "demo21-marketing-swarm"

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出。
# 蜂群最危險的幻覺是「五個 agent 各自編一套品牌故事」，所以把邊界講死在這裡。
CONTEXT_NOTE = (
    "你是企業行銷蜂群中的一個子智能體。品牌事實、語氣、禁用詞、可引用的數字"
    "全部來自 Orchestrator 級聯下來的 brand_context，不得自行補充或推論。"
    "brand.proof_points 以外的數字一律不得出現。輸出必須是單一 JSON 物件。"
)


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6，另加 Level 3 專屬旗標）。"""
    parser = argparse.ArgumentParser(
        description="模組 #21：多智能體行銷協同群（Orchestrator + 5 Sub-agent）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mock", action="store_true", default=True, help="離線模式，不呼叫真實 API（預設）"
    )
    mode.add_argument(
        "--live", action="store_true", help="串接真實 Anthropic API（需要 ANTHROPIC_API_KEY）"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="跑完整流程但不實際發送；並印出將呼叫哪些外部端點、送出什麼",
    )
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="通知管道，預設 console",
    )
    parser.add_argument(
        "--config",
        default=str(_DEMO_DIR / "config.yaml"),
        help="設定檔路徑，預設同目錄 config.yaml",
    )
    parser.add_argument(
        "--state-file",
        default=None,
        help="核准狀態檔路徑；預設取 config 的 state.file（測試請指向暫存目錄）",
    )
    parser.add_argument(
        "--audit-file",
        default=None,
        help="稽核日誌 JSONL 路徑；預設取 config 的 audit.file（測試請指向暫存目錄）",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="核准本週策略備忘錄（必須同時提供 --approved-by 具名人員）",
    )
    parser.add_argument(
        "--approved-by", default=None, help="核准人姓名；稽核日誌會逐筆記錄"
    )
    parser.add_argument(
        "--stage",
        default=None,
        help="覆寫本次執行的行銷階段（需存在於 brand_context.yml 的 STAGE_MAP）",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="覆寫當前時間（ISO 8601），供測試取得可重現的備忘錄排程時點",
    )
    return parser


def _resolve(rel: str | Path) -> Path:
    """把設定檔中的相對路徑轉成絕對路徑（禁止硬編碼使用者目錄）。"""
    path = Path(rel)
    return path if path.is_absolute() else _DEMO_DIR / path


def _resolve_now(raw: str | None, tz: Any) -> datetime:
    """決定「現在」。--now 供測試注入固定時間，未給則取系統時間。"""
    if not raw:
        return datetime.now(tz=tz)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SwarmError(f"--now 不是合法的 ISO 8601 時間：{raw!r}｜{exc}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz)


def _build_gate(runtime: dict[str, Any], diagnostics: Diagnostics) -> AutonomyGate:
    """建立自主權閘門。任何設定異常都往 DRAFT 降級，絕不往上放寬。"""
    level_name = str(runtime.get("autonomy", "draft"))
    try:
        level = AutonomyLevel(level_name)
    except ValueError:
        diagnostics.amber(
            symptom=f"未知的 autonomy 值 {level_name!r}",
            fix="改用預設的 draft；合法值為 read_only / draft / supervised_auto",
        )
        level = AutonomyLevel.DRAFT
    try:
        return AutonomyGate(
            level=level,
            approved_senders=list(runtime.get("approved_senders") or []),
            days_in_draft=int(runtime.get("days_in_draft", 0)),
        )
    except AutonomyError as exc:
        diagnostics.amber(
            symptom=f"自主權設定違規：{exc}",
            fix="已降級為 DRAFT；補上 approved_senders 後才可開啟 supervised_auto",
        )
        return AutonomyGate(level=AutonomyLevel.DRAFT)


def _build_swarm(
    swarm_cfg: dict[str, Any], brand_context: dict[str, Any], audit: AuditLog
) -> Orchestrator:
    """建立 Orchestrator 並把五個 Sub-agent 掛上匯流排，然後做第一次級聯。"""
    raw_agents = swarm_cfg.get("agents") or []
    if not raw_agents:
        raise SwarmError("config.yaml 的 swarm.agents 是空的，蜂群沒有子智能體可派工")
    orchestrator = Orchestrator(
        brand_context=brand_context,
        audit=audit,
        display_name=str(swarm_cfg.get("orchestrator_name", "Marketing Director Agent")),
    )
    for raw in raw_agents:
        orchestrator.register(SubAgent(AgentSpec.from_config(raw)))
    orchestrator.cascade("蜂群初始化：brand_context.yml 首次級聯至所有 Sub-agent")
    return orchestrator


def _apply_white_label(
    orchestrator: Orchestrator, white_label: dict[str, Any], audit: AuditLog
) -> dict[str, Any]:
    """套用白牌覆寫：整份 brand_context 抽換。

    覆寫走 update_brand_context -> 版本 +1 -> 立刻級聯，
    因此「整組抽換品牌」不需要動任何 Sub-agent 的程式碼或提示詞。
    """
    if not white_label.get("enabled"):
        return {"enabled": False, "tenant_slug": ""}
    patch = dict(white_label.get("overrides") or {})
    tenant = str(white_label.get("tenant_slug") or "").strip()
    if tenant:
        patch["tenant_slug"] = tenant
    context = orchestrator.update_brand_context(
        patch, reason=f"白牌覆寫：切換至租戶 {tenant or '(未命名)'}"
    )
    audit.record(
        action="white_label_applied",
        target=f"tenant/{context.tenant_slug}",
        rationale="白牌覆寫：整份 brand_context 抽換後級聯至五個 Sub-agent",
        details={"context_version": context.version, "keys": sorted(patch)},
    )
    return {
        "enabled": True,
        "tenant_slug": context.tenant_slug,
        "context_version": context.version,
    }


# ---------------------------------------------------------------------------
# 核准狀態（單一人類監督節點的落地儲存）
# ---------------------------------------------------------------------------
def load_state(path: Path) -> dict[str, Any]:
    """讀取核准狀態檔。不存在視為「尚無任何核准」，這是安全的預設。"""
    if not path.is_file():
        return {"approvals": {}}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SwarmError(f"狀態檔無法解析：{path.resolve()}｜{exc}") from exc
    if not isinstance(parsed, dict):
        raise SwarmError(f"狀態檔內容必須是 JSON 物件：{path.resolve()}")
    parsed.setdefault("approvals", {})
    return parsed


def save_state(path: Path, state: dict[str, Any]) -> None:
    """寫回核准狀態檔（父目錄不存在就建立）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        raise SwarmError(f"狀態檔寫入失敗：{path.resolve()}｜{exc}") from exc


def _resolve_approval(
    args: argparse.Namespace,
    memo: dict[str, Any],
    state: dict[str, Any],
    audit: AuditLog,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """處理本次執行的核准狀態，回傳 approval 區塊。"""
    memo_id = str(memo["memo_id"])
    approvals: dict[str, Any] = state.setdefault("approvals", {})
    if getattr(args, "approve", False):
        approver = str(getattr(args, "approved_by", "") or "").strip()
        if not approver:
            diagnostics.red(
                symptom="--approve 未指定核准人",
                cause="稽核軌跡要求每一次核准都能追溯到具名的人",
                fix="改用 --approve --approved-by '姓名' 重新執行",
            )
            raise SwarmError("--approve 必須搭配 --approved-by")
        approvals[memo_id] = {
            "approved_by": approver,
            "approved_at": str(memo["generated_at"]),
            "context_version": memo.get("context_version"),
        }
        audit.record(
            action="strategy_memo_approved",
            target=memo_id,
            rationale="apxG_p05 單一人類監督節點：策略備忘錄經人工審核後放行",
            is_human_approved=True,
            approved_by=approver,
            details={"context_version": memo.get("context_version")},
        )
    record = approvals.get(memo_id)
    if not memo.get("approval_required", True):
        return {"memo_id": memo_id, "is_approved": True, "approved_by": None,
                "reason": "config 關閉了 approval_required（不建議）"}
    if record:
        return {
            "memo_id": memo_id,
            "is_approved": True,
            "approved_by": str(record.get("approved_by")),
            "approved_at": str(record.get("approved_at")),
        }
    audit.record(
        action="approval_pending",
        target=memo_id,
        rationale="備忘錄尚未經人工核准，所有 Sub-agent 產出鎖在草稿狀態",
        details={"human_review_minutes": memo.get("human_review_minutes")},
    )
    return {"memo_id": memo_id, "is_approved": False, "approved_by": None}


# ---------------------------------------------------------------------------
# 商業模型（金額一律 Decimal，避免浮點誤差出現在報價上）
# ---------------------------------------------------------------------------
def _money(value: Any) -> Decimal:
    """把設定值轉成 Decimal。無法解析就明確拋錯，不用 0 掩蓋。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError) as exc:
        raise SwarmError(f"金額欄位無法解析為數字：{value!r}") from exc


def compute_economics(
    module_cfg: dict[str, Any], economics_cfg: dict[str, Any]
) -> dict[str, Any]:
    """依 ch07_p04 / apxG_p04 的數字算出客戶端財務模型。

    刻意用程式驗證書中「首月省下的預算即超過建置費」這句話，而不是照抄 ——
    換成別的報價時這個結論可能就不成立，提案時講錯會很難看。
    """
    setup = _money(module_cfg.get("client_setup_price", 0))
    monthly = _money(module_cfg.get("client_monthly_price", 0))
    replaced_low = _money(economics_cfg.get("replaced_cost_low", 0))
    replaced_high = _money(economics_cfg.get("replaced_cost_high", 0))
    net_low = replaced_low - monthly
    net_high = replaced_high - monthly
    return {
        "client_setup_price": str(setup),
        "client_monthly_price": str(monthly),
        "replaced_cost_low": str(replaced_low),
        "replaced_cost_high": str(replaced_high),
        "monthly_net_saving_low": str(net_low),
        "monthly_net_saving_high": str(net_high),
        "annual_net_saving_low": str(net_low * 12),
        "annual_net_saving_high": str(net_high * 12),
        # 書中原句：「首月省下的預算即超過建置費」——用最保守的 low 端驗證
        "is_first_month_payback": net_low >= setup,
        "profit_margin_before_pct": economics_cfg.get("profit_margin_before_pct"),
        "profit_margin_after_pct": economics_cfg.get("profit_margin_after_pct"),
        "output_multiplier": economics_cfg.get("output_multiplier"),
        # 原簡報未提供顧問端內部回收工時，依規定不推估
        "recovered_hours_per_month": module_cfg.get("recovered_hours_per_month"),
    }


# ---------------------------------------------------------------------------
# 結果組裝與輸出
# ---------------------------------------------------------------------------
def _collect_warnings(
    actions: list[dict[str, Any]],
    cascade: dict[str, list[str]],
    desynced: list[str],
    approval: dict[str, Any],
) -> list[str]:
    """把所有需要人處理的問題彙整成一份清單。"""
    warnings: list[str] = []
    for agent_id in cascade.get("refused", []):
        warnings.append(
            f"{agent_id} 的 INHERIT_FROM_ORCHESTRATOR 為 false，"
            "它會自行維護品牌資料 —— 這正是跨渠道品牌衝突的來源"
        )
    for agent_id in desynced:
        warnings.append(f"{agent_id} 的品牌上下文與 Orchestrator 不同步，暫停其發布權")
    for action in actions:
        if action["guardrail_violations"]:
            terms = "、".join(action["guardrail_violations"])
            warnings.append(f"{action['display_name']} 產出命中品牌禁用詞：{terms}")
        if not action["is_within_quota"] and action["status"] != "skipped_inactive_stage":
            warnings.append(
                f"{action['display_name']} 產能 {action['produced']} 未落在配額 "
                f"{action['quota']} 內"
            )
    if not approval["is_approved"]:
        warnings.append(
            f"策略備忘錄 {approval['memo_id']} 尚未核准，"
            "所有產出鎖在草稿；請用 --approve --approved-by '姓名' 放行"
        )
    return warnings


def _totals(actions: list[dict[str, Any]]) -> dict[str, int]:
    """統計本週蜂群產出。"""
    return {
        "agents_total": len(actions),
        "agents_dispatched": sum(1 for a in actions if a["status"] == STATUS_DISPATCHED),
        "deliverables": sum(int(a["produced"]) for a in actions),
        "auto_publish": sum(1 for a in actions if a["publish_mode"] == "auto"),
        "draft_only": sum(1 for a in actions if a["publish_mode"] == "draft"),
    }


def format_summary(result: dict[str, Any]) -> str:
    """把蜂群一週產出渲染成人可讀摘要（也是通知內文）。"""
    totals = result["totals"]
    approval = result["approval"]
    approval_text = (
        f"已核准（{approval['approved_by']}）" if approval["is_approved"] else "待核准"
    )
    lines = [
        f"🐝 {result['module_name']}｜租戶 {result['tenant_slug']}｜階段 {result['stage']}",
        f"編排器：{result['orchestrator']}｜brand_context v{result['context_version']}"
        f"（{result['context_checksum']}）已級聯 {len(result['cascade']['inherited'])} 個子智能體",
        f"策略備忘錄 {approval['memo_id']}：{approval_text}"
        f"｜人工審核預估 {result['human_review_minutes']} 分鐘",
        f"安全閥 --dry-run：{'PASS' if result['preflight']['passed'] else 'BLOCKED'}"
        f"（檢查 {result['preflight']['checked']} 條整合）",
        f"產出：{totals['deliverables']} 件／派工 {totals['agents_dispatched']} 個 agent"
        f"／可自動發布 {totals['auto_publish']}／草稿 {totals['draft_only']}",
        "",
    ]
    for action in result["actions"]:
        lines.append(
            f"— {action['display_name']}｜{action['produced']} / {action['quota']}"
            f"｜{action['status']}｜{action['publish_mode']}"
            f"｜ctx v{action['context_version']}"
        )
    if result["warnings"]:
        lines.append("")
        lines.append("⚠️ 審核時要處理的提醒：")
        lines.extend(f"  - {warning}" for warning in result["warnings"])
    lines.append("")
    lines.append(f"🧾 稽核軌跡：{result['audit']['entry_count']} 筆 -> {result['audit']['file']}")
    return "\n".join(lines)


def _deliver(result: dict[str, Any], args: argparse.Namespace, diagnostics: Diagnostics) -> bool:
    """送出摘要。dry-run 只印不送，並完整揭露將呼叫的外部端點與送出內容。"""
    if getattr(args, "dry_run", False):
        if result["mode"] == "mock":
            diagnostics.green("dry-run（mock）：已跑完整流程，LLM 與業務系統皆未實際呼叫")
        else:
            # --live --dry-run 只擋業務系統，內容生成是真的打 API、真的算錢。
            # 用 amber 而不是 green 講這件事，因為它會產生使用者沒預期的帳單。
            diagnostics.amber(
                symptom=(
                    "dry-run（live）：業務系統未送出，但策略備忘錄與五個 Sub-agent 的"
                    "內容生成已實際呼叫 Anthropic API，會產生費用"
                ),
                fix="若要完全零外部呼叫與零成本，請改用 --mock --dry-run",
            )
        print(result["preflight_report"])
        print(result["summary_text"])
        return False
    channel = getattr(args, "notify", "console")
    try:
        notifier = Notifier(channel=channel)
    except NotifierError as exc:
        diagnostics.amber(
            symptom=f"通知管道 {channel} 無法建立：{exc}",
            fix="已改用 console 輸出；檢查憑證與 channel 名稱",
        )
        notifier = Notifier(channel="console")
    return notifier.send(result["summary_text"], subject="本週行銷蜂群產出待審")


def _prepare_context(args: argparse.Namespace, diagnostics: Diagnostics) -> dict[str, Any]:
    """讀設定、決定時區與備忘錄時點、開啟稽核日誌。"""
    config = load_config(args.config)
    memo_cfg = config.get("strategy_memo") or {}
    schedule = memo_cfg.get("schedule") or {}
    tz, tz_warning = resolve_timezone(
        str(schedule.get("timezone", "Asia/Taipei")),
        int(schedule.get("fallback_utc_offset_hours", 8)),
    )
    if tz_warning:
        diagnostics.amber(symptom=tz_warning, fix="pip install tzdata")
    now = _resolve_now(getattr(args, "now", None), tz)
    slot = current_memo_slot(
        now, str(schedule.get("weekday", "SUN")), str(schedule.get("time", "07:00"))
    )
    module_cfg = config.get("module") or {}
    audit_cfg = config.get("audit") or {}
    audit = AuditLog(
        path=_resolve(getattr(args, "audit_file", None) or audit_cfg.get("file", "audit/swarm-audit.jsonl")),
        module_id=str(module_cfg.get("id", "21")),
        module_name=str(module_cfg.get("name", MODULE_NAME)),
        tz=tz,
        enabled=bool(audit_cfg.get("enabled", True)),
    )
    return {"config": config, "tz": tz, "now": now, "slot": slot, "audit": audit,
            "memo_cfg": memo_cfg, "tz_warning": tz_warning}


def _run_preflight(
    orchestrator: Orchestrator,
    config: dict[str, Any],
    is_mock: bool,
    audit: AuditLog,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """跑強制安全閥。沒過就不准進入 dispatch —— 這是 apxG_p03 的鐵律。"""
    preflight = preflight_dry_run(
        agents=orchestrator.agents,
        integrations=config.get("integrations") or {},
        is_mock=is_mock,
        audit=audit,
    )
    if not preflight["passed"]:
        blocked = ", ".join(
            f"{check['agent_id']}:{check['status']}"
            for check in preflight["hard_fail"] + preflight["credential_gaps"]
        )
        diagnostics.red(
            symptom=f"強制安全閥未通過：{blocked}",
            cause="整合端點未登記、非 https，或 live 模式缺少憑證環境變數",
            fix="修正 config.yaml 的 integrations 區段或補齊環境變數後重跑",
        )
        raise SwarmError(f"強制安全閥未通過：{blocked}")
    return preflight


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    diagnostics = Diagnostics(
        MODULE_NAME, exit_on_red=bool(getattr(args, "exit_on_red", True))
    )
    prepared = _prepare_context(args, diagnostics)
    config, audit, memo_cfg = prepared["config"], prepared["audit"], prepared["memo_cfg"]
    swarm_cfg = config.get("swarm") or {}
    is_mock = not bool(getattr(args, "live", False))

    # ① brand_context.yml —— 唯一真理來源
    brand_context = load_config(_resolve(str(swarm_cfg.get("brand_context_file", "brand_context.yml"))))
    orchestrator = _build_swarm(swarm_cfg, brand_context, audit)
    white_label = _apply_white_label(orchestrator, config.get("white_label") or {}, audit)
    cascade = {"inherited": [a.agent_id for a in orchestrator.agents if a.has_context],
               "refused": [a.agent_id for a in orchestrator.agents if not a.has_context]}
    _check_tone_examples(orchestrator, swarm_cfg, diagnostics)

    # ② 整合層 + 強制安全閥（任何對外呼叫之前）
    preflight = _run_preflight(orchestrator, config, is_mock, audit, diagnostics)

    # ③ 每週日 07:00 策略備忘錄 -> 單一人類監督節點
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    memo = generate_strategy_memo(
        client, orchestrator, _DEMO_DIR, memo_cfg, prepared["slot"]
    )
    memo["human_review_minutes"] = int(memo_cfg.get("human_review_minutes", 20))
    state_path = _resolve(
        getattr(args, "state_file", None) or (config.get("state") or {}).get("file", "state/swarm-state.json")
    )
    state = load_state(state_path)
    approval = _resolve_approval(args, memo, state, audit, diagnostics)
    save_state(state_path, state)

    # ④ Task Dispatch -> 五條 Agent Action
    stage_id = str(getattr(args, "stage", None) or swarm_cfg.get("active_stage", "awareness"))
    gate = _build_gate(config.get("runtime") or {}, diagnostics)
    actions = dispatch_agents(
        orchestrator=orchestrator,
        stage_id=stage_id,
        memo=memo,
        client=client,
        module_dir=_DEMO_DIR,
        gate=gate,
        audit=audit,
        is_approved=bool(approval["is_approved"]),
        approved_by=approval.get("approved_by"),
    )
    result = _assemble_result(
        config, orchestrator, stage_id, memo, approval, preflight,
        actions, cascade, white_label, gate, args,
    )
    for warning in result["warnings"]:
        diagnostics.amber(symptom=warning, fix="人工審核時處理，確認後再放行發布")
    audit.record(
        action="run_completed",
        target=f"{result['tenant_slug']}/{stage_id}",
        rationale="本次蜂群執行結束，留存產出統計供事後稽核",
        is_human_approved=bool(approval["is_approved"]),
        approved_by=approval.get("approved_by"),
        details=result["totals"],
    )
    result["audit"] = audit.summary()
    result["summary_text"] = format_summary(result)
    result["notified"] = _deliver(result, args, diagnostics)
    # 放在 _deliver 之後：--live --dry-run 的費用警示是在送出階段才發出的
    result["amber_count"] = diagnostics.amber_count
    return result


def _check_tone_examples(
    orchestrator: Orchestrator, swarm_cfg: dict[str, Any], diagnostics: Diagnostics
) -> None:
    """語氣樣本不足時發琥珀警示（對應契約的 tone_mismatch 已知症狀）。"""
    minimum = int(swarm_cfg.get("min_tone_examples", 3))
    actual = len(orchestrator.context.tone_examples)
    if actual < minimum:
        diagnostics.report(
            "tone_mismatch", f"brand_context.yml 只有 {actual} 則語氣樣本，需要 {minimum} 則"
        )


def _assemble_result(
    config: dict[str, Any],
    orchestrator: Orchestrator,
    stage_id: str,
    memo: dict[str, Any],
    approval: dict[str, Any],
    preflight: dict[str, Any],
    actions: list[dict[str, Any]],
    cascade: dict[str, list[str]],
    white_label: dict[str, Any],
    gate: AutonomyGate,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """把所有產出組成回傳結果（鍵名依 CONTRACT 建議的標準六欄 + 模組專屬）。"""
    module_cfg = config.get("module") or {}
    context = orchestrator.context
    desynced = orchestrator.desynced_agents()
    warnings = _collect_warnings(actions, cascade, desynced, approval)
    return {
        "module_id": str(module_cfg.get("id", "21")),
        "module_name": str(module_cfg.get("name", "多智能體行銷協同群")),
        "mode": "mock" if not getattr(args, "live", False) else "live",
        "dry_run": bool(getattr(args, "dry_run", False)),
        "orchestrator": orchestrator.display_name,
        "tenant_slug": context.tenant_slug,
        "context_version": context.version,
        "context_checksum": context.checksum,
        "cascade": {**cascade, "desynced": desynced},
        "white_label": white_label,
        "stage": stage_id,
        "stage_label": str(context.stage(stage_id).get("label", stage_id)),
        "memo": memo,
        "approval": approval,
        "human_review_minutes": int(memo.get("human_review_minutes", 20)),
        "preflight": {
            "passed": bool(preflight["passed"]),
            "checked": len(preflight["checks"]),
            "checks": preflight["checks"],
        },
        "preflight_report": format_preflight_report(preflight),
        "actions": actions,
        "totals": _totals(actions),
        "economics": compute_economics(module_cfg, config.get("economics") or {}),
        "autonomy_level": gate.level.value,
        "warnings": warnings + list(gate.warnings),
    }


def main() -> int:
    """解析參數 -> run() -> 印出結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (SwarmError, AuditError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    totals = result["totals"]
    print(
        f"\n✅ 完成：{totals['agents_dispatched']}/{totals['agents_total']} 個子智能體派工、"
        f"{totals['deliverables']} 件產出、"
        f"{'已核准' if result['approval']['is_approved'] else '待人工核准'}、"
        f"警告 {len(result['warnings'])} 則、稽核 {result['audit']['entry_count']} 筆"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
