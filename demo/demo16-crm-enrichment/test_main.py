"""demo16 測試（契約 §8：happy / edge / integration 三個）。

全部離線執行，不呼叫任何真實 API；狀態檔與 CSV 一律指到 pytest 的 tmp_path，
跑完測試不會在模組目錄留下 state/。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import enricher  # noqa: E402
import main  # noqa: E402
from enricher import (  # noqa: E402
    ACTION_NO_DATA,
    STATUS_FAILED,
    ProviderError,
    ProviderResult,
    decide_field,
    enrich_contact,
    is_blank,
)

#: 與 config.yaml 的 enrichment.target_fields 一致
TARGET_FIELDS = (
    "industry",
    "sic_code",
    "employee_count",
    "annual_revenue",
    "tech_stack",
    "job_title",
    "company_country",
)


def _args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    """組出 run() 需要的 Namespace，預設走 mock + console，檔案全部寫暫存目錄。"""
    return main.build_parser().parse_args(
        [
            "--mock",
            "--notify", "console",
            "--state-file", str(tmp_path / "enrichment-state.json"),
            "--csv-out", str(tmp_path / "enrichment-report.csv"),
            *extra,
        ]
    )


def test_happy_path(tmp_path):
    """六筆聯絡人：一筆近期已豐富化被跳過，其餘五筆補值、評分、排序、輸出 CSV。"""
    result = main.run(_args(tmp_path))

    assert result["module_id"] == "16"
    assert result["mode"] == "mock"
    assert result["dry_run"] is False

    # C-1006 四天前才豐富化過 -> 跳過，不消耗外部額度
    assert result["run"] == {
        "scanned": 6,
        "processed": 5,
        "skipped": 1,
        "failed_providers": [],
    }
    assert result["skipped_contacts"][0]["contact_id"] == "C-1006"

    # C-1002 與 C-1005 各補 7 個空欄位；C-1004 三個欄位衝突；C-1003 外部查無資料
    assert result["totals"]["fields_filled"] == 14
    assert result["totals"]["conflicts"] == 3
    assert result["totals"]["enrichment_failed"] == 1
    assert result["bands"] == {"hot": 2, "warm": 1, "cool": 2}

    # 權重全部來自 config.yaml，分數必須可完全重現
    assert result["scores"]["C-1001"]["score"] == "100.0"
    assert result["scores"]["C-1002"]["score"] == "51.6"
    assert result["scores"]["C-1004"]["band"] == "cool"

    # 高分卻 135 天沒人聯絡 = 書中要撈出來的沉睡機會，且不因 stale 被降級
    sleeping = result["scores"]["C-1005"]
    assert sleeping["score"] == "84.5"
    assert (sleeping["band"], sleeping["grade"]) == ("hot", "hot")
    assert sleeping["is_reengagement_target"] is True
    assert sleeping["days_since_contact"] == 135

    # 交付物：CSV 報告 + 狀態檔；mock 模式一律不連 CRM
    assert result["apply"]["reason"] == "mock"
    assert result["apply"]["crm_written"] is False
    csv_lines = Path(result["apply"]["csv_file"]).read_text(encoding="utf-8-sig").splitlines()
    assert len(csv_lines) == 6  # 表頭 + 5 筆
    assert Path(result["apply"]["state_file"]).is_file()
    assert result["delivery"]["delivered"] is True


def test_edge_case_missing_external_data_never_overwrites(tmp_path):
    """邊界（本模組最重要的一條）：外部查無資料時，既有 CRM 值必須原封不動。

    絕不可用空字串、None 或猜測值覆蓋——會覆蓋的豐富化，比不做豐富化更糟。
    """
    contact = {
        "contact_id": "C-9001",
        "company": "Ghost Ltd",
        "domain": "ghost.example",
        "industry": "Logistics",
        "sic_code": "49410",
        "employee_count": 0,          # 0 是荒謬值，但仍是「有填」，不得被視為空白
        "annual_revenue": "750000.00",
        "tech_stack": ["notion"],
        "job_title": "Operations Owner",
        "company_country": "GB",
    }
    empty_source = ProviderResult("apollo", "Apollo", TARGET_FIELDS, {})

    record = enrich_contact(contact, [empty_source], [], TARGET_FIELDS, [], "2026-08-24T02:00:00")

    assert record.status == STATUS_FAILED
    assert record.filled == ()
    for field_name in TARGET_FIELDS:
        assert record.record[field_name] == contact[field_name], f"{field_name} 被覆蓋了"
    assert all(item.action == ACTION_NO_DATA for item in record.decisions)
    assert all(item.external_value is None for item in record.decisions)

    # 0 / False 不是空白；"N/A"、"-"、空清單才是
    assert is_blank(0) is False and is_blank(False) is False
    assert is_blank("N/A") is True and is_blank([]) is True and is_blank(None) is True

    # 外部回空字串同樣不構成候選值，因此仍走「查無資料、保留原值」
    blank_source = ProviderResult("apollo", "Apollo", ("industry",), {"ghost.example": {"industry": "  "}})
    only_blank = enrich_contact(contact, [blank_source], [], ("industry",), [], "2026-08-24T02:00:00")
    assert only_blank.record["industry"] == "Logistics"
    assert only_blank.decisions[0].action == ACTION_NO_DATA

    # 既有值與外部值衝突時，一律保留 CRM 值（外部值只進待審清單）
    conflict = decide_field("employee_count", 40, [("Apollo", 800)])
    assert conflict.is_conflict is True and conflict.is_write is False

    # 整條流程跑一次：C-1003 三個來源都查不到，欄位必須與 CRM 原值相同
    result = main.run(_args(tmp_path))
    ghost = next(item for item in result["contacts"] if item["contact_id"] == "C-1003")
    assert ghost["status"] == STATUS_FAILED
    assert ghost["filled_fields"] == [] and ghost["conflict_fields"] == []
    assert result["scores"]["C-1003"]["is_low_confidence"] is True


def test_integration_provider_failure_still_delivers(tmp_path, monkeypatch, capsys):
    """整合：Apollo 掛掉時仍照常產出名單、標「N 個來源無回應」、走 Diagnostics.amber。

    同時驗證自主權階梯（draft 不寫 CRM）與 dry-run 的變更計畫輸出。
    """
    real_loader = enricher.load_provider

    def _boom(entry, base_dir):
        if entry.get("id") == "apollo":
            raise ProviderError("Apollo API 逾時（模擬故障）")
        return real_loader(entry, base_dir)

    monkeypatch.setattr(enricher, "load_provider", _boom)

    result = main.run(_args(tmp_path, "--dry-run"))

    # 單一來源失敗不中斷其他來源
    assert result["is_partial"] is True
    assert [item["provider_id"] for item in result["failed_providers"]] == ["apollo"]
    assert "⚠️ 1 個來源無回應：Apollo" in result["report_text"]
    assert result["amber_count"] >= 1

    # Companies House 與 LinkedIn 照常供應欄位：C-1002 仍補到 4 個（少了 Apollo 的 3 個）
    kite = next(item for item in result["contacts"] if item["contact_id"] == "C-1002")
    assert sorted(kite["filled_fields"]) == [
        "company_country", "industry", "job_title", "sic_code",
    ]

    # 資料變殘缺時分數會下修（C-1002 從 51.6/warm 掉到 36.0/cool），
    # 而且系統誠實地把缺項標出來，不假裝完整、也不推估補值
    assert result["scores"]["C-1002"]["score"] == "36.0"
    assert result["scores"]["C-1002"]["band"] == "cool"
    assert "employee_count" not in kite["filled_fields"]

    sleeping = result["scores"]["C-1005"]
    assert sleeping["score"] == "53.9"  # LinkedIn 仍供應 employee_count，故未歸零
    assert sleeping["is_reengagement_target"] is True
    assert sleeping["is_low_confidence"] is True
    assert sorted(sleeping["missing_inputs"]) == ["revenue_band", "tech_stack"]

    # --dry-run：印出「哪個欄位、從什麼值變成什麼值」，且不寫任何檔案
    assert "變更計畫（尚未寫入 CRM）" in result["change_plan"]
    assert "industry" in result["change_plan"] and "→" in result["change_plan"]
    assert result["apply"] == {
        "crm_written": False,
        "records_written": 0,
        "csv_file": None,
        "state_file": None,
        "reason": "dry-run",
    }
    assert not (tmp_path / "enrichment-report.csv").exists()
    assert not (tmp_path / "enrichment-state.json").exists()

    # dry-run 不發送，但流程仍完整跑完
    assert result["delivery"]["delivered"] is False
    assert result["delivery"]["reason"] == "dry-run"
    assert "Apollo" in capsys.readouterr().err

    # 走 main() 的實際印出路徑：只驗 result["change_plan"] 會繞過 CLI 輸出邏輯，
    # 而 --dry-run 的全部價值就是「讓人在終端機上看到將要改什麼」。
    # 看不到 = 功能不存在，因此這裡用 capsys 斷言 stdout 真的有變更計畫。
    monkeypatch.setattr(
        sys, "argv",
        ["main.py", "--mock", "--dry-run", "--notify", "console",
         "--state-file", str(tmp_path / "cli-state.json"),
         "--csv-out", str(tmp_path / "cli-report.csv")],
    )
    assert main.main() == 0
    stdout = capsys.readouterr().out
    assert "變更計畫（尚未寫入 CRM）" in stdout
    assert "Kite Labs" in stdout
    assert "industry" in stdout and "→" in stdout  # 從什麼值變成什麼值
    assert "保留 CRM" in stdout                     # 衝突欄位的處置也要看得到
    assert not (tmp_path / "cli-report.csv").exists()
