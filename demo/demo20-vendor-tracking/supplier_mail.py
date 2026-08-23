"""供應商信箱擷取與回覆解析（附錄F 的 Email Match + Classification）。

流程固定兩段，順序不可對調：

1. **Email Match**：先用 PO 號碼把郵件綁到採購單，再驗寄件網域是否屬於該 PO 的供應商。
   先比對再分類，是為了讓「內容看起來像確認、但寄件人不是這家供應商」的信
   在還沒進狀態機之前就被擋下。
2. **Classification**：分類為確認收件 / 出貨通知 / 到貨通知 / 延遲通知 / 發票。

本模組最重要的一條紀律：**解析失敗必須警報，不可靜默當成「沒有更新」**。
供應商換了信件範本、業務用手機回了一句沒頭沒尾的話、或有人拿別的網域冒名回覆——
這些都不代表「這張 PO 沒動靜」，而代表「我們讀不懂，需要人看」。
把讀不懂當成沒事，正是這套系統最容易發生的無聲故障。

檔名刻意不叫 `mailbox.py`：那會遮蔽標準庫的 `mailbox` 模組
（demo 目錄會被插到 `sys.path` 最前面），將來只要有任何相依套件
`import mailbox`，就會拿到這一份而炸在完全無關的地方。
"""

from __future__ import annotations

import email
import imaplib
import json
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from orders import (
    EVENT_ACKNOWLEDGEMENT,
    EVENT_DELAY,
    EVENT_DELIVERY,
    EVENT_INVOICE,
    EVENT_SHIPMENT,
    OrderError,
    PurchaseOrder,
    SupplierEvent,
    parse_timestamp,
)

# PO 號碼樣式。至少 6 位數字，避免把 "PO-1" 這種內文縮寫誤當單號。
PO_PATTERN = re.compile(r"\bPO-\d{6,}\b", re.IGNORECASE)

# 供應商在信中順口提到的新交期。允許 "revised ETA / new ETA / ETA" 三種寫法，
# 後面最多隔 12 個非數字字元（冒號、空白、破折號等）才接日期。
ETA_PATTERN = re.compile(
    r"\b(?:revised\s+eta|new\s+eta|eta)\b[^\d]{0,12}(\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?)",
    re.IGNORECASE,
)

# 分類規則。**順序即優先序**，由具體到籠統：
# 「shipment notice ... revised ETA」同時含出貨與交期字樣，先命中的才算數。
CLASSIFIER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (EVENT_DELAY, ("delay", "delayed", "postpone", "reschedul", "延遲", "延期", "遅延")),
    (EVENT_DELIVERY, ("delivered", "delivery confirmation", "signed for", "到貨", "簽收", "納品")),
    (EVENT_SHIPMENT, ("shipped", "shipment", "dispatch", "tracking", "出貨", "出荷", "発送")),
    (EVENT_INVOICE, ("invoice", "statement of account", "發票", "請求書")),
    (EVENT_ACKNOWLEDGEMENT, ("acknowledg", "confirm", "確認", "受注", "承認")),
)

FAILURE_NO_PO = "no_po_number"
FAILURE_AMBIGUOUS_PO = "ambiguous_po_number"
FAILURE_UNKNOWN_PO = "unknown_po_number"
FAILURE_DOMAIN_MISMATCH = "domain_mismatch"
FAILURE_UNCLASSIFIED = "unclassified"
FAILURE_BAD_TIMESTAMP = "bad_timestamp"


class MailboxError(RuntimeError):
    """信箱無法讀取（檔案損毀、IMAP 連線或認證失敗）"""


@dataclass(frozen=True)
class ParseFailure:
    """一封讀不懂的供應商回覆。每一筆都會被轉成 AMBER，不得靜默丟棄。"""

    email_id: str
    sender: str
    subject: str
    kind: str
    reason: str
    po_number: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """轉成 JSON 可序列化的形狀"""
        return {
            "email_id": self.email_id,
            "sender": self.sender,
            "subject": self.subject,
            "kind": self.kind,
            "reason": self.reason,
            "po_number": self.po_number,
        }


