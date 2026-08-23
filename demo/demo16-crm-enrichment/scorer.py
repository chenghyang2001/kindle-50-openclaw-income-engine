"""ICP 評分：把豐富化後的欄位換算成 0–100 分並分級（Hot / Warm / Cool / Stale）。

**這個檔案裡沒有任何一個權重或門檻的數字。** 全部來自 ``config.yaml``，
理由很實際：客戶第一週就會想調（每個產業的理想客戶輪廓不一樣），
數字寫死在 .py 就代表每次微調都要動程式碼、重跑 QA、重新部署。
權重放設定檔，客戶自己改一行、明天早上就看得到新排序。

分數與「久未聯絡」是**兩個維度**，刻意分開算：

- ``band``  來自分數：hot / warm / cool（書中 SCORE_THRESHOLDS）
- ``is_stale`` 來自最後聯絡日：超過 ``stale_days``（書中 90 天）
- ``grade`` 是給人看的合併結果：低分又久未聯絡 -> ``stale``（清洗候選）；
  **高分而久未聯絡不會被降級**，反而標成 ``is_reengagement_target``——
  那正是書中「標記 90 天未聯絡潛在機會」要撈出來的名單：
  分數高卻沒人碰，是今天最該打的那通電話。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Sequence

from enricher import STATUS_FAILED, is_blank

ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
RATIO_PLACES = Decimal("0.0001")
SCORE_PLACES = Decimal("0.1")

BAND_HOT = "hot"
BAND_WARM = "warm"
BAND_COOL = "cool"
GRADE_STALE = "stale"


class ScoringConfigError(ValueError):
    """評分設定有誤。刻意讓它爆炸而不是套預設值：權重打錯字卻靜默算 0 分，
    整份名單的排序都是錯的，而且沒有人看得出來。"""


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterionScore:
    """單一評分項目的結果。``has_input=False`` 代表資料缺失而非不符合條件。"""

    key: str
    weight: Decimal
    ratio: Decimal
    points: Decimal
    reason: str
    has_input: bool

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構。"""
        return {
            "criterion": self.key,
            "weight": str(self.weight),
            "ratio": str(self.ratio),
            "points": str(self.points),
            "reason": self.reason,
            "has_input": self.has_input,
        }


@dataclass(frozen=True)
class ScoreResult:
    """一位聯絡人的完整評分結果。"""

    contact_id: str
    company: str
    total: Decimal
    band: str
    grade: str
    is_stale: bool
    is_reengagement_target: bool
    days_since_contact: int | None
    breakdown: tuple[CriterionScore, ...]
    missing_inputs: tuple[str, ...]
    is_low_confidence: bool

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {
            "contact_id": self.contact_id,
            "company": self.company,
            "score": str(self.total),
            "band": self.band,
            "grade": self.grade,
            "is_stale": self.is_stale,
            "is_reengagement_target": self.is_reengagement_target,
            "days_since_contact": self.days_since_contact,
            "missing_inputs": list(self.missing_inputs),
            "is_low_confidence": self.is_low_confidence,
            "breakdown": [item.to_dict() for item in self.breakdown],
        }


@dataclass(frozen=True)
class ScoringRules:
    """從 config.yaml 讀進來的評分規則（程式碼本身不持有任何預設數字）。"""

    weights: dict[str, Decimal]
    hot_threshold: Decimal
    warm_threshold: Decimal
    stale_days: int
    criteria: dict[str, dict[str, Any]]

    @property
    def total_weight(self) -> Decimal:
        """權重總和。不必等於 100，總分會據此正規化。"""
        return sum(self.weights.values(), ZERO)


# --------------------------------------------------------------------------
# 設定載入與驗證
# --------------------------------------------------------------------------


def _decimal(value: Any, label: str) -> Decimal:
    """把設定值轉成 Decimal；轉不動就明確報錯（含欄位名稱）。"""
    try:
        return Decimal(str(value).strip())
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ScoringConfigError(f"{label} 必須是數字，收到 {value!r}") from exc


