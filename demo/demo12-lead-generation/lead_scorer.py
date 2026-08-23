"""ICP 評分引擎、冷開發信法遵閘門、三階段外聯節奏狀態機。

刻意與 `main.py` 分離：本模組屬於「主動對外發送」型自動化，下面兩條規則的
正確性比任何功能都重要，必須能脫離 LLM / 通知管道 / 設定載入被單獨測試。

1. **評分與發送權限是兩件事**（`LeadScorer` vs `ComplianceGate`）。
   任何線索都可以被評分——評分只是內部分析，不觸碰對方，屬 READ_ONLY 安全範圍；
   能不能寄信是另一道獨立閘門。兩者若揉在一起，「不能寄但想知道值不值得補資料」
   這個天天發生的需求就會逼人繞過法遵檢查。

2. **`require_unsubscribe` 是設定檔改不掉的硬規則**（與 demo10 的 `stop_on_reply`
   同一種設計）。寄出一封沒有退訂管道的冷開發信，傷害是不可逆的：對方按下
   「檢舉垃圾郵件」之後，受損的是整個寄件網域的信譽，不只是這一封。

金額一律 `Decimal`：管線價值會被拿去對客戶報 ROI，float 的二進位誤差在這種
場合是直接的商譽風險。分數同樣用 `Decimal`，因為權重是可設定的，
0.1 + 0.2 這類誤差會讓「為什麼是 74.99 分不是 75 分」變成無法回答的問題。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 狀態檔一律以模組目錄為基準，禁止硬編碼使用者路徑
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = MODULE_DIR / "state" / "outreach_state.json"

ZERO = Decimal("0")
HALF = Decimal("0.5")
ONE = Decimal("1")
HUNDRED = Decimal("100")
TWO_PLACES = Decimal("0.01")

# ICP 分數帶（門檻值可在 config.yaml 調整）
BAND_HOT = "hot"
BAND_WARM = "warm"
BAND_COLD = "cold"

# 決策動作
ACTION_OUTREACH = "outreach"
ACTION_ENRICH = "enrich"
ACTION_REJECT = "reject"
ACTION_BLOCK = "block"

# 法遵阻擋原因（對外回報用的穩定字串鍵，測試會直接斷言）
BLOCK_MISSING_EMAIL = "missing_email"
BLOCK_SUPPRESSED = "suppressed"
BLOCK_UNLAWFUL_SOURCE = "unlawful_source"
BLOCK_CONSENT_REQUIRED = "consent_required_region"

# 其他決策原因
REJECT_BELOW_THRESHOLD = "below_min_score"
ENRICH_INCOMPLETE = "incomplete_profile"
HALT_CADENCE_COMPLETE = "cadence_complete"
HALT_NOT_DUE = "not_due"
HALT_BAD_DATA = "bad_data"

# 評分權重的四個面向（順序即報表輸出順序）
WEIGHT_FIELDS = ("industry", "company_size", "trigger_event", "tech_stack")

# 寄件人識別必備欄位（CAN-SPAM：可辨識寄件人 + 有效回覆管道 + 實體郵寄地址）
REQUIRED_IDENTITY_FIELDS = (
    "sender_name",
    "company_name",
    "postal_address",
    "reply_to_email",
    "unsubscribe_url",
)


class ScoringError(ValueError):
    """ICP 設定或線索資料格式錯誤。"""


class ComplianceBlocked(RuntimeError):
    """發送前複查判定不得外送（法遵閘門）。"""

    def __init__(self, lead_id: str, reason: str, detail: str) -> None:
        super().__init__(f"[{lead_id}] {reason}：{detail}")
        self.lead_id = lead_id
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# 基礎工具
# ---------------------------------------------------------------------------


def normalize_text(value: object) -> str:
    """統一比對用字串：去空白 + 轉小寫（產業 / 技術 / email 比對都走這裡）。"""
    return str(value if value is not None else "").strip().lower()


def to_decimal(value: object, context: str) -> Decimal:
    """把設定或資料裡的數字轉成 Decimal。

    先轉字串再進 `Decimal` 是刻意的：`Decimal(0.1)` 會把 float 的二進位誤差
    原封不動帶進來，`Decimal("0.1")` 才是使用者在 YAML 裡真正寫下的那個數。
    """
    if isinstance(value, Decimal):
        return value
    if value is None or isinstance(value, bool):
        raise ScoringError(f"{context} 需要數字，收到 {value!r}")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ScoringError(f"{context} 無法轉成數字：{value!r}") from exc


def quantize_score(value: Decimal) -> Decimal:
    """分數固定兩位小數，避免權重正規化後出現無盡小數。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal) -> Decimal:
    """金額固定兩位小數（分）。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_int(value: object, context: str) -> int:
    """整數欄位轉換，格式錯誤即拋錯而非給預設值。"""
    if isinstance(value, bool) or value is None:
        raise ScoringError(f"{context} 需要整數，收到 {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ScoringError(f"{context} 無法轉成整數：{value!r}") from exc


def field_value(lead: dict, dotted: str) -> object:
    """支援 ``"trigger_event.type"`` 這種巢狀欄位查詢，找不到回 None。"""
    current: object = lead
    for part in str(dotted).split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def is_filled(value: object) -> bool:
    """欄位是否算「有填」。

    空字串、空清單、None 視為未填；`0` 與 `False` 視為有填——員工數 0 是
    荒謬但明確的資料，和「沒有這個欄位」是完全不同的兩件事，
    用 `if value:` 判斷會把兩者混為一談。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _clean_set(values: Sequence[str] | None) -> set[str]:
    """把設定清單轉成「去空白 + 轉小寫 + 去空值 + 去重」的集合。"""
    return {normalize_text(item) for item in (values or []) if normalize_text(item)}


