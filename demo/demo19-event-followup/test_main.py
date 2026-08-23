"""模組 #19 的三個測試（happy / edge / integration）。

integration 測試是本模組最重要的一個：它驗證「與會者已回覆就絕對不再收到任何
後續」——即使 config 刻意把 stop_on_reply 關掉，即使跨兩次執行、帶著狀態檔。

三個測試都把時區解析凍結成固定 UTC+8。理由：`resolve_timezone()` 在缺 IANA
時區資料庫的機器（Windows 預設即是）上找不到 Asia/Taipei，會降級並發出一則
AMBER——這與被測的業務邏輯完全無關，卻會污染 amber_count，讓同一份程式碼在
不同機器上得到不同結果。時區不是這裡要驗的東西，直接把變因移除。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import timedelta, timezone as dt_timezone
from pathlib import Path

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo19  # noqa: E402
from segmenter import (  # noqa: E402
    HALT_NOT_DUE,
    HALT_REPLIED,
    HALT_SEQUENCE_COMPLETE,
    HALT_UNSUBSCRIBED,
)

BASE_CONFIG_PATH = MODULE_DIR / "config.yaml"
REPLIED_ATTENDEE_ID = "A-2006"
UNSUBSCRIBED_ATTENDEE_ID = "A-2007"
WHITELISTED_ATTENDEE_ID = "A-2001"


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


def _run(config_path: Path | None = None, state_file: Path | None = None) -> dict:
    """以 --mock 跑一次主流程。"""
    argv = ["--mock", "--notify", "console"]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    if state_file is not None:
        argv += ["--state-file", str(state_file)]
    return demo19.run(demo19.build_parser().parse_args(argv))


def _ids(entries: list) -> set[str]:
    """取出一組結果項目的 attendee_id。"""
    return {str(item["attendee_id"]) for item in entries}


def _freeze_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """把時區解析換成固定 UTC+8，讓測試不受本機有無 tzdata 影響。"""
    monkeypatch.setattr(
        demo19,
        "resolve_timezone",
        lambda name, fallback_offset_hours=8: (dt_timezone(timedelta(hours=8)), None),
    )


def test_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """標準 mock 名單：9 位與會者 -> 分三群、5 封草稿、4 筆中止、1 筆業務交棒。"""
    _freeze_timezone(monkeypatch)

    result = _run()

    assert result["module_id"] == "19"
    assert result["mode"] == "mock"
    assert result["total_attendees"] == 9
    assert result["stop_on_reply"] is True
    assert result["respect_unsubscribe"] is True
    assert result["is_compliant"] is True
    # 未指定 --state-file 且非 live，狀態檔完全不介入 -> 結果可重現
    assert result["state_file"] is None

    # 分眾結果：hot 3（含已回覆者）、warm 4（含已退訂者）、cold 2
    assert result["segment_counts"] == {"hot": 3, "warm": 4, "cold": 2, "unclassified": 0}

    # 預設自主權為 DRAFT，因此不該有任何自動送出
    assert result["sent"] == []
    assert len(result["drafted"]) == 5
    assert len(result["halted"]) == 4

    # 每一群都走自己的序列，且推進到正確的段
    by_id = {item["attendee_id"]: item for item in result["drafted"]}
    assert (by_id["A-2001"]["segment"], by_id["A-2001"]["step_type"]) == (
        "hot",
        "hot_personal",
    )
    assert (by_id["A-2002"]["segment"], by_id["A-2002"]["step_type"]) == (
        "hot",
        "hot_meeting_offer",
    )
    assert (by_id["A-2003"]["segment"], by_id["A-2003"]["step_type"]) == (
        "warm",
        "warm_recap",
    )
    assert (by_id["A-2004"]["segment"], by_id["A-2004"]["step_type"]) == (
        "warm",
        "warm_resource",
    )
    assert (by_id["A-2005"]["segment"], by_id["A-2005"]["step_type"]) == (
        "cold",
        "reengage_recording",
    )

    # 每封草稿都要有內容，且信尾必須帶退訂連結（行銷法遵）
    assert all(item["body"] for item in result["drafted"])
    assert all("退訂" in item["body"] for item in result["drafted"])

    # 中止原因涵蓋四條不同路徑
    halt_reasons = {item["attendee_id"]: item["reason"] for item in result["halted"]}
    assert halt_reasons[REPLIED_ATTENDEE_ID] == HALT_REPLIED
    assert halt_reasons[UNSUBSCRIBED_ATTENDEE_ID] == HALT_UNSUBSCRIBED
    assert halt_reasons["A-2008"] == HALT_SEQUENCE_COMPLETE
    assert halt_reasons["A-2009"] == HALT_NOT_DUE

    # 只有 hot 群且尚未交棒過的 A-2001 會被推進業務 Slack
    assert len(result["crm_handovers"]) == 1
    handover = result["crm_handovers"][0]
    assert handover["attendee_id"] == WHITELISTED_ATTENDEE_ID
    assert handover["slack_channel"] == "#sales-hot-leads"
    # mock 基準時間刻意設在活動結束 3.5 天後（為了讓多段序列同時到期），
    # 因此 30 分鐘的交棒 SLA 必然逾時 —— 這正是要驗證的告警路徑
    assert handover["is_within_sla"] is False
    assert result["amber_count"] == 1
    assert any("交棒逾時" in warning for warning in result["warnings"])


def test_edge_case_empty_attendee_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """邊界：活動零報名 / 名單尚未同步時要安靜跑完，不可拋例外。"""
    _freeze_timezone(monkeypatch)

    empty_path = tmp_path / "attendees_empty.json"
    empty_path.write_text("[]", encoding="utf-8")

    config = _load_base_config()
    config["mock"]["attendees"] = str(empty_path)
    config_path = _write_config(tmp_path, config)

    result = _run(config_path)

    assert result["total_attendees"] == 0
    assert result["sent"] == []
    assert result["drafted"] == []
    assert result["halted"] == []
    assert result["crm_handovers"] == []
    assert result["segment_counts"] == {"hot": 0, "warm": 0, "cold": 0, "unclassified": 0}
    # 空名單不是異常，不該產生任何 AMBER 警示
    # （時區已凍結，此處若非 0 必定來自業務邏輯，不會是缺 tzdata 造成的假失敗）
    assert result["amber_count"] == 0
    assert result["warnings"] == []
    assert result["stop_on_reply"] is True
    # 摘要仍要能組出來（不可因為空名單而崩在字串格式化）
    assert result["module_name"] in demo19._summarise(result)


def test_integration_reply_and_unsubscribe_block_all_sends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整合：兩個安全開關強制生效 + 已回覆/已退訂者跨兩次執行零外送 + 白名單降級。"""
    _freeze_timezone(monkeypatch)

    config = _load_base_config()
    # 刻意把兩個最重要的安全開關關掉，程式必須拒絕接受
    config["safety"]["stop_on_reply"] = False
    # 開全自動，但白名單只放 A-2001 的網域
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["@hengyu-tech.com"]
    config["runtime"]["days_in_draft"] = 30
    config_path = _write_config(tmp_path, config)
    state_file = tmp_path / "state" / "event_state.json"

    first = _run(config_path, state_file)

    # 1. stop_on_reply 被強制覆寫，且透過 Diagnostics 發出 AMBER
    assert first["stop_on_reply"] is True
    assert first["respect_unsubscribe"] is True
    assert first["amber_count"] >= 2  # stop_on_reply 覆寫 + 交棒逾時
    assert any("stop_on_reply" in warning for warning in first["warnings"])

    # 2. 最重要的一條：已回覆與已退訂者不得出現在任何外送或草稿中
    delivered = _ids(first["sent"]) | _ids(first["drafted"])
    assert REPLIED_ATTENDEE_ID not in delivered
    assert UNSUBSCRIBED_ATTENDEE_ID not in delivered
    halted_reasons = {item["attendee_id"]: item["reason"] for item in first["halted"]}
    assert halted_reasons[REPLIED_ATTENDEE_ID] == HALT_REPLIED
    assert halted_reasons[UNSUBSCRIBED_ATTENDEE_ID] == HALT_UNSUBSCRIBED
    # 已回覆者是 hot 群、分數最高，卻連業務交棒都不該發生
    assert REPLIED_ATTENDEE_ID not in _ids(first["crm_handovers"])

    # 3. 自主權：命中白名單才自動送出，其餘一律降級為草稿
    assert _ids(first["sent"]) == {WHITELISTED_ATTENDEE_ID}
    assert _ids(first["drafted"]) == {"A-2002", "A-2003", "A-2004", "A-2005"}
    assert first["sent"][0]["autonomy"] == "supervised_auto"
    assert all(item["autonomy"] == "draft" for item in first["drafted"])

    # 3b. 退訂連結必須逐人個人化——所有人共用一組連結等於沒有退訂機制，
    #     系統無從得知「要退訂誰」，而且外觀合規會讓問題更晚才被發現
    messages = first["sent"] + first["drafted"]
    matches = [re.search(r"token=([0-9a-f]{16})\b", item["body"]) for item in messages]
    assert all(match is not None for match in matches), "退訂連結缺少 16 碼 token"
    tokens = [match.group(1) for match in matches if match is not None]
    assert len(set(tokens)) == len(messages), "不同與會者的退訂 token 不得重複"
    # 沒有任何未替換的佔位符殘留（{{ATTENDEE_TOKEN}} 必須已被取代）
    assert all("{{" not in item["body"] for item in messages)
    # token 不得洩漏 email 本身（退訂連結會進 log 與 referer）
    assert all(
        "@" not in item["body"].rsplit("退訂：", 1)[-1] for item in messages
    )

    # 4. --state-file 生效：進度確實落地
    assert first["state_file"] == str(state_file)
    assert state_file.exists()
    persisted = json.loads(state_file.read_text(encoding="utf-8"))
    assert persisted["sent"][WHITELISTED_ATTENDEE_ID] == [30]
    assert persisted["crm_handover"] == [WHITELISTED_ATTENDEE_ID]

    # 5. 第二次執行（讀同一份狀態檔）：序列往前推進，但已回覆/已退訂者依然零外送
    second = _run(config_path, state_file)
    assert _ids(second["sent"]) == {WHITELISTED_ATTENDEE_ID}
    assert second["sent"][0]["step_type"] == "hot_meeting_offer"  # 已從 30 分鐘推進到 Day 1
    delivered_again = _ids(second["sent"]) | _ids(second["drafted"])
    assert REPLIED_ATTENDEE_ID not in delivered_again
    assert UNSUBSCRIBED_ATTENDEE_ID not in delivered_again
    # 已交棒過的人不會被重複推進業務 Slack
    assert second["crm_handovers"] == []

    # 6. 結果必須可 JSON 序列化（供 CRM 回寫 / 稽核留存）
    assert json.loads(json.dumps(second, ensure_ascii=False))["total_attendees"] == 9
