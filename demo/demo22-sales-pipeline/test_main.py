"""模組 #22 的三個測試（happy / edge / integration）。

integration 測試是本模組最重要的一個：它同時驗證兩條企業級硬規則
1. `halt_on_reply` 真的不可停用——即使 config 刻意把它關掉，已回覆的客戶
   仍然零外送。
2. Enrichment 的 `<2 小時` SLA 超時**會叫**——同時反映在 AMBER 計數、
   `warnings` 文字與 JSONL 稽核軌跡三處，不允許任何一處靜默。

所有測試一律離線（`--mock`），且把狀態檔與稽核檔導到 tmp_path，
不污染模組目錄下的正式檔案。
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta, timezone as dt_timezone
from pathlib import Path

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo22  # noqa: E402
from _shared.llm_client import LLMError  # noqa: E402
from audit import (  # noqa: E402
    ACTION_CHAIN_HALTED,
    ACTION_EVENT_REJECTED,
    ACTION_SAFETY_OVERRIDE,
    ACTION_SLA_BREACH,
    read_entries,
    verify_entries,
)
from pipeline import (  # noqa: E402
    HALT_REPLIED,
    HALT_SEQUENCE_COMPLETE,
    REJECT_ILLEGAL_TRANSITION,
    IllegalTransitionError,
    SalesPipeline,
)

BASE_CONFIG_PATH = MODULE_DIR / "config.yaml"
REPLIED_DEAL_ID = "D-2206"          # 已回覆，安全機制主測案例
WHITELISTED_DEAL_ID = "D-2205"      # 白名單命中，唯一應自動送出的交易
SLA_BREACH_DEAL_ID = "D-2202"       # Enrichment 逾時 5 小時
ILLEGAL_EVENT_DEAL_ID = "D-2210"    # discovery -> closed_won 非法轉移


def _load_base_config() -> dict:
    """讀出正式 config.yaml 作為各測試的基底。"""
    return yaml.safe_load(BASE_CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, config: dict) -> Path:
    """把改過的設定寫成臨時 config.yaml，避免測試污染正式設定。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _run(tmp_path: Path, config_path: Path | None = None) -> dict:
    """以 --mock 跑一次主流程；狀態檔與稽核檔一律導到 tmp_path。"""
    argv = [
        "--mock",
        "--notify",
        "console",
        "--state-file",
        str(tmp_path / "state.json"),
        "--audit-file",
        str(tmp_path / "audit.jsonl"),
    ]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return demo22.run(demo22.build_parser().parse_args(argv))


def _ids(entries: list) -> set[str]:
    """取出一組結果項目的 deal_id。"""
    return {str(item["deal_id"]) for item in entries}


def _freeze_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """把時區解析換成固定 UTC+8，讓測試不受本機有無 tzdata 影響。

    `resolve_timezone()` 在缺 IANA 時區資料庫的機器（Windows 預設即是）上會
    找不到 Asia/Taipei，降級並發出一則 AMBER——這與被測的業務邏輯完全無關，
    卻會污染 `amber_count`。時區不是這裡要驗的東西，直接把變因移除。
    """
    monkeypatch.setattr(
        demo22,
        "resolve_timezone",
        lambda name, fallback_offset_hours=8: (dt_timezone(timedelta(hours=8)), None),
    )


