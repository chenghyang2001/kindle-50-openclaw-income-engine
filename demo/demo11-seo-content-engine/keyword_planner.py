"""模組 #11：SEO 內容引擎 — Phase 1 主題選擇（Topic Selection）。

把 Google Search Console 的成效資料，變成「這週該寫哪三篇」的決定。

核心規則來自附錄F p04 的 `prompt.txt` 逐字內容：
**prefer keywords where the site ranks position 8-20**（優先挑排名 8-20 的字）。

為什麼這條規則值得寫死在程式裡而不是交給 LLM 判斷：
排名 8-20 代表 Google 已經認可這個頁面與這個字相關，只是還沒排到前段。
補一篇對題的內容是「推一把」；排名 1-7 再寫增量有限；排名 20 以後多半
不是內容問題（是網站權重或站內結構），寫再多篇也追不上。
這是一條可驗證的商業規則，不該每週交給模型重新發明一次。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# 候選字的來源：GSC 實際資料，或由 SEED_TOPICS 推導出來的長尾字
SOURCE_SEARCH_CONSOLE = "search_console"
SOURCE_SEED_EXPANSION = "seed_expansion"

# 優先級分層：數字越小越先被選
TIER_STRIKING = 0  # 排名 8-20，書中指定的甜蜜區
TIER_LONG_TAIL = 1  # 排名 20 之後或沒有排名資料的長尾字
TIER_ALREADY_RANKING = 2  # 排名 1-7，已經在前段，增量最小

MAX_DIFFICULTY_CAP = 99  # 難度 100 會讓分數歸零，保留一點區別度


class KeywordPlannerError(RuntimeError):
    """關鍵字規劃流程中可預期的失敗（資料格式錯誤、來源為空等）。"""


@dataclass(frozen=True)
class SelectionSettings:
    """選字門檻（對應 config.yaml 的 search_data 與 content_settings）。"""

    position_min: float
    position_max: float
    min_impressions: int
    max_difficulty: int
    top_keywords: int
    articles_per_week: int

    @classmethod
    def from_config(
        cls, search_data: dict[str, Any], content_settings: dict[str, Any]
    ) -> "SelectionSettings":
        """從設定片段建立門檻物件。缺欄位用書中預設值，但不接受不合理的區間。"""
        striking = search_data.get("striking_distance") or {}
        position_min = float(striking.get("position_min", 8))
        position_max = float(striking.get("position_max", 20))
        if position_min > position_max:
            raise KeywordPlannerError(
                f"striking_distance 區間顛倒：position_min={position_min} > position_max={position_max}"
            )
        return cls(
            position_min=position_min,
            position_max=position_max,
            min_impressions=int(search_data.get("min_impressions", 0)),
            max_difficulty=int(search_data.get("max_difficulty", 100)),
            top_keywords=int(search_data.get("top_keywords", 12)),
            articles_per_week=int(content_settings.get("articles_per_week", 3)),
        )


@dataclass(frozen=True)
class KeywordCandidate:
    """單一關鍵字候選（GSC 一列，或一個推導出來的長尾字）。"""

    query: str
    position: float | None
    impressions: int
    clicks: int
    difficulty: int
    seed_topic: str | None
    source: str

    @classmethod
    def from_raw(
        cls, raw: dict[str, Any], seed_topics: list[str]
    ) -> "KeywordCandidate":
        """從 GSC 匯出的一列建立候選字。缺 query 直接拋錯，不用空字串頂替。"""
        query = str(raw.get("query", "")).strip()
        if not query:
            raise KeywordPlannerError(f"搜尋資料有一列缺少 query 欄位：{raw!r}")
        try:
            position = None if raw.get("position") is None else float(raw["position"])
            impressions = int(raw.get("impressions", 0))
            clicks = int(raw.get("clicks", 0))
            difficulty = int(raw.get("difficulty", 0))
        except (TypeError, ValueError) as exc:
            raise KeywordPlannerError(f"關鍵字 {query!r} 的數值欄位無法解析：{exc}") from exc
        if position is not None and position <= 0:
            raise KeywordPlannerError(f"關鍵字 {query!r} 的 position 必須大於 0，收到 {position}")
        return cls(
            query=query,
            position=position,
            impressions=max(0, impressions),
            clicks=max(0, clicks),
            difficulty=min(max(0, difficulty), 100),
            seed_topic=match_seed_topic(query, seed_topics),
            source=SOURCE_SEARCH_CONSOLE,
        )

    @property
    def has_position(self) -> bool:
        """是否有實際排名資料（長尾擴展字沒有）。"""
        return self.position is not None

    def as_dict(self) -> dict[str, Any]:
        """轉成可序列化的 dict（供結果回傳與通知渲染）。"""
        return {
            "query": self.query,
            "position": self.position,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "difficulty": self.difficulty,
            "seed_topic": self.seed_topic,
            "source": self.source,
        }


# --------------------------------------------------------------------------
# 資料載入
# --------------------------------------------------------------------------


def load_json_file(path: str | Path) -> dict[str, Any]:
    """讀取 UTF-8 JSON。檔案不存在或格式錯誤都要明確報出絕對路徑。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"找不到檔案：{target.resolve()}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise KeywordPlannerError(f"JSON 解析失敗（{target.resolve()}）：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise KeywordPlannerError(f"檔案不是 UTF-8 編碼（{target.resolve()}）：{exc}") from exc
    if not isinstance(data, dict):
        raise KeywordPlannerError(f"JSON 頂層必須是物件（{target.resolve()}）")
    return data


