"""demo24 — 結構化評分引擎（模組 #24）。

這裡是 `structured_criteria_only=true` 的實作落點，設計上刻意**不含任何模型呼叫**：
分數 100% 由 `config.yaml` 的加權條件矩陣以確定性程式算出，同一份履歷跑一百次
結果完全相同。模型只負責把既定結果寫成人話（面試題、拒絕信），不參與計分。

為什麼評分要離開模型：
就業歧視訴訟的攻防點是「你憑什麼刷掉我」。若分數來自模型的自由判斷，
就無法在法庭上重現當時的計算；把評分寫成可讀的權重表，才有辦法逐項舉證。

權重乘數（ch07_p07 逐字）：Must-have **x3**、Preferred **x1**、Behavioural **x2**。
分數 = Σ(權重 × 命中比例) ÷ Σ(權重) × 100。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

DECISION_DISQUALIFIED = "disqualified"
DECISION_SHORTLIST = "shortlist"
DECISION_HOLD = "hold"
DECISION_REJECT = "warm_rejection"

# Disqualifier 只允許這三種規則型別。刻意不做成通用運算式：
# 一旦支援任意運算式，篩選規則就會長成沒人看得懂的迷你 DSL，稽核時無法解釋。
_SUPPORTED_RULES = ("is_false", "less_than", "missing")


class ScoringError(RuntimeError):
    """評分設定不合法（權重缺漏、規則型別未支援、條件為空等）。"""


@dataclass(frozen=True)
class Criterion:
    """單一評分條件。keywords 全部以小寫比對。"""

    criterion_id: str
    label: str
    category: str
    keywords: tuple[str, ...]
    required_hits: int
    weight: int


@dataclass(frozen=True)
class CriterionScore:
    """單一條件的評分結果，保留命中的關鍵字供舉證。"""

    criterion_id: str
    label: str
    category: str
    weight: int
    required_hits: int
    hits: tuple[str, ...]
    ratio: float
    points: float

    def to_dict(self) -> dict[str, Any]:
        """序列化（含 hits，這是「憑什麼給分」的唯一證據）。"""
        return {
            "criterion_id": self.criterion_id,
            "label": self.label,
            "category": self.category,
            "weight": self.weight,
            "required_hits": self.required_hits,
            "hits": list(self.hits),
            "ratio": round(self.ratio, 4),
            "points": round(self.points, 4),
        }


@dataclass(frozen=True)
class CandidateScore:
    """單一候選人的完整評分結果（只帶匿名識別碼，不含任何身分資訊）。"""

    identifier: str
    total_score: float
    decision: str
    criteria: tuple[CriterionScore, ...]
    disqualifiers: tuple[str, ...]

    @property
    def is_disqualified(self) -> bool:
        """是否命中 disqualifier（命中即不進入排名）。"""
        return bool(self.disqualifiers)

    def strengths(self) -> tuple[str, ...]:
        """有命中證據的條件標籤（拒絕信只能引用這裡面的內容，不得虛構）。"""
        return tuple(item.label for item in self.criteria if item.hits)

    def gaps(self) -> tuple[str, ...]:
        """未達 required_hits 的條件標籤（面試題的第 2 題由此產生）。"""
        return tuple(item.label for item in self.criteria if item.ratio < 1.0)

    def to_dict(self) -> dict[str, Any]:
        """序列化成可寫進報表 / 稽核日誌的結構。"""
        return {
            "identifier": self.identifier,
            "total_score": round(self.total_score, 2),
            "decision": self.decision,
            "disqualifiers": list(self.disqualifiers),
            "criteria": [item.to_dict() for item in self.criteria],
            "strengths": list(self.strengths()),
            "gaps": list(self.gaps()),
        }


def build_criteria(config: dict[str, Any]) -> tuple[Criterion, ...]:
    """把 config 的三類條件攤平成一份權重已解析的條件清單。"""
    scoring = config.get("scoring") or {}
    multipliers = scoring.get("weight_multipliers") or {}
    groups = scoring.get("criteria") or {}
    criteria: list[Criterion] = []
    for category, entries in groups.items():
        weight = multipliers.get(category)
        if not isinstance(weight, int) or weight <= 0:
            raise ScoringError(f"scoring.weight_multipliers 缺少 {category} 的正整數權重")
        criteria.extend(_build_group(category, int(weight), entries or []))
    if not criteria:
        raise ScoringError("scoring.criteria 為空：沒有條件就沒有可稽核的評分依據")
    return tuple(criteria)


def _build_group(category: str, weight: int, entries: Iterable[dict[str, Any]]) -> list[Criterion]:
    """建立單一類別（must_have / preferred / behavioural）底下的所有條件。"""
    built: list[Criterion] = []
    for entry in entries:
        keywords = tuple(str(word).lower() for word in entry.get("keywords") or ())
        required = int(entry.get("required_hits", 1))
        if not keywords or required <= 0:
            raise ScoringError(f"條件 {entry.get('id')!r} 缺少 keywords 或 required_hits 不合法")
        built.append(
            Criterion(
                criterion_id=str(entry.get("id", "")),
                label=str(entry.get("label", entry.get("id", ""))),
                category=category,
                keywords=keywords,
                required_hits=required,
                weight=weight,
            )
        )
    return built


def criteria_fingerprint(config: dict[str, Any]) -> str:
    """條件矩陣的指紋（sha256 前 12 碼）。

    寫進稽核日誌後，日後可以證明「這批人是用哪一版規則篩的」——
    規則被偷改過卻沒人發現，是招募稽核最常見的破口。
    """
    scoring = config.get("scoring") or {}
    payload = json.dumps(
        {
            "weight_multipliers": scoring.get("weight_multipliers"),
            "criteria": scoring.get("criteria"),
            "thresholds": scoring.get("thresholds"),
            "disqualifiers": scoring.get("disqualifiers"),
            "evidence_fields": scoring.get("evidence_fields"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def evidence_text(fields: dict[str, Any], evidence_fields: Sequence[str]) -> str:
    """把允許參與評分的欄位串成一段小寫文字。未列出的欄位一律讀不到。"""
    chunks: list[str] = []
    for name in evidence_fields:
        value = fields.get(name)
        if isinstance(value, str):
            chunks.append(value)
        elif isinstance(value, list):
            chunks.extend(str(item) for item in value)
    return " \n".join(chunks).lower()


def score_criterion(criterion: Criterion, text: str) -> CriterionScore:
    """單一條件計分：命中比例 = 命中關鍵字數 ÷ required_hits，上限 1.0。"""
    hits = tuple(word for word in criterion.keywords if word in text)
    ratio = min(1.0, len(hits) / criterion.required_hits)
    return CriterionScore(
        criterion_id=criterion.criterion_id,
        label=criterion.label,
        category=criterion.category,
        weight=criterion.weight,
        required_hits=criterion.required_hits,
        hits=hits,
        ratio=ratio,
        points=criterion.weight * ratio,
    )


def check_disqualifiers(fields: dict[str, Any], rules: Iterable[dict[str, Any]]) -> tuple[str, ...]:
    """回傳命中的 disqualifier 標籤。規則型別只支援 _SUPPORTED_RULES 三種。"""
    hit: list[str] = []
    for rule in rules:
        name = str(rule.get("field", ""))
        kind = str(rule.get("rule", ""))
        if kind not in _SUPPORTED_RULES:
            raise ScoringError(f"不支援的 disqualifier 規則 {kind!r}，可用：{_SUPPORTED_RULES}")
        if _rule_hits(kind, fields.get(name), rule.get("value")):
            hit.append(str(rule.get("label", rule.get("id", name))))
    return tuple(hit)


def _rule_hits(kind: str, value: Any, threshold: Any) -> bool:
    """單一規則判定。缺欄位一律視為命中——資料不全時寧可擋下來人工看。"""
    if kind == "missing":
        return value is None or value == ""
    if value is None:
        return True
    if kind == "is_false":
        return value is False
    # less_than：非數值一律視為命中（欄位型別不對就不該讓它默默通過）
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return True
    return float(value) < float(threshold)


def decide(total_score: float, disqualifiers: Sequence[str], thresholds: dict[str, Any]) -> str:
    """依 SPEC #24 的三分支（外加中段保留）決定處置。"""
    if disqualifiers:
        return DECISION_DISQUALIFIED
    if total_score > float(thresholds.get("shortlist_min", 75)):
        return DECISION_SHORTLIST
    if total_score < float(thresholds.get("rejection_max", 40)):
        return DECISION_REJECT
    return DECISION_HOLD