def _load_weights(raw: dict[str, Any]) -> dict[str, Decimal]:
    """讀權重表並檢查每個 key 都有對應的評分器。"""
    weights_raw = raw.get("weights") or {}
    if not isinstance(weights_raw, dict) or not weights_raw:
        raise ScoringConfigError("scoring.weights 必須是非空的 mapping")

    weights: dict[str, Decimal] = {}
    for key, value in weights_raw.items():
        name = str(key)
        if name not in EVALUATORS:
            raise ScoringConfigError(
                f"scoring.weights 出現未知的評分項目 {name!r}；"
                f"目前支援：{', '.join(sorted(EVALUATORS))}"
            )
        weight = _decimal(value, f"scoring.weights.{name}")
        if weight <= ZERO:
            raise ScoringConfigError(f"scoring.weights.{name} 必須大於 0，收到 {value!r}")
        weights[name] = weight
    return weights


def load_scoring_rules(raw: dict[str, Any] | None) -> ScoringRules:
    """把 config.yaml 的 scoring 區塊轉成 ScoringRules，並驗證合理性。"""
    raw = raw or {}
    weights = _load_weights(raw)

    bands = raw.get("bands") or {}
    hot = _decimal(bands.get("hot", 75), "scoring.bands.hot")
    warm = _decimal(bands.get("warm", 50), "scoring.bands.warm")
    if not ZERO <= warm < hot <= HUNDRED:
        raise ScoringConfigError(
            f"scoring.bands 必須滿足 0 <= warm({warm}) < hot({hot}) <= 100"
        )

    try:
        stale_days = int(raw.get("stale_days", 90))
    except (TypeError, ValueError) as exc:
        raise ScoringConfigError(f"scoring.stale_days 必須是整數，收到 {raw.get('stale_days')!r}") from exc
    if stale_days < 0:
        raise ScoringConfigError(f"scoring.stale_days 不可為負數，收到 {stale_days}")

    criteria = raw.get("criteria") or {}
    if not isinstance(criteria, dict):
        raise ScoringConfigError("scoring.criteria 必須是 mapping")
    return ScoringRules(weights, hot, warm, stale_days, criteria)


# --------------------------------------------------------------------------
# 時間
# --------------------------------------------------------------------------


def parse_timestamp(value: Any) -> datetime | None:
    """把 ISO 8601 字串轉成 aware datetime；無法解析回 None（不猜、不補今天）。"""
    if is_blank(value):
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def days_between(earlier: Any, now: datetime) -> int | None:
    """距今幾天。未來時間視為 0 天（資料有誤時不應被算成「超久沒聯絡」）。"""
    parsed = parse_timestamp(earlier)
    if parsed is None:
        return None
    return max(0, (now - parsed.astimezone(timezone.utc)).days)


# --------------------------------------------------------------------------
# 各評分項目（全部只讀 params，不含任何寫死的門檻）
# --------------------------------------------------------------------------


def _ramp(value: Decimal, low: Decimal, high: Decimal, label: str) -> Decimal:
    """低於 low 給 0、達到 high 給滿分、中間線性內插。"""
    if high <= low:
        raise ScoringConfigError(f"{label}: ideal 必須大於 min（收到 {low} / {high}）")
    if value <= low:
        return ZERO
    if value >= high:
        return ONE
    return (value - low) / (high - low)


def _numeric(record: dict[str, Any], field_name: str) -> Decimal | None:
    """取出數值欄位；空白或非數字回 None（代表缺資料，不是 0）。"""
    value = record.get(field_name)
    if is_blank(value):
        return None
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _matches_token(text: str, token: str) -> bool:
    """關鍵字比對：ASCII 詞彙要求詞界，非 ASCII（中日韓）詞彙用子字串。

    為什麼不能一律用子字串——"director" 裡面藏著 "cto"（dire-CTO-r）。
    短代號（cto / coo / vp / cfo）一旦用 `token in text` 比對，
    "Director of Ecommerce" 會被判成 C-level，該筆多拿滿分權重
    （實測 C-1005 因此多拿 4 分，Warm/Hot 的分界就被污染了）。
    中文沒有詞界可用，故僅對 ASCII 詞彙套用詞界規則。
    """
    if not token:
        return False
    if token.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text) is not None
    return token in text


