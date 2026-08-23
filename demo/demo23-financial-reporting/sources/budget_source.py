"""預算資料源：對應 apxG_p08 的 `BUDGET_CSV_PATH`（`openclaw config set BUDGET_CSV_PATH=...`）。

預算不是 API，是客戶財務團隊維護的一份檔案。因此本資料源同時吃兩種格式：

- `.csv`：`--live` 的正式來源（`BUDGET_CSV_PATH` 指向的檔案），欄位 `code,label,amount`
- `.json`：`--mock` 用的離線樣本（與 CSV 同語意）

沒有預算就沒有變異數分析，整份董事會報告會退化成「把上個月的數字念一遍」，
所以預算檔缺漏時要明確標成失敗資料源，不可靜默視為預算 0（那會讓每一行都是
+∞% 的天文變異數，警報全紅等於沒有警報）。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from . import (
    SourceError,
    SourceFacts,
    load_csv_rows,
    load_mock_payload,
    quantize_money,
    require_currency,
    require_list,
    require_mapping,
    to_decimal,
)

SOURCE_ID = "budget"
DISPLAY_NAME = "年度預算檔"
#: 本地檔案沒有 OAuth scope，仍以字串宣告唯讀，讓守衛規則對五個資料源一致適用。
SCOPE = "local_file.read"

CSV_REQUIRED_COLUMNS = ("code", "amount")


def _rows_from_csv(path: Path) -> tuple[list[tuple[str, Decimal]], str]:
    """讀 CSV 預算。幣別取自欄位 currency，沒有就預設與報表幣別相同的空字串。"""
    rows = load_csv_rows(path, SOURCE_ID)
    if not rows:
        raise SourceError(f"{SOURCE_ID} 的 CSV 沒有任何資料列：{path}")

    missing = [col for col in CSV_REQUIRED_COLUMNS if col not in rows[0]]
    if missing:
        raise SourceError(f"{SOURCE_ID} 的 CSV 缺少欄位 {missing}：{path}")

    currency = str(rows[0].get("currency", "")).strip().upper()
    parsed = [
        (str(row.get("code", "")).strip(), to_decimal(row.get("amount"), SOURCE_ID, "amount"))
        for row in rows
    ]
    return parsed, currency


def _rows_from_json(path: Path) -> tuple[list[tuple[str, Decimal]], str]:
    """讀 JSON 預算樣本。"""
    payload = load_mock_payload(path, SOURCE_ID)
    currency = require_currency(payload, SOURCE_ID)
    parsed = []
    for item in require_list(payload, "lines", SOURCE_ID, path):
        record = require_mapping(item, SOURCE_ID, "lines")
        code = str(record.get("code", "")).strip()
        parsed.append((code, to_decimal(record.get("amount"), SOURCE_ID, f"{code}.amount")))
    return parsed, currency


def fetch(path: Path) -> SourceFacts:
    """依副檔名選擇 CSV / JSON 解析，回傳 code -> 預算金額 的對照表。"""
    if str(path).startswith("${"):
        raise SourceError(
            f"{SOURCE_ID} 的路徑仍是未展開的環境變數 {path}；"
            "請先 `openclaw config set BUDGET_CSV_PATH=<path>` 或設定同名環境變數"
        )

    parsed, currency = (
        _rows_from_csv(path) if path.suffix.lower() == ".csv" else _rows_from_json(path)
    )

    budget: dict[str, Decimal] = {}
    for code, amount in parsed:
        if not code:
            raise SourceError(f"{SOURCE_ID} 有預算列缺少 code：{path}")
        if code in budget:
            raise SourceError(f"{SOURCE_ID} 有重複預算科目 {code}：{path}")
        budget[code] = quantize_money(amount)

    return SourceFacts(
        source_id=SOURCE_ID,
        display_name=DISPLAY_NAME,
        scope=SCOPE,
        currency=currency or "",
        budget_by_code=budget,
        highlights={"預算科目數": str(len(budget)), "來源檔": path.name},
    )
