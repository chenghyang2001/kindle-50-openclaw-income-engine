"""Shopify 資料源：電商當日訂單與淨營收。

`--live` 時對應 Shopify Admin API 的 Orders endpoint；本檔只做 mock 讀檔。
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

SOURCE_ID = "shopify"
DISPLAY_NAME = "Shopify"

#: 只有這個狀態的訂單才算當日營收；退款 / 待付款訂單一律排除。
PAID_STATUS = "paid"


def _paid_orders(orders: list) -> list[dict]:
    """挑出已付款訂單。狀態欄位缺漏視為未付款（保守估計，不高報營收）。"""
    paid = []
    for item in orders:
        record = require_mapping(item, SOURCE_ID, "orders")
        if record.get("status") == PAID_STATUS:
            paid.append(record)
    return paid


def fetch(mock_path: Path) -> SourceSnapshot:
    """回傳 Shopify 當日快照：營收 = 已付款訂單總額 - 當日退款總額。

    退款可能對應到前幾天的訂單，所以是獨立陣列而不是從 orders 扣；
    真實 Shopify 的 refunds 也是這個資料形狀。
    """
    payload = load_mock_payload(mock_path, SOURCE_ID)
    orders = require_list(payload, "orders", SOURCE_ID, mock_path)
    refunds = require_list(payload, "refunds", SOURCE_ID, mock_path)

    paid = _paid_orders(orders)
    gross = Decimal("0")
    for record in paid:
        gross += to_decimal(record.get("total"), SOURCE_ID, "orders.total")

    refunded = Decimal("0")
    for item in refunds:
        record = require_mapping(item, SOURCE_ID, "refunds")
        refunded += to_decimal(record.get("amount"), SOURCE_ID, "refunds.amount")

    return SourceSnapshot(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        revenue=quantize_money(gross - refunded),
        order_count=len(paid),
        highlights={
            "訂單毛額": f"{quantize_money(gross):,.2f}",
            "退款": f"{quantize_money(refunded):,.2f}",
        },
    )
