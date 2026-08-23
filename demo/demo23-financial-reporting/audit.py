"""稽核軌跡（JSONL）— 董事會級財務報表的「誰在什麼時候做了什麼」。

為什麼財務模組要有自己的稽核日誌，而不是靠 stdout / 通知紀錄：

1. **審核是法遵動作**：apxG_p08 要求 T+1 財務總監人工核准後才可對董事會發布。
   「核准過了」這件事必須留下不可否認的紀錄，否則事後查核只剩口頭說法。
2. **stdout 會被輪替掉**：主控台輸出不是紀錄，重跑一次就沒了。
3. **JSONL 可 append、可逐行解析**：中途中斷不會毀掉整個檔案（JSON 陣列會）。

刻意**不改 `_shared/`**：稽核需求是本模組專屬（其他 demo 不需要保存財務核准鏈），
把它塞進共用層會讓 10 個模組全部背上一個用不到的依賴。

寫入失敗一律拋 `AuditError` 由呼叫端升級為紅色警報：**沒有稽核軌跡就不准發報表**，
不是「記不下來就算了」。這是本模組唯一會因為「記錄失敗」而停下來的地方。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable

#: 稽核事件代碼。集中成常數，避免各處拼字不一致導致事後 grep 漏事件。
EVENT_RUN_STARTED = "run_started"
EVENT_SCOPE_VERIFIED = "read_only_scope_verified"
EVENT_SCOPE_VIOLATION = "read_only_scope_violation"
EVENT_DRY_RUN_SELFTEST = "dry_run_selftest"
EVENT_SOURCE_READ = "source_read"
EVENT_SOURCE_FAILED = "source_read_failed"
EVENT_PACK_GENERATED = "board_pack_generated"
EVENT_APPROVAL_REQUESTED = "approval_requested"
EVENT_APPROVAL_GRANTED = "approval_granted"
EVENT_APPROVAL_REJECTED = "approval_rejected"
EVENT_APPROVAL_INVALIDATED = "approval_invalidated"
EVENT_APPROVAL_SLA_BREACHED = "approval_sla_breached"
EVENT_DISPATCH = "board_dispatch"
EVENT_DISPATCH_BLOCKED = "board_dispatch_blocked"
EVENT_RUN_FINISHED = "run_finished"


class AuditError(RuntimeError):
    """稽核軌跡無法寫入或無法讀回。"""


def content_fingerprint(payload: str) -> str:
    """對報表內容取 SHA-256 指紋（前 16 碼）。

    核准綁定的是「這一份數字」，不是「這個月份」。任何一個科目金額變動都會讓指紋
    改變，先前的核准隨即失效、必須重審——否則「先核准一份乾淨的，再換掉數字發出去」
    這條路是敞開的。
    """
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


class AuditLog:
    """單次執行的稽核寫入口。每個事件一行 JSON。"""

    def __init__(
        self,
        path: str | Path,
        module_name: str,
        tz: tzinfo | None = None,
        run_id: str | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        """
        path:         JSONL 檔案位置（`--audit-file` 可覆寫）
        module_name:  寫進每一行的模組識別
        tz:           時間戳時區（本模組一律用 zoneinfo 解析出的 tzinfo，不用 pytz）
        now_provider: 測試注入固定時間用；預設為 `datetime.now(tz)`
        """
        self._path = Path(path).expanduser()
        self._module_name = module_name
        self._tz = tz
        self._run_id = run_id or uuid.uuid4().hex[:12]
        self._now = now_provider or (lambda: datetime.now(self._tz))
        self._count = 0

    @property
    def path(self) -> Path:
        """稽核檔絕對路徑（供結果 dict 與 README 指引使用）。"""
        return self._path

    @property
    def run_id(self) -> str:
        """本次執行的識別碼，同一次跑出來的所有事件共用。"""
        return self._run_id

    @property
    def event_count(self) -> int:
        """本次執行已寫入的事件數。"""
        return self._count

    def record(
        self,
        event: str,
        detail: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """寫入一則稽核事件並回傳寫入的內容（供測試斷言與結果 dict 引用）。"""
        entry = {
            "ts": self._now().isoformat(timespec="seconds"),
            "run_id": self._run_id,
            "module": self._module_name,
            "event": event,
            "actor": actor,
            "detail": detail or {},
        }
        self._append(entry)
        self._count += 1
        return entry

    def _append(self, entry: dict[str, Any]) -> None:
        """實際落地。父目錄不存在就補建，寫入失敗轉成 AuditError。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise AuditError(f"稽核軌跡寫入失敗：{self._path}｜{exc}") from exc

    def events(self) -> list[dict[str, Any]]:
        """讀回本檔全部事件（含歷史執行）。"""
        return read_events(self._path)


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """讀取 JSONL 稽核檔。檔案不存在回空清單；單行損毀明確報錯（不靜默略過）。"""
    target = Path(path).expanduser()
    if not target.is_file():
        return []

    events: list[dict[str, Any]] = []
    try:
        raw_lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AuditError(f"稽核軌跡讀取失敗：{target}｜{exc}") from exc

    for number, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AuditError(f"稽核軌跡第 {number} 行不是合法 JSON：{target}｜{exc}") from exc
    return events


def resolve_audit_path(cli_value: str | None, config_value: str, module_dir: Path) -> Path:
    """決定稽核檔位置：`--audit-file` > 環境變數 > config.yaml。

    相對路徑一律以**模組目錄**為基準而不是 cwd：從別的目錄執行時，
    稽核檔散落在使用者當下所在的資料夾，等同於沒有稽核軌跡。
    """
    override = cli_value or os.environ.get("OPENCLAW_AUDIT_LOG", "").strip()
    chosen = override or config_value
    expanded = Path(os.path.expandvars(str(chosen))).expanduser()
    return expanded if expanded.is_absolute() else (module_dir / expanded)
