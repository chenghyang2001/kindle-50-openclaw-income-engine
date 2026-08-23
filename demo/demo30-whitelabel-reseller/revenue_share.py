"""模組 #30 — 分潤計算（20-30% 經銷商 / 70-80% 基礎設施提供者）。

規格來源：apxG_p19「20-30% 經銷商 / 70-80% 基礎設施提供者」。
ch07_p14 印為「20-300%」，主 Claude 已裁決該值為誤植（分潤總和不可能超過 100%），
本檔以 20-30% / 70-80% 實作，並在 `SplitPolicy.validate()` 強制總和為 100。

金額一律 `decimal.Decimal`：
浮點數的 0.1 + 0.2 != 0.3 在營收拆帳上不是學術問題——每月對帳差一分錢，
經銷商就會質疑整套基礎設施的可信度，而那正是白牌模式唯一的資產。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

CENT = Decimal("0.01")
HUNDRED = Decimal("100")

# apxG_p19 的分潤帶。落在帶外不算錯（合約可談），但一定要讓人看見。
DEFAULT_BAND_MIN = Decimal("20")
DEFAULT_BAND_MAX = Decimal("30")


class RevenueShareError(ValueError):
    """分潤設定或金額違規。"""


def to_money(value: Any, field: str = "金額") -> Decimal:
    """把設定檔或 JSON 來的數值轉成 Decimal。

    一律先轉 `str` 再進 Decimal：`Decimal(0.1)` 會忠實保留二進位誤差，
    `Decimal(str(0.1))` 才是人類寫下的那個 0.1。
    """
    if isinstance(value, bool) or value is None:
        raise RevenueShareError(f"{field} 必須是數值，收到 {value!r}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RevenueShareError(f"{field} 無法解析為數值：{value!r}") from exc
    if not amount.is_finite():
        raise RevenueShareError(f"{field} 必須是有限數值：{value!r}")
    if amount < 0:
        raise RevenueShareError(f"{field} 不可為負數：{value!r}")
    return amount


def format_money(amount: Decimal) -> str:
    """統一輸出到分為止的字串，避免各處 quantize 不一致。"""
    return str(amount.quantize(CENT, rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class SplitPolicy:
    """分潤政策。比例可設定，但總和必須恰為 100。"""

    reseller_pct: Decimal
    provider_pct: Decimal
    band_min: Decimal = DEFAULT_BAND_MIN
    band_max: Decimal = DEFAULT_BAND_MAX

    @classmethod
    def from_config(cls, config: dict | None) -> "SplitPolicy":
        """從 config.yaml 的 revenue_share 區段建立政策。"""
        section = config or {}
        band = section.get("reseller_band") or [DEFAULT_BAND_MIN, DEFAULT_BAND_MAX]
        if not isinstance(band, (list, tuple)) or len(band) != 2:
            raise RevenueShareError(f"reseller_band 必須是兩個元素的清單：{band!r}")
        return cls(
            reseller_pct=to_money(section.get("reseller_pct", 25), "reseller_pct"),
            provider_pct=to_money(section.get("provider_pct", 75), "provider_pct"),
            band_min=to_money(band[0], "reseller_band[0]"),
            band_max=to_money(band[1], "reseller_band[1]"),
        )

    def validate(self) -> list[str]:
        """總和不為 100 直接拋錯；落在 20-30 帶外回傳警告字串。"""
        total = self.reseller_pct + self.provider_pct
        if total != HUNDRED:
            raise RevenueShareError(
                f"分潤比例總和必須為 100，實得 {total}"
                f"（經銷商 {self.reseller_pct} + 提供者 {self.provider_pct}）"
            )
        if self.band_min > self.band_max:
            raise RevenueShareError(f"reseller_band 上下限顛倒：{self.band_min}-{self.band_max}")
        if not self.band_min <= self.reseller_pct <= self.band_max:
            return [
                f"經銷商分潤 {self.reseller_pct}% 落在書中建議帶 "
                f"{self.band_min}-{self.band_max}% 之外，請確認經銷商協議已載明"
            ]
        return []


def split_fee(monthly_fee: Decimal, policy: SplitPolicy) -> dict[str, Decimal]:
    """把單一子客戶月費拆成經銷商 / 提供者兩份。

    只對經銷商那份做四捨五入，提供者拿「總額減經銷商」的餘數——
    兩邊各自 quantize 會製造出總和對不上的分位差。
    """
    fee = monthly_fee.quantize(CENT, rounding=ROUND_HALF_UP)
    reseller = (fee * policy.reseller_pct / HUNDRED).quantize(CENT, rounding=ROUND_HALF_UP)
    provider = fee - reseller
    if reseller + provider != fee:
        raise RevenueShareError(f"拆帳結果與總額不符：{reseller} + {provider} != {fee}")
    return {"fee": fee, "reseller": reseller, "provider": provider}


def check_fee_band(monthly_fee: Decimal, minimum: Decimal, maximum: Decimal) -> str | None:
    """子客戶月費是否落在書中 $1,000–$2,000/月/家 的區間內。"""
    if minimum > maximum:
        raise RevenueShareError(f"月費區間上下限顛倒：{minimum}-{maximum}")
    if minimum <= monthly_fee <= maximum:
        return None
    return (
        f"子客戶月費 {format_money(monthly_fee)} 落在書中區間 "
        f"{format_money(minimum)}–{format_money(maximum)} 之外"
    )


def aggregate(splits: list[dict[str, Decimal]]) -> dict[str, Decimal]:
    """加總多筆拆帳結果。空清單回傳三個零，不回 None。"""
    totals = {"fee": Decimal("0"), "reseller": Decimal("0"), "provider": Decimal("0")}
    for item in splits:
        for key in totals:
            totals[key] += item[key]
    return totals


def as_display(totals: dict[str, Decimal]) -> dict[str, str]:
    """把 Decimal 結果轉成可 JSON 序列化的字串（Decimal 不能直接進 json）。"""
    return {key: format_money(value) for key, value in totals.items()}
