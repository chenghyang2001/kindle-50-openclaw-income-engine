"""每日 SKU 分析：流速、可售天數、STATUS 五級分類。

STATUS 五級與判定順序**逐字實作**附錄 G（apxG_p14）的
Daily SKU Analysis Prompt，順序本身就是規格的一部分：

    REORDER_URGENT      : days_on_hand < reorder_point
    REORDER_RECOMMENDED : days_on_hand < reorder_point * 1.5
    SLOW_MOVER          : velocity in bottom {{SLOW_MOVER_PERCENTILE}}%
                          for {{SLOW_MOVER_DAYS}} days
    OVERSTOCK           : days_on_hand > {{OVERSTOCK_DOH}}
    HEALTHY             : none of the above

先判缺貨再判滯銷是有意義的：一個滯銷了 30 天但今天剛好賣光的 SKU，
該做的事是「補貨」而不是「打折清倉」。順序調換會讓建議完全相反。

金額與流速一律走 `decimal.Decimal`。本模組的輸出會直接變成線上售價，
float 的 `0.1 + 0.2 != 0.3` 在這裡不是學術問題，是真的會少收錢。
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

MONEY_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.001")
DAYS_QUANT = Decimal("0.01")

STATUS_REORDER_URGENT = "REORDER_URGENT"
STATUS_REORDER_RECOMMENDED = "REORDER_RECOMMENDED"
STATUS_SLOW_MOVER = "SLOW_MOVER"
STATUS_OVERSTOCK = "OVERSTOCK"
STATUS_HEALTHY = "HEALTHY"

BAND_SLOW = "slow_mover"
BAND_NEUTRAL = "velocity_neutral"
BAND_FAST = "fast_mover"

# 分析階段就能認定的異常（會直接影響定價安全閥，必須進報告與稽核）
FLAG_NEGATIVE_MARGIN = "NEGATIVE_MARGIN"
FLAG_STOCKOUT = "STOCKOUT"
FLAG_MISSING_COMPETITOR = "MISSING_COMPETITOR_PRICE"


class AnalyserError(ValueError):
    """SKU 資料不合法或來源無法讀取"""


def _to_decimal(raw: Any, field: str) -> Decimal:
    """共用的 Decimal 轉換；bool 先擋掉（bool 是 int 的子類，True 會變成 1）。

    float 先經 `str()` 再進 Decimal，避免把二進位誤差帶進來
    （`Decimal(0.1)` 會是 0.1000000000000000055511151231257827）。
    """
    if isinstance(raw, bool):
        raise AnalyserError(f"{field} 不接受布林值：{raw!r}")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    elif isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except InvalidOperation as exc:
            raise AnalyserError(f"無法解析 {field}：{raw!r}") from exc
    else:
        raise AnalyserError(f"不支援的 {field} 型別：{type(raw).__name__}")
    if not value.is_finite():
        raise AnalyserError(f"{field} 必須是有限數值，收到 {raw!r}")
    return value


def to_money(raw: Any, field: str = "price") -> Decimal:
    """把價格轉成 Decimal（正數、兩位小數）"""
    value = _to_decimal(raw, field)
    if value <= 0:
        raise AnalyserError(f"{field} 必須為正數，收到 {raw!r}")
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def to_rate(raw: Any, field: str = "velocity") -> Decimal:
    """把流速／天數等非負數值轉成 Decimal（允許 0）"""
    value = _to_decimal(raw, field)
    if value < 0:
        raise AnalyserError(f"{field} 不可為負數，收到 {raw!r}")
    return value.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def to_signed_rate(raw: Any, field: str = "percent") -> Decimal:
    """把**可能為負**的百分比轉成 Decimal（刻意不做正負檢查）。

    與 `to_rate()` 的分工：
      - `to_rate()`  用於「負值＝資料壞掉」的欄位（流速、天數、庫存門檻），
                     負值直接拋錯是對的，因為那種資料不該繼續往下走。
      - `to_signed_rate()` 用於**設定檔驗證**，那裡的負值是要被「回報」的問題，
                     不是要被中止的例外——在轉換階段就拋錯，呼叫端只會看到
                     第一個錯誤，永遠拿不到完整的問題清單。
    """
    return _to_decimal(raw, field).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


def to_count(raw: Any, field: str = "current_stock") -> int:
    """把庫存數量轉成非負整數"""
    if isinstance(raw, bool):
        raise AnalyserError(f"{field} 不接受布林值：{raw!r}")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise AnalyserError(f"無法解析 {field}：{raw!r}") from exc
    if value < 0:
        raise AnalyserError(f"{field} 不可為負數，收到 {raw!r}")
    return value


@dataclass(frozen=True)
class SkuAnalysis:
    """單一 SKU 的每日分析結果。

    前 8 個欄位對應 Daily SKU Analysis Prompt 明列的回傳欄位，
    其後為本模組定價與稽核所需的補充欄位。
    """

    sku_id: str
    product_name: str
    current_stock: int
    avg_daily_velocity_7d: Decimal
    avg_daily_velocity_30d: Decimal
    days_on_hand: Decimal | None
    reorder_point: Decimal
    status: str
    velocity_band: str
    days_since_last_sale: int
    current_price: Decimal
    cost_price: Decimal
    competitor_price: Decimal | None
    category: str
    flags: tuple[str, ...]

    @property
    def is_stockout(self) -> bool:
        """庫存為 0（缺貨中）"""
        return self.current_stock == 0

    @property
    def has_negative_margin(self) -> bool:
        """現售價已經不高於成本價——這種 SKU 禁止任何自動降價"""
        return self.current_price <= self.cost_price

    def as_dict(self) -> dict[str, Any]:
        """轉成 JSON 可序列化形狀；金額保留字串，避免下游又退回 float"""
        return {
            "sku_id": self.sku_id,
            "product_name": self.product_name,
            "current_stock": self.current_stock,
            "avg_daily_velocity_7d": str(self.avg_daily_velocity_7d),
            "avg_daily_velocity_30d": str(self.avg_daily_velocity_30d),
            "days_on_hand": None if self.days_on_hand is None else str(self.days_on_hand),
            "reorder_point": str(self.reorder_point),
            "status": self.status,
            "velocity_band": self.velocity_band,
            "days_since_last_sale": self.days_since_last_sale,
            "current_price": str(self.current_price),
            "cost_price": str(self.cost_price),
            "competitor_price": (
                None if self.competitor_price is None else str(self.competitor_price)
            ),
            "category": self.category,
            "flags": list(self.flags),
        }


def days_on_hand(current_stock: int, velocity_7d: Decimal) -> Decimal | None:
    """可售天數 = 現有庫存 / 近 7 日平均日銷量。

    兩個特例刻意不用「除以 0 就給一個很大的數」草草帶過：
      - 庫存 0         -> 0 天（缺貨，最急）
      - 流速 0 且有庫存 -> None（賣不動，可售天數在數學上是無限大）
    用 None 表示無限，下游才能明確區分「賣不動」與「還能撐很久」。
    """
    if current_stock == 0:
        return Decimal("0")
    if velocity_7d <= 0:
        return None
    return (Decimal(current_stock) / velocity_7d).quantize(DAYS_QUANT, rounding=ROUND_HALF_UP)


def velocity_bands(
    velocities: list[Decimal], slow_percentile: int, fast_percentile: int
) -> tuple[Decimal, Decimal]:
    """用最近排名法（nearest-rank）算出慢速／快速的切點。

    回傳 (slow_cutoff, fast_cutoff)：v <= slow_cutoff 視為滯銷帶，
    v >= fast_cutoff 視為熱銷帶。SKU 數太少導致兩個切點交疊時，
    一律退回「全部視為中性」（以 -1 表示切點失效）——寧可不分類，
    也不要在 3 個 SKU 的樣本上宣稱誰是前 20%。
    """
    if not velocities:
        raise AnalyserError("沒有任何 SKU 可計算流速分位")
    ordered = sorted(velocities)
    size = len(ordered)
    slow_index = min(max(math.ceil(size * slow_percentile / 100), 1), size) - 1
    fast_index = size - min(max(math.ceil(size * fast_percentile / 100), 1), size)
    slow_cutoff = ordered[slow_index]
    fast_cutoff = ordered[fast_index]
    if fast_cutoff <= slow_cutoff:
        return Decimal("-1"), Decimal("-1")
    return slow_cutoff, fast_cutoff


def classify_band(velocity: Decimal, slow_cutoff: Decimal, fast_cutoff: Decimal) -> str:
    """依切點把流速歸入 slow / neutral / fast 三帶"""
    if slow_cutoff < 0 or fast_cutoff < 0:
        return BAND_NEUTRAL
    if velocity <= slow_cutoff:
        return BAND_SLOW
    if velocity >= fast_cutoff:
        return BAND_FAST
    return BAND_NEUTRAL


def classify_status(
    doh: Decimal | None,
    reorder_point: Decimal,
    band: str,
    days_since_last_sale: int,
    settings: dict[str, Any],
) -> str:
    """STATUS 五級分類，判定順序逐字照 Daily SKU Analysis Prompt。"""
    slow_days = to_count(settings.get("slow_mover_days", 14), "slow_mover_days")
    overstock_doh = to_rate(settings.get("overstock_doh", 90), "overstock_doh")
    multiplier = to_rate(
        settings.get("reorder_recommended_multiplier", "1.5"), "reorder_recommended_multiplier"
    )
    if doh is not None and doh < reorder_point:
        return STATUS_REORDER_URGENT
    if doh is not None and doh < reorder_point * multiplier:
        return STATUS_REORDER_RECOMMENDED
    if band == BAND_SLOW and days_since_last_sale >= slow_days:
        return STATUS_SLOW_MOVER
    if doh is None or doh > overstock_doh:
        return STATUS_OVERSTOCK
    return STATUS_HEALTHY


def _collect_flags(
    current_stock: int, current_price: Decimal, cost_price: Decimal, competitor: Decimal | None
) -> tuple[str, ...]:
    """標記分析階段就能認定的異常，供定價安全閥、報告與稽核使用"""
    flags: list[str] = []
    if current_stock == 0:
        flags.append(FLAG_STOCKOUT)
    if current_price <= cost_price:
        flags.append(FLAG_NEGATIVE_MARGIN)
    if competitor is None:
        flags.append(FLAG_MISSING_COMPETITOR)
    return tuple(flags)


def _analyse_one(
    record: dict[str, Any],
    competitor_prices: dict[str, Any],
    settings: dict[str, Any],
    slow_cutoff: Decimal,
    fast_cutoff: Decimal,
) -> SkuAnalysis:
    """把一筆原始 SKU 快照轉成分析結果"""
    sku_id = str(record.get("sku_id", "")).strip()
    if not sku_id:
        raise AnalyserError(f"SKU 缺少 sku_id：{record!r}")
    stock = to_count(record.get("current_stock"), f"{sku_id}.current_stock")
    velocity_7d = to_rate(record.get("avg_daily_velocity_7d", 0), f"{sku_id}.avg_daily_velocity_7d")
    doh = days_on_hand(stock, velocity_7d)
    stale_days = to_count(record.get("days_since_last_sale", 0), f"{sku_id}.days_since_last_sale")
    band = classify_band(velocity_7d, slow_cutoff, fast_cutoff)
    price = to_money(record.get("current_price"), f"{sku_id}.current_price")
    cost = to_money(record.get("cost_price"), f"{sku_id}.cost_price")
    raw_competitor = competitor_prices.get(sku_id)
    competitor = (
        None if raw_competitor is None else to_money(raw_competitor, f"{sku_id}.competitor_price")
    )
    reorder_point = to_rate(record.get("reorder_point", 0), f"{sku_id}.reorder_point")
    return SkuAnalysis(
        sku_id=sku_id,
        product_name=str(record.get("product_name", sku_id)),
        current_stock=stock,
        avg_daily_velocity_7d=velocity_7d,
        avg_daily_velocity_30d=to_rate(
            record.get("avg_daily_velocity_30d", 0), f"{sku_id}.avg_daily_velocity_30d"
        ),
        days_on_hand=doh,
        reorder_point=reorder_point,
        status=classify_status(doh, reorder_point, band, stale_days, settings),
        velocity_band=band,
        days_since_last_sale=stale_days,
        current_price=price,
        cost_price=cost,
        competitor_price=competitor,
        category=str(record.get("category", "uncategorised")),
        flags=_collect_flags(stock, price, cost, competitor),
    )


def analyse_skus(
    records: list[dict[str, Any]],
    competitor_prices: dict[str, Any],
    settings: dict[str, Any],
) -> list[SkuAnalysis]:
    """分析整批 SKU。流速分位需要看整體分布，所以必須整批一起算。"""
    if not records:
        raise AnalyserError("SKU 快照是空的，無法分析（請確認資料來源）")
    velocities = [
        to_rate(item.get("avg_daily_velocity_7d", 0), "avg_daily_velocity_7d") for item in records
    ]
    slow_cutoff, fast_cutoff = velocity_bands(
        velocities,
        to_count(settings.get("slow_mover_percentile", 30), "slow_mover_percentile"),
        to_count(settings.get("fast_mover_percentile", 20), "fast_mover_percentile"),
    )
    return [
        _analyse_one(record, competitor_prices, settings, slow_cutoff, fast_cutoff)
        for record in records
    ]


def summarise(analyses: list[SkuAnalysis]) -> dict[str, int]:
    """依 STATUS 統計，供報告開頭的一行摘要使用"""
    counts = {
        STATUS_REORDER_URGENT: 0,
        STATUS_REORDER_RECOMMENDED: 0,
        STATUS_SLOW_MOVER: 0,
        STATUS_OVERSTOCK: 0,
        STATUS_HEALTHY: 0,
    }
    for item in analyses:
        counts[item.status] = counts.get(item.status, 0) + 1
    counts["total"] = len(analyses)
    counts["stockouts"] = sum(1 for item in analyses if item.is_stockout)
    counts["negative_margin"] = sum(1 for item in analyses if item.has_negative_margin)
    return counts


def stale_candidates(analyses: list[SkuAnalysis], slow_mover_days: int) -> list[SkuAnalysis]:
    """挑出滯銷天數達門檻的 SKU（觸發 promotional_brief_generator 的依據）。

    門檻天數來自 config，預設 14——原著兩處數字不一致（ch07_p09 寫 21 天、
    apxG_p14 寫 14 天），本專案採 14 並讓它可設定，理由見 README。
    """
    return [
        item
        for item in analyses
        if item.velocity_band == BAND_SLOW and item.days_since_last_sale >= slow_mover_days
    ]


def load_json(path: Path) -> dict[str, Any]:
    """讀取離線快照 JSON；損毀時明確報錯，不退回空資料假裝一切正常。"""
    if not path.is_file():
        raise AnalyserError(f"找不到資料檔：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AnalyserError(f"資料檔無法解析：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise AnalyserError(f"資料檔最外層必須是物件：{path}")
    return payload


def fetch_live_inventory(
    shop_domain: str, admin_token: str, timeout: int = 30
) -> list[dict[str, Any]]:
    """`--live` 模式下的 Shopify Admin API 庫存讀取（唯讀 scope）。

    只用 `read_inventory` / `read_products`——寫回售價走另一條需要人工核准的
    路徑，讀取端永遠不該持有寫入權限。依契約用標準庫 urllib，不用 requests。
    """
    if not shop_domain or not admin_token:
        raise AnalyserError("缺少 Shopify 網域或 Admin API token，無法進入 --live 模式")
    url = f"https://{shop_domain}/admin/api/2024-10/products.json?limit=250"
    request = urllib.request.Request(
        url,
        headers={"X-Shopify-Access-Token": admin_token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise AnalyserError(f"Shopify 回傳 HTTP {exc.code}：{exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AnalyserError(f"無法連線 Shopify（{shop_domain}）：{exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AnalyserError(f"Shopify 回應不是合法 JSON：{exc}") from exc
    products = payload.get("products")
    if not isinstance(products, list):
        raise AnalyserError("Shopify 回應缺少 products 陣列")
    return products
