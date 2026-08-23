"""預約對話狀態機。

書中第 03 章的核心：把「5-12 條訊息的來回」壓成一條可驗證的狀態路徑。

    INQUIRY ──► SLOTS_OFFERED ──► SLOT_SELECTED ──► CONFIRMED
                    ▲   ▲                │              │
                    │   └────────────────┘              │
                    │        SLOT_TAKEN（樂觀鎖擋下）     │
                    │                                   ▼
                    └──────── OFFER_SLOTS ──── RESCHEDULE_REQUESTED

只有 TRANSITIONS 表列出的 (狀態, 事件) 組合是合法的；其餘一律拋
StateTransitionError。這是刻意的——排程流程的 bug 幾乎都長成「在錯的
狀態做了對的事」，讓它在第一時間爆掉比事後對帳便宜得多。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class BookingState(Enum):
    """對話狀態"""

    INQUIRY = "inquiry"                          # 收到詢問，尚未提供時段
    SLOTS_OFFERED = "slots_offered"              # 已提供 N 個時段，等待客戶選擇
    SLOT_SELECTED = "slot_selected"              # 客戶已選定，尚未寫入日曆
    CONFIRMED = "confirmed"                      # 已寫入日曆並發出確認
    RESCHEDULE_REQUESTED = "reschedule_requested"  # 已確認的預約被要求改期


class BookingEvent(Enum):
    """驅動狀態轉移的事件"""

    RECEIVE_INQUIRY = "receive_inquiry"
    OFFER_SLOTS = "offer_slots"
    SELECT_SLOT = "select_slot"
    CONFIRM = "confirm"
    SLOT_TAKEN = "slot_taken"                # 樂觀鎖衝突：時段剛被別人訂走
    REQUEST_RESCHEDULE = "request_reschedule"


class StateTransitionError(RuntimeError):
    """在目前狀態下不允許此事件"""


# (目前狀態, 事件) -> 下一狀態
TRANSITIONS: dict[tuple[BookingState, BookingEvent], BookingState] = {
    (BookingState.INQUIRY, BookingEvent.RECEIVE_INQUIRY): BookingState.INQUIRY,
    (BookingState.INQUIRY, BookingEvent.OFFER_SLOTS): BookingState.SLOTS_OFFERED,
    (BookingState.SLOTS_OFFERED, BookingEvent.OFFER_SLOTS): BookingState.SLOTS_OFFERED,
    (BookingState.SLOTS_OFFERED, BookingEvent.SELECT_SLOT): BookingState.SLOT_SELECTED,
    (BookingState.SLOT_SELECTED, BookingEvent.CONFIRM): BookingState.CONFIRMED,
    # 衝突後直接退回 SLOTS_OFFERED——重新提供 3 個時段是同一個動作的一部分
    (BookingState.SLOT_SELECTED, BookingEvent.SLOT_TAKEN): BookingState.SLOTS_OFFERED,
    (BookingState.CONFIRMED, BookingEvent.REQUEST_RESCHEDULE): BookingState.RESCHEDULE_REQUESTED,
    (BookingState.RESCHEDULE_REQUESTED, BookingEvent.OFFER_SLOTS): BookingState.SLOTS_OFFERED,
}

# 已無後續動作的終態（僅供報表判讀，CONFIRMED 仍可因改期離開）
TERMINAL_STATES: frozenset[BookingState] = frozenset()


class ConversationStateMachine:
    """單一對話的狀態機。context 存放時段、預約與日曆版本等隨附資料。"""

    def __init__(
        self,
        conversation_id: str,
        customer: str,
        handle: str,
        state: BookingState = BookingState.INQUIRY,
        context: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.customer = customer
        self.handle = handle
        self.state = state
        self.context: dict[str, Any] = context if context is not None else {}
        self.history: list[dict[str, str]] = history if history is not None else []

    def can_apply(self, event: BookingEvent) -> bool:
        """此事件在目前狀態下是否合法"""
        return (self.state, event) in TRANSITIONS

    def apply(self, event: BookingEvent, **payload: Any) -> BookingState:
        """套用事件並回傳新狀態；非法轉移拋 StateTransitionError。"""
        key = (self.state, event)
        if key not in TRANSITIONS:
            raise StateTransitionError(
                f"對話 {self.conversation_id}：狀態 {self.state.value} 不允許事件 {event.value}"
            )
        previous = self.state
        self.state = TRANSITIONS[key]
        self.context.update(payload)
        self.history.append(
            {
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event.value,
                "from": previous.value,
                "to": self.state.value,
            }
        )
        return self.state

    def to_dict(self) -> dict[str, Any]:
        """序列化為可寫入 JSON 的 dict"""
        return {
            "conversation_id": self.conversation_id,
            "customer": self.customer,
            "handle": self.handle,
            "state": self.state.value,
            "context": self.context,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationStateMachine":
        """由 to_dict() 的輸出還原"""
        return cls(
            conversation_id=data["conversation_id"],
            customer=data.get("customer", ""),
            handle=data.get("handle", ""),
            state=BookingState(data.get("state", BookingState.INQUIRY.value)),
            context=data.get("context", {}),
            history=data.get("history", []),
        )


class ConversationStore:
    """對話狀態的 JSON 持久化。路徑一律由呼叫端以 Path(__file__).parent 推算。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, ConversationStateMachine]:
        """讀回既有對話；檔案不存在視為空狀態（首次啟動的正常情況）。"""
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"狀態檔損毀或無法讀取：{self.path}（{exc}）") from exc
        return {
            key: ConversationStateMachine.from_dict(value)
            for key, value in raw.get("conversations", {}).items()
        }

    def save(self, machines: dict[str, ConversationStateMachine]) -> Path:
        """寫回狀態檔，回傳實際寫入路徑。"""
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "conversations": {key: m.to_dict() for key, m in machines.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.path
