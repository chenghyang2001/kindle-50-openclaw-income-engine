"""模組 #11：SEO 內容引擎（SEO Content Engine）— 主流程。

每週一 08:00（cron `0 8 * * 1`）自動跑一次：

    抓 Top 12 關鍵字 -> 挑出排名 8-20 的可攻擊字 -> 選 3 篇 -> 草擬 1500 字含 FAQ
    -> 產出內部連結建議 -> 推草稿進 CMS 待審 -> 發週一內容簡報

自主權預設 DRAFT：SEO 文章掛上客戶網域就是對外發言，一篇含錯誤數據的文章被
Google 索引後會跟著客戶好幾年，撤稿成本遠高於每週一次的人工審閱。

檔案分工對應附錄F p04 的「系統大腦」設計：
    context.json（環境與規則） -> 本目錄的 config.yaml
    prompt.txt（角色與任務）   -> 本目錄的 prompts/*.md
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402

from content_generator import (  # noqa: E402
    MOCK_MARKER,
    ContentGeneratorError,
    DraftSettings,
    apply_topic_overrides,
    build_cms_payload,
    build_selection_user_prompt,
    build_topic_brief,
    draft_article,
    finalize_article,
    load_prompt,
    parse_topics_json,
)
from keyword_planner import (  # noqa: E402
    KeywordPlannerError,
    SelectionSettings,
    keywords_in_cooldown,
    load_candidates,
    load_json_file,
    load_state,
    save_state,
    select_topics,
)

MODULE_NAME = "demo11-seo-content-engine"

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出。
# 這個模組最貴的幻覺是「編造出來的數據」——文章一旦被索引就會跟著客戶網域好幾年。
CONTEXT_NOTE = (
    "你正在產出會被 Google 長期索引、掛在客戶網域上的 SEO 文章。"
    "只能使用品牌檔提供的事實；任何需要具體數字、客戶名稱、專案時程的位置，"
    "一律寫成【待填：說明要填什麼】交給編輯補值，不得自行推估。"
)

DEFAULT_HOURLY_RATE = "75"
CENTS = Decimal("0.01")


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6）。"""
    parser = argparse.ArgumentParser(
        description="模組 #11：SEO 內容引擎（每週選 3 個關鍵字並草擬 1500 字文章）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mock", action="store_true", default=True, help="離線模式，不呼叫真實 API（預設）"
    )
    mode.add_argument(
        "--live", action="store_true", help="串真實 API；缺憑證會明確報錯，不會偷偷退回 mock"
    )
    parser.add_argument("--dry-run", action="store_true", help="跑完整流程但不發送通知、不寫狀態檔")
    parser.add_argument(
        "--notify", choices=list(Notifier.SUPPORTED), default="console", help="通知管道，預設 console"
    )
    parser.add_argument(
        "--config", default=str(MODULE_DIR / "config.yaml"), help="設定檔路徑，預設同目錄 config.yaml"
    )
    parser.add_argument(
        "--state-file", default=None, help="已產出關鍵字的狀態檔路徑（預設同目錄 .state.json）"
    )
    return parser


def _resolve(rel: str | Path) -> Path:
    """把設定檔中的相對路徑轉成絕對路徑（禁止硬編碼使用者目錄）。"""
    path = Path(rel)
    return path if path.is_absolute() else MODULE_DIR / path


def _resolve_state_file(args: argparse.Namespace, state_config: dict[str, Any]) -> Path:
    """決定狀態檔位置：--state-file 優先，其次 config，最後同目錄 .state.json。"""
    override = getattr(args, "state_file", None)
    if override:
        return Path(override).expanduser().resolve()
    return _resolve(str(state_config.get("published_file", ".state.json")))


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


# --------------------------------------------------------------------------
# 輸入載入
# --------------------------------------------------------------------------


