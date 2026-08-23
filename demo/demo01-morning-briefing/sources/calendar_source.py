"""行事曆來源。

書中鐵律：**行事曆是權重最高的輸入**。今日行程決定簡報的排序，
因此這個來源失敗時一律走 RED（中止），不像新聞可以降級成 AMBER。

- ``is_mock=True``：讀 ``mock/calendar.json``，零憑證。
- ``is_mock=False``：呼叫 Google Calendar API；缺 token 立刻 RED 並附上修復步驟。
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import SourceError, read_mock_payload

EVENTS_ENDPOINT = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
ALL_DAY_LABEL = "全天"


def fetch_events(
    source_config: dict,
    base_dir: Path,
    is_mock: bool,
    diagnostics: Any,
) -> list[dict]:
    """取得今日行程，回傳已正規化的事件清單（依開始時間排序）。"""
    if not source_config.get("enabled", True):
        return []

    if is_mock:
        raw_events = read_mock_payload(
            base_dir, source_config.get("mock_file", "mock/calendar.json"), "events"
        )
        events = [normalize_event(item) for item in raw_events]
    else:
        events = _fetch_live_events(source_config, diagnostics)

    return sorted(events, key=lambda event: event["start"])


def normalize_event(raw: dict) -> dict:
    """把離線 JSON 的事件轉成主流程使用的統一結構。"""
    return {
        "id": str(raw.get("id", "")),
        "start": str(raw.get("start", ALL_DAY_LABEL)),
        "end": str(raw.get("end", "")),
        "title": str(raw.get("title", "（無標題）")),
        "attendees": [str(person) for person in raw.get("attendees", []) or []],
        "location": str(raw.get("location", "")),
        "importance": str(raw.get("importance", "medium")),
        "prep_minutes": int(raw.get("prep_minutes", 0) or 0),
        "prep_note": str(raw.get("prep_note", "")),
    }


def _fetch_live_events(source_config: dict, diagnostics: Any) -> list[dict]:
    """live 模式：讀 OAuth token 後呼叫 Google Calendar events.list。"""
    token_env = str(source_config.get("token_env", "GOOGLE_CALENDAR_TOKEN"))
    token_location = os.environ.get(token_env)
    if not token_location:
        diagnostics.red(
            symptom="google_calendar_token_missing",
            cause=f"環境變數 {token_env} 未設定，無法存取 Google Calendar",
            fix=f"先完成一次 Google OAuth 取得 token JSON，再設定 {token_env} 指向該檔案路徑",
        )
        raise SourceError(f"缺少環境變數 {token_env}，行事曆為最高權重來源，流程中止")

    access_token = _read_access_token(Path(token_location), token_env, diagnostics)
    payload = _request_events(source_config, access_token, diagnostics)
    return [_normalize_google_event(item) for item in payload.get("items", [])]


def _read_access_token(token_path: Path, token_env: str, diagnostics: Any) -> str:
    """從 token JSON 取出 access_token；格式不符即 RED。"""
    try:
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.red(
            symptom="oauth_error",
            cause=f"{token_env} 指向的 token 檔無法讀取或不是合法 JSON：{token_path}",
            fix="重新執行 Google OAuth 流程產生 token，並確認設定永久 refresh token",
        )
        raise SourceError(f"token 檔無法解析：{token_path}") from exc

    access_token = token_data.get("access_token") or token_data.get("token")
    if not access_token:
        diagnostics.red(
            symptom="oauth_error",
            cause=f"token 檔缺少 access_token 欄位：{token_path}",
            fix="確認 OAuth 流程有帶 offline access，並重新產生 token",
        )
        raise SourceError(f"token 檔缺少 access_token：{token_path}")
    return str(access_token)


def _request_events(source_config: dict, access_token: str, diagnostics: Any) -> dict:
    """呼叫 Google Calendar events.list，回傳原始 JSON。"""
    lookahead_hours = int(source_config.get("lookahead_hours", 18) or 18)
    now = datetime.now().astimezone()
    query = urllib.parse.urlencode(
        {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(hours=lookahead_hours)).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "20",
        }
    )
    calendar_id = urllib.parse.quote(str(source_config.get("calendar_id", "primary")))
    url = f"{EVENTS_ENDPOINT.format(calendar_id=calendar_id)}?{query}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        diagnostics.red(
            symptom="oauth_error",
            cause=f"Google Calendar API 回應 HTTP {exc.code}",
            fix="401/403 代表 token 過期或範圍不足，重新授權；其餘狀態碼請查 API 配額",
        )
        raise SourceError(f"Google Calendar API 失敗：HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        diagnostics.red(
            symptom="calendar_unreachable",
            cause=f"無法連線或解析 Google Calendar 回應：{exc}",
            fix="確認網路連線與 DNS，再重跑一次；行事曆為最高權重來源，不可略過",
        )
        raise SourceError(f"Google Calendar 連線失敗：{exc}") from exc


def _normalize_google_event(item: dict) -> dict:
    """把 Google Calendar API 的事件轉成統一結構。"""
    attendees = [
        str(person.get("email", ""))
        for person in item.get("attendees", []) or []
        if person.get("email")
    ]
    return {
        "id": str(item.get("id", "")),
        "start": _to_hhmm(item.get("start", {})),
        "end": _to_hhmm(item.get("end", {})),
        "title": str(item.get("summary", "（無標題）")),
        "attendees": attendees,
        "location": str(item.get("location", "")),
        # Google 沒有「重要性」欄位，用與會人數當代理指標：有外部與會者就是重要會議。
        "importance": "high" if attendees else "low",
        "prep_minutes": 0,
        "prep_note": str(item.get("description", ""))[:60],
    }


def _to_hhmm(slot: dict) -> str:
    """把 Google 的 start/end 物件轉成 HH:MM；全天事件回傳「全天」。"""
    raw_value = slot.get("dateTime")
    if not raw_value:
        return ALL_DAY_LABEL
    try:
        return datetime.fromisoformat(str(raw_value)).strftime("%H:%M")
    except ValueError:
        return ALL_DAY_LABEL
