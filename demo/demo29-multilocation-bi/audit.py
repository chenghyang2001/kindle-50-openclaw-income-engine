"""模組層級的 JSONL 稽核軌跡（Level 3 企業級要求）。

刻意**不改 `_shared/`**：稽核欄位是 demo29 的權限模型專屬（誰、何時、看了哪些據點），
硬塞進共用層會逼其他 9 個模組接受一組它們用不到的欄位。

設計取捨（fail-closed）：
    對「資料存取」這類事件，寫不進稽核檔預設視為**致命**（`fail_closed=True`）。
    理由是本模組的賣點就是「店長看不到他店數據」，而唯一能事後證明這件事的
    只有稽核軌跡。稽核寫不進去卻照樣發報表，等於宣稱「我們有紀錄」但實際沒有——
    那比沒有紀錄更糟。需要在唯讀檔案系統上跑（容器、CI）時才把 `audit.fail_closed`
    設成 false，並自行承擔「這次執行無法舉證」的後果。

每一行是一個 JSON 物件，欄位固定，方便 `jq` / SIEM 直接吃：
    {"ts": ISO-8601 UTC, "run_id":…, "module":…, "event":…, "actor":…,
     "role":…, "pack":…, "visible_site_ids":[…],
     "role_was_unknown":…, "denial_reason":…, …事件專屬欄位}
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: 事件代碼常數。用常數而非字面字串，打錯字會在 import 期就被 linter 抓到。
EVENT_ACCESS_RESOLVED = "access_resolved"
EVENT_DATA_ACCESS = "data_access"
EVENT_PREFLIGHT = "preflight"
EVENT_DELIVERY = "delivery"
EVENT_STATE_UPDATE = "state_update"


class AuditError(RuntimeError):
    """稽核軌跡無法寫入。fail_closed 模式下必須讓整個流程停下來。"""


def new_run_id() -> str:
    """一次執行一個 run_id，讓同一輪的多筆事件可被串起來。"""
    return uuid.uuid4().hex[:12]


def utc_now() -> str:
    """稽核時間一律 UTC ISO-8601。多據點跨時區，用本地時間會對不起來。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AuditLog:
    """單次執行的稽核寫入器。"""

    def __init__(
        self,
        path: Path,
        module_name: str,
        run_id: str | None = None,
        fail_closed: bool = True,
    ) -> None:
        """
        path:        JSONL 檔案路徑（相對路徑由呼叫端先解析成絕對路徑）
        module_name: 寫入每筆事件的 module 欄位
        run_id:      未給則自動產生
        fail_closed: True -> 寫入失敗拋 AuditError；False -> 記錄失敗原因後繼續
        """
        self._path = Path(path)
        self._module_name = str(module_name)
        self._run_id = run_id or new_run_id()
        self._fail_closed = bool(fail_closed)
        self._events: list[dict[str, Any]] = []
        self._write_error: str | None = None

    @property
    def path(self) -> Path:
        """稽核檔絕對路徑。"""
        return self._path

    @property
    def run_id(self) -> str:
        """本次執行的識別碼。"""
        return self._run_id

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """本次執行已記錄的事件（含寫檔失敗時仍留在記憶體的那幾筆）。"""
        return tuple(self._events)

    @property
    def is_healthy(self) -> bool:
        """稽核軌跡是否完整落地。preflight 會檢查這一項。"""
        return self._write_error is None

    @property
    def write_error(self) -> str | None:
        """最後一次寫入失敗的原因（healthy 時為 None）。"""
        return self._write_error

    def record(self, event: str, scope: Any = None, **fields: Any) -> dict[str, Any]:
        """記一筆事件並立刻 append 到檔案。回傳寫入的 dict（供測試斷言）。

        `scope` 接受 `access_control.AccessScope`（或任何有 `to_dict()` 的物件），
        自動展開成 actor / role / pack / visible_site_ids 四個固定欄位——
        稽核的重點正是「誰、以什麼角色、看了哪些據點」，不能靠呼叫端每次手填。
        """
        entry: dict[str, Any] = {
            "ts": utc_now(),
            "run_id": self._run_id,
            "module": self._module_name,
            "event": str(event),
        }
        entry.update(_scope_fields(scope))
        entry.update(fields)
        self._events.append(entry)
        self._append(entry)
        return entry

    def _append(self, entry: dict[str, Any]) -> None:
        """單行 append。同時處理目錄不存在與寫入失敗兩種情況。"""
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            self._write_error = f"{type(exc).__name__}: {exc}"
            if self._fail_closed:
                raise AuditError(
                    f"稽核軌跡寫入失敗（{self._path}）：{exc}。"
                    "本模組的權限隔離承諾必須可被稽核佐證，因此中止本次執行；"
                    "若確定要在唯讀環境跑，請把 config 的 audit.fail_closed 設為 false"
                ) from exc


def _scope_fields(scope: Any) -> dict[str, Any]:
    """從 AccessScope 抽出稽核固定欄位；scope 為 None 時回空 dict。"""
    if scope is None:
        return {}
    payload = scope.to_dict() if hasattr(scope, "to_dict") else dict(scope)
    return {
        "actor": payload.get("actor"),
        "role": payload.get("role"),
        "pack": payload.get("pack"),
        "visible_site_ids": payload.get("visible_site_ids"),
        # 角色認不出來的呼叫可能是設定錯誤，也可能是有人在試探角色名稱。
        # 兩者都要留痕，否則事後只看得到「這次沒看到任何據點」而不知道為什麼。
        "role_was_unknown": payload.get("role_was_unknown", False),
        "denial_reason": payload.get("denial_reason"),
    }


def read_events(path: Path) -> list[dict[str, Any]]:
    """讀回整份稽核軌跡（測試與稽核查驗用）。

    壞掉的行會拋 `AuditError` 而不是被跳過：稽核檔一旦有無法解析的行，
    「這份紀錄是完整的」這個前提就不成立了，靜默略過等於偽造完整性。
    """
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"無法讀取稽核軌跡 {file_path}：{exc}") from exc

    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AuditError(f"稽核軌跡第 {line_no} 行不是合法 JSON（{file_path}）：{exc}") from exc
    return events