def matches_entry(target: str, entries: Sequence[str]) -> str | None:
    """email 比對，回傳命中的清單項目（未命中回 None）。

    規則與 `_shared.autonomy.AutonomyGate._is_approved` 完全一致：
    `"@"` 開頭做網域比對，其餘一律當成完整 email 做精確相等。
    抑制名單若允許裸網域結尾比對，`ops@not-acme.com` 會被 `acme.com` 誤判命中——
    在抑制名單這一側，誤判方向是「該寄的沒寄」，比誤發安全，但仍會讓
    使用者以為名單失效而去放寬規則，不如一開始就用同一套明確規則。
    """
    value = normalize_text(target)
    if not value:
        return None
    for entry in entries:
        item = normalize_text(entry)
        if not item:
            continue
        if item.startswith("@"):
            if value.endswith(item):
                return item
        elif value == item:
            return item
    return None


def resolve_timezone(name: str, fallback_offset_hours: int = 8) -> tuple[tzinfo, str | None]:
    """取得時區物件，取不到就退回固定偏移，回傳 ``(tzinfo, 警告或 None)``。

    Windows 與精簡容器沒有系統 IANA 時區資料庫，`zoneinfo` 需要額外的 tzdata
    套件；本專案第三方依賴只允許 PyYAML 與 pytest，因此改為明確降級 + 警告，
    而不是讓整支程式在啟動時就掛掉。
    """
    tz_name = str(name or "").strip()
    if not tz_name:
        return dt_timezone(timedelta(hours=fallback_offset_hours)), None
    try:
        return ZoneInfo(tz_name), None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        warning = f"找不到時區資料庫項目 {tz_name!r}，已退回固定 UTC+{fallback_offset_hours} 偏移"
        return dt_timezone(timedelta(hours=fallback_offset_hours)), warning


