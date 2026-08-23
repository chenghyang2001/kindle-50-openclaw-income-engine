"""demo07 預約排程器測試（3 個案例：happy / edge / integration）。

全部離線可跑：不呼叫任何外部 API、不需要憑證。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main  # noqa: E402
from calendar_client import (  # noqa: E402
    CalendarClient,
    CalendarConflictError,
    Slot,
    resolve_timezone,
)


def _args(tmp_path: Path, *extra: str):
    """用正式的 parser 產生 Namespace，確保測試走的是真實 CLI 介面。

    狀態檔一律以 --state-file 指到 tmp_path，跑完測試不會在模組目錄留下 state/。
    """
    state_file = str(tmp_path / "conversations-state.json")
    return main.build_parser().parse_args(["--mock", "--state-file", state_file, *extra])


def _base_config() -> dict:
    """讀取模組正式設定，供測試改寫成臨時設定用。"""
    return yaml.safe_load((MODULE_DIR / "config.yaml").read_text(encoding="utf-8"))


def test_happy_path(tmp_path: Path) -> None:
    """三段標準對話重播後：全部成交、狀態機收在 confirmed、時區為台北時間。"""
    result = main.run(_args(tmp_path))

    # --state-file 必須真的生效（狀態寫到暫存目錄，而非模組目錄）
    assert result["state_file"] == str(tmp_path / "conversations-state.json")
    assert Path(result["state_file"]).exists()

    assert result["is_mock"] is True
    assert len(result["conversations"]) == 3
    assert result["bookings_created"] == 3
    assert result["reschedules"] == 1

    for record in result["conversations"]:
        assert record["final_state"] == "confirmed", record["id"]
        assert record["booking"] is not None
        # 每段對話至少要有「提供時段」與「確認」兩則回覆
        assert len(record["replies"]) >= 2
        assert "回覆數字就好" in record["replies"][0]
        assert "要改期的話" in record["replies"][-1]

    # 確認訊息帶的是 +08:00 的在地時間，不是 UTC
    first_booking = result["conversations"][0]["booking"]
    assert datetime.fromisoformat(first_booking["start"]).utcoffset().total_seconds() == 8 * 3600
    # 每筆成交都會讓日曆版本前進（樂觀鎖的基礎）
    assert result["calendar_version"] > 6


def test_edge_case_empty_input_and_closed_weekend(tmp_path: Path) -> None:
    """邊界：沒有任何對話時不可炸；週末公休時段永遠不得出現在建議清單。"""
    config = _base_config()
    empty_file = tmp_path / "conversations.json"
    empty_file.write_text(json.dumps({"conversations": []}), encoding="utf-8")
    config["mock"]["conversations_file"] = str(empty_file)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    result = main.run(_args(tmp_path, "--config", str(config_path)))
    assert result["conversations"] == []
    assert result["bookings_created"] == 0
    assert result["summary"]  # 空結果仍要產出可讀摘要，不是空字串

    tz, _ = resolve_timezone(config["scheduling"]["timezone"], 8)
    calendar = CalendarClient(
        calendar_path=MODULE_DIR / config["mock"]["calendar_file"],
        tz=tz,
        slot_duration_minutes=config["scheduling"]["slot_duration_minutes"],
        business_hours=config["scheduling"]["business_hours"],
        min_lead_time_minutes=0,
    )
    # 週五收工後查詢，下一批時段必須跳過六、日
    friday_evening = datetime(2026, 9, 11, 16, 45, tzinfo=tz)
    slots = calendar.available_slots(friday_evening, 3)
    assert len(slots) == 3
    assert all(slot.start.weekday() < 5 for slot in slots)
    assert all(slot.start >= friday_evening for slot in slots)


def test_integration_optimistic_lock_and_autonomy(tmp_path: Path) -> None:
    """整合：樂觀鎖擋下重複預約、代理程式自動改提新時段，且自主權停在 draft。"""
    config = _base_config()
    tz, _ = resolve_timezone(config["scheduling"]["timezone"], 8)
    calendar = CalendarClient(
        calendar_path=MODULE_DIR / config["mock"]["calendar_file"],
        tz=tz,
        slot_duration_minutes=config["scheduling"]["slot_duration_minutes"],
        business_hours=config["scheduling"]["business_hours"],
    )
    stale_version = calendar.version
    slot = calendar.available_slots(datetime(2026, 9, 7, 8, 30, tzinfo=tz), 1)[0]

    # 另一位客戶先寫入 -> 版本前進
    calendar.create_booking(slot, "搶先者", "臨時預約", expected_version=stale_version)
    later = Slot(start=slot.end, end=slot.end + (slot.end - slot.start))
    with pytest.raises(CalendarConflictError):
        calendar.create_booking(later, "遲到者", "洽談", expected_version=stale_version)

    # 端對端：衝突情境的對話最終仍要成交，且不需要人工介入
    result = main.run(_args(tmp_path, "--notify", "console"))
    conflicted = next(r for r in result["conversations"] if r["scenario"] == "slot_conflict")
    assert conflicted["conflicts"] == 1
    assert conflicted["final_state"] == "confirmed"
    assert conflicted["offer_rounds"] >= 2  # 衝突後有重新提供一輪時段
    assert "剛被預訂走了" in conflicted["replies"][1]
    assert result["conflicts_resolved"] == 1

    # 自主權：預設 draft，故不得自動送出；amber 有記錄；console 通道視為送達
    assert conflicted["autonomy_level"] == "draft"
    assert conflicted["is_auto_sent"] is False
    assert result["amber_count"] >= 1
    assert result["is_notified"] is True
