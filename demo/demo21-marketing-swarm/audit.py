"""稽核軌跡（Audit Trail）— Level 3 企業級硬性要求。

每一筆稽核紀錄回答五個問題，缺一不可：
何時（timestamp）、做了什麼（action）、對誰（target）、
依據什麼決定（rationale）、有沒有經過人工核准（is_human_approved / approved_by）。

為什麼各模組自行實作而不放進 `_shared/`：
`_shared/` 是 10 個 Level 1/2 模組共用的凍結契約，Level 3 的稽核需求
（人工核准欄位、決策依據欄位）是後來才長出來的。動 `_shared/` 會波及
已通過 QA 的模組，收益不抵風險。

格式選 JSONL 而非 JSON 陣列：稽核日誌是「只追加」的，JSONL 可以在
程式中途被 kill 的情況下仍保留前面所有完整的行，JSON 陣列則整份壞掉。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any


class AuditError(RuntimeError):
    """稽核日誌無法寫入。Level 3 要求稽核不可靜默失敗。"""


@dataclass(frozen=True)
class AuditEntry:
    """單筆稽核紀錄（不可變，寫出去就不能改）。"""

    timestamp: str
    module_id: str
    module_name: str
    action: str
    target: str
    rationale: str
    is_human_approved: bool
    approved_by: str | None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """轉成可序列化 dict（JSONL 一行一筆）。"""
        return {
            "timestamp": self.timestamp,
            "module_id": self.module_id,
            "module_name": self.module_name,
            "action": self.action,
            "target": self.target,
            "rationale": self.rationale,
            "is_human_approved": self.is_human_approved,
            "approved_by": self.approved_by,
            "details": self.details,
        }


class AuditLog:
    """只追加的稽核日誌。

    enabled=False 時仍會把紀錄留在記憶體（供測試與摘要用）但不落地，
    這樣呼叫端不需要到處寫 `if audit:` 判斷。
    """

    def __init__(
        self,
        path: str | Path,
        module_id: str,
        module_name: str,
        tz: tzinfo | None = None,
        enabled: bool = True,
    ) -> None:
        self._path = Path(path).expanduser()
        self._module_id = module_id
        self._module_name = module_name
        self._tz = tz
        self._enabled = bool(enabled)
        self._entries: list[AuditEntry] = []

    @property
    def path(self) -> Path:
        """稽核檔案的絕對路徑（供結果 dict 回報給使用者）。"""
        return self._path.resolve()

    @property
    def entries(self) -> list[AuditEntry]:
        """本次執行累積的所有紀錄。"""
        return list(self._entries)

    @property
    def is_enabled(self) -> bool:
        """是否實際寫檔。"""
        return self._enabled

    def record(
        self,
        action: str,
        target: str,
        rationale: str,
        is_human_approved: bool = False,
        approved_by: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """寫入一筆稽核紀錄並立刻落地。"""
        if not action or not rationale:
            raise AuditError("稽核紀錄必須同時具備 action 與 rationale（決策依據）")
        entry = AuditEntry(
            timestamp=datetime.now(tz=self._tz).isoformat(timespec="seconds"),
            module_id=self._module_id,
            module_name=self._module_name,
            action=action,
            target=target,
            rationale=rationale,
            is_human_approved=bool(is_human_approved),
            approved_by=approved_by,
            details=dict(details or {}),
        )
        self._entries.append(entry)
        if self._enabled:
            self._append(entry)
        return entry

    def _append(self, entry: AuditEntry) -> None:
        """把一筆紀錄追加到 JSONL 檔。父目錄不存在就建立。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry.to_dict(), ensure_ascii=False)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except OSError as exc:
            raise AuditError(f"稽核日誌寫入失敗：{self._path}｜{exc}") from exc

    def summary(self) -> dict[str, Any]:
        """回報稽核概況（供 run() 結果與通知摘要使用）。"""
        approved = [entry for entry in self._entries if entry.is_human_approved]
        return {
            "file": str(self.path),
            "enabled": self._enabled,
            "entry_count": len(self._entries),
            "human_approved_count": len(approved),
            "actions": [entry.action for entry in self._entries],
        }


def read_entries(path: str | Path) -> list[dict[str, Any]]:
    """讀回 JSONL 稽核檔（供測試與事後稽核查驗）。

    刻意不吞掉壞行：稽核檔出現無法解析的行代表寫入過程被破壞，
    這件事必須讓查驗的人知道，而不是安靜跳過。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise AuditError(f"找不到稽核檔：{target.resolve()}")
    entries: list[dict[str, Any]] = []
    try:
        raw_lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditError(f"稽核檔讀取失敗：{target.resolve()}｜{exc}") from exc
    for line_no, raw in enumerate(raw_lines, start=1):
        if not raw.strip():
            continue
        try:
            entries.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise AuditError(f"稽核檔第 {line_no} 行不是合法 JSON：{exc}") from exc
    return entries
