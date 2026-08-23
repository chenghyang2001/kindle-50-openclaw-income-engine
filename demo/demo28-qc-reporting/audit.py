"""JSONL 稽核軌跡（模組自帶，刻意不動 `_shared/`）。

為什麼品管模組要自己的稽核日誌，而不是沿用 `Diagnostics` 的紅／琥珀／綠燈：

`Diagnostics` 是給**維運端**看的即時健康號誌，印在 stderr、跑完就沒了。
四階報告鏈本身是客戶的**品質稽核軌跡**（apxG_p16：班別 → 日 → 週 → 月/董事會），
稽核員三個月後要能回答「這條警報是哪一班、哪一台機、幾點被偵測到、誰收到報告」。
那需要一份可持久化、可逐筆回溯、且**看得出有沒有被事後刪行**的紀錄。

因此每一行 JSONL 都帶 `prev_hash` / `entry_hash` 串成雜湊鏈：
任何一行被改動或抽掉，`verify_file()` 都會在該行斷鏈。這不是防駭客
（有寫入權的人可以整份重算），而是防「無心的事後修飾」——這在品管紀錄
上是最常見、也最致命的一種資料失真。

寫入失敗時**不靜默吞掉**：計入 `write_failures`，並呼叫 `on_write_error`
回報給上層。稽核軌跡寫不進去的那一次執行，在合規上等同沒發生。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Callable, Iterable

#: 環境變數優先於 config.yaml，讓正式環境把稽核日誌導到受控儲存區。
AUDIT_ENV_VAR = "OPENCLAW_QC_AUDIT_LOG"

#: 雜湊鏈的起始值（第一筆的 prev_hash）。
GENESIS_HASH = "0" * 64


class AuditError(RuntimeError):
    """稽核軌跡本身的錯誤（路徑不可用、雜湊鏈斷裂）。"""


def resolve_audit_path(
    cli_path: str | None,
    config_path: str | None,
    module_dir: Path,
) -> Path:
    """決定稽核日誌位置：CLI 旗標 > 環境變數 > config.yaml > 模組預設。

    刻意不使用 cwd 相對路徑：使用者從其他目錄執行時，稽核檔會被丟進
    不相干的資料夾，之後誰也找不到那次執行的紀錄。
    """
    for candidate in (cli_path, os.environ.get(AUDIT_ENV_VAR), config_path):
        if candidate:
            path = Path(str(candidate)).expanduser()
            return path if path.is_absolute() else (module_dir / path)
    return module_dir / "audit" / "qc-audit.jsonl"


def _canonical(payload: dict[str, Any]) -> str:
    """穩定序列化：排序鍵、不留空白，確保同樣內容永遠算出同樣雜湊。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_entry_hash(prev_hash: str, body: dict[str, Any]) -> str:
    """以 prev_hash + 本筆內容算出 sha256，串成雜湊鏈。"""
    material = f"{prev_hash}|{_canonical(body)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class AuditEntry:
    """一筆稽核紀錄。`body` 是進入雜湊的部分，`entry_hash` 不進入自己的雜湊。"""

    seq: int
    event: str
    recorded_at: str
    module: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def body(self) -> dict[str, Any]:
        """回傳參與雜湊計算的欄位（不含 entry_hash 本身）。"""
        return {
            "seq": self.seq,
            "event": self.event,
            "recorded_at": self.recorded_at,
            "module": self.module,
            "payload": self.payload,
        }

    def to_dict(self) -> dict[str, Any]:
        """完整 JSON-safe 結構（寫進 JSONL 的那一行）。"""
        data = self.body()
        data["prev_hash"] = self.prev_hash
        data["entry_hash"] = self.entry_hash
        return data


def _default_error_reporter(message: str) -> None:
    """預設的寫入失敗回報：印到 stderr，絕不靜默。"""
    print(f"[audit] 稽核軌跡寫入失敗：{message}", file=sys.stderr)


