"""條款比對與風險升級 — Clause Comparison Engine 的 Step 2 / 四分類 / Step 4（附錄F p16）。

引擎四步驟（原圖標示為 Step 1 / Step 2 / Step 4，四分類方塊未編號，如實照做）：

    Step 1  Extract  逐字提取（在 extractor.py）
    Step 2  Compare  與 CLAUSE_LIBRARY 的標準立場做逐字與語義比對
    （四分類）Standard / Deviation / Missing / Red Flag
    Step 4  Action   紅旗繞過常規備忘錄，直送資深合夥人警報

分類的安全不對稱（本檔最重要的設計決策）：
四個分類裡只有 **Standard 是「放行」**，其餘三個都會把案件推回人類手上。
因此「不確定」時絕不可以判 Standard——那是唯一會造成假性安心的錯誤。
不確定一律依 `review.unresolved_verdict`（預設 Deviation）降級，並標 needs_human_review。
少判一條 Standard 只是多花律師五分鐘；多判一條 Standard 可能讓公司簽下無上限責任。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from extractor import ContractDocument, ExtractedClause, parse_money


class Verdict(Enum):
    """附錄F p16 的四分類。**只有這四種，不得增設第五種。**"""

    STANDARD = "Standard"
    DEVIATION = "Deviation"
    MISSING = "Missing"
    RED_FLAG = "Red Flag"


@dataclass
class RedFlagHit:
    """一次硬性紅線命中。`matched_text` 是原文逐字切片，非改寫。"""

    rule_id: str
    label_en: str
    label_zh: str
    why: str
    section_ref: str
    section_heading: str
    matched_text: str
    escalate_to: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "label_en": self.label_en,
            "label_zh": self.label_zh,
            "why": self.why,
            "section_ref": self.section_ref,
            "section_heading": self.section_heading,
            "matched_text": self.matched_text,
            "escalate_to": self.escalate_to,
        }


@dataclass
class Finding:
    """一項偏離發現：風險說明 + 建議替代字詞（附錄F：Deviation 要提供替代字詞）。"""

    note: str
    suggested_wording: str

    def to_dict(self) -> dict[str, Any]:
        return {"note": self.note, "suggested_wording": self.suggested_wording}


@dataclass
class ClauseAssessment:
    """單一條款的最終判定。`quote` 只可能是原文逐字切片或 None。"""

    clause_id: str
    name_en: str
    name_zh: str
    verdict: Verdict
    standard_position: str
    quote: str | None
    section_ref: str | None
    confidence: float
    is_verbatim_verified: bool
    needs_human_review: bool
    review_reason: str
    findings: list[Finding] = field(default_factory=list)
    red_flag_rule_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "verdict": self.verdict.value,
            "standard_position": self.standard_position,
            "quote": self.quote,
            "section_ref": self.section_ref,
            "confidence": self.confidence,
            "is_verbatim_verified": self.is_verbatim_verified,
            "needs_human_review": self.needs_human_review,
            "review_reason": self.review_reason,
            "findings": [item.to_dict() for item in self.findings],
            "red_flag_rule_id": self.red_flag_rule_id,
        }


@dataclass
class ReviewResult:
    """一份合約的完整審查結果。"""

    contract_id: str
    jurisdiction: str
    assessments: list[ClauseAssessment]
    red_flags: list[RedFlagHit]
    warnings: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """四分類計數（鍵名即分類原文，方便直接進報表）。"""
        tally = {verdict.value: 0 for verdict in Verdict}
        for item in self.assessments:
            tally[item.verdict.value] += 1
        return tally

    @property
    def needs_review_count(self) -> int:
        return sum(1 for item in self.assessments if item.needs_human_review)

    @property
    def has_red_flag(self) -> bool:
        return bool(self.red_flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "jurisdiction": self.jurisdiction,
            "assessments": [item.to_dict() for item in self.assessments],
            "red_flags": [item.to_dict() for item in self.red_flags],
            "counts": self.counts,
            "needs_review_count": self.needs_review_count,
            "has_red_flag": self.has_red_flag,
            "warnings": list(self.warnings),
        }


class JurisdictionError(ValueError):
    """管轄權未設定或不在支援清單（沒有管轄權就沒有比對基準）。"""


def check_jurisdiction(document: ContractDocument, config: dict[str, Any]) -> None:
    """比對前置條件：合約管轄權必須與已配置的 JURISDICTION 一致且受支援。"""
    setting = config.get("jurisdiction") or {}
    configured = str(setting.get("code", "")).strip()
    supported = {str(item) for item in setting.get("supported") or []}
    if not configured:
        raise JurisdictionError("未配置 jurisdiction.code，條款比對基準不成立")
    if configured not in supported:
        raise JurisdictionError(f"jurisdiction.code={configured!r} 不在 supported 清單內")
    if document.jurisdiction and document.jurisdiction != configured:
        raise JurisdictionError(
            f"合約管轄權 {document.jurisdiction!r} 與配置的 {configured!r} 不同，"
            "必須先切換 CLAUSE_LIBRARY 基準立場才能比對"
        )


def scan_red_flags(
    document: ContractDocument, rules: list[dict[str, Any]]
) -> list[RedFlagHit]:
    """逐段掃描 RISK_ESCALATION_RULES。同一規則只回報第一次命中，避免重複轟炸。"""
    hits: list[RedFlagHit] = []
    seen: set[str] = set()
    for rule in rules:
        rule_id = str(rule.get("id", "unknown"))
        for section in document.sections:
            found = _first_match(list(rule.get("trigger_immediately_if") or []), section.text)
            if found is None or rule_id in seen:
                continue
            seen.add(rule_id)
            hits.append(
                RedFlagHit(
                    rule_id=rule_id,
                    label_en=str(rule.get("label_en", "")),
                    label_zh=str(rule.get("label_zh", "")),
                    why=str(rule.get("why", "")),
                    section_ref=section.number,
                    section_heading=section.heading,
                    # 逐字切片：紅旗警報引用的一樣是原文，不是規則名稱的轉述
                    matched_text=section.text[found.start() : found.end()],
                    escalate_to=str(rule.get("escalate_to", "senior_partner")),
                )
            )
    return hits


def _first_match(patterns: list[str], target: str) -> re.Match[str] | None:
    """回傳第一個命中的樣式；樣式寫壞（設定檔錯誤）直接拋出，不吞掉。"""
    for pattern in patterns:
        found = re.search(pattern, target)
        if found:
            return found
    return None


def _missing_requirements(clause_def: dict[str, Any], quote: str) -> list[Finding]:
    """檢查條款是否具備 must_include 的保護要素；缺任一即為偏離。"""
    findings: list[Finding] = []
    for pattern in clause_def.get("must_include") or []:
        if not re.search(pattern, quote):
            findings.append(
                Finding(
                    note=f"條款未包含基準立場要求的要素（樣式 {pattern}）。",
                    suggested_wording=str(clause_def.get("standard_position", "")),
                )
            )
    return findings


def _pattern_deviations(clause_def: dict[str, Any], quote: str) -> list[Finding]:
    """比對 deviation_if 樣式，命中即產生一項含替代字詞的發現。"""
    findings: list[Finding] = []
    for entry in clause_def.get("deviation_if") or []:
        if not isinstance(entry, dict):
            continue
        if re.search(str(entry.get("pattern", "")), quote):
            findings.append(
                Finding(
                    note=str(entry.get("note", "偏離基準立場")),
                    suggested_wording=str(entry.get("suggested_wording", "")),
                )
            )
    return findings


def _amount_deviation(
    clause_def: dict[str, Any], quote: str, document: ContractDocument
) -> Finding | None:
    """金額型基準比對。金額一律走 Decimal，全程禁止 float（尾差會變成爭議）。"""
    policy = clause_def.get("amount_policy") or {}
    kind = str(policy.get("kind", ""))
    threshold = _policy_decimal(policy.get("value"))
    actual = parse_money(quote)
    if not kind or threshold is None or actual is None:
        return None
    limit = _resolve_threshold(kind, threshold, document)
    if limit is None:
        return None
    is_deviation = actual > limit if kind == "max_multiple_of_annual_value" else actual < limit
    if not is_deviation:
        return None
    return Finding(
        note=f"{policy.get('note', '金額偏離基準')}（合約：{actual}，基準：{limit}）",
        suggested_wording=str(policy.get("suggested_wording", "")),
    )


def _policy_decimal(raw: Any) -> Decimal | None:
    """設定檔的金額／倍數字串轉 Decimal；寫壞就回 None 由呼叫端跳過該項比對。"""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _resolve_threshold(
    kind: str, threshold: Decimal, document: ContractDocument
) -> Decimal | None:
    """把基準換算成可比的絕對金額。倍數型需要年度合約金額，缺就放棄比對。"""
    if kind == "min_amount":
        return threshold
    if kind == "max_multiple_of_annual_value":
        return None if document.annual_value is None else document.annual_value * threshold
    return None


class ClauseClassifier:
    """把逐字提取結果轉成四分類判定 + 紅旗升級清單。"""

    def __init__(self, config: dict[str, Any], diagnostics: Any) -> None:
        self._config = config
        self._library: list[dict[str, Any]] = list(config.get("clause_library") or [])
        self._rules: list[dict[str, Any]] = list(config.get("risk_escalation_rules") or [])
        self._review: dict[str, Any] = config.get("review") or {}
        self._diagnostics = diagnostics

    def classify(
        self, document: ContractDocument, extracted: list[ExtractedClause]
    ) -> ReviewResult:
        """Step 2 + 四分類 + 紅旗掃描。呼叫前必須已通過 check_jurisdiction。"""
        red_flags = scan_red_flags(document, self._rules)
        flagged_sections = {hit.section_ref for hit in red_flags}
        rule_by_section = {hit.section_ref: hit for hit in red_flags}
        definitions = {str(item.get("id")): item for item in self._library}
        assessments = [
            self._assess(clause, definitions.get(clause.clause_id, {}), document,
                         flagged_sections, rule_by_section)
            for clause in extracted
        ]
        result = ReviewResult(
            contract_id=document.contract_id,
            jurisdiction=document.jurisdiction,
            assessments=assessments,
            red_flags=red_flags,
        )
        self._collect_warnings(result)
        return result

    def _assess(
        self,
        clause: ExtractedClause,
        clause_def: dict[str, Any],
        document: ContractDocument,
        flagged_sections: set[str],
        rule_by_section: dict[str, RedFlagHit],
    ) -> ClauseAssessment:
        """單一條款四選一。順序：Red Flag > Missing > Deviation > Standard。"""
        assessment = ClauseAssessment(
            clause_id=clause.clause_id,
            name_en=clause.name_en,
            name_zh=clause.name_zh,
            verdict=Verdict.MISSING,
            standard_position=str(clause_def.get("standard_position", "")),
            quote=clause.quote,
            section_ref=clause.section_ref,
            confidence=clause.confidence,
            is_verbatim_verified=clause.is_verbatim_verified,
            needs_human_review=clause.needs_human_review,
            review_reason=clause.review_reason,
        )
        if clause.section_ref in flagged_sections:
            return self._as_red_flag(assessment, rule_by_section[clause.section_ref])
        if not clause.is_found:
            assessment.findings.append(_missing_finding(clause_def))
            return assessment
        if clause.quote is None:
            return self._as_unresolved(assessment)
        return self._compare(assessment, clause_def, clause.quote, document)

    def _as_red_flag(self, assessment: ClauseAssessment, hit: RedFlagHit) -> ClauseAssessment:
        """紅旗判定：命中硬性紅線，一切其他比對結果都不再重要。"""
        assessment.verdict = Verdict.RED_FLAG
        assessment.red_flag_rule_id = hit.rule_id
        assessment.findings.append(
            Finding(
                note=f"命中硬性紅線「{hit.label_zh}（{hit.label_en}）」：{hit.why}",
                suggested_wording="本項不進行字詞協商，退回重擬或終止談判由資深合夥人決定。",
            )
        )
        return assessment

    def _as_unresolved(self, assessment: ClauseAssessment) -> ClauseAssessment:
        """條款存在但引文未通過逐字驗證：**絕不可判 Standard**，一律降級。"""
        assessment.verdict = Verdict(str(self._review.get("unresolved_verdict", "Deviation")))
        assessment.needs_human_review = True
        assessment.findings.append(
            Finding(
                note="條款存在，但提取結果未通過逐字驗證，無法確認是否符合基準立場。"
                "系統不猜測，已降級為需人工判讀。",
                suggested_wording="請人工翻閱原文條款後手動判定。",
            )
        )
        return assessment

    def _compare(
        self,
        assessment: ClauseAssessment,
        clause_def: dict[str, Any],
        quote: str,
        document: ContractDocument,
    ) -> ClauseAssessment:
        """Step 2 Compare：逐字（樣式）＋金額基準比對，決定 Standard 或 Deviation。"""
        findings = _missing_requirements(clause_def, quote) + _pattern_deviations(clause_def, quote)
        amount_finding = _amount_deviation(clause_def, quote, document)
        if amount_finding is not None:
            findings.append(amount_finding)
        assessment.findings.extend(findings)
        if findings:
            assessment.verdict = Verdict.DEVIATION
            return assessment
        if assessment.needs_human_review:
            # 引文驗過但信心不足（僅內文命中／被截斷）→ 不給 Standard 這個放行印章
            return self._as_unresolved(assessment)
        assessment.verdict = Verdict.STANDARD
        return assessment

    def _collect_warnings(self, result: ReviewResult) -> None:
        """把需要人工注意的狀況同時寫進 warnings 與 diagnostics（雙寫）。"""
        counts = result.counts
        if counts[Verdict.MISSING.value]:
            self._warn(
                result,
                f"{counts[Verdict.MISSING.value]} 條關鍵保護條款在合約中完全缺失",
                "以 CLAUSE_LIBRARY 的標準條款文字向對方提出增補",
            )
        if result.needs_review_count:
            self._warn(
                result,
                f"{result.needs_review_count} 條無法由系統確認，已標記需人工判讀",
                "人工翻閱原文條款；勿將未確認項目視為通過",
            )
        for hit in result.red_flags:
            self._warn(
                result,
                f"紅旗：{hit.label_zh}（第 {hit.section_ref} 條 {hit.section_heading}）",
                "已繞過常規備忘錄，直送資深合夥人警報",
            )

    def _warn(self, result: ReviewResult, symptom: str, fix: str) -> None:
        """警示雙寫：warnings 給備忘錄讀者，amber 給營運監控。"""
        result.warnings.append(symptom)
        self._diagnostics.amber(symptom, fix)


def _missing_finding(clause_def: dict[str, Any]) -> Finding:
    """缺失條款的發現內容：直接給出應補上的標準立場。"""
    return Finding(
        note="合約全文中找不到本條款的任何對應文字（標題與內文樣式皆未命中）。",
        suggested_wording=str(clause_def.get("standard_position", "請補上本條款")),
    )


def build_escalations(
    result: ReviewResult, config: dict[str, Any], already_alerted: bool = False
) -> list[dict[str, Any]]:
    """Step 4 Action：紅旗繞過常規備忘錄，直送資深合夥人。

    `already_alerted` 來自狀態檔：同一份合約（相同全文雜湊）已警報過就不重複發送，
    但仍會保留在回傳結果中標示 is_suppressed，讓稽核看得到「當時判定過紅旗」。
    """
    settings = config.get("escalation") or {}
    recipient = str(settings.get("senior_partner", ""))
    is_suppressed = already_alerted and bool(settings.get("suppress_duplicate_alert", True))
    return [
        {
            "rule_id": hit.rule_id,
            "label_zh": hit.label_zh,
            "recipient": recipient,
            "section_ref": hit.section_ref,
            "matched_text": hit.matched_text,
            "bypass_memo": bool(settings.get("bypass_memo_on_red_flag", True)),
            "is_suppressed": is_suppressed,
        }
        for hit in result.red_flags
    ]
