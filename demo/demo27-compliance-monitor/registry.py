"""demo27 — 三份 CSV 稽核台帳（附錄G apxG_p15 逐字實作）。

| 檔案 | 內容 |
| --- | --- |
| `contract_inventory.csv` | 合約到期軌跡 |
| `licence_inventory.csv`  | 執照到期軌跡 |
| `policy_register.csv`    | 內部政策審查週期軌跡 |

**台帳即稽核證據**，因此三條硬規則：

1. **追加式（append-only）**：一律 `mode="a"`，本檔案沒有任何一處用 `"w"` 開啟台帳。
   歷史列永遠保留——今天判定錯了，明天的更正是「再追加一列」，不是改掉舊列。
2. **每筆含時間戳與來源依據**：`recorded_at` + `run_id` + `source_ref`（來源檔#識別碼）
   + `evidence_quote`（條款原文逐字），任何一列都能回推當時憑什麼這樣判。
3. **標頭一經寫入不得變更**：既有檔標頭與本版欄位不符時直接報錯，
   絕不「順手」改寫或覆蓋——那會讓整份稽核軌跡失去證據能力。

⚠️ 法律免責：台帳內容僅供合規團隊初步篩選，不構成法律意見。
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from analyser import Finding

CONTRACT_COLUMNS: tuple[str, ...] = (
    "recorded_at", "run_id", "contract_id", "counterparty", "contract_type",
    "effective_date", "expiry_date", "days_to_expiry", "warning_stage",
    "auto_renew", "notice_period_days", "annual_value", "currency", "owner",
    "escalation_level", "delivery_status", "needs_human_review", "review_reasons",
    "clause_confidence", "source_ref", "evidence_quote",
)

LICENCE_COLUMNS: tuple[str, ...] = (
    "recorded_at", "run_id", "licence_id", "licence_name", "issuing_authority",
    "jurisdiction", "issued_date", "expiry_date", "days_to_expiry", "warning_stage",
    "owner", "escalation_level", "delivery_status", "needs_human_review",
    "review_reasons", "clause_confidence", "source_ref", "evidence_quote",
)

POLICY_COLUMNS: tuple[str, ...] = (
    "recorded_at", "run_id", "policy_id", "policy_name", "owner", "approval_body",
    "last_reviewed", "review_cycle_days", "next_review_due", "days_to_review_due",
    "review_stage", "escalation_level", "delivery_status", "needs_human_review",
    "review_reasons", "clause_confidence", "source_ref", "evidence_quote",
)


class RegistryError(RuntimeError):
    """台帳寫入失敗或標頭不相容（絕不覆蓋既有稽核軌跡）"""


@dataclass(frozen=True)
class LedgerDecision:
    """一次執行對某筆發現做出的升級決定（寫進台帳的判定欄）。"""

    level: str
    delivery_status: str


@dataclass(frozen=True)
class LedgerSpec:
    """一份台帳的欄位定義與列組裝方式。"""

    kind: str
    config_key: str
    columns: tuple[str, ...]
    build_row: Callable[[Finding, LedgerDecision, str, str], dict[str, str]]


def _common_fields(finding: Finding, decision: LedgerDecision, recorded_at: str, run_id: str) -> dict[str, str]:
    """所有台帳共用的稽核欄位（時間戳 / 來源依據 / 逐字佐證）。"""
    return {
        "recorded_at": recorded_at,
        "run_id": run_id,
        "owner": finding.owner,
        "escalation_level": decision.level or "none",
        "delivery_status": decision.delivery_status,
        "needs_human_review": "true" if finding.needs_human_review else "false",
        "review_reasons": "；".join(finding.review_reasons),
        "clause_confidence": "" if finding.confidence is None else f"{finding.confidence:.2f}",
        "source_ref": finding.source_ref,
        "evidence_quote": finding.evidence,
    }


def _days_text(finding: Finding) -> str:
    """天數欄：算不出來就留空，不填 0（0 代表「今天到期」，語意完全不同）。"""
    return "" if finding.days is None else str(finding.days)


def build_contract_row(finding: Finding, decision: LedgerDecision, recorded_at: str, run_id: str) -> dict[str, str]:
    """組出 contract_inventory.csv 的一列。"""
    details = finding.details
    row = _common_fields(finding, decision, recorded_at, run_id)
    row.update(
        {
            "contract_id": finding.record_id,
            "counterparty": details.get("counterparty", ""),
            "contract_type": details.get("contract_type", ""),
            "effective_date": details.get("effective_date", ""),
            "expiry_date": details.get("expiry_date", ""),
            "days_to_expiry": _days_text(finding),
            "warning_stage": finding.stage,
            "auto_renew": details.get("auto_renew", ""),
            "notice_period_days": details.get("notice_period_days", ""),
            "annual_value": "" if finding.amount is None else str(finding.amount),
            "currency": details.get("currency", ""),
        }
    )
    return row


def build_licence_row(finding: Finding, decision: LedgerDecision, recorded_at: str, run_id: str) -> dict[str, str]:
    """組出 licence_inventory.csv 的一列。"""
    details = finding.details
    row = _common_fields(finding, decision, recorded_at, run_id)
    row.update(
        {
            "licence_id": finding.record_id,
            "licence_name": finding.title,
            "issuing_authority": details.get("issuing_authority", ""),
            "jurisdiction": details.get("jurisdiction", ""),
            "issued_date": details.get("issued_date", ""),
            "expiry_date": details.get("expiry_date", ""),
            "days_to_expiry": _days_text(finding),
            "warning_stage": finding.stage,
        }
    )
    return row


def build_policy_row(finding: Finding, decision: LedgerDecision, recorded_at: str, run_id: str) -> dict[str, str]:
    """組出 policy_register.csv 的一列。"""
    details = finding.details
    row = _common_fields(finding, decision, recorded_at, run_id)
    row.update(
        {
            "policy_id": finding.record_id,
            "policy_name": finding.title,
            "approval_body": details.get("approval_body", ""),
            "last_reviewed": details.get("last_reviewed", ""),
            "review_cycle_days": details.get("review_cycle_days", ""),
            "next_review_due": details.get("next_review_due", ""),
            "days_to_review_due": _days_text(finding),
            "review_stage": finding.stage,
        }
    )
    return row


LEDGER_SPECS: tuple[LedgerSpec, ...] = (
    LedgerSpec("contract", "contracts", CONTRACT_COLUMNS, build_contract_row),
    LedgerSpec("licence", "licences", LICENCE_COLUMNS, build_licence_row),
    LedgerSpec("policy", "policies", POLICY_COLUMNS, build_policy_row),
)


def _read_existing_header(path: Path) -> list[str] | None:
    """讀既有台帳的標頭列；檔案不存在或為空回 None。"""
    if not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return next(csv.reader(handle), [])
    except OSError as exc:
        raise RegistryError(f"讀取台帳標頭失敗：{path}｜{exc}") from exc


def append_rows(path: Path, columns: tuple[str, ...], rows: Iterable[dict[str, str]]) -> int:
    """把資料列**追加**到 CSV 台帳，回傳實際寫入列數。

    只在檔案不存在或長度為 0 時寫標頭；既有標頭不符即報錯，不改寫歷史。
    """
    payload = list(rows)
    header = _read_existing_header(path)
    if header is not None and header != list(columns):
        raise RegistryError(
            f"台帳標頭與本版欄位不符：{path}\n  既有：{header}\n  預期：{list(columns)}\n"
            "  台帳是稽核證據，本模組不會覆寫既有檔；請改用新的 --registry-dir 或人工遷移。"
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="raise")
            if header is None:
                writer.writeheader()
            for row in payload:
                writer.writerow(row)
    except (OSError, ValueError) as exc:
        raise RegistryError(f"台帳寫入失敗：{path}｜{exc}") from exc
    return len(payload)


def ledger_paths(registry_dir: Path, files_config: dict[str, Any]) -> dict[str, Path]:
    """依 config.registry.files 算出三份台帳的實際路徑。"""
    paths: dict[str, Path] = {}
    for spec in LEDGER_SPECS:
        filename = str(files_config.get(spec.config_key) or f"{spec.config_key}.csv")
        paths[spec.kind] = registry_dir / filename
    return paths


def write_ledgers(
    findings: list[Finding],
    decisions: dict[str, LedgerDecision],
    paths: dict[str, Path],
    recorded_at: str,
    run_id: str,
) -> dict[str, int]:
    """把本次所有發現追加進對應台帳，回傳 {台帳種類: 新增列數}。

    **未升級的發現一樣要入帳**——稽核要看的是「每天都有掃、掃到什麼」，
    只記警報會讓「這段期間系統其實沒在跑」變得無法證明。
    """
    written: dict[str, int] = {}
    fallback = LedgerDecision(level="", delivery_status="not_escalated")
    for spec in LEDGER_SPECS:
        rows = [
            spec.build_row(finding, decisions.get(finding.key, fallback), recorded_at, run_id)
            for finding in findings
            if finding.kind == spec.kind
        ]
        written[spec.kind] = append_rows(paths[spec.kind], spec.columns, rows)
    return written
