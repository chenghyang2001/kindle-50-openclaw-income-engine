"""demo05 客戶評價監控與回覆草擬 — 主流程。

每 6 小時掃一次 Google / Trustpilot / Amazon，偵測新評價、評分情緒、依品牌語氣草擬回覆，
並走**雙軌通知路徑**：

    1–2 星負評 → 升級路徑（escalation）：每則立即單獨推播，30 分鐘內到手機，不等彙整批次
    3–5 星評價 → 一般路徑（routine）  ：併成一則彙整通知

自主權預設 DRAFT——公開回覆送出後無法撤回，本模組不提供自動送出（理由見 README）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import LLMClient, LLMError  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402
from monitor import (  # noqa: E402
    MIN_FETCH_DELAY_SECONDS,
    SEEN_FILE_DEFAULT,
    ReviewFetchError,
    collect_reviews,
    filter_new_reviews,
    load_seen_ids,
    module_dir,
    review_key,
    route_reviews,
    save_seen_ids,
    score_sentiment,
    state_path,
    unparsed_ratings,
)

MODULE_NAME = "demo05-review-monitor"

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出
CONTEXT_NOTE = (
    "這些文字會出現在公開的評價頁面，任何一句都可能被截圖轉發。"
    "寫給看評價的潛在客戶看，不是寫給抱怨者一個人看。"
)


def load_prompt(name: str) -> str:
    """讀取 prompts/<name>.md。提示詞是資產，一律獨立成檔不內嵌。"""
    path = MODULE_DIR / "prompts" / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(f"找不到提示詞檔：{path}")
    return path.read_text(encoding="utf-8")


def load_brand_voice(brand_cfg: dict[str, Any], diag: Diagnostics) -> dict[str, Any]:
    """合併 config 的品牌語氣設定與語氣樣本檔。

    樣本檔缺失只發琥珀警示（對應書中 tone_mismatch），不中斷流程——
    沒有樣本的回覆只是比較平淡，不是災難。
    """
    merged: dict[str, Any] = dict(brand_cfg)
    profile_name = str(brand_cfg.get("profile_file") or "")
    profile_path = (module_dir() / profile_name).resolve() if profile_name else None
    if profile_path is None or not profile_path.is_file():
        diag.amber("缺少品牌語氣樣本（tone_mismatch）", "在 mock/brand_voice.json 補 3-5 則真實回覆樣本")
        merged.setdefault("tone_examples", [])
        return merged
    with profile_path.open(encoding="utf-8") as handle:
        profile = json.load(handle)
    merged["tone_examples"] = profile.get("tone_examples", [])
    merged.setdefault("signature", profile.get("signature", ""))
    return merged


def build_gate(runtime_cfg: dict[str, Any], diag: Diagnostics) -> AutonomyGate:
    """建立自主權閘門。設定有誤一律往安全側倒（退回 DRAFT），絕不放寬。"""
    level_name = str(runtime_cfg.get("autonomy", "draft")).strip().lower()
    try:
        level = AutonomyLevel(level_name)
    except ValueError:
        diag.amber(f"未知的自主權設定「{level_name}」", "runtime.autonomy 僅接受 read_only / draft / supervised_auto，本次退回 draft")
        level = AutonomyLevel.DRAFT
    if level is AutonomyLevel.SUPERVISED_AUTO:
        diag.amber("公開評價回覆被設為 SUPERVISED_AUTO", "公開回覆送出後無法撤回，建議改回 draft（見 README）")
    try:
        return AutonomyGate(
            level=level,
            approved_senders=list(runtime_cfg.get("approved_senders") or []),
            days_in_draft=int(runtime_cfg.get("days_in_draft", 0)),
        )
    except AutonomyError as exc:
        diag.amber(f"自主權設定違規：{exc}", "已強制退回 DRAFT，補齊 approved_senders 後再調整")
        return AutonomyGate(level=AutonomyLevel.DRAFT)


def summarize_review(review: dict[str, Any]) -> dict[str, Any]:
    """壓成通知與測試用的精簡結構（不夾帶評價全文，避免通知爆量）。"""
    return {
        "key": review_key(review),
        "review_id": review.get("review_id", ""),
        "platform": review.get("platform", ""),
        "author": review.get("author", ""),
        "rating": int(review.get("rating", 0)),
        "sentiment": review.get("sentiment", "unknown"),
        "title": review.get("title", ""),
        "url": review.get("url", ""),
        "posted_at": review.get("posted_at", ""),
        "summary": review.get("summary", ""),
    }


def gather_scored_reviews(
    monitor_cfg: dict[str, Any],
    state_file: Path,
    is_mock: bool,
    llm: LLMClient,
    diag: Diagnostics,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """抓取 → 去重 → 情緒評分。回傳 (已評分的新評價, 統計)。"""
    platforms = list(monitor_cfg.get("platforms") or [])
    if not platforms:
        diag.red("沒有任何可監控的平台", "config.yaml 的 monitor.platforms 為空", "至少啟用一個平台並填 mock_file / api_url")
    delay = float(monitor_cfg.get("fetch_delay_seconds", MIN_FETCH_DELAY_SECONDS))
    reviews, errors = collect_reviews(platforms, module_dir(), is_mock=is_mock, delay_seconds=delay)
    for message in errors:
        diag.amber(f"平台抓取失敗：{message}", "檢查該平台的 mock_file 是否存在，或 live 模式的憑證與網路")
    if errors and not reviews:
        diag.red("所有評價來源都抓不到資料", "; ".join(errors), "先用 --mock 確認流程，再逐一修復平台憑證")
    seen = load_seen_ids(state_file)
    fresh, duplicates = filter_new_reviews(reviews, seen)
    broken = unparsed_ratings(fresh)
    if broken:
        diag.amber(f"{len(broken)} 則評價的星等無法解析：{', '.join(broken)}", "檢查來源欄位命名是否改版；這些評價一律走一般路徑，不會被當成負評")
    threshold = int(monitor_cfg.get("escalation_threshold_stars", 2))
    prompt = load_prompt("sentiment")
    scored = [score_sentiment(review, threshold, llm, prompt) for review in fresh]
    stats = {"fetched": len(reviews), "duplicates": len(duplicates), "errors": errors}
    return scored, stats


def _reply_user_prompt(review: dict[str, Any], brand_voice: dict[str, Any]) -> str:
    """組出 draft_reply.md 要求的輸入格式（品牌區塊 + 評價區塊）。"""
    examples = brand_voice.get("tone_examples") or []
    sample_lines = "\n".join(
        f"- {item.get('context', '')} → {item.get('reply', '')}" for item in examples
    )
    banned = "、".join(brand_voice.get("banned_phrases") or []) or "（無）"
    return (
        f"品牌語氣：{brand_voice.get('tone', '')}\n"
        f"禁語：{banned}\n"
        f"署名：{brand_voice.get('signature', '')}\n"
        f"字數上限：{brand_voice.get('max_words', 120)}\n"
        f"語氣樣本：\n{sample_lines or '（無）'}\n"
        "---\n"
        f"平台：{review.get('platform', '')}\n"
        f"星等：{review.get('rating', 0)}\n"
        f"標題：{review.get('title', '')}\n"
        f"內文：{review.get('text', '')}\n"
        f"情緒判讀：{review.get('summary', '')}"
    )


def draft_replies(
    scored_reviews: list[dict[str, Any]],
    gate: AutonomyGate,
    llm: LLMClient,
    brand_voice: dict[str, Any],
) -> list[dict[str, Any]]:
    """為每則新評價草擬回覆。

    READ_ONLY 層級完全不草擬（只分類與分析）；其餘層級一律產出草稿，
    但 ``can_send`` 由 AutonomyGate 決定，本模組不自行放行。
    """
    if gate.effective_level("*") is AutonomyLevel.READ_ONLY:
        return []
    prompt = load_prompt("draft_reply")
    drafts: list[dict[str, Any]] = []
    for review in scored_reviews:
        platform = str(review.get("platform", ""))
        reply = llm.complete(system=prompt, user=_reply_user_prompt(review, brand_voice), max_tokens=800)
        drafts.append(
            {
                "key": review_key(review),
                "review_id": review.get("review_id", ""),
                "platform": platform,
                "author": review.get("author", ""),
                "rating": int(review.get("rating", 0)),
                "is_escalation": bool(review.get("is_escalation")),
                "autonomy": gate.effective_level(platform).value,
                "can_send": gate.can_send(platform),
                "reply": str(reply).strip(),
            }
        )
    return drafts


def format_alert(review: dict[str, Any], reply: str, escalation_minutes: int) -> str:
    """單則負評的升級警報文案（越短越好，這是要在手機鎖定畫面上被讀完的）。"""
    stars = "★" * int(review.get("rating", 0))
    return (
        f"🚨 負評警報（{escalation_minutes} 分鐘內處理）\n"
        f"平台：{review.get('platform', '')}｜星等：{stars}（{review.get('rating', 0)}）\n"
        f"作者：{review.get('author', '')}\n"
        f"標題：{review.get('title', '')}\n"
        f"判讀：{review.get('summary', '') or '（未取得）'}\n"
        f"連結：{review.get('url', '') or '（無）'}\n"
        f"--- 回覆草稿（需人工審查後才可送出）---\n{reply or '（未產生）'}"
    )


def format_digest(
    routine_reviews: list[dict[str, Any]], module_name: str, max_items: int
) -> str:
    """一般評價彙整文案。超過 max_items 只給統計，避免通知變成噪音。

    星等無法判讀（rating=0）的評價會被排到最前面並明確標示：amber 警示是給維運看的，
    這則彙整是給老闆看的，他不會去翻 stderr。這類評價可能藏著負評，必須在正常閱讀
    流程中就被看見。排序也保證它們不會因為 max_items 截斷而消失。
    """
    if not routine_reviews:
        return f"【{module_name}】本輪無一般評價。"
    unknown = [item for item in routine_reviews if int(item.get("rating", 0)) == 0]
    ordered = unknown + [item for item in routine_reviews if int(item.get("rating", 0)) != 0]
    lines = [f"【{module_name}】一般評價彙整：共 {len(routine_reviews)} 則"]
    if unknown:
        lines.append(f"⚠️ {len(unknown)} 則評價星等無法判讀，已列入下方明細，請人工確認")
    for review in ordered[:max_items]:
        rating = int(review.get("rating", 0))
        stars = f"{rating} 星" if rating else "星等未知 ⚠️"
        lines.append(
            f"・{review.get('platform', '')}｜{stars}｜"
            f"{review.get('author', '')}：{review.get('title', '') or '（無標題）'}"
        )
    if len(ordered) > max_items:
        lines.append(f"…另有 {len(ordered) - max_items} 則未列出，草稿已備妥待審。")
    return "\n".join(lines)


def dispatch_notifications(
    notifier: Notifier,
    routed: dict[str, list[dict[str, Any]]],
    drafts: list[dict[str, Any]],
    monitor_cfg: dict[str, Any],
    is_dry_run: bool,
) -> dict[str, Any]:
    """雙軌發送：負評逐則立即推播，一般評價併成一則彙整。"""
    reply_by_key = {draft["key"]: draft["reply"] for draft in drafts}
    escalation_minutes = int(monitor_cfg.get("escalation_minutes", 30))
    sent_escalations = 0
    for review in routed["escalation"]:
        text = format_alert(review, reply_by_key.get(review_key(review), ""), escalation_minutes)
        if is_dry_run:
            continue
        if notifier.send(text, subject=f"🚨 {review.get('rating', 0)} 星負評｜{review.get('platform', '')}"):
            sent_escalations += 1
    digest_sent = False
    if routed["routine"] and not is_dry_run:
        digest = format_digest(routed["routine"], "客戶評價監控", int(monitor_cfg.get("digest_max_items", 10)))
        digest_sent = notifier.send(digest, subject="客戶評價彙整")
    return {
        "escalation_sent": sent_escalations,
        "escalation_pending": len(routed["escalation"]) - sent_escalations,
        "digest_sent": digest_sent,
    }


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（介面依 CONTRACT.md §6）。"""
    parser = argparse.ArgumentParser(description="demo05 客戶評價監控與回覆草擬")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式，零憑證零網路（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實平台 API（與 --mock 互斥）")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不實際發送、不寫入去重狀態")
    parser.add_argument("--notify", choices=list(Notifier.SUPPORTED), default="console", help="通知管道")
    parser.add_argument("--config", default=str(MODULE_DIR / "config.yaml"), help="設定檔路徑")
    parser.add_argument("--state-file", default=None, help="去重狀態檔路徑（預設同目錄 .seen.json）")
    return parser


