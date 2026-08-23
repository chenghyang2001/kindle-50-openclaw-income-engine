"""模組級稽核軌跡（append-only JSONL）。

**為什麼不寫進 `_shared/`**：`_shared/` 是十個 demo 共用的凍結契約，
本模組的稽核需求源自公平住房法（哪一筆推薦、依據哪些欄位、送給誰、
為什麼被去重擋下）——這是 Level 3 房地產專屬的義務，
放進 `_shared/` 會逼另外九個模組一起改，違反契約凍結原則。

**為什麼是 append-only JSONL 而不是 JSON 陣列**：
稽核軌跡的價值在於「不能被改」。JSONL 每行獨立，
中途當掉最多壞掉最後一行，前面已落地的紀錄仍可讀；
JSON 陣列則需要重寫整個檔案，一次中斷就整份失效。

**寫入失敗一律拋錯，不吞掉**：稽核軌跡寫不進去時，
繼續送出推薦等於在沒有紀錄的情況下對外通訊——正是法遵最怕的狀態。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditError(RuntimeError):
    """稽核軌跡無法寫入或讀取"""


def _new_run_id() -> str:
    """本次執行的識別碼；同一次執行寫出的所有事件共用，方便事後撈整段軌跡"""
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{os.urandom(4).hex()}"


class AuditLog:
    """單次執行的稽核軌跡寫入器。"""

    def __init__(
        self,
        path: str | Path,
        module_name: str,
        *,
        run_id: str | None = None,
        is_enabled: bool = True,
    ) -> None:
        """
        path:        JSONL 檔案路徑（父目錄會自動建立）
        module_name: 寫進每一行的模組名稱
        run_id:      本次執行識別碼，未指定時自動產生
        is_enabled:  False 時只在記憶體累積、不落地（供測試與 --dry-run 之外的特殊情境）
        """
        if not isinstance(module_name, str) or not module_name.strip():
            raise AuditError("module_name 必須是非空字串")
        self._path = Path(path)
        self._module_name = module_name.strip()
        self._run_id = run_id or _new_run_id()
        self._is_enabled = bool(is_enabled)
        self._records: list[dict[str, Any]] = []

    @property
    def path(self) -> Path:
        """稽核檔絕對路徑"""
        return self._path

    @property
    def run_id(self) -> str:
        """本次執行識別碼"""
        return self._run_id

    @property
    def is_enabled(self) -> bool:
        """是否實際落地寫檔"""
        return self._is_enabled

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        """本次執行已記錄的事件（供測試斷言與結果 dict 使用）"""
        return tuple(self._records)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        """寫一筆稽核事件並回傳該筆內容。

        `event` 是事件代碼（如 `notification_sent`）；其餘欄位以關鍵字傳入。
        保留欄位（ts / run_id / module / event）不可被 fields 覆蓋，
        否則軌跡可以被呼叫端偽造。
        """
        if not isinstance(event, str) or not event.strip():
            raise AuditError("event 必須是非空字串")
        reserved = {"ts", "run_id", "module", "event"} & set(fields)
        if reserved:
            raise AuditError(f"稽核保留欄位不可覆寫：{sorted(reserved)}")

        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": self._run_id,
            "module": self._module_name,
            "event": event.strip(),
        }
        entry.update(fields)
        self._records.append(entry)
        if self._is_enabled:
            self._append(entry)
        return entry

    def _append(self, entry: dict[str, Any]) -> None:
        """把單筆事件序列化後追加一行；任何 I/O 失敗都轉成 AuditError 往上拋"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry, ensure_ascii=False, default=str)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except (OSError, TypeError, ValueError) as exc:
            raise AuditError(f"稽核軌跡寫入失敗（{self._path}）：{exc}") from exc


def read_audit(path: str | Path) -> list[dict[str, Any]]:
    """讀回整份稽核軌跡（供測試與事後稽核）。

    壞掉的那一行會明確報出行號，不靜默跳過——
    「有一行讀不出來」本身就是稽核事件，必須被看見。
    """
    target = Path(path)
    if not target.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        raw_lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditError(f"稽核軌跡讀取失敗（{target}）：{exc}") from exc
    for line_no, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AuditError(f"稽核軌跡第 {line_no} 行不是合法 JSON（{target}）：{exc}") from exc
    return entries
