"""稽核軌跡（JSONL）：企業級模組的「誰、在什麼時候、依據什麼、做了什麼」。

為何 Level 3 一定要有這一層：
    Level 1/2 的模組出錯，最壞是少寄一封信；Level 3 的模組直接改寫客戶 CRM、
    寄出提案、啟動 5 節點追蹤序列。當客戶問「為什麼這筆交易被自動跳過提案」
    或「這封信是誰核准寄出的」，沒有稽核軌跡就只能猜。

每一列（JSON Lines，一行一筆）固定五個必要欄位：
    timestamp          事件時間（ISO 8601，含時區）
    action             動作代碼（穩定字串，供下游 grep / 匯入 BI）
    subject            對象（交易 ID、事件 ID 或執行代號）
    rationale          決策依據（為什麼做這個決定）
    is_human_approved  是否已取得人工核准

刻意不寫入的東西：
    信件與提案正文一律不落地，只記長度與雜湊。稽核軌跡的保存期通常遠長於
    客戶通訊內容的保存政策，把正文寫進去等於把 GDPR 風險寫進 log。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_AUDIT_PATH = MODULE_DIR / "audit" / "pipeline_audit.jsonl"

# 動作代碼（穩定字串鍵，測試與下游 BI 會直接比對）
ACTION_RUN_STARTED = "run_started"
ACTION_RUN_COMPLETED = "run_completed"
ACTION_EVENT_REJECTED = "event_rejected"
ACTION_SLA_BREACH = "sla_breach"
ACTION_CHAIN_DRAFTED = "chain_drafted"
ACTION_CHAIN_EXECUTED = "chain_executed"
ACTION_CHAIN_HALTED = "chain_halted"
ACTION_DRY_RUN_RECEIPT = "dry_run_receipt"
ACTION_SAFETY_OVERRIDE = "safety_override"

REQUIRED_FIELDS = ("timestamp", "action", "subject", "rationale", "is_human_approved")


class AuditError(RuntimeError):
    """稽核軌跡寫入失敗。稽核寫不進去屬於嚴重問題，不可靜默吞掉。"""


def body_digest(text: str) -> dict[str, Any]:
    """把產出的正文換成「長度 + SHA256 前 16 碼」，可驗證但不外洩內容。"""
    payload = str(text or "")
    return {
        "chars": len(payload),
        "sha256_16": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


class AuditLog:
    """單次執行的稽核軌跡寫入器（append-only JSONL）。"""

    def __init__(
        self,
        path: str | Path | None = None,
        module_name: str = "demo22-sales-pipeline",
        run_id: str = "",
        is_dry_run: bool = False,
        enabled: bool = True,
    ) -> None:
        """
        path:        JSONL 檔路徑，預設模組目錄下的 audit/pipeline_audit.jsonl
        run_id:      本次執行代號，讓同一次執行的所有列可被關聯查詢
        is_dry_run:  空跑旗標，會寫進每一列以便事後區分演練與正式動作
        enabled:     False 時只留記憶體紀錄不寫檔（供測試或唯讀環境）
        """
        if not isinstance(module_name, str) or not module_name.strip():
            raise ValueError("module_name 必須是非空字串")
        self._path = Path(path) if path is not None else DEFAULT_AUDIT_PATH
        self._module_name = module_name.strip()
        self._run_id = str(run_id or "")
        self._is_dry_run = bool(is_dry_run)
        self._enabled = bool(enabled)
        self._entries: list[dict] = []

    @property
    def path(self) -> Path:
        """稽核檔絕對路徑。"""
        return self._path

    @property
    def entries(self) -> list[dict]:
        """本次執行已寫入的所有列（記憶體副本，供測試斷言）。"""
        return list(self._entries)

    @property
    def is_enabled(self) -> bool:
        """是否實際落地寫檔。"""
        return self._enabled

    def record(
        self,
        action: str,
        subject: str,
        rationale: str,
        is_human_approved: bool,
        when: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict:
        """寫一列稽核紀錄並回傳該列內容。"""
        if not str(action or "").strip():
            raise ValueError("action 不可為空")
        if not str(rationale or "").strip():
            raise ValueError("rationale（決策依據）不可為空——稽核軌跡的價值就在這一欄")
        entry = self._compose(action, subject, rationale, is_human_approved, when, extra)
        self._entries.append(entry)
        if self._enabled:
            self._append(entry)
        return entry

    def _compose(
        self,
        action: str,
        subject: str,
        rationale: str,
        is_human_approved: bool,
        when: datetime | None,
        extra: dict[str, Any] | None,
    ) -> dict:
        """組出單列內容（必要欄位固定在前，額外欄位收在 detail）。"""
        stamp = when if when is not None else datetime.now().astimezone()
        return {
            "timestamp": stamp.isoformat(),
            "action": str(action).strip(),
            "subject": str(subject or "<none>"),
            "rationale": str(rationale).strip(),
            "is_human_approved": bool(is_human_approved),
            "module": self._module_name,
            "run_id": self._run_id,
            "is_dry_run": self._is_dry_run,
            "detail": dict(extra or {}),
        }

    def _append(self, entry: dict) -> None:
        """追加一行 JSON 到稽核檔；寫不進去要明確報錯。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            raise AuditError(f"稽核軌跡寫入失敗：{self._path}｜{exc}") from exc


def read_entries(path: str | Path) -> list[dict]:
    """讀回稽核檔的所有列（供測試與事後查核）。空白行略過。"""
    target = Path(path)
    if not target.is_file():
        raise AuditError(f"找不到稽核檔：{target.resolve()}")
    entries: list[dict] = []
    for line_no, line in enumerate(target.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise AuditError(f"稽核檔第 {line_no} 行不是合法 JSON：{target}") from exc
    return entries


def verify_entries(entries: list[dict]) -> list[str]:
    """檢查每一列是否都具備五個必要欄位，回傳問題描述清單（空清單代表合格）。"""
    problems: list[str] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"第 {index} 列不是 object")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in entry]
        if missing:
            problems.append(f"第 {index} 列缺少欄位：{', '.join(missing)}")
    return problems
