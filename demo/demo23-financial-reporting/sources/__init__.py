"""demo23 財務資料源套件：Xero / QuickBooks / Sage / 預算檔 / BambooHR 薪資。

這一層是本模組**三條財務鐵律**的第一道防線：

1. **唯讀（read scope）**：每個資料源都必須宣告 scope，並在任何取數動作之前
   通過 `assert_read_only_scope()`。scope 內只要出現 write / create / update /
   delete / post / modify / full / admin / manage 任一字樣，或根本沒有 read 字樣，
   一律拋 `ReadOnlyViolation` **中止整個流程**——不是警告、不是降級。
   `--live` 走 HTTP 時再由 `fetch_live_json()` 擋一次：非 GET / HEAD 直接拒絕。
   兩道守衛刻意重複：設定檔寫錯與程式碼寫錯是兩種不同的錯，各擋各的。
2. **金額一律 `Decimal`**：JSON / CSV 內的金額全部以字串儲存（`"412500.00"`
   而非 `412500.00`），避免 float 二進位誤差在 12 個月滾動預測裡被放大。
   本套件**不存在任何 float 運算**。
3. **幣別不可混加**：每個資料源都必須回報自己的 `currency`，由 `board_pack`
   在聚合前比對；不同幣別的資料源會被剔除並標示，絕不相加。

另外，取數失敗（檔案不存在、JSON 損毀、欄位缺漏、金額無法解析、現金流不平衡）
一律收斂成 `SourceError`，由上層標記「資料不完整」後照常出報表——但財務報表的
不完整警告比一般報表醒目得多，理由見 `board_pack.partial_banner()`。
"""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

#: 金額統一兩位小數。
TWO_PLACES = Decimal("0.01")
#: 百分比統一一位小數。
ONE_PLACE = Decimal("0.1")

#: 唯讀 HTTP 動詞白名單。財務系統的任何非讀取請求都不該從這個模組發出去。
READ_ONLY_HTTP_METHODS = frozenset({"GET", "HEAD"})

#: scope 字串中一旦出現這些字樣即視為具寫入能力，直接拒絕。
WRITE_SCOPE_MARKERS = (
    "write",
    "create",
    "update",
    "delete",
    "post",
    "modify",
    "full",
    "admin",
    "manage",
)

#: P&L 行的合法分類。other 保留給非營業損益（利息、匯兌）。
PNL_CATEGORIES = ("revenue", "cogs", "opex", "other")


class SourceError(RuntimeError):
    """單一資料源取數失敗。訊息必須指出是哪個資料源、哪個欄位、哪個檔案。"""


class ReadOnlyViolation(RuntimeError):
    """偵測到可能寫入財務系統的設定或呼叫。這是不可協商的錯誤，必須中止流程。"""


# --------------------------------------------------------------------------
# 唯讀守衛
# --------------------------------------------------------------------------


def assert_read_only_scope(source_id: str, scope: Any) -> str:
    """檢查資料源 scope 是唯讀。任何疑慮一律拒絕，不做「應該沒問題」的假設。"""
    if not isinstance(scope, str) or not scope.strip():
        raise ReadOnlyViolation(f"{source_id} 未宣告 scope；財務資料源必須明寫唯讀 scope")

    normalized = scope.strip().lower()
    hits = [marker for marker in WRITE_SCOPE_MARKERS if marker in normalized]
    if hits:
        raise ReadOnlyViolation(
            f"{source_id} 的 scope {scope!r} 含寫入字樣 {hits}；"
            "財務資料源一律唯讀，請改用 read scope 的憑證"
        )
    if "read" not in normalized:
        raise ReadOnlyViolation(
            f"{source_id} 的 scope {scope!r} 未包含 read；無法確認為唯讀，拒絕取數"
        )
    return scope.strip()


