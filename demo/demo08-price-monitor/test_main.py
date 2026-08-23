"""demo08 競品價格監控警報 —— 3 個測試（happy / edge / integration）。

edge case 是本模組的重點：驗證 competitor_e（網站改版導致選擇器失效）
與 competitor_f（價格欄位變成非數字）**確實被警報**，
而不是被靜默跳過、也不是被當成「價格無變動」混進比對結果。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

_DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402


def _load(module_name: str, filename: str):
    """以絕對路徑載入本 demo 的模組。

    10 個 demo 都有同名的 main.py / scraper.py，一次跑整個 demo/ 目錄時
    plain import 會抓到別的 demo 的同名模組，因此這裡固定綁死檔案路徑。
    """
    spec = importlib.util.spec_from_file_location(module_name, _DEMO_DIR / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# 順序不可調換：main 內部會 import comparator / scraper，必須先佔住 sys.modules
comparator = _load("comparator", "comparator.py")
scraper = _load("scraper", "scraper.py")
price_monitor = _load("main", "main.py")

TARGET_A = "Competitor A — Linen Bedding Set"
TARGET_B = "Competitor B — Ceramic Table Lamp"
TARGET_C = "Competitor C — Oak Side Table"
TARGET_D = "Competitor D — Wool Throw Blanket"
TARGET_E = "Competitor E — Rattan Armchair"
TARGET_F = "Competitor F — Marble Coffee Table"


def _args(tmp_path: Path, config_path: Path | None = None):
    """組出離線模式的 CLI 參數，狀態檔一律指向 tmp 避免污染 repo"""
    argv = ["--mock", "--state-file", str(tmp_path / "baselines.json")]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return price_monitor.build_parser().parse_args(argv)


def _read_state(result: dict) -> dict:
    """讀回本次寫出的基準狀態檔"""
    return json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))


def test_happy_path(tmp_path):
    """標準 mock 輸入：6 目標 → 4 筆解析成功，A(-18%) 與 D(-6%) 觸發警報"""
    result = price_monitor.run(_args(tmp_path))

    assert result["checked"] == 6
    assert result["parsed"] == 4
    assert result["failed"] == 2

    changes = {c["name"]: c for c in result["changes"]}
    assert changes[TARGET_A]["baseline_price"] == "129.00"
    assert changes[TARGET_A]["current_price"] == "105.78"
    assert changes[TARGET_A]["delta_percent"] == "-18.00"
    assert changes[TARGET_A]["direction"] == "drop"
    assert changes[TARGET_D]["delta_percent"] == "-6.00"
    assert changes[TARGET_B]["direction"] == "flat"
    # C 漲價 3%，未達 5% 閾值，不得警報
    assert changes[TARGET_C]["delta_percent"] == "3.00"
    assert changes[TARGET_C]["is_breach"] is False
    assert {b["name"] for b in result["breaches"]} == {TARGET_A, TARGET_D}

    # 本次觀測價格已寫回狀態檔，成為明天的基準
    state = _read_state(result)
    assert state["targets"][TARGET_A]["price"] == "105.78"
    assert set(state["targets"]) == {TARGET_A, TARGET_B, TARGET_C, TARGET_D}


def test_edge_case_parse_failures_are_alerted(tmp_path):
    """解析失敗必須警報：改版失效（E）與髒資料（F）都要現形，不得靜默跳過"""
    # 先驗解析層本身：改版後的頁面抓不到、非數字內容解不出價格
    redesigned = (_DEMO_DIR / "mock" / "pages" / "competitor_e.html").read_text(encoding="utf-8")
    assert scraper.extract_text(redesigned, "span.price") is None
    assert scraper.parse_price("Call for pricing") is None

    result = price_monitor.run(_args(tmp_path))

    reasons = {f["name"]: f["reason"] for f in result["failures"]}
    assert set(reasons) == {TARGET_E, TARGET_F}
    assert "找不到對應元素" in reasons[TARGET_E]
    assert "非數字" in reasons[TARGET_F]

    # 報告必須明白寫出「N 個目標解析失敗」，且每筆失敗都轉成 AMBER
    assert "2 個目標解析失敗" in result["report"]
    assert result["amber_count"] >= 2

    # 失敗目標既不能被當成「無變動」，也不能污染基準狀態檔
    assert all(c["name"] not in {TARGET_E, TARGET_F} for c in result["changes"])
    state = _read_state(result)
    assert TARGET_E not in state["targets"]
    assert TARGET_F not in state["targets"]


def test_integration_autonomy_downgrade_and_console_notify(tmp_path, capsys):
    """與 _shared 的互動：supervised_auto 未命中白名單 → 降級 draft + AMBER + console 送出"""
    config = yaml.safe_load((_DEMO_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"].update(
        {
            "autonomy": "supervised_auto",
            "approved_senders": ["@allowed.example"],
            "days_in_draft": 0,
            "alert_recipient": "owner@other.example",
            "notify_channel": "console",
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # AutonomyGate 的降級規則本身
    gate = AutonomyGate(
        level=AutonomyLevel.SUPERVISED_AUTO,
        approved_senders=["@allowed.example"],
        days_in_draft=0,
    )
    assert gate.effective_level("owner@other.example") is AutonomyLevel.DRAFT
    assert gate.effective_level("ops@allowed.example") is AutonomyLevel.SUPERVISED_AUTO
    assert gate.warnings, "未滿 14 天就開 supervised_auto 必須留下警告"

    result = price_monitor.run(_args(tmp_path, config_path))

    assert result["delivery"] == "draft"
    assert result["notify_channel"] == "console"
    assert result["notified"] is True
    # 2 筆解析失敗 + 至少 1 筆「未滿 14 天」自主權警告
    assert result["amber_count"] >= 3

    printed = capsys.readouterr().out
    assert "【草稿・待人工核准】" in printed
    assert TARGET_A in printed