def match_seed_topic(query: str, seed_topics: list[str]) -> str | None:
    """判斷查詢字屬於哪個種子主題（SEED_TOPICS）。

    比對用小寫子字串：中文沒有詞界，用子字串是最不會漏抓的做法；
    英文縮寫（wms / erp）則靠小寫化避免大小寫造成的漏配。
    """
    lowered = query.lower()
    for topic in seed_topics:
        text = str(topic).strip()
        if text and text.lower() in lowered:
            return text
    return None


def load_candidates(
    path: str | Path, seed_topics: list[str]
) -> list[KeywordCandidate]:
    """讀取搜尋成效資料，回傳候選字清單。沒有任何一列就是紅色警報等級的問題。"""
    payload = load_json_file(path)
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise KeywordPlannerError(f"搜尋資料沒有任何 rows：{Path(path).resolve()}")
    return [KeywordCandidate.from_raw(row, seed_topics) for row in rows]


# --------------------------------------------------------------------------
# 篩選與排序
# --------------------------------------------------------------------------


def top_keywords(
    candidates: list[KeywordCandidate], limit: int
) -> list[KeywordCandidate]:
    """依曝光量取前 N 名（ch05_p04：Agent 自動抓取 Top 12 關鍵字）。

    先取 Top N 再篩門檻，而不是反過來：這樣「本週看了哪 12 個字」是一個
    穩定可稽核的清單，客戶問起來能直接回答，不會因為門檻微調就換一批。
    """
    if limit <= 0:
        return list(candidates)
    ordered = sorted(candidates, key=lambda item: (-item.impressions, item.query))
    return ordered[:limit]


def is_striking_distance(
    position: float | None, low: float, high: float
) -> bool:
    """是否落在「可攻擊距離」（含邊界）。position 缺值一律不算。"""
    if position is None:
        return False
    return low <= position <= high


def tier_of(candidate: KeywordCandidate, settings: SelectionSettings) -> int:
    """把候選字分到三個優先層。"""
    if is_striking_distance(
        candidate.position, settings.position_min, settings.position_max
    ):
        return TIER_STRIKING
    if candidate.position is not None and candidate.position < settings.position_min:
        return TIER_ALREADY_RANKING
    return TIER_LONG_TAIL


