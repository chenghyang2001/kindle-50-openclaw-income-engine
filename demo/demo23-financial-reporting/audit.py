"""稽核軌跡（JSONL + 雜湊鏈）— 董事會級財務報表的「誰在什麼時候做了什麼」。

為什麼財務模組要有自己的稽核日誌，而不是靠 stdout / 通知紀錄：

1. **審核是法遵動作**：apxG_p08 要求 T+1 財務總監人工核准後才可對董事會發布。
   「核准過了」這件事必須留下不可否認的紀錄，否則事後查核只剩口頭說法。
2. **stdout 會被輪替掉**：主控台輸出不是紀錄，重跑一次就沒了。
3. **JSONL 可 append、可逐行解析**：中途中斷不會毀掉整個檔案（JSON 陣列會）。

刻意**不改 `_shared/`**：稽核需求是本模組專屬（其他 demo 不需要保存財務核准鏈），
把它塞進共用層會讓 10 個模組全部背上一個用不到的依賴。

## 雜湊鏈（為什麼「附加寫入」還不夠）

本模組花了很大力氣做**核准綁定內容指紋**——核准的是那一份數字，改了數字核准就失效。
但如果稽核日誌只是純附加的 JSONL，**核准紀錄本身可以被偽造**：能碰到檔案的人可以把
`approval_granted` 的 `approved_by` 從 A 改成 B，或把時間往前挪，讓一份逾時的核准
看起來合規。前門鎖了，後門沒鎖。

因此每一筆都帶 `prev_hash` 與 `entry_hash`：

    entry_hash = sha256(prev_hash + 本筆內容的正規化 JSON)

- 改掉任何一筆的內容（時間戳、核准人、detail）→ `entry_hash` 對不上內容。
- 整行刪掉 → 下一筆的 `prev_hash` 接不上前一筆。
- 兩者都由 `verify_file()` 指出**第一個斷鏈的行號**，稽核人員可直接定位竄改位置。

這擋不住「整份檔案被換掉」（那需要外部 WORM 儲存或簽章服務），但財務稽核的真實
威脅模型本來就是內部人的小幅竄改，不是重建整條鏈。

寫入失敗一律拋 `AuditError` 由呼叫端升級為紅色警報：**沒有稽核軌跡就不准發報表**，
不是「記不下來就算了」。這是本模組唯一會因為「記錄失敗」而停下來的地方。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date, datetime, tzinfo
from decimal import Decimal
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

#: 鏈的起點。第一筆的 prev_hash 固定為此值，讓「檔案從第一行就被砍掉」也看得出來。
GENESIS_HASH = "0" * 64

#: 參與雜湊計算的欄位（順序無關，序列化時會 sort_keys）。
HASHED_FIELDS = ("seq", "ts", "run_id", "module", "event", "actor", "detail")


class AuditError(RuntimeError):
    """稽核軌跡無法寫入、無法讀回，或既有內容已損壞。"""


# --------------------------------------------------------------------------
# 雜湊
# --------------------------------------------------------------------------


def normalize(value: Any) -> Any:
    """把內容正規化成可穩定序列化的形式。

    `Decimal` 一律轉字串：本模組全程用 Decimal 表達金額，而 `json.dumps` 不接受
    Decimal；若寫入時轉一種、驗證時轉另一種（例如 float），重算出來的雜湊會與當初
    寫入的不同，整條鏈會出現「假斷點」，稽核人員無從分辨真竄改與假警報。
    因此寫入與驗證共用這一個函式。
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize(item) for item in value]
    return value


