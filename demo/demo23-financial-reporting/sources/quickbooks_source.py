"""QuickBooks 資料源：當期現金流量（營業 / 投資 / 融資）與期初期末餘額。

`--live` 對應 QuickBooks Reports/CashFlow API，scope 固定
`com.intuit.quickbooks.accounting (read)`（apxG_p08 逐字）。唯讀。
"""

from __future__ import annotations

from pathlib import Path

from . import (
    SourceFacts,
    build_cashflow,
    load_mock_payload,
    require_currency,
)

SOURCE_ID = "quickbooks"
DISPLAY_NAME = "QuickBooks"
SCOPE = "com.intuit.quickbooks.accounting (read)"


def fetch(path: Path) -> SourceFacts:
    """回傳現金流量事實。恆等式不平衡時由 `build_cashflow()` 拋 SourceError。"""
    payload = load_mock_payload(path, SOURCE_ID)
    currency = require_currency(payload, SOURCE_ID)
    cashflow = build_cashflow(payload, SOURCE_ID)

    return SourceFacts(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        scope=SCOPE,
        currency=currency,
        cashflow=cashflow,
        highlights={
            "期末現金": f"{cashflow.closing_balance:,.2f}",
            "月營業現金支出": f"{cashflow.monthly_operating_outflow:,.2f}",
        },
    )
