"""模組 #10 的三個測試（happy / edge / integration）。

integration 測試是本模組最重要的一個：它驗證「客戶已回覆就絕對不再發信」
這條安全機制真的生效——即使 config 刻意把 stop_on_reply 關掉。
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

import main as demo10  # noqa: E402
from sequencer import HALT_REPLIED, HALT_STAGE_CLOSED  # noqa: E402

BASE_CONFIG_PATH = MODULE_DIR / "config.yaml"
REPLIED_PROSPECT_ID = "P-1004"
WHITELISTED_PROSPECT_ID = "P-1003"


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


def _run(config_path: Path | None = None, notify: str = "console") -> dict:
    """以 --mock 跑一次主流程。"""
    argv = ["--mock", "--notify", notify]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return demo10.run(demo10.build_parser().parse_args(argv))


def _ids(entries: list) -> set[str]:
    """取出一組結果項目的 prospect_id。"""
    return {str(item["prospect_id"]) for item in entries}


def _freeze_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """把時區解析換成固定 UTC+8，讓測試不受本機有無 tzdata 影響。

    `resolve_timezone()` 在缺 IANA 時區資料庫的機器（Windows 預設即是）上會
    找不到 Asia/Taipei，降級並發出一則 AMBER——這與被測的業務邏輯完全無關，
    卻會污染 `amber_count`。時區不是這個測試要驗的東西，直接把變因移除。
    """
    monkeypatch.setattr(
        demo10,
        "resolve_timezone",
        lambda name, fallback_offset_hours=8: (dt_timezone(timedelta(hours=8)), None),
    )


def test_happy_path() -> None:
    """標準 mock 輸入：6 位潛在客戶 -> 3 封待審草稿 + 3 筆中止。"""
    result = _run()

    assert result["module_id"] == "10"
    assert result["mode"] == "mock"
    assert result["total_prospects"] == 6
    assert result["stop_on_reply"] is True

    # 預設自主權為 DRAFT，因此不該有任何自動送出
    assert result["sent"] == []
    assert len(result["drafted"]) == 3
    assert len(result["halted"]) == 3

    # 三段序列各觸發一次，且順序與型別正確
    by_id = {item["prospect_id"]: item for item in result["drafted"]}
    assert by_id["P-1001"]["step_day"] == 3
    assert by_id["P-1001"]["step_type"] == "gentle_check"
    assert by_id["P-1002"]["step_day"] == 7
    assert by_id["P-1002"]["step_type"] == "case_study"
    assert by_id["P-1003"]["step_day"] == 14
    assert by_id["P-1003"]["step_type"] == "final_check"

    # 只有 Day 7 那封會挑案例研究，且必須挑到工具機那篇
    assert by_id["P-1002"]["case_study_id"] == "CS-01"
    assert by_id["P-1001"]["case_study_id"] is None
    assert by_id["P-1003"]["case_study_id"] is None

    # 每封草稿都要有實際內容（mock 模式由 LLMClient 回傳佔位字串）
    assert all(item["body"] for item in result["drafted"])

    # 中止原因涵蓋「已回覆」與「階段結案」兩條路徑
    halt_reasons = {item["prospect_id"]: item["reason"] for item in result["halted"]}
    assert halt_reasons[REPLIED_PROSPECT_ID] == HALT_REPLIED
    assert halt_reasons["P-1006"] == HALT_STAGE_CLOSED


def test_edge_case_empty_prospect_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """邊界：CRM 沒有任何待跟進客戶時要安靜跑完，不可拋例外。"""
    # 時區解析與「清單是否為空」無關，先凍結成固定偏移，避免環境雜訊
    _freeze_timezone(monkeypatch)

    empty_path = tmp_path / "prospects_empty.json"
    empty_path.write_text("[]", encoding="utf-8")

    config = _load_base_config()
    config["mock"]["prospects"] = str(empty_path)
    config_path = _write_config(tmp_path, config)

    result = _run(config_path)

    assert result["total_prospects"] == 0
    assert result["sent"] == []
    assert result["drafted"] == []
    assert result["halted"] == []
    # 空清單不是異常，不該產生任何 AMBER 警示
    # （時區已凍結，此處若非 0 必定來自業務邏輯，不會是缺 tzdata 造成的假失敗）
    assert result["amber_count"] == 0
    assert result["warnings"] == []
    assert result["stop_on_reply"] is True
    # 摘要仍要能組出來（不可因為空清單而崩在字串格式化）
    assert result["module_name"] in demo10._summarise(result)


def test_integration_reply_halts_sequence_and_autonomy_downgrades(
    tmp_path: Path,
) -> None:
    """整合：stop_on_reply 強制生效 + 已回覆客戶零外送 + 自主權白名單降級。"""
    config = _load_base_config()
    # 刻意把最重要的安全開關關掉，程式必須拒絕接受
    config["safety"]["stop_on_reply"] = False
    # 開全自動，但白名單只放 P-1003 的網域
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["@ruizhi-data.com"]
    config["runtime"]["days_in_draft"] = 30
    config_path = _write_config(tmp_path, config)

    result = _run(config_path)

    # 1. stop_on_reply 被強制覆寫，且透過 Diagnostics 發出 AMBER
    assert result["stop_on_reply"] is True
    assert result["amber_count"] >= 1
    assert any("stop_on_reply" in warning for warning in result["warnings"])

    # 2. 最重要的一條：已回覆的客戶不得出現在任何外送或草稿中
    delivered = _ids(result["sent"]) | _ids(result["drafted"])
    assert REPLIED_PROSPECT_ID not in delivered
    halted_by_reply = {
        item["prospect_id"]
        for item in result["halted"]
        if item["reason"] == HALT_REPLIED
    }
    assert REPLIED_PROSPECT_ID in halted_by_reply

    # 3. 自主權：命中白名單才自動送出，其餘一律降級為草稿
    assert _ids(result["sent"]) == {WHITELISTED_PROSPECT_ID}
    assert _ids(result["drafted"]) == {"P-1001", "P-1002"}
    sent_entry = result["sent"][0]
    assert sent_entry["autonomy"] == "supervised_auto"
    assert all(item["autonomy"] == "draft" for item in result["drafted"])

    # 4. 結果必須可 JSON 序列化（供 CRM 回寫 / 稽核留存）
    assert json.loads(json.dumps(result, ensure_ascii=False))["total_prospects"] == 6
