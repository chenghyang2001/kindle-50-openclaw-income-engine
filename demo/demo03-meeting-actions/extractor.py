"""會議逐字稿 → 摘要 / 決策 / 行動項目 的提取引擎（第 03 章）。

設計鐵律：行動項目只能來自「明確陳述的承諾」。
確定性的承諾閘門（detect_commitments）在 mock 與 live 兩種模式都是唯一權威，
LLM 只負責摘要與決策敘述；LLM 額外提出的行動項目若在逐字稿找不到
對應的承諾原句，一律丟棄並記 amber。

為什麼不讓模型自由發揮：書中實測顯示，容許模糊推論會把「之後可能要做」
寫成承諾，準確率從 85-92% 掉到 60% 以下——客戶對這套系統的信任只要壞一次
就再也回不來，漏一項的代價遠小於捏造一項。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = MODULE_DIR / "prompts" / "extract.md"

# 模糊語氣：命中任一即整句否決，優先於所有承諾樣式
HEDGE_PATTERNS: tuple[str, ...] = (
    r"\bmaybe\b",
    r"\bperhaps\b",
    r"\bprobably\b",
    r"\bmight\b",
    r"\bsomeone\b",
    r"\bwe\s+should\b",
    r"\bit'?d\s+be\s+(?:nice|good|useful)\b",
    r"\bif\s+(?:i|we)\s+have\s+time\b",
    r"\bat\s+some\s+point\b",
    r"或許",
    r"也許",
    r"可能",
    r"有人",
    r"有空",
    r"再看看",
    r"再說",
    r"盡量",
)

# 第一人稱承諾 → 負責人 = 說話者
FIRST_PERSON_PATTERNS: tuple[str, ...] = (
    r"\bi\s+will\b",
    r"\bi'?ll\b",
    r"\bi\s+am\s+going\s+to\b",
    r"\bi'?m\s+going\s+to\b",
    r"\bi\s+(?:can|shall)\s+(?:take|handle|own|do|send|prepare)\b",
    r"我來",
    r"我會",
    r"我負責",
    r"我去",
    r"我處理",
)

# 直接請求 → 負責人 = 句中被指名的人（找不到就是 None）
REQUEST_PATTERNS: tuple[str, ...] = (
    r"\bcan\s+you\b",
    r"\bcould\s+you\b",
    r"\bwill\s+you\b",
    r"\bwould\s+you\b",
    r"\bplease\s+(?:can|could|would)\b",
    r"請你",
    r"請幫",
    r"麻煩你",
    r"你來",
)

# 團體承諾 → 明確但未指名，owner 必須是 None
GROUP_PATTERNS: tuple[str, ...] = (
    r"\bwe\s+will\b",
    r"\bwe'?ll\b",
    r"\bwe\s+are\s+going\s+to\b",
    r"我們會",
    r"我們來",
)

DECISION_PATTERNS: tuple[str, ...] = (
    r"\bdecision\s*:",
    r"\bwe\s+(?:have\s+)?decided\b",
    r"\bwe\s+agreed\b",
    r"決定",
    r"拍板",
    r"結論是",
)

# 期限只採用逐字稿出現的時間詞，不做日期推算（跨時區推算錯了比沒有更糟）
DUE_PATTERNS: tuple[str, ...] = (
    r"\b(?:by|before|until)\s+"
    r"((?:the\s+)?end\s+of\s+(?:the\s+)?\w+|eod|cob|next\s+\w+|this\s+\w+|\w+day|\d{1,2}(?:st|nd|rd|th)?\s+\w+)",
    r"\b(this\s+(?:morning|afternoon|evening|week)|tomorrow|today)\b",
    r"((?:今天|明天|後天|本週|下週|下星期|月底|週末)前?)",
    r"((?:週|周|星期)[一二三四五六日天]前?)",
)

BASE_CONFIDENCE: dict[str, float] = {
    "first_person": 0.92,
    "request_named": 0.85,
    "request_unassigned": 0.55,
    "group": 0.50,
}


@dataclass
class ActionItem:
    """單一行動項目。owner=None 代表逐字稿沒有指名，系統不猜測。"""

    text: str
    owner: str | None
    speaker: str
    line: int
    kind: str
    commitment_phrase: str
    due_hint: str | None
    confidence: float

    @property
    def is_unassigned(self) -> bool:
        return self.owner is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "owner": self.owner,
            "speaker": self.speaker,
            "line": self.line,
            "kind": self.kind,
            "commitment_phrase": self.commitment_phrase,
            "due_hint": self.due_hint,
            "confidence": self.confidence,
        }


@dataclass
class ExtractionResult:
    """一場會議的完整提取結果。"""

    transcript_id: str
    title: str
    summary: str
    decisions: list[str]
    action_items: list[ActionItem]
    quality: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    llm_note: str = ""

    @property
    def unassigned_count(self) -> int:
        return sum(1 for item in self.action_items if item.is_unassigned)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transcript_id": self.transcript_id,
            "title": self.title,
            "summary": self.summary,
            "decisions": list(self.decisions),
            "action_items": [item.to_dict() for item in self.action_items],
            "unassigned_count": self.unassigned_count,
            "quality": dict(self.quality),
            "warnings": list(self.warnings),
            "llm_note": self.llm_note,
        }


def load_transcript(path: str | Path) -> dict[str, Any]:
    """讀取逐字稿 JSON。缺檔或格式錯誤一律拋例外，不靜默回傳空資料。"""
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"找不到逐字稿檔案：{file_path}")
    with file_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("utterances"), list):
        raise ValueError(f"逐字稿格式錯誤（缺少 utterances 陣列）：{file_path}")
    return data


def _match_first(patterns: tuple[str, ...], text: str) -> str | None:
    """回傳第一個命中的樣式所對應的原文片語。"""
    for pattern in patterns:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            return found.group(0)
    return None


def classify_utterance(text: str) -> tuple[str, str] | None:
    """判定這句是否為明確承諾。回傳 (類型, 命中片語)，模糊語氣一律回 None。"""
    if not text:
        return None
    if _match_first(HEDGE_PATTERNS, text):
        return None
    for kind, patterns in (
        ("first_person", FIRST_PERSON_PATTERNS),
        ("request", REQUEST_PATTERNS),
        ("group", GROUP_PATTERNS),
    ):
        phrase = _match_first(patterns, text)
        if phrase:
            return kind, phrase
    return None


def extract_due_hint(text: str) -> str | None:
    """抽出逐字稿中出現的期限用語；沒說期限就是 None。"""
    for pattern in DUE_PATTERNS:
        found = re.search(pattern, text, re.IGNORECASE)
        if found:
            return found.group(1).strip()
    return None


def _name_tokens(name: str) -> list[str]:
    """姓名比對用的候選字串：全名優先，再拆姓與名。"""
    parts = [part for part in name.replace("　", " ").split(" ") if part]
    tokens = [name, *parts]
    return list(dict.fromkeys(tokens))


def find_named_owner(text: str, speaker: str, participants: list[str]) -> str | None:
    """找出句中被指名的與會者。找不到就回 None——鐵律：絕不猜測負責人。"""
    best_index: int | None = None
    owner: str | None = None
    for name in participants:
        if name == speaker:
            continue
        for token in _name_tokens(name):
            index = text.find(token)
            if index >= 0 and (best_index is None or index < best_index):
                best_index, owner = index, name
    return owner


def _resolve_owner(
    kind: str, text: str, speaker: str, participants: list[str]
) -> tuple[str | None, float]:
    """依承諾類型決定負責人與基礎信心值。"""
    if kind == "first_person":
        return speaker, BASE_CONFIDENCE["first_person"]
    if kind == "group":
        return None, BASE_CONFIDENCE["group"]
    owner = find_named_owner(text, speaker, participants)
    if owner is None:
        return None, BASE_CONFIDENCE["request_unassigned"]
    return owner, BASE_CONFIDENCE["request_named"]


def detect_commitments(
    transcript: dict[str, Any], confidence_factor: float = 1.0
) -> list[ActionItem]:
    """逐句掃描，只留下明確承諾。一句最多產生一項行動。"""
    participants = [
        str(person.get("name", "")) for person in transcript.get("attendees", [])
    ]
    items: list[ActionItem] = []
    for position, utterance in enumerate(transcript.get("utterances", []), start=1):
        text = str(utterance.get("text") or "").strip()
        speaker = str(utterance.get("speaker") or "unknown")
        verdict = classify_utterance(text)
        if verdict is None:
            continue
        kind, phrase = verdict
        owner, base = _resolve_owner(kind, text, speaker, participants)
        items.append(
            ActionItem(
                text=text,
                owner=owner,
                speaker=speaker,
                line=int(utterance.get("index", position)),
                kind=kind,
                commitment_phrase=phrase,
                due_hint=extract_due_hint(text),
                confidence=round(base * confidence_factor, 2),
            )
        )
    return items


def detect_decisions(transcript: dict[str, Any]) -> list[str]:
    """抓出已拍板的決策原句（保留說話者，方便回溯）。"""
    decisions: list[str] = []
    for utterance in transcript.get("utterances", []):
        text = str(utterance.get("text") or "").strip()
        if text and _match_first(DECISION_PATTERNS, text):
            decisions.append(f"{utterance.get('speaker', 'unknown')}：{text}")
    return decisions


def assess_quality(
    transcript: dict[str, Any], extraction_config: dict[str, Any]
) -> dict[str, Any]:
    """判定逐字稿品質等級，決定要對客戶宣告哪一段準確率區間。"""
    utterances = transcript.get("utterances", [])
    overlap_count = sum(1 for item in utterances if item.get("overlap"))
    overlap_ratio = overlap_count / len(utterances) if utterances else 0.0
    threshold = float(extraction_config.get("overlap_messy_threshold", 0.15))
    hint = str(transcript.get("quality_hint", "clear")).lower()
    profile = "messy" if hint == "messy" or overlap_ratio > threshold else "clear"
    band = (extraction_config.get("quality_profiles") or {}).get(profile) or {}
    return {
        "profile": profile,
        "overlap_ratio": round(overlap_ratio, 3),
        "accuracy_min": float(band.get("accuracy_min", 0.60)),
        "accuracy_max": float(band.get("accuracy_max", 0.75)),
        "confidence_factor": float(band.get("confidence_factor", 0.80)),
    }


def _meeting_minutes(transcript: dict[str, Any]) -> int | None:
    """從起訖時間算會議長度；時間格式壞掉就回 None，不讓摘要整個失敗。"""
    started, ended = transcript.get("started_at"), transcript.get("ended_at")
    if not started or not ended:
        return None
    try:
        delta = datetime.fromisoformat(str(ended)) - datetime.fromisoformat(str(started))
    except ValueError:
        return None
    return max(int(delta.total_seconds() // 60), 0)


def build_summary(
    transcript: dict[str, Any],
    items: list[ActionItem],
    decisions: list[str],
    quality: dict[str, Any],
) -> str:
    """確定性摘要：mock 模式的輸出，也是 live 模式 LLM 失效時的保底。"""
    minutes = _meeting_minutes(transcript)
    length = f"{minutes} 分鐘" if minutes is not None else "長度未知"
    unassigned = sum(1 for item in items if item.is_unassigned)
    attendees = len(transcript.get("attendees", []))
    return (
        f"{transcript.get('title', '未命名會議')}｜"
        f"{transcript.get('platform', 'unknown')}｜{attendees} 人與會｜{length}。\n"
        f"擷取到 {len(items)} 項明確承諾（{unassigned} 項未指定負責人）、"
        f"{len(decisions)} 項已拍板決策。\n"
        f"逐字稿品質判定為「{quality['profile']}」。"
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


def count_unbacked_items(
    llm_items: list[Any], approved: list[ActionItem]
) -> int:
    """算出有幾項 LLM 行動項目找不到承諾原句佐證（這些會被丟棄）。"""
    approved_texts = [item.text for item in approved]
    unbacked = 0
    for entry in llm_items:
        evidence = str((entry or {}).get("evidence", "")).strip() if isinstance(entry, dict) else ""
        if not evidence or not any(evidence in text or text in evidence for text in approved_texts):
            unbacked += 1
    return unbacked


class ActionExtractor:
    """把一份逐字稿轉成可寄出的行動清單。"""

    def __init__(
        self,
        config: dict[str, Any],
        llm: Any,
        diagnostics: Any,
        prompt_path: str | Path | None = None,
    ) -> None:
        self._extraction: dict[str, Any] = config.get("extraction") or {}
        self._llm_config: dict[str, Any] = config.get("llm") or {}
        self._llm = llm
        self._diagnostics = diagnostics
        self._prompt_path = Path(prompt_path) if prompt_path else DEFAULT_PROMPT_PATH

    def read_prompt(self) -> str:
        """提示詞是資產，獨立成檔。缺檔就明確報錯，不用內嵌字串偷偷頂替。"""
        if not self._prompt_path.is_file():
            raise FileNotFoundError(f"找不到提示詞檔案：{self._prompt_path}")
        return self._prompt_path.read_text(encoding="utf-8")

    def extract(self, transcript: dict[str, Any]) -> ExtractionResult:
        """主流程：品質判定 → 承諾閘門 → 決策 → LLM 摘要 → 警示彙整。"""
        quality = assess_quality(transcript, self._extraction)
        items = detect_commitments(transcript, quality["confidence_factor"])
        decisions = detect_decisions(transcript)
        result = ExtractionResult(
            transcript_id=str(transcript.get("transcript_id", "unknown")),
            title=str(transcript.get("title", "未命名會議")),
            summary=build_summary(transcript, items, decisions, quality),
            decisions=decisions,
            action_items=items,
            quality=quality,
        )
        self._apply_llm(transcript, result)
        self._collect_warnings(result)
        return result

    def _apply_llm(self, transcript: dict[str, Any], result: ExtractionResult) -> None:
        """呼叫 LLM 取摘要與決策；行動項目仍受確定性承諾閘門管制。"""
        raw = self._llm.complete(
            system=self.read_prompt(),
            user=json.dumps(transcript, ensure_ascii=False),
            max_tokens=int(self._llm_config.get("max_tokens", 2000)),
        )
        result.llm_note = str(raw)[:120]
        if str(raw).startswith("[MOCK]"):
            return
        payload = _safe_json(str(raw))
        if payload is None:
            self._diagnostics.amber(
                "LLM 回傳非 JSON，改用確定性摘要",
                "檢查 prompts/extract.md 的「輸出格式」段落是否被覆寫",
            )
            return
        result.summary = str(payload.get("summary") or result.summary)
        result.decisions = [str(item) for item in payload.get("decisions") or []] or result.decisions
        dropped = count_unbacked_items(payload.get("action_items") or [], result.action_items)
        if dropped:
            self._diagnostics.amber(
                f"丟棄 {dropped} 項無承諾原句佐證的 LLM 行動項目",
                "提示詞需再次強調 evidence 必須逐字引用逐字稿",
            )

    def _collect_warnings(self, result: ExtractionResult) -> None:
        """把需要人工注意的狀況同時寫進 warnings 與 diagnostics。"""
        floor = float(self._extraction.get("confidence_floor", 0.6))
        if result.unassigned_count:
            self._warn(
                result,
                f"{result.unassigned_count} 項行動未指定負責人，已標記 owner=null（系統不猜測）",
                "在寄出前補上負責人，或請主持人於會中明確指派",
            )
        low_confidence = [i for i in result.action_items if i.confidence < floor]
        if low_confidence:
            self._warn(
                result,
                f"{len(low_confidence)} 項行動信心值低於 {floor}",
                "人工複核這幾句原文後再轉派",
            )
        if result.quality["profile"] == "messy":
            self._warn(
                result,
                "逐字稿為多人重疊 / 口語化，預期準確率僅 "
                f"{result.quality['accuracy_min']:.0%}-{result.quality['accuracy_max']:.0%}",
                "請主持人要求輪流發言，或改用每人獨立音軌的錄音設定",
            )

    def _warn(self, result: ExtractionResult, symptom: str, fix: str) -> None:
        """警示一律雙寫：warnings 給客戶看得到的輸出，amber 給營運監控。"""
        result.warnings.append(symptom)
        self._diagnostics.amber(symptom, fix)
