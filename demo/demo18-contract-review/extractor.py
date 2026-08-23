"""條款提取引擎 — Clause Comparison Engine 的 Step 1: Extract（附錄F p16）。

設計鐵律（三條，違反任一即為重大缺陷）：

1. **逐字引用（Quote verbatim）**：所有輸出的引文都是合約原文的精確子字串。
   本模組用「切片而非重組」的方式產生引文（`text[start:end]`），
   結構上就不可能改寫；`verify_verbatim()` 再做一次事後驗證當作保險絲。

2. **提取不到就說不到**：找不到條款就回報 `found=False`，
   絕不用「業界通常會有」補一條。信心不足一律標 `needs_human_review`。

3. **LLM 只能提議，不能決定**：`--live` 模式下 LLM 提出的引文一律回原文驗證，
   驗不過的直接丟棄並記 amber（比照 demo03 的承諾閘門與 demo06 的模糊掃描）。

為什麼不用 PDF/DOCX 函式庫：正式部署時由前置轉檔器把合約轉成本模組吃的純文字
結構（sections）。轉檔錯誤與提取錯誤混在一起會讓「引文對不上原文」變成無解懸案，
因此刻意讓本模組的輸入就是「已經是文字的真相」，責任邊界清楚。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = MODULE_DIR / "prompts" / "extract_clauses.md"

# 句子邊界：英文合約以 . ; : 收句，中文以 。；收句。用於把命中位置擴張成完整一句。
_SENTENCE_END = re.compile(r"[.;:。；]\s")

# 金額樣式：符號式（£1,234.56）與代碼式（GBP 1,234.56）兩種都要接。
_MONEY_PATTERNS: tuple[str, ...] = (
    r"[£$€]\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    r"\b(?:GBP|USD|EUR|TWD)\s?([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
)


@dataclass
class ContractSection:
    """合約的一個條號區塊。`text` 是唯一的引文真相來源。"""

    number: str
    heading: str
    text: str


@dataclass
class ContractDocument:
    """一份待審合約。金額欄位一律 Decimal，禁止 float（財務尾差會累積成爭議）。"""

    contract_id: str
    title: str
    counterparty: str
    jurisdiction: str
    currency: str
    annual_value: Decimal | None
    sections: list[ContractSection]
    received_via: str = "unknown"
    page_count: int = 0

    @property
    def full_text(self) -> str:
        """全文（含標題），供紅旗掃描與逐字驗證使用。"""
        return "\n\n".join(f"{s.number}. {s.heading}\n{s.text}" for s in self.sections)

    @property
    def content_hash(self) -> str:
        """全文 SHA256：合約改一個字就換一個雜湊，用於去重複警報。"""
        return hashlib.sha256(self.full_text.encode("utf-8")).hexdigest()

    def to_summary(self) -> dict[str, Any]:
        """給報表與測試斷言用的輕量摘要（不含全文，避免結果 dict 爆量）。"""
        return {
            "contract_id": self.contract_id,
            "title": self.title,
            "counterparty": self.counterparty,
            "jurisdiction": self.jurisdiction,
            "currency": self.currency,
            "annual_value": str(self.annual_value) if self.annual_value is not None else None,
            "received_via": self.received_via,
            "page_count": self.page_count,
            "section_count": len(self.sections),
            "content_hash": self.content_hash,
        }


@dataclass
class ExtractedClause:
    """單一條款的提取結果。`quote=None` 代表沒有通過逐字驗證，一律不輸出引文。"""

    clause_id: str
    name_en: str
    name_zh: str
    is_found: bool
    quote: str | None = None
    section_ref: str | None = None
    match_kind: str = "none"  # heading | body | none
    confidence: float = 0.0
    is_verbatim_verified: bool = False
    is_truncated: bool = False
    needs_human_review: bool = False
    review_reason: str = ""
    searched_pattern_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "clause_id": self.clause_id,
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "is_found": self.is_found,
            "quote": self.quote,
            "section_ref": self.section_ref,
            "match_kind": self.match_kind,
            "confidence": self.confidence,
            "is_verbatim_verified": self.is_verbatim_verified,
            "is_truncated": self.is_truncated,
            "needs_human_review": self.needs_human_review,
            "review_reason": self.review_reason,
            "searched_pattern_count": self.searched_pattern_count,
        }


class ContractFormatError(ValueError):
    """合約 JSON 結構不符（缺 sections、欄位型別錯誤等）。"""


def verify_verbatim(quote: str, source: str) -> bool:
    """引文是否為原文的精確子字串。空引文一律不算通過（不給模糊的好處）。"""
    return bool(quote) and quote in source


def parse_money(text: str) -> Decimal | None:
    """抓出文字中第一個金額並轉成 Decimal。抓不到或無法解析回 None。

    刻意只取「第一個」：條款裡出現多個金額時，哪個才是上限屬於法律判斷，
    系統若自行挑選就是在替律師做決定。取第一個並在報表標示，由人複核。
    """
    for pattern in _MONEY_PATTERNS:
        found = re.search(pattern, text)
        if not found:
            continue
        try:
            return Decimal(found.group(1).replace(",", ""))
        except InvalidOperation:
            return None
    return None


def _to_decimal(raw: Any) -> Decimal | None:
    """把設定檔或 JSON 的字串金額轉 Decimal；無法解析回 None 由呼叫端處理。"""
    if raw is None:
        return None
    try:
        return Decimal(str(raw).replace(",", "").strip())
    except InvalidOperation:
        return None


def load_contract(path: str | Path) -> ContractDocument:
    """讀取合約 JSON（純文字結構）。缺檔或格式錯誤一律拋例外，不靜默回空資料。"""
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到合約檔案：{file_path}")
    with file_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise ContractFormatError(f"合約格式錯誤（缺少 sections 陣列）：{file_path}")
    financials = data.get("financials") or {}
    return ContractDocument(
        contract_id=str(data.get("contract_id", "unknown")),
        title=str(data.get("title", "未命名合約")),
        counterparty=str(data.get("counterparty", "unknown")),
        jurisdiction=str(data.get("jurisdiction", "")),
        currency=str(financials.get("currency", "")),
        annual_value=_to_decimal(financials.get("annual_value")),
        sections=_build_sections(data["sections"], file_path),
        received_via=str(data.get("received_via", "unknown")),
        page_count=int(data.get("page_count", 0) or 0),
    )


def _build_sections(raw_sections: list[Any], file_path: Path) -> list[ContractSection]:
    """把 JSON 的 sections 轉成 dataclass；任何一段缺 text 就整份拒收。"""
    sections: list[ContractSection] = []
    for position, item in enumerate(raw_sections, start=1):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            raise ContractFormatError(f"合約第 {position} 段缺少 text 內容：{file_path}")
        sections.append(
            ContractSection(
                number=str(item.get("number", position)),
                heading=str(item.get("heading", "")),
                text=str(item["text"]),
            )
        )
    return sections


def sentence_span(text: str, start: int, end: int) -> tuple[int, int]:
    """把命中位置擴張到完整句子的起訖索引（回傳索引，由呼叫端切片保證逐字）。"""
    left = 0
    for boundary in _SENTENCE_END.finditer(text, 0, start):
        left = boundary.end()
    right_match = _SENTENCE_END.search(text, end)
    right = right_match.end() if right_match else len(text)
    return left, right


def truncate_at_sentence(text: str, limit: int) -> tuple[str, bool]:
    """超長引文以句子邊界截斷。回傳 (原文精確前綴, 是否被截斷)。"""
    if len(text) <= limit:
        return text, False
    window = text[:limit]
    boundaries = list(_SENTENCE_END.finditer(window))
    cut = boundaries[-1].end() if boundaries else limit
    return text[:cut], True


def _search_patterns(patterns: list[str], target: str) -> re.Match[str] | None:
    """回傳第一個命中的樣式。樣式本身寫壞（設定檔錯誤）一律拋出，不吞掉。"""
    for pattern in patterns:
        found = re.search(pattern, target)
        if found:
            return found
    return None


def locate_clause(
    document: ContractDocument, clause_def: dict[str, Any], quote_limit: int
) -> tuple[ContractSection, str, str] | None:
    """在合約中定位條款。回傳 (區塊, 引文, 命中方式)；找不到回 None。

    標題命中 → 取整條條款全文（附錄F：精準提取特定條款「全文」）。
    僅內文命中 → 只取命中的那一句，因為標題不符時無法確定整段都屬於這條條款。
    """
    heading_patterns = list(clause_def.get("heading_patterns") or [])
    for section in document.sections:
        if _search_patterns(heading_patterns, section.heading):
            quote, _ = truncate_at_sentence(section.text, quote_limit)
            return section, quote, "heading"
    body_patterns = list(clause_def.get("body_patterns") or [])
    for section in document.sections:
        found = _search_patterns(body_patterns, section.text)
        if found:
            left, right = sentence_span(section.text, found.start(), found.end())
            return section, section.text[left:right], "body"
    return None


class ClauseExtractor:
    """把一份合約轉成 14 條（依 CLAUSE_LIBRARY）逐字提取結果。"""

    def __init__(
        self,
        config: dict[str, Any],
        llm: Any,
        diagnostics: Any,
        prompt_path: str | Path | None = None,
    ) -> None:
        self._library: list[dict[str, Any]] = list(config.get("clause_library") or [])
        self._settings: dict[str, Any] = config.get("extraction") or {}
        self._llm_config: dict[str, Any] = config.get("llm") or {}
        self._llm = llm
        self._diagnostics = diagnostics
        self._prompt_path = Path(prompt_path) if prompt_path else DEFAULT_PROMPT_PATH

    @property
    def library(self) -> list[dict[str, Any]]:
        """CLAUSE_LIBRARY 原始定義（分類器需要 must_include / deviation_if）。"""
        return self._library

    def read_prompt(self) -> str:
        """提示詞是資產，獨立成檔。缺檔就明確報錯，不用內嵌字串偷偷頂替。"""
        if not self._prompt_path.is_file():
            raise FileNotFoundError(f"找不到提示詞檔案：{self._prompt_path}")
        return self._prompt_path.read_text(encoding="utf-8")

    def extract(self, document: ContractDocument) -> list[ExtractedClause]:
        """主流程：逐條定位 → 逐字驗證 → 信心評估 →（live 才跑）LLM 補提議。"""
        results = [self._extract_one(document, clause_def) for clause_def in self._library]
        self._apply_llm(document, results)
        return results

    def _extract_one(
        self, document: ContractDocument, clause_def: dict[str, Any]
    ) -> ExtractedClause:
        """定位單一條款並完成逐字驗證。找不到就誠實回報 is_found=False。"""
        clause = ExtractedClause(
            clause_id=str(clause_def.get("id", "unknown")),
            name_en=str(clause_def.get("name_en", "")),
            name_zh=str(clause_def.get("name_zh", "")),
            is_found=False,
            searched_pattern_count=len(clause_def.get("heading_patterns") or [])
            + len(clause_def.get("body_patterns") or []),
        )
        limit = int(self._settings.get("quote_max_chars", 800))
        located = locate_clause(document, clause_def, limit)
        if located is None:
            return clause
        section, quote, match_kind = located
        clause.is_found = True
        clause.section_ref = section.number
        clause.match_kind = match_kind
        clause.is_truncated = quote != section.text and match_kind == "heading"
        self._verify_and_score(clause, quote, section.text)
        return clause

    def _verify_and_score(self, clause: ExtractedClause, quote: str, source: str) -> None:
        """逐字驗證 + 信心評分。驗不過就不輸出引文（寧可空白也不給假引文）。"""
        if not verify_verbatim(quote, source):
            self._flag(
                clause,
                "提取結果無法通過逐字驗證，引文已丟棄；本條需人工翻閱原文確認",
                "檢查前置轉檔器是否改動了合約文字（空白、引號、連字號的正規化）",
            )
            return
        clause.quote = quote
        clause.is_verbatim_verified = True
        confidence_key = (
            "heading_match_confidence" if clause.match_kind == "heading" else "body_match_confidence"
        )
        clause.confidence = float(self._settings.get(confidence_key, 0.72))
        floor = float(self._settings.get("confidence_floor", 0.75))
        if clause.confidence < floor:
            self._flag(
                clause,
                f"僅由內文關鍵字命中（信心 {clause.confidence:.2f} < {floor:.2f}），"
                "無法確認引文涵蓋整條條款",
                "在 CLAUSE_LIBRARY 補上這份合約使用的條款標題寫法",
                keep_quote=True,
            )
        elif clause.is_truncated:
            self._flag(
                clause,
                f"條款全文超過 {self._settings.get('quote_max_chars', 800)} 字，"
                "引文為原文精確前綴，後段未納入比對",
                "調高 extraction.quote_max_chars 或請前置轉檔器拆分過長條款",
                keep_quote=True,
            )

    def _flag(
        self, clause: ExtractedClause, reason: str, fix: str, keep_quote: bool = False
    ) -> None:
        """標記需人工複核。預設連引文一起丟棄——沒驗過的文字不該出現在律師眼前。"""
        clause.needs_human_review = True
        clause.review_reason = reason
        if not keep_quote:
            clause.quote = None
            clause.is_verbatim_verified = False
            clause.confidence = 0.0
        self._diagnostics.amber(f"[{clause.clause_id}] {reason}", fix)

    def _apply_llm(self, document: ContractDocument, results: list[ExtractedClause]) -> None:
        """live 模式讓 LLM 補提議，但每則引文都要回原文驗證，驗不過一律丟棄。"""
        raw = self._llm.complete(
            system=self.read_prompt(),
            user=self._build_user_payload(document),
            max_tokens=int(self._llm_config.get("max_tokens", 3000)),
        )
        if str(raw).startswith("[MOCK]"):
            return
        payload = _safe_json(str(raw))
        if payload is None:
            self._diagnostics.amber(
                "LLM 回傳非 JSON，本輪僅採用確定性提取結果",
                "檢查 prompts/extract_clauses.md 的「輸出格式」段落是否被覆寫",
            )
            return
        dropped = self._merge_llm_clauses(document, results, payload.get("clauses") or [])
        if dropped:
            self._diagnostics.amber(
                f"丟棄 {dropped} 則無法在合約原文比對到的 LLM 引文",
                "提示詞需再次強調 quote 必須逐字複製，不得改寫或跨段拼接",
            )

    def _build_user_payload(self, document: ContractDocument) -> str:
        """送給 LLM 的輸入：管轄權 + 精簡版 library + 合約全文。"""
        return json.dumps(
            {
                "jurisdiction": document.jurisdiction,
                "clause_library": [
                    {
                        "clause_id": item.get("id"),
                        "name_en": item.get("name_en"),
                        "standard_position": item.get("standard_position"),
                    }
                    for item in self._library
                ],
                "contract": {
                    "contract_id": document.contract_id,
                    "sections": [
                        {"number": s.number, "heading": s.heading, "text": s.text}
                        for s in document.sections
                    ],
                },
            },
            ensure_ascii=False,
        )

    def _merge_llm_clauses(
        self,
        document: ContractDocument,
        results: list[ExtractedClause],
        llm_clauses: list[Any],
    ) -> int:
        """只補「確定性引擎沒找到」的條款，且引文必須通過逐字驗證。回傳丟棄則數。"""
        index = {item.clause_id: item for item in results}
        full_text = document.full_text
        dropped = 0
        for entry in llm_clauses:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            clause = index.get(str(entry.get("clause_id", "")))
            quote = str(entry.get("quote") or "")
            if clause is None or clause.is_found or not entry.get("found"):
                continue
            if not verify_verbatim(quote, full_text):
                dropped += 1
                continue
            self._accept_llm_clause(clause, entry, quote)
        return dropped

    def _accept_llm_clause(
        self, clause: ExtractedClause, entry: dict[str, Any], quote: str
    ) -> None:
        """採用一則已通過逐字驗證的 LLM 引文，並強制標記為需人工複核。"""
        clause.is_found = True
        clause.quote = quote
        clause.is_verbatim_verified = True
        clause.section_ref = str(entry.get("section_ref") or "")
        clause.match_kind = "llm"
        clause.confidence = float(self._settings.get("body_match_confidence", 0.72))
        clause.needs_human_review = True
        clause.review_reason = (
            "由語言模型定位、已通過逐字驗證，但未命中任何 CLAUSE_LIBRARY 樣式，需人工確認"
        )


def _safe_json(raw: str) -> dict[str, Any] | None:
    """LLM 回傳可能夾雜前後說明文字，取第一個 JSON 物件；解析失敗回 None。"""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
