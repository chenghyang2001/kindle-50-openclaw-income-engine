"""Stripe 資料源：訂閱與一次性收款的當日淨入帳。

`--live` 時對應 Stripe Charges / BalanceTransactions API；本檔只做 mock 讀檔。
這個資料源在 `test_main.py` 的 integration 測試中會被刻意打掛，用來驗證
「單源故障 → 報表照常產出並標記部分資料」的降級路徑。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from . import (
    SourceSnapshot,
    load_mock_payload,
    quantize_money,
    require_list,
    require_mapping,
    to_decimal,
)

SOURCE_ID = "stripe"
DISPLAY_NAME = "Stripe"

#: 只認 succeeded；failed / pending 的授權金額不是真的收到的錢。
SUCCEEDED_STATUS = "succeeded"


def fetch(mock_path: Path) -> SourceSnapshot:
    """回傳 Stripe 當日快照：營收 = 成功收款總額 - 金流手續費。

    扣掉手續費才是實際入帳，與 CRM / Shopify 的口徑一致（三者都是淨額）。
    """
    payload = load_mock_payload(mock_path, SOURCE_ID)
    charges = require_list(payload, "charges", SOURCE_ID, mock_path)

    collected = Decimal("0")
    succeeded_count = 0
    for item in charges:
        record = require_mapping(item, SOURCE_ID, "charges")
        if record.get("status") != SUCCEEDED_STATUS:
            continue
        collected += to_decimal(record.get("amount"), SOURCE_ID, "charges.amount")
        succeeded_count += 1

    fees = to_decimal(payload.get("fees", "0"), SOURCE_ID, "fees")

    return SourceSnapshot(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        revenue=quantize_money(collected - fees),
        order_count=succeeded_count,
        highlights={
            "收款毛額": f"{quantize_money(collected):,.2f}",
            "金流手續費": f"{quantize_money(fees):,.2f}",
            "失敗筆數": str(len(charges) - succeeded_count),
        },
    )