def parse_iso(value: object, tz: tzinfo) -> datetime:
    """解析 ISO 8601 字串成帶時區的 datetime；無時區者視為 tz 當地時間。"""
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ScoringError(f"無法解析時間字串：{value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def sum_pipeline_value(entries: Iterable[dict], key: str = "estimated_acv_usd") -> Decimal:
    """加總管線價值（全程 Decimal）。缺值或格式錯誤的項目以 0 計並不中斷。"""
    total = ZERO
    for entry in entries:
        raw = entry.get(key)
        if not is_filled(raw):
            continue
        try:
            total += to_decimal(raw, f"{entry.get('lead_id')} 的 {key}")
        except ScoringError:
            continue
    return quantize_money(total)


# ---------------------------------------------------------------------------
# ICP 判準與評分
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IcpWeights:
    """四個評分面向的權重（總和 100）。"""

    industry: Decimal
    company_size: Decimal
    trigger_event: Decimal
    tech_stack: Decimal

    @classmethod
    def from_config(cls, raw: object) -> tuple["IcpWeights", list[str]]:
        """讀取權重設定，回傳 ``(權重, 警告清單)``；總和不是 100 就等比正規化。"""
        if not isinstance(raw, dict) or not raw:
            raise ScoringError("scoring.weights 必須是非空 mapping")
        values: dict[str, Decimal] = {}
        for name in WEIGHT_FIELDS:
            if name not in raw:
                raise ScoringError(f"scoring.weights 缺少必填項目 {name}")
            number = to_decimal(raw[name], f"scoring.weights.{name}")
            if number < ZERO:
                raise ScoringError(f"scoring.weights.{name} 不可為負數：{number}")
            values[name] = number
        total = sum(values.values(), ZERO)
        if total <= ZERO:
            raise ScoringError("scoring.weights 總和必須大於 0")
        warnings: list[str] = []
        if total != HUNDRED:
            warnings.append(
                f"scoring.weights 總和為 {total} 而非 100，已等比正規化；分數仍落在 0-100"
            )
            values = {name: value / total * HUNDRED for name, value in values.items()}
        return cls(**values), warnings

    def as_dict(self) -> dict[str, str]:
        """轉成 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {name: str(quantize_score(getattr(self, name))) for name in WEIGHT_FIELDS}


@dataclass(frozen=True)
class ScoreComponent:
    """單一評分面向的結果。`evidence` 同時是破冰信的素材。"""

    name: str
    weight: Decimal
    ratio: Decimal
    evidence: str

    @property
    def points(self) -> Decimal:
        """本面向實得分數。"""
        return quantize_score(self.weight * self.ratio)

    def as_dict(self) -> dict:
        """轉成 JSON-safe 結構。"""
        return {
            "name": self.name,
            "weight": str(quantize_score(self.weight)),
            "ratio": str(self.ratio),
            "points": str(self.points),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ScoreCard:
    """一筆線索的完整評分結果。"""

    lead_id: str
    total: Decimal
    band: str
    components: tuple[ScoreComponent, ...]
    completeness: Decimal
    missing_fields: tuple[str, ...]

    def as_dict(self) -> dict:
        """轉成 JSON-safe 結構。"""
        return {
            "lead_id": self.lead_id,
            "score": str(self.total),
            "band": self.band,
            "completeness": str(self.completeness),
            "missing_fields": list(self.missing_fields),
            "components": [item.as_dict() for item in self.components],
        }


@dataclass(frozen=True)
class IcpCriteria:
    """ICP 判準（產業 / 規模 / 觸發事件 / 技術特徵）。"""

    target_industries: frozenset[str]
    adjacent_industries: frozenset[str]
    ideal_min: int
    ideal_max: int
    acceptable_min: int
    acceptable_max: int
    preferred_tech: frozenset[str]
    trigger_events: dict[str, Decimal]
    trigger_recency_days: int

    @classmethod
    def from_config(cls, raw: object) -> "IcpCriteria":
        """從 config.yaml 的 icp 區段建立。"""
        if not isinstance(raw, dict) or not raw:
            raise ScoringError("config.yaml 的 icp 區段必須是非空 mapping")
        bounds = _size_bounds(raw.get("employee_range"))
        return cls(
            target_industries=_normalized_set(raw.get("target_industries"), "icp.target_industries"),
            adjacent_industries=_normalized_set(
                raw.get("adjacent_industries"), "icp.adjacent_industries", allow_empty=True
            ),
            ideal_min=bounds[0],
            ideal_max=bounds[1],
            acceptable_min=bounds[2],
            acceptable_max=bounds[3],
            preferred_tech=_normalized_set(raw.get("preferred_tech"), "icp.preferred_tech"),
            trigger_events=_trigger_table(raw.get("trigger_events")),
            trigger_recency_days=to_int(raw.get("trigger_recency_days", 45), "icp.trigger_recency_days"),
        )

    def industry_ratio(self, lead: dict) -> tuple[Decimal, str]:
        """產業匹配度：核心目標 1.0、鄰接產業 0.5、其餘 0。"""
        raw = lead.get("industry")
        value = normalize_text(raw)
        if not value:
            return ZERO, "未提供產業，無法判定匹配度"
        if value in self.target_industries:
            return ONE, f"核心目標產業（{raw}）"
        if value in self.adjacent_industries:
            return HALF, f"鄰接產業（{raw}），需人工再確認是否納入 ICP"
        return ZERO, f"非目標產業（{raw}）"

    def size_ratio(self, lead: dict) -> tuple[Decimal, str]:
        """公司規模：理想區間 1.0、可接受區間 0.5、其餘 0。"""
        raw = lead.get("employee_count")
        if not is_filled(raw):
            return ZERO, "未提供員工數"
        count = to_int(raw, f"{lead.get('id')} 的 employee_count")
        if self.ideal_min <= count <= self.ideal_max:
            return ONE, f"{count} 人，落在理想區間 {self.ideal_min}-{self.ideal_max}"
        if self.acceptable_min <= count <= self.acceptable_max:
            return HALF, f"{count} 人，僅落在可接受區間 {self.acceptable_min}-{self.acceptable_max}"
        return ZERO, f"{count} 人，落在 ICP 規模區間之外"

    def trigger_ratio(self, lead: dict, now: datetime) -> tuple[Decimal, str]:
        """觸發事件：型別權重 × 時效。超過 trigger_recency_days 一律歸零。"""
        event = lead.get("trigger_event")
        event = event if isinstance(event, dict) else {}
        event_type = normalize_text(event.get("type"))
        if not event_type:
            return ZERO, "近期無可用的觸發事件"
        ratio = self.trigger_events.get(event_type)
        if ratio is None:
            return ZERO, f"未知的觸發事件型別（{event_type}），不予計分"
        observed = event.get("observed_at")
        if not is_filled(observed):
            return ratio * HALF, f"{event_type} 缺少發生時間，時效無法確認，折半計分"
        age_days = max(0, (now - parse_iso(observed, now.tzinfo)).days)
        if age_days > self.trigger_recency_days:
            return ZERO, f"{event_type} 已過期（{age_days} 天 > {self.trigger_recency_days} 天）"
        return ratio, f"{event_type}，{age_days} 天前｜{str(event.get('headline') or '').strip()}"

    def tech_ratio(self, lead: dict) -> tuple[Decimal, str]:
        """技術特徵：命中 2 項以上 1.0、命中 1 項 0.5、其餘 0。"""
        raw = lead.get("tech_stack")
        items = [normalize_text(item) for item in raw] if isinstance(raw, (list, tuple)) else []
        items = [item for item in items if item]
        if not items:
            return ZERO, "未提供技術特徵"
        matches = sorted({item for item in items if item in self.preferred_tech})
        if len(matches) >= 2:
            return ONE, f"命中偏好技術 {', '.join(matches)}"
        if len(matches) == 1:
            return HALF, f"僅命中偏好技術 {matches[0]}"
        return ZERO, f"技術堆疊 {', '.join(items)} 未命中任何偏好項目"


def _normalized_set(raw: object, context: str, allow_empty: bool = False) -> frozenset[str]:
    """把清單設定轉成正規化後的集合。"""
    if raw is None and allow_empty:
        return frozenset()
    if not isinstance(raw, (list, tuple)):
        raise ScoringError(f"{context} 必須是清單，收到 {type(raw).__name__}")
    values = {normalize_text(item) for item in raw if normalize_text(item)}
    if not values and not allow_empty:
        raise ScoringError(f"{context} 不可為空")
    return frozenset(values)


def _size_bounds(raw: object) -> tuple[int, int, int, int]:
    """解析員工數區間，並確保可接受區間包住理想區間。"""
    if not isinstance(raw, dict):
        raise ScoringError("icp.employee_range 必須是 mapping")
    ideal_min = to_int(raw.get("ideal_min"), "icp.employee_range.ideal_min")
    ideal_max = to_int(raw.get("ideal_max"), "icp.employee_range.ideal_max")
    acceptable_min = to_int(raw.get("acceptable_min", ideal_min), "icp.employee_range.acceptable_min")
    acceptable_max = to_int(raw.get("acceptable_max", ideal_max), "icp.employee_range.acceptable_max")
    if ideal_min > ideal_max or acceptable_min > acceptable_max:
        raise ScoringError("icp.employee_range 的 min 不可大於 max")
    if acceptable_min > ideal_min or acceptable_max < ideal_max:
        raise ScoringError("icp.employee_range 的可接受區間必須完整包住理想區間")
    return ideal_min, ideal_max, acceptable_min, acceptable_max


def _trigger_table(raw: object) -> dict[str, Decimal]:
    """解析觸發事件權重表，比例必須落在 0-1。"""
    if not isinstance(raw, dict) or not raw:
        raise ScoringError("icp.trigger_events 必須是非空 mapping")
    table: dict[str, Decimal] = {}
    for key, value in raw.items():
        ratio = to_decimal(value, f"icp.trigger_events.{key}")
        if not ZERO <= ratio <= ONE:
            raise ScoringError(f"icp.trigger_events.{key} 必須落在 0-1，收到 {ratio}")
        table[normalize_text(key)] = ratio
    return table


class LeadScorer:
    """ICP 評分引擎：把公開資料換算成 0-100 分與一句可用的破冰理由。"""

    def __init__(
        self,
        criteria: IcpCriteria,
        weights: IcpWeights,
        hot_threshold: Decimal,
        warm_threshold: Decimal,
        min_score: Decimal,
        required_fields: Sequence[str],
        min_completeness: Decimal,
    ) -> None:
        if not ZERO <= warm_threshold <= hot_threshold <= HUNDRED:
            raise ScoringError(
                f"分數門檻必須滿足 0 <= warm({warm_threshold}) <= hot({hot_threshold}) <= 100"
            )
        if not ZERO <= min_completeness <= ONE:
            raise ScoringError(f"scoring.min_completeness 必須落在 0-1，收到 {min_completeness}")
        self._criteria = criteria
        self._weights = weights
        self._hot = hot_threshold
        self._warm = warm_threshold
        self._min_score = min_score
        self._required_fields = tuple(str(item) for item in (required_fields or ()))
        self._min_completeness = min_completeness

    @property
    def weights(self) -> IcpWeights:
        """目前使用的權重。"""
        return self._weights

    @property
    def min_score(self) -> Decimal:
        """進入外聯序列的最低分數。"""
        return self._min_score

    def completeness(self, lead: dict) -> tuple[Decimal, tuple[str, ...]]:
        """資料完整度 = 已填必要欄位 / 必要欄位總數，並回傳缺漏清單。"""
        if not self._required_fields:
            return ONE, ()
        missing = [name for name in self._required_fields if not is_filled(field_value(lead, name))]
        filled = len(self._required_fields) - len(missing)
        ratio = Decimal(filled) / Decimal(len(self._required_fields))
        return ratio.quantize(TWO_PLACES, rounding=ROUND_HALF_UP), tuple(missing)

    def band_of(self, total: Decimal) -> str:
        """依門檻判定分數帶。"""
        if total >= self._hot:
            return BAND_HOT
        if total >= self._warm:
            return BAND_WARM
        return BAND_COLD

    def _components(self, lead: dict, now: datetime) -> tuple[ScoreComponent, ...]:
        """算出四個面向的得分與證據。"""
        pairs = (
            ("industry", self._criteria.industry_ratio(lead)),
            ("company_size", self._criteria.size_ratio(lead)),
            ("trigger_event", self._criteria.trigger_ratio(lead, now)),
            ("tech_stack", self._criteria.tech_ratio(lead)),
        )
        return tuple(
            ScoreComponent(
                name=name,
                weight=getattr(self._weights, name),
                ratio=ratio,
                evidence=evidence,
            )
            for name, (ratio, evidence) in pairs
        )

    def score(self, lead: dict, now: datetime) -> ScoreCard:
        """對單一線索評分（純內部分析，不觸碰對方，READ_ONLY 安全）。"""
        components = self._components(lead, now)
        total = quantize_score(sum((item.points for item in components), ZERO))
        ratio, missing = self.completeness(lead)
        return ScoreCard(
            lead_id=str(lead.get("id") or "<unknown>"),
            total=total,
            band=self.band_of(total),
            components=components,
            completeness=ratio,
            missing_fields=missing,
        )

    def decide(self, card: ScoreCard, block: tuple[str, str] | None) -> tuple[str, str, str]:
        """依評分與法遵結果決定動作，回傳 ``(action, reason, detail)``。

        順序即優先權：法遵 > 資料完整度 > 分數門檻。
        法遵排最前面是因為「不能聯繫的人」再高分也不能寄，
        而資料完整度排在分數之前，是因為殘缺資料算出來的低分沒有參考價值——
        那是「我們不知道」，不是「這家公司不適合」。
        """
        if block is not None:
            return ACTION_BLOCK, block[0], block[1]
        if card.completeness < self._min_completeness:
            missing = "、".join(card.missing_fields) or "(無)"
            return (
                ACTION_ENRICH,
                ENRICH_INCOMPLETE,
                f"資料完整度 {card.completeness} 低於門檻 {self._min_completeness}；待補欄位：{missing}",
            )
        if card.total < self._min_score:
            return (
                ACTION_REJECT,
                REJECT_BELOW_THRESHOLD,
                f"ICP 分數 {card.total} 低於外聯門檻 {self._min_score}，不進序列",
            )
        return ACTION_OUTREACH, card.band, f"ICP 分數 {card.total}（{card.band}）"


def build_scorer(config: dict) -> tuple[LeadScorer, list[str]]:
    """從 config.yaml 組出評分引擎，回傳 ``(scorer, 警告清單)``。"""
    scoring = config.get("scoring")
    if not isinstance(scoring, dict) or not scoring:
        raise ScoringError("config.yaml 的 scoring 區段必須是非空 mapping")
    weights, warnings = IcpWeights.from_config(scoring.get("weights"))
    thresholds = scoring.get("thresholds")
    if not isinstance(thresholds, dict):
        raise ScoringError("scoring.thresholds 必須是 mapping")
    scorer = LeadScorer(
        criteria=IcpCriteria.from_config(config.get("icp")),
        weights=weights,
        hot_threshold=to_decimal(thresholds.get("hot", 75), "scoring.thresholds.hot"),
        warm_threshold=to_decimal(thresholds.get("warm", 50), "scoring.thresholds.warm"),
        min_score=to_decimal(scoring.get("min_score_for_outreach", 50), "scoring.min_score_for_outreach"),
        required_fields=scoring.get("required_fields") or (),
        min_completeness=to_decimal(scoring.get("min_completeness", 0), "scoring.min_completeness"),
    )
    return scorer, warnings


# ---------------------------------------------------------------------------
# 法遵閘門（⚠️ 實作補充，非原著內容）
# ---------------------------------------------------------------------------


class ComplianceGate:
    """冷開發信法遵閘門：來源合法性、抑制名單、同意法域、退訂與寄件人識別。

    原簡報完全沒有提到冷開發信的法律風險，這一整層是實作補充。
    但沒有它就不能上線：GDPR（歐盟）、CAN-SPAM（美國）與台灣個資法對
    「未經請求的商業電子郵件」都有明確要求，違反的代價遠高於這個模組的月費。
    """

    def __init__(
        self,
        allowed_source_bases: Sequence[str],
        suppression_entries: Sequence[str],
        consent_required_regions: Sequence[str],
        sender_identity: dict | None = None,
        require_unsubscribe: bool = True,
    ) -> None:
        self.forced_overrides: list[str] = []
        # 硬規則：不論外部傳什麼，一律 True。只留下紀錄供上層發 AMBER。
        if require_unsubscribe is not True:
            self.forced_overrides.append(
                f"compliance.require_unsubscribe 被設為 {require_unsubscribe!r}，已強制覆寫為 True"
            )
        self._require_unsubscribe = True
        self._allowed = _clean_set(allowed_source_bases)
        if not self._allowed:
            raise ScoringError(
                "compliance.allowed_source_bases 不可為空；沒有合法來源清單等同放行任何買來的名單"
            )
        self._suppression = sorted(_clean_set(suppression_entries))
        self._consent_regions = _clean_set(consent_required_regions)
        self._identity = {
            str(key): str(value if value is not None else "").strip()
            for key, value in (sender_identity or {}).items()
        }
        self.identity_gaps = [name for name in REQUIRED_IDENTITY_FIELDS if not self._is_identity_filled(name)]

    def _is_identity_filled(self, name: str) -> bool:
        """識別欄位是否可用。仍是未展開的 ``${ENV}`` 一律視為未填。"""
        value = self._identity.get(name, "")
        return bool(value) and not value.startswith("${")

    @property
    def is_unsubscribe_required(self) -> bool:
        """永遠為 True——這是本模組不可停用的硬規則。"""
        return self._require_unsubscribe

    @property
    def is_identity_complete(self) -> bool:
        """寄件人識別是否齊備；不齊備時上層必須把所有外聯降級為草稿。"""
        return not self.identity_gaps

    @property
    def suppression_size(self) -> int:
        """抑制名單筆數（供稽核報表用）。"""
        return len(self._suppression)

    def check(self, lead: dict) -> tuple[str, str] | None:
        """回傳 ``(阻擋原因, 說明)``；可外送則回 None。順序即優先權。"""
        email = normalize_text(lead.get("email"))
        if not email:
            return BLOCK_MISSING_EMAIL, "沒有聯絡 email，既無從外送也無從提供退訂管道"
        matched = matches_entry(email, self._suppression)
        if matched is not None:
            return BLOCK_SUPPRESSED, f"命中抑制名單項目 {matched!r}；曾退訂或要求刪除，永久不得再聯繫"
        basis = normalize_text(lead.get("source_basis"))
        if basis not in self._allowed:
            return (
                BLOCK_UNLAWFUL_SOURCE,
                f"聯絡資訊來源依據 {basis or '(未填)'} 不在合法清單內，不得作為冷開發對象",
            )
        region = normalize_text(lead.get("region"))
        if region in self._consent_regions and lead.get("has_opt_in") is not True:
            return (
                BLOCK_CONSENT_REQUIRED,
                f"{region.upper()} 屬事前同意（opt-in）法域，缺少可稽核的同意紀錄",
            )
        return None

    def footer(self, lead: dict) -> str:
        """每封外聯信必附的識別與退訂區塊。

        刻意由程式串接，而不是寫在提示詞裡交給 LLM 生成：法定必要資訊不能
        依賴模型「記得寫」，也不能讓模型有機會改寫退訂連結或郵寄地址。
        """
        source_note = str(lead.get("source_note") or lead.get("source_basis") or "").strip()
        return "\n".join(
            [
                "—",
                f"{self._identity.get('sender_name', '')}｜{self._identity.get('company_name', '')}",
                f"地址：{self._identity.get('postal_address', '')}",
                f"回覆信箱：{self._identity.get('reply_to_email', '')}",
                f"您的聯絡資訊來源：{source_note or '(未註明)'}",
                f"不想再收到這類信件，請點此退訂：{self._identity.get('unsubscribe_url', '')}",
            ]
        )


def build_compliance_gate(config: dict, suppression_entries: Sequence[str]) -> ComplianceGate:
    """從 config.yaml 的 compliance 區段組出法遵閘門。"""
    raw = config.get("compliance")
    if not isinstance(raw, dict) or not raw:
        raise ScoringError("config.yaml 的 compliance 區段必須是非空 mapping；對外發送模組不得省略")
    return ComplianceGate(
        allowed_source_bases=raw.get("allowed_source_bases") or [],
        suppression_entries=suppression_entries,
        consent_required_regions=raw.get("consent_required_regions") or [],
        sender_identity=raw.get("sender_identity") or {},
        require_unsubscribe=raw.get("require_unsubscribe", True),
    )


# ---------------------------------------------------------------------------
# 三階段外聯節奏
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CadenceStep:
    """外聯序列中的一段。`day` 為「線索進入管線後第幾天」，非絕對日期。"""

    day: int
    type: str
    prompt: str
    max_chars: int

    @classmethod
    def from_config(cls, raw: object) -> "CadenceStep":
        """從 config.yaml 的 cadence 項目建立，欄位缺失即拋錯而非給預設值。"""
        if not isinstance(raw, dict):
            raise ScoringError(f"cadence 項目必須是 mapping，收到 {type(raw).__name__}")
        try:
            day = int(raw["day"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ScoringError(f"cadence 項目缺少合法的 day：{raw!r}") from exc
        if day < 0:
            raise ScoringError(f"cadence 的 day 不可為負數，收到 {day}")
        step_type = str(raw.get("type") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not step_type or not prompt:
            raise ScoringError(f"cadence 項目缺少 type 或 prompt：{raw!r}")
        max_chars = to_int(raw.get("max_chars", 150), "cadence.max_chars")
        return cls(day=day, type=step_type, prompt=prompt, max_chars=max_chars)

    def as_dict(self) -> dict:
        """轉成可 JSON 序列化的 dict。"""
        return {"day": self.day, "type": self.type, "prompt": self.prompt, "max_chars": self.max_chars}


def build_cadence(raw_cadence: object) -> list[CadenceStep]:
    """把 config.yaml 的 cadence 區段轉成 CadenceStep 清單。"""
    if not isinstance(raw_cadence, (list, tuple)) or not raw_cadence:
        raise ScoringError("config.yaml 的 cadence 必須是非空清單")
    steps = [CadenceStep.from_config(item) for item in raw_cadence]
    days = [step.day for step in steps]
    if len(set(days)) != len(days):
        raise ScoringError(f"cadence 的 day 不可重複：{days}")
    return steps


class OutreachState:
    """外聯進度：記錄每個線索已送出哪幾段。

    `persist=False`（mock / dry-run 預設）時完全在記憶體運作，既不讀也不寫，
    讓 `--mock` 每次執行結果一模一樣，QA 可重複驗證。
    """

    def __init__(self, path: Path | None = None, persist: bool = False) -> None:
        self._path = Path(path) if path is not None else DEFAULT_STATE_PATH
        self._persist = bool(persist)
        self._data: dict[str, set[int]] = {}
        if self._persist:
            self._load()

    @property
    def path(self) -> Path:
        """狀態檔絕對路徑。"""
        return self._path

    @property
    def is_persisted(self) -> bool:
        """本次執行是否會把進度寫回磁碟。"""
        return self._persist

    def _load(self) -> None:
        """讀取既有狀態檔；檔案損毀要明確報錯，不可靜默當成空狀態。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScoringError(f"狀態檔無法讀取或解析：{self._path}") from exc
        if not isinstance(raw, dict):
            raise ScoringError(f"狀態檔格式錯誤（應為 object）：{self._path}")
        for lead_id, days in raw.items():
            self._data[str(lead_id)] = _coerce_days(days, f"狀態檔 {lead_id}")

    def sent_days(self, lead_id: str) -> set[int]:
        """回傳該線索已送出的 day 集合。"""
        return set(self._data.get(str(lead_id), set()))

    def mark_sent(self, lead_id: str, day: int) -> None:
        """標記某段已送出，`persist=True` 時立即寫回磁碟。"""
        self._data.setdefault(str(lead_id), set()).add(int(day))
        if self._persist:
            self.save()

    def save(self) -> None:
        """寫回狀態檔（含建立父目錄）。"""
        payload = {key: sorted(value) for key, value in self._data.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            raise ScoringError(f"狀態檔寫入失敗：{self._path}") from exc


def _coerce_days(raw: object, context: str) -> set[int]:
    """把 stages_sent 之類的清單轉成 int 集合，格式錯誤即拋錯。"""
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple, set)):
        raise ScoringError(f"{context} 必須是清單，收到 {type(raw).__name__}")
    try:
        return {int(item) for item in raw}
    except (TypeError, ValueError) as exc:
        raise ScoringError(f"{context} 含非整數項目：{raw!r}") from exc


