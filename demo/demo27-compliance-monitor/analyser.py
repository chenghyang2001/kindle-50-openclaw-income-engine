"""demo27 — 法務文件與法規來源分析器。

三大監控來源（附錄G apxG_p15）各有一支分析函式：

* Regulatory（政府 RSS/API）→ 影響初篩，等級**只採用公告自述**，未載明就交人工
* Contracts（合約庫）        → 到期前 120 / 60 / 14 天三階段警告
* Policies（內部政策）       → 審查週期逾期旗標

⚠️ 法律免責：本模組的判定僅供合規團隊**初步篩選**，不構成法律意見。
到期日、義務與風險判定必須由合格法律專業人員確認。

設計鐵律（與 demo03 同源）：**逐字引用，絕不推論**。
來源沒寫的欄位一律留 None 並標 `needs_human_review`，不用商業慣例補完。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 到期/逾期階段代碼。"unknown" 代表算不出天數（缺日期或格式錯），與 "none" 意義不同。
STAGE_OVERDUE = "overdue"
STAGE_UNKNOWN = "unknown"
STAGE_NONE = "none"
STAGE_REGULATORY = "regulatory"

# 法規公告允許出現的自述等級；不在其中一律視為未載明
DECLARED_LEVELS: tuple[str, ...] = ("critical", "high", "standard")


class AnalyserError(RuntimeError):
    """來源檔缺失或結構不符（不是資料內容問題，是檔案本身讀不了）"""


def resolve_timezone(name: str, fallback_offset_hours: int) -> tuple[tzinfo, str | None]:
    """取得時區物件，回傳 (tzinfo, 警告訊息或 None)。

    Windows 預設沒有 IANA tzdata，`ZoneInfo("Asia/Taipei")` 會直接拋錯。
    到期日只差一天就可能是「已逾期」與「還有 14 天」的差別，
    因此降級為固定時差而非讓整條流程掛掉——但一定要發 amber 讓人看得見。
    """
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError) as exc:
        offset = timezone(timedelta(hours=fallback_offset_hours), name)
        return offset, (
            f"找不到時區資料 {name}（{exc}），已降級為固定 UTC{fallback_offset_hours:+d}；"
            "如需正確處理日光節約時間請安裝 tzdata 套件"
        )


def resolve_now(raw: str | None, tz: tzinfo) -> datetime:
    """決定「現在」：raw 有值就用它（測試/示範釘時），否則取系統時間。

    raw 若不帶時區資訊，一律視為設定檔時區的當地時間（不猜 UTC）。
    """
    if not raw:
        return datetime.now(tz)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise AnalyserError(f"--now / mock.frozen_now 不是 ISO 8601 時間：{raw!r}") from exc
    return parsed.astimezone(tz) if parsed.tzinfo else parsed.replace(tzinfo=tz)


@dataclass(frozen=True)
class AnalysisContext:
    """一次執行共用的判定基準（全部來自 config，禁止散落魔術數字）。"""

    today: date
    tz_name: str
    warning_days: tuple[int, ...]
    confidence_floor: float
    overdue_grace_days: int
    default_policy_cycle_days: int


@dataclass(frozen=True)
class Finding:
    """單一稽核發現。`evidence` 一律是來源條款原文，供台帳回溯。"""

    kind: str
    record_id: str
    title: str
    stage: str
    days: int | None
    needs_human_review: bool
    review_reasons: tuple[str, ...]
    evidence: str
    source_ref: str
    confidence: float | None
    owner: str
    declared_level: str | None = None
    amount: Decimal | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """去重狀態檔用的唯一鍵。"""
        return f"{self.kind}:{self.record_id}"


def load_source(path: Path, list_key: str) -> tuple[list[dict[str, Any]], str]:
    """讀取離線來源 JSON，回傳 (紀錄清單, 來源標記)。

    只吃純文字 JSON——本模組**不解析 PDF**，避免引入無法離線驗證的二進位解析依賴。
    """
    if not path.is_file():
        raise AnalyserError(f"找不到來源檔：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalyserError(f"來源檔讀取失敗：{path}｜{exc}") from exc
    records = payload.get(list_key)
    if not isinstance(records, list):
        raise AnalyserError(f"來源檔 {path} 缺少陣列欄位 {list_key!r}")
    marker = str(payload.get("source_system") or path.name)
    return [item for item in records if isinstance(item, dict)], marker


def warning_stage(days: int | None, warning_days: tuple[int, ...], grace_days: int) -> str:
    """把「剩餘天數」對應到 120 / 60 / 14 三階段警告代碼。"""
    if days is None:
        return STAGE_UNKNOWN
    if days < -abs(grace_days):
        return STAGE_OVERDUE
    for threshold in sorted(warning_days):
        if days <= threshold:
            return f"stage_{threshold}"
    return STAGE_NONE


def _safe_date(raw: Any) -> tuple[date | None, str | None]:
    """解析 ISO 日期。缺值或格式錯都不猜，回 (None, 原因)。"""
    if raw in (None, ""):
        return None, None
    try:
        return date.fromisoformat(str(raw)), None
    except ValueError:
        return None, f"日期不是 ISO 8601 格式：{raw!r}（不推算，交人工確認）"


def _safe_decimal(raw: Any) -> tuple[Decimal | None, str | None]:
    """金額一律走 Decimal，避免浮點誤差污染稽核台帳。"""
    if raw in (None, ""):
        return None, None
    try:
        return Decimal(str(raw)), None
    except (InvalidOperation, ValueError):
        return None, f"金額無法解析為 Decimal：{raw!r}"


def _confidence_reasons(record: dict[str, Any], ctx: AnalysisContext) -> list[str]:
    """信心值與來源自述備註造成的人工複核理由。"""
    reasons: list[str] = []
    raw = record.get("clause_confidence")
    if raw is None:
        reasons.append("來源未提供條款判讀信心值")
    else:
        try:
            confidence = float(raw)
        except (TypeError, ValueError):
            reasons.append(f"信心值無法解析：{raw!r}")
        else:
            if confidence < ctx.confidence_floor:
                reasons.append(
                    f"條款判讀信心 {confidence:.2f} 低於門檻 {ctx.confidence_floor:.2f}（條款看不懂，不猜）"
                )
    note = record.get("clause_note") or record.get("note")
    if note:
        reasons.append(str(note))
    return reasons


def _missing_field_reasons(record: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    """缺必要欄位一律列為人工複核理由，不填預設值。"""
    return [f"來源缺少必要欄位 {name}" for name in required if not record.get(name)]


def _confidence_of(record: dict[str, Any]) -> float | None:
    """取出信心值；解析不了就回 None（不填 0，0 會被誤讀成「確定不可信」）。"""
    try:
        return float(record["clause_confidence"])
    except (KeyError, TypeError, ValueError):
        return None


def _expiry_finding(
    record: dict[str, Any],
    ctx: AnalysisContext,
    kind: str,
    id_key: str,
    title_key: str,
    required: tuple[str, ...],
) -> Finding:
    """合約 / 執照共用的到期判定（兩者欄位結構一致，只差識別鍵）。"""
    reasons = _missing_field_reasons(record, required) + _confidence_reasons(record, ctx)
    expiry, date_reason = _safe_date(record.get("expiry_date"))
    if date_reason:
        reasons.append(date_reason)
    amount, amount_reason = _safe_decimal(record.get("annual_value"))
    if amount_reason:
        reasons.append(amount_reason)
    days = None if expiry is None else (expiry - ctx.today).days
    return Finding(
        kind=kind,
        record_id=str(record.get(id_key) or "（來源未提供識別碼）"),
        title=str(record.get(title_key) or "（來源未提供名稱）"),
        stage=warning_stage(days, ctx.warning_days, ctx.overdue_grace_days),
        days=days,
        needs_human_review=bool(reasons),
        review_reasons=tuple(reasons),
        evidence=str(record.get("renewal_clause") or "（來源未提供條款原文）"),
        source_ref="",
        confidence=_confidence_of(record),
        owner=str(record.get("owner") or "（來源未指定負責人）"),
        amount=amount,
        details={
            "expiry_date": expiry.isoformat() if expiry else "",
            "effective_date": str(record.get("effective_date") or ""),
            "issued_date": str(record.get("issued_date") or ""),
            "counterparty": str(record.get("counterparty") or ""),
            "contract_type": str(record.get("contract_type") or ""),
            "issuing_authority": str(record.get("issuing_authority") or ""),
            "jurisdiction": str(record.get("jurisdiction") or ""),
            "auto_renew": "" if record.get("auto_renew") is None else str(record["auto_renew"]),
            "notice_period_days": "" if record.get("notice_period_days") is None else str(record["notice_period_days"]),
            "currency": str(record.get("currency") or ""),
        },
    )


def _with_source(findings: list[Finding], marker: str) -> list[Finding]:
    """把來源標記寫進每筆 Finding（稽核台帳的「來源依據」欄）。"""
    return [replace(finding, source_ref=f"{marker}#{finding.record_id}") for finding in findings]


def analyse_contracts(records: list[dict[str, Any]], marker: str, ctx: AnalysisContext) -> list[Finding]:
    """合約庫：到期前 120 / 60 / 14 天三階段警告 + 條款可讀性旗標。"""
    findings = [
        _expiry_finding(record, ctx, "contract", "contract_id", "counterparty", ("expiry_date", "counterparty"))
        for record in records
    ]
    return _with_source(findings, marker)


def analyse_licences(records: list[dict[str, Any]], marker: str, ctx: AnalysisContext) -> list[Finding]:
    """執照庫：同樣走三階段警告，另外要求發證機關必須有值（否則不知道向誰送件）。"""
    findings = [
        _expiry_finding(
            record, ctx, "licence", "licence_id", "licence_name", ("expiry_date", "issuing_authority")
        )
        for record in records
    ]
    return _with_source(findings, marker)


def _policy_due_date(record: dict[str, Any], ctx: AnalysisContext) -> tuple[date | None, list[str]]:
    """算出下次應審查日；缺 last_reviewed 或週期未定義就回 None（不套用預設週期硬算）。"""
    reasons: list[str] = []
    last_reviewed, date_reason = _safe_date(record.get("last_reviewed"))
    if date_reason:
        reasons.append(date_reason)
    if last_reviewed is None:
        reasons.append("來源缺少 last_reviewed，無法計算下次審查日（不推估）")
        return None, reasons
    raw_cycle = record.get("review_cycle_days")
    if raw_cycle in (None, ""):
        reasons.append(f"來源未定義審查週期，僅供參考套用預設 {ctx.default_policy_cycle_days} 天")
        cycle = ctx.default_policy_cycle_days
    else:
        try:
            cycle = int(raw_cycle)
        except (TypeError, ValueError):
            reasons.append(f"審查週期無法解析為整數：{raw_cycle!r}")
            return None, reasons
    return last_reviewed + timedelta(days=cycle), reasons


def analyse_policies(records: list[dict[str, Any]], marker: str, ctx: AnalysisContext) -> list[Finding]:
    """內部政策：審查週期逾期旗標（Overdue Review Flagging）。"""
    findings: list[Finding] = []
    for record in records:
        due, reasons = _policy_due_date(record, ctx)
        reasons = _missing_field_reasons(record, ("policy_name",)) + reasons + _confidence_reasons(record, ctx)
        days = None if due is None else (due - ctx.today).days
        findings.append(
            Finding(
                kind="policy",
                record_id=str(record.get("policy_id") or "（來源未提供識別碼）"),
                title=str(record.get("policy_name") or "（來源未提供名稱）"),
                stage=warning_stage(days, ctx.warning_days, ctx.overdue_grace_days),
                days=days,
                needs_human_review=bool(reasons),
                review_reasons=tuple(reasons),
                evidence=str(record.get("review_clause") or "（來源未提供審查條款原文）"),
                source_ref="",
                confidence=_confidence_of(record),
                owner=str(record.get("owner") or "（來源未指定負責人）"),
                details={
                    "last_reviewed": str(record.get("last_reviewed") or ""),
                    "review_cycle_days": "" if record.get("review_cycle_days") is None else str(record["review_cycle_days"]),
                    "next_review_due": due.isoformat() if due else "",
                    "approval_body": str(record.get("approval_body") or ""),
                },
            )
        )
    return _with_source(findings, marker)


def _declared_level(record: dict[str, Any]) -> str | None:
    """只採用公告**自述**的影響等級；未載明或值不認得一律回 None（不自行判定）。"""
    raw = record.get("impact_level")
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value if value in DECLARED_LEVELS else None


def analyse_regulatory(records: list[dict[str, Any]], marker: str, ctx: AnalysisContext) -> list[Finding]:
    """法規來源：影響初篩。等級只採用公告自述，未載明就標 needs_human_review。"""
    findings: list[Finding] = []
    for record in records:
        declared = _declared_level(record)
        reasons = _missing_field_reasons(record, ("title", "authority"))
        if declared is None:
            reasons.append("公告未載明影響等級（或等級值不在允許清單），系統不自行判定")
        note = record.get("note")
        if note:
            reasons.append(str(note))
        findings.append(
            Finding(
                kind="regulatory",
                record_id=str(record.get("item_id") or "（來源未提供識別碼）"),
                title=str(record.get("title") or "（來源未提供標題）"),
                stage=STAGE_REGULATORY,
                days=None,
                needs_human_review=bool(reasons),
                review_reasons=tuple(reasons),
                evidence=str(record.get("excerpt") or "（來源未提供公告原文摘錄）"),
                source_ref="",
                confidence=None,
                owner=str(record.get("authority") or "（來源未提供主管機關）"),
                declared_level=declared,
                details={
                    "published_at": str(record.get("published_at") or ""),
                    "authority": str(record.get("authority") or ""),
                    "affects": ", ".join(str(item) for item in (record.get("affects") or [])),
                },
            )
        )
    return _with_source(findings, marker)


def at_risk_value(findings: list[Finding]) -> Decimal:
    """加總「已進入警告視窗 / 已逾期 / 到期日不明」合約的年度金額（Decimal，不用 float）。

    到期日不明也算在內：算不出來不等於沒有風險，反而是最該先看的一群。
    """
    total = Decimal("0")
    for finding in findings:
        if finding.amount is None or finding.stage in (STAGE_NONE, STAGE_REGULATORY):
            continue
        total += finding.amount
    return total
