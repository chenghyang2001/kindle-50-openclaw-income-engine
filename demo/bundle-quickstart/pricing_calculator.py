"""快速啟動方案報價機 — 第 04 章「Bundling Economics: Features vs. Experience」。

依「產業 + 模組組合」算出單賣總價、打包價、客戶年度價值、回收期與首年 ROI。

三個不可妥協的設計：

1. **金額一律 ``Decimal``，全檔禁止 ``float``**。報價會直接寫進提案與合約，
   ``0.1 + 0.2 != 0.3`` 這種誤差在年度加總時會被放大成客戶看得見的對不上帳。
2. **打包不是折扣，是降低決策摩擦**。模組組合恰為 {1, 2, 5, 9} 時直接套用
   書中的快速啟動方案定價（$995 + $200/mo），而不是把單價乘上某個折扣率——
   這個價格是產品定義，不是算出來的。
3. **未知產業要出聲**。查不到的產業一律退回預設時薪並附警告，
   絕不靜默用預設值假裝算對了（客戶時薪錯一倍，ROI 就錯一倍）。

用法：
    python pricing_calculator.py
    python pricing_calculator.py --industry ecommerce --modules 1,2,5,9
    python pricing_calculator.py --modules 1,2,3,4,5 --hourly-rate 120 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# --------------------------------------------------------------------------- #
# 定價表（10 個模組，數字來自第 03/04 章與各 demo 的 config.yaml）
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModulePricing:
    """單一模組的定價與價值資料。"""

    module_id: int
    name: str
    setup: Decimal
    monthly: Decimal
    recovered_hours: Decimal


PRICING_TABLE: dict[int, ModulePricing] = {
    1: ModulePricing(1, "晨間情報簡報", Decimal("300"), Decimal("75"), Decimal("35")),
    2: ModulePricing(2, "收件匣清零代理", Decimal("400"), Decimal("100"), Decimal("33")),
    3: ModulePricing(3, "會議紀錄與行動提取", Decimal("350"), Decimal("85"), Decimal("11")),
    4: ModulePricing(4, "社群媒體內容排程", Decimal("350"), Decimal("90"), Decimal("26")),
    5: ModulePricing(5, "客戶評價監控", Decimal("300"), Decimal("80"), Decimal("11")),
    6: ModulePricing(6, "發票處理與費用分類", Decimal("350"), Decimal("85"), Decimal("8")),
    7: ModulePricing(7, "預約排程器", Decimal("300"), Decimal("75"), Decimal("10")),
    8: ModulePricing(8, "競品價格監控警報", Decimal("280"), Decimal("70"), Decimal("11")),
    9: ModulePricing(9, "每日銷售與進度報表", Decimal("300"), Decimal("80"), Decimal("11")),
    10: ModulePricing(10, "客戶跟進序列自動化", Decimal("350"), Decimal("90"), Decimal("12")),
}

# 書中的快速啟動方案：這四個模組合起來就是「客戶的一個週一早晨」。
QUICKSTART_MODULE_IDS: frozenset[int] = frozenset({1, 2, 5, 9})
QUICKSTART_SETUP = Decimal("995")
QUICKSTART_MONTHLY = Decimal("200")
QUICKSTART_PLAN_NAME = "快速啟動方案（The Quick-Start Experience）"

# 非書中組合的通用折扣：4 個模組以上打 8 折（可用 CLI 覆寫）。
DEFAULT_DISCOUNT_RATE = Decimal("0.8")
DEFAULT_DISCOUNT_MIN_MODULES = 4

# 產業預設時薪（USD/hr）。這是「客戶自己的時間值多少」，不是我們的收費。
INDUSTRY_HOURLY_RATES: dict[str, Decimal] = {
    "ecommerce": Decimal("45"),
    "legal": Decimal("250"),
    "saas": Decimal("85"),
    "clinic": Decimal("120"),
    "consulting": Decimal("150"),
    "other": Decimal("60"),
}
INDUSTRY_LABELS: dict[str, str] = {
    "ecommerce": "電商",
    "legal": "法律事務所",
    "saas": "SaaS",
    "clinic": "診所",
    "consulting": "顧問",
    "other": "一般中小企業",
}
DEFAULT_INDUSTRY = "other"

CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "US$",
    "TWD": "NT$",
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
}

MONTHS_PER_YEAR = Decimal("12")
HUNDRED = Decimal("100")
MONEY_PLACES = Decimal("0.01")
RATIO_PLACES = Decimal("0.1")


# --------------------------------------------------------------------------- #
# 數值工具
# --------------------------------------------------------------------------- #
def money(value: Decimal) -> Decimal:
    """金額一律四捨五入到分位。銀行家捨入會讓報價單少一分錢，客戶會問。"""
    return value.quantize(MONEY_PLACES, rounding=ROUND_HALF_UP)


def ratio(value: Decimal) -> Decimal:
    """比率／百分比統一取到小數一位。"""
    return value.quantize(RATIO_PLACES, rounding=ROUND_HALF_UP)


def format_money(value: Decimal, currency: str) -> str:
    """帶幣別符號的金額字串；未知幣別退回「代碼 + 空格」的中性寫法。"""
    symbol = CURRENCY_SYMBOLS.get(currency)
    amount = f"{money(value):,.2f}"
    return f"{symbol}{amount}" if symbol else f"{currency} {amount}"


def to_decimal(raw: str) -> Decimal:
    """把 CLI 傳進來的字串轉成 Decimal；不合法就明確報錯，不回 0 掩蓋。"""
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"無法解析為數字：{raw!r}") from exc


# --------------------------------------------------------------------------- #
# 參數解析
# --------------------------------------------------------------------------- #
def parse_module_ids(raw: str) -> list[int]:
    """把 "1,2,5,9" 解析成排序後的模組編號清單，並驗證每個編號都在定價表內。"""
    tokens = [token.strip() for token in str(raw).split(",") if token.strip()]
    if not tokens:
        raise ValueError("--modules 不可為空，至少要選一個模組")
    ids: set[int] = set()
    for token in tokens:
        if not token.isdigit():
            raise ValueError(f"模組編號必須是正整數，收到：{token!r}")
        module_id = int(token)
        if module_id not in PRICING_TABLE:
            raise ValueError(
                f"未知的模組編號 {module_id}；可用編號：{sorted(PRICING_TABLE)}"
            )
        ids.add(module_id)
    return sorted(ids)


def resolve_hourly_rate(industry: str, override: str | None) -> tuple[Decimal, str, list[str]]:
    """決定客戶時薪。回傳（時薪, 正規化後的產業代碼, 警告清單）。"""
    warnings: list[str] = []
    key = str(industry).strip().lower()
    if key not in INDUSTRY_HOURLY_RATES:
        warnings.append(
            f"未知產業 {industry!r}，已退回預設值 {DEFAULT_INDUSTRY}"
            f"（時薪 {INDUSTRY_HOURLY_RATES[DEFAULT_INDUSTRY]}）；"
            f"可用產業：{sorted(INDUSTRY_HOURLY_RATES)}"
        )
        key = DEFAULT_INDUSTRY
    rate = INDUSTRY_HOURLY_RATES[key]
    if override is not None:
        rate = to_decimal(override)
        if rate <= 0:
            raise ValueError(f"--hourly-rate 必須大於 0，收到：{override!r}")
    return rate, key, warnings


# --------------------------------------------------------------------------- #
# 定價計算
# --------------------------------------------------------------------------- #
def sum_list_price(module_ids: list[int]) -> tuple[Decimal, Decimal, Decimal]:
    """單賣加總：（設定費, 月費, 每月回收時數）。"""
    setup = sum((PRICING_TABLE[i].setup for i in module_ids), Decimal("0"))
    monthly = sum((PRICING_TABLE[i].monthly for i in module_ids), Decimal("0"))
    hours = sum((PRICING_TABLE[i].recovered_hours for i in module_ids), Decimal("0"))
    return setup, monthly, hours


def resolve_bundle_price(
    module_ids: list[int],
    list_setup: Decimal,
    list_monthly: Decimal,
    discount_rate: Decimal,
    discount_min_modules: int,
) -> tuple[Decimal, Decimal, str, str]:
    """決定打包價。回傳（設定費, 月費, 方案名稱, 定價依據說明）。"""
    if set(module_ids) == QUICKSTART_MODULE_IDS:
        return (
            QUICKSTART_SETUP,
            QUICKSTART_MONTHLY,
            QUICKSTART_PLAN_NAME,
            "第 04 章書定價（此組合是一個體驗，不是四個功能的折扣）",
        )
    if len(module_ids) >= discount_min_modules:
        return (
            money(list_setup * discount_rate),
            money(list_monthly * discount_rate),
            f"自訂組合（{len(module_ids)} 模組）",
            f"{discount_min_modules} 個模組以上套用 {ratio(discount_rate * HUNDRED)}% 定價",
        )
    return (
        list_setup,
        list_monthly,
        f"單獨銷售（{len(module_ids)} 模組）",
        f"未達 {discount_min_modules} 個模組門檻，不套用打包定價",
    )


def compute_value(
    hours_per_month: Decimal,
    hourly_rate: Decimal,
    setup: Decimal,
    monthly: Decimal,
) -> dict[str, Decimal | None]:
    """算出客戶端的價值面數字：月價值、年價值、年投入、回收期、首年 ROI。"""
    monthly_value = hours_per_month * hourly_rate
    annual_value = monthly_value * MONTHS_PER_YEAR
    annual_cost = setup + monthly * MONTHS_PER_YEAR
    net_monthly = monthly_value - monthly
    return {
        "monthly_value": money(monthly_value),
        "annual_value": money(annual_value),
        "annual_cost": money(annual_cost),
        "annual_net_gain": money(annual_value - annual_cost),
        # 淨月效益 <= 0 代表「這筆錢永遠回不來」，回 None 而不是 0 或無限大。
        "payback_months": ratio(setup / net_monthly) if net_monthly > 0 else None,
        "roi_pct": ratio((annual_value - annual_cost) / annual_cost * HUNDRED)
        if annual_cost > 0
        else None,
    }


def build_quote(
    module_ids: list[int],
    industry: str,
    hourly_rate: Decimal,
    currency: str,
    discount_rate: Decimal,
    discount_min_modules: int,
    warnings: list[str],
) -> dict:
    """組出完整報價結構（所有金額仍是 Decimal，序列化交給 quote_to_json）。"""
    list_setup, list_monthly, hours = sum_list_price(module_ids)
    setup, monthly, plan_name, basis = resolve_bundle_price(
        module_ids, list_setup, list_monthly, discount_rate, discount_min_modules
    )
    return {
        "plan_name": plan_name,
        "pricing_basis": basis,
        "currency": currency,
        "industry": industry,
        "industry_label": INDUSTRY_LABELS.get(industry, industry),
        "hourly_rate": money(hourly_rate),
        "modules": [
            {
                "module_id": PRICING_TABLE[i].module_id,
                "name": PRICING_TABLE[i].name,
                "setup": money(PRICING_TABLE[i].setup),
                "monthly": money(PRICING_TABLE[i].monthly),
                "recovered_hours": PRICING_TABLE[i].recovered_hours,
            }
            for i in module_ids
        ],
        "list_setup": money(list_setup),
        "list_monthly": money(list_monthly),
        "bundle_setup": money(setup),
        "bundle_monthly": money(monthly),
        "setup_savings": money(list_setup - setup),
        "monthly_savings": money(list_monthly - monthly),
        "recovered_hours_per_month": hours,
        "value": compute_value(hours, hourly_rate, setup, monthly),
        "warnings": list(warnings),
    }


# --------------------------------------------------------------------------- #
# 輸出
# --------------------------------------------------------------------------- #
def render_module_lines(quote: dict) -> list[str]:
    """列出被選中的模組與各自單價。"""
    currency = str(quote["currency"])
    return [
        f"  #{item['module_id']:<2} {item['name']}"
        f"｜設定 {format_money(item['setup'], currency)}"
        f"｜月費 {format_money(item['monthly'], currency)}"
        f"｜回收 {item['recovered_hours']} hrs/mo"
        for item in quote["modules"]
    ]


def render_value_lines(quote: dict) -> list[str]:
    """客戶價值區塊：把「省下的時間」翻譯成客戶聽得懂的錢。"""
    currency = str(quote["currency"])
    value = quote["value"]
    payback = value["payback_months"]
    roi = value["roi_pct"]
    return [
        f"  每月回收工時   {quote['recovered_hours_per_month']} 小時",
        f"  每月價值       {format_money(value['monthly_value'], currency)}",
        f"  年度價值       {format_money(value['annual_value'], currency)}",
        f"  年度投入       {format_money(value['annual_cost'], currency)}（首年營收）",
        f"  年度淨效益     {format_money(value['annual_net_gain'], currency)}",
        f"  投資回收期     {payback} 個月" if payback is not None else "  投資回收期     無法回收（每月價值低於月費）",
        f"  首年 ROI       {roi}%" if roi is not None else "  首年 ROI       無法計算（年度投入為 0）",
    ]


def render_text(quote: dict) -> str:
    """人看的報價單。給客戶看的版本請用 proposal-template.md，這份是內部算帳用。"""
    currency = str(quote["currency"])
    lines = [
        "=" * 60,
        f"報價單：{quote['plan_name']}",
        "=" * 60,
        f"產業：{quote['industry']}（{quote['industry_label']}）"
        f"｜客戶時薪：{format_money(quote['hourly_rate'], currency)}/hr",
        f"定價依據：{quote['pricing_basis']}",
        "",
        "選定模組",
        *render_module_lines(quote),
        "",
        "單獨銷售（Features）",
        f"  設定費  {format_money(quote['list_setup'], currency)}",
        f"  月費    {format_money(quote['list_monthly'], currency)}",
        "",
        "打包價（Experience）",
        f"  設定費  {format_money(quote['bundle_setup'], currency)}"
        f"（省 {format_money(quote['setup_savings'], currency)}）",
        f"  月費    {format_money(quote['bundle_monthly'], currency)}"
        f"（省 {format_money(quote['monthly_savings'], currency)}）",
        "",
        "客戶價值",
        *render_value_lines(quote),
    ]
    if quote["warnings"]:
        lines.extend(["", "警告"])
        lines.extend(f"  ! {text}" for text in quote["warnings"])
    lines.append("=" * 60)
    return "\n".join(lines)


def quote_to_json(quote: dict) -> str:
    """Decimal 一律轉字串再序列化，保住精度（float 會偷偷改掉小數）。"""
    return json.dumps(quote, ensure_ascii=False, indent=2, default=str)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器。"""
    parser = argparse.ArgumentParser(
        prog="pricing_calculator",
        description="依產業與模組組合計算快速啟動方案報價、客戶價值與 ROI",
    )
    parser.add_argument(
        "--modules",
        default="1,2,5,9",
        help="模組編號，逗號分隔（預設 1,2,5,9 = 書中的快速啟動方案）",
    )
    parser.add_argument(
        "--industry",
        default=DEFAULT_INDUSTRY,
        help=f"客戶產業，決定預設時薪；可用值：{sorted(INDUSTRY_HOURLY_RATES)}",
    )
    parser.add_argument("--currency", default="USD", help="幣別代碼，預設 USD")
    parser.add_argument(
        "--hourly-rate",
        default=None,
        help="客戶時薪（覆寫產業預設值），用於計算回收價值與 ROI",
    )
    parser.add_argument(
        "--discount-rate",
        default=str(DEFAULT_DISCOUNT_RATE),
        help=f"非書中組合的打包折扣率，預設 {DEFAULT_DISCOUNT_RATE}（8 折）",
    )
    parser.add_argument(
        "--discount-min-modules",
        type=int,
        default=DEFAULT_DISCOUNT_MIN_MODULES,
        help=f"套用折扣的最少模組數，預設 {DEFAULT_DISCOUNT_MIN_MODULES}",
    )
    parser.add_argument("--json", action="store_true", help="輸出 JSON 而非人讀報價單")
    return parser


def run(args: argparse.Namespace) -> dict:
    """執行報價計算並回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    module_ids = parse_module_ids(args.modules)
    hourly_rate, industry, warnings = resolve_hourly_rate(
        args.industry, getattr(args, "hourly_rate", None)
    )
    discount_rate = to_decimal(args.discount_rate)
    if not Decimal("0") < discount_rate <= Decimal("1"):
        raise ValueError(f"--discount-rate 必須介於 0（不含）到 1 之間，收到：{args.discount_rate!r}")
    return build_quote(
        module_ids=module_ids,
        industry=industry,
        hourly_rate=hourly_rate,
        currency=str(args.currency).strip().upper(),
        discount_rate=discount_rate,
        discount_min_modules=int(args.discount_min_modules),
        warnings=warnings,
    )


def main() -> int:
    """解析參數 → run() → 印出報價 → 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        quote = run(args)
    except (ValueError, KeyError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    print(quote_to_json(quote) if args.json else render_text(quote))
    for warning in quote["warnings"]:
        print(f"警告：{warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