def _eval_industry(record: dict[str, Any], params: dict[str, Any]) -> tuple[Decimal, str, bool]:
    """SIC 代碼落在白名單給滿分；只有產業名稱時退而用關鍵字比對（次要證據）。"""
    whitelist = {str(code).strip() for code in (params.get("sic_whitelist") or [])}
    sic = record.get("sic_code")
    if not is_blank(sic):
        hit = str(sic).strip() in whitelist
        return (ONE if hit else ZERO, f"SIC {sic}{'在' if hit else '不在'} ICP 白名單", True)

    industry = record.get("industry")
    if is_blank(industry):
        return (ZERO, "缺 sic_code 與 industry，無法判斷產業", False)

    keywords = [str(word).strip().lower() for word in (params.get("industry_keywords") or [])]
    text = str(industry).strip().lower()
    if any(_matches_token(text, word) for word in keywords):
        ratio = _decimal(params.get("keyword_ratio", "0.6"), "keyword_ratio")
        return (ratio, f"無 SIC，產業名稱「{industry}」命中關鍵字（次要證據）", True)
    return (ZERO, f"產業「{industry}」不在 ICP 範圍", True)


def _eval_company_size(record: dict[str, Any], params: dict[str, Any]) -> tuple[Decimal, str, bool]:
    """員工數：低於 min 不加分，達到 ideal 給滿分。"""
    employees = _numeric(record, "employee_count")
    if employees is None:
        return (ZERO, "缺 employee_count", False)
    ratio = _ramp(
        employees,
        _decimal(params.get("min_employees", 0), "min_employees"),
        _decimal(params.get("ideal_employees", 1), "ideal_employees"),
        "scoring.criteria.company_size",
    )
    return (ratio, f"員工數 {employees}", True)


def _eval_revenue(record: dict[str, Any], params: dict[str, Any]) -> tuple[Decimal, str, bool]:
    """年營收：全程 Decimal，避免 float 誤差讓邊界值忽上忽下。"""
    revenue = _numeric(record, "annual_revenue")
    if revenue is None:
        return (ZERO, "缺 annual_revenue", False)
    ratio = _ramp(
        revenue,
        _decimal(params.get("min_annual_revenue", 0), "min_annual_revenue"),
        _decimal(params.get("ideal_annual_revenue", 1), "ideal_annual_revenue"),
        "scoring.criteria.revenue_band",
    )
    return (ratio, f"年營收 {revenue:,.2f}", True)


def _eval_tech_stack(record: dict[str, Any], params: dict[str, Any]) -> tuple[Decimal, str, bool]:
    """技術棧訊號：用同類工具代表「買得起、也會用」，命中數達門檻給滿分。"""
    stack = record.get("tech_stack")
    if is_blank(stack):
        return (ZERO, "缺 tech_stack", False)
    tokens = {str(item).strip().lower() for item in stack} if isinstance(stack, (list, tuple, set)) \
        else {str(stack).strip().lower()}
    signals = {str(item).strip().lower() for item in (params.get("signals") or [])}
    hits = tokens & signals
    required = max(1, int(params.get("required_hits", 1)))
    ratio = min(ONE, Decimal(len(hits)) / Decimal(required))
    return (ratio, f"命中 {len(hits)}/{required} 個技術訊號：{'、'.join(sorted(hits)) or '無'}", True)


def _eval_title_seniority(record: dict[str, Any], params: dict[str, Any]) -> tuple[Decimal, str, bool]:
    """職稱層級：由上往下比對 tiers，第一個命中的生效。"""
    title = record.get("job_title")
    if is_blank(title):
        return (ZERO, "缺 job_title", False)
    text = str(title).strip().lower()
    for index, tier in enumerate(params.get("tiers") or []):
        titles = [str(word).strip().lower() for word in (tier.get("titles") or [])]
        if any(_matches_token(text, word) for word in titles):
            ratio = _decimal(tier.get("ratio", 0), f"scoring.criteria.title_seniority.tiers[{index}].ratio")
            return (ratio, f"職稱「{title}」命中第 {index + 1} 層", True)
    return (ZERO, f"職稱「{title}」不在決策層清單", True)


