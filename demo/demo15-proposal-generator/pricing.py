"""demo15 報價計算核心 —— RATE_CARD 與 A/B/C 三選項定價。

對應 SPEC #15 的 `Processing` 段：
`RATE_CARD` 組合服務項目與標準收費、`Pricing Strategy` 自動生成三個投資選項。

三個不可妥協的設計：

1. **金額一律 ``decimal.Decimal``，全檔禁止 ``float``**。
   本檔的輸出會直接變成客戶看到的報價單，而報價單簽下去就是合約金額。
   ``0.1 + 0.2 != 0.3`` 的二進位誤差經過「小計 → 折扣 → 稅 → 年度加總」四層放大後，
   會變成客戶對不上的尾差。因此 ``to_decimal()`` **直接拒收 float**——
   設定檔把金額寫成未加引號的 YAML 數字時會當場報錯，而不是安靜地算錯。
2. **價格永遠由程式算，永遠不由 LLM 產生**（遮蔽機制見 ``proposal_builder.py``）。
3. **超出範圍要出聲**：折扣超過上限、數量超過 RATE_CARD 上限、含稅建置費超過天花板，
   一律標記 ``requires_human_pricing`` 交人工核價，絕不自行外推一個「看起來合理」的數字。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

MONEY_PLACES = Decimal("0.01")
PERCENT = Decimal("100")
MONTHS_PER_YEAR = 12
ZERO = Decimal("0")

BILLING_ONE_OFF = "one_off"
BILLING_MONTHLY = "monthly"
SUPPORTED_BILLING = (BILLING_ONE_OFF, BILLING_MONTHLY)


class PricingError(ValueError):
    """報價設定或輸入不合法（未知服務代碼、金額格式錯誤、計費方式不支援）。"""


# --------------------------------------------------------------------------- #
# 數值工具（全檔唯一允許碰數字的地方）
# --------------------------------------------------------------------------- #
def to_decimal(value: Any, field_name: str) -> Decimal:
    """把設定檔／輸入資料轉成 ``Decimal``。``float`` 一律拒收。

    拒收 float 是刻意的防呆：YAML 裡 ``unit_price: 1200.00``（未加引號）會被解析成
    float，此時精度已經在進入本模組之前就損失了，事後再轉 Decimal 也救不回來。
    """
    if isinstance(value, bool):
        raise PricingError(f"{field_name} 不可是布林值：{value!r}")
    if isinstance(value, float):
        raise PricingError(
            f"{field_name} 收到 float（{value!r}）。金額欄位請在 YAML 中加引號寫成字串，"
            "例如 unit_price 寫成 \"1200.00\"，以免二進位浮點誤差被寫進報價單"
        )
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if value is None:
        raise PricingError(f"{field_name} 缺少數值")
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise PricingError(f"{field_name} 無法解析為數字：{value!r}") from exc


def money(value: Decimal) -> Decimal:
    """金額四捨五入到分位。用 ROUND_HALF_UP 而非銀行家捨入——報價單少一分錢客戶會問。"""
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    """金額轉字串。JSON 序列化一律走字串，避免下游 ``json.load`` 把它還原成 float。"""
    return f"{money(value):.2f}"


def format_money(value: Decimal, currency: str) -> str:
    """人類可讀金額（含千分位與幣別代碼），用於報價表與通知摘要。"""
    return f"{currency} {money(value):,.2f}"


def format_rate(rate: Decimal) -> str:
    """折扣／稅率轉成百分比字串，例如 Decimal("0.05") -> "5%"。"""
    text = f"{rate * PERCENT:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text or '0'}%"


def to_quantity(value: Any, field_name: str) -> int:
    """數量必須是正整數。0 或負數代表來源資料有問題，當場報錯而非略過。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise PricingError(f"{field_name} 必須是整數，收到 {value!r}")
    if value < 1:
        raise PricingError(f"{field_name} 必須 >= 1，收到 {value}")
    return value


# --------------------------------------------------------------------------- #
# RATE_CARD
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RateCardItem:
    """定價卡上的一個服務項目。"""

    code: str
    name: str
    billing: str
    unit: str
    unit_price: Decimal
    max_quantity: int


