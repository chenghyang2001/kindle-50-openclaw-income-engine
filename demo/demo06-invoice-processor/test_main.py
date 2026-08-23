"""demo06 測試（中等複雜度：happy / edge / integration 共 3 個）。

全部離線可跑，不呼叫任何真實 API。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.config_loader import load_config  # noqa: E402
import main as demo06  # noqa: E402
from extractor import extract_invoice, load_mock_invoices  # noqa: E402

MOCK_PATH = MODULE_DIR / "mock" / "invoices.json"


def _run(extra_args: list[str] | None = None) -> dict[str, Any]:
    """用 CLI 參數跑完整流程，回傳 run() 的結果 dict。"""
    args = demo06.build_parser().parse_args(["--mock", *(extra_args or [])])
    return demo06.run(args)


def _by_filename(items: list[dict[str, Any]], filename: str) -> dict[str, Any]:
    """從結果清單取出指定檔名那一筆。"""
    return next(item for item in items if item["filename"] == filename)


def test_happy_path() -> None:
    """標準 mock 輸入：4 張可信發票入帳、金額與科目與 expected 完全一致。"""
    result = _run()
    records = load_mock_invoices(MOCK_PATH)

    assert result["invoice_count"] == 5
    assert result["posted_count"] == 4
    assert result["skipped_count"] == 1
    assert result["mode"] == "mock"

    for record in records:
        expected = record["expected"]
        if expected["needs_review"]:
            continue
        extracted = _by_filename(result["extracted"], record["filename"])
        posting = _by_filename(result["postings"], record["filename"])
        assert extracted["vendor"] == expected["vendor"]
        assert extracted["invoice_date"] == expected["invoice_date"]
        assert extracted["total_amount"] == expected["total_amount"]
        assert extracted["tax_amount"] == expected["tax_amount"]
        assert extracted["currency"] == expected["currency"]
        assert extracted["standard_name"] == expected["standard_name"]
        assert posting["account_code"] == expected["account_code"]
        assert posting["status"] == "posted"

    # 金額比對一律走 Decimal：504.00 + 77.40 + 21.60 = 603.00（GBP），USD 不可混加。
    assert Decimal(result["totals_by_currency"]["GBP"]) == Decimal("603.00")
    assert Decimal(result["totals_by_currency"]["USD"]) == Decimal("59.99")
    assert result["posted_path"] is not None and Path(result["posted_path"]).is_file()


def test_edge_case_blurry_scan_never_posted() -> None:
    """邊界案例：模糊掃描資料不全 -> needs_review=True 且絕不發布；空輸入同樣被擋下。"""
    result = _run()
    target = _by_filename(result["extracted"], "scan-blurry-print-copy.pdf")

    assert target["needs_review"] is True
    assert target["standard_name"] is None
    assert target["total_amount"] is None          # "1,240.O0" 刻意解析失敗
    assert any("缺少欄位" in issue for issue in target["issues"])
    assert result["needs_review"] == ["scan-blurry-print-copy.pdf"]

    posting = _by_filename(result["postings"], "scan-blurry-print-copy.pdf")
    assert posting["status"] == "needs_review"
    assert posting["account_code"] == "6999"       # 未命中任何規則 -> 待分類支出
    assert all(
        p["status"] != "posted"
        for p in result["postings"]
        if p["filename"] == "scan-blurry-print-copy.pdf"
    )

    # 空輸入：四個必填欄位全缺，同樣標記為待覆核。
    settings = load_config(MODULE_DIR / "config.yaml")["extraction"]
    empty = extract_invoice({"filename": "empty.pdf", "raw_text": ""}, settings)
    assert empty.needs_review is True
    assert len(empty.issues) == 4


def test_integration_autonomy_downgrade_and_console_notify(capsys: Any) -> None:
    """整合案例：autonomy 降級為 draft -> 全部改建草稿；AMBER 累計；console 通知送出。"""
    result = _run(["--autonomy", "draft", "--notify", "console"])

    assert result["autonomy_level"] == "draft"
    assert result["can_post"] is False
    assert result["posted_count"] == 0
    assert all(p["status"] in ("draft", "needs_review") for p in result["postings"])
    assert result["totals_by_currency"] == {}

    # AMBER 至少兩則：模糊掃描（信心不足）＋ Adobe（外幣）。
    assert result["amber_count"] >= 2
    assert result["notified"] is True
    assert "發票處理與費用分類" in capsys.readouterr().out