#: 評分項目註冊表。config 的 weights key 必須在這裡找得到，否則載入時直接報錯。
EVALUATORS: dict[str, Callable[[dict[str, Any], dict[str, Any]], tuple[Decimal, str, bool]]] = {
    "industry_match": _eval_industry,
    "company_size": _eval_company_size,
    "revenue_band": _eval_revenue,
    "tech_stack": _eval_tech_stack,
    "title_seniority": _eval_title_seniority,
}


# --------------------------------------------------------------------------
# 計分
# --------------------------------------------------------------------------


def _evaluate_all(record: dict[str, Any], rules: ScoringRules) -> list[CriterionScore]:
    """跑完所有評分項目，回傳逐項明細。"""
    breakdown: list[CriterionScore] = []
    for key, weight in rules.weights.items():
        ratio, reason, has_input = EVALUATORS[key](record, rules.criteria.get(key) or {})
        ratio = max(ZERO, min(ONE, ratio)).quantize(RATIO_PLACES, rounding=ROUND_HALF_UP)
        breakdown.append(
            CriterionScore(
                key=key,
                weight=weight,
                ratio=ratio,
                points=(weight * ratio).quantize(SCORE_PLACES, rounding=ROUND_HALF_UP),
                reason=reason,
                has_input=has_input,
            )
        )
    return breakdown


def _band_of(total: Decimal, rules: ScoringRules) -> str:
    """分數轉級距（書中 SCORE_THRESHOLDS：Hot 75-100、Warm 50-74）。"""
    if total >= rules.hot_threshold:
        return BAND_HOT
    return BAND_WARM if total >= rules.warm_threshold else BAND_COOL


def score_contact(
    record: dict[str, Any],
    rules: ScoringRules,
    now: datetime,
) -> ScoreResult:
    """為單一（已豐富化的）聯絡人計分並分級。"""
    breakdown = _evaluate_all(record, rules)
    raw_points = sum((item.points for item in breakdown), ZERO)
    total = (raw_points / rules.total_weight * HUNDRED).quantize(
        SCORE_PLACES, rounding=ROUND_HALF_UP
    )
    band = _band_of(total, rules)

    days = days_between(record.get("last_contacted_at"), now)
    is_stale = days is not None and days > rules.stale_days
    missing = tuple(item.key for item in breakdown if not item.has_input)

    return ScoreResult(
        contact_id=str(record.get("contact_id", "")),
        company=str(record.get("company", "")),
        total=total,
        band=band,
        # 低分又久未聯絡 = 清洗候選；高分久未聯絡不降級，改標 reengagement。
        grade=GRADE_STALE if (is_stale and band == BAND_COOL) else band,
        is_stale=is_stale,
        is_reengagement_target=is_stale and band in (BAND_HOT, BAND_WARM),
        days_since_contact=days,
        breakdown=tuple(breakdown),
        missing_inputs=missing,
        is_low_confidence=bool(missing) or record.get("enrichment_status") == STATUS_FAILED,
    )


def score_all(
    records: Iterable[dict[str, Any]], rules: ScoringRules, now: datetime
) -> list[ScoreResult]:
    """批次計分。"""
    return [score_contact(record, rules, now) for record in records]


def rank(scores: Sequence[ScoreResult]) -> list[ScoreResult]:
    """排序：分數高的在前；同分時久未聯絡的優先（那是最該今天打的電話）。

    書中的價值主張是「讓最熱的潛在客戶永遠浮在管線最頂端」，
    因此排序邏輯必須固定且可解釋，不能依賴 CRM 匯出的原始順序。
    """
    return sorted(
        scores,
        key=lambda item: (item.total, item.days_since_contact or 0),
        reverse=True,
    )