def opportunity_score(
    candidate: KeywordCandidate, settings: SelectionSettings
) -> float:
    """機會分數：曝光量 × 難度折扣 × 排名接近度。

    排名接近度讓同樣曝光量的字裡，位置 9 的排在位置 19 前面——越接近第一頁，
    補一篇內容就能推上去的機率越高。沒有排名資料的擴展字沒有這個加成。
    """
    if not candidate.has_position or candidate.impressions <= 0:
        return 0.0
    difficulty = min(candidate.difficulty, MAX_DIFFICULTY_CAP)
    base = candidate.impressions * (1.0 - difficulty / 100.0)
    span = settings.position_max - settings.position_min
    if span <= 0 or candidate.position is None:
        return base
    proximity = 1.0 + max(0.0, (settings.position_max - candidate.position)) / span * 0.5
    return base * proximity


def rejection_reason(
    candidate: KeywordCandidate, settings: SelectionSettings
) -> str | None:
    """回傳這個字被門檻擋下的原因；通過就回 None。"""
    if candidate.source == SOURCE_SEED_EXPANSION:
        return None
    if candidate.impressions < settings.min_impressions:
        return f"曝光量 {candidate.impressions} 低於門檻 {settings.min_impressions}"
    if candidate.difficulty > settings.max_difficulty:
        return f"難度 {candidate.difficulty} 高於門檻 {settings.max_difficulty}"
    return None


def filter_eligible(
    candidates: list[KeywordCandidate], settings: SelectionSettings
) -> tuple[list[KeywordCandidate], list[str]]:
    """套用曝光量與難度門檻，回傳 (通過清單, 被擋下的說明)。"""
    eligible: list[KeywordCandidate] = []
    rejected: list[str] = []
    for candidate in candidates:
        reason = rejection_reason(candidate, settings)
        if reason is None:
            eligible.append(candidate)
        else:
            rejected.append(f"{candidate.query}（{reason}）")
    return eligible, rejected


def expand_long_tail(
    seed_topics: list[str],
    modifiers: list[str],
    known_queries: set[str],
) -> list[KeywordCandidate]:
    """種子主題 × 修飾詞 = 長尾候選字，排除已在 GSC 出現過的組合。

    這些字沒有實際曝光數據，屬於推測性題目，因此一律歸在 TIER_LONG_TAIL，
    只有在可攻擊距離的字不夠 articles_per_week 時才會被選中。
    """
    expanded: list[KeywordCandidate] = []
    seen_lower = {str(item).lower() for item in known_queries}
    for topic in seed_topics:
        for modifier in modifiers:
            query = f"{topic} {modifier}".strip()
            if not query or query.lower() in seen_lower:
                continue
            seen_lower.add(query.lower())
            expanded.append(
                KeywordCandidate(
                    query=query,
                    position=None,
                    impressions=0,
                    clicks=0,
                    difficulty=0,
                    seed_topic=str(topic),
                    source=SOURCE_SEED_EXPANSION,
                )
            )
    return expanded


def rank_candidates(
    candidates: list[KeywordCandidate], settings: SelectionSettings
) -> list[KeywordCandidate]:
    """依 (優先層, 機會分數) 排序，同分時用查詢字排序確保結果可重現。"""
    return sorted(
        candidates,
        key=lambda item: (
            tier_of(item, settings),
            -opportunity_score(item, settings),
            item.query,
        ),
    )


# --------------------------------------------------------------------------
# 狀態檔（避免每週選到同一題）
# --------------------------------------------------------------------------


def load_state(path: str | Path) -> dict[str, str]:
    """讀取狀態檔，回傳 {關鍵字: 最後產出日期 ISO 字串}。檔案不存在視為空。"""
    target = Path(path)
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise KeywordPlannerError(f"狀態檔損毀（{target.resolve()}）：{exc}") from exc
    published = data.get("published") if isinstance(data, dict) else None
    if not isinstance(published, dict):
        return {}
    return {str(key): str(value) for key, value in published.items()}


