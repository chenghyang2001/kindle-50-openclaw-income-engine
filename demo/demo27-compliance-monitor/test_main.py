"""demo27 三個測試（CONTRACT §8）：happy / edge / integration。

一律離線執行：不呼叫任何真實 API，來源全部來自 mock/ 目錄。
時間一律釘在 `config.mock.frozen_now`（2026-08-24T09:00:00+08:00），
台帳與狀態檔一律寫到 `tmp_path`，跑完測試不會在模組目錄留下 registry/。
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo_main  # noqa: E402
from analyser import warning_stage  # noqa: E402
from escalation import LEVEL_RANK  # noqa: E402


def _args(tmp_path: Path, **overrides: Any):
    """建出預設 CLI 參數；台帳/狀態檔一律導到 tmp_path，red 改拋例外而非結束行程。"""
    args = demo_main.build_parser().parse_args([])
    args.exit_on_red = False
    args.registry_dir = str(tmp_path / "registry")
    args.state_file = str(tmp_path / "state.json")
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _levels(result: dict[str, Any]) -> dict[str, list[str]]:
    """把升級清單整理成 {級別: [record_id, ...]}。"""
    grouped: dict[str, list[str]] = {level: [] for level in LEVEL_RANK}
    for notice in result["notices"]:
        grouped[notice["level"]].append(notice["record_id"])
    return grouped


def test_happy_path(tmp_path: Path) -> None:
    """標準 mock 來源：19 筆物件，到期天數與 120/60/14 三階段判定完全對得上。"""
    result = demo_main.run(_args(tmp_path))
    stages = {item["record_id"]: (item["stage"], item["days"]) for item in result["findings"]}

    assert result["mode"] == "mock"
    assert result["module_id"] == "27"
    assert result["dry_run"] is False
    assert len(result["findings"]) == 19

    # 三階段警告：120 / 60 / 14 天（附錄G apxG_p15）
    assert stages["CTR-2023-0442"] == ("stage_120", 118)
    assert stages["CTR-2025-0088"] == ("stage_60", 52)
    assert stages["CTR-2025-0201"] == ("stage_14", 11)
    assert stages["CTR-2022-0009"] == ("overdue", -24)
    assert stages["CTR-2024-0117"] == ("none", 372), "還早的合約不該進警告視窗"
    assert stages["POL-AML-02"] == ("overdue", -195), "政策審查逾期必須被旗標"

    # 金額一律 Decimal，字串化後不得出現浮點誤差
    assert result["at_risk_value"] == "496900.00"
    assert "不構成法律意見" in result["disclaimer"]
    assert "不構成法律意見" in result["message"]
    assert result["channel_probe"]["slack"]["available"] is True
    assert warning_stage(None, (120, 60, 14), 0) == "unknown"


def test_edge_case_never_guesses_missing_or_unreadable_terms(tmp_path: Path) -> None:
    """邊界：條款看不懂 / 缺必要欄位一律標 needs_human_review，絕不猜值。"""
    result = demo_main.run(_args(tmp_path))
    findings = {item["record_id"]: item for item in result["findings"]}

    # 1. 條款交叉引用未附上的 Schedule，信心 0.31 → 需人工複核，但到期日照樣逐字採用
    unreadable = findings["CTR-2026-0333"]
    assert unreadable["needs_human_review"] is True
    assert unreadable["confidence"] == 0.31
    assert any("信心" in reason for reason in unreadable["review_reasons"])
    assert unreadable["details"]["expiry_date"] == "2026-11-05"
    assert "Schedule 7 Part B" in unreadable["evidence"], "條款原文必須逐字保留供稽核回溯"

    # 2. 來源沒有 expiry_date → 一律留空，禁止用 effective_date + 12 個月推算
    missing = findings["CTR-2026-0410"]
    assert missing["details"]["expiry_date"] == ""
    assert missing["days"] is None
    assert missing["stage"] == "unknown"
    assert any("expiry_date" in reason for reason in missing["review_reasons"])

    # 3. 政策沒有 last_reviewed → 不推估下次審查日
    assert findings["POL-WB-04"]["details"]["next_review_due"] == ""
    # 4. 公告未載明影響等級 → 系統不自行判定
    assert findings["REG-2026-0731"]["declared_level"] is None
    assert result["needs_human_review_count"] == 5
    assert result["amber_count"] >= 5


def test_integration_escalation_paths_and_append_only_ledgers(tmp_path: Path) -> None:
    """整合：三級升級路徑 + 台帳 append-only + autonomy 降級 + amber + console notifier。"""
    config = yaml.safe_load((MODULE_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"].update(
        {
            "autonomy": "supervised_auto",
            "approved_senders": ["@acme.example"],
            "days_in_draft": 3,  # 未滿 14 天 → AutonomyGate 應發出警告
        }
    )
    config["escalation"]["levels"]["critical"]["recipients"] = ["gc@acme.example", "cco@acme.example"]
    config["escalation"]["levels"]["high"]["recipients"] = ["manager@vendor-lab.example"]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    first = demo_main.run(_args(tmp_path, config=str(config_path), notify="console"))
    grouped = _levels(first)

    # --- 三級 Escalation Matrix 的三條路徑都要真的走到 ---
    assert set(grouped["critical"]) == {
        "CTR-2025-0201",  # 到期前 11 天
        "CTR-2022-0009",  # 已逾期
        "LIC-HAZ-0071",  # 執照到期前 8 天
        "POL-AML-02",  # 政策審查逾期
        "REG-2026-0812",  # 公告自述 critical
    }
    assert set(grouped["standard"]) == {"CTR-2023-0442", "REG-2026-0805", "REG-2026-0722"}
    assert "CTR-2025-0088" in grouped["high"] and "POL-IS-03" in grouped["high"]
    critical = next(item for item in first["notices"] if item["record_id"] == "LIC-HAZ-0071")
    assert critical["channels"] == ["slack", "email"], "Critical 必須雙通道，單一通道即漏報"
    assert critical["delivery_status"] == "ready"
    assert next(item for item in first["notices"] if item["record_id"] == "REG-2026-0805")["channels"] == ["email"]

    # --- autonomy 降級：白名單內自動送，白名單外降為草稿 ---
    actions = {(item["record_id"], item["recipient"]): item["action"] for item in first["deliveries"]}
    assert actions[("LIC-HAZ-0071", "gc@acme.example")] == "auto_sent"
    assert actions[("CTR-2025-0088", "manager@vendor-lab.example")] == "draft"
    assert first["warnings"], "未滿 14 天應累積自主權警告"
    assert first["amber_count"] >= 1

    # --- 台帳 append-only：再跑一次，歷史列必須完整保留 ---
    assert first["ledger_rows"] == {"contract": 7, "licence": 4, "policy": 4}
    second = demo_main.run(_args(tmp_path, config=str(config_path), notify="console"))
    ledger = Path(first["registry_files"]["contract"])
    with ledger.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 14, "第二次執行必須是追加，不可覆蓋第一次的稽核軌跡"
    assert ledger.read_text(encoding="utf-8").count("recorded_at,run_id") == 1, "標頭只寫一次"
    assert rows[0]["source_ref"].endswith("#CTR-2024-0117")
    assert rows[0]["recorded_at"].startswith("2026-08-24")
    overdue_rows = [row for row in rows if row["contract_id"] == "CTR-2022-0009"]
    assert all(row["escalation_level"] == "critical" and row["warning_stage"] == "overdue" for row in overdue_rows)

    # --- 去重狀態檔：同階段第二次不重複轟炸法務長，但台帳照樣入帳 ---
    assert all(item["is_suppressed"] for item in second["notices"])
    assert second["ledger_rows"] == {"contract": 7, "licence": 4, "policy": 4}