def load_messages(path: Path) -> list[dict[str, Any]]:
    """讀取 mock 信箱 JSON（欄位：id / from / subject / received_at / body）。"""
    if not path.is_file():
        raise MailboxError(f"找不到 mock 信箱檔：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MailboxError(f"mock 信箱檔損毀無法解析：{path}（{exc}）") from exc
    messages = payload.get("messages") if isinstance(payload, dict) else payload
    if not isinstance(messages, list):
        raise MailboxError(f"mock 信箱檔缺少 messages 陣列：{path}")
    return messages


def fetch_imap_messages(host: str, port: int, user: str, password: str, folder: str,
                        limit: int, timeout: int) -> list[dict[str, Any]]:
    """live 模式：用標準庫 imaplib 抓取供應商信箱最近 `limit` 封信。

    只讀不刪、不標已讀：這個信箱是採購人員也在看的，
    自動化不該改變人類看到的收件匣狀態。
    """
    try:
        with imaplib.IMAP4_SSL(host, port, timeout=timeout) as client:
            client.login(user, password)
            client.select(folder, readonly=True)
            status, data = client.search(None, "ALL")
            if status != "OK" or not data or data[0] is None:
                raise MailboxError(f"IMAP 搜尋失敗（status={status}）：{host}/{folder}")
            uids = data[0].split()[-int(limit):]
            return [_fetch_one(client, uid) for uid in uids]
    except imaplib.IMAP4.error as exc:
        raise MailboxError(f"IMAP 登入或指令失敗：{host}（{exc}）") from exc
    except OSError as exc:
        raise MailboxError(f"無法連線 IMAP 伺服器：{host}:{port}（{exc}）") from exc


def _fetch_one(client: imaplib.IMAP4_SSL, uid: bytes) -> dict[str, Any]:
    """抓取單封信並轉成與 mock 檔相同的形狀"""
    status, payload = client.fetch(uid, "(RFC822)")
    if status != "OK" or not payload or not isinstance(payload[0], tuple):
        raise MailboxError(f"IMAP 取信失敗（uid={uid!r}, status={status}）")
    message = email.message_from_bytes(payload[0][1])
    return {
        "id": uid.decode("ascii", errors="replace"),
        "from": _decode_field(message.get("From")),
        "subject": _decode_field(message.get("Subject")),
        # Date 標頭是 RFC 2822 格式（"Mon, 24 Aug 2026 09:00:00 +0800"），
        # 必須先轉成 ISO 8601，下游的 parse_timestamp 才讀得懂。
        "received_at": _header_date_to_iso(message.get("Date")),
        "body": _extract_body(message),
    }


def _header_date_to_iso(raw: str | None) -> str:
    """RFC 2822 的 Date 標頭轉 ISO 8601；轉不了就原樣回傳交由下游警報。"""
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return raw


def _decode_field(raw: str | None) -> str:
    """解碼 RFC 2047 編碼的信件標頭（中日文寄件人／主旨常見）"""
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except (UnicodeDecodeError, LookupError, ValueError):
        # 標頭編碼壞掉不該讓整批郵件處理中斷；原樣回傳，後續分類失敗會被警報
        return raw


def _extract_body(message: Message) -> str:
    """取出第一段 text/plain 內容（不解析 HTML，避免引入額外依賴）"""
    if not message.is_multipart():
        return _decode_payload(message)
    for part in message.walk():
        if part.get_content_type() == "text/plain":
            return _decode_payload(part)
    return ""


def _decode_payload(part: Message) -> str:
    """把單一 MIME part 解成字串；解不開時退回 replace 而非拋錯"""
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(part.get_payload() or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


# ── Email Match ──────────────────────────────────────────────────────
def find_po_numbers(text: str) -> list[str]:
    """從主旨與內文中找出所有不重複的 PO 號碼（大寫正規化）"""
    found = [match.group(0).upper() for match in PO_PATTERN.finditer(text)]
    return list(dict.fromkeys(found))


def is_domain_match(sender: str, supplier_domain: str) -> bool:
    """寄件網域比對。supplier_domain 必須以 '@' 開頭。

    不接受裸網域結尾比對：白名單若寫 "delta-circuit.example"，
    "sales@fake-delta-circuit.example" 會誤判命中，
    冒名者只要註冊一個含目標網域的網域名，就能讓假的「已確認」寫進狀態機。
    這條紀律與 `_shared/autonomy.py` 的白名單比對一致。
    """
    if not supplier_domain.startswith("@"):
        raise OrderError(f"供應商網域必須以 '@' 開頭：{supplier_domain!r}")
    return sender.strip().lower().endswith(supplier_domain.lower())


def classify(text: str) -> str | None:
    """依關鍵字規則分類；一條都沒命中回 None（呼叫端必須警報，不可當成沒事）。"""
    lowered = text.lower()
    for kind, keywords in CLASSIFIER_RULES:
        if any(keyword in lowered for keyword in keywords):
            return kind
    return None


def extract_revised_eta(text: str, tz: tzinfo) -> datetime | None:
    """從內文抓出供應商提到的新交期；抓不到回 None（不猜）。"""
    match = ETA_PATTERN.search(text)
    if match is None:
        return None
    try:
        return parse_timestamp(match.group(1).replace(" ", "T"), tz)
    except OrderError:
        # 日期樣式對但值不合法（例如 2026-02-31）：當成沒抓到，交期沿用原承諾
        return None


def parse_messages(messages: Iterable[dict[str, Any]], orders: Iterable[PurchaseOrder],
                   tz: tzinfo) -> tuple[list[SupplierEvent], list[ParseFailure]]:
    """把整批郵件轉成 (成功事件, 解析失敗)。失敗清單一定要被呼叫端警報。"""
    orders_by_po = {order.po_number: order for order in orders}
    events: list[SupplierEvent] = []
    failures: list[ParseFailure] = []
    for message in messages:
        event, failure = parse_one(message, orders_by_po, tz)
        if event is not None:
            events.append(event)
        if failure is not None:
            failures.append(failure)
    return events, failures


def parse_one(message: dict[str, Any], orders_by_po: dict[str, PurchaseOrder],
              tz: tzinfo) -> tuple[SupplierEvent | None, ParseFailure | None]:
    """解析單封郵件。回傳 (事件, 失敗) —— 兩者必定恰有一個為 None。"""
    email_id = str(message.get("id") or "(無 id)")
    sender = str(message.get("from") or "")
    subject = str(message.get("subject") or "")
    text = f"{subject}\n{message.get('body') or ''}"

    order, failure = _match_order(email_id, sender, subject, text, orders_by_po)
    if order is None:
        return None, failure

    kind = classify(text)
    if kind is None:
        return None, ParseFailure(
            email_id=email_id, sender=sender, subject=subject, kind=FAILURE_UNCLASSIFIED,
            po_number=order.po_number,
            reason="無法分類供應商回覆內容（非確認／出貨／到貨／延遲／發票），需人工判讀",
        )
    try:
        occurred_at = parse_timestamp(message.get("received_at"), tz)
    except OrderError as exc:
        # 收信時間讀不出來就無法排序事件，也無法算逾期。當成解析失敗警報，
        # 不可用「現在」頂替——那會讓一封三週前的確認信看起來像剛剛才到。
        return None, ParseFailure(
            email_id=email_id, sender=sender, subject=subject, kind=FAILURE_BAD_TIMESTAMP,
            po_number=order.po_number, reason=f"收信時間無法解析：{exc}",
        )
    return SupplierEvent(
        email_id=email_id,
        po_number=order.po_number,
        kind=kind,
        occurred_at=occurred_at,
        subject=subject,
        revised_eta=extract_revised_eta(text, tz),
    ), None


def _match_order(email_id: str, sender: str, subject: str, text: str,
                 orders_by_po: dict[str, PurchaseOrder]
                 ) -> tuple[PurchaseOrder | None, ParseFailure | None]:
    """Email Match：PO 號碼 → 採購單 → 寄件網域驗證。任何一關不過都回失敗。"""
    def fail(kind: str, reason: str, po_number: str | None = None) -> ParseFailure:
        return ParseFailure(email_id=email_id, sender=sender, subject=subject,
                            kind=kind, reason=reason, po_number=po_number)

    numbers = find_po_numbers(text)
    if not numbers:
        return None, fail(FAILURE_NO_PO, "主旨與內文都找不到 PO 號碼，無法歸屬到採購單")
    if len(numbers) > 1:
        return None, fail(FAILURE_AMBIGUOUS_PO,
                          f"同一封信提及多張 PO（{', '.join(numbers)}），無法確定對象")
    po_number = numbers[0]
    order = orders_by_po.get(po_number)
    if order is None:
        return None, fail(FAILURE_UNKNOWN_PO, f"{po_number} 不在 ERP 的未結採購單清單中", po_number)
    if not is_domain_match(sender, order.supplier.domain):
        return None, fail(
            FAILURE_DOMAIN_MISMATCH,
            f"寄件網域 {sender!r} 與 {po_number} 的供應商 "
            f"{order.supplier.name}（{order.supplier.domain}）不符，疑似冒名或轉寄",
            po_number,
        )
    return order, None