def save_state(path: str | Path, published: dict[str, str]) -> None:
    """寫回狀態檔（UTF-8）。父目錄不存在就建起來。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"published": dict(sorted(published.items()))}
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _parse_iso_date(value: str) -> date | None:
    """把 ISO 日期字串轉成 date；格式不對回 None（當成從未產出過）。"""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def keywords_in_cooldown(
    published: dict[str, str], cooldown_days: int, today: date | None = None
) -> set[str]:
    """回傳仍在冷卻期內、本週不該重複寫的關鍵字（小寫）。

    冷卻期而非永久排除：一個字在 90 天後值得改版重寫，那時搜尋意圖與
    競品內容都變了，硬性封鎖等於逼客戶永遠不能更新既有文章。
    """
    if cooldown_days <= 0:
        return set()
    reference = today or date.today()
    cutoff = reference - timedelta(days=cooldown_days)
    blocked: set[str] = set()
    for query, stamp in published.items():
        drafted_on = _parse_iso_date(stamp)
        if drafted_on is None or drafted_on > cutoff:
            blocked.add(query.lower())
    return blocked


# --------------------------------------------------------------------------
# Phase 1 主流程
# --------------------------------------------------------------------------


def select_topics(
    candidates: list[KeywordCandidate],
    settings: SelectionSettings,
    excluded: set[str] | None = None,
    seed_topics: list[str] | None = None,
    modifiers: list[str] | None = None,
) -> dict[str, Any]:
    """執行 Phase 1：抓 Top N -> 套門檻 -> 排序 -> 選出本週要寫的幾篇。

    回傳 {"selected", "reviewed", "rejected", "warnings", "stats"}。
    """
    blocked = {item.lower() for item in (excluded or set())}
    reviewed = top_keywords(candidates, settings.top_keywords)
    eligible, rejected = filter_eligible(reviewed, settings)
    pool = [item for item in eligible if item.query.lower() not in blocked]
    warnings = _cooldown_warning(eligible, pool)

    striking = [item for item in pool if tier_of(item, settings) == TIER_STRIKING]
    if len(striking) < settings.articles_per_week:
        pool = pool + _fallback_pool(
            candidates, pool, blocked, seed_topics or [], modifiers or []
        )
        warnings.append(
            f"可攻擊距離（位置 {settings.position_min:g}-{settings.position_max:g}）"
            f"只找到 {len(striking)} 個字，不足本週的 {settings.articles_per_week} 篇；"
            f"已改用長尾擴展字補位，這些字沒有實際曝光數據，請人工確認題目值得寫"
        )

    ranked = rank_candidates(pool, settings)
    selected = ranked[: settings.articles_per_week]
    warnings.extend(_shortage_warning(selected, settings))
    return {
        "selected": selected,
        "reviewed": reviewed,
        "rejected": rejected,
        "warnings": warnings,
        "stats": {
            "reviewed_count": len(reviewed),
            "eligible_count": len(eligible),
            "striking_count": len(striking),
            "pool_count": len(pool),
        },
    }


def _cooldown_warning(
    eligible: list[KeywordCandidate], pool: list[KeywordCandidate]
) -> list[str]:
    """被冷卻期擋下的字要說出來，否則客戶會以為 Agent 漏抓。"""
    skipped = len(eligible) - len(pool)
    if skipped <= 0:
        return []
    return [f"有 {skipped} 個合格關鍵字仍在冷卻期內（近期已寫過），本週略過"]


def _fallback_pool(
    candidates: list[KeywordCandidate],
    pool: list[KeywordCandidate],
    blocked: set[str],
    seed_topics: list[str],
    modifiers: list[str],
) -> list[KeywordCandidate]:
    """可攻擊距離的字不夠時，補上長尾擴展字。"""
    known = {item.query for item in candidates} | {item.query for item in pool}
    expanded = expand_long_tail(seed_topics, modifiers, known)
    return [item for item in expanded if item.query.lower() not in blocked]


def _shortage_warning(
    selected: list[KeywordCandidate], settings: SelectionSettings
) -> list[str]:
    """真的湊不滿篇數時要明說，不可靜默少產出。"""
    if len(selected) >= settings.articles_per_week:
        return []
    return [
        f"本週只選出 {len(selected)} 個題目，少於設定的 "
        f"{settings.articles_per_week} 篇；請補充 SEED_TOPICS 或放寬曝光量門檻"
    ]
