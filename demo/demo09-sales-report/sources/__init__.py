"""demo09 的資料源套件：CRM / Shopify / Stripe 三個獨立營收管道。

設計重點（第 03 章「單一資料源掛掉要標部分資料而非整份失敗」）：

1. **每個資料源都是可降級的**。任何取數失敗（檔案不存在、JSON 損毀、欄位缺漏、
   數字無法轉成 Decimal）一律收斂成 `SourceError`，由 `aggregator` 接住並標記
   「部分資料」，報表照常產出。資料源本身不做 `sys.exit`、不印警告、不回傳 0
   假裝成功——靜默補 0 會讓團隊誤以為當天真的沒賣出東西。
2. **金額一律 `Decimal`**。JSON 內的金額全部以字串儲存（`"1200.00"` 而非
   `1200.00`），避免 float 二進位誤差在月累計時被放大。
3. **三個資料源的營收互不重疊**：CRM = 業務直簽成交、Shopify = 電商訂單、
   Stripe = 訂閱收款。因此 `aggregator` 可以直接相加而不會重複計算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

TWO_PLACES = Decimal("0.01")


class SourceError(RuntimeError):
    """單一資料源取數失敗。訊息必須指出是哪個資料源、哪個欄位、哪個檔案。"""


@dataclass(frozen=True)
class SourceSnapshot:
    """單一資料源當日的取數結果。

    revenue:      該管道當日淨營收（已扣退款 / 手續費）
    order_count:  該管道當日成交筆數
    highlights:   給報表用的補充說明，key/value 皆為已格式化的字串
    """

    source_id: str
    display_name: str
    revenue: Decimal
    order_count: int
    highlights: dict[str, str] = field(default_factory=dict)


def quantize_money(value: Decimal) -> Decimal:
    """統一把金額收斂到小數 2 位。用 ROUND_HALF_UP 而非 Decimal 預設的
    ROUND_HALF_EVEN，因為財務報表的慣例是四捨五入，銀行家捨入會讓對帳的人困惑。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def to_decimal(value: Any, source_id: str, field_name: str) -> Decimal:
    """把 JSON 取出的值轉成 Decimal。先轉 str 再進 Decimal，避免 float 誤差。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SourceError(
            f"{source_id} 的 {field_name} 不是合法金額：{value!r}"
        ) from exc


def load_mock_payload(mock_path: Path, source_id: str) -> dict:
    """讀取並解析 mock JSON。

    三種失敗（讀不到檔、JSON 壞掉、頂層不是物件）都轉成 SourceError，
    讓上層走「部分資料」路徑；這也是 `--live` 時 API 逾時要對應的行為。
    """
    try:
        raw = mock_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(f"{source_id} 無法讀取資料檔 {mock_path}：{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError(f"{source_id} 的資料檔 JSON 解析失敗 {mock_path}：{exc}") from exc

    if not isinstance(payload, dict):
        raise SourceError(f"{source_id} 的資料檔頂層必須是物件：{mock_path}")
    return payload


def require_list(payload: dict, key: str, source_id: str, mock_path: Path) -> list:
    """取出必要的陣列欄位；缺漏或型別不符一律 SourceError。"""
    value = payload.get(key)
    if not isinstance(value, list):
        raise SourceError(f"{source_id} 缺少陣列欄位 {key}：{mock_path}")
    return value


def require_mapping(item: Any, source_id: str, field_name: str) -> dict:
    """陣列元素必須是物件，否則後續 `.get()` 會拋出難以理解的 AttributeError。"""
    if not isinstance(item, dict):
        raise SourceError(f"{source_id} 的 {field_name} 元素必須是物件：{item!r}")
    return item


# 子模組匯入必須留在檔尾：crm_source 等會 `from . import SourceSnapshot`，
# 若提前 import，子模組會拿到尚未定義完成的套件名稱空間而 ImportError。
from . import crm_source, shopify_source, stripe_source  # noqa: E402

#: 資料源註冊表。aggregator 以 config.yaml 的 `sources[].id` 查表取得 fetcher。
#: 測試可用 `monkeypatch.setitem(sources.FETCHERS, "stripe", boom)` 模擬單源故障。
FETCHERS: dict[str, Callable[[Path], SourceSnapshot]] = {
    crm_source.SOURCE_ID: crm_source.fetch,
    shopify_source.SOURCE_ID: shopify_source.fetch,
    stripe_source.SOURCE_ID: stripe_source.fetch,
}

__all__ = [
    "FETCHERS",
    "SourceError",
    "SourceSnapshot",
    "load_mock_payload",
    "quantize_money",
    "require_list",
    "require_mapping",
    "to_decimal",
]
