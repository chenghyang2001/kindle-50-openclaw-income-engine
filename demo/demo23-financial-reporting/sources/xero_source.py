"""Xero 資料源：當期損益表實際數（含去年同期）。

`--live` 對應 Xero Reports/ProfitAndLoss API，scope 固定 `accounting.transactions.read`
（附錄G apxG_p08 明列）。本檔只做唯讀取數，**沒有任何建立 / 更新 / 刪除的程式路徑**。
"""

from __future__ import annotations

from pathlib import Path

from . import (
    PNL_CATEGORIES,
    PnLLine,
    SourceError,
    SourceFacts,
    load_mock_payload,
    optional_decimal,
    quantize_money,
    require_currency,
    require_list,
    require_mapping,
    to_decimal,
)

SOURCE_ID = "xero"
DISPLAY_NAME = "Xero"
#: 唯讀 scope（apxG_p08 逐字）。改成任何含寫入字樣的值都會被 assert_read_only_scope 擋下。
SCOPE = "accounting.transactions.read"


def _parse_line(item: object, path: Path) -> PnLLine:
    """把一筆 JSON 科目行轉成 PnLLine，欄位缺漏一律 SourceError。"""
    record = require_mapping(item, SOURCE_ID, "pnl_lines")
    code = str(record.get("code", "")).strip()
    label = str(record.get("label", "")).strip()
    category = str(record.get("category", "")).strip().lower()

    if not code or not label:
        raise SourceError(f"{SOURCE_ID} 的科目行缺少 code 或 label：{record!r}（{path}）")
    if category not in PNL_CATEGORIES:
        raise SourceError(
            f"{SOURCE_ID} 的科目 {code} 分類不合法：{category!r}，"
            f"可用：{'/'.join(PNL_CATEGORIES)}"
        )

    return PnLLine(
        code=code,
        label=label,
        category=category,
        actual=quantize_money(to_decimal(record.get("actual"), SOURCE_ID, f"{code}.actual")),
        prior_year=optional_decimal(record.get("prior_year"), SOURCE_ID, f"{code}.prior_year"),
    )


def fetch(path: Path) -> SourceFacts:
    """回傳當期損益表科目行。重複的 code 直接報錯——重複科目會讓合計悄悄多算一次。"""
    payload = load_mock_payload(path, SOURCE_ID)
    currency = require_currency(payload, SOURCE_ID)
    lines = tuple(_parse_line(item, path) for item in require_list(payload, "pnl_lines", SOURCE_ID, path))

    codes = [line.code for line in lines]
    duplicates = sorted({code for code in codes if codes.count(code) > 1})
    if duplicates:
        raise SourceError(f"{SOURCE_ID} 有重複科目代碼 {duplicates}：{path}")

    return SourceFacts(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        scope=SCOPE,
        currency=currency,
        pnl_lines=lines,
        highlights={
            "科目行數": str(len(lines)),
            "帳務期間": str(payload.get("period", "—")),
        },
    )
