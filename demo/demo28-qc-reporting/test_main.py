"""demo28 測試（契約 §8：happy / edge / integration 三個）。

全部離線執行，不呼叫任何真實 API。時區與基準時間一律注入固定值，
狀態檔與稽核檔一律寫到 pytest 的 tmp_path，不污染模組目錄。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import chain as chain_mod  # noqa: E402
import main  # noqa: E402
from aggregator import (  # noqa: E402
    AlertSeverity,
    ControlChart,
    LineSpec,
    ShiftRecord,
    analyse_shift,
    build_chart,
    linear_slope,
    load_thresholds,
    load_trend_config,
    nelson_violations,
    percent,
)
from audit import verify_file  # noqa: E402

#: 固定基準時間（含時區），確保報告期間與週／月分組每次跑都一樣。
FIXED_AS_OF = "2026-08-22T06:00:00+08:00"

#: L1 夜班那一點 12.5315 超出 UCL 12.524 —— 單班異常的代表案例。
SINGLE_SHIFT_ALERT = "nelson:L1:L1-2026-08-21-C:R1"


def _args(tmp_path: Path, **overrides) -> argparse.Namespace:
    """組出 run() 需要的 Namespace，預設走 mock + console + 注入固定時區與時間。"""
    base = {
        "mock": True,
        "dry_run": False,
        "notify": "console",
        "config": str(MODULE_DIR / "config.yaml"),
        "tier": "daily",
        "state_file": str(tmp_path / "qc-state.json"),
        "audit_file": str(tmp_path / "qc-audit.jsonl"),
        "audit_enabled": True,
        "as_of": FIXED_AS_OF,
        "timezone_name": "Asia/Taipei",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _alert_ids(result: dict) -> set[str]:
    return {alert["alert_id"] for alert in result["alerts"]}


# --------------------------------------------------------------------------
# 1. Happy path
# --------------------------------------------------------------------------


def test_happy_path(tmp_path):
    """標準 mock 輸入：四階報告鏈完整產出，SPC 判定與預測性警告都正確。"""
    result = main.run(_args(tmp_path))

    assert result["module_id"] == "28"
    assert result["mode"] == "mock"
    assert result["timezone"] == "Asia/Taipei"

    # 四階都要有報告：3 條線 × 9 班 = 27 份班末、3 天 = 3 份晨報、1 週、1 月
    chain = result["chain"]
    assert len(chain["shift_end"]) == 27
    assert len(chain["daily"]) == 3
    assert len(chain["weekly"]) == 1
    assert len(chain["monthly"]) == 1

    # 同一份底層資料、四種呈現：受眾與輸出格式必須各不相同
    audiences = {tier: chain[tier][0]["audience"] for tier in chain}
    assert len(set(audiences.values())) == 4
    assert "Supervisor" in audiences["shift_end"]
    assert "Ops Director" in audiences["daily"]
    assert "Quality Manager" in audiences["weekly"]
    assert "Board" in audiences["monthly"]
    assert "PDF Pack" in chain["monthly"][0]["output_format"]
    assert len(chain["monthly"][0]["pdf_pack"]) == 4

    # 詳細度依階層遞減：班末逐點量測，董事會只給高層摘要
    assert "逐點量測" in chain["shift_end"][0]["detail_level"]
    assert "高層摘要" in chain["monthly"][0]["detail_level"]

    # 單班異常：L1 夜班有一點超出 UCL → Nelson Rule 1
    assert SINGLE_SHIFT_ALERT in _alert_ids(result)

    # 連續趨勢惡化：L2 不良率一路上升，在撞上限前 5 個班次內發出預警
    forecasts = {line["line_id"]: line["forecast"] for line in result["plant"]["lines"]}
    l2 = forecasts["L2"]
    assert Decimal(l2["slope_pct_per_shift"]) > 0
    assert l2["severity"] in ("major", "critical")
    assert Decimal(l2["shifts_to_breach"]) <= Decimal("5")

    # 正常班：L3 全期穩定，不應有任何警報
    assert [a for a in result["alerts"] if a["line_id"] == "L3"] == []

    # 稽核軌跡有落地且雜湊鏈完整（可供事後稽核逐行驗證）
    assert result["audit"]["write_failures"] == 0
    assert result["audit"]["chain_verified"] is True
    is_valid, message = verify_file(result["audit"]["path"])
    assert is_valid, message

    # console 通道視為本機列印，不受自主權閘門管制，實際送出
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["preflight"]["passed"] is True
    assert result["state"]["saved"] is True


# --------------------------------------------------------------------------
# 2. Edge case
# --------------------------------------------------------------------------


def test_edge_case_zero_output_and_empty_readings():
    """極值：投入為 0、無任何量測值、σ 為 0、單點趨勢、無時區的基準時間。

    核心不可拋 ZeroDivisionError；不良率與良率必須是 None 而不是 0——
    「這班沒有產出」和「這班不良率 0%」在品管上是完全不同的兩件事。
    """
    spec = LineSpec(
        line_id="LX", line_name="測試線", process="Test", mes_system="Plex",
        metric_name="尺寸", unit="mm",
        baseline_mean=Decimal("10.000"), baseline_sigma=Decimal("0.010"),
        usl=Decimal("10.040"), lsl=Decimal("9.960"),
        defect_rate_limit_pct=Decimal("1.00"), target_yield_pct=Decimal("99.00"),
    )
    record = ShiftRecord(
        line_id="LX", shift_id="LX-2026-08-21-A", shift_date="2026-08-21", shift_code="A",
        supervisor="測試", units_produced=0, units_defective=0, readings=(),
    )
    chart = build_chart(spec)
    analysis = analyse_shift(record, spec, chart, load_thresholds(None, None), FIXED_AS_OF)

    assert analysis.defect_rate_pct is None
    assert analysis.yield_pct is None
    assert analysis.mean_reading is None
    assert analysis.violations == ()

    # 停線必須自己浮上來，不可被當成「表現完美」
    assert [a.alert_id for a in analysis.alerts] == ["no_output:LX:LX-2026-08-21-A"]
    assert analysis.alerts[0].severity is AlertSeverity.MAJOR
    payload = analysis.to_dict()
    assert payload["defect_rate_pct"] is None
    assert payload["yield_pct"] is None

    # 百分比防零除：分母 0 一律回 None，不回 0
    assert percent(Decimal("0"), Decimal("0")) is None
    assert percent(Decimal("3"), Decimal("0")) is None

    # σ 為 0 的退化控制圖不可除以零；空序列不可觸發任何 Nelson Rule
    flat = ControlChart(*(Decimal("0"),) * 8)
    assert flat.sigma_offset(Decimal("5")) == Decimal("0")
    assert nelson_violations([], chart) == []
    assert nelson_violations([], flat) == []

    # 趨勢外推：只有一個資料點時無法定義斜率，必須回 None 而不是 0
    assert linear_slope([Decimal("1.0")]) is None
    assert load_trend_config(None).window_shifts >= 2

    # 時區注入：帶時區直接採用；不帶時區時套用傳入的時區，不留 naive datetime
    aware = main.resolve_as_of("2026-08-22T06:00:00+08:00", dt_timezone.utc)
    assert aware.utcoffset().total_seconds() == 8 * 3600
    naive = main.resolve_as_of("2026-08-22T06:00:00", dt_timezone.utc)
    assert naive.tzinfo is dt_timezone.utc


# --------------------------------------------------------------------------
# 3. Integration — 本模組的核心保證
# --------------------------------------------------------------------------


def test_integration_alerts_visible_in_all_four_tiers(tmp_path, capsys):
    """整合：任何一階偵測到的異常，四階都看得見，且不被平均值稀釋。

    這是本模組最重要的一條保證，同時涵蓋：
    - 單班異常（L1 夜班 3σ 超限）貫穿班末 → 日 → 週 → 月
    - 某產線無資料（L4）產生 CRITICAL 警報並貫穿上三階
    - 當日平均不良率遠低於上限時，高階報告仍強制印出警示橫幅
    - 聚合只做聯集：全廠警報 == 各線警報 ∪ 資料缺漏警報
    - 守門機制本身有效：漏掉警報時 `assert_no_alert_dropped` 會拋錯
    - 與 _shared 的互動：autonomy 預設 draft、diagnostics、notifier console
    """
    result = main.run(_args(tmp_path, tier="all"))
    tiers = result["tier_alert_ids"]

    # (1) 班末偵測到的每一則，日／週／月三階都必須看得到
    shift_ids = set(tiers["shift_end"])
    assert shift_ids, "班末階應至少偵測到一則警報，否則本測試無意義"
    for upper in ("daily", "weekly", "monthly"):
        assert shift_ids <= set(tiers[upper]), f"{upper} 階漏掉了班末警報"

    # (2) 單班異常貫穿四階，不是被 27 個班別的平均稀釋掉
    assert SINGLE_SHIFT_ALERT in shift_ids
    for upper in ("daily", "weekly", "monthly"):
        assert SINGLE_SHIFT_ALERT in set(tiers[upper])

    # (3) 某產線無資料 → CRITICAL，從第 2 階（廠長）起貫穿到董事會
    outage_ids = [a["alert_id"] for a in result["alerts"] if a["category"] == "data_outage"]
    assert outage_ids == ["outage:L4:2026-08"]
    assert result["alerts"][0]["severity"] == "critical"
    for upper in ("daily", "weekly", "monthly"):
        assert outage_ids[0] in set(tiers[upper])

    # (4) 平均值正常也不得結案：08-21 全廠不良率遠低於各線上限，
    #     但該日晨報仍必須印出警示橫幅，把警報推回讀者眼前。
    daily = [r for r in result["chain"]["daily"] if r["period_key"] == "2026-08-21"][0]
    assert Decimal(daily["aggregate"]["defect_rate_pct"]) < Decimal("1.50")
    assert daily["alert_counts"]["critical"] >= 1
    assert "品質警報未結案" in daily["body_markdown"]
    assert "不因平均而消失" in daily["body_markdown"]

    # (5) 董事會報告包同樣看得到重大項目與資料缺漏
    board = result["chain"]["monthly"][0]
    assert board["alert_counts"]["critical"] >= 2
    assert "塗裝 D 線" in board["body_markdown"]

    # (6) 聚合只做聯集：全廠警報恰為各線警報 + 資料缺漏警報
    plant = result["plant"]
    from_lines = {a["alert_id"] for line in plant["lines"] for a in line["alerts"]}
    from_outage = {f"outage:{o['line_id']}:2026-08" for o in plant["outages"]}
    assert {a["alert_id"] for a in plant["alerts"]} == from_lines | from_outage

    # (7) 守門機制本身有效：上階漏掉下階警報時必須拋錯，不是靜默通過
    class _FakeReport:
        alert_ids = {"nelson:L9:L9-2026-08-21-A:R1"}

    try:
        chain_mod.assert_no_alert_dropped([_FakeReport()], [], "班末報告", "每日晨報")
    except chain_mod.AlertSuppressionError as exc:
        assert "漏掉" in str(exc)
    else:  # pragma: no cover - 守門失效時才會走到
        raise AssertionError("assert_no_alert_dropped 未在警報被吃掉時拋錯")

    # (8) 與 _shared 的互動：autonomy 預設 draft、console 有輸出、無琥珀燈
    assert result["warnings"] == []
    assert result["amber_count"] == 0
    assert result["delivery"]["channel"] == "console"
    assert "班末品質報告" in capsys.readouterr().out

    # (9) 未結案警報寫進狀態檔，下次執行會沿用（不會自己痊癒）
    state = json.loads(Path(result["state"]["path"]).read_text(encoding="utf-8"))
    assert SINGLE_SHIFT_ALERT in {a["alert_id"] for a in state["open_alerts"]}

    # (10) 稽核軌跡逐階記錄各階看見的警報 id，事後可證明沒有被吃掉
    assert "tier_built" in result["audit"]["events"]
    assert result["audit"]["chain_verified"] is True
