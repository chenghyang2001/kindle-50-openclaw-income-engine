"""demo05 評價監控核心：抓取、去重、情緒評分。

設計重點（踩過才知道的三件事）：
1. 去重狀態檔一律以 ``Path(__file__).parent`` 推算。這個模組會被打包成獨立交付版
   丟到客戶機器上跑，任何絕對路徑都會在第一次部署就爆掉。
2. live 模式每次抓取前強制 sleep >= 1 秒。Google / Trustpilot 都會對短時間內的連續
   請求回 429，而評價 API 被封鎖後要等的是「小時」不是「分鐘」。
3. **星等是升級與否的唯一硬指標，LLM 不得推翻。** 語言模型判錯情緒只是內容不好看，
   判錯升級路徑會讓 1 星負評躺在彙整批次裡過夜——那正是這個模組要解決的問題本身。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# 去重狀態檔預設檔名（實際路徑由 state_path() 推算）
SEEN_FILE_DEFAULT = ".seen.json"

# live 抓取的最小間隔秒數。config 給更小的值也會被拉回這個下限。
MIN_FETCH_DELAY_SECONDS = 1.0

# .seen.json 只保留最近 N 筆，避免長年執行後狀態檔無限膨脹
SEEN_KEEP_LAST = 1000

# 星等 → 情緒標籤。0 代表來源資料的 rating 無法解析。
SENTIMENT_BY_RATING: dict[int, str] = {
    0: "unknown",
    1: "very_negative",
    2: "negative",
    3: "neutral",
    4: "positive",
    5: "very_positive",
}

# 星等 → 情緒分數（-1.0 最負面 ~ +1.0 最正面）
SCORE_BY_RATING: dict[int, float] = {0: 0.0, 1: -1.0, 2: -0.5, 3: 0.0, 4: 0.5, 5: 1.0}


class ReviewFetchError(RuntimeError):
    """單一平台抓取失敗。由呼叫端決定要升級成紅色警報還是琥珀警示。"""


def state_path(file_name: str = SEEN_FILE_DEFAULT) -> Path:
    """回傳去重狀態檔的絕對路徑（以本檔位置推算，禁止硬編碼）。"""
    return Path(__file__).resolve().parent / file_name


def module_dir() -> Path:
    """回傳本模組所在目錄，供 mock 檔相對路徑解析使用。"""
    return Path(__file__).resolve().parent


def review_key(review: dict[str, Any]) -> str:
    """去重鍵值。跨平台可能撞 review_id，所以一律加上平台前綴。"""
    return f"{review.get('platform', '?')}:{review.get('review_id', '')}"


def normalize_review(raw: dict[str, Any], platform_id: str) -> dict[str, Any]:
    """把各平台格式壓成統一欄位。

    rating 解析失敗時給 0 而非預設 5：靜默當成好評會讓負評消失，
    寧可留下 0 讓呼叫端發琥珀警示。
    """
    try:
        rating = int(raw.get("rating"))
    except (TypeError, ValueError):
        rating = 0
    if rating < 0 or rating > 5:
        rating = 0
    return {
        "review_id": str(raw.get("review_id") or raw.get("id") or ""),
        "platform": platform_id,
        "author": str(raw.get("author") or "匿名"),
        "rating": rating,
        "title": str(raw.get("title") or ""),
        "text": str(raw.get("text") or ""),
        "posted_at": str(raw.get("posted_at") or ""),
        "url": str(raw.get("url") or ""),
        "language": str(raw.get("language") or ""),
    }


def load_seen_ids(path: Path) -> set[str]:
    """讀取已處理過的 review key。檔案不存在（首次執行）回空集合。"""
    if not path.is_file():
        return set()
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        # 狀態檔壞掉不該讓整條流程停擺，但也不能假裝沒事——重跑會重複通知一次
        print(f"警告：去重狀態檔無法讀取（{path}）：{exc}，本次視為首次執行")
        return set()
    seen = payload.get("seen", []) if isinstance(payload, dict) else payload
    return {str(item) for item in seen} if isinstance(seen, list) else set()


def save_seen_ids(path: Path, seen: set[str], keep_last: int = SEEN_KEEP_LAST) -> None:
    """寫回去重狀態檔（UTF-8）。超過 keep_last 只保留字典序末段，避免無限成長。"""
    ordered = sorted(seen)[-keep_last:]
    payload = {"seen": ordered, "count": len(ordered)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _read_mock_reviews(base_dir: Path, relative_path: str) -> list[dict[str, Any]]:
    """讀離線 mock 評價檔（零網路零憑證）。"""
    if not relative_path:
        raise ReviewFetchError("平台未設定 mock_file，mock 模式無資料可讀")
    path = (base_dir / relative_path).resolve()
    if not path.is_file():
        raise ReviewFetchError(f"找不到 mock 評價檔：{path}")
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    reviews = payload.get("reviews", []) if isinstance(payload, dict) else payload
    if not isinstance(reviews, list):
        raise ReviewFetchError(f"mock 評價檔格式錯誤（reviews 應為 list）：{path}")
    return reviews


def _fetch_live_reviews(platform: dict[str, Any], delay_seconds: float) -> list[dict[str, Any]]:
    """live 抓取：先睡滿間隔（rate limit 防護），再走 urllib。憑證缺失一律明確報錯。"""
    time.sleep(max(delay_seconds, MIN_FETCH_DELAY_SECONDS))
    platform_id = str(platform.get("id", "?"))
    token_env = str(platform.get("token_env") or "")
    token = os.environ.get(token_env) if token_env else None
    if not token:
        missing = token_env if token_env else "（未設定 token_env）"
        raise ReviewFetchError(f"平台 {platform_id} 缺少環境變數 {missing}")
    url = str(platform.get("api_url") or "")
    if not url:
        raise ReviewFetchError(f"平台 {platform_id} 未設定 api_url")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ReviewFetchError(f"{platform_id} 回應非合法 JSON：{exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ReviewFetchError(f"抓取 {platform_id} 失敗：{exc}") from exc
    return payload.get("reviews", []) if isinstance(payload, dict) else []


def collect_reviews(
    platforms: list[dict[str, Any]],
    base_dir: Path,
    is_mock: bool = True,
    delay_seconds: float = MIN_FETCH_DELAY_SECONDS,
) -> tuple[list[dict[str, Any]], list[str]]:
    """逐平台抓取評價。

    回傳 ``(評價清單, 錯誤訊息清單)``。單一平台失敗不吞掉也不中斷其他平台——
    Trustpilot 掛了不該讓 Google 的 1 星負評也跟著漏掉。
    """
    collected: list[dict[str, Any]] = []
    errors: list[str] = []
    for platform in platforms:
        if not platform.get("enabled", True):
            continue
        platform_id = str(platform.get("id", "?"))
        try:
            raw_reviews = (
                _read_mock_reviews(base_dir, str(platform.get("mock_file", "")))
                if is_mock
                else _fetch_live_reviews(platform, delay_seconds)
            )
        except (ReviewFetchError, OSError, ValueError) as exc:
            errors.append(f"{platform_id}：{exc}")
            continue
        collected.extend(normalize_review(item, platform_id) for item in raw_reviews)
    return collected, errors


def filter_new_reviews(
    reviews: list[dict[str, Any]], seen_ids: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """濾掉已處理過的評價。同批次內的重複 key 也會被擋（來源 API 偶爾回重複頁）。"""
    fresh: list[dict[str, Any]] = []
    duplicates: list[str] = []
    batch_keys: set[str] = set()
    for review in reviews:
        key = review_key(review)
        if key in seen_ids or key in batch_keys:
            duplicates.append(key)
            continue
        batch_keys.add(key)
        fresh.append(review)
    return fresh, duplicates


def is_escalation(review: dict[str, Any], threshold_stars: int = 2) -> bool:
    """1 星到 threshold_stars 星走升級路徑。rating=0（解析失敗）不當負評，另行警示。"""
    rating = int(review.get("rating", 0))
    return 1 <= rating <= threshold_stars


def _sentiment_user_prompt(review: dict[str, Any]) -> str:
    """組出 sentiment.md 要求的輸入格式。"""
    return (
        f"平台：{review.get('platform', '')}\n"
        f"星等：{review.get('rating', 0)}\n"
        f"標題：{review.get('title', '')}\n"
        f"內文：{review.get('text', '')}"
    )


def score_sentiment(
    review: dict[str, Any],
    threshold_stars: int = 2,
    llm: Any | None = None,
    prompt: str = "",
) -> dict[str, Any]:
    """替單則評價加上情緒欄位。

    情緒標籤與 ``is_escalation`` 由星等硬性決定；LLM 只補一段敘述（``summary``）。
    這樣即使 LLM 逾時或回傳垃圾，雙軌路由依然正確。
    """
    rating = int(review.get("rating", 0))
    summary = ""
    if llm is not None and prompt:
        raw_summary = llm.complete(
            system=prompt, user=_sentiment_user_prompt(review), max_tokens=300
        )
        summary = str(raw_summary).strip()
    scored = dict(review)
    scored.update(
        {
            "sentiment": SENTIMENT_BY_RATING.get(rating, "unknown"),
            "sentiment_score": SCORE_BY_RATING.get(rating, 0.0),
            "is_escalation": is_escalation(review, threshold_stars),
            "summary": summary,
        }
    )
    return scored


def route_reviews(scored_reviews: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """雙軌通知路徑分流。

    escalation：1–2 星，立即單獨推播，**不等彙整批次**。
    routine   ：3–5 星（含 rating 解析失敗者），併入彙整通知。
    """
    escalation = [review for review in scored_reviews if review.get("is_escalation")]
    routine = [review for review in scored_reviews if not review.get("is_escalation")]
    return {"escalation": escalation, "routine": routine}


def unparsed_ratings(reviews: list[dict[str, Any]]) -> list[str]:
    """回傳 rating 解析失敗（=0）的評價 key，供呼叫端發琥珀警示。"""
    return [review_key(review) for review in reviews if int(review.get("rating", 0)) == 0]
