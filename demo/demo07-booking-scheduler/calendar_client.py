"""日曆用戶端：查詢可用時段 + 建立預約（含樂觀鎖）。

mock 模式讀 `mock/calendar.json`，全程不連網、不需憑證。

**防重複預約**採樂觀鎖：提供時段時把當下的 `version` 一併交給對話，
客戶選定後寫入時必須帶回同一個 `version`。期間只要有任何人寫入日曆，
version 就會遞增，落後的寫入會被 CalendarConflictError 擋下。
這比「寫入前再查一次」可靠——後者在兩人同時選中同一格時仍會雙重預約。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 星期索引 -> config.business_hours 的鍵
WEEKDAY_KEYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_ZH: tuple[str, ...] = ("一", "二", "三", "四", "五", "六", "日")


class CalendarError(RuntimeError):
    """日曆讀寫失敗"""


class CalendarConflictError(CalendarError):
    """樂觀鎖衝突：時段已被佔用，或日曆版本已過期"""


def resolve_timezone(name: str, fallback_offset_hours: int) -> tuple[tzinfo, str | None]:
    """取得時區物件。

    回傳 (tzinfo, 警告訊息或 None)。Windows 預設沒有 IANA tzdata，
    ZoneInfo("Asia/Taipei") 會直接拋錯，故降級為固定時差而非讓整條流程掛掉
    （代價：該區若有日光節約時間會失準，因此一定要發 amber 讓人看得見）。
    """
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError) as exc:
        offset = timezone(timedelta(hours=fallback_offset_hours), name)
        return offset, (
            f"找不到時區資料 {name}（{exc}），已降級為固定 UTC{fallback_offset_hours:+d}；"
            "如需正確處理日光節約時間請安裝 tzdata 套件"
        )


@dataclass(frozen=True)
class Slot:
    """單一可預約時段（兩端皆為帶時區的 datetime）"""

    start: datetime
    end: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def label(self) -> str:
        """繁中時段標籤，例：09/08（二）11:15-12:00

        刻意用 ASCII 連字號而非破折號：Windows 主控台若落回 cp950，
        U+2013 會直接讓 print 拋 UnicodeEncodeError。
        """
        weekday = WEEKDAY_ZH[self.start.weekday()]
        return (
            f"{self.start:%m/%d}（{weekday}）"
            f"{self.start:%H:%M}-{self.end:%H:%M}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label(),
        }


def _parse_hhmm(value: str) -> time:
    """把 "09:00" 轉成 time 物件"""
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
        return time(hour=hour, minute=minute)
    except ValueError as exc:
        raise CalendarError(f"營業時間格式錯誤（應為 HH:MM）：{value!r}") from exc


class CalendarClient:
    """讀取既有行程、算出可用時段、建立預約。"""

    def __init__(
        self,
        calendar_path: Path,
        tz: tzinfo,
        slot_duration_minutes: int,
        business_hours: dict[str, list[str]],
        min_lead_time_minutes: int = 0,
        horizon_days: int = 14,
        persist: bool = False,
    ) -> None:
        """persist=False（mock 預設）只在記憶體中改動，不污染 mock 資料檔。"""
        self.calendar_path = Path(calendar_path)
        self.tz = tz
        self.slot_duration = timedelta(minutes=slot_duration_minutes)
        self.business_hours = business_hours
        self.min_lead_time = timedelta(minutes=min_lead_time_minutes)
        self.horizon_days = horizon_days
        self.persist = persist
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        """讀日曆快照。檔案不存在或格式錯誤都要明確報錯，不可靜默給空日曆。"""
        if not self.calendar_path.exists():
            raise FileNotFoundError(f"找不到日曆檔：{self.calendar_path.resolve()}")
        try:
            data = json.loads(self.calendar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise CalendarError(f"日曆檔無法解析：{self.calendar_path}（{exc}）") from exc
        data.setdefault("version", 0)
        data.setdefault("bookings", [])
        return data

    @property
    def version(self) -> int:
        """目前日曆版本，樂觀鎖的比對依據"""
        return int(self._data["version"])

    @property
    def bookings(self) -> list[dict[str, Any]]:
        return list(self._data["bookings"])

    def _busy_ranges(self) -> list[tuple[datetime, datetime]]:
        """既有行程轉成 (start, end) 區間，統一換算到本地時區。"""
        ranges: list[tuple[datetime, datetime]] = []
        for booking in self._data["bookings"]:
            try:
                start = datetime.fromisoformat(booking["start"]).astimezone(self.tz)
                end = datetime.fromisoformat(booking["end"]).astimezone(self.tz)
            except (KeyError, ValueError) as exc:
                raise CalendarError(f"行程時間格式錯誤：{booking!r}（{exc}）") from exc
            ranges.append((start, end))
        return ranges

    def _day_slots(self, day: date) -> Iterator[Slot]:
        """產生某一天營業時間內、對齊時段長度的候選時段（不判斷是否被佔）。"""
        hours = self.business_hours.get(WEEKDAY_KEYS[day.weekday()]) or []
        if len(hours) < 2:
            return  # 空陣列 = 公休日（週末），直接跳過
        opens = datetime.combine(day, _parse_hhmm(hours[0]), tzinfo=self.tz)
        closes = datetime.combine(day, _parse_hhmm(hours[1]), tzinfo=self.tz)
        cursor = opens
        while cursor + self.slot_duration <= closes:
            yield Slot(start=cursor, end=cursor + self.slot_duration)
            cursor += self.slot_duration

    def is_free(self, slot: Slot) -> bool:
        """時段是否與既有行程重疊（端點相接不算重疊）"""
        return all(
            slot.end <= busy_start or slot.start >= busy_end
            for busy_start, busy_end in self._busy_ranges()
        )

    def available_slots(self, now: datetime, count: int) -> list[Slot]:
        """從 now + 最短前置時間起，往後找出最多 count 個可用時段。"""
        earliest = now.astimezone(self.tz) + self.min_lead_time
        found: list[Slot] = []
        for offset in range(self.horizon_days + 1):
            for slot in self._day_slots(earliest.date() + timedelta(days=offset)):
                if slot.start >= earliest and self.is_free(slot):
                    found.append(slot)
                    if len(found) >= count:
                        return found
        return found

    def create_booking(
        self,
        slot: Slot,
        customer: str,
        title: str,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """寫入預約。版本過期或時段已被佔用皆拋 CalendarConflictError。"""
        if expected_version is not None and expected_version != self.version:
            raise CalendarConflictError(
                f"日曆已被更新（提供時段時為 v{expected_version}，目前為 v{self.version}），"
                f"{slot.label()} 需重新確認"
            )
        if not self.is_free(slot):
            raise CalendarConflictError(f"時段 {slot.label()} 已被預訂")
        booking = {
            "id": f"BK-{slot.start:%Y%m%d-%H%M}",
            "start": slot.start.isoformat(),
            "end": slot.end.isoformat(),
            "customer": customer,
            "title": title,
        }
        self._data["bookings"].append(booking)
        self._data["version"] = self.version + 1
        if self.persist:
            self._write()
        return booking

    def cancel_booking(self, booking_id: str) -> bool:
        """取消預約（改期的第一步）。找不到回 False，版本仍遞增以維持鎖的語意。"""
        remaining = [b for b in self._data["bookings"] if b.get("id") != booking_id]
        is_removed = len(remaining) != len(self._data["bookings"])
        self._data["bookings"] = remaining
        self._data["version"] = self.version + 1
        if self.persist:
            self._write()
        return is_removed

    def _write(self) -> None:
        """把記憶體中的日曆寫回檔案（persist=True 時才呼叫）"""
        try:
            self.calendar_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            raise CalendarError(f"寫入日曆失敗：{self.calendar_path}（{exc}）") from exc
