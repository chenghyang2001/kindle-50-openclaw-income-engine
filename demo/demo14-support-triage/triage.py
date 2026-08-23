"""demo14 客服分流決策樹核心（附錄 F p12 — Triage Decision Logic）。

決策樹逐字實作自 apxF_p12：

    Inbound Message（多渠道訊息進件）
        v
    Intent Classification（意圖判定）
        ├─ Order Status      -> Query Shopify API -> Auto-Reply：提供精確物流更新
        ├─ FAQ               -> Search knowledge_base -> Auto-Reply：提供標準解答
        ├─ Refund / Dispute  -> DO NOT PROCESS（Holding Response）-> Escalate to Slack
        └─ Negative/VIP/Legal-> Immediate Flag -> Escalate to Slack（High Priority）

三個設計決定（踩過才知道為什麼）：

1. **分類是規則式的，LLM 只當顧問，不得推翻路由。**
   語言模型分類錯 FAQ 只是回答不夠貼切；分類錯退款爭議，代表機器人替公司對一筆
   爭議款做出了回應——那是法律風險，不是體驗問題。因此 `classify()` 只看關鍵字規則，
   LLM 的判讀存進 `advisory` 欄位供人事後檢討，永遠不改變 `category`。

2. **自動回覆的內容只能來自知識庫或訂單系統，不能由 LLM 生成。**
   客服對外講的每一句都可能被截圖當成承諾。知識庫的 30-50 條答案是有人審過的，
   LLM 生成的句子沒有。因此 `build_faq_reply()` / `build_order_reply()` 都是模板填空。

3. **判斷順序就是安全順序。** 先擋掉「絕不可自動回覆」的類別，最後才輪到自動回覆。
   任何一則訊息只要同時命中退款訊號與 FAQ 關鍵字，一律走退款路徑。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── 分類（apxF_p12 的四個分支 + 一個保底類別）────────────────────────────────
CATEGORY_ORDER_STATUS = "order_status"
CATEGORY_FAQ = "faq"
CATEGORY_REFUND_DISPUTE = "refund_dispute"
CATEGORY_NEGATIVE_VIP_LEGAL = "negative_vip_legal"
# 決策樹沒有涵蓋的訊息。往安全側倒：升級給人，不猜。
CATEGORY_UNKNOWN = "unknown"

ACTION_AUTO_REPLY = "auto_reply"
ACTION_ESCALATE = "escalate"

PRIORITY_HIGH = "HIGH"
PRIORITY_NORMAL = "NORMAL"

# 硬規則：這些類別永遠不會自動回覆，且不可由 config 關閉。
# 修改這個常數等同移除本模組的安全底線，請不要這麼做（README 有完整理由）。
NEVER_AUTO_REPLY: frozenset[str] = frozenset(
    {CATEGORY_REFUND_DISPUTE, CATEGORY_NEGATIVE_VIP_LEGAL, CATEGORY_UNKNOWN}
)

# 去重狀態檔預設檔名（實際路徑由 state_path() 推算）
HANDLED_FILE_DEFAULT = ".handled.json"
# 狀態檔只保留最近 N 筆，避免長年執行後無限膨脹
HANDLED_KEEP_LAST = 2000
# live 模式每個渠道抓取前的最小間隔秒數（Intercom / Meta Graph 都會對連續請求回 429）
MIN_FETCH_DELAY_SECONDS = 1.0
# 書中規格：知識庫 30-50 條常見問答
KB_MIN_ENTRIES = 30
KB_MAX_ENTRIES = 50
# 書中規格：一般問題直接回覆，長度 < 100 字
REPLY_MAX_WORDS = 100

_CJK_START = "一"
_CJK_END = "鿿"
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9]+")
# 金額擷取：支援 NT$48,000 / $48000 / 48,000 元 三種常見寫法
_AMOUNT_PATTERN = re.compile(r"(?:NT\$|US\$|\$|新台幣)\s*([\d,]+)|([\d,]{3,})\s*(?:元|塊)")


class SupportFetchError(RuntimeError):
    """單一渠道抓取失敗。由呼叫端決定升級成紅色警報還是琥珀警示。"""


# ── 路徑工具（一律以本檔位置推算，禁止硬編碼）──────────────────────────────
def module_dir() -> Path:
    """回傳本模組所在目錄，供 mock / 知識庫相對路徑解析使用。"""
    return Path(__file__).resolve().parent


def state_path(file_name: str = HANDLED_FILE_DEFAULT) -> Path:
    """回傳去重狀態檔的絕對路徑。"""
    return module_dir() / file_name


def resolve_data_file(relative_path: str) -> Path:
    """把 config 給的相對路徑接到模組目錄底下；已是絕對路徑則原樣採用。"""
    return (module_dir() / relative_path).resolve()


# ── 文字工具 ────────────────────────────────────────────────────────────────
def count_words(text: str) -> int:
    """計算字數：中日韓字元逐字計，拉丁文字以「詞」計。

    書中的「< 100 字」是對中文說的。用 len() 會把標點也算進去，用 split() 又完全
    量不到中文，兩種都會讓長度限制形同虛設，因此混合計算。
    """
    cjk = sum(1 for char in text if _CJK_START <= char <= _CJK_END)
    latin = len(_LATIN_TOKEN.findall(text))
    return cjk + latin


def one_sentence_summary(text: str, limit: int = 60) -> str:
    """壓成一句話，供 Slack 升級卡片使用。

    刻意不呼叫 LLM：升級卡片是安全關鍵路徑，API 掛掉時它仍然必須送得出去。
    """
    flat = " ".join(str(text).split())
    for mark in ("。", "！", "？", "\n", ". ", "! ", "? "):
        head, sep, _ = flat.partition(mark)
        if sep and len(head) <= limit:
            return head + (mark.strip() or "")
    return flat[:limit] + ("…" if len(flat) > limit else "")


def detect_signals(text: str, signal_words: list[str]) -> list[str]:
    """回傳命中的訊號詞（不分大小寫）。回傳清單而非布林，讓升級卡片能說明「為什麼」。"""
    lowered = str(text).lower()
    return [word for word in signal_words if word and str(word).lower() in lowered]


def extract_amount(text: str) -> float | None:
    """從訊息中抓出爭議金額。抓不到回 None（代表未提金額，不代表金額為 0）。"""
    match = _AMOUNT_PATTERN.search(str(text))
    if not match:
        return None
    raw = match.group(1) or match.group(2) or ""
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def extract_order_id(text: str, pattern: str) -> str | None:
    """依 config 的正規表達式抓訂單編號。pattern 寫錯時拋 ValueError，不靜默略過。"""
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"order_id_pattern 不是合法的正規表達式：{pattern!r}（{exc}）") from exc
    match = compiled.search(str(text))
    if not match:
        return None
    captured = match.group(1) if match.groups() else match.group(0)
    return captured.strip().upper()


# ── 訊息正規化與抓取 ────────────────────────────────────────────────────────
def normalize_message(raw: dict[str, Any], channel_id: str) -> dict[str, Any]:
    """把各渠道格式壓成統一欄位。

    缺欄位一律給空字串而非 None：下游全部是字串比對，None 會在每個比對點都要多一次
    防呆，反而更容易漏。
    """
    return {
        "message_id": str(raw.get("message_id") or raw.get("id") or ""),
        "channel": channel_id,
        "customer_name": str(raw.get("customer_name") or "匿名客戶"),
        "customer_contact": str(raw.get("customer_contact") or ""),
        "subject": str(raw.get("subject") or ""),
        "text": str(raw.get("text") or ""),
        "received_at": str(raw.get("received_at") or ""),
        "locale": str(raw.get("locale") or "zh-TW"),
        "is_vip": bool(raw.get("is_vip", False)),
    }


def message_key(message: dict[str, Any]) -> str:
    """去重鍵值。不同渠道可能撞 message_id，所以一律加渠道前綴。"""
    return f"{message.get('channel', '?')}:{message.get('message_id', '')}"


def _read_json_file(path: Path, what: str) -> Any:
    """讀 JSON（UTF-8）。缺檔或格式壞掉都拋 SupportFetchError，訊息含絕對路徑。"""
    if not path.is_file():
        raise SupportFetchError(f"找不到{what}：{path}")
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise SupportFetchError(f"{what}不是合法 JSON（{path}）：{exc}") from exc
    except OSError as exc:
        raise SupportFetchError(f"{what}讀取失敗（{path}）：{exc}") from exc


def _read_mock_messages(relative_path: str) -> list[dict[str, Any]]:
    """讀離線 mock 訊息檔（零網路零憑證）。"""
    if not relative_path:
        raise SupportFetchError("渠道未設定 mock_file，mock 模式無資料可讀")
    payload = _read_json_file(resolve_data_file(relative_path), "mock 訊息檔")
    items = payload.get("messages", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise SupportFetchError("mock 訊息檔格式錯誤：messages 必須是陣列")
    return items


def _fetch_live_messages(channel: dict[str, Any], token: str, timeout: int) -> list[dict[str, Any]]:
    """live 模式抓取單一渠道。各家 API 回傳結構不同，統一取 messages / data 欄位。"""
    api_url = str(channel.get("api_url") or "")
    if not api_url:
        raise SupportFetchError(f"渠道 {channel.get('id')} 未設定 api_url")
    request = urllib.request.Request(
        api_url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SupportFetchError(f"渠道 {channel.get('id')} 回應 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SupportFetchError(f"渠道 {channel.get('id')} 連線失敗：{exc}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SupportFetchError(f"渠道 {channel.get('id')} 回應不是合法 JSON：{exc}") from exc
    items = payload.get("messages") or payload.get("data") or []
    return items if isinstance(items, list) else []


def collect_messages(
    channels: list[dict[str, Any]],
    is_mock: bool,
    env: dict[str, str],
    delay_seconds: float = MIN_FETCH_DELAY_SECONDS,
    timeout: int = 30,
) -> tuple[list[dict[str, Any]], list[str]]:
    """逐一抓取所有啟用的渠道，回傳 (訊息清單, 錯誤訊息清單)。

    **單一渠道失敗不中斷其他渠道**：Instagram 的 token 過期不該讓 Gmail 的客訴也一起
    停擺。錯誤收集起來交給呼叫端決定嚴重度（全部失敗才是紅色警報）。
    """
    delay = max(float(delay_seconds), MIN_FETCH_DELAY_SECONDS)
    messages: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, channel in enumerate(channels):
        if not channel.get("enabled", True):
            continue
        channel_id = str(channel.get("id") or f"channel-{index}")
        try:
            if is_mock:
                raw_items = _read_mock_messages(str(channel.get("mock_file") or ""))
            else:
                if index:
                    time.sleep(delay)
                token = env.get(str(channel.get("token_env") or ""), "")
                if not token:
                    raise SupportFetchError(f"渠道 {channel_id} 缺少環境變數 {channel.get('token_env')}")
                raw_items = _fetch_live_messages(channel, token, timeout)
        except SupportFetchError as exc:
            errors.append(str(exc))
            continue
        messages.extend(normalize_message(item, channel_id) for item in raw_items)
    return messages, errors


def filter_new_messages(
    messages: list[dict[str, Any]], handled: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """濾掉已處理過的訊息，回傳 (新訊息, 被略過的 key)。"""
    fresh: list[dict[str, Any]] = []
    skipped: list[str] = []
    seen_this_run: set[str] = set()
    for message in messages:
        key = message_key(message)
        if key in handled or key in seen_this_run:
            skipped.append(key)
            continue
        seen_this_run.add(key)
        fresh.append(message)
    return fresh, skipped


def load_handled_keys(path: Path) -> set[str]:
    """讀取已處理的訊息 key。檔案不存在（首次執行）回空集合。"""
    if not path.is_file():
        return set()
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        # 狀態檔壞掉不該讓客服停擺。代價是重覆處理一輪（客戶多收一封），
        # 但反過來「靜默當成已處理」會讓真的客訴消失，那是不可接受的失敗方向。
        print(f"警告：去重狀態檔無法讀取（{path}）：{exc}，本次視為首次執行")
        return set()
    keys = payload.get("handled", []) if isinstance(payload, dict) else payload
    return {str(item) for item in keys} if isinstance(keys, list) else set()


def save_handled_keys(path: Path, handled: set[str], keep_last: int = HANDLED_KEEP_LAST) -> None:
    """寫回去重狀態檔（UTF-8）。超過 keep_last 只保留字典序末段。"""
    ordered = sorted(handled)[-keep_last:]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"handled": ordered, "count": len(ordered)}, handle, ensure_ascii=False, indent=2)


# ── 知識庫與訂單查詢 ────────────────────────────────────────────────────────
def load_knowledge_base(relative_path: str) -> list[dict[str, Any]]:
    """讀取知識庫。條目數是否落在 30-50 由呼叫端檢查（那是設定品質問題，不是錯誤）。"""
    payload = _read_json_file(resolve_data_file(relative_path), "知識庫檔")
    entries = payload.get("entries", []) if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        raise SupportFetchError("知識庫格式錯誤：entries 必須是陣列")
    return [item for item in entries if isinstance(item, dict)]


def match_faq(text: str, knowledge_base: list[dict[str, Any]], min_score: int) -> tuple[dict[str, Any] | None, int]:
    """在知識庫中找最相符的一條，回傳 (條目, 分數)。分數未達門檻回 (None, 分數)。

    用關鍵字命中數計分而非語意相似度：分數算法必須能被客服主管用肉眼複查，
    「為什麼機器人回了這條」要有一句話能解釋得清楚。
    """
    lowered = str(text).lower()
    best: dict[str, Any] | None = None
    best_score = 0
    for entry in knowledge_base:
        keywords = [str(word).lower() for word in entry.get("keywords", []) if word]
        score = sum(1 for word in keywords if word in lowered)
        if score > best_score:
            best, best_score = entry, score
    return (best, best_score) if best_score >= max(int(min_score), 1) else (None, best_score)


def load_orders(relative_path: str) -> dict[str, dict[str, Any]]:
    """讀取訂單資料（mock 模式的 Shopify 替身），以訂單編號為 key。"""
    payload = _read_json_file(resolve_data_file(relative_path), "訂單資料檔")
    orders = payload.get("orders", []) if isinstance(payload, dict) else payload
    if not isinstance(orders, list):
        raise SupportFetchError("訂單資料格式錯誤：orders 必須是陣列")
    return {str(item.get("order_id", "")).upper(): item for item in orders if isinstance(item, dict)}


def lookup_order(order_id: str | None, orders: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """查訂單。查無資料回 None（呼叫端會改走升級路徑，不會編一個進度出來）。"""
    if not order_id:
        return None
    return orders.get(order_id.strip().upper())


# ── 決策樹：意圖判定 ────────────────────────────────────────────────────────
def _is_vip(message: dict[str, Any], vip_contacts: list[str]) -> bool:
    """VIP 判定：訊息自帶旗標，或聯絡方式命中 VIP 名單（不分大小寫）。"""
    if message.get("is_vip"):
        return True
    contact = str(message.get("customer_contact", "")).strip().lower()
    return bool(contact) and contact in {str(item).strip().lower() for item in vip_contacts}


def _blank_verdict() -> dict[str, Any]:
    """分類結果的預設骨架。所有欄位一次給齊，下游不必到處 .get() 猜有沒有。"""
    return {
        "category": CATEGORY_UNKNOWN,
        "priority": PRIORITY_NORMAL,
        "action": ACTION_ESCALATE,
        "signals": [],
        "is_vip": False,
        "order_id": None,
        "order_found": False,
        "faq_id": None,
        "faq_score": 0,
        "refund_amount": None,
        "reason": "",
    }


def _classify_refund(text: str, rules: dict[str, Any], danger: list[str], verdict: dict[str, Any]) -> dict[str, Any]:
    """退款 / 爭議分支：DO NOT PROCESS。

    金額門檻（書中的 ESCALATION_RULES）只影響**優先度**，不影響「是否自動回覆」——
    因為答案永遠是不自動回覆。門檻低不代表可以讓機器人處理小額退款。
    """
    amount = extract_amount(text)
    threshold = float(rules.get("refund_amount_threshold", 0) or 0)
    is_over_threshold = amount is not None and threshold > 0 and amount >= threshold
    verdict.update(
        category=CATEGORY_REFUND_DISPUTE,
        action=ACTION_ESCALATE,
        priority=PRIORITY_HIGH if (danger or is_over_threshold or verdict["is_vip"]) else PRIORITY_NORMAL,
        refund_amount=amount,
        reason="命中退款/爭議訊號，依 apxF_p12 為 DO NOT PROCESS，只給 Holding Response 並升級真人",
    )
    if is_over_threshold:
        verdict["reason"] += f"；金額 {amount:.0f} 已達門檻 {threshold:.0f}"
    return verdict


def _classify_auto(text: str, rules: dict[str, Any], knowledge_base: list[dict[str, Any]],
                   kb_min_score: int, orders: dict[str, dict[str, Any]], verdict: dict[str, Any]) -> dict[str, Any]:
    """訂單狀態 / FAQ 分支：可自動回覆。查不到資料時一律降級為升級，不編造內容。"""
    order_id = extract_order_id(text, str(rules.get("order_id_pattern", r"\b([A-Z]{2}-\d{4,6})\b")))
    order = lookup_order(order_id, orders)
    if detect_signals(text, rules.get("order_status", [])) or order_id:
        verdict.update(category=CATEGORY_ORDER_STATUS, order_id=order_id, order_found=order is not None)
        if order is None:
            verdict["reason"] = f"疑似訂單查詢但查無訂單（{order_id or '未提供編號'}），升級真人核對"
            return verdict
        verdict.update(action=ACTION_AUTO_REPLY, reason=f"訂單查詢命中 {order_id}，回覆系統中的物流狀態")
        return verdict
    entry, score = match_faq(text, knowledge_base, kb_min_score)
    verdict["faq_score"] = score
    if entry is None:
        verdict["reason"] = f"知識庫最高命中分數 {score} 未達門檻 {kb_min_score}，不猜答案，升級真人"
        return verdict
    verdict.update(
        category=CATEGORY_FAQ,
        action=ACTION_AUTO_REPLY,
        faq_id=str(entry.get("id", "")),
        reason=f"知識庫條目 {entry.get('id')} 命中 {score} 個關鍵字，回覆已審核過的標準解答",
    )
    return verdict


def classify(
    message: dict[str, Any],
    rules: dict[str, Any],
    knowledge_base: list[dict[str, Any]],
    kb_min_score: int,
    orders: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """apxF_p12 決策樹主判定。判斷順序 = 安全順序，先擋不可自動回覆的類別。"""
    text = f"{message.get('subject', '')} {message.get('text', '')}"
    verdict = _blank_verdict()
    verdict["is_vip"] = _is_vip(message, list(rules.get("vip_contacts") or []))
    refund = detect_signals(text, rules.get("refund_dispute", []))
    danger = detect_signals(text, rules.get("negative_vip_legal", []))
    verdict["signals"] = refund + danger
    if refund:
        return _classify_refund(text, rules, danger, verdict)
    if danger or verdict["is_vip"]:
        verdict.update(
            category=CATEGORY_NEGATIVE_VIP_LEGAL,
            action=ACTION_ESCALATE,
            priority=PRIORITY_HIGH,
            reason="命中負面情緒 / VIP / 法務訊號，依 apxF_p12 為 Immediate Flag，高優先升級",
        )
        return verdict
    return _classify_auto(text, rules, knowledge_base, kb_min_score, orders, verdict)


# ── never_claim_human：誠實揭露 AI 身分 ────────────────────────────────────
def find_impersonation(text: str, banned_phrases: list[str]) -> list[str]:
    """找出回覆中冒充真人的措辭。回傳命中清單（空清單代表通過）。

    這是 `never_claim_human` 的程式層防線。提示詞可以被忽略、模板可以被改壞，
    但只要送出前會走過這個函式，冒充真人的句子就出不去。
    """
    lowered = str(text).lower()
    return [phrase for phrase in banned_phrases if phrase and str(phrase).lower() in lowered]


def has_disclosure(text: str, disclosure: str) -> bool:
    """檢查回覆是否含 AI 身分揭露。揭露句可含變動的括號，故只比對其核心片段。"""
    core = str(disclosure).strip().strip("（）()").strip()
    return bool(core) and core in str(text)


def apply_disclosure(text: str, disclosure: str) -> str:
    """確保回覆結尾帶有 AI 身分揭露。已有就不重複附加。"""
    body = str(text).rstrip()
    if not disclosure:
        return body
    return body if has_disclosure(body, disclosure) else f"{body}\n{disclosure}"


# ── 回覆與升級文案 ──────────────────────────────────────────────────────────
def _trim_to_limit(body: str, max_words: int) -> tuple[str, bool]:
    """把回覆本文壓到字數上限內（以句號為界），回傳 (本文, 是否被截斷)。"""
    if count_words(body) <= max_words:
        return body, False
    kept: list[str] = []
    for sentence in re.split(r"(?<=[。！？])", body):
        if not sentence:
            continue
        if count_words("".join(kept) + sentence) > max_words:
            break
        kept.append(sentence)
    trimmed = "".join(kept).strip()
    return (trimmed or body[:max_words]), True


def build_faq_reply(entry: dict[str, Any], greeting: str, disclosure: str, max_words: int) -> dict[str, Any]:
    """組出 FAQ 標準解答。內容完全來自知識庫，不經 LLM 改寫。

    字數上限只計算本文：身分揭露句是法遵要求的固定尾巴，讓它去擠壓答案空間毫無道理。
    """
    body, is_trimmed = _trim_to_limit(str(entry.get("answer", "")).strip(), max_words)
    text = apply_disclosure(f"{greeting}\n{body}".strip(), disclosure)
    return {"text": text, "source": f"knowledge_base:{entry.get('id', '')}", "is_trimmed": is_trimmed}


def build_order_reply(order: dict[str, Any], greeting: str, disclosure: str, max_words: int) -> dict[str, Any]:
    """組出訂單物流回覆。每一個數字都來自訂單系統，沒有一個是推估的。"""
    lines = [
        f"訂單 {order.get('order_id', '')} 目前狀態：{order.get('status_text', '')}。",
        f"物流：{order.get('carrier', '')}　追蹤編號：{order.get('tracking_no', '') or '尚未產生'}。",
        f"預計送達：{order.get('eta', '') or '出貨後另行通知'}。",
    ]
    body, is_trimmed = _trim_to_limit("\n".join(line for line in lines if line.strip("：。 ")), max_words)
    text = apply_disclosure(f"{greeting}\n{body}".strip(), disclosure)
    return {"text": text, "source": f"orders:{order.get('order_id', '')}", "is_trimmed": is_trimmed}


def build_holding_response(template: str, message: dict[str, Any], disclosure: str) -> str:
    """組出 Holding Response（退款/爭議專用）。

    它**只確認收到、只給時限**，不承認責任、不承諾金額、不說明處理結果——
    這三件事任何一件被機器人講出去，都可能在後續爭議中被當成公司立場。
    """
    filled = str(template).replace("{NAME}", str(message.get("customer_name", "")))
    return apply_disclosure(filled.strip(), disclosure)


def format_escalation_payload(message: dict[str, Any], verdict: dict[str, Any]) -> str:
    """Slack 升級卡片，格式逐字照抄 apxF_p12：

        *[PRIORITY] Support Escalation - {CHANNEL} - {CATEGORY}*
        > Customer: {NAME} | Query: [1-sentence summary]
    """
    summary = one_sentence_summary(message.get("subject") or message.get("text", ""))
    return (
        f"*[{verdict['priority']}] Support Escalation - "
        f"{str(message.get('channel', '')).upper()} - {str(verdict['category']).upper()}*\n"
        f"> Customer: {message.get('customer_name', '')} | Query: {summary}"
    )
