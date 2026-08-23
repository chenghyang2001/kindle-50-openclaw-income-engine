"""模組 #30 — 稽核軌跡（append-only JSONL）。

為何自行實作而不改 `_shared/`：
稽核需求是本模組（多租戶白牌）特有的，其餘 29 個模組不需要。
把它塞進 `_shared/` 等於讓一個凍結的共用契約為單一模組轉向。

為何是 JSONL 而不是單一 JSON 陣列：
稽核日誌只會被追加、不會被重寫。JSONL 的每一行都是獨立完整的紀錄，
程式中途被砍也只會損失最後一行，不會讓整份日誌變成無法解析的殘檔。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SEVERITIES = ("green", "amber", "red")


class AuditError(RuntimeError):
    """稽核日誌寫入或參數違規。"""


def _utc_now() -> str:
    """預設時鐘：UTC ISO-8601。稽核跨時區比對時，本地時間只會製造爭議。"""
    return datetime.now(timezone.utc).isoformat()


class AuditLog:
    """單次執行的稽核軌跡。同時保留記憶體副本，供結果回報與測試斷言。"""

    def __init__(
        self,
        path: str | Path,
        module: str,
        clock: Callable[[], str] | None = None,
        run_id: str | None = None,
    ) -> None:
        """
        path:   JSONL 檔案路徑（父目錄不存在時自動建立）
        module: 模組標籤，寫進每一行
        clock:  取得時間戳的函式；mock 模式傳入固定時鐘可讓輸出完全可重現
        run_id: 本次執行識別碼，讓同一個檔案裡的多次執行可以被切開來看
        """
        if not isinstance(module, str) or not module.strip():
            raise AuditError("module 必須是非空字串")
        self._path = Path(path).expanduser()
        self._module = module.strip()
        self._clock = clock or _utc_now
        self._run_id = run_id or self._clock()
        self._entries: list[dict] = []

    @property
    def path(self) -> Path:
        """稽核日誌的絕對路徑。"""
        return self._path.resolve() if self._path.exists() else self._path.absolute()

    @property
    def entries(self) -> list[dict]:
        """本次執行寫入的所有紀錄（複本，外部改不到內部狀態）。"""
        return [dict(entry) for entry in self._entries]

    def record(
        self,
        event: str,
        severity: str,
        namespace: str | None = None,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict:
        """追加一筆稽核紀錄並回傳它。

        severity 只收 green / amber / red——自由字串會讓日後的日誌查詢
        變成猜謎（"warn"？"WARNING"？"amber"？）。
        """
        normalized = str(severity or "").strip().lower()
        if normalized not in SEVERITIES:
            raise AuditError(f"未知的 severity {severity!r}，可用：{', '.join(SEVERITIES)}")
        if not isinstance(event, str) or not event.strip():
            raise AuditError("event 必須是非空字串")
        entry = {
            "timestamp": self._clock(),
            "run_id": self._run_id,
            "module": self._module,
            "event": event.strip(),
            "severity": normalized,
            "namespace": namespace,
            "actor": actor,
            "detail": detail or {},
        }
        self._entries.append(entry)
        self._append(entry)
        return entry

    def _append(self, entry: dict) -> None:
        """實際落檔。寫不進去要立刻炸——寫不了稽核就不該繼續動租戶資料。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            raise AuditError(f"稽核日誌寫入失敗：{self._path}｜{exc}") from exc

    def counts(self) -> dict[str, int]:
        """依 severity 統計本次執行的紀錄數。"""
        result = {level: 0 for level in SEVERITIES}
        for entry in self._entries:
            result[entry["severity"]] += 1
        return result

    @staticmethod
    def load(path: str | Path) -> list[dict]:
        """讀回既有的 JSONL 稽核檔（供事後查驗與測試）。

        壞掉的行不靜默略過：稽核檔出現無法解析的行本身就是一個事件。
        """
        target = Path(path).expanduser()
        if not target.is_file():
            raise AuditError(f"找不到稽核日誌：{target.absolute()}")
        entries: list[dict] = []
        for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise AuditError(f"稽核日誌第 {number} 行無法解析：{target}｜{exc}") from exc
        return entries
