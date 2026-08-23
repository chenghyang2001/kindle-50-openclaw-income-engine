"""demo26 電商庫存與定價最佳化 —— 3 個測試（happy / edge / integration）。

edge case 是本模組的重點，而且刻意挑最貴的兩種錯：
    1. **建議價低於成本** —— 一個小數點錯誤就能虧本賣光整批庫存
    2. **變動幅度超過上限** —— 一次 -20% 的清倉價不該由機器自己按下去
兩者都必須被擋在寫回平台之前，而且要留下可稽核的紀錄。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import yaml

_DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402


def _load(module_name: str, filename: str):
    """以絕對路徑載入本 demo 的模組。

    多個 demo 都有同名的 main.py，一次跑整個 demo/ 目錄時 plain import
    會抓到別的 demo 的同名模組，因此這裡固定綁死檔案路徑。
    """
    spec = importlib.util.spec_from_file_location(module_name, _DEMO_DIR / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# 順序不可調換：main 內部會 import analyser / audit / pricer，必須先佔住 sys.modules
analyser = _load("analyser", "analyser.py")
audit = _load("audit", "audit.py")
pricer = _load("pricer", "pricer.py")
optimiser = _load("main", "main.py")

# 直接呼叫 pricer 時使用的設定，數值與 config.yaml 的預設值一致
SETTINGS = {
    "slow_mover_days": 14,
    "slow_mover_percentile": 30,
    "fast_mover_percentile": 20,
    "overstock_doh": 90,
    "reorder_recommended_multiplier": "1.5",
    "max_price_change_percent": 10,
    "max_price_change_ceiling": 30,
    "min_margin_percent": 5,
    "undercut_match_delta_percent": 1,
    "fast_mover_increase_percent": 5,
    "overstock_clearance_percent": 20,
    "promo_discount_percent": 10,
    "competitor_neutral_band_percent": 3,
    "competitor_undercut_threshold_percent": 5,
    "reduce_if_below_pct": 5,
    "increase_if_days_of_stock_under": 14,
    "hold_if_within_pct": 3,
}


def _args(tmp_path: Path, config_path: Path | None = None):
    """組出離線模式的 CLI 參數；狀態檔與稽核檔一律指向 tmp，避免污染 repo"""
    argv = [
        "--mock",
        "--state-file", str(tmp_path / "pricing_state.json"),
        "--audit-file", str(tmp_path / "pricing_audit.jsonl"),
    ]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return optimiser.build_parser().parse_args(argv)


def _by_sku(rows: list[dict]) -> dict[str, dict]:
    """把結果清單轉成以 sku_id 為索引的字典"""
    return {row["sku_id"]: row for row in rows}


def test_happy_path(tmp_path):
    """標準 mock 輸入：8 個 SKU 走完分析 -> 定價 -> 促銷企劃 -> 稽核"""
    result = optimiser.run(_args(tmp_path))

    assert result["module_id"] == "26"
    assert result["mode"] == "mock"
    assert result["stats"]["total"] == 8

    # STATUS 五級分類（判定順序即規格，改動順序會讓建議完全相反）
    skus = _by_sku(result["skus"])
    assert skus["SKU-1005"]["status"] == "REORDER_URGENT"      # 庫存 0
    assert skus["SKU-1007"]["status"] == "REORDER_RECOMMENDED"  # 可售 12.22 天 < 補貨點 x1.5
    assert skus["SKU-1003"]["status"] == "SLOW_MOVER"           # 滯銷 21 天
    assert skus["SKU-1006"]["status"] == "OVERSTOCK"            # 可售 320 天
    assert skus["SKU-1001"]["status"] == "HEALTHY"
    assert skus["SKU-1001"]["days_on_hand"] == "13.13"
    assert skus["SKU-1004"]["flags"] == ["NEGATIVE_MARGIN"]

    # 兩筆通過安全閥的調價草稿：熱銷調漲、滯銷降價匹配對手 -1%
    drafts = _by_sku(result["drafts"])
    assert set(drafts) == {"SKU-1001", "SKU-1003"}
    assert drafts["SKU-1001"]["proposed_price"] == "135.45"
    assert drafts["SKU-1001"]["change_percent"] == "5.00"
    assert drafts["SKU-1001"]["rules_matched"] == ["increase_if"]
    assert drafts["SKU-1003"]["proposed_price"] == "217.80"   # 對手 220.00 再 -1%
    assert drafts["SKU-1003"]["change_percent"] == "-9.25"
    assert drafts["SKU-1003"]["rules_matched"] == ["reduce_if"]
    # 調價一律以 DRAFT 產出，程式不會自己寫回平台
    assert all(row["approval_state"] == "DRAFT" for row in result["drafts"])

    # 滯銷 14 天以上觸發 promotional_brief_generator；負毛利的 SKU-1004 不列入候選
    assert result["promotional_skus"] == ["SKU-1003", "SKU-1008"]
    assert result["promotional_brief"].strip()

    # 稽核軌跡落地且串鏈完整
    is_valid, note = audit.verify_chain(result["audit_file"])
    assert is_valid, note
    events = [row["event"] for row in audit.read_entries(result["audit_file"])]
    assert events[0] == "run_started" and events[-1] == "run_completed"
    assert events.count("pricing_decision") == 8

    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert state["skus"]["SKU-1003"]["proposed_price"] == "217.80"


def test_edge_case_price_rails_block_dangerous_changes(tmp_path):
    """定價安全鐵律：低於成本、超過變動上限的建議都必須被擋下並升級人工"""
    result = optimiser.run(_args(tmp_path))
    blocked = _by_sku(result["blocked"])
    assert set(blocked) == {"SKU-1004", "SKU-1006"}

    # (1) 成本價高於售價 -> RED，且完全不產生任何建議價
    negative = blocked["SKU-1004"]
    assert negative["reject_reason"] == "NEGATIVE_MARGIN"
    assert negative["severity"] == "red"
    assert negative["proposed_price"] is None
    assert result["red_alerts"], "負毛利必須發出 RED，不可靜默略過"

    # (2) 清倉 -20% 超過單次上限 10% -> 擋下，但保留「本來想改成多少」供人工判斷
    capped = blocked["SKU-1006"]
    assert capped["reject_reason"] == "EXCEEDS_MAX_CHANGE"
    assert capped["blocked_price"] == "60.00"
    assert capped["change_percent"] == "-20.00"
    assert capped["proposed_price"] is None

    # (3) 直接對 pricer 施壓：矩陣要求降價匹配對手，但那個價格會低於成本
    analysis = analyser.SkuAnalysis(
        sku_id="SKU-TEST", product_name="Below Cost Trap", current_stock=50,
        avg_daily_velocity_7d=Decimal("0.200"), avg_daily_velocity_30d=Decimal("0.300"),
        days_on_hand=Decimal("250.00"), reorder_point=Decimal("10.000"),
        status=analyser.STATUS_SLOW_MOVER, velocity_band=analyser.BAND_SLOW,
        days_since_last_sale=20, current_price=Decimal("100.00"),
        cost_price=Decimal("95.00"), competitor_price=Decimal("80.00"),
        category="test", flags=(),
    )
    proposal = pricer.propose(analysis, SETTINGS)
    assert proposal.action == pricer.ACTION_REDUCE_MATCH
    assert proposal.reject_reason == pricer.REJECT_BELOW_COST
    assert proposal.severity == "red"
    assert proposal.proposed_price is None
    assert proposal.blocked_price == Decimal("79.20")   # 對手 80.00 -1%，低於成本 95.00

    # 被擋下的項目全部進了稽核軌跡，事後查得到「誰想改、為什麼沒改成」
    entries = audit.read_entries(result["audit_file"])
    red_rows = [row for row in entries if row["severity"] == "red"]
    assert [row["sku_id"] for row in red_rows] == ["SKU-1004"]


def test_integration_autonomy_downgrade_and_console_notify(tmp_path, capsys):
    """與 _shared 的互動：supervised_auto 未命中白名單 -> 降級 draft + AMBER + console 送出"""
    config = yaml.safe_load((_DEMO_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"].update(
        {
            "autonomy": "supervised_auto",
            "approved_senders": ["@allowed.example"],
            "days_in_draft": 0,
            "alert_recipient": "ops@other.example",
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
    assert gate.effective_level("ops@other.example") is AutonomyLevel.DRAFT
    assert gate.effective_level("ops@allowed.example") is AutonomyLevel.SUPERVISED_AUTO
    assert gate.warnings, "未滿 14 天就開 supervised_auto 必須留下警告"

    result = optimiser.run(_args(tmp_path, config_path))

    assert result["delivery"] == "draft"
    assert result["notify_channel"] == "console"
    assert result["notified"] is True
    # 缺貨 1 筆 + 超過變動上限 1 筆 + 至少 1 筆「未滿 14 天」自主權警告
    assert result["amber_count"] >= 3
    assert any("SKU-1006" in item for item in result["warnings"])

    printed = capsys.readouterr().out
    assert "【草稿・待人工核准】" in printed
    assert "需人工核准後才會寫回平台" in printed
    assert "SKU-1001" in printed

    is_valid, note = audit.verify_chain(result["audit_file"])
    assert is_valid, note