def score_candidate(
    identifier: str, fields: dict[str, Any], criteria: tuple[Criterion, ...], config: dict[str, Any]
) -> CandidateScore:
    """對單一份匿名申請計分。輸入必須已完成匿名化（呼叫端負責保證）。"""
    scoring = config.get("scoring") or {}
    text = evidence_text(fields, scoring.get("evidence_fields") or ())
    scored = tuple(score_criterion(item, text) for item in criteria)
    total_weight = sum(item.weight for item in criteria)
    if total_weight <= 0:
        raise ScoringError("條件權重總和為 0，無法正規化分數")
    total = sum(item.points for item in scored) / total_weight * 100
    disqualifiers = check_disqualifiers(fields, scoring.get("disqualifiers") or ())
    return CandidateScore(
        identifier=identifier,
        total_score=round(total, 2),
        decision=decide(round(total, 2), disqualifiers, scoring.get("thresholds") or {}),
        criteria=scored,
        disqualifiers=disqualifiers,
    )


def score_all(
    anonymised: Iterable[Any], config: dict[str, Any]
) -> list[CandidateScore]:
    """批次計分（輸入為 AnonymisedApplication）。回傳順序與輸入一致。"""
    criteria = build_criteria(config)
    return [score_candidate(item.identifier, item.fields, criteria, config) for item in anonymised]