def _load_inputs(
    config: dict[str, Any], diagnostics: Diagnostics
) -> tuple[list[Any], list[dict[str, Any]], dict[str, Any]]:
    """載入搜尋成效資料、既有頁面清單與品牌檔。"""
    search_data = config.get("search_data") or {}
    content_settings = config.get("content_settings") or {}
    seed_topics = [str(item) for item in content_settings.get("seed_topics") or []]
    if not seed_topics:
        diagnostics.amber(
            symptom="SEED_TOPICS 是空的，關鍵字無法歸類到種子主題",
            fix="在 config.yaml 的 content_settings.seed_topics 至少填 3 個主題",
        )
    try:
        candidates = load_candidates(
            _resolve(str(search_data.get("export_file", "mock/search_console.json"))), seed_topics
        )
    except (KeywordPlannerError, FileNotFoundError) as exc:
        diagnostics.red(
            symptom="讀不到搜尋成效資料，本週無法選題",
            cause=str(exc),
            fix="確認 GSC 匯出檔存在且為 UTF-8 JSON（rows 陣列），或用 --config 指定其他設定檔",
        )
        raise  # red() 一定會 sys.exit 或拋 RedAlert；保留以確保例外不會被吞掉
    links_config = content_settings.get("internal_links") or {}
    pages = load_json_file(
        _resolve(str(links_config.get("site_pages_file", "mock/site_pages.json")))
    ).get("pages")
    brand = load_json_file(
        _resolve(str(content_settings.get("brand_profile_file", "mock/brand_profile.json")))
    )
    return candidates, list(pages or []), brand


# --------------------------------------------------------------------------
# Phase 1：主題選擇
# --------------------------------------------------------------------------


