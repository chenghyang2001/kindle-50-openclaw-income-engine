"""demo09 測試（契約 §8：happy / edge / integration 三個）。

全部離線執行，不呼叫任何真實 API。
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main  # noqa: E402
import sources  # noqa: E402
from aggregator import Targets, build_report, load_thresholds  # noqa: E402
from sources import SourceError  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    """組出 run() 需要的 Namespace，預設走 mock + console。"""
    base = {
        "mock": True,
        "dry_run": False,
        "notify": "console",
        "config": str(MODULE_DIR / "config.yaml"),
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_happy_path():
    """三個資料源都正常：金額正確聚合、達成率算對、報表完整產出。"""
    result = main.run(_args())

    # CRM 1,750 + Shopify 942 + Stripe 358 = 3,050
    assert result["totals"]["revenue"] == "3050.00"
    assert result["totals"]["orders"] == 13
    assert result["is_partial"] is False
    assert result["failed_sources"] == []
    assert len(result["sources"]) == 3

    # 3,050 / 4,200 = 72.6%，低於 80% 下限 → 觸發一條異常
    assert result["attainment"]["daily_pct"] == "72.6"
    assert result["attainment"]["gap_to_daily_target"] == "-1150.00"
    assert len(result["anomalies"]) == 1
    assert "達成率" in result["anomalies"][0]

    assert result["deliver_at"] == "07:00"
    assert result["delivery"]["delivered"] is True
    assert "每日銷售與進度報表" in result["report_text"]
    assert "部分資料" not in result["report_text"]


def test_edge_case_zero_target_and_no_history():
    """極值：每日目標 0、無 7 日歷史、無任何資料源。

    不可拋 ZeroDivisionError，且達成率必須是 None 而不是 0——
    「目標 0 元」和「達成率 0%」是完全不同的兩件事。
    """
    targets = Targets(
        daily_revenue=Decimal("0"),
        monthly_revenue=Decimal("0"),
        month_to_date_revenue=Decimal("0"),
        trailing_7_day_revenue=(),
    )
    report = build_report([], [], targets, load_thresholds(None), "USD")

    assert report.total_revenue == Decimal("0.00")
    assert report.order_count == 0
    assert report.daily_attainment_pct is None
    assert report.monthly_attainment_pct is None
    assert report.trailing_avg is None
    assert report.deviation_pct is None
    assert any("每日目標為 0" in item for item in report.anomalies)

    payload = report.to_dict()
    assert payload["attainment"]["daily_pct"] is None
    assert payload["trailing_7_day"]["deviation_pct"] is None
    assert payload["is_partial"] is False


def test_integration_stripe_failure_still_delivers(monkeypatch, capsys):
    """整合：Stripe 拋例外時，報表仍產出、標記部分資料、走 Diagnostics.amber。

    這是本模組的核心保證——單源故障不得讓整份報表失敗。
    """

    def _boom(mock_path: Path):
        raise SourceError("Stripe API 逾時（模擬故障）")

    monkeypatch.setitem(sources.FETCHERS, "stripe", _boom)

    result = main.run(_args())

    # 報表照常產出，但明確標記為部分資料
    assert result["is_partial"] is True
    assert [item["source_id"] for item in result["failed_sources"]] == ["stripe"]
    assert "⚠️ 部分資料：Stripe 無回應" in result["report_text"]
    assert "Stripe" in result["report_text"]

    # 剩下兩個資料源的數字照算：1,750 + 942 = 2,692
    assert len(result["sources"]) == 2
    assert result["totals"]["revenue"] == "2692.00"
    assert result["attainment"]["daily_pct"] == "64.1"

    # 走了 Diagnostics 的琥珀燈，而不是紅色警報退出
    assert result["amber_count"] >= 1

    # 達成率過低 + 偏離 7 日均值超過 30%，共兩條異常
    assert len(result["anomalies"]) == 2

    # 仍然實際送出（console 通道），不是靜默失敗
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["channel"] == "console"
    assert "部分資料" in capsys.readouterr().out