def fetch_live_json(
    url: str,
    headers: dict[str, str],
    method: str = "GET",
    timeout: int = 30,
) -> dict:
    """`--live` 時的唯讀 HTTP 取數。非 GET / HEAD 直接拒絕，不留任何寫入路徑。"""
    verb = str(method).upper()
    if verb not in READ_ONLY_HTTP_METHODS:
        raise ReadOnlyViolation(
            f"拒絕以 {verb} 呼叫財務 API {url}；本模組只允許 {sorted(READ_ONLY_HTTP_METHODS)}"
        )

    request = urllib.request.Request(url, headers=headers, method=verb)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SourceError(f"財務 API 回傳 HTTP {exc.code}：{url}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"財務 API 連線失敗：{url}｜{exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"財務 API 回應不是合法 JSON：{url}｜{exc}") from exc

    if not isinstance(payload, dict):
        raise SourceError(f"財務 API 回應頂層必須是物件：{url}")
    return payload


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PnLLine:
    """單一損益表科目行。金額全部 Decimal，缺值用 None 而非 0。

    budget 由預算資料源事後補上（`board_pack` 依 code 對應），因此這裡預設 None。
    """

    code: str
    label: str
    category: str
    actual: Decimal
    prior_year: Decimal | None = None


@dataclass(frozen=True)
class CashflowFacts:
    """現金流量三段式與期初期末餘額。"""

    opening_balance: Decimal
    operating: Decimal
    investing: Decimal
    financing: Decimal
    closing_balance: Decimal
    monthly_operating_outflow: Decimal


@dataclass(frozen=True)
class PipelineFacts:
    """業務管道與應收現況，供滾動預測使用。"""

    open_pipeline_value: Decimal
    weighted_pipeline_value: Decimal
    monthly_recurring_revenue: Decimal
    invoice_count: int
    overdue_receivables: Decimal


@dataclass(frozen=True)
class SourceFacts:
    """單一資料源的取數結果。各資料源只填自己負責的區塊，其餘留空。"""

    source_id: str
    display_name: str
    scope: str
    currency: str
    pnl_lines: tuple[PnLLine, ...] = ()
    budget_by_code: dict[str, Decimal] = field(default_factory=dict)
    cashflow: CashflowFacts | None = None
    pipeline: PipelineFacts | None = None
    highlights: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# 解析工具
# --------------------------------------------------------------------------


def quantize_money(value: Decimal) -> Decimal:
    """金額收斂到小數 2 位。用 ROUND_HALF_UP 而非 Decimal 預設的 ROUND_HALF_EVEN：
    財務報表的慣例是四捨五入，銀行家捨入會讓對帳的人算不出同一個數字。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def quantize_pct(value: Decimal) -> Decimal:
    """百分比收斂到小數 1 位（同樣 ROUND_HALF_UP）。"""
    return value.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)


def to_decimal(value: Any, source_id: str, field_name: str) -> Decimal:
    """把 JSON / CSV 取出的值轉成 Decimal。

    刻意拒絕 float 輸入：`Decimal(0.1)` 會得到 0.1000000000000000055511151231…，
    在 12 個月累加後足以讓董事會看到對不起來的尾差。資料檔請一律寫字串。
    """
    if isinstance(value, float):
        raise SourceError(
            f"{source_id} 的 {field_name} 是 float（{value!r}）；"
            "財務金額必須以字串儲存，避免二進位誤差"
        )
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SourceError(f"{source_id} 的 {field_name} 不是合法金額：{value!r}") from exc


def optional_decimal(value: Any, source_id: str, field_name: str) -> Decimal | None:
    """缺值回 None（不是 0）——「沒有去年同期」與「去年同期是 0」是兩件事。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return quantize_money(to_decimal(value, source_id, field_name))


def load_mock_payload(path: Path, source_id: str) -> dict:
    """讀取並解析 JSON 資料檔。三種失敗都轉成 SourceError 交給上層降級。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(f"{source_id} 無法讀取資料檔 {path}：{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError(f"{source_id} 的資料檔 JSON 解析失敗 {path}：{exc}") from exc

    if not isinstance(payload, dict):
        raise SourceError(f"{source_id} 的資料檔頂層必須是物件：{path}")
    return payload


def load_csv_rows(path: Path, source_id: str) -> list[dict[str, str]]:
    """讀取預算 CSV（`BUDGET_CSV_PATH` 指向的檔案）。"""
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except OSError as exc:
        raise SourceError(f"{source_id} 無法讀取 CSV {path}：{exc}") from exc
    except csv.Error as exc:
        raise SourceError(f"{source_id} 的 CSV 解析失敗 {path}：{exc}") from exc


def require_list(payload: dict, key: str, source_id: str, path: Path) -> list:
    """取出必要的陣列欄位；缺漏或型別不符一律 SourceError。"""
    value = payload.get(key)
    if not isinstance(value, list):
        raise SourceError(f"{source_id} 缺少陣列欄位 {key}：{path}")
    return value


def require_mapping(item: Any, source_id: str, field_name: str) -> dict:
    """陣列元素必須是物件，否則後續 `.get()` 會拋出難以理解的 AttributeError。"""
    if not isinstance(item, dict):
        raise SourceError(f"{source_id} 的 {field_name} 元素必須是物件：{item!r}")
    return item


def require_currency(payload: dict, source_id: str) -> str:
    """幣別是聚合安全的前提，缺了就不能用——寧可標成失敗資料源。"""
    currency = payload.get("currency")
    if not isinstance(currency, str) or len(currency.strip()) != 3:
        raise SourceError(f"{source_id} 缺少三碼幣別（currency），無法確認可否聚合")
    return currency.strip().upper()


def build_cashflow(payload: dict, source_id: str) -> CashflowFacts:
    """組出現金流量並做恆等式自檢：期初 + 營業 + 投資 + 融資 == 期末。

    這個自檢是刻意的：帳上串不起來的現金流送到董事會，比沒有現金流更糟——
    董事會會據以討論，但整段討論建立在錯的數字上。
    """
    block = require_mapping(payload.get("cashflow"), source_id, "cashflow")
    fields = ("opening_balance", "operating", "investing", "financing", "closing_balance")
    values = {
        name: quantize_money(to_decimal(block.get(name), source_id, f"cashflow.{name}"))
        for name in fields
    }
    computed = (
        values["opening_balance"]
        + values["operating"]
        + values["investing"]
        + values["financing"]
    )
    if computed != values["closing_balance"]:
        raise SourceError(
            f"{source_id} 現金流不平衡：期初+營業+投資+融資={computed}，"
            f"但期末為 {values['closing_balance']}"
        )
    outflow = quantize_money(
        to_decimal(block.get("monthly_operating_outflow"), source_id, "cashflow.monthly_operating_outflow")
    )
    if outflow < 0:
        raise SourceError(f"{source_id} 的 monthly_operating_outflow 不可為負：{outflow}")
    return CashflowFacts(monthly_operating_outflow=outflow, **values)


# 子模組匯入必須留在檔尾：各 source 會 `from . import SourceFacts`，
# 若提前 import，子模組會拿到尚未定義完成的套件名稱空間而 ImportError。
from . import (  # noqa: E402
    budget_source,
    payroll_source,
    quickbooks_source,
    sage_source,
    xero_source,
)

#: 資料源註冊表。`board_pack.collect()` 以 config.yaml 的 `sources[].id` 查表。
#: 測試可用 `monkeypatch.setitem(sources.FETCHERS, "xero", boom)` 模擬單源故障。
FETCHERS: dict[str, Callable[[Path], SourceFacts]] = {
    xero_source.SOURCE_ID: xero_source.fetch,
    quickbooks_source.SOURCE_ID: quickbooks_source.fetch,
    sage_source.SOURCE_ID: sage_source.fetch,
    budget_source.SOURCE_ID: budget_source.fetch,
    payroll_source.SOURCE_ID: payroll_source.fetch,
}

__all__ = [
    "CashflowFacts",
    "FETCHERS",
    "ONE_PLACE",
    "PNL_CATEGORIES",
    "PipelineFacts",
    "PnLLine",
    "ReadOnlyViolation",
    "SourceError",
    "SourceFacts",
    "TWO_PLACES",
    "assert_read_only_scope",
    "build_cashflow",
    "fetch_live_json",
    "load_csv_rows",
    "load_mock_payload",
    "optional_decimal",
    "quantize_money",
    "quantize_pct",
    "require_currency",
    "require_list",
    "require_mapping",
    "to_decimal",
]
