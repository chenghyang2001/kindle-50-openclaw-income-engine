"""BambooHR 薪資資料源：當期人事成本，併入損益表 opex。

`--live` 需要 `BAMBOOHR_API_KEY`（apxG_p08 逐字），同樣只讀不寫。

人事成本刻意獨立成一個資料源而不是併在 Xero 裡：薪資是多數企業最大的單一費用，
且經常在會計系統以彙總分錄入帳（看不到人頭數）。獨立取數才能在敘述裡回答
「費用增加是加人還是加薪」——那是董事會每個月都會問的第一個問題。
"""

from __future__ import annotations

from pathlib import Path

from . import (
    PnLLine,
    SourceError,
    SourceFacts,
    load_mock_payload,
    optional_decimal,
    quantize_money,
    require_currency,
    require_mapping,
    to_decimal,
)

SOURCE_ID = "payroll"
DISPLAY_NAME = "BambooHR 薪資"
SCOPE = "employees.payroll.read"


def fetch(path: Path) -> SourceFacts:
    """回傳單一 opex 科目行（人事成本）＋人頭數 highlight。"""
    payload = load_mock_payload(path, SOURCE_ID)
    currency = require_currency(payload, SOURCE_ID)
    block = require_mapping(payload.get("payroll"), SOURCE_ID, "payroll")

    code = str(block.get("code", "")).strip()
    label = str(block.get("label", "")).strip()
    if not code or not label:
        raise SourceError(f"{SOURCE_ID} 的 payroll 區塊缺少 code 或 label：{path}")

    headcount = block.get("headcount")
    if not isinstance(headcount, int) or headcount < 0:
        raise SourceError(f"{SOURCE_ID} 的 payroll.headcount 必須是非負整數：{headcount!r}")

    actual = quantize_money(to_decimal(block.get("actual"), SOURCE_ID, f"{code}.actual"))
    line = PnLLine(
        code=code,
        label=label,
        category="opex",
        actual=actual,
        prior_year=optional_decimal(block.get("prior_year"), SOURCE_ID, f"{code}.prior_year"),
    )

    per_head = quantize_money(actual / headcount) if headcount else None
    return SourceFacts(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        scope=SCOPE,
        currency=currency,
        pnl_lines=(line,),
        highlights={
            "在職人數": str(headcount),
            "平均人事成本": f"{per_head:,.2f}" if per_head is not None else "—",
        },
    )
