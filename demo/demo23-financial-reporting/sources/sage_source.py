"""Sage 資料源：業務管道與應收現況，供 12 個月滾動預測使用。

`--live` 對應 Sage Accounting `sales_invoices (all read)`（apxG_p08 逐字）。唯讀。

為什麼預測的新業務基礎用「加權管道 (weighted pipeline)」而不是「總管道」：
總管道含大量不會成交的案子，直接乘轉換率會讓三種情境全部樂觀，
董事會拿到的 downside 情境會比實際的 base 還好看——那等於沒有 downside。
"""

from __future__ import annotations

from pathlib import Path

from . import (
    PipelineFacts,
    SourceError,
    SourceFacts,
    load_mock_payload,
    quantize_money,
    require_currency,
    require_mapping,
    to_decimal,
)

SOURCE_ID = "sage"
DISPLAY_NAME = "Sage"
SCOPE = "sales_invoices (all read)"


def fetch(path: Path) -> SourceFacts:
    """回傳管道事實。加權管道大於總管道視為資料錯誤，直接拒收。"""
    payload = load_mock_payload(path, SOURCE_ID)
    currency = require_currency(payload, SOURCE_ID)
    block = require_mapping(payload.get("pipeline"), SOURCE_ID, "pipeline")

    def _money(name: str):
        return quantize_money(to_decimal(block.get(name), SOURCE_ID, f"pipeline.{name}"))

    open_value = _money("open_pipeline_value")
    weighted = _money("weighted_pipeline_value")
    if weighted > open_value:
        raise SourceError(
            f"{SOURCE_ID} 加權管道 {weighted} 大於總管道 {open_value}；"
            "加權值必為總值的子集，資料有誤"
        )

    invoice_count = block.get("invoice_count")
    if not isinstance(invoice_count, int) or invoice_count < 0:
        raise SourceError(f"{SOURCE_ID} 的 pipeline.invoice_count 必須是非負整數：{invoice_count!r}")

    pipeline = PipelineFacts(
        open_pipeline_value=open_value,
        weighted_pipeline_value=weighted,
        monthly_recurring_revenue=_money("monthly_recurring_revenue"),
        invoice_count=invoice_count,
        overdue_receivables=_money("overdue_receivables"),
    )

    return SourceFacts(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        scope=SCOPE,
        currency=currency,
        pipeline=pipeline,
        highlights={
            "加權管道": f"{pipeline.weighted_pipeline_value:,.2f}",
            "月經常性營收": f"{pipeline.monthly_recurring_revenue:,.2f}",
            "逾期應收": f"{pipeline.overdue_receivables:,.2f}",
            "發票筆數": str(pipeline.invoice_count),
        },
    )
