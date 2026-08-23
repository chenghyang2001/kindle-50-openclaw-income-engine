"""客戶入職狀態機 + 狀態持久化（第 05 章 #13 的核心）。

入職不是「一次性動作」，是一條**有序、跨 30 天、可中斷可續跑**的流程：

    NEW ──DEAL_CLOSED_WON──► CLOSED_WON ──SEND_WELCOME_PACK──► WELCOME_SENT
        ──SCHEDULE_KICKOFF──► KICKOFF_SCHEDULED ──HOLD_KICKOFF──► KICKOFF_HELD
        ──SEND_DAY7_CHECKIN──► DAY7_DONE ──SEND_DAY30_REVIEW──► DAY30_DONE
        ──COMPLETE──► COMPLETED

只有 `TRANSITIONS` 白名單列出的 `(狀態, 事件)` 組合合法，其餘一律拋
`StateTransitionError`。入職流程的災難幾乎都長成「在錯的階段做了對的事」——
第 7 天的關心信在客戶還沒收到歡迎信之前就寄出去，比不寄還傷。讓它當場爆掉
比事後跟客戶道歉便宜得多。

**冪等性**由 `completed_stages` 負責，而非只靠 `state`：
狀態只記錄「走到哪」，`completed_stages` 記錄「哪些階段確實交付過、何時交付」。
CRM webhook 重送、排程重跑、同一批次出現重複觸發時，靠它擋掉重複寄信與重複建資料夾。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class OnboardingState(Enum):
    """入職流程狀態（順序即為書中 Day 0 -> Day 30 時間軸）"""

    NEW = "new"                                # 尚未收到 CRM webhook
    CLOSED_WON = "closed_won"                  # 交易階段轉為 Closed Won，歡迎包待寄
    WELCOME_SENT = "welcome_sent"              # Day 0 歡迎包已交付
    KICKOFF_SCHEDULED = "kickoff_scheduled"    # 啟動會議預約連結已送出
    KICKOFF_HELD = "kickoff_held"              # 啟動會議已完成
    DAY7_DONE = "day7_done"                    # 第 7 天主動關心已送出
    DAY30_DONE = "day30_done"                  # 第 30 天成效回顧已送出
    COMPLETED = "completed"                    # 入職結案


class OnboardingEvent(Enum):
    """驅動狀態轉移的事件"""

    DEAL_CLOSED_WON = "deal_closed_won"
    SEND_WELCOME_PACK = "send_welcome_pack"
    SCHEDULE_KICKOFF = "schedule_kickoff"
    HOLD_KICKOFF = "hold_kickoff"
    SEND_DAY7_CHECKIN = "send_day7_checkin"
    SEND_DAY30_REVIEW = "send_day30_review"
    COMPLETE = "complete"


class StateTransitionError(RuntimeError):
    """在目前狀態下不允許此事件"""


class DuplicateStageError(RuntimeError):
    """同一階段被要求交付兩次（冪等性防線被踩到）"""


# (目前狀態, 事件) -> 下一狀態。這張表是凍結的白名單，新增階段必須同時改這裡。
TRANSITIONS: dict[tuple[OnboardingState, OnboardingEvent], OnboardingState] = {
    (OnboardingState.NEW, OnboardingEvent.DEAL_CLOSED_WON): OnboardingState.CLOSED_WON,
    (OnboardingState.CLOSED_WON, OnboardingEvent.SEND_WELCOME_PACK): OnboardingState.WELCOME_SENT,
    (OnboardingState.WELCOME_SENT, OnboardingEvent.SCHEDULE_KICKOFF): OnboardingState.KICKOFF_SCHEDULED,
    (OnboardingState.KICKOFF_SCHEDULED, OnboardingEvent.HOLD_KICKOFF): OnboardingState.KICKOFF_HELD,
    (OnboardingState.KICKOFF_HELD, OnboardingEvent.SEND_DAY7_CHECKIN): OnboardingState.DAY7_DONE,
    (OnboardingState.DAY7_DONE, OnboardingEvent.SEND_DAY30_REVIEW): OnboardingState.DAY30_DONE,
    (OnboardingState.DAY30_DONE, OnboardingEvent.COMPLETE): OnboardingState.COMPLETED,
}

# 終態：不再有任何後續動作。COMPLETED 之後客戶進入常態服務，不再由入職流程管理。
TERMINAL_STATES: frozenset[OnboardingState] = frozenset({OnboardingState.COMPLETED})


class ClientOnboarding:
    """單一客戶的入職狀態機。

    context 存放隨附資料（歡迎包摘要、Calendly 連結、負責人指派等），
    completed_stages 是 `階段 key -> 交付時間 ISO 字串`，即冪等性的帳本。
    """

    def __init__(
        self,
        client_id: str,
        company: str,
        contact_email: str = "",
        state: OnboardingState = OnboardingState.NEW,
        completed_stages: dict[str, str] | None = None,
        context: dict[str, Any] | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> None:
        self.client_id = client_id
        self.company = company
        self.contact_email = contact_email
        self.state = state
        self.completed_stages: dict[str, str] = dict(completed_stages or {})
        self.context: dict[str, Any] = dict(context or {})
        self.history: list[dict[str, str]] = list(history or [])

    # ------------------------------------------------------------------ #
    # 冪等性
    # ------------------------------------------------------------------ #
    def is_stage_done(self, stage_key: str) -> bool:
        """此階段是否已交付過（重複觸發時唯一該問的問題）"""
        return stage_key in self.completed_stages

    def stage_done_at(self, stage_key: str) -> str:
        """回傳該階段的交付時間；未交付回空字串。"""
        return self.completed_stages.get(stage_key, "")

    def idempotency_key(self, stage_key: str) -> str:
        """外部系統（Gmail / Slack / Drive）去重用的鍵，同客戶同階段永遠相同。"""
        return f"{self.client_id}:{stage_key}"

    # ------------------------------------------------------------------ #
    # 狀態轉移
    # ------------------------------------------------------------------ #
    def can_apply(self, event: OnboardingEvent) -> bool:
        """此事件在目前狀態下是否合法"""
        return (self.state, event) in TRANSITIONS

    def apply(self, event: OnboardingEvent, **payload: Any) -> OnboardingState:
        """套用事件並回傳新狀態；非法轉移拋 StateTransitionError。"""
        key = (self.state, event)
        if key not in TRANSITIONS:
            raise StateTransitionError(
                f"客戶 {self.client_id}（{self.company}）："
                f"狀態 {self.state.value} 不允許事件 {event.value}"
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

    def mark_stage_done(
        self, stage_key: str, event: OnboardingEvent, at: str, **payload: Any
    ) -> OnboardingState:
        """交付一個階段：先擋重複，再走狀態轉移，最後記進冪等帳本。

        順序刻意是「先擋重複」——狀態機本身也會擋（WELCOME_SENT 不允許再次
        SEND_WELCOME_PACK），但那時拋的是 StateTransitionError，語意是「流程錯亂」；
        重複觸發是**正常且預期**的事件，要用專屬例外區分，呼叫端才知道該靜靜跳過。
        """
        if self.is_stage_done(stage_key):
            raise DuplicateStageError(
                f"客戶 {self.client_id}：階段 {stage_key} 已於 "
                f"{self.completed_stages[stage_key]} 交付過，不可重複執行"
            )
        state = self.apply(event, **payload)
        self.completed_stages[stage_key] = at
        return state

    @property
    def is_terminal(self) -> bool:
        """入職是否已結案"""
        return self.state in TERMINAL_STATES

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        """序列化為可寫入 JSON 的 dict"""
        return {
            "client_id": self.client_id,
            "company": self.company,
            "contact_email": self.contact_email,
            "state": self.state.value,
            "completed_stages": self.completed_stages,
            "context": self.context,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClientOnboarding":
        """由 to_dict() 的輸出還原。未知的 state 值視為資料損毀，明確拋錯。"""
        raw_state = data.get("state", OnboardingState.NEW.value)
        try:
            state = OnboardingState(raw_state)
        except ValueError as exc:
            raise ValueError(f"未知的入職狀態 {raw_state!r}（客戶 {data.get('client_id')}）") from exc
        return cls(
            client_id=data["client_id"],
            company=data.get("company", ""),
            contact_email=data.get("contact_email", ""),
            state=state,
            completed_stages=data.get("completed_stages", {}),
            context=data.get("context", {}),
            history=data.get("history", []),
        )


class OnboardingStore:
    """入職狀態的 JSON 持久化。

    這個檔案就是冪等性的實體憑據：刪了它，所有客戶都會被重新寄一次歡迎信。
    路徑一律由呼叫端以 `Path(__file__).parent` 推算，禁止硬編碼使用者路徑。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, ClientOnboarding]:
        """讀回既有入職紀錄；檔案不存在視為空狀態（首次啟動的正常情況）。"""
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"入職狀態檔損毀或無法讀取：{self.path}（{exc}）") from exc
        if not isinstance(raw, dict):
            raise RuntimeError(f"入職狀態檔頂層必須是 mapping：{self.path}")
        return {
            key: ClientOnboarding.from_dict(value)
            for key, value in raw.get("clients", {}).items()
        }

    def save(self, clients: dict[str, ClientOnboarding]) -> Path:
        """寫回狀態檔，回傳實際寫入路徑。"""
        payload = {
            "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "clients": {key: item.to_dict() for key, item in clients.items()},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.path
