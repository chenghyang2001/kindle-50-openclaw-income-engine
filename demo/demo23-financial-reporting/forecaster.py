"""12 個月三情境滾動預測（apxG_p09 的 `FORECAST_MODEL parameters`）。

簡報逐字給定的三組參數（一字不改地實作進 config.yaml 的 `forecast.scenarios`）：

| 情境 | pipeline_conversion | cost_assumption |
| --- | --- | --- |
| Base | 1.0 | flat |
| Upside | 1.2 | controlled_growth |
| Downside | 0.8 | cost_reduction |

**簡報只給了 `cost_assumption` 的標籤，沒有給對應的月成本增減率**，因此
`forecast.cost_growth` 的三個數字是本實作定義的預設假設（flat=0%、
controlled_growth=+1.5%/月、cost_reduction=-2.0%/月），已在 README 標明來源，
不冒充成簡報數字。客戶導入時應由財務總監覆寫成該公司的實際假設。

模型本身刻意保持極簡且可口頭複述——董事會裡沒有人會相信一個講不清楚的預測：

    當月營收 = 月經常性營收(MRR) + (加權管道 ÷ 預測月數) × pipeline_conversion
    當月成本 = 本期實際成本 × (1 + 月成本增減率) ^ 月序
    當月獲利 = 當月營收 - 當月成本
    月末現金 = 期末現金餘額 + 累計獲利

全程 `Decimal`，本檔沒有任何 float。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from sources import quantize_money

#: 簡報未提供、由本實作定義的預設月成本增減率。
DEFAULT_COST_GROWTH: dict[str, str] = {
    "flat": "0.000",
    "controlled_growth": "0.015",
    "cost_reduction": "-0.020",
}

#: 簡報逐字給定的三情境參數（config.yaml 缺漏時的後備值）。
DEFAULT_SCENARIOS: dict[str, dict[str, str]] = {
    "base": {"label": "Base", "pipeline_conversion": "1.0", "cost_assumption": "flat"},
    "upside": {"label": "Upside", "pipeline_conversion": "1.2", "cost_assumption": "controlled_growth"},
    "downside": {"label": "Downside", "pipeline_conversion": "0.8", "cost_assumption": "cost_reduction"},
}


class ForecastError(ValueError):
    """預測參數不合法（例如轉換率為負、預測月數 <= 0）。"""


def _ratio(value: Any, field_name: str) -> Decimal:
    """把設定檔中的「比率」轉成 Decimal。

    比率（轉換率、成本增減率）容許 YAML 寫成裸數字，金額則不容許（見
    `sources.to_decimal`）：比率是人工設定的假設值、位數少，`str(1.2)` 可精確還原；
    金額來自帳務系統且會被累加 12 次，float 的尾差會累積到報表上看得見。
    """
    try:
        return Decimal(str(value).strip())
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ForecastError(f"forecast.{field_name} 不是合法數字：{value!r}") from exc


@dataclass(frozen=True)
class ScenarioParams:
    """單一情境的參數。"""

    name: str
    label: str
    pipeline_conversion: Decimal
    cost_assumption: str
    monthly_cost_growth: Decimal


@dataclass(frozen=True)
class ForecastMonth:
    """單月預測結果。"""

    label: str
    revenue: Decimal
    cost: Decimal
    profit: Decimal
    closing_cash: Decimal

    def to_dict(self) -> dict[str, str]:
        """轉成 JSON-safe 結構。"""
        return {
            "label": self.label,
            "revenue": str(self.revenue),
            "cost": str(self.cost),
            "profit": str(self.profit),
            "closing_cash": str(self.closing_cash),
        }


@dataclass(frozen=True)
class ScenarioForecast:
    """單一情境的 12 個月結果與彙總。"""

    params: ScenarioParams
    months: tuple[ForecastMonth, ...]
    total_revenue: Decimal
    total_profit: Decimal
    min_closing_cash: Decimal
    min_cash_month: str

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構。"""
        return {
            "name": self.params.name,
            "label": self.params.label,
            "pipeline_conversion": str(self.params.pipeline_conversion),
            "cost_assumption": self.params.cost_assumption,
            "monthly_cost_growth": str(self.params.monthly_cost_growth),
            "months": [month.to_dict() for month in self.months],
            "total_revenue": str(self.total_revenue),
            "total_profit": str(self.total_profit),
            "min_closing_cash": str(self.min_closing_cash),
            "min_cash_month": self.min_cash_month,
        }


