"""CRM 資料源：業務直簽成交金額與銷售管道（pipeline）階段概況。

`--live` 時對應 HubSpot / Pipedrive 之類的 Deals API；本檔只做 mock 讀檔，
但錯誤語意與線上版一致（取不到就 raise SourceError，不補 0）。
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

SOURCE_ID = "crm"
DISPLAY_NAME = "CRM"


def _sum_amounts(items: list, field_name: str) -> Decimal:
    """加總陣列中每個物件的 amount 欄位。"""
    total = Decimal("0")
    for item in items:
        record = require_mapping(item, SOURCE_ID, field_name)
        total += to_decimal(record.get("amount"), SOURCE_ID, f"{field_name}.amount")
    return total


def fetch(mock_path: Path) -> SourceSnapshot:
    """回傳 CRM 當日快照：營收 = 今日 closed_won 成交金額總和。

    銷售管道（pipeline）總值不計入營收——那是「還沒收到的錢」，
    混進當日營收會讓達成率虛胖，是這類報表最常見的信任殺手。
    """
    payload = load_mock_payload(mock_path, SOURCE_ID)
    closed_won = require_list(payload, "closed_won", SOURCE_ID, mock_path)
    pipeline = require_list(payload, "pipeline", SOURCE_ID, mock_path)

    revenue = quantize_money(_sum_amounts(closed_won, "closed_won"))
    pipeline_value = quantize_money(_sum_amounts(pipeline, "pipeline"))

    return SourceSnapshot(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        revenue=revenue,
        order_count=len(closed_won),
        highlights={
            "銷售管道總值": f"{pipeline_value:,.2f}",
            "管道階段數": str(len(pipeline)),
        },
    )