def test_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """標準 mock 輸入：10 筆交易 -> 8 個待審草稿 + 2 筆中止 + 2 筆 SLA 超時。"""
    _freeze_timezone(monkeypatch)

    result = _run(tmp_path)

    assert result["module_id"] == "22"
    assert result["mode"] == "mock"
    assert result["total_deals"] == 10
    assert result["halt_on_reply"] is True

    # 預設自主權為 DRAFT，因此不該有任何自動送出
    assert result["executed"] == []
    assert len(result["drafted"]) == 8
    assert len(result["halted"]) == 2

    # 階段狀態機：合法事件生效、非法事件被擋、交易維持原階段
    assert result["stage_counts"] == {
        "lead_captured": 2,
        "discovery": 3,
        "proposal_sent": 3,
        "closed_won": 1,
        "closed_lost": 1,
    }
    assert len(result["rejected_events"]) == 1
    rejected = result["rejected_events"][0]
    assert rejected["deal_id"] == ILLEGAL_EVENT_DEAL_ID
    assert rejected["reason"] == REJECT_ILLEGAL_TRANSITION

    # 五條鏈路各自被正確觸發
    by_id = {item["deal_id"]: item for item in result["drafted"]}
    assert by_id["D-2201"]["chain"] == "enrichment"
    assert by_id["D-2203"]["chain"] == "proposal"
    assert by_id["D-2205"]["chain"] == "follow_up"
    assert by_id["D-2205"]["step_day"] == 2
    assert by_id["D-2208"]["chain"] == "onboarding"
    assert by_id["D-2209"]["chain"] == "renurture"
    assert by_id["D-2209"]["step_day"] == 30
    # 非法事件被擋下後，該交易仍留在 discovery 並照常跑提案鏈
    assert by_id[ILLEGAL_EVENT_DEAL_ID]["chain"] == "proposal"

    # 每筆草稿都要有實際內容（mock 模式由 LLMClient 回傳佔位字串）
    assert all(item["body"] for item in result["drafted"])

    # 中止原因涵蓋「已回覆」與「序列已跑完」兩條路徑
    halts = {item["deal_id"]: item["reason"] for item in result["halted"]}
    assert halts[REPLIED_DEAL_ID] == HALT_REPLIED
    assert halts["D-2207"] == HALT_SEQUENCE_COMPLETE

    # 金額一律 Decimal 計算後以字串輸出（未結案交易總和）
    assert result["open_pipeline_value_usd"] == "537000.00"

    # SLA：兩筆逾時，且都不是靜默的（amber = 1 事件拒絕 + 2 SLA）
    assert {item["deal_id"] for item in result["sla_breaches"]} == {"D-2202", "D-2204"}
    assert result["amber_count"] == 3


def test_edge_case_empty_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """邊界：CRM 沒有任何交易與事件時要安靜跑完，不可拋例外。"""
    _freeze_timezone(monkeypatch)

    empty_deals = tmp_path / "deals_empty.json"
    empty_deals.write_text("[]", encoding="utf-8")
    empty_events = tmp_path / "events_empty.json"
    empty_events.write_text("[]", encoding="utf-8")

    config = _load_base_config()
    config["mock"]["deals"] = str(empty_deals)
    config["mock"]["crm_events"] = str(empty_events)
    config_path = _write_config(tmp_path, config)

    result = _run(tmp_path, config_path)

    assert result["total_deals"] == 0
    assert result["executed"] == []
    assert result["drafted"] == []
    assert result["halted"] == []
    assert result["sla_breaches"] == []
    assert result["rejected_events"] == []
    assert result["planned_calls"] == []
    # 空管線不是異常，不該產生任何 AMBER 警示（時區已凍結，不會有假失敗）
    assert result["amber_count"] == 0
    assert result["warnings"] == []
    assert result["halt_on_reply"] is True
    assert result["open_pipeline_value_usd"] == "0.00"
    # 摘要仍要能組出來（不可因為空清單而崩在字串格式化）
    assert result["module_name"] in demo22._summarise(result)

    # 稽核軌跡即使無交易也要留下 run_started / run_completed 兩列
    entries = read_entries(result["audit_file"])
    assert verify_entries(entries) == []
    assert {item["action"] for item in entries} == {"run_started", "run_completed"}