@dataclass(frozen=True)
class RollingForecast:
    """三情境滾動預測整體。"""

    period: str
    currency: str
    horizon_months: int
    recurring_revenue: Decimal
    weighted_pipeline: Decimal
    monthly_cost_base: Decimal
    opening_cash: Decimal
    scenarios: tuple[ScenarioForecast, ...]

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（併入 BoardPack JSON 一起餵給 LLM）。"""
        return {
            "period": self.period,
            "currency": self.currency,
            "horizon_months": self.horizon_months,
            "assumptions": {
                "recurring_revenue": str(self.recurring_revenue),
                "weighted_pipeline": str(self.weighted_pipeline),
                "monthly_cost_base": str(self.monthly_cost_base),
                "opening_cash": str(self.opening_cash),
            },
            "scenarios": [item.to_dict() for item in self.scenarios],
        }


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def load_scenarios(config: dict | None) -> tuple[ScenarioParams, ...]:
    """從 config.yaml 的 `forecast` 區塊讀三情境參數，缺漏用簡報原值後備。"""
    block = config or {}
    raw_scenarios = block.get("scenarios") or DEFAULT_SCENARIOS
    if not isinstance(raw_scenarios, dict):
        raise ForecastError(
            f"forecast.scenarios 必須是 mapping（情境名稱 -> 參數），實際為 {type(raw_scenarios).__name__}"
        )
    growth_map = {**DEFAULT_COST_GROWTH, **(block.get("cost_growth") or {})}

    scenarios: list[ScenarioParams] = []
    for name, raw in raw_scenarios.items():
        entry = raw if isinstance(raw, dict) else {}
        conversion = _ratio(entry.get("pipeline_conversion", "1.0"), f"{name}.pipeline_conversion")
        if conversion < 0:
            raise ForecastError(f"情境 {name} 的 pipeline_conversion 不可為負：{conversion}")

        assumption = str(entry.get("cost_assumption", "flat")).strip()
        if assumption not in growth_map:
            raise ForecastError(
                f"情境 {name} 的 cost_assumption {assumption!r} 沒有對應的月成本增減率，"
                f"請在 forecast.cost_growth 補上（現有：{sorted(growth_map)}）"
            )

        scenarios.append(
            ScenarioParams(
                name=str(name),
                label=str(entry.get("label", str(name).title())),
                pipeline_conversion=conversion,
                cost_assumption=assumption,
                monthly_cost_growth=_ratio(growth_map[assumption], f"cost_growth.{assumption}"),
            )
        )
    return tuple(scenarios)


def month_labels(period: str, horizon_months: int) -> tuple[str, ...]:
    """由本期（`YYYY-MM`）往後推 N 個月的標籤。刻意自行推算，不引入 dateutil。"""
    try:
        year_text, month_text = str(period).split("-", 1)
        year, month = int(year_text), int(month_text)
    except (ValueError, AttributeError) as exc:
        raise ForecastError(f"reporting.period 必須是 YYYY-MM 格式，收到 {period!r}") from exc
    if not 1 <= month <= 12:
        raise ForecastError(f"reporting.period 的月份不合法：{period!r}")

    labels = []
    for step in range(1, horizon_months + 1):
        total = (year * 12 + (month - 1)) + step
        labels.append(f"{total // 12:04d}-{total % 12 + 1:02d}")
    return tuple(labels)


# --------------------------------------------------------------------------
# 計算
# --------------------------------------------------------------------------


def _build_scenario(
    params: ScenarioParams,
    labels: tuple[str, ...],
    recurring_revenue: Decimal,
    pipeline_per_month: Decimal,
    monthly_cost_base: Decimal,
    opening_cash: Decimal,
) -> ScenarioForecast:
    """跑完單一情境的 N 個月。

    成本以「未量化的連乘值」滾動、只在輸出時量化：每月都拿量化後的值再乘一次，
    12 個月後的捨入誤差會累積到看得見的程度。
    """
    months: list[ForecastMonth] = []
    revenue = quantize_money(recurring_revenue + pipeline_per_month * params.pipeline_conversion)
    running_cost = monthly_cost_base
    cash = opening_cash

    for label in labels:
        running_cost = running_cost * (Decimal("1") + params.monthly_cost_growth)
        cost = quantize_money(running_cost)
        profit = quantize_money(revenue - cost)
        cash = quantize_money(cash + profit)
        months.append(ForecastMonth(label=label, revenue=revenue, cost=cost, profit=profit, closing_cash=cash))

    lowest = min(months, key=lambda item: item.closing_cash)
    return ScenarioForecast(
        params=params,
        months=tuple(months),
        total_revenue=quantize_money(sum((m.revenue for m in months), Decimal("0"))),
        total_profit=quantize_money(sum((m.profit for m in months), Decimal("0"))),
        min_closing_cash=lowest.closing_cash,
        min_cash_month=lowest.label,
    )


def build_forecast(
    period: str,
    currency: str,
    horizon_months: int,
    recurring_revenue: Decimal,
    weighted_pipeline: Decimal,
    monthly_cost_base: Decimal,
    opening_cash: Decimal,
    scenarios: Iterable[ScenarioParams],
) -> RollingForecast:
    """產出三情境 × N 個月的滾動預測。"""
    if horizon_months <= 0:
        raise ForecastError(f"forecast.horizon_months 必須大於 0，收到 {horizon_months}")

    labels = month_labels(period, horizon_months)
    pipeline_per_month = quantize_money(weighted_pipeline / Decimal(horizon_months))

    return RollingForecast(
        period=period,
        currency=currency,
        horizon_months=horizon_months,
        recurring_revenue=recurring_revenue,
        weighted_pipeline=weighted_pipeline,
        monthly_cost_base=monthly_cost_base,
        opening_cash=opening_cash,
        scenarios=tuple(
            _build_scenario(
                params, labels, recurring_revenue, pipeline_per_month, monthly_cost_base, opening_cash
            )
            for params in scenarios
        ),
    )


# --------------------------------------------------------------------------
# 排版
# --------------------------------------------------------------------------


def render_forecast(forecast: RollingForecast) -> str:
    """12-MONTH ROLLING FORECAST 區塊。逐月列三情境營收，再給每個情境的彙總。"""
    cur = forecast.currency
    lines = [
        f"【4. {forecast.horizon_months}-MONTH ROLLING FORECAST】",
        f"  假設：MRR {cur} {forecast.recurring_revenue:,.2f}"
        f"｜加權管道 {cur} {forecast.weighted_pipeline:,.2f}"
        f"｜月成本基準 {cur} {forecast.monthly_cost_base:,.2f}",
        "  月份｜" + "｜".join(f"{item.params.label} 營收" for item in forecast.scenarios),
    ]

    for index in range(forecast.horizon_months):
        cells = "｜".join(
            f"{item.months[index].revenue:,.2f}" for item in forecast.scenarios
        )
        lines.append(f"  {forecast.scenarios[0].months[index].label}｜{cells}")

    lines.append("  " + "─" * 60)
    for item in forecast.scenarios:
        lines.append(
            f"  {item.params.label}（轉換率 {item.params.pipeline_conversion}"
            f"｜成本假設 {item.params.cost_assumption} {item.params.monthly_cost_growth:+}/月）："
            f"總營收 {cur} {item.total_revenue:,.2f}"
            f"｜總獲利 {cur} {item.total_profit:,.2f}"
            f"｜最低現金 {cur} {item.min_closing_cash:,.2f}（{item.min_cash_month}）"
        )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_COST_GROWTH",
    "DEFAULT_SCENARIOS",
    "ForecastError",
    "ForecastMonth",
    "RollingForecast",
    "ScenarioForecast",
    "ScenarioParams",
    "build_forecast",
    "load_scenarios",
    "month_labels",
    "render_forecast",
]