class RateCard:
    """``config.yaml`` 的 ``rate_card`` 區塊。未知代碼一律拋錯，不套用任何預設價。"""

    def __init__(self, items_config: list[dict[str, Any]] | None) -> None:
        if not items_config:
            raise PricingError("rate_card 不可為空：沒有定價卡就沒有報價依據")
        self._items: dict[str, RateCardItem] = {}
        for raw in items_config:
            item = self._parse_item(raw)
            if item.code in self._items:
                raise PricingError(f"RATE_CARD 服務代碼重複：{item.code}")
            self._items[item.code] = item

    @staticmethod
    def _parse_item(raw: dict[str, Any]) -> RateCardItem:
        """解析單一定價卡項目；欄位缺失或計費方式不支援即當場報錯。"""
        code = str(raw.get("code", "")).strip()
        if not code:
            raise PricingError(f"RATE_CARD 項目缺少 code：{raw!r}")
        billing = str(raw.get("billing", "")).strip()
        if billing not in SUPPORTED_BILLING:
            raise PricingError(
                f"{code} 的 billing 必須是 {'/'.join(SUPPORTED_BILLING)} 之一，收到 {billing!r}"
            )
        max_quantity = to_quantity(raw.get("max_quantity", 1), f"rate_card.{code}.max_quantity")
        return RateCardItem(
            code=code,
            name=str(raw.get("name", code)),
            billing=billing,
            unit=str(raw.get("unit", "式")),
            unit_price=to_decimal(raw.get("unit_price"), f"rate_card.{code}.unit_price"),
            max_quantity=max_quantity,
        )

    def item(self, code: str) -> RateCardItem:
        """取得服務項目；未知代碼拋 ``PricingError``，絕不猜一個預設價。"""
        try:
            return self._items[code]
        except KeyError as exc:
            raise PricingError(f"RATE_CARD 沒有服務代碼 {code!r}；可用代碼：{self.codes}") from exc

    @property
    def codes(self) -> list[str]:
        """所有已定義的服務代碼（排序後，用於錯誤訊息）。"""
        return sorted(self._items)


# --------------------------------------------------------------------------- #
# 報價資料結構
# --------------------------------------------------------------------------- #
@dataclass
class LineItem:
    """報價單上的一行。``amount = unit_price × quantity``，全程 Decimal。"""

    code: str
    name: str
    billing: str
    unit: str
    quantity: int
    unit_price: Decimal
    amount: Decimal

    def to_dict(self) -> dict[str, Any]:
        """轉成可 JSON 序列化的 dict（金額一律字串）。"""
        return {
            "code": self.code,
            "name": self.name,
            "billing": self.billing,
            "unit": self.unit,
            "quantity": self.quantity,
            "unit_price": money_str(self.unit_price),
            "amount": money_str(self.amount),
        }


@dataclass
class QuoteOption:
    """A／B／C 其中一個投資選項的完整金額結構。"""

    tier_key: str
    tier_name: str
    summary: str
    currency: str
    is_recommended: bool
    lines: list[LineItem]
    one_off_subtotal: Decimal
    monthly_subtotal: Decimal
    discount_rate: Decimal
    discount_amount: Decimal
    one_off_after_discount: Decimal
    tax_rate: Decimal
    one_off_tax: Decimal
    monthly_tax: Decimal
    setup_total: Decimal
    monthly_total: Decimal
    first_year_total: Decimal
    issues: list[str] = field(default_factory=list)

    @property
    def requires_human_pricing(self) -> bool:
        """有任何一項超出自動報價範圍就必須人工核價。"""
        return bool(self.issues)

    def to_dict(self) -> dict[str, Any]:
        """轉成可 JSON 序列化的 dict（金額一律字串）。"""
        return {
            "tier_key": self.tier_key,
            "tier_name": self.tier_name,
            "summary": self.summary,
            "currency": self.currency,
            "is_recommended": self.is_recommended,
            "lines": [line.to_dict() for line in self.lines],
            "one_off_subtotal": money_str(self.one_off_subtotal),
            "monthly_subtotal": money_str(self.monthly_subtotal),
            "discount_rate": str(self.discount_rate),
            "discount_amount": money_str(self.discount_amount),
            "one_off_after_discount": money_str(self.one_off_after_discount),
            "tax_rate": str(self.tax_rate),
            "one_off_tax": money_str(self.one_off_tax),
            "monthly_tax": money_str(self.monthly_tax),
            "setup_total": money_str(self.setup_total),
            "monthly_total": money_str(self.monthly_total),
            "first_year_total": money_str(self.first_year_total),
            "issues": list(self.issues),
            "requires_human_pricing": self.requires_human_pricing,
        }


