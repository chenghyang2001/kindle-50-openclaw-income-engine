"""demo26 — 電商庫存與定價最佳化（主流程）。

每日一次：讀 SKU 快照 → 算流速與可售天數 → STATUS 五級分類 →
套用 3x3 決策矩陣與 `pricing_rules` → **過三道定價安全閥** →
產出調價草稿與滯銷品促銷企劃 → 寫稽核軌跡 → 發報告。

本模組與其他 demo 最大的不同：它會改動**線上售價**。
因此整份程式的預設立場是「不動作」——
每一個調價建議都必須同時通過矩陣、pricing_rules 與三道安全閥，
少一項就退回 HOLD；通過了也只是 `DRAFT`，仍要人工核准才會寫回平台。

自動化能省下的是「發現問題的時間」，不是「決定要不要改價的責任」。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402

from analyser import (  # noqa: E402
    AnalyserError,
    SkuAnalysis,
    analyse_skus,
    fetch_live_inventory,
    load_json,
    stale_candidates,
    summarise,
    to_count,
    to_money,
)
from audit import AuditError, AuditLog, SEVERITY_AMBER, SEVERITY_RED, new_run_id  # noqa: E402
from pricer import (  # noqa: E402
    PriceProposal,
    PricerError,
    STATE_DRAFT,
    propose_all,
    summarise_proposals,
    validate_settings,
)

MODULE_NAME = "demo26-inventory-pricing"
NOTIFY_CHANNELS = ("console", "telegram", "gmail", "line", "whatsapp")
DEFAULT_CONFIG = _DEMO_DIR / "config.yaml"
ANALYSIS_PROMPT = _DEMO_DIR / "prompts" / "daily_sku_analysis.md"
PROMO_PROMPT = _DEMO_DIR / "prompts" / "promotional_brief.md"
ANALYSIS_FIXTURE = _DEMO_DIR / "mock" / "daily_analysis_fixture.md"
PROMO_FIXTURE = _DEMO_DIR / "mock" / "promotional_brief_fixture.md"
CONTEXT_NOTE = (
    "讀者是電商營運總監。所有數字已由程式用 Decimal 算完並通過安全閥，"
    "你只負責解讀，不得自行計算或修改任何金額。"
)

DELIVERY_SENT = "sent"
DELIVERY_DRAFT = "draft"
DELIVERY_DRY_RUN = "dry_run"


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT.md §6，另加狀態檔與稽核檔路徑）"""
    parser = argparse.ArgumentParser(
        prog=MODULE_NAME, description="電商庫存與定價最佳化（Level 3 · 自動化 #26）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True,
                      help="離線模式：讀本地快照，不觸網、不呼叫 API（預設）")
    mode.add_argument("--live", dest="mock", action="store_false",
                      help="串接 Shopify Admin API 與 Claude API（會先強制跑內部 dry-run 測試）")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="跑完整流程但不發送、不寫狀態檔、不落地稽核軌跡")
    parser.add_argument("--notify", choices=NOTIFY_CHANNELS, default=None,
                        help="通知管道，未指定時採用 config.yaml 的 runtime.notify_channel")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="設定檔路徑（預設為本目錄的 config.yaml）")
    parser.add_argument("--state-file", dest="state_file", default=None,
                        help="狀態檔路徑，未指定時採用 config.yaml 的 paths.state_file")
    parser.add_argument("--audit-file", dest="audit_file", default=None,
                        help="稽核軌跡 JSONL 路徑，未指定時採用 config.yaml 的 paths.audit_file")
    return parser


