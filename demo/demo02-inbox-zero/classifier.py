"""收件匣分類引擎：VIP / FYI / SPAM 三分類與情緒評分。

設計取捨（第 04 章）：分類這一層刻意用**決定性規則**而不是 LLM。
理由有三個：
1. 可稽核 —— 每一封信都能說出「為什麼被歸到這類」，客戶要的是這個，不是黑箱。
2. 零成本 —— 60 封信不用 60 次 API 呼叫，`--mock` 才能真的零憑證跑完。
3. 可重現 —— 同一封信永遠得到同一個結果，回歸測試才有意義。
LLM 只在兩個地方出場：低信心信件的第二意見（`--live` 且設定開啟時），
以及 VIP 回覆草稿的撰寫。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORY_VIP = "VIP"
CATEGORY_FYI = "FYI"
CATEGORY_SPAM = "SPAM"

# 網域與個人命中算強訊號，主旨關鍵字只算弱訊號。
# 這個權重差距是刻意的：釣魚信最愛在主旨塞 urgent / invoice 冒充 VIP，
# 若讓關鍵字擁有和網域相同的份量，垃圾信就能一路直達創辦人眼前。
SCORE_DOMAIN = 3
SCORE_INDIVIDUAL = 3
SCORE_SUBJECT_KEYWORD = 1
VIP_STRONG_THRESHOLD = 3
SPAM_STRONG_THRESHOLD = 3

# 垃圾信訊號詞。單一命中不足以定罪（電子報也會寫 unsubscribe），
# 要累積到 SPAM_STRONG_THRESHOLD 才算強訊號。
SPAM_HINTS: tuple[str, ...] = (
    "unsubscribe", "click here", "limited time", "act now", "risk-free",
    "free trial", "special offer", "guaranteed", "no obligation",
    "pre-approved", "congratulations", "winner", "crypto", "lottery",
    "退訂", "點擊這裡", "限時", "免費試用", "特惠", "保證獲利",
)

NEGATIVE_HINTS: tuple[str, ...] = (
    "delay", "delayed", "overdue", "complaint", "unacceptable", "escalate",
    "cancel", "refund", "mistake", "missed", "chase", "not happy", "short",
    "延遲", "抱歉", "取消", "客訴", "問題", "來不及",
)

POSITIVE_HINTS: tuple[str, ...] = (
    "thanks", "thank you", "appreciate", "great", "happy to", "aligned",
    "confirmed", "well done", "sharper", "pleased", "no problem",
    "謝謝", "沒問題", "太好了", "順利",
)

_ADDRESS_PATTERN = re.compile(r"<([^<>]+)>")


def extract_address(raw_from: str) -> str:
    """從 `顯示名稱 <a@b.com>` 取出小寫的純信箱位址；取不到就回原字串。"""
    text = (raw_from or "").strip()
    match = _ADDRESS_PATTERN.search(text)
    return (match.group(1) if match else text).strip().lower()


@dataclass(frozen=True)
class VipRules:
    """VIP_SENDERS 三層設定：網域 / 特定個人 / 主旨關鍵字。"""

    domains: tuple[str, ...] = ()
    individuals: tuple[str, ...] = ()
    subject_keywords: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, section: dict | None) -> "VipRules":
        """從 config.yaml 的 `vip_senders` 區段建立規則，一律轉小寫比對。"""
        data = section or {}
        return cls(
            domains=_lower_tuple(data.get("domains")),
            individuals=_lower_tuple(data.get("individuals")),
            subject_keywords=_lower_tuple(data.get("subject_keywords")),
        )

    @property
    def is_empty(self) -> bool:
        """三層全空代表使用者根本沒設定 VIP，這是最常見的誤判來源。"""
        return not (self.domains or self.individuals or self.subject_keywords)


def _lower_tuple(values: list[str] | None) -> tuple[str, ...]:
    """把設定值正規化成小寫、去空白、去空值的 tuple。"""
    return tuple(str(v).strip().lower() for v in (values or []) if str(v).strip())


@dataclass
class ClassifiedEmail:
    """單封信的分類結果，欄位都保留下來讓客戶能追問「為什麼」。"""

    address: str
    sender: str
    subject: str
    received_at: str
    category: str
    confidence: float
    vip_score: int
    spam_score: int
    sentiment_score: float
    sentiment_label: str
    reasons: list[str] = field(default_factory=list)
    is_suspect_misclassification: bool = False
    body: str = ""

    @property
    def is_vip(self) -> bool:
        return self.category == CATEGORY_VIP

    def to_dict(self) -> dict:
        """轉成可 JSON 序列化的 dict（不含信件全文，避免報表洩漏內容）。"""
        return {
            "address": self.address,
            "sender": self.sender,
            "subject": self.subject,
            "received_at": self.received_at,
            "category": self.category,
            "confidence": self.confidence,
            "sentiment_score": self.sentiment_score,
            "sentiment_label": self.sentiment_label,
            "reasons": list(self.reasons),
            "is_suspect_misclassification": self.is_suspect_misclassification,
        }


def _find_hints(haystack: str, hints: tuple[str, ...]) -> list[str]:
    """回傳在文字中出現的訊號詞清單（已小寫比對，同一詞只算一次）。"""
    return [hint for hint in hints if hint in haystack]


def score_vip(address: str, subject: str, rules: VipRules) -> tuple[int, list[str]]:
    """依三層 VIP 規則計分，並回傳人看得懂的命中理由。"""
    score = 0
    reasons: list[str] = []
    for domain in rules.domains:
        if address.endswith(domain):
            score += SCORE_DOMAIN
            reasons.append(f"VIP 網域命中：{domain}")
            break
    if address in rules.individuals:
        score += SCORE_INDIVIDUAL
        reasons.append(f"VIP 個人命中：{address}")
    for keyword in rules.subject_keywords:
        if keyword in subject:
            score += SCORE_SUBJECT_KEYWORD
            reasons.append(f"VIP 主旨關鍵字：{keyword}")
    return score, reasons


def score_spam(text: str) -> tuple[int, list[str]]:
    """依訊號詞數量計算垃圾信分數。"""
    hits = _find_hints(text, SPAM_HINTS)
    reasons = [f"垃圾訊號詞：{', '.join(hits)}"] if hits else []
    return len(hits), reasons


def score_sentiment(text: str) -> tuple[float, str]:
    """回傳 (-1.0 ~ 1.0 的情緒分數, 標籤)。中性代表沒有明顯情緒詞。"""
    positives = len(_find_hints(text, POSITIVE_HINTS))
    negatives = len(_find_hints(text, NEGATIVE_HINTS))
    total = positives + negatives
    if total == 0:
        return 0.0, "neutral"
    score = round((positives - negatives) / total, 2)
    if score >= 0.2:
        return score, "positive"
    if score <= -0.2:
        return score, "negative"
    return score, "neutral"


def _decide(vip_score: int, spam_score: int) -> tuple[str, bool, float]:
    """依分數決定類別，回傳 (類別, 是否疑似誤判, 信心值)。

    順序是刻意的：強 VIP 訊號（網域／個人）優先於任何垃圾訊號，
    因為漏掉真 VIP 的代價遠高於多看一封廣告；反過來，只靠主旨關鍵字
    的弱 VIP 訊號打不過強垃圾訊號，但會被標記為疑似誤判送去人工覆核。
    """
    if vip_score >= VIP_STRONG_THRESHOLD:
        return CATEGORY_VIP, False, 0.95
    if spam_score >= SPAM_STRONG_THRESHOLD:
        is_suspect = vip_score > 0
        return CATEGORY_SPAM, is_suspect, 0.50 if is_suspect else 0.90
    if vip_score >= 1:
        return CATEGORY_VIP, False, 0.60
    if spam_score >= 1:
        return CATEGORY_SPAM, False, 0.55
    return CATEGORY_FYI, False, 0.70


def classify_email(email: dict, rules: VipRules) -> ClassifiedEmail:
    """分類單封信。缺欄位一律當空字串處理，不讓髒資料中斷整批作業。"""
    sender = str(email.get("from") or "")
    subject = str(email.get("subject") or "")
    body = str(email.get("body") or "")
    address = extract_address(sender)
    haystack = f"{subject}\n{body}".lower()

    vip_score, vip_reasons = score_vip(address, subject.lower(), rules)
    spam_score, spam_reasons = score_spam(haystack)
    category, is_suspect, confidence = _decide(vip_score, spam_score)
    sentiment_score, sentiment_label = score_sentiment(haystack)

    reasons = vip_reasons + spam_reasons
    if is_suspect:
        reasons.append("疑似誤判：命中 VIP 主旨關鍵字但整封信是行銷樣態")
    if not reasons:
        reasons.append("無 VIP 訊號也無垃圾訊號，歸為知會類")

    return ClassifiedEmail(
        address=address, sender=sender, subject=subject,
        received_at=str(email.get("received_at") or ""),
        category=category, confidence=confidence,
        vip_score=vip_score, spam_score=spam_score,
        sentiment_score=sentiment_score, sentiment_label=sentiment_label,
        reasons=reasons, is_suspect_misclassification=is_suspect, body=body,
    )


def classify_inbox(emails: list[dict], rules: VipRules) -> list[ClassifiedEmail]:
    """批次分類整個收件匣。"""
    return [classify_email(email, rules) for email in emails]


def summarise(results: list[ClassifiedEmail]) -> dict[str, int]:
    """統計各類別數量，供報表與測試斷言使用。"""
    counts = {"total": len(results), "vip": 0, "fyi": 0, "spam": 0}
    keys = {CATEGORY_VIP: "vip", CATEGORY_FYI: "fyi", CATEGORY_SPAM: "spam"}
    for result in results:
        counts[keys[result.category]] += 1
    return counts


def suspected_misclassifications(
    results: list[ClassifiedEmail],
) -> list[ClassifiedEmail]:
    """挑出被歸為 SPAM 但帶有 VIP 訊號的信件（琥珀色警示的觸發來源）。"""
    return [r for r in results if r.is_suspect_misclassification]


def low_confidence(
    results: list[ClassifiedEmail], threshold: float
) -> list[ClassifiedEmail]:
    """挑出信心值低於門檻的信件，供 LLM 第二意見覆核。"""
    return [r for r in results if r.confidence < threshold]
