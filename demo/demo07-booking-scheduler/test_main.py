"""demo07 預約排程器測試（3 個原始案例：happy / edge / integration，
外加端對端驗證發現的 3 個 bug 修復回歸測試）。

全部離線可跑：不呼叫任何外部 API、不需要憑證。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

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
from state_machine import BookingState  # noqa: E402


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


# --------------------------------------------------------------------------- #
# 回歸測試：端對端真實驗證發現的三個 bug（見 main.py 修復說明）
# --------------------------------------------------------------------------- #
class _StubLLM:
    """假 LLM：回傳固定字串，供 `_polish()` 驗證邏輯測試使用（不呼叫任何外部程序、
    不牽涉真正的 `_shared.llm_client.LLMClient`）。
    """

    def __init__(self, response: str) -> None:
        self._response = response

    def complete(self, system: str, user: str, max_tokens: int = 400) -> str:
        return self._response


def _stub_polish_context(llm_response: str) -> main.SchedulerContext:
    """組出只夠 `_polish()` 用的最小 SchedulerContext（is_mock=False 才會真的呼叫 LLM）。

    calendar / gate 等 `_polish()` 用不到的欄位填 None：SchedulerContext 是
    dataclass，不會在建構時做型別檢查，只要呼叫路徑不觸碰這些欄位就安全。
    """
    return main.SchedulerContext(
        config={},
        calendar=None,  # type: ignore[arg-type]
        gate=None,  # type: ignore[arg-type]
        diagnostics=main.Diagnostics("test-polish", exit_on_red=False),
        llm=_StubLLM(llm_response),  # type: ignore[arg-type]
        prompt="",
        now=datetime(2026, 9, 7, 8, 30),
        slots_to_offer=3,
        timezone_label="Asia/Taipei",
        is_mock=False,
    )


def test_polish_keeps_llm_output_when_key_facts_preserved() -> None:
    """Bug 1 happy path：LLM 潤飾後仍保留所有時段標籤，直接採用潤飾後的文字。"""
    template = (
        "感謝你的詢問！以下是最近可預約的時段（Asia/Taipei 時間）：\n"
        "1. 09/25（四）10:00-10:45\n"
        "哪一個方便？回覆數字就好。"
    )
    polished_by_llm = (
        "您好，感謝您的詢問！最近可預約的時段如下：\n"
        "1. 09/25（四）10:00-10:45\n"
        "方便的話麻煩回覆數字，謝謝！"
    )
    ctx = _stub_polish_context(polished_by_llm)

    result = main._polish(ctx, template)

    assert result == polished_by_llm
    assert ctx.diagnostics.amber_count == 0


def test_polish_rejects_llm_output_missing_slot_labels() -> None:
    """Bug 1 核心場景：LLM 把客戶訊息誤判成內部狀態報告（遺漏時段標籤）時，
    必須捨棄 LLM 輸出、改用原始模板，並留下 amber 警示。
    """
    template = (
        "感謝你的詢問！以下是最近可預約的時段（Asia/Taipei 時間）：\n"
        "1. 09/25（四）10:00-10:45\n"
        "2. 09/26（五）11:00-11:45\n"
        "哪一個方便？回覆數字就好。"
    )
    hallucinated = "（已送出時段選項，等待客戶回覆數字 1、2 或 3，暫不需要進一步動作。）"
    ctx = _stub_polish_context(hallucinated)

    result = main._polish(ctx, template)

    assert result == template
    assert ctx.diagnostics.amber_count == 1


def test_polish_skips_validation_when_text_has_no_key_facts() -> None:
    """`_handle_reschedule` 開場句沒有時段標籤／預約編號，驗證邏輯要放行 LLM 輸出
    （這是刻意設計，不是遺漏：泛用確認句沒有具體事實需要保護）。
    """
    template = "沒問題，我幫你改。原本的時段已經釋出了。"
    polished_by_llm = "沒問題喔，我馬上幫你改期，原本的時段已經釋出囉！"
    ctx = _stub_polish_context(polished_by_llm)

    result = main._polish(ctx, template)

    assert result == polished_by_llm
    assert ctx.diagnostics.amber_count == 0


def test_handle_reschedule_uses_fixed_string_without_polish() -> None:
    """`_handle_reschedule` 的固定安撫語不再送進 `_polish()`／LLM，第一則回覆必須
    逐字等於原始字串——即使 stub LLM 回傳完全不同的內容（模擬曾經實測到的
    「LLM 憑空捏造假時段」事故），也不能滲透進來。is_mock=False（live 模式）
    才有意義：mock 模式下 `_polish()` 本來就直接短路回傳原文，測不出差異。
    """
    fixed_reply = "沒問題，我幫你改。原本的時段已經釋出了。"
    hallucinated_by_llm = "當然沒問題！幫你改到 09/30（三）14:00-14:45，就這樣定了喔！"

    config = _base_config()
    tz, _ = resolve_timezone(config["scheduling"]["timezone"], 8)
    calendar = CalendarClient(
        calendar_path=MODULE_DIR / config["mock"]["calendar_file"],
        tz=tz,
        slot_duration_minutes=config["scheduling"]["slot_duration_minutes"],
        business_hours=config["scheduling"]["business_hours"],
        min_lead_time_minutes=0,
        persist=False,
    )
    ctx = main.SchedulerContext(
        config=config,
        calendar=calendar,
        gate=None,  # type: ignore[arg-type]
        diagnostics=main.Diagnostics("test-reschedule", exit_on_red=False),
        llm=_StubLLM(hallucinated_by_llm),  # type: ignore[arg-type]
        prompt="",
        now=datetime(2026, 9, 7, 8, 30, tzinfo=tz),
        slots_to_offer=3,
        timezone_label=config["scheduling"]["timezone"],
        is_mock=False,  # live 模式：_polish() 才會真的呼叫 LLM，才能驗證這句沒被送進去
    )
    machine = main.ConversationStateMachine(
        conversation_id="test-reschedule-conv",
        customer="測試客戶",
        handle="@test_handle",
        state=BookingState.CONFIRMED,
        context={"booking": None},  # 沒有既有預約，_handle_reschedule 不會呼叫 cancel_booking
    )
    # offer_rounds 必須初始化：_handle_reschedule 內部會呼叫 _offer_slots()，
    # 而 _offer_slots() 執行 record["offer_rounds"] += 1，缺這個欄位會直接 KeyError
    record: dict[str, Any] = {"reschedules": 0, "booking": None, "offer_rounds": 0}

    replies = main._handle_reschedule(ctx, machine, record)

    assert replies[0] == fixed_reply
    assert hallucinated_by_llm not in replies[0]
    assert record["reschedules"] == 1


def test_resolve_calendar_path_live_mode_seeds_independent_state(tmp_path: Path) -> None:
    """Bug 3：live 模式首次呼叫要從 mock fixture 複製一份到獨立狀態路徑，
    且絕對不能動到版控追蹤的 mock/calendar.json 本尊。
    """
    config = _base_config()
    live_calendar_path = tmp_path / "state" / "calendar.json"
    config["state"]["calendar_file"] = str(live_calendar_path)  # 絕對路徑，繞開 MODULE_DIR 接合
    mock_calendar_path = MODULE_DIR / config["mock"]["calendar_file"]
    original_mock_calendar = mock_calendar_path.read_text(encoding="utf-8")

    resolved = main._resolve_calendar_path(config, is_mock=False)

    assert resolved == live_calendar_path
    assert live_calendar_path.exists()
    assert live_calendar_path.read_text(encoding="utf-8") == original_mock_calendar
    # 版控追蹤的示範資料檔完全沒被寫入
    assert mock_calendar_path.read_text(encoding="utf-8") == original_mock_calendar

    # mock 模式必須直接指回示範資料檔，不建立任何獨立狀態
    assert main._resolve_calendar_path(config, is_mock=True) == mock_calendar_path


def test_console_notify_does_not_duplicate_summary_in_final_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug 2：--notify console 時，摘要只能在 `_notify()` 印一次，最終報告不可重複放。"""
    result = main.run(_args(tmp_path, "--notify", "console"))
    capsys.readouterr()  # run() 內部 _notify() 已經印過一次，清掉不影響本測試的斷言

    console_already_printed = result["notify_channel"] == "console" and result["is_notified"]
    assert console_already_printed is True
    report = main._render_report(result, include_summary=not console_already_printed)
    assert result["summary"] not in report

    # 對照組：--dry-run 時 _notify() 完全沒送出，摘要必須留在最終報告內，
    # 確認拿掉重複的同時沒有連帶影響到「非 console 已送」情境的行為
    result_dry = main.run(_args(tmp_path, "--dry-run"))
    assert result_dry["is_notified"] is False
    report_dry = main._render_report(result_dry, include_summary=True)
    assert result_dry["summary"] in report_dry
