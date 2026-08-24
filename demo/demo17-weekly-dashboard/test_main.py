"""demo17 測試（契約 §8：happy / edge / integration 三個）。

全部離線執行，不呼叫任何真實 API，也不寫入版控中的 mock/ 檔案
（需要狀態檔的測試一律用 pytest 的 tmp_path）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import aggregator  # noqa: E402
import main  # noqa: E402
from _shared.llm_client import LLMError  # noqa: E402

MOCK_DIR = MODULE_DIR / "mock"


def _args(**overrides) -> argparse.Namespace:
    """組出 run() 需要的 Namespace，預設走 mock + console + 不寫狀態檔。"""
    base = {
        "mock": True,
        "dry_run": False,
        "notify": "console",
        "config": str(MODULE_DIR / "config.yaml"),
        "state_file": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _metric(result: dict, kpi_id: str) -> dict:
    """從結果 dict 中挑出指定 KPI，找不到就讓測試明確失敗。"""
    for block in result["blocks"]:
        for item in block["metrics"]:
            if item["kpi_id"] == kpi_id:
                return item
    raise AssertionError(f"結果中找不到 KPI {kpi_id}")


def _block(result: dict, block_id: str) -> dict:
    for block in result["blocks"]:
        if block["block_id"] == block_id:
            return block
    raise AssertionError(f"結果中找不到區塊 {block_id}")


def test_happy_path():
    """正常週：六源都回應、四週歷史齊全，WoW / 移動平均 / 燈號 / 異常全部算對。"""
    result = main.run(_args())

    assert result["module_id"] == "17"
    assert result["week_id"] == "2026-W33"
    assert result["comparison_week_id"] == "2026-W32"
    assert result["history_weeks"] == 4
    assert result["is_partial"] is False
    assert result["failed_sources"] == []
    assert len(result["blocks"]) == 4
    assert sum(len(block["metrics"]) for block in result["blocks"]) == 18

    # 13,000 → 13,650 = +5.0%；四週均 (12000+12500+12800+13000)/4 = 12,575
    sessions = _metric(result, "ga4_sessions")
    assert sessions["value"] == "13650"
    assert sessions["wow_pct"] == "5.0"
    assert sessions["moving_avg"] == "12575.00"
    assert sessions["rag"] == "green"

    # 廣告花費 4,000 → 4,800 = +20%。direction=down，所以是紅燈 + 異常。
    spend = _metric(result, "google_ads_spend")
    assert spend["wow_pct"] == "20.0"
    assert spend["rag"] == "red"
    assert spend["is_anomaly"] is True

    # 新增名單 120 → 144 = +20%：同樣觸發異常，但方向對客戶有利 → 綠燈。
    leads = _metric(result, "crm_new_leads")
    assert leads["wow_pct"] == "20.0"
    assert leads["is_anomaly"] is True
    assert leads["rag"] == "green"

    assert _block(result, "advertising")["rag"] == "red"
    assert _block(result, "pipeline")["rag"] == "green"
    assert result["overall_rag"] == "red"
    assert len(result["anomalies"]) == 2

    # Focus Actions 依 favourable_pct 升冪：-20% 花費、-10% ROAS、-5% 停留時間
    assert [item["kpi_id"] for item in result["focus_actions"]] == [
        "google_ads_spend",
        "google_ads_roas",
        "ga4_avg_session_seconds",
    ]

    assert result["deliver_day"] == "monday"
    assert result["deliver_at"] == "07:00"
    assert result["delivery"]["delivered"] is True
    assert result["amber_count"] == 0
    assert "每週績效儀表板" in result["report_text"]
    assert "部分資料" not in result["report_text"]
    # 未指定 --state-file 時絕不動任何檔案，避免跑一次就弄髒版控中的 mock/。
    assert result["state"]["written"] is False


def test_edge_case_first_week_without_history(tmp_path):
    """邊界：首週沒有任何上週資料可比較。

    所有 WoW 必須是 None 而不是 0——「上週沒有資料」和「這週沒變化」是完全
    不同的兩件事，把首週當成 0 會算出假的 +100%，管理層會據此開錯的會。
    跑完後狀態檔應寫入本週基準線，供下週比較。
    """
    state_file = tmp_path / "weekly-state.json"
    state_file.write_text(
        (MOCK_DIR / "history-first-week.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = main.run(_args(state_file=str(state_file)))

    assert result["has_comparison"] is False
    assert result["comparison_week_id"] is None
    assert result["history_weeks"] == 0
    assert result["overall_rag"] == "unknown"
    assert result["anomalies"] == []
    assert result["focus_actions"] == []

    sessions = _metric(result, "ga4_sessions")
    assert sessions["value"] == "13650"  # 絕對值仍要取得
    assert sessions["previous"] is None
    assert sessions["wow_pct"] is None  # 不可是 "0.0"
    assert sessions["moving_avg"] is None
    assert sessions["rag"] == "unknown"  # 灰燈，不是綠燈

    assert "首次執行" in result["report_text"]
    assert "無上週資料" in result["report_text"]
    assert result["delivery"]["delivered"] is True

    # 首週跑完必須留下基準線，否則下週依然沒有比較對象。
    assert result["state"]["written"] is True
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert [week["week_id"] for week in saved["weeks"]] == ["2026-W33"]
    assert saved["weeks"][0]["values"]["ga4_sessions"] == "13650"


def test_integration_meta_outage_still_delivers(monkeypatch, capsys):
    """整合：Meta 資料源故障時，報表仍產出、標記部分資料、走 Diagnostics.amber。

    這是本模組的核心保證——六個資料源掛掉一個，不得讓整份週報失敗。
    同時驗證與 `_shared` 的互動：diagnostics 記琥珀燈（不是紅色警報退出）、
    notifier console 通道實際送出、autonomy 閘門不擋本機列印。
    """
    real_fetch = aggregator.fetch_source

    def _redirect(mock_path: Path, source_id: str):
        # 直接改讀「某源故障週」的 fixture，驗證真實的 error payload 路徑。
        if source_id == "meta":
            return real_fetch(MOCK_DIR / "meta-outage.json", source_id)
        return real_fetch(mock_path, source_id)

    monkeypatch.setattr(aggregator, "fetch_source", _redirect)

    result = main.run(_args())

    # 報表照常產出，但明確標記為部分資料
    assert result["is_partial"] is True
    assert [item["source_id"] for item in result["failed_sources"]] == ["meta"]
    assert "Meta Marketing API 回應 500" in result["failed_sources"][0]["reason"]
    assert "⚠️ 部分資料：Meta Ads 無回應" in result["report_text"]

    # 故障源的 KPI 留在報表上標「無資料」，不補 0、不從畫面消失
    meta_spend = _metric(result, "meta_spend")
    assert meta_spend["value"] is None
    assert meta_spend["wow_pct"] is None
    assert meta_spend["rag"] == "unknown"
    assert "Meta Ads 無回應" in meta_spend["unavailable_reason"]
    assert _block(result, "advertising")["is_partial"] is True
    assert "無資料" in result["report_text"]

    # 同區塊的 Google Ads 照算，其他區塊完全不受影響
    assert _metric(result, "google_ads_spend")["wow_pct"] == "20.0"
    assert _block(result, "finance")["is_partial"] is False
    assert result["overall_rag"] == "red"
    assert len(result["anomalies"]) == 2
    assert len(result["focus_actions"]) == 3

    # 走了 Diagnostics 的琥珀燈，而不是紅色警報退出
    assert result["amber_count"] >= 1

    # 仍然實際送出（console 通道），不是靜默失敗
    assert result["delivery"]["delivered"] is True
    assert result["delivery"]["channel"] == "console"
    assert "部分資料" in capsys.readouterr().out


def test_dry_run_console_prints_report(monkeypatch, capsys):
    """回歸測試（happy path）：--dry-run + console 通道時，main() 仍須印出完整週報。

    dry-run 模式下 deliver() 直接 return，從未呼叫 Notifier，
    main() 若只靠「channel != console」判斷是否要印，會漏掉這個組合，
    終端機只剩下 stderr 的診斷訊息，週報文字整份消失。
    """
    monkeypatch.setattr(
        sys,
        "argv",
        ["main.py", "--mock", "--dry-run", "--config", str(MODULE_DIR / "config.yaml")],
    )

    exit_code = main.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "每週績效儀表板" in output
    assert "2026-W33" in output


def test_dry_run_non_console_prints_report_once(monkeypatch, capsys):
    """回歸測試（edge case）：非 console 通道 + --dry-run 時週報不可被印兩次。

    確保修復判斷式的 `or` 條件不會讓非 console 通道疊加印出兩份週報。
    """
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "main.py",
            "--mock",
            "--dry-run",
            "--notify",
            "telegram",
            "--config",
            str(MODULE_DIR / "config.yaml"),
        ],
    )

    exit_code = main.main()

    output = capsys.readouterr().out
    assert exit_code == 0
    # 用含表情符號的報表抬頭行比對（而非裸字串），
    # 因為 AI 敘述摘要區塊的 mock 內容可能回顯提示詞裡同樣的字樣，
    # 用裸字串比對會誤判成「印了兩次」。
    assert output.count("📊 每週績效儀表板｜") == 1


def test_main_catches_llm_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--live 模式下 CLI 逾時等狀況會拋 LLMError；main() 必須吃下來變成 exit code 1，
    而不是讓 raw traceback 砸給使用者（demo11 既有慣例的補齊）。
    """

    def _raise_llm_error(args: argparse.Namespace) -> dict:
        raise LLMError("模擬 CLI 逾時")

    monkeypatch.setattr(main, "run", _raise_llm_error)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    exit_code = main.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "錯誤：" in captured.err
    assert "模擬 CLI 逾時" in captured.err
