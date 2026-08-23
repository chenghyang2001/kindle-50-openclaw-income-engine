"""晨間簡報的三個資料來源（行事曆 / 信件 / 新聞）。

設計原則：
1. 每個來源都提供同一組簽名 ``fetch_xxx(source_config, base_dir, is_mock, diagnostics)``，
   讓 ``main.py`` 可以用同一段迴圈邏輯處理，新增來源不必改主流程。
2. ``is_mock=True`` 時一律只讀本地 JSON，零憑證、零網路。
3. ``is_mock=False`` 缺憑證時走 ``Diagnostics.red`` 明確中止，
   **絕不偷偷退回 mock**（書中第 04 章：靜默降級會讓客戶以為系統還活著）。
"""

import json
from pathlib import Path


class SourceError(RuntimeError):
    """資料來源取得失敗（檔案缺失、格式錯誤、外部服務失敗）"""


def resolve_path(base_dir: Path, relative_path: str) -> Path:
    """把設定檔中的相對路徑接到模組根目錄。

    設定檔一律寫相對路徑，這台機器與另一台機器的家目錄不同，
    寫死絕對路徑同步過去就會壞掉。
    """
    candidate = Path(relative_path)
    return candidate if candidate.is_absolute() else base_dir / candidate


def read_mock_payload(base_dir: Path, relative_path: str, list_key: str) -> list[dict]:
    """讀取離線 JSON 並取出指定的陣列欄位。

    容許兩種格式：頂層直接是陣列，或頂層是物件、資料放在 ``list_key`` 底下。
    """
    path = resolve_path(base_dir, relative_path)
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SourceError(f"找不到離線資料檔：{path}") from exc
    except OSError as exc:
        raise SourceError(f"無法讀取離線資料檔 {path}：{exc}") from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SourceError(f"離線資料檔不是合法 JSON：{path}（{exc}）") from exc

    items = payload.get(list_key, []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SourceError(f"離線資料檔 {path} 的「{list_key}」必須是陣列")
    return items


from .calendar_source import fetch_events  # noqa: E402
from .email_source import fetch_messages  # noqa: E402
from .news_source import fetch_headlines  # noqa: E402

__all__ = [
    "SourceError",
    "fetch_events",
    "fetch_headlines",
    "fetch_messages",
    "read_mock_payload",
    "resolve_path",
]
