"""定價決策：3x3 決策矩陣 + `pricing_rules` + 定價安全鐵律。

本檔是整個模組風險最高的地方——它產出的數字會變成線上售價。
一個小數點錯誤就可能把整批庫存虧本賣光，所以這裡的預設值一律偏保守：

    1. 任何建議售價**不得低於（或等於）成本價** -> 直接拒絕並升級 RED
    2. 單次變動幅度超過 `max_price_change_percent` -> 拒絕並升級人工
    3. 所有調價建議一律以 `DRAFT` 產出，**人工核准後**才可能寫回平台
    4. 缺貨中的 SKU 不調價（該做的是補貨，不是改價）
    5. 沒有對手報價就不動價（沒有依據的調價等於瞎猜）

決策矩陣（apxG_p14，庫存水位 x 對手定價）與 `pricing_rules` 的關係，
原簡報並未言明，本專案採取的解讀是：

    決策矩陣決定「動作型態」（降價／調漲／清理／保持現狀）
    pricing_rules 決定「觸發門檻」（差距要多大才動手）

兩者**都成立**才會產生調價建議，任一不成立則退回 HOLD。
這是保守方向的解讀：規格留白處寧可少動一次價，也不要多動一次。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from analyser import (
    BAND_FAST,
    BAND_NEUTRAL,
    BAND_SLOW,
    STATUS_OVERSTOCK,
    AnalyserError,
    SkuAnalysis,
    to_rate,
    to_signed_rate,
)

MONEY_QUANT = Decimal("0.01")
PERCENT_QUANT = Decimal("0.01")
HUNDRED = Decimal("100")

# 對手定價相對位置
POSITION_UNDERCUT = "undercut"
POSITION_NEUTRAL = "neutral"
POSITION_ABOVE = "above"
POSITION_UNKNOWN = "unknown"

# 矩陣的三個列（庫存水位）
ROW_SLOW_MOVER = "slow_mover"
ROW_FAST_MOVER = "fast_mover"
ROW_OVERSTOCK = "overstock"

# 建議動作
ACTION_HOLD = "HOLD"
ACTION_REDUCE_MATCH = "REDUCE_MATCH_COMPETITOR"
ACTION_INCREASE = "INCREASE"
ACTION_PROMOTE = "PROMOTE"
ACTION_CLEAR = "CLEAR"

# 核准狀態：DRAFT 是預設，永遠需要人工核准才可能寫回平台
STATE_DRAFT = "DRAFT"
STATE_HOLD = "HOLD"
STATE_REJECTED = "REJECTED"

# 拒絕原因
REJECT_BELOW_COST = "BELOW_COST"
REJECT_BELOW_MIN_MARGIN = "BELOW_MIN_MARGIN"
REJECT_EXCEEDS_MAX_CHANGE = "EXCEEDS_MAX_CHANGE"
REJECT_NEGATIVE_MARGIN = "NEGATIVE_MARGIN"

SEVERITY_INFO = "info"
SEVERITY_AMBER = "amber"
SEVERITY_RED = "red"

# apxG_p14 的 3x3 矩陣。原圖留白的格子代表「不建議動作」，
# 這裡明確寫成 HOLD，而不是讓它變成未定義行為。
DECISION_MATRIX: dict[tuple[str, str], str] = {
    (ROW_SLOW_MOVER, POSITION_UNDERCUT): ACTION_REDUCE_MATCH,   # 建議降價匹配對手 -1%
    (ROW_SLOW_MOVER, POSITION_NEUTRAL): ACTION_HOLD,            # （原圖空白）
    (ROW_SLOW_MOVER, POSITION_ABOVE): ACTION_HOLD,              # 保持現狀
    (ROW_FAST_MOVER, POSITION_UNDERCUT): ACTION_HOLD,           # （原圖空白）
    (ROW_FAST_MOVER, POSITION_NEUTRAL): ACTION_PROMOTE,         # 建議促銷
    (ROW_FAST_MOVER, POSITION_ABOVE): ACTION_INCREASE,          # 建議調漲
    (ROW_OVERSTOCK, POSITION_UNDERCUT): ACTION_HOLD,            # （原圖空白）
    (ROW_OVERSTOCK, POSITION_NEUTRAL): ACTION_CLEAR,            # 建議清理
    (ROW_OVERSTOCK, POSITION_ABOVE): ACTION_CLEAR,              # 建議清理
}


class PricerError(ValueError):
    """定價設定不合法"""


@dataclass(frozen=True)
class PriceProposal:
    """單一 SKU 的定價建議（含被安全閥擋下的紀錄）。

    被拒絕的建議**也會保留原始計算值**（`blocked_price`），
    這樣人工審查時才看得到「系統本來想改成多少、為什麼不准」。
    """

    sku_id: str
    product_name: str
    status: str
    velocity_band: str
    matrix_row: str | None
    competitor_position: str
    competitor_gap_percent: Decimal | None
    action: str
    rules_matched: tuple[str, ...]
    current_price: Decimal
    cost_price: Decimal
    proposed_price: Decimal | None
    blocked_price: Decimal | None
    change_percent: Decimal | None
    approval_state: str
    reject_reason: str | None
    severity: str
    reason: str

    @property
    def is_price_change(self) -> bool:
        """本筆是否真的產生了一個可送審的新價格"""
        return self.approval_state == STATE_DRAFT and self.proposed_price is not None

    @property
    def needs_human_escalation(self) -> bool:
        """是否必須升級人工（被安全閥擋下的都算）"""
        return self.approval_state == STATE_REJECTED

    def as_dict(self) -> dict[str, Any]:
        """轉成 JSON 可序列化形狀；金額一律字串，杜絕下游退回 float"""
        return {
            "sku_id": self.sku_id,
            "product_name": self.product_name,
            "status": self.status,
            "velocity_band": self.velocity_band,
            "matrix_row": self.matrix_row,
            "competitor_position": self.competitor_position,
            "competitor_gap_percent": (
                None if self.competitor_gap_percent is None else str(self.competitor_gap_percent)
            ),
            "action": self.action,
            "rules_matched": list(self.rules_matched),
            "current_price": str(self.current_price),
            "cost_price": str(self.cost_price),
            "proposed_price": None if self.proposed_price is None else str(self.proposed_price),
            "blocked_price": None if self.blocked_price is None else str(self.blocked_price),
            "change_percent": None if self.change_percent is None else str(self.change_percent),
            "approval_state": self.approval_state,
            "reject_reason": self.reject_reason,
            "severity": self.severity,
            "reason": self.reason,
        }


def _pct(value: Decimal) -> Decimal:
    """百分比一律四捨五入到小數兩位"""
    return value.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    """金額一律四捨五入到小數兩位"""
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def competitor_gap_percent(current_price: Decimal, competitor_price: Decimal) -> Decimal:
    """對手價相對我方售價的差距百分比（負值＝對手比我們便宜）"""
    if current_price <= 0:
        raise PricerError(f"我方售價必須為正數，收到 {current_price}")
    return _pct((competitor_price - current_price) / current_price * HUNDRED)


def classify_position(gap_percent: Decimal, settings: dict[str, Any]) -> str:
    """把差距百分比歸入 undercut / neutral / above。

    刻意讓 -5% ~ -3% 這段落入 NEUTRAL（等同不動作）：
    `pricing_rules.reduce_if` 明寫要「對手低於我方超過 5%」才降價，
    所以介於中性帶與 5% 之間的小幅劣勢一律當作「還不用反應」。
    """
    neutral_band = to_rate(settings.get("competitor_neutral_band_percent", 3), "neutral_band")
    undercut_threshold = to_rate(
        settings.get("competitor_undercut_threshold_percent", 5), "undercut_threshold"
    )
    if gap_percent <= -undercut_threshold:
        return POSITION_UNDERCUT
    if gap_percent > neutral_band:
        return POSITION_ABOVE
    return POSITION_NEUTRAL


def matrix_row_of(analysis: SkuAnalysis) -> str | None:
    """決定 SKU 落在矩陣的哪一列；三列都不屬於就回 None（＝不動作）。

    OVERSTOCK 優先於流速帶：庫存積到滿出來時，它就是清理對象，
    不管當初賣得快不快。
    """
    if analysis.status == STATUS_OVERSTOCK:
        return ROW_OVERSTOCK
    if analysis.velocity_band == BAND_SLOW:
        return ROW_SLOW_MOVER
    if analysis.velocity_band == BAND_FAST:
        return ROW_FAST_MOVER
    return None


def evaluate_pricing_rules(
    analysis: SkuAnalysis, gap_percent: Decimal, settings: dict[str, Any]
) -> tuple[str, ...]:
    """逐字實作 `pricing_rules` 三條規則，回傳命中的規則名稱。

        reduce_if  : slow_mover AND competitor_price_below_ours_by_pct > 5
        increase_if: fast_mover AND days_of_stock < 14 AND competitor_price_above_ours
        hold_if    : velocity_neutral AND competitor_within_3pct

    三個數字（5 / 14 / 3）在 config 可調，預設就是規格上的字面值。
    """
    below_by_pct = -gap_percent if gap_percent < 0 else Decimal("0")
    reduce_threshold = to_rate(settings.get("reduce_if_below_pct", 5), "reduce_if_below_pct")
    increase_max_doh = to_rate(settings.get("increase_if_days_of_stock_under", 14),
                               "increase_if_days_of_stock_under")
    hold_band = to_rate(settings.get("hold_if_within_pct", 3), "hold_if_within_pct")
    doh = analysis.days_on_hand

    matched: list[str] = []
    if analysis.velocity_band == BAND_SLOW and below_by_pct > reduce_threshold:
        matched.append("reduce_if")
    if analysis.velocity_band == BAND_FAST and doh is not None and doh < increase_max_doh and gap_percent > 0:
        matched.append("increase_if")
    if analysis.velocity_band == BAND_NEUTRAL and abs(gap_percent) <= hold_band:
        matched.append("hold_if")
    return tuple(matched)


def target_price(action: str, analysis: SkuAnalysis, settings: dict[str, Any]) -> Decimal | None:
    """依動作算出目標售價；不涉及改價的動作回 None。"""
    competitor = analysis.competitor_price
    if action == ACTION_REDUCE_MATCH and competitor is not None:
        # 「建議降價匹配對手 -1%」：貼著對手價再往下 1%
        delta = to_rate(settings.get("undercut_match_delta_percent", 1), "undercut_match_delta")
        return _money(competitor * (HUNDRED - delta) / HUNDRED)
    if action == ACTION_INCREASE:
        delta = to_rate(settings.get("fast_mover_increase_percent", 5), "fast_mover_increase")
        return _money(analysis.current_price * (HUNDRED + delta) / HUNDRED)
    if action == ACTION_CLEAR:
        delta = to_rate(settings.get("overstock_clearance_percent", 20), "overstock_clearance")
        return _money(analysis.current_price * (HUNDRED - delta) / HUNDRED)
    return None


def _gate_action(action: str, rules: tuple[str, ...]) -> tuple[str, str]:
    """矩陣動作 x pricing_rules 門檻的交叉判定，回傳 (最終動作, 說明)。"""
    if action == ACTION_REDUCE_MATCH and "reduce_if" not in rules:
        return ACTION_HOLD, "矩陣建議降價，但未達 pricing_rules.reduce_if 門檻，維持原價"
    if action == ACTION_INCREASE and "increase_if" not in rules:
        return ACTION_HOLD, "矩陣建議調漲，但未達 pricing_rules.increase_if 條件，維持原價"
    return action, ""


def _pre_pricing_block(analysis: SkuAnalysis) -> tuple[str, str, str] | None:
    """進入計價前的三道否決；回傳 (approval_state, severity, 說明) 或 None。"""
    if analysis.has_negative_margin:
        return (
            STATE_REJECTED,
            SEVERITY_RED,
            f"售價 {analysis.current_price} 未高於成本 {analysis.cost_price}，"
            "本模組禁止對負毛利商品自動調價，一律升級人工",
        )
    if analysis.is_stockout:
        return STATE_HOLD, SEVERITY_AMBER, "缺貨中：優先補貨，調價無意義"
    if analysis.competitor_price is None:
        return STATE_HOLD, SEVERITY_AMBER, "缺少對手報價，沒有比價依據，不動價"
    return None


def _check_rails(
    candidate: Decimal, analysis: SkuAnalysis, settings: dict[str, Any]
) -> tuple[str | None, str, str]:
    """安全閥檢查，回傳 (拒絕原因或 None, severity, 說明)。"""
    min_margin = to_rate(settings.get("min_margin_percent", 5), "min_margin_percent")
    max_change = to_rate(settings.get("max_price_change_percent", 10), "max_price_change_percent")
    floor_price = _money(analysis.cost_price * (HUNDRED + min_margin) / HUNDRED)
    change = _pct((candidate - analysis.current_price) / analysis.current_price * HUNDRED)
    if candidate <= analysis.cost_price:
        return (
            REJECT_BELOW_COST,
            SEVERITY_RED,
            f"建議價 {candidate} 不高於成本 {analysis.cost_price}：虧本賣，一律拒絕",
        )
    if candidate < floor_price:
        return (
            REJECT_BELOW_MIN_MARGIN,
            SEVERITY_AMBER,
            f"建議價 {candidate} 低於最低毛利底價 {floor_price}"
            f"（成本 +{_pct(min_margin)}%），需人工核准",
        )
    if abs(change) > max_change:
        return (
            REJECT_EXCEEDS_MAX_CHANGE,
            SEVERITY_AMBER,
            f"變動 {change}% 超過單次上限 {_pct(max_change)}%，拒絕自動執行並升級人工",
        )
    return None, SEVERITY_INFO, f"通過安全閥，變動 {change}%，待人工核准後寫回平台"


def propose(analysis: SkuAnalysis, settings: dict[str, Any]) -> PriceProposal:
    """對單一 SKU 產出定價建議（永遠是 DRAFT 或被擋下的紀錄，不會直接生效）。"""
    row = matrix_row_of(analysis)
    gap = (
        None
        if analysis.competitor_price is None
        else competitor_gap_percent(analysis.current_price, analysis.competitor_price)
    )
    position = POSITION_UNKNOWN if gap is None else classify_position(gap, settings)
    rules = () if gap is None else evaluate_pricing_rules(analysis, gap, settings)

    blocked = _pre_pricing_block(analysis)
    if blocked is not None:
        state, severity, reason = blocked
        return _build(
            analysis, row, position, gap, ACTION_HOLD, rules,
            proposed=None, blocked=None, change=None, state=state,
            reject_reason=REJECT_NEGATIVE_MARGIN if state == STATE_REJECTED else None,
            severity=severity, reason=reason,
        )

    action = DECISION_MATRIX.get((row, position), ACTION_HOLD) if row else ACTION_HOLD
    action, gate_note = _gate_action(action, rules)
    candidate = target_price(action, analysis, settings)
    if candidate is None:
        return _build(
            analysis, row, position, gap, action, rules,
            proposed=None, blocked=None, change=None, state=STATE_HOLD,
            reject_reason=None, severity=SEVERITY_INFO,
            reason=gate_note or _hold_reason(action, row, position),
        )

    reject_reason, severity, reason = _check_rails(candidate, analysis, settings)
    change = _pct((candidate - analysis.current_price) / analysis.current_price * HUNDRED)
    if reject_reason is not None:
        return _build(
            analysis, row, position, gap, action, rules,
            proposed=None, blocked=candidate, change=change, state=STATE_REJECTED,
            reject_reason=reject_reason, severity=severity, reason=reason,
        )
    return _build(
        analysis, row, position, gap, action, rules,
        proposed=candidate, blocked=None, change=change, state=STATE_DRAFT,
        reject_reason=None, severity=severity, reason=reason,
    )


def _hold_reason(action: str, row: str | None, position: str) -> str:
    """組出 HOLD 的人話理由（讓報告看得出「為什麼不動」）"""
    if action == ACTION_PROMOTE:
        return "熱銷且與對手價位相當：建議走行銷促銷，不動價"
    if row is None:
        return "流速中性且未達積壓門檻：矩陣三列皆不適用，維持原價"
    return f"矩陣格 ({row} x {position}) 建議保持現狀"


def _build(
    analysis: SkuAnalysis,
    row: str | None,
    position: str,
    gap: Decimal | None,
    action: str,
    rules: tuple[str, ...],
    *,
    proposed: Decimal | None,
    blocked: Decimal | None,
    change: Decimal | None,
    state: str,
    reject_reason: str | None,
    severity: str,
    reason: str,
) -> PriceProposal:
    """統一組出 PriceProposal（參數順序固定，避免各分支自行拼裝而漏欄位）"""
    return PriceProposal(
        sku_id=analysis.sku_id,
        product_name=analysis.product_name,
        status=analysis.status,
        velocity_band=analysis.velocity_band,
        matrix_row=row,
        competitor_position=position,
        competitor_gap_percent=gap,
        action=action,
        rules_matched=rules,
        current_price=analysis.current_price,
        cost_price=analysis.cost_price,
        proposed_price=proposed,
        blocked_price=blocked,
        change_percent=change,
        approval_state=state,
        reject_reason=reject_reason,
        severity=severity,
        reason=reason,
    )


def propose_all(analyses: list[SkuAnalysis], settings: dict[str, Any]) -> list[PriceProposal]:
    """對整批 SKU 產出定價建議"""
    return [propose(item, settings) for item in analyses]


def summarise_proposals(proposals: list[PriceProposal]) -> dict[str, int]:
    """統計調價建議，供報告與退出碼判斷"""
    return {
        "total": len(proposals),
        "drafts": sum(1 for item in proposals if item.is_price_change),
        "holds": sum(1 for item in proposals if item.approval_state == STATE_HOLD),
        "rejected": sum(1 for item in proposals if item.needs_human_escalation),
        "red": sum(1 for item in proposals if item.severity == SEVERITY_RED),
    }


def _percent_or_problem(
    settings: dict[str, Any], key: str, default: Any, problems: list[str]
) -> Decimal | None:
    """讀出一個 pricing 百分比設定；轉不動就把問題收進清單並回傳 None。

    這裡刻意用 `to_signed_rate()` 而不是 `to_rate()`：後者對負值直接拋
    `AnalyserError`，會讓 `validate_settings()` 在第一個問題就中斷。
    負值是本函式要**回報**的問題之一，不是要中止流程的例外——
    設定檔同時有兩個錯時，使用者必須一次看到兩個，而不是修完一個再發現還有一個。
    """
    try:
        return to_signed_rate(settings.get(key, default), f"pricing.{key}")
    except AnalyserError as exc:
        problems.append(str(exc))
        return None


def validate_settings(settings: dict[str, Any]) -> list[str]:
    """啟動時檢查定價設定是否合理，回傳**完整**問題清單（空清單＝通過）。

    這些檢查故意做在跑分析之前：設定錯了就整批建議都錯，
    與其產出一份看似正常的錯誤報告，不如當場擋住。

    本函式**不拋例外**：無法解析、負值、超出上限全部收斂成問題字串，
    呼叫端才拿得到一次到位的清單。
    """
    problems: list[str] = []
    max_change = _percent_or_problem(settings, "max_price_change_percent", 10, problems)
    ceiling = _percent_or_problem(settings, "max_price_change_ceiling", 30, problems)
    if max_change is not None and max_change <= 0:
        problems.append("pricing.max_price_change_percent 必須大於 0，否則永遠無法調價")
    if max_change is not None and ceiling is not None and max_change > ceiling:
        problems.append(
            f"pricing.max_price_change_percent={_pct(max_change)}% 超過安全上限 "
            f"{_pct(ceiling)}%，單次調價幅度過大等於關掉安全閥"
        )
    min_margin = _percent_or_problem(settings, "min_margin_percent", 5, problems)
    if min_margin is not None and min_margin < 0:
        problems.append("pricing.min_margin_percent 不可為負數（那等於允許虧本賣）")
    for key in ("undercut_match_delta_percent", "fast_mover_increase_percent",
                "overstock_clearance_percent"):
        value = _percent_or_problem(settings, key, 1, problems)
        if value is not None and value < 0:
            problems.append(f"pricing.{key} 不可為負數")
    return problems