def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律相對於本 demo 目錄解析，避免受呼叫端的工作目錄影響"""
    path = Path(raw)
    return path if path.is_absolute() else (_DEMO_DIR / path)


def _emit_red(diagnostics: Diagnostics, red_alerts: list[str], *,
              symptom: str, cause: str, fix: str) -> None:
    """發出 RED 警報但**不中斷**整批流程。

    `Diagnostics.red()` 在 exit_on_red=False 時會拋 RedAlert，這裡刻意攔下：
    單一 SKU 的定價違規不該讓其餘 7 個 SKU 的庫存報告一起消失——
    那會讓「今天有一筆負毛利」變成「今天什麼都不知道」。

    RED 事實不會被吞掉：它已經印在 stderr、寫進稽核軌跡、列進報告的
    「需人工介入」段，而且會讓行程以退出碼 1 結束。
    """
    try:
        diagnostics.red(symptom=symptom, cause=cause, fix=fix)
    except RedAlert as exc:
        red_alerts.append(str(exc))


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
        days_in_draft=to_count(runtime.get("days_in_draft", 0) or 0, "days_in_draft"),
    )
    for warning in gate.warnings:
        diagnostics.amber(symptom=warning, fix="維持 draft 直到滿 14 天且客戶已簽核")
    return gate


def _load_feeds(config: dict, is_mock: bool) -> tuple[list[dict], dict[str, Any], dict]:
    """載入三份資料：SKU 快照、對手價、需求訊號。

    對手價**永遠**從 feed 檔讀取（由 PRICE_WATCH 監控器另行產出並寫入），
    本模組只負責消費結果，不自己去爬別人的網站——把抓取與決策拆開，
    才不會因為對方改版就讓定價引擎跟著壞掉。
    """
    paths = config.get("paths", {})
    competitor = load_json(_resolve_path(paths.get("competitor_snapshot", "mock/competitor_prices.json")))
    demand = load_json(_resolve_path(paths.get("demand_snapshot", "mock/demand_signals.json")))
    prices = competitor.get("prices")
    if not isinstance(prices, dict):
        raise AnalyserError("對手價 feed 缺少 prices 物件")
    records = _load_sku_records(config, is_mock)
    return records, prices, demand


def _load_sku_records(config: dict, is_mock: bool) -> list[dict]:
    """取得 SKU 原始快照：mock 讀本地 JSON，live 打 Shopify Admin API。"""
    paths = config.get("paths", {})
    if is_mock:
        payload = load_json(_resolve_path(paths.get("sku_snapshot", "mock/skus.json")))
        records = payload.get("skus")
        if not isinstance(records, list):
            raise AnalyserError("SKU 快照缺少 skus 陣列")
        return records
    shopify = config.get("integrations", {}).get("shopify", {})
    products = fetch_live_inventory(
        str(shopify.get("shop_domain", "")),
        str(shopify.get("admin_token", "")),
        to_count(shopify.get("timeout_seconds", 30), "timeout_seconds"),
    )
    velocity_feed = load_json(_resolve_path(paths.get("velocity_feed", "state/velocity_feed.json")))
    return _merge_shopify(products, velocity_feed.get("skus", {}))


def _merge_shopify(products: list[dict], velocity_feed: dict[str, Any]) -> list[dict]:
    """把 Shopify 商品變體合併上流速／成本 feed。

    Shopify 的 products API 沒有銷售流速與成本，那些來自 ERP／分析系統的匯出檔。
    缺任何一個 SKU 的流速資料就明確報錯——用 0 補上會讓它被判成 OVERSTOCK，
    然後系統會「建議清理」一個其實賣得很好的商品。
    """
    records: list[dict] = []
    for product in products:
        for variant in product.get("variants", []):
            sku_id = str(variant.get("sku", "")).strip()
            if not sku_id:
                continue
            metrics = velocity_feed.get(sku_id)
            if not isinstance(metrics, dict):
                raise AnalyserError(f"流速／成本 feed 缺少 {sku_id}，拒絕以 0 代入")
            records.append({
                "sku_id": sku_id,
                "product_name": str(product.get("title", sku_id)),
                "category": str(product.get("product_type", "uncategorised")),
                "current_stock": variant.get("inventory_quantity", 0),
                "current_price": variant.get("price"),
                **metrics,
            })
    if not records:
        raise AnalyserError("Shopify 回傳的商品中沒有任何帶 SKU 的變體")
    return records


def _preflight_dry_run(config: dict, channel: str, audit: AuditLog) -> dict[str, Any]:
    """全域安全閥（apxG_p03）：對外 API 呼叫前必先跑一次內部 dry-run 通訊測試。

    這裡只驗「我們自己這一側」是否健康，不驗資料有沒有異常
    （負毛利之類的異常本來就該被下游安全閥擋，不是 preflight 的職責）：
        1. 提示詞與 fixture 檔案讀得到
        2. 定價設定通過 validate_settings（安全閥沒有被設定檔關掉）
        3. 通知管道名稱合法（Notifier 建得起來，但不送出）
        4. 用離線快照完整跑一次 分析 -> 定價，且每個 SKU 都拿到一筆建議
        5. 稽核軌跡寫得進去
    任何一項不過就中止，絕不帶著壞掉的設定去改客戶的線上售價。
    """
    for required in (ANALYSIS_PROMPT, PROMO_PROMPT, ANALYSIS_FIXTURE, PROMO_FIXTURE):
        if not required.is_file():
            raise PricerError(f"preflight 失敗：找不到必要檔案 {required}")
    problems = validate_settings(config.get("pricing", {}))
    if problems:
        raise PricerError("preflight 失敗：" + "；".join(problems))
    Notifier(channel)
    records, prices, _ = _load_feeds(config, is_mock=True)
    settings = {**config.get("inventory", {}), **config.get("pricing", {})}
    analyses = analyse_skus(records, prices, settings)
    proposals = propose_all(analyses, settings)
    if len(proposals) != len(analyses):
        raise PricerError("preflight 失敗：定價建議數與 SKU 數不一致")
    summary = {"skus": len(analyses), "proposals": len(proposals), "channel": channel}
    audit.record("preflight_dry_run", detail=summary)
    return summary


def _record_proposals(
    proposals: list[PriceProposal], audit: AuditLog, diagnostics: Diagnostics,
    red_alerts: list[str], warnings: list[str],
) -> None:
    """逐筆把定價決策寫進稽核軌跡，並依 severity 升級 AMBER / RED。"""
    for item in proposals:
        audit.record(
            "pricing_decision",
            severity=item.severity if item.severity in (SEVERITY_AMBER, SEVERITY_RED) else "info",
            sku_id=item.sku_id,
            detail=item.as_dict(),
        )
        if item.severity == SEVERITY_RED:
            warnings.append(f"{item.sku_id}：{item.reason}")
            _emit_red(
                diagnostics, red_alerts,
                symptom=f"{item.sku_id}｜{item.product_name}：{item.reason}",
                cause="建議售價不高於成本價，或該商品目前已是負毛利",
                fix="人工決定認賠出清、調整成本結構或下架，系統不會自動調價",
            )
        elif item.severity == SEVERITY_AMBER:
            warnings.append(f"{item.sku_id}：{item.reason}")
            diagnostics.amber(
                symptom=f"{item.sku_id}｜{item.product_name}：{item.reason}",
                fix="由人工核准後手動調整，或調整 config 的門檻並重跑",
            )


def _build_llm_input(
    analyses: list[SkuAnalysis], proposals: list[PriceProposal],
    demand: dict, settings: dict[str, Any],
) -> str:
    """組出餵給 Daily SKU Analysis 提示詞的結構化資料（五段）"""
    blocked = [item for item in proposals if item.needs_human_escalation]
    sections = [
        "SETTINGS:",
        f"- slow_mover_days: {settings.get('slow_mover_days')}",
        f"- overstock_doh: {settings.get('overstock_doh')}",
        f"- max_price_change_percent: {settings.get('max_price_change_percent')}",
        f"- min_margin_percent: {settings.get('min_margin_percent')}",
        "SKUS:",
    ]
    sections += [f"- {_sku_line(item)}" for item in analyses]
    sections.append("PRICING:")
    sections += [f"- {_proposal_line(item)}" for item in proposals]
    sections.append("DEMAND:")
    sections += [
        f"- {name}：指數 {info.get('index')}｜30 日 {info.get('delta_30d_percent')}%"
        f"｜{info.get('note', '')}"
        for name, info in (demand.get("categories") or {}).items()
    ]
    sections.append("BLOCKED:")
    sections += [f"- {item.sku_id}｜{item.reject_reason}｜{item.reason}" for item in blocked] or ["- （無）"]
    return "\n".join(sections)


def _sku_line(item: SkuAnalysis) -> str:
    """單一 SKU 的一行摘要（欄位順序照 Daily SKU Analysis Prompt）"""
    doh = "無限（賣不動）" if item.days_on_hand is None else str(item.days_on_hand)
    return (
        f"{item.sku_id}｜{item.product_name}｜庫存 {item.current_stock}｜"
        f"v7d {item.avg_daily_velocity_7d}｜v30d {item.avg_daily_velocity_30d}｜"
        f"可售 {doh} 天｜補貨點 {item.reorder_point}｜{item.status}｜{item.velocity_band}｜"
        f"滯銷 {item.days_since_last_sale} 天｜售價 {item.current_price}｜成本 {item.cost_price}｜"
        f"對手 {item.competitor_price}｜旗標 {','.join(item.flags) or '無'}"
    )


def _proposal_line(item: PriceProposal) -> str:
    """單一定價建議的一行摘要"""
    target = item.proposed_price if item.proposed_price is not None else item.blocked_price
    change = "—" if item.change_percent is None else f"{item.change_percent}%"
    return (
        f"{item.sku_id}｜{item.action}｜{item.matrix_row or '不適用'} x {item.competitor_position}"
        f"｜規則 {','.join(item.rules_matched) or '無'}｜{item.current_price} -> {target or '不變'}"
        f"（{change}）｜{item.approval_state}｜{item.reason}"
    )


def _build_promo_input(
    candidates: list[SkuAnalysis], excluded: list[SkuAnalysis],
    demand: dict, discount_percent: Decimal,
) -> str:
    """組出餵給 promotional_brief_generator 的結構化資料"""
    sections = [f"SUGGESTED_DISCOUNT_PERCENT: {discount_percent}", "CANDIDATES:"]
    sections += [f"- {_sku_line(item)}" for item in candidates] or ["- （無）"]
    sections.append("EXCLUDED:")
    sections += [
        f"- {item.sku_id}｜{item.product_name}｜排除原因：售價 {item.current_price} "
        f"未高於成本 {item.cost_price}（NEGATIVE_MARGIN）"
        for item in excluded
    ] or ["- （無）"]
    sections.append("DEMAND:")
    sections += [
        f"- {name}：指數 {info.get('index')}｜30 日 {info.get('delta_30d_percent')}%"
        for name, info in (demand.get("categories") or {}).items()
    ]
    return "\n".join(sections)


def _render_promotional_brief(
    client: LLMClient, analyses: list[SkuAnalysis], demand: dict,
    settings: dict[str, Any], is_mock: bool, audit: AuditLog,
) -> tuple[str, list[str]]:
    """滯銷達門檻天數就呼叫 promotional_brief_generator（apxG_p14 底部警告條）。

    負毛利商品**不進候選**：對一個已經賠錢的品項再打折，只是把虧損放大。
    它們改列進排除清單，交給人決定要不要認賠出清。
    """
    slow_days = to_count(settings.get("slow_mover_days", 14), "slow_mover_days")
    stale = stale_candidates(analyses, slow_days)
    candidates = [item for item in stale if not item.has_negative_margin and not item.is_stockout]
    excluded = [item for item in stale if item.has_negative_margin or item.is_stockout]
    audit.record(
        "promotional_brief_trigger",
        detail={
            "slow_mover_days": slow_days,
            "candidates": [item.sku_id for item in candidates],
            "excluded": [item.sku_id for item in excluded],
        },
    )
    if not candidates:
        return "", []
    discount = to_money(settings.get("promo_discount_percent", 10), "promo_discount_percent")
    text = client.complete(
        system=PROMO_PROMPT.read_text(encoding="utf-8"),
        user=_build_promo_input(candidates, excluded, demand, discount),
        max_tokens=1200,
        fixture=PROMO_FIXTURE if is_mock else None,
    )
    return text, [item.sku_id for item in candidates]


def _build_report(
    config: dict, stats: dict[str, int], pstats: dict[str, int],
    narrative: str, promo: str, proposals: list[PriceProposal], run_id: str,
) -> str:
    """組出最終要發送的報告全文"""
    module = config.get("module", {})
    header = (
        f"【庫存與定價最佳化 · {module.get('name', MODULE_NAME)}】"
        f"{datetime.now().astimezone():%Y-%m-%d}｜run {run_id}\n"
        f"{stats['total']} 個 SKU｜缺貨 {stats['stockouts']}｜"
        f"補貨急件 {stats['REORDER_URGENT']}｜滯銷 {stats['SLOW_MOVER']}｜"
        f"積壓 {stats['OVERSTOCK']}｜調價草稿 {pstats['drafts']}｜"
        f"擋下待人工 {pstats['rejected']}"
    )
    blocks = [header, "", narrative.strip()]
    if promo.strip():
        blocks += ["", "── 滯銷品促銷企劃（promotional_brief_generator）──", promo.strip()]
    blocks += ["", "── 原始定價決策（全部為草稿，未寫回平台）──"]
    blocks += [f"  {_report_mark(item)} {_proposal_line(item)}" for item in proposals]
    blocks += ["", "※ 所有調價建議狀態皆為 DRAFT／REJECTED，需人工核准後才會寫回平台。"]
    return "\n".join(blocks)


def _report_mark(item: PriceProposal) -> str:
    """報告中每行前面的狀態標記"""
    if item.severity == SEVERITY_RED:
        return "[RED]"
    if item.needs_human_escalation:
        return "[擋下]"
    if item.is_price_change:
        return "[草稿]"
    return "     "


def _deliver(report: str, *, channel: str, gate: AutonomyGate, recipient: str,
             is_dry_run: bool, subject: str) -> tuple[str, bool]:
    """依自主權層級決定送出或留為草稿，回傳 (delivery, is_notified)"""
    if is_dry_run:
        return DELIVERY_DRY_RUN, False
    if gate.can_send(recipient):
        return DELIVERY_SENT, Notifier(channel).send(report, subject=subject)
    # 未取得自動送出授權：降級為草稿，只印在本機供人工過目，不推到對外管道
    drafted = f"【草稿・待人工核准】收件人 {recipient} 未取得自動送出授權\n\n{report}"
    return DELIVERY_DRAFT, Notifier("console").send(drafted, subject=subject)


def _save_state(path: Path, run_id: str, proposals: list[PriceProposal]) -> None:
    """把本次的售價與待核准草稿寫進狀態檔，作為下次執行的比對基準。"""
    payload = {
        "version": 1,
        "run_id": run_id,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "skus": {
            item.sku_id: {
                "current_price": str(item.current_price),
                "approval_state": item.approval_state,
                "proposed_price": None if item.proposed_price is None else str(item.proposed_price),
            }
            for item in proposals
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程並回傳結果 dict（不做 sys.exit，交給 main() 決定退出碼）"""
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=False)
    red_alerts: list[str] = []
    warnings: list[str] = []
    required_env = None if args.mock else ["ANTHROPIC_API_KEY", "SHOPIFY_ADMIN_TOKEN"]
    config = load_config(_resolve_path(args.config), required_env=required_env)
    runtime = config.get("runtime", {})
    settings = {**config.get("inventory", {}), **config.get("pricing", {})}
    channel = args.notify or str(runtime.get("notify_channel", "console"))

    run_id = new_run_id()
    audit = AuditLog(
        _resolve_path(args.audit_file or config.get("paths", {}).get("audit_file", "audit/pricing_audit.jsonl")),
        MODULE_NAME, run_id=run_id, is_enabled=not args.dry_run,
    )
    audit.record("run_started", detail={"mode": "mock" if args.mock else "live",
                                        "dry_run": bool(args.dry_run), "channel": channel})

    problems = validate_settings(config.get("pricing", {}))
    if problems:
        diagnostics.red(
            symptom="定價安全設定不合法：" + "；".join(problems),
            cause="config.yaml 的 pricing 區塊把安全閥設成無效或過寬的值",
            fix="修正 pricing 區塊後重跑；安全閥是本模組唯一的防呆機制",
        )

    preflight = _preflight_dry_run(config, channel, audit) if not args.mock else None
    records, prices, demand = _load_feeds(config, is_mock=args.mock)
    analyses = analyse_skus(records, prices, settings)
    proposals = propose_all(analyses, settings)
    _record_proposals(proposals, audit, diagnostics, red_alerts, warnings)

    gate = _build_gate(runtime, diagnostics)
    client = LLMClient(mock=args.mock, context_note=CONTEXT_NOTE)
    promo, promo_skus = _render_promotional_brief(client, analyses, demand, settings, args.mock, audit)
    narrative = client.complete(
        system=ANALYSIS_PROMPT.read_text(encoding="utf-8"),
        user=_build_llm_input(analyses, proposals, demand, settings),
        max_tokens=1500,
        fixture=ANALYSIS_FIXTURE if args.mock else None,
    )

    stats, pstats = summarise(analyses), summarise_proposals(proposals)
    report = _build_report(config, stats, pstats, narrative, promo, proposals, run_id)
    delivery, is_notified = _deliver(
        report, channel=channel, gate=gate,
        recipient=str(runtime.get("alert_recipient", "")), is_dry_run=args.dry_run,
        subject=f"庫存與定價｜{pstats['drafts']} 筆調價草稿｜{pstats['rejected']} 筆待人工",
    )

    state_path = _resolve_path(args.state_file or config.get("paths", {}).get("state_file", "state/pricing_state.json"))
    if not args.dry_run:
        _save_state(state_path, run_id, proposals)
    audit.record("run_completed", detail={"stats": stats, "pricing": pstats, "delivery": delivery})
    if not red_alerts and pstats["rejected"] == 0:
        diagnostics.green(f"{stats['total']} 個 SKU 分析完成，{pstats['drafts']} 筆調價草稿待核准")

    return _build_result(
        config, analyses, proposals, stats, pstats, narrative, promo, promo_skus, report,
        delivery=delivery, is_notified=is_notified, channel=channel, is_mock=args.mock,
        is_dry_run=bool(args.dry_run), diagnostics=diagnostics, red_alerts=red_alerts,
        warnings=warnings, audit=audit, state_path=state_path, preflight=preflight,
    )