# --------------------------------------------------------------------------- #
# 報價引擎
# --------------------------------------------------------------------------- #
class QuoteEngine:
    """依 RATE_CARD 與折扣／稅率規則產出三個投資選項。"""

    def __init__(self, pricing_config: dict[str, Any] | None, rate_card: RateCard) -> None:
        config = pricing_config or {}
        self.rate_card = rate_card
        self.currency = str(config.get("currency", "USD"))
        self.tax_rate = to_decimal(config.get("tax_rate", "0"), "pricing.tax_rate")
        self.tax_label = str(config.get("tax_label", "稅"))
        self.quote_valid_days = to_quantity(
            config.get("quote_valid_days", 14), "pricing.quote_valid_days"
        )
        guardrails = config.get("guardrails") or {}
        self.max_discount_rate = to_decimal(
            guardrails.get("max_discount_rate", "0.20"), "pricing.guardrails.max_discount_rate"
        )
        self.auto_quote_ceiling = to_decimal(
            guardrails.get("auto_quote_ceiling", "25000"), "pricing.guardrails.auto_quote_ceiling"
        )
        self._discount_tiers = self._parse_discount_tiers(config.get("discount_tiers") or [])
        self._tiers = list(config.get("tiers") or [])
        if not self._tiers:
            raise PricingError("pricing.tiers 不可為空：SPEC #15 要求一律產出 A/B/C 三個投資選項")

    @staticmethod
    def _parse_discount_tiers(raw_tiers: list[dict[str, Any]]) -> list[tuple[Decimal, Decimal]]:
        """解析折扣級距並依門檻由大到小排序（設定檔順序寫反時不會默默套錯級距）。"""
        tiers: list[tuple[Decimal, Decimal]] = []
        for index, raw in enumerate(raw_tiers):
            threshold = to_decimal(
                raw.get("min_subtotal"), f"pricing.discount_tiers[{index}].min_subtotal"
            )
            rate = to_decimal(raw.get("rate"), f"pricing.discount_tiers[{index}].rate")
            if rate < ZERO or rate > Decimal("1"):
                raise PricingError(f"折扣級距 {index} 的 rate 必須落在 0–1 之間，收到 {rate}")
            tiers.append((threshold, rate))
        return sorted(tiers, key=lambda pair: pair[0], reverse=True)

    def valid_until(self, as_of: date) -> date:
        """報價有效期限 = 報價日 + ``pricing.quote_valid_days``。"""
        return as_of + timedelta(days=self.quote_valid_days)

    def ladder_discount_rate(self, one_off_subtotal: Decimal) -> Decimal:
        """依一次性費用小計查折扣級距；沒命中任何級距回 0。"""
        for threshold, rate in self._discount_tiers:
            if one_off_subtotal >= threshold:
                return rate
        return ZERO

    def resolve_discount_rate(
        self, one_off_subtotal: Decimal, requested_rate: Decimal | None
    ) -> tuple[Decimal, list[str]]:
        """決定實際折扣率。回傳（折扣率, 需人工核價的理由清單）。

        業務手動要求的折扣若高於上限，**不照給也不靜默忽略**：夾到上限並要求人工核價。
        """
        issues: list[str] = []
        rate = self.ladder_discount_rate(one_off_subtotal)
        if requested_rate is None:
            return rate, issues
        if requested_rate > self.max_discount_rate:
            issues.append(
                f"業務要求折扣 {format_rate(requested_rate)} 超過上限 "
                f"{format_rate(self.max_discount_rate)}，已夾到上限，需主管核價"
            )
            return self.max_discount_rate, issues
        return max(rate, requested_rate), issues

    def _merge_quantities(
        self, requested_services: list[dict[str, Any]], add_ons: list[dict[str, Any]]
    ) -> dict[str, int]:
        """把客戶需求與方案加值項目合併成「代碼 -> 數量」。同代碼出現兩次即累加。"""
        merged: dict[str, int] = {}
        for source, label in ((requested_services, "requested_services"), (add_ons, "add_ons")):
            for index, entry in enumerate(source or []):
                code = str(entry.get("code", "")).strip()
                if not code:
                    raise PricingError(f"{label}[{index}] 缺少 code：{entry!r}")
                quantity = to_quantity(entry.get("quantity", 1), f"{label}[{index}].quantity")
                merged[code] = merged.get(code, 0) + quantity
        if not merged:
            raise PricingError("報價項目為空：至少要有一項服務才能產生報價")
        return merged

    def _build_lines(self, quantities: dict[str, int]) -> tuple[list[LineItem], list[str]]:
        """展開成報價明細；數量超過 RATE_CARD 上限即列為需人工核價的理由。"""
        lines: list[LineItem] = []
        issues: list[str] = []
        for code, quantity in quantities.items():
            item = self.rate_card.item(code)
            if quantity > item.max_quantity:
                issues.append(
                    f"{item.name}（{code}）數量 {quantity} 超過 RATE_CARD 上限 "
                    f"{item.max_quantity}，需人工確認交付量能與報價"
                )
            lines.append(
                LineItem(
                    code=code,
                    name=item.name,
                    billing=item.billing,
                    unit=item.unit,
                    quantity=quantity,
                    unit_price=item.unit_price,
                    amount=money(item.unit_price * Decimal(quantity)),
                )
            )
        # 一次性費用排在訂閱之前，讓報價表的閱讀順序與客戶的付款順序一致。
        lines.sort(key=lambda line: (line.billing != BILLING_ONE_OFF, line.code))
        return lines, issues

    def _subtotals(self, lines: list[LineItem]) -> tuple[Decimal, Decimal]:
        """依計費方式分開加總：（一次性小計, 每月小計）。兩者永遠不可混加。"""
        one_off = sum((line.amount for line in lines if line.billing == BILLING_ONE_OFF), ZERO)
        monthly = sum((line.amount for line in lines if line.billing == BILLING_MONTHLY), ZERO)
        return money(one_off), money(monthly)

    def build_option(
        self,
        tier_config: dict[str, Any],
        requested_services: list[dict[str, Any]],
        requested_discount_rate: Decimal | None = None,
    ) -> QuoteOption:
        """算出單一方案（A／B／C 其中之一）的完整金額。"""
        quantities = self._merge_quantities(requested_services, tier_config.get("add_ons") or [])
        lines, issues = self._build_lines(quantities)
        one_off_subtotal, monthly_subtotal = self._subtotals(lines)
        discount_rate, discount_issues = self.resolve_discount_rate(
            one_off_subtotal, requested_discount_rate
        )
        issues.extend(discount_issues)
        totals = self._totals(one_off_subtotal, monthly_subtotal, discount_rate)
        if totals["setup_total"] > self.auto_quote_ceiling:
            issues.append(
                f"含稅建置費 {format_money(totals['setup_total'], self.currency)} 超過自動報價天花板 "
                f"{format_money(self.auto_quote_ceiling, self.currency)}，需主管核價"
            )
        return QuoteOption(
            tier_key=str(tier_config.get("key", "")),
            tier_name=str(tier_config.get("name", tier_config.get("key", ""))),
            summary=str(tier_config.get("summary", "")),
            currency=self.currency,
            is_recommended=bool(tier_config.get("is_recommended", False)),
            lines=lines,
            one_off_subtotal=one_off_subtotal,
            monthly_subtotal=monthly_subtotal,
            discount_rate=discount_rate,
            tax_rate=self.tax_rate,
            issues=issues,
            **totals,
        )

    def _totals(
        self, one_off_subtotal: Decimal, monthly_subtotal: Decimal, discount_rate: Decimal
    ) -> dict[str, Decimal]:
        """折扣 → 稅 → 總計。折扣只作用於一次性費用（``pricing.discount_basis: one_off``）：
        訂閱月費是長期履約成本，打折會直接侵蝕 MRR 的健康度。
        """
        discount_amount = money(one_off_subtotal * discount_rate)
        one_off_after_discount = money(one_off_subtotal - discount_amount)
        one_off_tax = money(one_off_after_discount * self.tax_rate)
        monthly_tax = money(monthly_subtotal * self.tax_rate)
        setup_total = money(one_off_after_discount + one_off_tax)
        monthly_total = money(monthly_subtotal + monthly_tax)
        return {
            "discount_amount": discount_amount,
            "one_off_after_discount": one_off_after_discount,
            "one_off_tax": one_off_tax,
            "monthly_tax": monthly_tax,
            "setup_total": setup_total,
            "monthly_total": monthly_total,
            "first_year_total": money(setup_total + monthly_total * Decimal(MONTHS_PER_YEAR)),
        }

    def build_options(
        self,
        requested_services: list[dict[str, Any]],
        requested_discount_rate: Decimal | None = None,
    ) -> list[QuoteOption]:
        """產出 A／B／C 三個投資選項（SPEC #15 的 Pricing Strategy）。"""
        return [
            self.build_option(tier, requested_services, requested_discount_rate)
            for tier in self._tiers
        ]


def pick_recommended(options: list[QuoteOption]) -> QuoteOption:
    """挑出要標示為「推薦」的方案；沒有任何一個標記時退回中間那個。"""
    if not options:
        raise PricingError("報價選項為空，無法挑選推薦方案")
    for option in options:
        if option.is_recommended:
            return option
    return options[len(options) // 2]