def test_integration_halt_on_reply_and_sla_alerting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整合：halt_on_reply 強制生效 + SLA 超時警報 + 自主權降級 + 稽核軌跡。"""
    _freeze_timezone(monkeypatch)

    config = _load_base_config()
    # 刻意把最重要的安全開關關掉，程式必須拒絕接受
    config["pipeline"]["chains"]["follow_up"]["halt_on_reply"] = False
    # 開全自動，但白名單只放 D-2205 的網域
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["@bosen-tech.example.com"]
    config["runtime"]["days_in_draft"] = 30
    config_path = _write_config(tmp_path, config)

    result = _run(tmp_path, config_path)

    # 1. halt_on_reply 被強制覆寫，且透過 Diagnostics 發出 AMBER
    assert result["halt_on_reply"] is True
    assert any("halt_on_reply" in warning for warning in result["warnings"])

    # 2. 最重要的一條：已回覆的客戶不得出現在任何外送或草稿中
    delivered = _ids(result["executed"]) | _ids(result["drafted"])
    assert REPLIED_DEAL_ID not in delivered
    halted_by_reply = {
        item["deal_id"] for item in result["halted"] if item["reason"] == HALT_REPLIED
    }
    assert REPLIED_DEAL_ID in halted_by_reply

    # 3. SLA 超時必須「叫」：警告文字 + AMBER 計數 + 稽核軌跡三處都要有
    breach = next(
        item for item in result["sla_breaches"] if item["deal_id"] == SLA_BREACH_DEAL_ID
    )
    assert breach["chain"] == "enrichment"
    assert breach["sla_minutes"] == 120
    assert breach["overdue_minutes"] == 180  # 進站 5 小時 - 2 小時門檻
    assert any(SLA_BREACH_DEAL_ID in warning and "SLA" in warning for warning in result["warnings"])
    # amber = 1 事件拒絕 + 2 SLA 超時 + 1 halt_on_reply 強制覆寫
    assert result["amber_count"] == 4

    # 4. 自主權：命中白名單才自動送出，其餘一律降級為草稿
    assert _ids(result["executed"]) == {WHITELISTED_DEAL_ID}
    assert result["executed"][0]["autonomy"] == "supervised_auto"
    assert all(item["autonomy"] == "draft" for item in result["drafted"])

    # 5. 稽核軌跡：五個必要欄位齊全，且三類關鍵事件都留下紀錄
    entries = read_entries(result["audit_file"])
    assert verify_entries(entries) == []
    actions = {item["action"] for item in entries}
    assert {ACTION_SLA_BREACH, ACTION_EVENT_REJECTED, ACTION_SAFETY_OVERRIDE} <= actions
    reply_halt = [
        item
        for item in entries
        if item["action"] == ACTION_CHAIN_HALTED and item["subject"] == REPLIED_DEAL_ID
    ]
    assert reply_halt and HALT_REPLIED in reply_halt[0]["rationale"]
    # 自動送出的那一筆必須被標記為已取得人工核准（白名單即事前核准）
    executed_rows = [item for item in entries if item["subject"] == WHITELISTED_DEAL_ID]
    assert any(item["is_human_approved"] for item in executed_rows)

    # 6. 狀態機本身：非法轉移必須拋錯，不是靠呼叫端自律
    with pytest.raises(IllegalTransitionError):
        SalesPipeline.validate_transition("discovery", "closed_won")

    # 7. 結果必須可 JSON 序列化（供 CRM 回寫 / 稽核留存）
    assert json.loads(json.dumps(result, ensure_ascii=False))["total_deals"] == 10


def test_main_catches_llm_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--live 模式下 LLM 呼叫逾時等狀況會拋 LLMError；main() 必須吃下來變成 exit code 1，
    而不是讓 raw traceback 砸給使用者（demo11 既有慣例的補齊）。
    """

    def _raise_llm_error(args):
        raise LLMError("模擬 CLI 逾時")

    monkeypatch.setattr(demo22, "run", _raise_llm_error)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    exit_code = demo22.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "錯誤：" in captured.err
    assert "模擬 CLI 逾時" in captured.err
