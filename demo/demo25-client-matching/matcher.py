"""物件 × 買方條件的加權比對引擎、公平住房法遵閘門、通知去重狀態檔。

三件事在這裡：

1. **法遵閘門**（`assert_criteria_compliant`）：房地產推薦在多國受公平住房法
   （Fair Housing Act / Equality Act）規範，媒合條件不得直接或間接使用種族、
   宗教、家庭狀況、身心障礙等受保護特徵。本模組採「白名單 + 受保護詞根偵測」
   雙重把關，命中即**拒絕執行並拋錯**，絕不靜默略過該欄位——
   靜默略過會讓違規條件看起來「有生效」，是比當場失敗更糟的結果。
2. **加權評分**（`score_match`）：Hard Match 權重 3x、Soft Match 權重 1x，
   分數 = 命中權重 ÷ 可得權重 × 100（規格 apxG_p12）。
3. **通知去重與條件變更重比對**（`apply_criteria_change` / `mark_notified`）：
   同一買方對同一物件只通知一次；買方條件一改變，該買方的去重紀錄整批清空，
   讓既有物件重新走一次比對。

金額一律 `decimal.Decimal`：房價是拿去談判的數字，float 的
`0.1 + 0.2 != 0.3` 誤差在客戶面前無法辯解。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

STATE_VERSION = 1
SCORE_QUANT = Decimal("0.01")

TIER_PERFECT = "perfect"
TIER_STRONG = "strong"
TIER_BELOW = "below"

# 買方 ID 與物件 ID 用這個字元組成去重鍵，因此 ID 本身不得包含它
KEY_SEPARATOR = "|"


class MatchingError(ValueError):
    """比對資料不合法（缺欄位、型別錯誤、未實作的條件欄位等）"""


class ComplianceError(MatchingError):
    """媒合條件違反公平住房法遵規則（受保護特徵或白名單外欄位）"""


# ---------------------------------------------------------------------------
# 法遵：受保護特徵詞根 + 客觀條件白名單
# ---------------------------------------------------------------------------

# 詞根比對（子字串），涵蓋英文欄位名與中文欄位名兩種寫法。
# 刻意用「詞根」而非完整欄位名：buyer_race / race_preference / preferred_race
# 都要能擋下來。攻擊面是欄位命名的無限變體，白名單才是真正的防線，
# 本清單負責在「白名單被人放寬」時提供第二道保險。
PROTECTED_ATTRIBUTE_TOKENS: tuple[str, ...] = (
    "race", "racial", "ethnic", "colour", "skin_",
    "religio", "faith", "creed", "church", "mosque", "synagogue",
    "national_origin", "nationality", "citizenship", "ancestry", "immigrant", "immigration",
    "gender", "sexual_orientation", "lgbt", "pregnan",
    "marital", "spouse", "widow",
    "familial", "family_status", "children", "kids", "dependents",
    "disab", "handicap", "wheelchair", "impair", "mental_health", "medical",
    "buyer_age", "applicant_age", "age_group", "age_range", "elderly", "senior_only",
    "source_of_income", "welfare", "housing_voucher", "section_8", "section8",
    "種族", "族裔", "膚色", "宗教", "信仰", "國籍", "移民",
    "性別", "性向", "婚姻", "配偶", "懷孕",
    "家庭狀況", "子女", "小孩", "扶養",
    "身心障礙", "殘障", "行動不便", "精神", "病史",
    "買方年齡", "年齡層", "補助", "低收",
)

# 只有客觀的物件條件可以進入媒合。新增欄位前必須先確認它不是受保護特徵的代理變數
# （例：郵遞區號本身是客觀的，但若用來排除特定族裔社區即構成 redlining，
#  故 README 要求 preferred_postcodes 只能由買方本人填寫、不得由業務代填）。
DEFAULT_ALLOWED_CRITERIA_FIELDS: tuple[str, ...] = (
    "max_price",
    "min_price",
    "min_bedrooms",
    "max_bedrooms",
    "min_bathrooms",
    "property_type",
    "preferred_postcodes",
    "required_features",
    "min_floor_area",
    "max_property_age_years",
)


def normalise_field_name(name: Any) -> str:
    """欄位名正規化：小寫、空白與連字號一律轉底線，避免用大小寫繞過白名單"""
    return re.sub(r"[\s\-]+", "_", str(name).strip().lower())


def detect_protected_fields(field_names: Iterable[Any]) -> list[str]:
    """回傳疑似含受保護特徵的欄位名清單（空清單代表通過）"""
    flagged: list[str] = []
    for raw in field_names:
        normalised = normalise_field_name(raw)
        if any(token in normalised for token in PROTECTED_ATTRIBUTE_TOKENS):
            flagged.append(str(raw))
    return flagged


def assert_criteria_compliant(
    field_names: Iterable[Any],
    allowed_fields: Iterable[Any],
    *,
    source: str,
) -> None:
    """法遵雙重把關；任一關卡未過就拋 ComplianceError，呼叫端必須中止執行。"""
    names = [str(name) for name in field_names]
    protected = detect_protected_fields(names)
    if protected:
        raise ComplianceError(
            f"{source} 含疑似受保護特徵欄位 {protected}："
            "公平住房法禁止以種族／宗教／家庭狀況／身心障礙等特徵做媒合，已拒絕執行"
        )
    allowed = {normalise_field_name(name) for name in allowed_fields}
    unknown = [name for name in names if normalise_field_name(name) not in allowed]
    if unknown:
        raise ComplianceError(
            f"{source} 含白名單外的欄位 {unknown}："
            f"僅允許客觀條件 {sorted(allowed)}；"
            "確認該欄位不含受保護特徵後，才可加入 config.yaml 的 allowed_criteria_fields"
        )


# ---------------------------------------------------------------------------
# 型別轉換
# ---------------------------------------------------------------------------

def to_money(raw: Any) -> Decimal:
    """把設定檔／資料源中的金額或面積轉成 Decimal（float 先過 str 避免二進位誤差）"""
    if isinstance(raw, bool):
        # bool 是 int 的子類，不先擋掉會讓 True 變成 1
        raise MatchingError(f"數值不接受布林值：{raw!r}")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    elif isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except InvalidOperation as exc:
            raise MatchingError(f"無法解析數值字串：{raw!r}") from exc
    else:
        raise MatchingError(f"不支援的數值型別：{type(raw).__name__}")
    if not value.is_finite() or value < 0:
        raise MatchingError(f"數值必須是非負的有限值，收到 {raw!r}")
    return value


def to_count(raw: Any, *, field_name: str) -> int:
    """把房間數／年份等整數欄位轉成 int，明確拒絕布林與非數字"""
    if isinstance(raw, bool):
        raise MatchingError(f"{field_name} 不接受布林值：{raw!r}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise MatchingError(f"{field_name} 必須是整數，收到 {raw!r}") from exc
    if value < 0:
        raise MatchingError(f"{field_name} 必須是非負整數，收到 {value}")
    return value


def to_text_set(raw: Any) -> frozenset[str]:
    """把字串或字串陣列統一成集合（單一字串視為只有一個元素的清單）"""
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        items: list[Any] = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        items = list(raw)
    else:
        raise MatchingError(f"必須是字串或字串陣列，收到 {type(raw).__name__}")
    return frozenset(str(item).strip() for item in items if str(item).strip())


def parse_timestamp(raw: Any, *, field_name: str) -> datetime:
    """解析 ISO 8601 時間字串；無時區資訊者一律視為 UTC，避免跨機器時差偏移"""
    if isinstance(raw, datetime):
        stamp = raw
    else:
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise MatchingError(f"{field_name} 不是合法的 ISO 8601 時間：{raw!r}") from exc
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------

def _require(raw: dict[str, Any], key: str, owner: str) -> Any:
    """取必填欄位，缺了就明確指出是哪一筆資料的哪個欄位"""
    if key not in raw or raw[key] is None:
        raise MatchingError(f"{owner} 缺少必填欄位 {key!r}")
    return raw[key]


def _require_id(raw: dict[str, Any], key: str) -> str:
    """ID 必填且不得含去重鍵分隔字元，否則狀態檔的鍵會被切錯"""
    value = str(_require(raw, key, "資料列")).strip()
    if not value:
        raise MatchingError(f"{key} 不可為空字串")
    if KEY_SEPARATOR in value:
        raise MatchingError(f"{key}={value!r} 不可包含 {KEY_SEPARATOR!r}（去重鍵的分隔字元）")
    return value


@dataclass(frozen=True)
class Listing:
    """單一物件（Property Portal Webhook 推來的那一筆）"""

    listing_id: str
    title: str
    price: Decimal
    bedrooms: int
    bathrooms: int
    property_type: str
    postcode: str
    features: frozenset[str]
    floor_area: Decimal
    property_age_years: int
    listed_at: datetime
    enquiries_last_7_days: int
    days_on_market: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Listing":
        """由 JSON 物件建立；缺欄位或型別錯誤一律當場拋錯，不用預設值掩蓋"""
        listing_id = _require_id(raw, "listing_id")
        return cls(
            listing_id=listing_id,
            title=str(raw.get("title", listing_id)),
            price=to_money(_require(raw, "price", listing_id)),
            bedrooms=to_count(_require(raw, "bedrooms", listing_id), field_name="bedrooms"),
            bathrooms=to_count(raw.get("bathrooms", 0), field_name="bathrooms"),
            property_type=str(_require(raw, "property_type", listing_id)).strip(),
            postcode=str(_require(raw, "postcode", listing_id)).strip(),
            features=to_text_set(raw.get("features")),
            floor_area=to_money(raw.get("floor_area", 0)),
            property_age_years=to_count(
                raw.get("property_age_years", 0), field_name="property_age_years"
            ),
            listed_at=parse_timestamp(
                _require(raw, "listed_at", listing_id), field_name="listed_at"
            ),
            enquiries_last_7_days=to_count(
                raw.get("enquiries_last_7_days", 0), field_name="enquiries_last_7_days"
            ),
            days_on_market=to_count(raw.get("days_on_market", 0), field_name="days_on_market"),
        )


@dataclass(frozen=True)
class Buyer:
    """單一註冊買方／批發商，criteria 只允許客觀條件（見法遵閘門）"""

    buyer_id: str
    name: str
    email: str
    criteria: dict[str, Any]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Buyer":
        """由 JSON 物件建立；criteria 必須是 dict，否則後續白名單檢查會被繞過"""
        buyer_id = _require_id(raw, "buyer_id")
        criteria = raw.get("criteria")
        if not isinstance(criteria, dict):
            raise MatchingError(
                f"買方 {buyer_id} 的 criteria 必須是物件，收到 {type(criteria).__name__}"
            )
        return cls(
            buyer_id=buyer_id,
            name=str(raw.get("name", buyer_id)),
            email=str(_require(raw, "email", buyer_id)).strip(),
            criteria=dict(criteria),
        )


@dataclass(frozen=True)
class MatchingCriteria:
    """來自 config.yaml 的比對參數（規格 apxG_p12 的 matching_criteria 區塊）"""

    hard_match_fields: tuple[str, ...]
    soft_match_fields: tuple[str, ...]
    match_score_threshold: Decimal
    hard_match_weight: int
    soft_match_weight: int
    perfect_score_min: Decimal
    strong_score_min: Decimal
    is_all_hard_matches_required: bool

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "MatchingCriteria":
        """讀 config 的 matching 區塊；權重必須為正整數，否則分母會歸零"""
        hard_weight = to_count(raw.get("hard_match_weight", 3), field_name="hard_match_weight")
        soft_weight = to_count(raw.get("soft_match_weight", 1), field_name="soft_match_weight")
        if hard_weight < 1 or soft_weight < 1:
            raise MatchingError("hard_match_weight 與 soft_match_weight 都必須 >= 1")
        return cls(
            hard_match_fields=tuple(str(f) for f in raw.get("hard_match_fields") or ()),
            soft_match_fields=tuple(str(f) for f in raw.get("soft_match_fields") or ()),
            match_score_threshold=to_money(raw.get("match_score_threshold", 80)),
            hard_match_weight=hard_weight,
            soft_match_weight=soft_weight,
            perfect_score_min=to_money(raw.get("perfect_score_min", 90)),
            strong_score_min=to_money(raw.get("strong_score_min", 75)),
            is_all_hard_matches_required=bool(raw.get("require_all_hard_matches", False)),
        )

    @property
    def all_fields(self) -> tuple[str, ...]:
        """Hard + Soft 全部欄位，供法遵閘門一次檢查"""
        return self.hard_match_fields + self.soft_match_fields


@dataclass(frozen=True)
class MatchResult:
    """單一「物件 × 買方」的比對結果"""

    listing_id: str
    buyer_id: str
    score: Decimal
    tier: str
    is_pushable: bool
    is_high_priority: bool
    hard_hits: tuple[str, ...]
    hard_misses: tuple[str, ...]
    soft_hits: tuple[str, ...]
    soft_misses: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """轉成 JSON 可序列化形狀；分數保留字串以免下游又退回 float"""
        return {
            "listing_id": self.listing_id,
            "buyer_id": self.buyer_id,
            "score": str(self.score),
            "tier": self.tier,
            "is_pushable": self.is_pushable,
            "is_high_priority": self.is_high_priority,
            "hard_hits": list(self.hard_hits),
            "hard_misses": list(self.hard_misses),
            "soft_hits": list(self.soft_hits),
            "soft_misses": list(self.soft_misses),
        }


# ---------------------------------------------------------------------------
# 條件評估
# ---------------------------------------------------------------------------

FieldEvaluator = Callable[[Listing, Any], bool]

# 每個白名單欄位都必須在此有對應實作；沒有實作就當場報錯，
# 而不是「查無此欄位 → 視為不命中」——後者會讓打錯字的條件靜靜地永遠不命中。
_FIELD_EVALUATORS: dict[str, FieldEvaluator] = {
    "max_price": lambda listing, value: listing.price <= to_money(value),
    "min_price": lambda listing, value: listing.price >= to_money(value),
    "min_bedrooms": lambda listing, value: listing.bedrooms
    >= to_count(value, field_name="min_bedrooms"),
    "max_bedrooms": lambda listing, value: listing.bedrooms
    <= to_count(value, field_name="max_bedrooms"),
    "min_bathrooms": lambda listing, value: listing.bathrooms
    >= to_count(value, field_name="min_bathrooms"),
    "property_type": lambda listing, value: listing.property_type in to_text_set(value),
    "preferred_postcodes": lambda listing, value: listing.postcode in to_text_set(value),
    "required_features": lambda listing, value: to_text_set(value) <= listing.features,
    "min_floor_area": lambda listing, value: listing.floor_area >= to_money(value),
    "max_property_age_years": lambda listing, value: listing.property_age_years
    <= to_count(value, field_name="max_property_age_years"),
}


def evaluate_field(field_name: str, listing: Listing, value: Any) -> bool:
    """評估單一條件欄位是否命中"""
    evaluator = _FIELD_EVALUATORS.get(normalise_field_name(field_name))
    if evaluator is None:
        raise MatchingError(
            f"條件欄位 {field_name!r} 沒有對應的比對邏輯，"
            f"可用欄位：{sorted(_FIELD_EVALUATORS)}"
        )
    return evaluator(listing, value)


def _weigh_fields(
    fields: Iterable[str], weight: int, listing: Listing, buyer: Buyer
) -> tuple[list[str], list[str], int, int]:
    """逐欄評估並累計權重，回傳 (命中欄位, 未命中欄位, 已得權重, 可得權重)。

    買方沒填的欄位不計入分母：沒填代表「不在意」，若計入會讓填得少的買方無故被扣分。
    """
    hits: list[str] = []
    misses: list[str] = []
    earned = 0
    possible = 0
    for name in fields:
        if name not in buyer.criteria:
            continue
        possible += weight
        if evaluate_field(name, listing, buyer.criteria[name]):
            hits.append(name)
            earned += weight
        else:
            misses.append(name)
    return hits, misses, earned, possible


def tier_for(score: Decimal, criteria: MatchingCriteria) -> str:
    """分級推播的三段（規格 apxG_p12：Perfect 90+ / Strong 75-89）"""
    if score >= criteria.perfect_score_min:
        return TIER_PERFECT
    if score >= criteria.strong_score_min:
        return TIER_STRONG
    return TIER_BELOW


def score_match(listing: Listing, buyer: Buyer, criteria: MatchingCriteria) -> MatchResult:
    """加權評分：分數 = 命中權重 ÷ 可得權重 × 100，Hard 3x / Soft 1x"""
    hard_hits, hard_misses, hard_earned, hard_possible = _weigh_fields(
        criteria.hard_match_fields, criteria.hard_match_weight, listing, buyer
    )
    soft_hits, soft_misses, soft_earned, soft_possible = _weigh_fields(
        criteria.soft_match_fields, criteria.soft_match_weight, listing, buyer
    )
    possible = hard_possible + soft_possible
    # 買方一個有效條件都沒填 → 分數 0 且不推播，避免「無條件 = 全部命中」的荒謬結果
    score = (
        Decimal(0)
        if possible == 0
        else (Decimal(hard_earned + soft_earned) * 100 / Decimal(possible)).quantize(SCORE_QUANT)
    )
    is_hard_gate_open = not criteria.is_all_hard_matches_required or not hard_misses
    return MatchResult(
        listing_id=listing.listing_id,
        buyer_id=buyer.buyer_id,
        score=score,
        tier=tier_for(score, criteria),
        is_pushable=possible > 0 and score >= criteria.match_score_threshold and is_hard_gate_open,
        is_high_priority=possible > 0 and score >= criteria.perfect_score_min,
        hard_hits=tuple(hard_hits),
        hard_misses=tuple(hard_misses),
        soft_hits=tuple(soft_hits),
        soft_misses=tuple(soft_misses),
    )


def minutes_since_listed(listing: Listing, now: datetime) -> int:
    """物件上架至今經過的分鐘數（上架時間在未來時回 0，不倒扣）"""
    elapsed = (now - listing.listed_at).total_seconds()
    return max(0, int(elapsed // 60))


def is_low_enquiry(listing: Listing, *, max_enquiries: int, min_days_on_market: int) -> bool:
    """低詢問度物件判定（分支 B：觸發 Vendor Pricing Pack 降價談判包）"""
    return (
        listing.enquiries_last_7_days <= max_enquiries
        and listing.days_on_market >= min_days_on_market
    )


# ---------------------------------------------------------------------------
# 去重狀態檔
# ---------------------------------------------------------------------------

def criteria_fingerprint(buyer: Buyer) -> str:
    """買方條件的 SHA-256 指紋；條件一改指紋就變，用來觸發既有物件重新比對"""
    payload = json.dumps(buyer.criteria, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def notification_key(buyer_id: str, listing_id: str) -> str:
    """去重鍵：一個買方對一個物件只通知一次"""
    return f"{buyer_id}{KEY_SEPARATOR}{listing_id}"


def empty_state() -> dict[str, Any]:
    """全新的狀態檔骨架"""
    return {"version": STATE_VERSION, "notified": {}, "criteria_fingerprints": {}}


def load_state(path: Path) -> dict[str, Any]:
    """讀取去重狀態檔；不存在視為首次執行，損毀則明確拋錯（不可靜默重置）。

    靜默重置會讓所有買方在同一天被重複通知一輪——這正是本模組要防的事。
    """
    if not path.is_file():
        return empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatchingError(f"去重狀態檔無法讀取（{path}）：{exc}") from exc
    if not isinstance(raw, dict):
        raise MatchingError(f"去重狀態檔格式錯誤（{path}）：最外層必須是物件")
    state = empty_state()
    state["version"] = raw.get("version", STATE_VERSION)
    for key in ("notified", "criteria_fingerprints"):
        value = raw.get(key, {})
        if not isinstance(value, dict):
            raise MatchingError(f"去重狀態檔的 {key} 必須是物件（{path}）")
        state[key] = dict(value)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    """寫回去重狀態檔（先寫暫存檔再改名，避免中途中斷留下半截 JSON）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        temp_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        temp_path.replace(path)
    except OSError as exc:
        raise MatchingError(f"去重狀態檔寫入失敗（{path}）：{exc}") from exc