def _run_phase1(
    config: dict[str, Any],
    candidates: list[Any],
    state_file: Path,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """執行 Phase 1（選題），並套用冷卻期避免每週選到同一個字。"""
    search_data = config.get("search_data") or {}
    content_settings = config.get("content_settings") or {}
    state_config = config.get("state") or {}
    settings = SelectionSettings.from_config(search_data, content_settings)
    excluded = keywords_in_cooldown(
        load_state(state_file), int(state_config.get("cooldown_days", 90))
    )
    selection = select_topics(
        candidates,
        settings,
        excluded,
        [str(item) for item in content_settings.get("seed_topics") or []],
        [str(item) for item in content_settings.get("long_tail_modifiers") or []],
    )
    selection["settings"] = settings
    if not selection["selected"]:
        diagnostics.red(
            symptom="本週一個題目都選不出來",
            cause="Top N 關鍵字全被曝光量／難度門檻或冷卻期擋下",
            fix="放寬 search_data.min_impressions / max_difficulty，或補充 SEED_TOPICS",
        )
    return selection


def _build_briefs(
    config: dict[str, Any], selection: dict[str, Any], brand: dict[str, Any]
) -> list[dict[str, Any]]:
    """把選中的關鍵字展開成主題簡報（角度 + H2 大綱 + FAQ 題目）。"""
    content_settings = config.get("content_settings") or {}
    return [
        build_topic_brief(candidate, brand, content_settings)
        for candidate in selection["selected"]
    ]


def _enrich_briefs(
    client: LLMClient,
    config: dict[str, Any],
    briefs: list[dict[str, Any]],
    brand: dict[str, Any],
) -> list[str]:
    """live 模式用 topic_selection 提示詞補角度與搜尋意圖；mock 維持離線推導。"""
    prompts = config.get("prompts") or {}
    system_prompt = load_prompt(
        _resolve(str(prompts.get("topic_selection", "prompts/topic_selection.md")))
    )
    raw = client.complete(
        system=system_prompt,
        user=build_selection_user_prompt(briefs, brand),
        max_tokens=2000,
    )
    text = (raw or "").strip()
    if not text or text.startswith(MOCK_MARKER):
        return []
    try:
        overrides = parse_topics_json(text)
    except ContentGeneratorError as exc:
        return [f"Phase 1 主題角度的模型輸出無法解析（{exc}），已改用程式端推導的角度"]
    apply_topic_overrides(briefs, overrides)
    return []


# --------------------------------------------------------------------------
# Phase 2：文章草擬
# --------------------------------------------------------------------------


def _run_phase2(
    client: LLMClient,
    config: dict[str, Any],
    briefs: list[dict[str, Any]],
    brand: dict[str, Any],
    site_pages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """逐篇草擬文章，回傳 (文章清單, 警告清單)。"""
    content_settings = config.get("content_settings") or {}
    settings = DraftSettings.from_config(content_settings)
    prompts = config.get("prompts") or {}
    system_prompt = load_prompt(
        _resolve(str(prompts.get("article_drafting", "prompts/article_drafting.md")))
    )
    cms_config = config.get("cms") or {}
    articles: list[dict[str, Any]] = []
    warnings: list[str] = []
    for brief in briefs:
        article, draft_warnings = draft_article(
            client, system_prompt, brief, brand, site_pages, settings
        )
        article, check_warnings = finalize_article(article, brief, brand, settings)
        article["brief"] = brief
        article["cms_payload"] = build_cms_payload(article, cms_config)
        articles.append(article)
        warnings.extend(draft_warnings)
        warnings.extend(check_warnings)
    return articles, warnings


def _apply_autonomy(articles: list[dict[str, Any]], gate: AutonomyGate) -> None:
    """依自主權層級標記每篇文章是「待審草稿」還是「可直接發布」。

    這裡把種子主題（seed topic）當成 recipient 餵給 AutonomyGate：白名單放的是
    「哪些主題群已經跑滿觀察期、客戶簽核可以自動發布」，其餘一律降級為草稿。
    """
    for article in articles:
        topic = str(article.get("seed_topic") or "（未分類）")
        article["status"] = "publish" if gate.can_send(topic) else "draft"
        article["effective_autonomy"] = gate.effective_level(topic).value
        payload = article.get("cms_payload")
        if isinstance(payload, dict):
            # CMS 的 default_status 只是初始值；能不能直接發布是安全決策，以閘門為準
            payload["status"] = article["status"]


# --------------------------------------------------------------------------
# 財務模型（金額一律 Decimal）
# --------------------------------------------------------------------------


def _money(value: Decimal) -> str:
    """把金額量化到分位再轉字串（避免 float 誤差寫進提案）。"""
    return str(value.quantize(CENTS, rounding=ROUND_HALF_UP))


def _money_label(value: str) -> str:
    """把金額字串排成人看的樣子（整數不顯示小數位，並加千分位）。"""
    amount = Decimal(value)
    if amount == amount.to_integral_value():
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def _financials(config: dict[str, Any]) -> dict[str, str]:
    """計算單一客戶的價值與淨效益。兩套定價並陳（附錄F 為主線，第05章為 premium）。"""
    module = config.get("module") or {}
    economics = config.get("economics") or {}
    hours = Decimal(str(module.get("recovered_hours_per_month", 0)))
    rate = Decimal(str(economics.get("hourly_rate", DEFAULT_HOURLY_RATE)))
    setup = Decimal(str(module.get("client_setup_price", 0)))
    monthly = Decimal(str(module.get("client_monthly_price", 0)))
    premium_setup = Decimal(str(module.get("premium_setup_price", 0)))
    premium_monthly = Decimal(str(module.get("premium_monthly_price", 0)))
    monthly_value = hours * rate
    return {
        "hourly_rate": _money(rate),
        "recovered_hours_per_month": str(hours),
        "monthly_value": _money(monthly_value),
        "client_setup_price": _money(setup),
        "client_monthly_price": _money(monthly),
        "first_month_net": _money(monthly_value - setup - monthly),
        "recurring_net": _money(monthly_value - monthly),
        "premium_setup_price": _money(premium_setup),
        "premium_monthly_price": _money(premium_monthly),
        "premium_first_month_net": _money(monthly_value - premium_setup - premium_monthly),
        "premium_recurring_net": _money(monthly_value - premium_monthly),
        "outsourcing_monthly_low": _money(Decimal(str(economics.get("outsourcing_monthly_low", 0)))),
        "outsourcing_monthly_high": _money(Decimal(str(economics.get("outsourcing_monthly_high", 0)))),
    }


# --------------------------------------------------------------------------
# 結果組裝與呈現
# --------------------------------------------------------------------------


def _article_row(article: dict[str, Any]) -> dict[str, Any]:
    """把一篇文章壓成回傳結果用的摘要列（含 CMS payload）。"""
    brief = article.get("brief") or {}
    return {
        "keyword": article.get("keyword", ""),
        "seed_topic": article.get("seed_topic", ""),
        "title": article.get("title", ""),
        "slug": article.get("slug", ""),
        "meta_description": article.get("meta_description", ""),
        "angle": brief.get("angle", ""),
        "search_intent": brief.get("search_intent", ""),
        "position": brief.get("position"),
        "impressions": brief.get("impressions", 0),
        "difficulty": brief.get("difficulty", 0),
        "word_count": article.get("word_count", 0),
        "section_count": article.get("section_count", 0),
        "faq_count": article.get("faq_count", 0),
        "link_count": article.get("link_count", 0),
        "placeholder_count": article.get("placeholder_count", 0),
        "internal_links": article.get("internal_links") or [],
        "status": article.get("status", "draft"),
        "effective_autonomy": article.get("effective_autonomy", AutonomyLevel.DRAFT.value),
        "source": article.get("source", "offline"),
        "cms_payload": article.get("cms_payload") or {},
    }


def _assemble_result(
    args: argparse.Namespace,
    config: dict[str, Any],
    selection: dict[str, Any],
    articles: list[dict[str, Any]],
    warnings: list[str],
    state_file: Path,
) -> dict[str, Any]:
    """把設定、選題與草稿組成回傳結果（供測試斷言與通知渲染）。"""
    module = config.get("module") or {}
    runtime = config.get("runtime") or {}
    trigger = config.get("trigger") or {}
    settings: SelectionSettings = selection["settings"]
    rows = [_article_row(article) for article in articles]
    return {
        "module_id": str(module.get("id", "11")),
        "module_name": str(module.get("name", "SEO 內容引擎")),
        "mode": "live" if getattr(args, "live", False) else "mock",
        "dry_run": bool(getattr(args, "dry_run", False)),
        "cms_provider": str((config.get("cms") or {}).get("provider", "")),
        "schedule": str(trigger.get("schedule", "")),
        "timezone": str(trigger.get("timezone", "")),
        "requested_autonomy": str(runtime.get("autonomy", "draft")),
        "striking_distance": [settings.position_min, settings.position_max],
        "articles_planned": settings.articles_per_week,
        "articles": rows,
        "articles_drafted": len(rows),
        "drafts": sum(1 for row in rows if row["status"] == "draft"),
        "scheduled": sum(1 for row in rows if row["status"] == "publish"),
        "total_words": sum(int(row["word_count"]) for row in rows),
        "total_placeholders": sum(int(row["placeholder_count"]) for row in rows),
        "keywords_reviewed": [item.as_dict() for item in selection["reviewed"]],
        "keywords_rejected": list(selection["rejected"]),
        "selected_keywords": [item.query for item in selection["selected"]],
        "stats": dict(selection["stats"]),
        "financials": _financials(config),
        "state_file": str(state_file),
        "warnings": list(warnings),
    }


def _summary_header(result: dict[str, Any], financials: dict[str, str]) -> list[str]:
    """簡報開頭三行：本週規模、觸發設定、客戶價值。"""
    low, high = result["striking_distance"]
    stats = result["stats"]
    return [
        f"📈 {result['module_name']}｜週一內容簡報（cron {result['schedule']}／{result['timezone']}）",
        f"檢視 Top {stats['reviewed_count']} 關鍵字，位置 {low:g}-{high:g} 命中 "
        f"{stats['striking_count']} 個，選出 {result['articles_drafted']}／"
        f"{result['articles_planned']} 篇；共 {result['total_words']:,} 字、"
        f"待填欄位 {result['total_placeholders']} 處",
        f"自主權 {result['requested_autonomy']}｜待審草稿 {result['drafts']} 篇"
        f"／可直接發布 {result['scheduled']} 篇｜模式 {result['mode']}",
        f"💰 每月回收 {financials['recovered_hours_per_month']} 小時 ≈ "
        f"{_money_label(financials['monthly_value'])}（時薪 {_money_label(financials['hourly_rate'])}）"
        f"；方案 {_money_label(financials['client_setup_price'])} 建置 + "
        f"{_money_label(financials['client_monthly_price'])}/月 → 次月起淨效益 "
        f"{_money_label(financials['recurring_net'])}",
    ]


def _article_block(index: int, row: dict[str, Any]) -> list[str]:
    """單篇文章在簡報中的區塊。"""
    position = "無排名資料" if row["position"] is None else f"位置 {row['position']}"
    lines = [
        "",
        f"— #{index}「{row['keyword']}」｜{position}｜曝光 {row['impressions']:,}"
        f"｜難度 {row['difficulty']}｜種子：{row['seed_topic']}",
        f"   標題：{row['title']}",
        f"   角度：{row['angle']}",
        f"   {row['word_count']:,} 字／{row['section_count']} 段／FAQ {row['faq_count']} 題"
        f"／內部連結 {row['link_count']} 個／待填 {row['placeholder_count']} 處"
        f"｜狀態 {row['status']}",
    ]
    lines += [f"   ↳ 連結：{link['anchor']}（{link['url']}）" for link in row["internal_links"]]
    return lines


def format_summary(result: dict[str, Any]) -> str:
    """把本週選題與草稿渲染成人可讀的週一內容簡報（也是通知內文）。"""
    financials = result["financials"]
    lines = _summary_header(result, financials)
    for index, row in enumerate(result["articles"], start=1):
        lines.extend(_article_block(index, row))
    if result["keywords_rejected"]:
        lines.append("")
        lines.append(f"🚫 被門檻擋下的字：{'、'.join(result['keywords_rejected'])}")
    if result["warnings"]:
        lines.append("")
        lines.append("⚠️ 審閱時要處理的提醒：")
        lines.extend(f"  - {warning}" for warning in result["warnings"])
    return "\n".join(lines)


def _deliver(summary: str, args: argparse.Namespace, diagnostics: Diagnostics) -> bool:
    """送出週一簡報。dry-run 只印不送，通道建不起來就退回 console。"""
    if getattr(args, "dry_run", False):
        diagnostics.green("dry-run：已產出本週草稿，未發送通知、未寫入狀態檔")
        print(summary)
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
    return notifier.send(summary, subject="本週 SEO 內容草稿待審")


def _persist_state(
    args: argparse.Namespace, state_file: Path, articles: list[dict[str, Any]]
) -> None:
    """把本週寫過的關鍵字記進狀態檔，下週冷卻期內不會再選到。dry-run 不寫。"""
    if getattr(args, "dry_run", False) or not articles:
        return
    published = load_state(state_file)
    today = date.today().isoformat()
    for article in articles:
        keyword = str(article.get("keyword", "")).strip()
        if keyword:
            published[keyword] = today
    save_state(state_file, published)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    # exit_on_red 讓測試能用 RedAlert 例外驗證紅色路徑，正式執行維持直接退出
    diagnostics = Diagnostics(
        MODULE_NAME, exit_on_red=bool(getattr(args, "exit_on_red", True))
    )
    config = load_config(args.config)
    gate = _build_gate(config.get("runtime") or {}, diagnostics)
    candidates, site_pages, brand = _load_inputs(config, diagnostics)
    state_file = _resolve_state_file(args, config.get("state") or {})
    selection = _run_phase1(config, candidates, state_file, diagnostics)
    briefs = _build_briefs(config, selection, brand)
    client = LLMClient(
        mock=not bool(getattr(args, "live", False)), context_note=CONTEXT_NOTE
    )
    warnings = list(selection["warnings"])
    warnings.extend(_enrich_briefs(client, config, briefs, brand))
    articles, draft_warnings = _run_phase2(client, config, briefs, brand, site_pages)
    warnings.extend(draft_warnings)
    _apply_autonomy(articles, gate)
    warnings.extend(gate.warnings)
    result = _assemble_result(args, config, selection, articles, warnings, state_file)
    for warning in result["warnings"]:
        diagnostics.amber(symptom=warning, fix="人工審閱時處理，確認後再推送 CMS")
    result["amber_count"] = diagnostics.amber_count
    result["summary_text"] = format_summary(result)
    result["notified"] = _deliver(result["summary_text"], args, diagnostics)
    _persist_state(args, state_file, articles)
    return result


def main() -> int:
    """解析參數 -> run() -> 印出結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (
        ContentGeneratorError,
        KeywordPlannerError,
        FileNotFoundError,
        OSError,
        ValueError,
    ) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(
        f"\n✅ 完成：{result['articles_drafted']} 篇草稿、"
        f"{result['total_words']:,} 字、待填欄位 {result['total_placeholders']} 處、"
        f"待審 {result['drafts']} 篇、警告 {len(result['warnings'])} 則"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