def _resolve_state_file(args: argparse.Namespace, monitor_cfg: dict[str, Any]) -> Path:
    """決定去重狀態檔位置。未指定時以本檔目錄推算，禁止硬編碼。"""
    if args.state_file:
        return Path(args.state_file).expanduser().resolve()
    return state_path(str(monitor_cfg.get("seen_state_file", SEEN_FILE_DEFAULT)))


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    is_mock = not args.live
    config = load_config(args.config)
    diag = Diagnostics(MODULE_NAME, exit_on_red=False)
    module_cfg = config.get("module") or {}
    monitor_cfg = config.get("monitor") or {}
    gate = build_gate(config.get("runtime") or {}, diag)
    llm = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    notifier = Notifier(channel=args.notify)
    state_file = _resolve_state_file(args, monitor_cfg)
    scored, stats = gather_scored_reviews(monitor_cfg, state_file, is_mock, llm, diag)
    brand_voice = load_brand_voice(config.get("brand_voice") or {}, diag)
    drafts = draft_replies(scored, gate, llm, brand_voice)
    routed = route_reviews(scored)
    notified = dispatch_notifications(notifier, routed, drafts, monitor_cfg, args.dry_run)
    if not args.dry_run and scored:
        save_seen_ids(state_file, load_seen_ids(state_file) | {review_key(item) for item in scored})
    return _build_result(args, module_cfg, monitor_cfg, gate, diag, scored, stats, routed, drafts, notified, state_file)


