"""demo13 客戶入職工作流測試（3 個案例：happy / edge / integration）。

全部離線可跑：不呼叫任何外部 API、不需要憑證。
狀態檔一律以 --state-file 指到 tmp_path，跑完不會在模組目錄留下 state/。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main  # noqa: E402
import onboarding  # noqa: E402
from onboarding import StageOutcome, load_sequence, missing_required  # noqa: E402
from state_machine import (  # noqa: E402
    ClientOnboarding,
    DuplicateStageError,
    OnboardingEvent,
    OnboardingState,
    StateTransitionError,
)


def _args(tmp_path: Path, *extra: str):
    """用正式的 parser 產生 Namespace，確保測試走的是真實 CLI 介面。"""
    state_file = str(tmp_path / "onboarding-state.json")
    return main.build_parser().parse_args(["--mock", "--state-file", state_file, *extra])


def _base_config() -> dict:
    """讀取模組正式設定，供測試改寫成臨時設定用。"""
    return yaml.safe_load((MODULE_DIR / "config.yaml").read_text(encoding="utf-8"))


def _stages_by_key(record: dict) -> dict[str, dict]:
    """把一位客戶的階段結果轉成 key -> 結果，方便逐階段斷言。"""
    return {stage["stage_key"]: stage for stage in record["stages"]}


def test_happy_path(tmp_path: Path) -> None:
    """標準批次：全新 / 進行中 / 已完成 / 重複觸發 / 資料缺漏 五種觸發各自走對路。"""
    result = main.run(_args(tmp_path))

    # --state-file 必須真的生效（狀態寫到暫存目錄，而非模組目錄）
    assert result["state_file"] == str(tmp_path / "onboarding-state.json")
    assert Path(result["state_file"]).exists()
    assert result["mode"] == "mock"
    assert result["dry_run"] is False
    assert len(result["clients"]) == 5

    clients = {
        record["client_id"]: record
        for record in result["clients"]
        if not record["is_duplicate_trigger"]
    }

    # 全新客戶：Day 0 兩個階段當輪交付，Day 3 之後的階段尚未到期
    brand_new = _stages_by_key(clients["CL-1001"])
    assert brand_new["welcome_pack"]["outcome"] == StageOutcome.SENT.value
    assert brand_new["kickoff_booking"]["outcome"] == StageOutcome.SENT.value
    assert brand_new["kickoff_meeting"]["outcome"] == StageOutcome.PENDING.value
    assert clients["CL-1001"]["final_state"] == OnboardingState.KICKOFF_SCHEDULED.value
    # WELCOME_PACK_TEMPLATE 必須真的插進客戶資料，且不得留下未替換的佔位符
    welcome_text = brand_new["welcome_pack"]["messages"][0]
    assert "林佩珊" in welcome_text and "Brightleaf Living" in welcome_text
    assert "陳彥廷" in welcome_text and "calendly.com/yenting-openclaw" in welcome_text
    assert "{" not in welcome_text
    # Output：Slack #new-clients 內部通知與歡迎信同一階段交付
    assert "#new-clients" in brand_new["welcome_pack"]["messages"][1]

    # 進行到一半的客戶：前三階段冪等跳過，本輪只做 Day 7
    mid = _stages_by_key(clients["CL-1002"])
    assert mid["welcome_pack"]["outcome"] == StageOutcome.SKIPPED_DONE.value
    assert mid["day7_checkin"]["outcome"] == StageOutcome.SENT.value
    assert mid["day30_review"]["outcome"] == StageOutcome.PENDING.value
    assert clients["CL-1002"]["final_state"] == OnboardingState.DAY7_DONE.value

    # 已完成的客戶：整條管線完全靜默
    done = _stages_by_key(clients["CL-1003"])
    assert all(stage["outcome"] == StageOutcome.SKIPPED_DONE.value for stage in done.values())

    assert result["stages_delivered"] == 3
    assert result["stages_skipped_idempotent"] == 8
    assert result["stages_blocked"] == 1
    assert result["duplicate_triggers"] == 1
    # 金額一律 Decimal 字串：32 小時 × $75 = $2,400/月
    assert result["financials"]["recovered_value_per_month"] == "2400.00"
    assert result["financials"]["client_monthly_price"] == "180.00"


def test_edge_case_blocked_and_empty_queue(tmp_path: Path) -> None:
    """邊界：資料缺漏必須明確標示卡關原因且不得靜默跳過；空觸發佇列不可炸。"""
    result = main.run(_args(tmp_path))
    blocked = _stages_by_key(
        next(record for record in result["clients"] if record["client_id"] == "CL-1004")
    )

    # 卡在 Day 0，缺哪幾個欄位要逐一列出（這份清單就是人工補件的工單）
    assert blocked["welcome_pack"]["outcome"] == StageOutcome.BLOCKED.value
    assert blocked["welcome_pack"]["missing_fields"] == [
        "contact_email",
        "sales_notes",
        "account_manager.name",
        "account_manager.email",
    ]
    assert blocked["welcome_pack"]["messages"] == []
    # 後續四個階段一律標示「前置未完成」，不可越過卡關階段直接寄第 7 天關心信
    for key in ("kickoff_booking", "kickoff_meeting", "day7_checkin", "day30_review"):
        assert blocked[key]["outcome"] == StageOutcome.PENDING.value
        assert "前置階段" in blocked[key]["reason"]
    assert "需人工補件" in result["summary"]
    # 只有空白字元的欄位也算缺漏（CRM 常見「欄位存在但沒填」）
    assert missing_required({"contact_email": "   "}, ("contact_email",)) == ("contact_email",)

    # 空觸發佇列：不炸、不寫出空報表，摘要仍要可讀
    config = _base_config()
    empty_file = tmp_path / "clients.json"
    empty_file.write_text(json.dumps({"triggers": []}), encoding="utf-8")
    config["mock"]["clients_file"] = str(empty_file)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    empty_result = main.run(_args(tmp_path, "--config", str(config_path)))
    assert empty_result["clients"] == []
    assert empty_result["stages_delivered"] == 0
    assert empty_result["summary"]


def test_integration_idempotency_autonomy_and_notifier(tmp_path: Path) -> None:
    """整合：重跑不重寄（冪等）、autonomy 停在 draft、console 通道送達、非法轉移拋錯。"""
    first = main.run(_args(tmp_path))
    second = main.run(_args(tmp_path))

    # 第二輪：同一個狀態檔 -> 一個階段都不再交付，全部冪等跳過
    assert first["stages_delivered"] == 3
    assert second["stages_delivered"] == 0
    replayed = _stages_by_key(
        next(record for record in second["clients"] if record["client_id"] == "CL-1001")
    )
    assert replayed["welcome_pack"]["outcome"] == StageOutcome.SKIPPED_DONE.value
    assert replayed["welcome_pack"]["messages"] == []
    # 去重鍵在兩輪之間必須一致，外部系統才擋得住重送
    assert replayed["welcome_pack"]["idempotency_key"] == "CL-1001:welcome_pack"

    # 同一批次內的重複 webhook 也要被擋（第一道防線）
    duplicate = next(record for record in first["clients"] if record["is_duplicate_trigger"])
    assert duplicate["client_id"] == "CL-1001"
    assert duplicate["stages"] == []

    # 自主權：預設 draft，歡迎信一律進草稿，不得自動寄給客戶
    brand_new = next(record for record in first["clients"] if record["client_id"] == "CL-1001")
    assert brand_new["autonomy_level"] == "draft"
    assert brand_new["is_auto_sent"] is False
    assert "草稿" in _stages_by_key(brand_new)["welcome_pack"]["delivery"]

    # diagnostics 有記錄卡關與重複觸發；console 通道視為送達
    assert first["amber_count"] >= 2
    assert first["is_notified"] is True
    assert first["warnings"] == []

    # 狀態機白名單：在錯的階段做對的事必須當場爆掉
    machine = ClientOnboarding(client_id="CL-9999", company="測試")
    with pytest.raises(StateTransitionError):
        machine.apply(OnboardingEvent.SEND_DAY7_CHECKIN)
    machine.apply(OnboardingEvent.DEAL_CLOSED_WON)
    machine.mark_stage_done("welcome_pack", OnboardingEvent.SEND_WELCOME_PACK, "2026-09-30T09:00:00")
    with pytest.raises(DuplicateStageError):
        machine.mark_stage_done(
            "welcome_pack", OnboardingEvent.SEND_WELCOME_PACK, "2026-09-30T09:01:00"
        )


def test_render_stage_messages_keeps_template_name(tmp_path: Path) -> None:
    """happy path：render_stage_messages 的新回傳型別要帶上樣板名稱。

    welcome_pack 這個 stage 底下有兩個樣板（客戶信 + 內部 Slack 通知），
    下游的 _polish() 必須能區分兩者，回傳型別因此從純文字清單改成
    (樣板名稱, 文字) 的 tuple 清單——這裡直接驗證 onboarding.py 那一層。
    """
    config = _base_config()
    templates = dict(config.get("templates", {}))
    stages = load_sequence(config)
    stage = next(item for item in stages if item.key == "welcome_pack")

    # 最小 context：不關心佔位符是否全部解析，只驗證回傳的 (name, text) 結構
    messages, _unresolved = onboarding.render_stage_messages(templates, stage, {})

    assert isinstance(messages, list)
    assert len(messages) == 2
    assert messages[0][0] == "welcome_pack"
    assert isinstance(messages[0][1], str)
    assert messages[1][0] == "slack_new_client"
    assert isinstance(messages[1][1], str)


def test_polish_skips_llm_for_internal_only_templates(tmp_path: Path) -> None:
    """edge case（regression 防呆）：內部樣板一律跳過 LLM 潤飾，原樣送出。

    這是實測 --live 重現過的 bug：slack_new_client／kickoff_meeting 這類內部
    通知若被套用「你是客戶經理，正在寫信給客戶」的提示詞，LLM 會把聯絡人
    email、客戶經理、成交時間等內部追蹤欄位整個改寫消失。本測試用假的
    ctx（is_mock=False，代表 live 模式）與假的 llm，斷言：
      1. 回傳值就是原始文字（沒被潤飾）
      2. llm.complete() 完全沒被呼叫過
    """
    config = _base_config()
    stages = load_sequence(config)
    welcome_stage = next(item for item in stages if item.key == "welcome_pack")
    kickoff_stage = next(item for item in stages if item.key == "kickoff_meeting")

    ctx = MagicMock()
    ctx.is_mock = False
    ctx.llm = MagicMock()
    ctx.prompts = {}

    original_slack_text = "原始內部通知文字"
    result_slack = main._polish(ctx, welcome_stage, "slack_new_client", original_slack_text)
    assert result_slack == original_slack_text
    ctx.llm.complete.assert_not_called()

    original_kickoff_text = "原始啟動會議提醒文字"
    result_kickoff = main._polish(ctx, kickoff_stage, "kickoff_meeting", original_kickoff_text)
    assert result_kickoff == original_kickoff_text
    ctx.llm.complete.assert_not_called()


def test_integration_internal_notice_not_rewritten_as_customer_letter(tmp_path: Path) -> None:
    """integration：跑一次完整 mock 流程，slack_new_client 訊息不能被誤潤飾或錯位。

    mock 模式本身就不會呼叫 LLM（is_mock 短路），但這裡驗證的是整條管線
    组裝出來的最終訊息內容正確對應到 slack_new_client 樣板本身的關鍵欄位，
    確保 render_stage_messages 回傳型別變更後、_execute_stage 解構 (name, text)
    的順序沒有被打亂，訊息也沒有跑錯位置。
    """
    result = main.run(_args(tmp_path))
    brand_new = next(
        record for record in result["clients"] if record["client_id"] == "CL-1001"
    )
    stages = _stages_by_key(brand_new)
    slack_message = stages["welcome_pack"]["messages"][1]

    assert "Slack" in slack_message
    assert "新客戶成交" in slack_message
    assert "陳彥廷" in slack_message  # 聯絡人 email 對應的姓名，內部追蹤欄位仍在