def rank(scores: Iterable[CandidateScore]) -> list[CandidateScore]:
    """排名：分數高者在前，同分則以識別碼排序（確保結果可重現）。"""
    return sorted(scores, key=lambda item: (-item.total_score, item.identifier))


def select_video_interviews(
    scores: Sequence[CandidateScore], received_count: int, config: dict[str, Any]
) -> tuple[str, ...]:
    """挑出要發非同步影片面試邀請的識別碼（ch07_p07：前 20%）。

    分母預設為 `received`（全體申請人），與書中「前 20% 的申請者」一致；
    設成 `scored` 則改以扣除 disqualified 後的人數計算，名額會變少。
    只有已進入 shortlist 的人才會拿到邀請——前 20% 是名額上限，不是保送門票。
    """
    settings = config.get("video_interview") or {}
    percent = float(settings.get("top_percent", 20))
    shortlisted = [item for item in rank(scores) if item.decision == DECISION_SHORTLIST]
    basis = str(settings.get("percent_basis", "received"))
    pool = received_count if basis == "received" else len([s for s in scores if not s.is_disqualified])
    quota = max(1, math.ceil(pool * percent / 100)) if pool else 0
    return tuple(item.identifier for item in shortlisted[:quota])


def format_shortlist(scores: Sequence[CandidateScore], invited: Sequence[str]) -> list[str]:
    """短名單呈現：`shortlist_presentation=identifiers_only`，只出現匿名識別碼。"""
    shortlisted = [item for item in rank(scores) if item.decision == DECISION_SHORTLIST]
    lines: list[str] = []
    for index, item in enumerate(shortlisted, start=1):
        mark = "影片面試邀請" if item.identifier in invited else "候補（未進前 20%）"
        lines.append(f"{index}. {item.identifier}｜{item.total_score:.2f} 分｜{mark}")
    return lines
