"""bundle-quickstart 測試（3 個：happy / edge / integration，依 CONTRACT.md §8）。

一律離線可跑：不呼叫任何真實 API，demo05 的去重狀態檔一律指向 pytest 的 tmp_path，
避免污染 demo 目錄下的 .seen.json。
"""

from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

BUNDLE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BUNDLE_DIR.parent))
sys.path.insert(0, str(BUNDLE_DIR))


def _load(unique_name: str, filename: str) -> Any:
    """以唯一模組名載入本目錄的檔案，避免整個 repo 一起跑 pytest 時撞名。"""
    spec = importlib.util.spec_from_file_location(unique_name, BUNDLE_DIR / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {BUNDLE_DIR / filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


pricing = _load("bundle_pricing_calculator", "pricing_calculator.py")
runner = _load("bundle_run_all", "run_all.py")


def _quote(argv: list[str]) -> dict:
    """跑一次報價計算。"""
    return pricing.run(pricing.build_parser().parse_args(argv))


def test_happy_path() -> None:
    """標準組合 1,2,5,9 -> 套用書中的 $995 + $200/mo，且所有金額都是 Decimal。"""
    quote = _quote(["--modules", "1,2,5,9", "--industry", "ecommerce"])

    # 單賣加總（第 04 章對照表）：$1,300 setup + $335/mo
    assert quote["list_setup"] == Decimal("1300.00")
    assert quote["list_monthly"] == Decimal("335.00")
    # 打包價是產品定義，不是折扣率算出來的
    assert quote["bundle_setup"] == Decimal("995.00")
    assert quote["bundle_monthly"] == Decimal("200.00")
    assert quote["setup_savings"] == Decimal("305.00")
    assert quote["monthly_savings"] == Decimal("135.00")
    assert quote["recovered_hours_per_month"] == Decimal("90")
    assert quote["warnings"] == []

    # 首年營收 = 995 + 200 * 12 = 3,395（README 的 Sarah Chen 案例數字）
    assert quote["value"]["annual_cost"] == Decimal("3395.00")
    # ecommerce 預設時薪 45：90 hrs × 45 = 4,050/mo
    assert quote["value"]["monthly_value"] == Decimal("4050.00")
    assert quote["value"]["payback_months"] is not None
    assert quote["value"]["roi_pct"] > Decimal("0")

    # 全檔禁止 float：任何一個金額變成 float 就是精度已經被吃掉了
    for key in ("list_setup", "bundle_setup", "bundle_monthly", "hourly_rate"):
        assert isinstance(quote[key], Decimal)
    assert all(isinstance(value, Decimal) for value in quote["value"].values())


def test_edge_case() -> None:
    """邊界：未知產業要出聲、非法模組編號要擋、缺鍵的 raw 不可讓正規化爆掉。"""
    # 1. 未知產業 -> 退回預設時薪但一定附警告，不靜默掩蓋
    quote = _quote(["--modules", "1", "--industry", "space-mining"])
    assert quote["industry"] == pricing.DEFAULT_INDUSTRY
    assert quote["hourly_rate"] == Decimal("60.00")
    assert any("未知產業" in text for text in quote["warnings"])
    # 單一模組未達打包門檻 -> 維持單價
    assert quote["bundle_setup"] == quote["list_setup"] == Decimal("300.00")

    # 2. 非法模組編號 / 空清單 -> 明確拋 ValueError，不回預設組合
    for bad in ("99", "abc", ""):
        try:
            _quote(["--modules", bad])
        except ValueError:
            pass
        else:
            raise AssertionError(f"--modules {bad!r} 應該要拋 ValueError")

    # 3. 正規化必須容忍完全空的、甚至不是 dict 的回傳值
    for raw in ({}, None, "unexpected"):
        item = runner.normalize_result("09", raw)
        assert item["module_id"] == "09"
        assert item["module_name"] == "每日銷售與進度報表"
        assert item["warnings"] == []
        assert item["amber_count"] == 0
        assert item["error"] is None

    # 4. 三種鍵名風格都要撈得到警告
    assert runner.extract_warnings({"autonomy_warnings": ["a"]}) == ["a"]
    assert runner.extract_warnings({"warnings": ["b"]}) == ["b"]
    assert runner.extract_warnings({"autonomy": {"warnings": ["c"]}}) == ["c"]
    partial = runner.extract_warnings(
        {"failed_sources": [{"display_name": "Stripe", "reason": "timeout"}]}
    )
    assert partial and "Stripe" in partial[0]


def test_integration(tmp_path: Path) -> None:
    """整合：--mock 跑完 4 個模組，且 demo01／demo09 的 sources 套件不得互相污染。"""
    argv = ["--mock", "--notify", "console", "--state-dir", str(tmp_path)]
    result = runner.run(runner.build_parser().parse_args(argv))

    assert result["mode"] == "mock"
    assert result["failed_count"] == 0, [item["error"] for item in result["modules"]]
    assert result["ok_count"] == 4

    # 執行順序必須照客戶的早晨時序，不是模組編號順序
    assert [item["module_id"] for item in result["modules"]] == ["01", "02", "09", "05"]

    # 命名空間隔離的關鍵驗證：兩個都有 sources 套件的模組各自拿到自己的資料。
    by_id = {item["module_id"]: item["raw"] for item in result["modules"]}
    assert by_id["01"]["word_count"] > 0
    assert set(by_id["01"]["source_counts"]) == {"calendar", "email", "news"}
    assert Decimal(by_id["09"]["totals"]["revenue"]) > 0
    assert {entry["source_id"] for entry in by_id["09"]["sources"]} == {
        "crm",
        "shopify",
        "stripe",
    }

    # 每個模組都要有正規化後的完整欄位
    for item in result["modules"]:
        assert set(item) == {
            "module_id",
            "module_name",
            "status",
            "warnings",
            "amber_count",
            "raw",
            "error",
        }
        assert item["status"] == "ok"
        assert isinstance(item["warnings"], list)

    # 摘要是客戶視角，不是工程 log
    summary = result["summary_text"]
    assert "這就是你週一早晨會看到的東西" in summary
    assert "完成 4/4 個模組" in summary
    assert "US$995.00" in summary

    # demo05 的去重狀態檔必須落在 tmp_path，不可污染模組目錄
    assert list(tmp_path.glob("*.seen.json"))