def _build_result(
    config: dict, analyses: list[SkuAnalysis], proposals: list[PriceProposal],
    stats: dict[str, int], pstats: dict[str, int], narrative: str, promo: str,
    promo_skus: list[str], report: str, *, delivery: str, is_notified: bool, channel: str,
    is_mock: bool, is_dry_run: bool, diagnostics: Diagnostics, red_alerts: list[str],
    warnings: list[str], audit: AuditLog, state_path: Path, preflight: dict | None,
) -> dict[str, Any]:
    """組出供測試斷言與下游使用的結果 dict（全部欄位皆可 JSON 序列化）。

    鍵名採用 CONTRACT.md §6 技術債註記中「未來若要標準化」建議的 6 個欄位
    （module_id / module_name / mode / dry_run / warnings / amber_count），
    新模組沒有理由再多發散一種命名。
    """
    module = config.get("module", {})
    return {
        "module_id": str(module.get("id", "26")),
        "module_name": str(module.get("name", MODULE_NAME)),
        "mode": "mock" if is_mock else "live",
        "dry_run": is_dry_run,
        "warnings": warnings,
        "amber_count": diagnostics.amber_count,
        "red_alerts": red_alerts,
        "preflight": preflight,
        "skus": [item.as_dict() for item in analyses],
        "proposals": [item.as_dict() for item in proposals],
        "drafts": [item.as_dict() for item in proposals if item.is_price_change],
        "blocked": [item.as_dict() for item in proposals if item.needs_human_escalation],
        "stats": stats,
        "pricing_stats": pstats,
        "promotional_brief": promo,
        "promotional_skus": promo_skus,
        "analysis_text": narrative,
        "report": report,
        "delivery": delivery,
        "notified": is_notified,
        "notify_channel": channel,
        "state_file": str(state_path),
        "audit_file": str(audit.path),
        "audit_run_id": audit.run_id,
        "audit_entries": len(audit.entries),
    }


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳退出碼。

    退出碼約定（讓排程器分辨「壞掉」「有事要看」與「一切正常」）：
        0 = 全部正常，沒有需要人介入的項目
        2 = 流程完成且有完整結果，但**有事要看**
            （RED 級的負毛利／低於成本被擋、AMBER 警示、待人工核准的調價）
        1 = 沒有結果：設定錯誤、資料源壞掉、preflight 失敗等致命狀況

    RED 走 2 而不是 1，是刻意的：本模組的 RED 幾乎都是「某個 SKU 的定價違規」，
    報告本身仍然完整可用。把它判成 1 會讓排程器誤以為整個任務失敗而重跑，
    但重跑一百次，那個負毛利商品還是負毛利——它需要的是人，不是重試。
    """
    args = build_parser().parse_args()
    try:
        result = run(args)
    except RedAlert as exc:
        print(f"紅色警報：{exc}", file=sys.stderr)
        return 1
    except (AnalyserError, PricerError, AuditError, NotifierError,
            FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    # dry-run 不經 Notifier，報告要自己印出來才看得到
    if result["delivery"] == DELIVERY_DRY_RUN:
        print(result["report"])
    print(
        f"\n完成：{result['stats']['total']} 個 SKU｜"
        f"{result['pricing_stats']['drafts']} 筆調價草稿（{STATE_DRAFT}）｜"
        f"{result['pricing_stats']['rejected']} 筆被安全閥擋下｜"
        f"RED {len(result['red_alerts'])}｜AMBER {result['amber_count']}｜"
        f"delivery={result['delivery']}",
        file=sys.stderr,
    )
    has_attention = bool(
        result["red_alerts"] or result["pricing_stats"]["rejected"]
        or result["amber_count"] or result["pricing_stats"]["drafts"]
    )
    return 2 if has_attention else 0


if __name__ == "__main__":
    sys.exit(main())
