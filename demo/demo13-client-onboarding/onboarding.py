"""入職階段定義、資料完整性檢查、訊息組裝與財務模型。

與 `state_machine.py` 的分工：
- `state_machine.py` 只管「合不合法、有沒有做過」（狀態 + 冪等帳本）
- 本模組管「該不該現在做、資料夠不夠做、做出來長什麼樣」

`ONBOARDING_SEQUENCE`（書中原名）完全由 `config.yaml` 的 `onboarding.sequence`
驅動，程式碼不內嵌任何階段常數——代理商幫不同客戶部署時只改 YAML，不動 .py。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from state_machine import OnboardingEvent

# 階段 key -> 狀態機事件。config.yaml 只能使用這裡登記過的 key，
# 打錯字會在啟動時就炸掉，而不是等到第 30 天才發現有個階段從來沒跑過。
STAGE_EVENTS: dict[str, OnboardingEvent] = {
    "welcome_pack": OnboardingEvent.SEND_WELCOME_PACK,
    "kickoff_booking": OnboardingEvent.SCHEDULE_KICKOFF,
    "kickoff_meeting": OnboardingEvent.HOLD_KICKOFF,
    "day7_checkin": OnboardingEvent.SEND_DAY7_CHECKIN,
    "day30_review": OnboardingEvent.SEND_DAY30_REVIEW,
}

_CENTS = Decimal("0.01")


class StageOutcome(Enum):
    """單一階段這一輪的處理結果"""

    SENT = "sent"                    # 本輪實際交付
    SKIPPED_DONE = "skipped_done"    # 先前已交付，冪等跳過
    PENDING = "pending"              # 時間未到 或 前置階段未完成
    BLOCKED = "blocked"              # 客戶資料缺漏，無法進行（必須明確標示原因）


@dataclass(frozen=True)
class StageSpec:
    """一個入職階段的規格（由 config.yaml 反序列化而來）"""

    key: str
    name: str
    day: int
    owner_role: str
    required_fields: tuple[str, ...]
    deliverables: tuple[str, ...]
    templates: tuple[str, ...]

    @property
    def event(self) -> OnboardingEvent:
        """本階段對應的狀態機事件"""
        return STAGE_EVENTS[self.key]


@dataclass
class StageResult:
    """單一階段的處理結果，可直接序列化進報表。"""

    stage_key: str
    stage_name: str
    stage_day: int
    outcome: StageOutcome
    reason: str
    owner: str = ""
    idempotency_key: str = ""
    missing_fields: tuple[str, ...] = ()
    messages: list[str] = field(default_factory=list)
    delivery: str = ""

    def to_dict(self) -> dict[str, Any]:
        """轉成純 JSON 型別（Enum 一律轉 value）"""
        return {
            "stage_key": self.stage_key,
            "stage_name": self.stage_name,
            "stage_day": self.stage_day,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "owner": self.owner,
            "idempotency_key": self.idempotency_key,
            "missing_fields": list(self.missing_fields),
            "messages": list(self.messages),
            "delivery": self.delivery,
        }


class SequenceConfigError(ValueError):
    """config.yaml 的 onboarding.sequence 設定有誤"""


# --------------------------------------------------------------------------- #
# 設定載入
# --------------------------------------------------------------------------- #
def load_sequence(config: dict[str, Any]) -> tuple[StageSpec, ...]:
    """把 config 的 onboarding.sequence 轉成凍結的 StageSpec 序列。

    順序即流程順序，且必須嚴格遞增——Day 30 排在 Day 7 前面是設定錯誤，
    不是「彈性」，因為狀態機的轉移表已經把順序寫死了。
    """
    raw_stages = config.get("onboarding", {}).get("sequence") or []
    if not raw_stages:
        raise SequenceConfigError("onboarding.sequence 不可為空，至少要有一個入職階段")
    stages = tuple(_build_stage(item) for item in raw_stages)
    _verify_order(stages)
    return stages


def _build_stage(item: dict[str, Any]) -> StageSpec:
    """單一階段的反序列化 + key 合法性檢查。"""
    key = str(item.get("key", "")).strip()
    if key not in STAGE_EVENTS:
        raise SequenceConfigError(
            f"未登記的入職階段 key {key!r}，可用：{', '.join(STAGE_EVENTS)}"
        )
    return StageSpec(
        key=key,
        name=str(item.get("name", key)),
        day=int(item.get("day", 0)),
        owner_role=str(item.get("owner_role", "")),
        required_fields=tuple(item.get("required_fields") or ()),
        deliverables=tuple(item.get("deliverables") or ()),
        templates=tuple(item.get("templates") or ()),
    )


def _verify_order(stages: tuple[StageSpec, ...]) -> None:
    """階段順序必須與狀態機轉移表一致，且 day 不可倒退。"""
    expected = [key for key in STAGE_EVENTS if key in {stage.key for stage in stages}]
    actual = [stage.key for stage in stages]
    if actual != expected:
        raise SequenceConfigError(
            f"階段順序與狀態機不符：設定為 {actual}，狀態機要求 {expected}"
        )
    for previous, current in zip(stages, stages[1:]):
        if current.day < previous.day:
            raise SequenceConfigError(
                f"階段 {current.key} 的 day={current.day} 早於前一階段 "
                f"{previous.key} 的 day={previous.day}"
            )


# --------------------------------------------------------------------------- #
# 客戶資料存取與完整性檢查
# --------------------------------------------------------------------------- #
def field_value(client: dict[str, Any], dotted_path: str) -> Any:
    """以 "account_manager.email" 這種點記法取值；任何一層缺失都回 None。"""
    current: Any = client
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def missing_required(client: dict[str, Any], required: tuple[str, ...]) -> tuple[str, ...]:
    """回傳缺漏的欄位清單。

    空字串與只有空白的字串一律視為缺漏：CRM 匯入時常見「欄位存在但沒填」，
    若當成有值，寄出去的就是「親愛的 ，你好」這種當場毀掉信任的信。
    """
    gaps: list[str] = []
    for path in required:
        value = field_value(client, path)
        if value is None or (isinstance(value, str) and not value.strip()):
            gaps.append(path)
    return tuple(gaps)


def days_elapsed(closed_won_at: str, now: datetime) -> int:
    """自成交起算的天數（無條件捨去）。時間格式錯誤要明確報錯，不可當成 Day 0。"""
    try:
        started = datetime.fromisoformat(closed_won_at)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"無法解析成交時間 {closed_won_at!r}：{exc}") from exc
    return max(0, (now - started).days)


def seconds_since(closed_won_at: str, now: datetime) -> int:
    """自成交起算的秒數，用來驗證書中「60 秒內寄出歡迎包」的 SLA。"""
    started = datetime.fromisoformat(closed_won_at)
    return max(0, int((now - started).total_seconds()))


# --------------------------------------------------------------------------- #
# 樣板渲染
# --------------------------------------------------------------------------- #
class _TrackingContext(dict):
    """format_map 專用：未提供的佔位符保留原樣並記錄下來。

    刻意不換成空字串——「{contact_name} 你好」比「 你好」容易在測試與人工
    抽查時被抓到，靜默的空白才是真正會寄出去的那一種錯。
    """

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(data)
        self.unresolved: list[str] = []

    def __missing__(self, key: str) -> str:
        if key not in self.unresolved:
            self.unresolved.append(key)
        return "{" + key + "}"


def render(template: str, context: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """渲染樣板，回傳 (文字, 未解析的佔位符)。"""
    tracker = _TrackingContext(context)
    return template.format_map(tracker), tuple(tracker.unresolved)


def kickoff_link(config: dict[str, Any], client: dict[str, Any]) -> str:
    """組出對應負責人的 Calendly 啟動會議連結（書中：串接 Calendly 消除排程摩擦）。

    帶上 name / email 參數讓客戶不必再填一次基本資料——排程摩擦有一半來自
    「還要我打字」。負責人 slug 缺失時回空字串，由必填欄位檢查負責擋下。
    """
    scheduling = config.get("scheduling", {})
    slug = field_value(client, "account_manager.calendly_slug") or ""
    if not slug:
        return ""
    base = str(scheduling.get("calendly_base_url", "")).rstrip("/")
    event = str(scheduling.get("kickoff_event_slug", ""))
    name = str(client.get("contact_name", ""))
    email = str(client.get("contact_email", ""))
    return f"{base}/{slug}/{event}?name={name}&email={email}"


def build_timeline(stages: tuple[StageSpec, ...]) -> str:
    """把 ONBOARDING_SEQUENCE 攤成客戶看得懂的時間軸文字（放進歡迎手冊）。"""
    return "\n".join(f"  Day {stage.day}：{stage.name}" for stage in stages)


def build_template_context(
    config: dict[str, Any],
    stages: tuple[StageSpec, ...],
    stage: StageSpec,
    client: dict[str, Any],
    delivery_label: str,
    autonomy_level: str,
) -> dict[str, Any]:
    """組出所有樣板共用的替換字典（WELCOME_PACK_TEMPLATE 的動態插入來源）。"""
    scheduling = config.get("scheduling", {})
    notifications = config.get("notifications", {})
    return {
        "agency_name": notifications.get("agency_name", ""),
        "company": client.get("company", ""),
        "contact_name": client.get("contact_name", ""),
        "contact_email": client.get("contact_email", ""),
        "plan": client.get("plan", ""),
        "closed_won_at": client.get("closed_won_at", ""),
        "sales_notes": client.get("sales_notes", ""),
        "kickoff_booked_at": client.get("kickoff_booked_at", "（尚未預約）"),
        "am_name": field_value(client, "account_manager.name") or "",
        "am_email": field_value(client, "account_manager.email") or "",
        "kickoff_link": kickoff_link(config, client) or "（負責人尚未設定 Calendly）",
        "kickoff_minutes": scheduling.get("kickoff_duration_minutes", 30),
        "slack_channel": notifications.get("slack_channel", ""),
        "onboarding_days": stages[-1].day if stages else 0,
        "timeline": build_timeline(stages),
        "stage_name": stage.name,
        "stage_day": stage.day,
        "deliverables": "\n".join(f"  - {item}" for item in stage.deliverables),
        "delivery_label": delivery_label,
        "autonomy_level": autonomy_level,
    }


def render_stage_messages(
    templates: dict[str, str], stage: StageSpec, context: dict[str, Any]
) -> tuple[list[tuple[str, str]], tuple[str, ...]]:
    """渲染一個階段要送出的所有訊息，回傳 ((樣板名稱, 訊息文字) 清單, 未解析佔位符)。

    保留樣板名稱是因為同一個 stage 底下可能同時有客戶信與內部通知（例如
    welcome_pack stage 的 welcome_pack + slack_new_client），下游的 LLM
    潤飾必須知道自己在潤飾哪一則，才不會把內部通知誤套用客戶語氣改寫。
    """
    messages: list[tuple[str, str]] = []
    unresolved: list[str] = []
    for name in stage.templates:
        template = templates.get(name)
        if template is None:
            raise SequenceConfigError(f"階段 {stage.key} 指定的樣板 {name!r} 不存在於 templates")
        text, gaps = render(template, context)
        messages.append((name, text.strip()))
        unresolved.extend(gap for gap in gaps if gap not in unresolved)
    return messages, tuple(unresolved)


# --------------------------------------------------------------------------- #
# 財務模型
# --------------------------------------------------------------------------- #
def _to_decimal(raw: Any, label: str) -> Decimal:
    """設定值轉 Decimal。金額全程禁用 float——尾差會累積成對不上的帳。"""
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"設定 {label} 不是合法數值：{raw!r}") from exc


def financial_summary(module_cfg: dict[str, Any]) -> dict[str, str]:
    """依 config 的定價與回收工時算出客戶端 ROI。金額一律 Decimal，輸出為字串。"""
    setup = _to_decimal(module_cfg.get("client_setup_price"), "client_setup_price")
    monthly = _to_decimal(module_cfg.get("client_monthly_price"), "client_monthly_price")
    hourly = _to_decimal(module_cfg.get("hourly_rate"), "hourly_rate")
    per_client = _to_decimal(module_cfg.get("recovered_hours_per_client"), "recovered_hours_per_client")
    clients = _to_decimal(module_cfg.get("new_clients_per_month"), "new_clients_per_month")

    monthly_hours = per_client * clients
    monthly_value = (monthly_hours * hourly).quantize(_CENTS)
    first_month_net = (monthly_value - setup - monthly).quantize(_CENTS)
    year_one_net = (monthly_value * 12 - setup - monthly * 12).quantize(_CENTS)
    return {
        "client_setup_price": str(setup.quantize(_CENTS)),
        "client_monthly_price": str(monthly.quantize(_CENTS)),
        "recovered_hours_per_month": str(monthly_hours.quantize(_CENTS)),
        "recovered_value_per_month": str(monthly_value),
        "first_month_net": str(first_month_net),
        "year_one_net": str(year_one_net),
        "payback_months": _payback_months(setup, monthly_value, monthly),
    }


def _payback_months(setup: Decimal, monthly_value: Decimal, monthly_fee: Decimal) -> str:
    """回收期＝設置費 ÷ 每月淨效益。淨效益 <= 0 時不硬算，明說「無法回收」。"""
    net_per_month = monthly_value - monthly_fee
    if net_per_month <= 0:
        return "無法回收（每月回收價值未超過月費）"
    return str((setup / net_per_month).quantize(_CENTS))