class OutreachPlanner:
    """三階段外聯節奏狀態機：決定「這個線索此刻該寄哪一段，還是先別寄」。"""

    def __init__(
        self,
        steps: Sequence[CadenceStep],
        tz: tzinfo,
        state: OutreachState | None = None,
        compliance: ComplianceGate | None = None,
    ) -> None:
        if not steps:
            raise ScoringError("cadence 不可為空，至少要有一段外聯")
        self._steps = tuple(sorted(steps, key=lambda item: item.day))
        self._tz = tz
        self._state = state if state is not None else OutreachState(persist=False)
        self._compliance = compliance

    @property
    def steps(self) -> tuple[CadenceStep, ...]:
        """依 day 排序後的序列。"""
        return self._steps

    @property
    def state(self) -> OutreachState:
        """底層進度狀態。"""
        return self._state

    def sent_days(self, lead: dict) -> set[int]:
        """合併「CRM 帶來的 stages_sent」與「本機狀態檔」的已送出紀錄。"""
        lead_id = str(lead.get("id") or "")
        days = _coerce_days(lead.get("stages_sent"), f"{lead_id} 的 stages_sent")
        return days | self._state.sent_days(lead_id)

    def next_step(self, lead: dict) -> CadenceStep | None:
        """回傳下一段尚未送出的外聯；全部送完則回 None。"""
        sent = self.sent_days(lead)
        for step in self._steps:
            if step.day not in sent:
                return step
        return None

    def due_at(self, lead: dict, step: CadenceStep) -> datetime:
        """到期時間 = 線索進入管線的時間 + N 天。

        用相對天數而非絕對日期，序列因此可以在任何時間點被重新排程而不錯亂。
        """
        raw = lead.get("discovered_at")
        if not is_filled(raw):
            raise ScoringError(
                f"{lead.get('id')!r} 缺少 discovered_at，無法計算 Day {step.day} 到期時間"
            )
        return parse_iso(raw, self._tz) + timedelta(days=step.day)

    def plan(self, lead: dict, now: datetime) -> dict:
        """判定此刻該寄哪一段；不該寄時回傳原因。"""
        step = self.next_step(lead)
        if step is None:
            return _plan_result(None, HALT_CADENCE_COMPLETE, "三階段外聯已全部送出", None)
        try:
            due = self.due_at(lead, step)
        except ScoringError as exc:
            return _plan_result(None, HALT_BAD_DATA, str(exc), None)
        if now < due:
            return _plan_result(
                None, HALT_NOT_DUE, f"Day {step.day} 於 {due.isoformat()} 才到期", due
            )
        return _plan_result(step, f"stage_day_{step.day}", f"到期時間 {due.isoformat()}", due)

    def assert_can_send(self, lead: dict) -> None:
        """**每一次實際發送前**都必須呼叫的最後一道閘門。

        排程判定與實際送出之間隔著 LLM 生成時間（數十秒到數十分鐘），
        對方完全可能在這段空窗按下退訂。抑制名單只在排程時查一次是不夠的。
        """
        if self._compliance is None:
            return
        blocked = self._compliance.check(lead)
        if blocked is not None:
            raise ComplianceBlocked(str(lead.get("id") or "<unknown>"), blocked[0], blocked[1])

    def mark_sent(self, lead: dict, step: CadenceStep) -> None:
        """記錄某段已送出，避免重複發送。"""
        self._state.mark_sent(str(lead.get("id") or ""), step.day)


def _plan_result(
    step: CadenceStep | None,
    reason: str,
    detail: str,
    due: datetime | None,
) -> dict:
    """組出排程判定結果。"""
    return {
        "step": step,
        "reason": reason,
        "detail": detail,
        "due_at": due.isoformat() if due is not None else None,
    }