def _build_result(
    args: argparse.Namespace,
    module_cfg: dict[str, Any],
    monitor_cfg: dict[str, Any],
    gate: AutonomyGate,
    diag: Diagnostics,
    scored: list[dict[str, Any]],
    stats: dict[str, Any],
    routed: dict[str, list[dict[str, Any]]],
    drafts: list[dict[str, Any]],
    notified: dict[str, Any],
    state_file: Path,
) -> dict[str, Any]:
    """組出回傳結構。獨立成函式，避免 run() 超過 30 行。"""
    return {
        "module_id": str(module_cfg.get("id", "05")),
        "module_name": str(module_cfg.get("name", "客戶評價監控與回覆草擬")),
        "mode": "mock" if not args.live else "live",
        "dry_run": bool(args.dry_run),
        "poll_interval_hours": int(monitor_cfg.get("poll_interval_hours", 6)),
        "escalation_minutes": int(monitor_cfg.get("escalation_minutes", 30)),
        "escalation_threshold_stars": int(monitor_cfg.get("escalation_threshold_stars", 2)),
        "fetched": int(stats["fetched"]),
        "new": len(scored),
        "skipped_duplicates": int(stats["duplicates"]),
        "fetch_errors": list(stats["errors"]),
        "escalations": [summarize_review(item) for item in routed["escalation"]],
        "routine": [summarize_review(item) for item in routed["routine"]],
        "drafts": drafts,
        "notified": notified,
        "notify_channel": args.notify,
        "autonomy_level": gate.effective_level("*").value,
        "warnings": list(gate.warnings),
        "amber_count": diag.amber_count,
        "state_file": str(state_file),
    }


def main() -> int:
    """解析參數 -> run() -> 印出結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except RedAlert as exc:
        print(f"紅色警報：{exc}", file=sys.stderr)
        return 1
    except (ReviewFetchError, NotifierError, LLMError, AutonomyError) as exc:
        print(f"執行失敗：{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"設定或資料錯誤：{exc}", file=sys.stderr)
        return 1
    print(
        f"\n抓取 {result['fetched']} 則｜新評價 {result['new']} 則｜"
        f"負評升級 {len(result['escalations'])} 則｜一般 {len(result['routine'])} 則｜"
        f"草稿 {len(result['drafts'])} 份（自主權：{result['autonomy_level']}）"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