def is_already_notified(state: dict[str, Any], buyer_id: str, listing_id: str) -> bool:
    """該買方是否已經收過這個物件的推薦"""
    return notification_key(buyer_id, listing_id) in state.get("notified", {})


def mark_notified(
    state: dict[str, Any],
    buyer_id: str,
    listing_id: str,
    *,
    score: Decimal,
    tier: str,
    at: datetime,
) -> None:
    """記錄一次已送出的通知，供下次執行去重"""
    state.setdefault("notified", {})[notification_key(buyer_id, listing_id)] = {
        "notified_at": at.isoformat(),
        "score": str(score),
        "tier": tier,
    }


def apply_criteria_change(state: dict[str, Any], buyer: Buyer) -> bool:
    """條件變更偵測：指紋不同就清空該買方的去重紀錄，讓既有物件重新比對。

    回傳 True 代表本次偵測到條件變更。首次見到的買方（無舊指紋）不算變更，
    否則新買方會被誤判成「條件剛改」而在稽核軌跡留下假事件。
    """
    fingerprints = state.setdefault("criteria_fingerprints", {})
    current = criteria_fingerprint(buyer)
    previous = fingerprints.get(buyer.buyer_id)
    fingerprints[buyer.buyer_id] = current
    if previous is None or previous == current:
        return False
    notified = state.setdefault("notified", {})
    prefix = f"{buyer.buyer_id}{KEY_SEPARATOR}"
    for key in [k for k in notified if k.startswith(prefix)]:
        del notified[key]
    return True
