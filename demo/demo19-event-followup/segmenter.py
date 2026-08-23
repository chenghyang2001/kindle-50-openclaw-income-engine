"""活動跟進分眾狀態機：分群判定、到期判定、發送前複查、進度持久化。

本模組刻意與 `main.py` 分離，因為「與會者已回覆 / 已退訂就中止」這兩條規則是
整個模組的安全核心，必須能被單獨測試，不受 LLM / 通知管道 / 設定載入的干擾。

與 demo10（B2B 提案跟進）的關鍵差異：
    demo10 的對象是單一已知交易對手，序列只有一條；
    本模組面對的是「一場活動的整份名單」，意向未明，因此**先分群再排序列**，
    每一群走完全不同的節奏與訴求。分群錯了，後面寫得再好都是打擾。

設計上的四個關鍵決定：

1. `stop_on_reply` 與 `respect_unsubscribe` 在建構子被強制為 `True`。
   呼叫端傳 `False` 不會生效，只會在 `forced_overrides` 留下紀錄供上層發 AMBER。
   理由：誤發跟進給已回覆的人會摧毀信任；忽視退訂則直接是法遵事故。
   兩者都不可逆，不能交給設定檔決定。
2. 到期判定（`plan`）與發送前複查（`assert_can_send`）是**兩道獨立的閘門**。
   排程當下沒回覆，不代表輪到實際送出時還沒回覆——中間隔著 LLM 生成時間。
   活動名單動輒數百人，這段空窗比 demo10 的單一對象長得多，風險也更高。
3. 分群依 config 中 `segments` 的**順序**比對，第一個命中者勝出。
   因此 hot 必須排在 warm 前面；順序即優先權，不另設分數。
4. 到期時間以「活動結束時間 + N 分鐘」計算。用分鐘而非天，是因為書中規格
   要求高熱度名單在活動結束 **30 分鐘內**交棒業務——用天為單位表達不了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 狀態檔預設放在模組目錄下，禁止硬編碼使用者路徑
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = MODULE_DIR / "state" / "event_state.json"

# 已結案狀態：一律不再推進序列
CLOSED_STATUSES = ("replied", "unsubscribed", "bounced", "converted", "opted_out")

# 中止原因（對外回報用的穩定字串鍵，測試會直接斷言）
HALT_REPLIED = "replied"
HALT_UNSUBSCRIBED = "unsubscribed"
HALT_STATUS_CLOSED = "status_closed"
HALT_SEQUENCE_COMPLETE = "sequence_complete"
HALT_NOT_DUE = "not_due"
HALT_BAD_DATA = "bad_data"
HALT_NO_SEGMENT = "no_segment"

ACTION_SEND = "send"
ACTION_HALT = "halt"


class SegmentError(ValueError):
    """分群設定或與會者資料格式錯誤。"""


class SequenceHalted(RuntimeError):
    """發送前複查判定必須中止（已回覆或已退訂）。"""

    def __init__(self, attendee_id: str, reason: str, detail: str) -> None:
        super().__init__(f"[{attendee_id}] {reason}：{detail}")
        self.attendee_id = attendee_id
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FollowUpStep:
    """序列中的一段跟進（時間基準為活動結束時間）。"""

    offset_minutes: int
    type: str
    prompt: str

    @classmethod
    def from_config(cls, raw: object) -> "FollowUpStep":
        """從 config 的 sequence 項目建立，欄位缺失即拋錯而非給預設值。"""
        if not isinstance(raw, dict):
            raise SegmentError(f"sequence 項目必須是 mapping，收到 {type(raw).__name__}")
        try:
            offset = int(raw["offset_minutes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SegmentError(f"sequence 項目缺少合法的 offset_minutes：{raw!r}") from exc
        step_type = str(raw.get("type") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not step_type or not prompt:
            raise SegmentError(f"sequence 項目缺少 type 或 prompt：{raw!r}")
        if offset <= 0:
            raise SegmentError(f"offset_minutes 必須為正整數，收到 {offset}")
        return cls(offset_minutes=offset, type=step_type, prompt=prompt)

    def as_dict(self) -> dict:
        """轉成可 JSON 序列化的 dict。"""
        return {
            "offset_minutes": self.offset_minutes,
            "type": self.type,
            "prompt": self.prompt,
        }


@dataclass(frozen=True)
class SegmentRule:
    """一個分群的判定條件與專屬序列。"""

    key: str
    label: str
    min_attendance_pct: float
    requires_engagement_signal: bool
    is_crm_handover: bool
    steps: tuple[FollowUpStep, ...]

    @classmethod
    def from_config(cls, raw: object) -> "SegmentRule":
        """從 config 的 segments 項目建立。"""
        if not isinstance(raw, dict):
            raise SegmentError(f"segments 項目必須是 mapping，收到 {type(raw).__name__}")
        key = str(raw.get("key") or "").strip()
        if not key:
            raise SegmentError(f"segments 項目缺少 key：{raw!r}")
        steps = _build_steps(raw.get("sequence"), key)
        try:
            threshold = float(raw.get("min_attendance_pct", 0))
        except (TypeError, ValueError) as exc:
            raise SegmentError(f"{key} 的 min_attendance_pct 非數字：{raw!r}") from exc
        return cls(
            key=key,
            label=str(raw.get("label") or key),
            min_attendance_pct=threshold,
            requires_engagement_signal=bool(raw.get("requires_engagement_signal")),
            is_crm_handover=bool(raw.get("is_crm_handover")),
            steps=tuple(steps),
        )


def _build_steps(raw_sequence: object, segment_key: str) -> list[FollowUpStep]:
    """把某分群的 sequence 區段轉成 FollowUpStep 清單。"""
    if not isinstance(raw_sequence, (list, tuple)) or not raw_sequence:
        raise SegmentError(f"分群 {segment_key} 的 sequence 必須是非空清單")
    steps = [FollowUpStep.from_config(item) for item in raw_sequence]
    offsets = [step.offset_minutes for step in steps]
    if len(set(offsets)) != len(offsets):
        raise SegmentError(f"分群 {segment_key} 的 offset_minutes 不可重複：{offsets}")
    return sorted(steps, key=lambda item: item.offset_minutes)


def build_segments(raw_segments: object) -> list[SegmentRule]:
    """把 config.yaml 的 segmentation.segments 轉成 SegmentRule 清單。

    刻意**不排序**：比對順序就是分群優先權，由設定檔的書寫順序決定。
    """
    if not isinstance(raw_segments, (list, tuple)) or not raw_segments:
        raise SegmentError("config.yaml 的 segmentation.segments 必須是非空清單")
    rules = [SegmentRule.from_config(item) for item in raw_segments]
    keys = [rule.key for rule in rules]
    if len(set(keys)) != len(keys):
        raise SegmentError(f"segments 的 key 不可重複：{keys}")
    return rules


def resolve_timezone(
    name: str,
    fallback_offset_hours: int = 8,
) -> tuple[tzinfo, str | None]:
    """取得時區物件，取不到就退回固定偏移。

    回傳 `(tzinfo, 警告訊息或 None)`。Windows 與精簡容器沒有系統 IANA 時區
    資料庫，`zoneinfo` 需要額外的 tzdata 套件；本專案第三方依賴只允許 PyYAML，
    因此改為明確降級 + 警告，而不是讓整支程式在啟動時就掛掉。
    """
    tz_name = str(name or "").strip()
    if not tz_name:
        return dt_timezone(timedelta(hours=fallback_offset_hours)), None
    try:
        return ZoneInfo(tz_name), None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        warning = (
            f"找不到時區資料庫項目 {tz_name!r}，已退回固定 UTC+{fallback_offset_hours} 偏移"
        )
        return dt_timezone(timedelta(hours=fallback_offset_hours)), warning


def parse_iso(value: object, tz: tzinfo) -> datetime:
    """解析 ISO 8601 字串成帶時區的 datetime；無時區者視為 tz 當地時間。"""
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise SegmentError(f"無法解析時間字串：{value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _coerce_offsets(raw: object, context: str) -> set[int]:
    """把 steps_sent 之類的清單轉成 int 集合，格式錯誤即拋錯。"""
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple, set)):
        raise SegmentError(f"{context} 必須是清單，收到 {type(raw).__name__}")
    try:
        return {int(item) for item in raw}
    except (TypeError, ValueError) as exc:
        raise SegmentError(f"{context} 含非整數項目：{raw!r}") from exc


class EventState:
    """跟進進度的持久化狀態：誰收過哪幾段、誰已交棒業務。

    `is_enabled=False`（`--mock` 預設）時完全在記憶體運作，既不讀也不寫，
    讓 `--mock` 每次執行的結果都一模一樣，QA 可重複驗證。
    指定 `--state-file` 或跑 `--live` 才會啟用檔案 I/O。
    """

    def __init__(
        self,
        path: Path | None = None,
        event_id: str = "",
        is_enabled: bool = False,
        is_writable: bool = False,
    ) -> None:
        self._path = Path(path) if path is not None else DEFAULT_STATE_PATH
        self._event_id = str(event_id or "")
        self._is_enabled = bool(is_enabled)
        self._is_writable = bool(is_writable) and self._is_enabled
        self._sent: dict[str, set[int]] = {}
        self._handover: set[str] = set()
        if self._is_enabled:
            self._load()

    @property
    def path(self) -> Path:
        """狀態檔絕對路徑。"""
        return self._path

    @property
    def is_enabled(self) -> bool:
        """是否啟用檔案 I/O。"""
        return self._is_enabled

    @property
    def is_writable(self) -> bool:
        """是否會寫回磁碟（`--dry-run` 時為 False）。"""
        return self._is_writable

    def _load(self) -> None:
        """讀取既有狀態檔；檔案損毀要明確報錯，不可靜默當成空狀態。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SegmentError(f"狀態檔無法讀取或解析：{self._path}") from exc
        if not isinstance(raw, dict):
            raise SegmentError(f"狀態檔格式錯誤（應為 object）：{self._path}")
        self._assert_same_event(raw.get("event_id"))
        sent = raw.get("sent") or {}
        if not isinstance(sent, dict):
            raise SegmentError(f"狀態檔的 sent 欄位應為 object：{self._path}")
        for attendee_id, offsets in sent.items():
            self._sent[str(attendee_id)] = _coerce_offsets(
                offsets, f"狀態檔 {attendee_id}"
            )
        self._handover = {str(item) for item in (raw.get("crm_handover") or [])}

    def _assert_same_event(self, stored_event_id: object) -> None:
        """狀態檔屬於另一場活動時直接拒絕，避免跨活動污染而漏發。"""
        stored = str(stored_event_id or "")
        if stored and self._event_id and stored != self._event_id:
            raise SegmentError(
                f"狀態檔屬於活動 {stored}，與本次活動 {self._event_id} 不符："
                f"{self._path}（請改用 --state-file 指定另一個檔案）"
            )

    def sent_offsets(self, attendee_id: str) -> set[int]:
        """回傳該與會者已送出的 offset_minutes 集合。"""
        return set(self._sent.get(str(attendee_id), set()))

    def has_handover(self, attendee_id: str) -> bool:
        """該與會者是否已交棒給業務。"""
        return str(attendee_id) in self._handover

    def mark_sent(self, attendee_id: str, offset_minutes: int) -> None:
        """標記某段已送出，`is_writable=True` 時立即寫回磁碟。"""
        self._sent.setdefault(str(attendee_id), set()).add(int(offset_minutes))
        self._flush()

    def mark_handover(self, attendee_id: str) -> None:
        """標記已交棒業務，避免同一人被重複推進 Slack。"""
        self._handover.add(str(attendee_id))
        self._flush()

    def _flush(self) -> None:
        """可寫時才落地，其餘情況純記憶體。"""
        if self._is_writable:
            self.save()

    def save(self) -> None:
        """寫回狀態檔（含建立父目錄）。"""
        payload = {
            "event_id": self._event_id,
            "sent": {key: sorted(value) for key, value in self._sent.items()},
            "crm_handover": sorted(self._handover),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            raise SegmentError(f"狀態檔寫入失敗：{self._path}") from exc


class EventFollowUpSequencer:
    """活動後跟進的分眾狀態機。"""

    def __init__(
        self,
        segments: Sequence[SegmentRule],
        tz: tzinfo,
        event_ended_at: datetime,
        stop_on_reply: bool = True,
        respect_unsubscribe: bool = True,
        state: EventState | None = None,
        engagement_signals: Sequence[str] | None = None,
        handover_deadline_minutes: int = 30,
    ) -> None:
        if not segments:
            raise SegmentError("segments 不可為空，至少要有一個分群")
        self._segments = tuple(segments)
        self._tz = tz
        self._event_ended_at = event_ended_at
        self._state = state if state is not None else EventState(is_enabled=False)
        self._signals = tuple(engagement_signals or ("asked_question",))
        self._handover_deadline_minutes = int(handover_deadline_minutes)
        self.forced_overrides: list[str] = []
        # 硬規則：不論外部傳什麼，一律 True。只留下紀錄供上層發 AMBER。
        self._stop_on_reply = self._force_true(stop_on_reply, "stop_on_reply")
        self._respect_unsubscribe = self._force_true(
            respect_unsubscribe, "respect_unsubscribe"
        )

    def _force_true(self, value: object, name: str) -> bool:
        """把不可停用的安全開關強制成 True，並記錄被竄改的事實。"""
        if value is not True:
            self.forced_overrides.append(
                f"{name} 被要求設為 {value!r}，已強制覆寫為 True"
            )
        return True

    @property
    def segments(self) -> tuple[SegmentRule, ...]:
        """依設定順序排列的分群規則。"""
        return self._segments

    @property
    def state(self) -> EventState:
        """底層進度狀態。"""
        return self._state

    @property
    def is_stop_on_reply_enabled(self) -> bool:
        """永遠為 True——這是本模組不可停用的硬規則。"""
        return self._stop_on_reply

    @property
    def is_unsubscribe_respected(self) -> bool:
        """永遠為 True——退訂同樣不可停用。"""
        return self._respect_unsubscribe

    @property
    def handover_deadline(self) -> datetime:
        """CRM 交棒的最後期限＝活動結束時間 + 設定的分鐘數。"""
        return self._event_ended_at + timedelta(
            minutes=self._handover_deadline_minutes
        )

    # ------------------------------------------------------------------
    # 分群判定
    # ------------------------------------------------------------------
    def attendance_pct(self, attendee: dict) -> float:
        """取得出席比例（0-100）；缺值視為 0（僅報名未出席）。"""
        raw = attendee.get("attendance_pct")
        if raw is None:
            return 0.0
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise SegmentError(
                f"{attendee.get('id')!r} 的 attendance_pct 非數字：{raw!r}"
            ) from exc

    def has_engagement_signal(self, attendee: dict) -> bool:
        """是否有任一高互動訊號（發問 / 下載資料 …）。"""
        for signal in self._signals:
            if bool(attendee.get(signal)):
                return True
        return bool(attendee.get("questions"))

    def classify(self, attendee: dict) -> SegmentRule | None:
        """依設定順序比對分群，第一個命中者勝出；都不命中回 None。

        出席率 >= 80 但沒有互動訊號的人會落到 warm——書中把 Fully Engaged
        定義為「出席 >80% **且** 發問」，未定義「高出席但沉默」該去哪。
        本實作讓它自然落入下一個門檻較低的分群，而不是另立第四群，
        理由：這種人的意向強度確實介於兩者之間，硬給 hot 會浪費業務人力。
        """
        pct = self.attendance_pct(attendee)
        for rule in self._segments:
            if pct < rule.min_attendance_pct:
                continue
            if rule.requires_engagement_signal and not self.has_engagement_signal(
                attendee
            ):
                continue
            return rule
        return None

    # ------------------------------------------------------------------
    # 中止條件
    # ------------------------------------------------------------------
    def has_replied(self, attendee: dict) -> bool:
        """判斷與會者是否已回覆（三個訊號任一成立即視為已回覆）。

        故意採寬鬆判定：寧可少發一封，也不要誤發給已回覆的人。
        """
        if bool(attendee.get("has_replied")):
            return True
        if attendee.get("replied_at"):
            return True
        return str(attendee.get("status") or "").strip().lower() == "replied"

    def is_unsubscribed(self, attendee: dict) -> bool:
        """判斷與會者是否已退訂（同樣採寬鬆判定）。"""
        if bool(attendee.get("is_unsubscribed")):
            return True
        if attendee.get("unsubscribed_at"):
            return True
        status = str(attendee.get("status") or "").strip().lower()
        return status in ("unsubscribed", "opted_out")

    def _blocking_reason(self, attendee: dict) -> tuple[str, str] | None:
        """回傳阻擋序列推進的原因；沒有阻擋則回 None。

        `has_replied` 與 `is_unsubscribed` 一定排在最前面——它們的優先權
        高於任何其他條件，包含分群與到期判定。
        """
        if self.has_replied(attendee):
            return (
                HALT_REPLIED,
                "與會者已回覆，依 stop_on_reply 硬規則立即中止整個序列",
            )
        if self.is_unsubscribed(attendee):
            return (
                HALT_UNSUBSCRIBED,
                "與會者已退訂，依 respect_unsubscribe 硬規則永久排除",
            )
        status = str(attendee.get("status") or "").strip().lower()
        if status in CLOSED_STATUSES:
            return (HALT_STATUS_CLOSED, f"名單狀態為 {status}，不再推進跟進序列")
        return None

    # ------------------------------------------------------------------
    # 序列推進
    # ------------------------------------------------------------------
    def sent_offsets(self, attendee: dict) -> set[int]:
        """合併「名單帶來的 steps_sent」與「本機狀態檔」的已送出紀錄。"""
        attendee_id = str(attendee.get("id") or "")
        offsets = _coerce_offsets(
            attendee.get("steps_sent"), f"{attendee_id} 的 steps_sent"
        )
        return offsets | self._state.sent_offsets(attendee_id)

    def next_step(self, attendee: dict, rule: SegmentRule) -> FollowUpStep | None:
        """回傳該分群中下一段尚未送出的跟進；全部送完則回 None。"""
        sent = self.sent_offsets(attendee)
        for step in rule.steps:
            if step.offset_minutes not in sent:
                return step
        return None

    def due_at(self, step: FollowUpStep) -> datetime:
        """計算某段跟進的到期時間＝活動結束時間 + N 分鐘。"""
        return self._event_ended_at + timedelta(minutes=step.offset_minutes)

    def evaluate(self, attendee: dict, now: datetime) -> dict:
        """判定單一與會者此刻該做什麼，回傳決策 dict。"""
        blocked = self._blocking_reason(attendee)
        if blocked is not None:
            return self._halt(attendee, blocked[0], blocked[1])
        try:
            rule = self.classify(attendee)
        except SegmentError as exc:
            return self._halt(attendee, HALT_BAD_DATA, str(exc))
        if rule is None:
            return self._halt(attendee, HALT_NO_SEGMENT, "不符合任何分群條件")
        step = self.next_step(attendee, rule)
        if step is None:
            return self._halt(
                attendee, HALT_SEQUENCE_COMPLETE, f"{rule.label} 序列已全部送出", rule
            )
        due = self.due_at(step)
        if now < due:
            return self._halt(
                attendee,
                HALT_NOT_DUE,
                f"{step.type} 於 {due.isoformat()} 才到期",
                rule,
            )
        return self._send_decision(attendee, rule, step, due)

    def plan(self, attendees: Iterable[dict], now: datetime) -> list[dict]:
        """批次判定，回傳與輸入同順序的決策清單。"""
        return [self.evaluate(attendee, now) for attendee in attendees]

    def assert_can_send(
        self,
        attendee: dict,
        reply_checker: Callable[[dict], bool] | None = None,
        unsubscribe_checker: Callable[[dict], bool] | None = None,
    ) -> None:
        """**每一次實際發送前**都必須呼叫的最後一道閘門。

        兩個 checker 讓正式環境改成即時查 CRM / 收件匣 / 退訂中心，
        而不是沿用排程當下的快照。一場 300 人的研討會跑完整輪產文可能要
        數十分鐘，這段空窗內有人回信或按退訂是常態而非例外。
        """
        attendee_id = str(attendee.get("id") or "<unknown>")
        replied = (reply_checker or self.has_replied)(attendee)
        if replied:
            raise SequenceHalted(
                attendee_id, HALT_REPLIED, "發送前複查偵測到已回覆，本次發送中止"
            )
        unsubscribed = (unsubscribe_checker or self.is_unsubscribed)(attendee)
        if unsubscribed:
            raise SequenceHalted(
                attendee_id, HALT_UNSUBSCRIBED, "發送前複查偵測到已退訂，本次發送中止"
            )

    def mark_sent(self, attendee: dict, offset_minutes: int) -> None:
        """記錄某段已送出，避免重複發送。"""
        self._state.mark_sent(str(attendee.get("id") or ""), int(offset_minutes))

    def should_handover(self, attendee: dict, rule: SegmentRule) -> bool:
        """是否需要把此人交棒給業務（只有 hot 群、且尚未交棒過）。"""
        if not rule.is_crm_handover:
            return False
        if bool(attendee.get("crm_handover_done")):
            return False
        return not self._state.has_handover(str(attendee.get("id") or ""))

    def mark_handover(self, attendee: dict) -> None:
        """記錄已交棒業務。"""
        self._state.mark_handover(str(attendee.get("id") or ""))

    # ------------------------------------------------------------------
    # 決策組裝
    # ------------------------------------------------------------------
    def _base_entry(self, attendee: dict, rule: SegmentRule | None) -> dict:
        """決策 dict 的共同欄位。"""
        return {
            "attendee_id": str(attendee.get("id") or "<unknown>"),
            "name": str(attendee.get("name") or ""),
            "company": str(attendee.get("company") or ""),
            "segment": rule.key if rule is not None else None,
            "segment_label": rule.label if rule is not None else None,
        }

    def _halt(
        self,
        attendee: dict,
        reason: str,
        detail: str,
        rule: SegmentRule | None = None,
    ) -> dict:
        """組出中止決策。"""
        entry = self._base_entry(attendee, rule)
        entry.update(
            {
                "action": ACTION_HALT,
                "reason": reason,
                "detail": detail,
                "step": None,
                "due_at": None,
            }
        )
        return entry

    def _send_decision(
        self,
        attendee: dict,
        rule: SegmentRule,
        step: FollowUpStep,
        due: datetime,
    ) -> dict:
        """組出「該發送」決策。"""
        entry = self._base_entry(attendee, rule)
        entry.update(
            {
                "action": ACTION_SEND,
                "reason": f"{rule.label} 的 {step.type} 已到期",
                "detail": f"到期時間 {due.isoformat()}",
                "step": step.as_dict(),
                "due_at": due.isoformat(),
            }
        )
        return entry
