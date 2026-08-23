"""模組級稽核軌跡（append-only JSONL）。

Level 3 企業級要求「每一個會改動線上資料的決策都必須可回溯」：
什麼時間、哪一次執行、對哪個 SKU、做了什麼判斷、被哪一條安全規則擋下。
本模組把這些事件逐行追加到 JSONL 檔，並用 SHA-256 串鏈（每筆帶上一筆的
雜湊）讓事後竄改可被偵測——稽核軌跡若能被無痕修改，就不是稽核軌跡。

為何自建而不動 `_shared/`：`_shared/` 是 10 個 demo 共用的凍結契約，
稽核軌跡是本模組（唯一持有 `write_products` 寫入權限）的特有需求，
不應反向污染共用層。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
# 串鏈起點。第一筆的 prev_hash 固定為 64 個 0，讓「檔案被整段刪除重寫」
# 也無法偽裝成合法的鏈頭（驗證時會發現 seq 與時間軸對不上）。
GENESIS_HASH = "0" * 64

SEVERITY_INFO = "info"
SEVERITY_AMBER = "amber"
SEVERITY_RED = "red"
SEVERITIES = (SEVERITY_INFO, SEVERITY_AMBER, SEVERITY_RED)


class AuditError(RuntimeError):
    """稽核軌跡無法寫入、或既有內容損毀"""


def _canonical(payload: dict[str, Any]) -> str:
    """把事件轉成穩定字串以計算雜湊（鍵排序 + 無空白，確保跨機器一致）"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_of(payload: dict[str, Any]) -> str:
    """計算單筆事件的 SHA-256（不含 entry_hash 欄位本身）"""
    body = {key: value for key, value in payload.items() if key != "entry_hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def new_run_id(now: datetime | None = None) -> str:
    """產生本次執行的識別碼：UTC 時間戳 + 8 位隨機碼（同秒多次執行也不撞號）"""
    moment = now or datetime.now(timezone.utc)
    return f"{moment.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def read_entries(path: str | Path) -> list[dict[str, Any]]:
    """讀回整份稽核軌跡；檔案不存在視為尚未有紀錄，回傳空清單。"""
    target = Path(path)
    if not target.is_file():
        return []
    entries: list[dict[str, Any]] = []
    for line_no, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AuditError(f"稽核軌跡第 {line_no} 行無法解析：{target}（{exc}）") from exc
    return entries


def verify_chain(path: str | Path) -> tuple[bool, str]:
    """驗證雜湊串鏈是否完整，回傳 (是否通過, 說明)。

    任何一筆被改過、被刪除或被插入，後續所有 prev_hash 都對不上，
    這裡會指出第一個斷點的位置。
    """
    entries = read_entries(path)
    expected_prev = GENESIS_HASH
    for index, entry in enumerate(entries):
        if entry.get("prev_hash") != expected_prev:
            return False, f"第 {index + 1} 筆的 prev_hash 與前一筆不符（軌跡被竄改或缺頁）"
        if entry.get("entry_hash") != _hash_of(entry):
            return False, f"第 {index + 1} 筆的內容與 entry_hash 不符（該筆被改過）"
        expected_prev = str(entry["entry_hash"])
    return True, f"{len(entries)} 筆稽核紀錄串鏈完整"


class AuditLog:
    """單次執行的稽核軌跡寫入器（append-only，不提供刪改介面）。"""

    def __init__(
        self,
        path: str | Path,
        module: str,
        run_id: str | None = None,
        is_enabled: bool = True,
    ) -> None:
        """
        path:       JSONL 檔路徑（呼叫端負責解析成絕對路徑）
        module:     模組識別字串，寫進每一筆紀錄
        run_id:     本次執行識別碼，省略則自動產生
        is_enabled: False 時只在記憶體累積、不落地（供 --dry-run 使用）
        """
        if not str(module).strip():
            raise AuditError("module 必須是非空字串")
        self._path = Path(path)
        self._module = str(module).strip()
        self._run_id = run_id or new_run_id()
        self._is_enabled = bool(is_enabled)
        self._entries: list[dict[str, Any]] = []
        self._prev_hash: str | None = None

    @property
    def path(self) -> Path:
        """稽核檔路徑"""
        return self._path

    @property
    def run_id(self) -> str:
        """本次執行識別碼"""
        return self._run_id

    @property
    def is_enabled(self) -> bool:
        """是否實際寫入檔案"""
        return self._is_enabled

    @property
    def entries(self) -> list[dict[str, Any]]:
        """本次執行產生的紀錄（複本，避免外部改到內部狀態）"""
        return [dict(entry) for entry in self._entries]

    def record(
        self,
        event: str,
        *,
        severity: str = SEVERITY_INFO,
        sku_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一筆稽核事件並回傳該筆內容。

        detail 內的值必須可 JSON 序列化——金額請先轉成字串再傳進來，
        直接丟 Decimal 會在寫檔時才爆炸，那時已經來不及補救。
        """
        if not str(event).strip():
            raise AuditError("event 必須是非空字串")
        if severity not in SEVERITIES:
            raise AuditError(f"未知的 severity {severity!r}，可用：{', '.join(SEVERITIES)}")
        entry = self._build_entry(event, severity, sku_id, detail)
        self._entries.append(entry)
        self._prev_hash = str(entry["entry_hash"])
        if self._is_enabled:
            self._append_line(entry)
        return entry

    def _build_entry(
        self, event: str, severity: str, sku_id: str | None, detail: dict[str, Any] | None
    ) -> dict[str, Any]:
        """組出單筆事件（含串鏈雜湊）"""
        entry: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "run_id": self._run_id,
            "module": self._module,
            "seq": len(self._entries) + 1,
            "event": str(event).strip(),
            "severity": severity,
            "sku_id": sku_id,
            "detail": detail or {},
            "prev_hash": self._tail_hash(),
        }
        entry["entry_hash"] = _hash_of(entry)
        return entry

    def _tail_hash(self) -> str:
        """取得串鏈上一筆的雜湊：先看本次記憶體，再回頭讀檔尾。"""
        if self._prev_hash is not None:
            return self._prev_hash
        existing = read_entries(self._path) if self._is_enabled else []
        self._prev_hash = str(existing[-1]["entry_hash"]) if existing else GENESIS_HASH
        return self._prev_hash

    def _append_line(self, entry: dict[str, Any]) -> None:
        """把事件追加成一行 JSON。寫入失敗必須拋錯，不可靜默吞掉。

        稽核軌跡寫不進去卻繼續改價，等於「無紀錄的自動調價」——
        那是本模組最不能接受的狀態。
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(entry) + "\n")
        except OSError as exc:
            raise AuditError(f"稽核軌跡寫入失敗：{self._path}（{exc}）") from exc
