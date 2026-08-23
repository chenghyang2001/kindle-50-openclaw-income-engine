"""demo24 — 稽核軌跡（JSONL + 雜湊鏈，模組 #24）。

為什麼自行實作而不動 `_shared/`：
稽核軌跡是本模組的法遵要求（就業歧視爭議發生時，必須能重現「當時憑什麼把這個人
刷掉」「是誰在什麼時候揭露了身分」），其他模組沒有這個需求。把它塞進共用層等於
讓十個模組都背上一個用不到的相依，也會讓 `_shared/` 的凍結契約被本模組單方面撐大。

格式：每行一筆 JSON（JSONL），欄位固定為
`seq / ts / module / event / actor / detail / prev_hash / entry_hash`。

**雜湊鏈**：`entry_hash = sha256(prev_hash + 本筆內容)`。
事後有人手動修掉某一行（例如刪掉一次未經核准的 reveal），`verify_chain()` 立刻
指出斷點。這不能防止整份檔案被換掉，但能讓「偷改一行」變成看得見的事件——
招募稽核的真實威脅模型本來就是內部人的小幅竄改，不是外部攻擊者。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


class AuditError(RuntimeError):
    """稽核日誌無法寫入或既有內容已損壞。"""


@dataclass(frozen=True)
class AuditEntry:
    """單筆稽核紀錄。"""

    seq: int
    ts: str
    module: str
    event: str
    actor: str
    detail: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict[str, Any]:
        """序列化成要寫進 JSONL 的那一行。"""
        return {
            "seq": self.seq,
            "ts": self.ts,
            "module": self.module,
            "event": self.event,
            "actor": self.actor,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


def compute_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """本筆雜湊 = sha256(前一筆雜湊 + 本筆內容的正規化 JSON)。"""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{prev_hash}{body}".encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    """UTC ISO-8601 時間戳。稽核一律用 UTC，避免夏令時間造成事件順序錯亂。"""
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """單一模組的稽核日誌。建立時會續接既有檔案的雜湊鏈。"""

    def __init__(self, path: str | Path, module_name: str, actor: str = "system") -> None:
        """
        path:        JSONL 檔路徑（父目錄不存在會自動建立）
        module_name: 寫入每一筆的 module 欄位
        actor:       預設操作者；`record()` 可逐筆覆寫
        """
        if not str(module_name).strip():
            raise AuditError("module_name 必須是非空字串")
        self._path = Path(path).expanduser()
        self._module_name = str(module_name).strip()
        self._actor = str(actor).strip() or "system"
        self._entries: list[AuditEntry] = []
        self._seq, self._prev_hash = self._read_tail()

    @property
    def path(self) -> Path:
        """稽核日誌的絕對路徑。"""
        return self._path.resolve()

    @property
    def entries(self) -> tuple[AuditEntry, ...]:
        """本次執行寫入的紀錄（不含開檔前既有的歷史）。"""
        return tuple(self._entries)

    def _read_tail(self) -> tuple[int, str]:
        """讀既有檔案的最後一筆，取得續接用的 seq 與 prev_hash。"""
        if not self._path.is_file():
            return 0, GENESIS_HASH
        last: dict[str, Any] | None = None
        for row in self.read_all():
            last = row
        if last is None:
            return 0, GENESIS_HASH
        return int(last.get("seq", 0)), str(last.get("entry_hash", GENESIS_HASH))

    def read_all(self) -> list[dict[str, Any]]:
        """讀回整份日誌。空行略過；壞行明確報錯，不靜默跳過。"""
        if not self._path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            content = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AuditError(f"讀取稽核日誌失敗：{self.path}｜{exc}") from exc
        for number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AuditError(f"稽核日誌第 {number} 行不是合法 JSON：{self.path}｜{exc}") from exc
        return rows

    def record(self, event: str, detail: dict[str, Any], actor: str | None = None) -> AuditEntry:
        """追加一筆紀錄並回傳。寫入失敗會拋 AuditError，不可靜默吞掉。

        稽核寫不進去時，正確反應是讓整條管線停下來：沒有軌跡的篩選結果
        在法遵上等於不存在，繼續跑只是在製造無法舉證的決定。
        """
        if not str(event).strip():
            raise AuditError("event 必須是非空字串")
        payload = {
            "seq": self._seq + 1,
            "ts": _utc_now_iso(),
            "module": self._module_name,
            "event": str(event).strip(),
            "actor": str(actor or self._actor).strip() or "system",
            "detail": detail or {},
        }
        entry = AuditEntry(
            **payload,
            prev_hash=self._prev_hash,
            entry_hash=compute_hash(self._prev_hash, payload),
        )
        self._append(entry)
        self._seq = entry.seq
        self._prev_hash = entry.entry_hash
        self._entries.append(entry)
        return entry

    def _append(self, entry: AuditEntry) -> None:
        """把一筆紀錄寫成 JSONL 的一行（UTF-8，行末換行）。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        except OSError as exc:
            raise AuditError(f"寫入稽核日誌失敗：{self.path}｜{exc}") from exc

    def verify_chain(self) -> list[str]:
        """驗證整份日誌的雜湊鏈，回傳所有斷點描述（空清單代表完整）。"""
        problems: list[str] = []
        prev = GENESIS_HASH
        for index, row in enumerate(self.read_all(), start=1):
            problems.extend(_verify_row(row, index, prev))
            prev = str(row.get("entry_hash", ""))
        return problems


def _verify_row(row: dict[str, Any], index: int, prev: str) -> list[str]:
    """驗證單一行：prev_hash 是否接得上、entry_hash 是否與內容相符。"""
    problems: list[str] = []
    if str(row.get("prev_hash", "")) != prev:
        problems.append(f"第 {index} 筆的 prev_hash 接不上前一筆（可能有紀錄被刪除）")
    payload = {key: row.get(key) for key in ("seq", "ts", "module", "event", "actor", "detail")}
    if compute_hash(str(row.get("prev_hash", "")), payload) != str(row.get("entry_hash", "")):
        problems.append(f"第 {index} 筆的 entry_hash 與內容不符（內容被竄改）")
    return problems