@dataclass
class AuditTrail:
    """附加式（append-only）JSONL 稽核軌跡。

    `enabled=False` 時只在記憶體累積、不落地，供測試與 `--dry-run` 使用。
    """

    path: Path
    module_name: str
    tz: tzinfo | None = None
    enabled: bool = True
    on_write_error: Callable[[str], None] = _default_error_reporter
    entries: list[AuditEntry] = field(default_factory=list)
    write_failures: int = 0
    _last_hash: str = GENESIS_HASH

    def record(self, event: str, **payload: Any) -> AuditEntry:
        """寫一筆稽核紀錄並回傳。event 用 snake_case 動詞短語。"""
        entry = self._build_entry(event, payload)
        self.entries.append(entry)
        self._last_hash = entry.entry_hash
        if self.enabled:
            self._append_line(entry)
        return entry

    def _build_entry(self, event: str, payload: dict[str, Any]) -> AuditEntry:
        """組出帶雜湊的 AuditEntry（不落地）。"""
        body = {
            "seq": len(self.entries) + 1,
            "event": event,
            "recorded_at": datetime.now(self.tz).isoformat(timespec="seconds"),
            "module": self.module_name,
            "payload": _json_safe(payload),
        }
        return AuditEntry(
            seq=body["seq"],
            event=body["event"],
            recorded_at=body["recorded_at"],
            module=body["module"],
            payload=body["payload"],
            prev_hash=self._last_hash,
            entry_hash=compute_entry_hash(self._last_hash, body),
        )

    def _append_line(self, entry: AuditEntry) -> None:
        """實際落地。失敗時計數並回報，不中斷報告鏈——
        報告該送還是要送，但這次執行會被標記為「稽核軌跡不完整」。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(_canonical(entry.to_dict()) + "\n")
        except OSError as exc:
            self.write_failures += 1
            self.on_write_error(f"{self.path}：{exc}")

    def verify_chain(self) -> bool:
        """驗證記憶體中的雜湊鏈是否首尾相連。"""
        prev = GENESIS_HASH
        for entry in self.entries:
            if entry.prev_hash != prev:
                return False
            if compute_entry_hash(prev, entry.body()) != entry.entry_hash:
                return False
            prev = entry.entry_hash
        return True

    def summary(self) -> dict[str, Any]:
        """給 `run()` 回傳結構用的摘要。"""
        return {
            "path": str(self.path),
            "enabled": self.enabled,
            "entry_count": len(self.entries),
            "write_failures": self.write_failures,
            "chain_verified": self.verify_chain(),
            "last_hash": self._last_hash,
            "events": [entry.event for entry in self.entries],
        }


def _json_safe(value: Any) -> Any:
    """把 Decimal / Path / 巢狀結構轉成 JSON 可序列化的形式。

    Decimal 一律轉字串而不是 float：稽核紀錄裡的 0.10 必須永遠是 0.10，
    不能在某台機器上重算成 0.1000000000000000055。
    """
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def verify_file(path: str | Path) -> tuple[bool, str]:
    """獨立驗證磁碟上的 JSONL 稽核檔（供 QA 與稽核員事後查核）。

    回傳 `(是否通過, 說明)`。任何一行被改寫或抽掉都會在該行斷鏈。
    """
    target = Path(path).expanduser()
    try:
        raw_lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError as exc:
        return False, f"讀不到稽核檔 {target}：{exc}"

    prev = GENESIS_HASH
    for line_no, raw in enumerate(raw_lines, start=1):
        ok, prev, message = _verify_line(raw, prev, line_no)
        if not ok:
            return False, message
    return True, f"稽核鏈完整，共 {len(raw_lines)} 筆"


def _verify_line(raw: str, prev: str, line_no: int) -> tuple[bool, str, str]:
    """驗證單行；回傳 `(是否通過, 新的 prev_hash, 訊息)`。"""
    try:
        record = json.loads(raw)
    except json.JSONDecodeError as exc:
        return False, prev, f"第 {line_no} 行 JSON 解析失敗：{exc}"

    if record.get("prev_hash") != prev:
        return False, prev, f"第 {line_no} 行 prev_hash 不接續（疑似被刪行或插入）"

    body = {key: record.get(key) for key in ("seq", "event", "recorded_at", "module", "payload")}
    expected = compute_entry_hash(prev, body)
    if record.get("entry_hash") != expected:
        return False, prev, f"第 {line_no} 行內容與 entry_hash 不符（疑似被竄改）"
    return True, expected, ""


def summarise_events(entries: Iterable[AuditEntry]) -> dict[str, int]:
    """統計各類事件出現次數，方便報告尾端附上稽核摘要。"""
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.event] = counts.get(entry.event, 0) + 1
    return counts
