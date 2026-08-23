"""demo15 測試（中等複雜度：happy / edge / integration 共 3 個）。

全部離線可跑，不呼叫任何真實 API。狀態檔與輸出目錄一律指到 pytest 的 tmp_path，
跑完測試不會在模組目錄留下 output/ 或 .proposals.json。
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo15  # noqa: E402
from pricing import PricingError, QuoteEngine, RateCard, to_decimal  # noqa: E402
from proposal_builder import redact_monetary_tokens  # noqa: E402

# 固定報價日，讓有效期限（+14 天）在測試中可重現。
AS_OF = "2026-08-24"
EXPECTED_VALID_UNTIL = "2026-09-07"


def _run(tmp_path: Path, extra_args: list[str] | None = None) -> dict[str, Any]:
    """用 CLI 參數跑完整流程，回傳 run() 的結果 dict。"""
    argv = [
        "--mock",
        "--as-of",
        AS_OF,
        "--state-file",
        str(tmp_path / ".proposals.json"),
        "--output-dir",
        str(tmp_path / "output"),
        *(extra_args or []),
    ]
    return demo15.run(demo15.build_parser().parse_args(argv))


def _proposal(result: dict[str, Any], deal_id: str) -> dict[str, Any]:
    """從結果中取出指定交易的提案。"""
    return next(item for item in result["proposals"] if item["deal_id"] == deal_id)


def _option(proposal: dict[str, Any], tier_key: str) -> dict[str, Any]:
    """從提案中取出指定方案（essential / recommended / comprehensive）。"""
    return next(item for item in proposal["options"] if item["tier_key"] == tier_key)


def test_happy_path(tmp_path: Path) -> None:
    """標準案件：三個投資選項的金額與 RATE_CARD 逐分吻合，狀態為草稿待核准。"""
    result = _run(tmp_path)

    assert result["module_id"] == "15"
    assert result["mode"] == "mock"
    assert result["proposal_count"] == 3

    proposal = _proposal(result, "NWL-2026-041")
    assert proposal["status"] == "draft_pending_approval"
    assert proposal["status_label"] == "草稿・待核准"
    assert proposal["valid_until"] == EXPECTED_VALID_UNTIL
    assert proposal["recommended_tier"] == "recommended"
    assert [opt["tier_key"] for opt in proposal["options"]] == [
        "essential",
        "recommended",
        "comprehensive",
    ]

    # 逐分比對（金額一律走 Decimal，字串比對會漏掉 "3360.0" 這種格式差異）。
    expected = {
        "essential": ("3200.00", "0.00", "3360.00", "0.00", "3360.00"),
        "recommended": ("3800.00", "0.00", "3990.00", "210.00", "6510.00"),
        "comprehensive": ("6300.00", "315.00", "6284.25", "472.50", "11954.25"),
    }
    for tier_key, (one_off, discount, setup, monthly, first_year) in expected.items():
        option = _option(proposal, tier_key)
        assert Decimal(option["one_off_subtotal"]) == Decimal(one_off)
        assert Decimal(option["discount_amount"]) == Decimal(discount)
        assert Decimal(option["setup_total"]) == Decimal(setup)
        assert Decimal(option["monthly_total"]) == Decimal(monthly)
        assert Decimal(option["first_year_total"]) == Decimal(first_year)
        assert option["requires_human_pricing"] is False

    # 電子簽署鐵律：一份都不能送出。
    assert result["signatures_sent"] == 0
    assert all(req["is_sent"] is False for req in result["signature_requests"])
    assert all(req["status"] == "pending_human_approval" for req in result["signature_requests"])

    # 草稿浮水印與檔案落地。
    assert "草稿・待核准" in proposal["markdown"]
    assert len(result["output_files"]) == 3
    assert Path(result["state_file"]).is_file()
    assert all(Path(path).is_file() for path in result["output_files"])


def test_edge_case_out_of_range_and_money_precision(tmp_path: Path) -> None:
    """邊界案例：超出報價範圍的大案必須轉人工核價；金額精度不得有浮點尾差。"""
    result = _run(tmp_path)
    proposal = _proposal(result, "MGH-2026-102")

    assert proposal["status"] == "needs_pricing_review"
    assert proposal["requires_human_pricing"] is True
    assert result["needs_pricing_review"] == [proposal["proposal_id"]]
    joined_issues = "｜".join(proposal["issues"])
    assert "超過上限" in joined_issues          # 折扣 30% > 上限 20%
    assert "超過 RATE_CARD 上限" in joined_issues  # 教育訓練 7 場 / 資料遷移 3 式
    assert "超過自動報價天花板" in joined_issues   # 方案 C 含稅建置費 > 25,000

    # 折扣被夾到上限而非照給；超範圍仍然算得出金額，只是不准自動送出。
    comprehensive = _option(proposal, "comprehensive")
    assert Decimal(comprehensive["discount_rate"]) == Decimal("0.20")
    assert Decimal(comprehensive["setup_total"]) == Decimal("26796.00")
    assert proposal["signature_request"]["is_sent"] is False

    # 精度：5,985 × 5% 用 float 會得到 299.25000000000006，Decimal 必須剛好是 299.25。
    northwind = _option(_proposal(result, "NWL-2026-041"), "comprehensive")
    assert Decimal(northwind["one_off_tax"]) == Decimal("299.25")
    assert Decimal(northwind["first_year_total"]) == Decimal("11954.25")

    # float 一律拒收：設定檔忘了加引號會當場報錯，而不是安靜地損失精度。
    with pytest.raises(PricingError):
        to_decimal(1200.00, "rate_card.demo.unit_price")
    # 未知服務代碼同樣當場報錯，不套用任何預設價。
    engine = QuoteEngine(
        {"currency": "USD", "tiers": [{"key": "essential", "add_ons": []}]},
        RateCard([{"code": "a", "name": "A", "billing": "one_off", "unit_price": "10.00"}]),
    )
    with pytest.raises(PricingError):
        engine.build_options([{"code": "does_not_exist", "quantity": 1}])


def test_integration_autonomy_redaction_and_notify(tmp_path: Path, capsys: Any) -> None:
    """整合案例：autonomy 白名單降級、LLM 金額遮蔽、console 通知，簽署仍然零送出。"""
    result = _run(tmp_path, ["--autonomy", "supervised_auto", "--notify", "console"])

    assert result["autonomy_level"] == "supervised_auto"
    # 白名單只有 @northwind-logistics.example：其餘客戶一律降級為內部審閱。
    assert _proposal(result, "NWL-2026-041")["delivery_mode"] == "auto_send_to_client"
    assert _proposal(result, "HRG-2026-088")["delivery_mode"] == "internal_review_only"
    assert _proposal(result, "MGH-2026-102")["delivery_mode"] == "internal_review_only"

    # 鐵律：即使自主權開到 supervised_auto，電子簽署依然零送出。
    assert result["signatures_sent"] == 0

    # 模型在敘事裡自己寫的兩處金額必須被遮蔽，且不得留在文件中。
    halcyon = _proposal(result, "HRG-2026-088")
    assert result["redaction_count"] == 2
    assert sorted(halcyon["redactions"]) == ["$4,500", "US$350"]
    assert "$4,500" not in halcyon["markdown"]
    assert "［金額由系統計算後填入］" in halcyon["markdown"]
    assert result["amber_count"] >= 1

    # 遮蔽器本身：抓得到金額，抓不到工時（否則敘事的「3 小時」會被誤遮）。
    cleaned, found = redact_monetary_tokens("投資 $1,200 可回收 3 小時", "[X]")
    assert found == ["$1,200"] and "3 小時" in cleaned

    assert result["notified"] is True
    assert "提案與報價生成器" in capsys.readouterr().out
