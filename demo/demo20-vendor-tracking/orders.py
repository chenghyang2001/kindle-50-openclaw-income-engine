"""採購訂單狀態機、逾期偵測、供應商計分卡與狀態檔管理。

本檔負責三件事：

1. **狀態機**：把供應商郵件事件套進「下單 → 確認 → 出貨 → 到貨」四段流程。
   只准前進，倒退（例如出貨通知晚於到貨通知）記為異常並保留原階段——
   讓錯序的郵件把 PO 打回上一段，會比沒收到那封信更糟。
2. **逾期偵測**：這是本模組真正的價值。沒有人記得哪張 PO 該催了，
   等到生產線停工才發現，就已經來不及。每張 PO 只發**最嚴重的那一則**警報。
3. **計分卡**：`on-time rate` 與 `avg acknowledgement time`，作為下次議價的籌碼。

金額一律 `decimal.Decimal`，且**不同幣別絕不相加**：
採購金額是要進 ERP 的數字，float 尾差與幣別混加都會讓帳對不起來。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ── 狀態機 ───────────────────────────────────────────────────────────
STAGE_PLACED = "placed"
STAGE_ACKNOWLEDGED = "acknowledged"
STAGE_SHIPPED = "shipped"
STAGE_DELIVERED = "delivered"
STAGE_ORDER = (STAGE_PLACED, STAGE_ACKNOWLEDGED, STAGE_SHIPPED, STAGE_DELIVERED)
STAGE_RANK = {name: index for index, name in enumerate(STAGE_ORDER)}
STAGE_LABEL = {
    STAGE_PLACED: "已下單",
    STAGE_ACKNOWLEDGED: "已確認",
    STAGE_SHIPPED: "已出貨",
    STAGE_DELIVERED: "已到貨",
}

# ── 郵件事件種類 ─────────────────────────────────────────────────────
EVENT_ACKNOWLEDGEMENT = "acknowledgement"
EVENT_SHIPMENT = "shipment"
EVENT_DELIVERY = "delivery"
EVENT_DELAY = "delay"
EVENT_INVOICE = "invoice"

_EVENT_TO_STAGE = {
    EVENT_ACKNOWLEDGEMENT: STAGE_ACKNOWLEDGED,
    EVENT_SHIPMENT: STAGE_SHIPPED,
    EVENT_DELIVERY: STAGE_DELIVERED,
}
_STAGE_TIMESTAMP_FIELD = {
    STAGE_ACKNOWLEDGED: "acknowledged_at",
    STAGE_SHIPPED: "shipped_at",
    STAGE_DELIVERED: "delivered_at",
}

# ── 警報種類（數字越大越嚴重，判定順序即由此排序）─────────────────────
ALERT_OVERDUE_DELIVERY = "overdue_delivery"
ALERT_UNACKNOWLEDGED = "unacknowledged"
ALERT_NOT_SHIPPED = "not_shipped"
ALERT_PRE_ETA_REMINDER = "pre_eta_reminder"

ALERT_SEVERITY = {
    ALERT_OVERDUE_DELIVERY: 4,
    ALERT_UNACKNOWLEDGED: 3,
    ALERT_NOT_SHIPPED: 2,
    ALERT_PRE_ETA_REMINDER: 1,
}
ALERT_LABEL = {
    ALERT_OVERDUE_DELIVERY: "逾期未到貨",
    ALERT_UNACKNOWLEDGED: "逾期未確認",
    ALERT_NOT_SHIPPED: "交期將至但尚未出貨",
    ALERT_PRE_ETA_REMINDER: "進入交期前追蹤窗",
}
ALERT_MARK = {
    ALERT_OVERDUE_DELIVERY: "[逾期]",
    ALERT_UNACKNOWLEDGED: "[逾期]",
    ALERT_NOT_SHIPPED: "[風險]",
    ALERT_PRE_ETA_REMINDER: "[追蹤]",
}
# 前三者是「已經出事」，需要催辦；最後一者只是提前提醒。
OVERDUE_ALERTS = (ALERT_OVERDUE_DELIVERY, ALERT_UNACKNOWLEDGED, ALERT_NOT_SHIPPED)

HOURS_QUANT = Decimal("0.01")
PERCENT_QUANT = Decimal("0.1")
SECONDS_PER_HOUR = Decimal("3600")
STATE_VERSION = 1


class OrderError(ValueError):
    """採購單資料不合法（金額非數字、時間無法解析、狀態檔損毀等）"""


# ── 基礎轉換 ─────────────────────────────────────────────────────────
def resolve_timezone(name: str, fallback_offset_hours: float) -> tuple[tzinfo, str | None]:
    """解析時區名稱，回傳 (tzinfo, 警告訊息或 None)。

    Windows 沒有系統 tz database，未安裝 `tzdata` 套件時 `ZoneInfo("Asia/Taipei")`
    會拋 ZoneInfoNotFoundError。這裡退回設定檔指定的固定 UTC 偏移，
    並把原因當成警告字串交回呼叫端轉成 AMBER。

    刻意**不**退回機器本地時間：同一批資料在不同機器算出不同的逾期時數，
    比「時區載不到」本身更難查。
    """
    label = str(name).strip()
    if label.upper() == "UTC":
        return timezone.utc, None
    try:
        return ZoneInfo(label), None
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        offset = timezone(timedelta(hours=float(fallback_offset_hours)))
        return offset, (
            f"時區 {label!r} 無法載入（{exc}），已改用固定偏移 "
            f"UTC{float(fallback_offset_hours):+g}；如需正確日光節約時間請安裝 tzdata"
        )


def parse_timestamp(raw: Any, tz: tzinfo) -> datetime:
    """把 ISO 8601 字串轉成 aware datetime；沒有時區資訊者一律套用 tz。"""
    if isinstance(raw, datetime):
        value = raw
    else:
        text = str(raw).strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except ValueError as exc:
            raise OrderError(f"無法解析時間字串：{raw!r}") from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


def to_money(raw: Any, decimal_places: int = 2) -> Decimal:
    """把採購金額轉成 Decimal。

    float 先經 `str()` 再進 Decimal，避免二進位誤差被帶進帳上
    （`Decimal(0.1)` 會是 0.1000000000000000055511151231257827）。
    """
    if isinstance(raw, bool):
        # bool 是 int 的子類，不先擋掉會讓 True 悄悄變成金額 1
        raise OrderError(f"金額不接受布林值：{raw!r}")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    elif isinstance(raw, str):
        try:
            value = Decimal(raw.replace(",", "").strip())
        except InvalidOperation as exc:
            raise OrderError(f"無法解析金額字串：{raw!r}") from exc
    else:
        raise OrderError(f"不支援的金額型別：{type(raw).__name__}")
    if not value.is_finite() or value <= 0:
        raise OrderError(f"採購金額必須為正數且有限：{raw!r}")
    quant = Decimal(1).scaleb(-int(decimal_places))
    return value.quantize(quant, rounding=ROUND_HALF_UP)


def hours_between(start: datetime, end: datetime) -> Decimal:
    """回傳兩個 aware datetime 相差的小時數（Decimal，四捨五入到小數兩位）。"""
    seconds = Decimal(str((end - start).total_seconds()))
    return (seconds / SECONDS_PER_HOUR).quantize(HOURS_QUANT, rounding=ROUND_HALF_UP)


# ── 資料結構 ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Supplier:
    """供應商名冊的一列。domain 必須以 '@' 開頭（見 config.yaml 的說明）。"""

    supplier_id: str
    name: str
    domain: str
    contact: str


@dataclass(frozen=True)
class PurchaseOrder:
    """一張採購單（來源：ERP 匯出）。狀態不存在這裡，一律由郵件事件推導。"""

    po_number: str
    supplier: Supplier
    description: str
    amount: Decimal
    currency: str
    placed_at: datetime
    eta: datetime


@dataclass
class SupplierEvent:
    """一封已成功匹配並分類的供應商回覆。"""

    email_id: str
    po_number: str
    kind: str
    occurred_at: datetime
    subject: str
    revised_eta: datetime | None = None


@dataclass
class OrderTrack:
    """單一 PO 的追蹤結果：目前階段、各階段時間戳、異常紀錄。"""

    order: PurchaseOrder
    stage: str = STAGE_PLACED
    acknowledged_at: datetime | None = None
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    revised_eta: datetime | None = None
    invoice_emails: list[str] = field(default_factory=list)
    delay_notices: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    @property
    def effective_eta(self) -> datetime:
        """催辦要對照的交期：有改期就用改期後的（那是供應商最新的承諾）。"""
        return self.revised_eta or self.order.eta

    @property
    def acknowledgement_hours(self) -> Decimal | None:
        """下單到確認耗時；尚未確認回 None（不可用 0 或門檻值頂替）。"""
        if self.acknowledged_at is None:
            return None
        return hours_between(self.order.placed_at, self.acknowledged_at)

    @property
    def is_delivered_on_time(self) -> bool | None:
        """準時與否一律對照**原始承諾交期**，改期不算準時。

        若改用改期後的日期判定，供應商只要不斷改期就能維持 100% 準時率，
        計分卡就失去談判籌碼的意義。尚未到貨回 None。
        """
        if self.delivered_at is None:
            return None
        return self.delivered_at <= self.order.eta

    def as_dict(self) -> dict[str, Any]:
        """轉成 JSON 可序列化的形狀；金額保留字串以免下游又退回 float。"""
        return {
            "po_number": self.order.po_number,
            "supplier_id": self.order.supplier.supplier_id,
            "supplier_name": self.order.supplier.name,
            "description": self.order.description,
            "amount": str(self.order.amount),
            "currency": self.order.currency,
            "placed_at": self.order.placed_at.isoformat(),
            "eta": self.order.eta.isoformat(),
            "revised_eta": _iso_or_none(self.revised_eta),
            "stage": self.stage,
            "stage_label": STAGE_LABEL[self.stage],
            "acknowledged_at": _iso_or_none(self.acknowledged_at),
            "shipped_at": _iso_or_none(self.shipped_at),
            "delivered_at": _iso_or_none(self.delivered_at),
            "acknowledgement_hours": _str_or_none(self.acknowledgement_hours),
            "is_delivered_on_time": self.is_delivered_on_time,
            "invoice_emails": list(self.invoice_emails),
            "delay_notices": list(self.delay_notices),
            "anomalies": list(self.anomalies),
        }


@dataclass(frozen=True)
class Alert:
    """單一 PO 的逾期／追蹤警報（一張 PO 只會有最嚴重的那一則）。"""

    po_number: str
    supplier_id: str
    supplier_name: str
    kind: str
    severity: int
    hours_late: Decimal
    detail: str

    def as_dict(self) -> dict[str, Any]:
        """轉成 JSON 可序列化的形狀"""
        return {
            "po_number": self.po_number,
            "supplier_id": self.supplier_id,
            "supplier_name": self.supplier_name,
            "kind": self.kind,
            "label": ALERT_LABEL[self.kind],
            "severity": self.severity,
            "hours_late": str(self.hours_late),
            "detail": self.detail,
            "is_overdue": self.kind in OVERDUE_ALERTS,
        }


def _iso_or_none(value: datetime | None) -> str | None:
    """datetime 轉 ISO 字串；None 原樣回傳"""
    return value.isoformat() if value is not None else None


def _str_or_none(value: Decimal | None) -> str | None:
    """Decimal 轉字串；None 原樣回傳（不可用 "0" 頂替，那會謊報已確認）"""
    return str(value) if value is not None else None


# ── 載入採購單 ───────────────────────────────────────────────────────
def build_supplier_registry(entries: Iterable[dict]) -> dict[str, Supplier]:
    """把 config 的 suppliers 區塊轉成 {supplier_id: Supplier}。"""
    registry: dict[str, Supplier] = {}
    for entry in entries:
        supplier_id = str(entry.get("id") or "").strip()
        domain = str(entry.get("domain") or "").strip()
        if not supplier_id:
            raise OrderError(f"供應商缺少 id：{entry!r}")
        if not domain.startswith("@"):
            raise OrderError(f"供應商 {supplier_id} 的 domain 必須以 '@' 開頭，收到 {domain!r}")
        registry[supplier_id] = Supplier(
            supplier_id=supplier_id,
            name=str(entry.get("name") or supplier_id),
            domain=domain.lower(),
            contact=str(entry.get("contact") or ""),
        )
    if not registry:
        raise OrderError("config.yaml 的 suppliers 區塊是空的，無法比對供應商網域")
    return registry


def build_order(record: dict, registry: dict[str, Supplier], tz: tzinfo,
                zero_decimal_currencies: Iterable[str]) -> PurchaseOrder:
    """把單筆 ERP 採購單紀錄轉成 PurchaseOrder。"""
    po_number = str(record.get("po_number") or "").strip().upper()
    supplier_id = str(record.get("supplier_id") or "").strip()
    if not po_number:
        raise OrderError(f"採購單缺少 po_number：{record!r}")
    supplier = registry.get(supplier_id)
    if supplier is None:
        raise OrderError(f"{po_number} 的 supplier_id {supplier_id!r} 不在 config 的供應商名冊中")
    currency = str(record.get("currency") or "").strip().upper()
    if not currency:
        raise OrderError(f"{po_number} 缺少幣別；金額沒有幣別就不能加總，拒絕猜測")
    places = 0 if currency in {c.upper() for c in zero_decimal_currencies} else 2
    return PurchaseOrder(
        po_number=po_number,
        supplier=supplier,
        description=str(record.get("description") or ""),
        amount=to_money(record.get("amount"), places),
        currency=currency,
        placed_at=parse_timestamp(record.get("placed_at"), tz),
        eta=parse_timestamp(record.get("eta"), tz),
    )


def load_orders(path: Path, registry: dict[str, Supplier], tz: tzinfo,
                zero_decimal_currencies: Iterable[str]) -> list[PurchaseOrder]:
    """讀取 ERP 採購單 JSON（mock 模式）。"""
    payload = _read_json(path, "ERP 採購單檔")
    records = payload.get("orders") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise OrderError(f"採購單檔格式錯誤，缺少 orders 陣列：{path}")
    return [build_order(record, registry, tz, zero_decimal_currencies) for record in records]


def fetch_erp_orders(url: str, token: str, timeout: int) -> list[dict]:
    """live 模式：向 ERP（Xero 等）取回未結採購單，回傳與 mock 檔相同形狀的 list。"""
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise OrderError(f"ERP 回傳 HTTP {exc.code}：{url}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise OrderError(f"無法連線 ERP：{url}（{exc}）") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OrderError(f"ERP 回應不是合法 JSON：{url}（{exc}）") from exc
    records = payload.get("orders") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise OrderError(f"ERP 回應缺少 orders 陣列：{url}")
    return records


# ── 狀態機 ───────────────────────────────────────────────────────────
def apply_event(track: OrderTrack, event: SupplierEvent) -> None:
    """把單一郵件事件套進狀態機。只准前進；倒退記為異常並保留原階段。"""
    if event.revised_eta is not None:
        track.revised_eta = event.revised_eta
    if event.kind == EVENT_INVOICE:
        track.invoice_emails.append(event.email_id)
        return
    if event.kind == EVENT_DELAY:
        track.delay_notices.append(event.email_id)
        return
    target = _EVENT_TO_STAGE.get(event.kind)
    if target is None:
        raise OrderError(f"未知的事件種類：{event.kind!r}（郵件 {event.email_id}）")
    if STAGE_RANK[target] < STAGE_RANK[track.stage]:
        track.anomalies.append(
            f"郵件 {event.email_id}（{event.kind}）晚於目前階段 "
            f"{STAGE_LABEL[track.stage]}，已忽略不倒退"
        )
        return
    track.stage = target
    _record_stage_time(track, target, event.occurred_at)


def _record_stage_time(track: OrderTrack, stage: str, occurred_at: datetime) -> None:
    """記錄階段時間戳。已有值就不覆寫——第一次確認才是真正的確認時間。"""
    attribute = _STAGE_TIMESTAMP_FIELD.get(stage)
    if attribute is None:
        return
    if getattr(track, attribute) is None:
        setattr(track, attribute, occurred_at)


def build_tracks(orders: Iterable[PurchaseOrder],
                 events: Iterable[SupplierEvent]) -> list[OrderTrack]:
    """把事件依時間排序後套進各自的 PO，回傳追蹤結果。"""
    tracks = {order.po_number: OrderTrack(order=order) for order in orders}
    for event in sorted(events, key=lambda item: item.occurred_at):
        track = tracks.get(event.po_number)
        if track is None:
            # 理論上不會發生：mailbox 已先確認 PO 存在。留著避免將來重構時靜默丟事件。
            raise OrderError(f"郵件 {event.email_id} 指向不存在的 PO：{event.po_number}")
        apply_event(track, event)
    return list(tracks.values())


# ── 逾期偵測 ─────────────────────────────────────────────────────────
def _check_overdue_delivery(track: OrderTrack, now: datetime, thresholds: dict) -> Alert | None:
    """過了交期加寬限期仍未到貨——最嚴重的一種，生產線隨時可能停。"""
    if track.stage == STAGE_DELIVERED:
        return None
    grace = float(thresholds.get("deliver_after_eta_grace_hours", 24))
    if now <= track.effective_eta + timedelta(hours=grace):
        return None
    late = hours_between(track.effective_eta, now)
    return _make_alert(
        track, ALERT_OVERDUE_DELIVERY, late,
        f"承諾交期 {_fmt(track.effective_eta)} 已過 {late} 小時仍未到貨"
        f"（目前狀態：{STAGE_LABEL[track.stage]}）",
    )


def _check_unacknowledged(track: OrderTrack, now: datetime, thresholds: dict) -> Alert | None:
    """下單後超過門檻仍未收到確認——書中的 UNACKNOWLEDGED_PO_HOURS。"""
    if track.stage != STAGE_PLACED:
        return None
    limit = Decimal(str(thresholds.get("unacknowledged_po_hours", 24)))
    elapsed = hours_between(track.order.placed_at, now)
    if elapsed <= limit:
        return None
    return _make_alert(
        track, ALERT_UNACKNOWLEDGED, elapsed - limit,
        f"下單後 {elapsed} 小時仍未確認（門檻 {limit} 小時）",
    )


def _check_not_shipped(track: OrderTrack, now: datetime, thresholds: dict) -> Alert | None:
    """交期前 N 小時仍未出貨——還沒真的遲到，但已經來不及了。"""
    if STAGE_RANK[track.stage] >= STAGE_RANK[STAGE_SHIPPED]:
        return None
    trigger = track.effective_eta - timedelta(
        hours=float(thresholds.get("ship_before_eta_hours", 72))
    )
    if now < trigger:
        return None
    remaining = hours_between(now, track.effective_eta)
    return _make_alert(
        track, ALERT_NOT_SHIPPED, Decimal("0.00"),
        f"距交期 {_fmt(track.effective_eta)} 剩 {remaining} 小時，"
        f"仍停留在 {STAGE_LABEL[track.stage]}",
    )


def _check_pre_eta(track: OrderTrack, now: datetime, thresholds: dict) -> Alert | None:
    """交期前 48 小時的主動追蹤（書中原文），不是逾期，只是提前確認。"""
    if track.stage != STAGE_SHIPPED:
        return None
    trigger = track.effective_eta - timedelta(
        hours=float(thresholds.get("chase_before_eta_hours", 48))
    )
    if not trigger <= now < track.effective_eta:
        return None
    remaining = hours_between(now, track.effective_eta)
    return _make_alert(
        track, ALERT_PRE_ETA_REMINDER, Decimal("0.00"),
        f"距交期 {_fmt(track.effective_eta)} 剩 {remaining} 小時，"
        f"已出貨，向供應商確認到貨時段",
    )


_ALERT_RULES = (_check_overdue_delivery, _check_unacknowledged, _check_not_shipped, _check_pre_eta)


def evaluate_order(track: OrderTrack, now: datetime, thresholds: dict) -> Alert | None:
    """由重到輕逐條判定，第一個命中就回傳。

    一張 PO 只發最嚴重的那一則：採購人員早上看到 20 行警報時，
    第 15 行寫什麼已經不重要了。
    """
    for rule in _ALERT_RULES:
        alert = rule(track, now, thresholds)
        if alert is not None:
            return alert
    return None


def evaluate_all(tracks: Iterable[OrderTrack], now: datetime, thresholds: dict) -> list[Alert]:
    """對所有 PO 跑逾期判定，依嚴重度遞減、逾期時數遞減排序。"""
    found = (evaluate_order(track, now, thresholds) for track in tracks)
    alerts = [alert for alert in found if alert is not None]
    return sorted(alerts, key=lambda a: (-a.severity, -a.hours_late, a.po_number))


def _make_alert(track: OrderTrack, kind: str, hours_late: Decimal, detail: str) -> Alert:
    """組出 Alert，統一帶入供應商資訊"""
    return Alert(
        po_number=track.order.po_number,
        supplier_id=track.order.supplier.supplier_id,
        supplier_name=track.order.supplier.name,
        kind=kind,
        severity=ALERT_SEVERITY[kind],
        hours_late=hours_late,
        detail=detail,
    )


def _fmt(value: datetime) -> str:
    """報告用的時間格式（含時區偏移，避免跨機器誤讀）"""
    return f"{value:%Y-%m-%d %H:%M %z}"


# ── 金額彙總與計分卡 ─────────────────────────────────────────────────
def totals_by_currency(tracks: Iterable[OrderTrack], *, only_open: bool = True) -> dict[str, str]:
    """依幣別加總採購金額。**不同幣別絕不相加**，沒有匯率就不做換算。"""
    totals: dict[str, Decimal] = {}
    for track in tracks:
        if only_open and track.stage == STAGE_DELIVERED:
            continue
        currency = track.order.currency
        totals[currency] = totals.get(currency, Decimal("0")) + track.order.amount
    return {currency: str(amount) for currency, amount in sorted(totals.items())}


def build_scorecard(tracks: Iterable[OrderTrack], period: str) -> list[dict[str, Any]]:
    """每月供應商計分卡：on-time rate 與 avg acknowledgement time。

    兩個比率在**沒有母體**時一律回 None，不回 0 也不回 100：
    「這個月沒有任何一張到貨」與「這個月每一張都遲到」是完全不同的事。
    """
    grouped: dict[str, list[OrderTrack]] = {}
    for track in tracks:
        grouped.setdefault(track.order.supplier.supplier_id, []).append(track)

    rows = [_scorecard_row(items, period) for items in grouped.values()]
    return sorted(rows, key=lambda row: str(row["supplier_name"]))


def _scorecard_row(items: list[OrderTrack], period: str) -> dict[str, Any]:
    """單一供應商的計分卡列"""
    supplier = items[0].order.supplier
    delivered = [track for track in items if track.delivered_at is not None]
    on_time = [track for track in delivered if track.is_delivered_on_time]
    ack_hours = [
        track.acknowledgement_hours for track in items
        if track.acknowledgement_hours is not None
    ]
    return {
        "period": period,
        "supplier_id": supplier.supplier_id,
        "supplier_name": supplier.name,
        "contact": supplier.contact,
        "orders": len(items),
        "delivered": len(delivered),
        "on_time": len(on_time),
        "on_time_rate": _rate(len(on_time), len(delivered)),
        "acknowledged": len(ack_hours),
        "avg_acknowledgement_hours": _average(ack_hours),
        "open_orders": len(items) - len(delivered),
        "outstanding_by_currency": totals_by_currency(items, only_open=True),
    }


def _rate(numerator: int, denominator: int) -> str | None:
    """百分比字串；分母為 0 回 None（沒有母體就沒有比率）"""
    if denominator <= 0:
        return None
    ratio = Decimal(numerator) * Decimal(100) / Decimal(denominator)
    return str(ratio.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP))


def _average(values: list[Decimal]) -> str | None:
    """平均值字串；空清單回 None"""
    if not values:
        return None
    total = sum(values, Decimal("0"))
    return str((total / Decimal(len(values))).quantize(HOURS_QUANT, rounding=ROUND_HALF_UP))


# ── 狀態檔（催辦紀錄）─────────────────────────────────────────────────
def load_state(path: Path) -> dict[str, Any]:
    """讀取催辦狀態檔；不存在視為首次執行。

    檔案存在但損毀時**拋 OrderError**（不靜默當成首次執行）：
    那會讓冷卻期紀錄整段消失，供應商當天就會收到重複的催辦信。
    """
    if not path.is_file():
        return {"version": STATE_VERSION, "chasers": {}, "processed_email_ids": []}
    payload = _read_json(path, "催辦狀態檔")
    if not isinstance(payload, dict):
        raise OrderError(f"狀態檔格式錯誤，最外層必須是物件：{path}")
    chasers = payload.get("chasers", {})
    if not isinstance(chasers, dict):
        raise OrderError(f"狀態檔的 chasers 欄位必須是物件：{path}")
    processed = payload.get("processed_email_ids", [])
    if not isinstance(processed, list):
        raise OrderError(f"狀態檔的 processed_email_ids 欄位必須是陣列：{path}")
    return {"version": STATE_VERSION, "chasers": chasers, "processed_email_ids": processed}


def save_state(path: Path, chasers: dict[str, Any], processed_email_ids: Iterable[str],
               now: datetime) -> None:
    """寫回催辦狀態檔（含本次的催辦時間與已處理郵件 ID）。"""
    payload = {
        "version": STATE_VERSION,
        "updated_at": now.isoformat(),
        "chasers": chasers,
        "processed_email_ids": sorted(set(processed_email_ids)),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def is_within_cooldown(entry: Any, now: datetime, cooldown_hours: float, tz: tzinfo) -> bool:
    """判斷這張 PO 是否還在催辦冷卻期內（避免每日排程重複轟炸供應商）。"""
    if not isinstance(entry, dict) or not entry.get("last_chased_at"):
        return False
    try:
        last = parse_timestamp(entry["last_chased_at"], tz)
    except OrderError:
        # 狀態檔裡的時間壞掉時寧可再催一次，也不要因為讀不懂而永久停催
        return False
    return now < last + timedelta(hours=float(cooldown_hours))


def _read_json(path: Path, label: str) -> Any:
    """統一的 JSON 讀檔（UTF-8），錯誤訊息帶上絕對路徑與用途。"""
    if not path.is_file():
        raise OrderError(f"找不到{label}：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise OrderError(f"{label}損毀無法解析：{path}（{exc}）") from exc