def compute_entry_hash(prev_hash: str, payload: dict[str, Any]) -> str:
    """本筆雜湊 = sha256(前一筆雜湊 + 本筆內容的正規化 JSON)。"""
    body = json.dumps(normalize(payload), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{prev_hash}{body}".encode("utf-8")).hexdigest()


def content_fingerprint(payload: str) -> str:
    """對報表內容取 SHA-256 指紋（前 16 碼）。

    核准綁定的是「這一份數字」，不是「這個月份」。任何一個科目金額變動都會讓指紋
    改變，先前的核准隨即失效、必須重審——否則「先核准一份乾淨的，再換掉數字發出去」
    這條路是敞開的。
    """
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# 讀取與驗證
# --------------------------------------------------------------------------


def read_rows(path: str | Path) -> list[tuple[int, dict[str, Any]]]:
    """讀回 (行號, 內容) 清單。空行略過；**壞行明確報錯，不靜默略過**。

    靜默略過壞行等同於偽造完整性：把一筆紀錄改成無法解析的垃圾，就能讓它從稽核
    結果中消失。所以解析失敗一律拋 `AuditError`，並附上行號。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        return []

    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"稽核軌跡讀取失敗：{target}｜{exc}") from exc

    rows: list[tuple[int, dict[str, Any]]] = []
    for number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AuditError(f"稽核軌跡第 {number} 行不是合法 JSON：{target}｜{exc}") from exc
        if not isinstance(parsed, dict):
            raise AuditError(f"稽核軌跡第 {number} 行不是物件：{target}")
        rows.append((number, parsed))
    return rows


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """讀取 JSONL 稽核檔（不含行號）。檔案不存在回空清單。"""
    return [row for _, row in read_rows(path)]


def _verify_row(row: dict[str, Any], line_number: int, prev_hash: str) -> list[str]:
    """驗證單一筆：prev_hash 是否接得上、entry_hash 是否與內容相符。"""
    problems: list[str] = []
    actual_prev = str(row.get("prev_hash", ""))
    if actual_prev != prev_hash:
        problems.append(
            f"第 {line_number} 行的 prev_hash 接不上前一筆（可能有紀錄被刪除或插入）"
        )
    payload = {key: row.get(key) for key in HASHED_FIELDS}
    if compute_entry_hash(actual_prev, payload) != str(row.get("entry_hash", "")):
        problems.append(f"第 {line_number} 行的 entry_hash 與內容不符（內容被竄改）")
    return problems


def verify_file(path: str | Path) -> list[str]:
    """獨立讀取磁碟檔驗證整條雜湊鏈，回傳所有問題描述（空清單代表完整）。

    訊息一律帶行號，且**第一個斷鏈點排在最前**——稽核人員要的是「從哪一行開始
    不能信」，不是一份沒有座標的錯誤清單。
    """
    problems: list[str] = []
    prev_hash = GENESIS_HASH
    for line_number, row in read_rows(path):
        problems.extend(_verify_row(row, line_number, prev_hash))
        prev_hash = str(row.get("entry_hash", ""))
    return problems


def first_broken_line(path: str | Path) -> int | None:
    """回傳第一個斷鏈的行號；整條鏈完整時回 None。"""
    problems = verify_file(path)
    if not problems:
        return None
    digits = "".join(char for char in problems[0] if char.isdigit())
    return int(digits) if digits else None


# --------------------------------------------------------------------------
# 寫入
# --------------------------------------------------------------------------


class AuditLog:
    """單次執行的稽核寫入口。每個事件一行 JSON，並串接成雜湊鏈。"""

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

        開檔時會讀既有檔案的最後一筆，**續接它的 seq 與 entry_hash**，
        讓雜湊鏈跨執行累積（每次執行重新從 GENESIS 起算等於自己剪斷鏈）。
        """
        if not str(module_name).strip():
            raise AuditError("module_name 必須是非空字串")
        self._path = Path(path).expanduser()
        self._module_name = str(module_name).strip()
        self._tz = tz
        self._run_id = run_id or uuid.uuid4().hex[:12]
        self._now = now_provider or (lambda: datetime.now(self._tz))
        self._entries: list[dict[str, Any]] = []
        self._seq, self._prev_hash = self._read_tail()
        self._chain_start = self._prev_hash

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
        return len(self._entries)

    @property
    def entries(self) -> tuple[dict[str, Any], ...]:
        """本次執行寫入的紀錄（不含開檔前既有的歷史）。"""
        return tuple(self._entries)

    def _read_tail(self) -> tuple[int, str]:
        """讀既有檔案的最後一筆，取得續接用的 seq 與 prev_hash。"""
        rows = read_rows(self._path)
        if not rows:
            return 0, GENESIS_HASH
        _, last = rows[-1]
        try:
            seq = int(last.get("seq", 0))
        except (TypeError, ValueError) as exc:
            raise AuditError(f"稽核軌跡最後一筆的 seq 不是整數：{self._path}") from exc
        return seq, str(last.get("entry_hash") or GENESIS_HASH)

    def record(
        self,
        event: str,
        detail: dict[str, Any] | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """寫入一則稽核事件並回傳寫入的內容（供測試斷言與結果 dict 引用）。"""
        if not str(event).strip():
            raise AuditError("event 必須是非空字串")

        payload = {
            "seq": self._seq + 1,
            "ts": self._now().isoformat(timespec="seconds"),
            "run_id": self._run_id,
            "module": self._module_name,
            "event": str(event).strip(),
            "actor": str(actor or "system").strip() or "system",
            "detail": normalize(detail or {}),
        }
        entry = {
            **payload,
            "prev_hash": self._prev_hash,
            "entry_hash": compute_entry_hash(self._prev_hash, payload),
        }
        self._append(entry)
        self._seq = int(entry["seq"])
        self._prev_hash = str(entry["entry_hash"])
        self._entries.append(entry)
        return entry

    def _append(self, entry: dict[str, Any]) -> None:
        """實際落地。父目錄不存在就補建，寫入失敗轉成 AuditError。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            raise AuditError(f"稽核軌跡寫入失敗：{self._path}｜{exc}") from exc

    def verify_chain(self) -> list[str]:
        """驗證**記憶體中**本次執行寫入的鏈，回傳所有斷點描述（空清單代表完整）。

        與 `verify_file()` 的分工：這個方法證明「程式自己產出的鏈是接得上的」；
        `verify_file()` 才是事後稽核用的——它重讀磁碟，抓的是寫入之後被動手腳的情況。
        """
        problems: list[str] = []
        prev_hash = self._chain_start
        for entry in self._entries:
            seq = entry.get("seq")
            if str(entry.get("prev_hash", "")) != prev_hash:
                problems.append(f"第 {seq} 筆（seq）的 prev_hash 接不上前一筆")
            payload = {key: entry.get(key) for key in HASHED_FIELDS}
            if compute_entry_hash(str(entry.get("prev_hash", "")), payload) != str(
                entry.get("entry_hash", "")
            ):
                problems.append(f"第 {seq} 筆（seq）的 entry_hash 與內容不符")
            prev_hash = str(entry.get("entry_hash", ""))
        return problems

    def verify_file(self) -> list[str]:
        """重讀本檔並驗證整條鏈（含開檔前的歷史紀錄）。"""
        return verify_file(self._path)

    def events(self) -> list[dict[str, Any]]:
        """讀回本檔全部事件（含歷史執行）。"""
        return read_events(self._path)


def resolve_audit_path(cli_value: str | None, config_value: str, module_dir: Path) -> Path:
    """決定稽核檔位置：`--audit-file` > 環境變數 > config.yaml。

    相對路徑一律以**模組目錄**為基準而不是 cwd：從別的目錄執行時，
    稽核檔散落在使用者當下所在的資料夾，等同於沒有稽核軌跡。
    """
    override = cli_value or os.environ.get("OPENCLAW_AUDIT_LOG", "").strip()
    chosen = override or config_value
    expanded = Path(os.path.expandvars(str(chosen))).expanduser()
    return expanded if expanded.is_absolute() else (module_dir / expanded)
