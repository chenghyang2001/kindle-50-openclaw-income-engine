"""跟進序列狀態機：到期判定、發送前回覆複查、進度持久化。

本模組刻意與 `main.py` 分離，因為「客戶已回覆就中止」這條規則是整個模組的
安全核心，必須能被單獨測試，不受 LLM / 通知管道 / 設定載入的干擾。

設計上的三個關鍵決定：

1. `stop_on_reply` 在建構子被強制為 `True`。呼叫端傳 `False` 不會生效，
   只會在 `forced_overrides` 留下紀錄供上層發 AMBER 警告。
   理由：誤發跟進給已回覆的客戶會嚴重損害關係，這種傷害不可逆，
   不能交給設定檔決定。
2. 到期判定（`plan`）與發送前複查（`assert_can_send`）是**兩道獨立的閘門**。
   排程當下沒回覆，不代表輪到實際送出時還沒回覆——中間可能隔了數十分鐘的
   LLM 生成時間。因此每一封信送出前都要再查一次。
3. 到期日以「提案寄出時間 + N 天」計算，不用絕對日期，序列因此可以在任何
   時間點被重新排程而不會錯亂。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 狀態檔一律放在模組目錄下，禁止硬編碼使用者路徑
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = MODULE_DIR / "state" / "sequence_state.json"

# CRM 階段：只有 proposal_sent 會推進序列
DEFAULT_ACTIVE_STAGE = "proposal_sent"
CLOSED_STAGES = ("replied", "won", "lost", "closed_won", "closed_lost")

# 中止原因（對外回報用的穩定字串鍵，測試會直接斷言）
HALT_REPLIED = "replied"
HALT_STAGE_CLOSED = "stage_closed"
HALT_STAGE_INACTIVE = "stage_inactive"
HALT_SEQUENCE_COMPLETE = "sequence_complete"
HALT_NOT_DUE = "not_due"
HALT_BAD_DATA = "bad_data"

ACTION_SEND = "send"
ACTION_HALT = "halt"


class SequenceError(ValueError):
    """序列設定或資料格式錯誤。"""


class SequenceHalted(RuntimeError):
    """發送前複查判定必須中止（最常見的原因是客戶已回覆）。"""

    def __init__(self, prospect_id: str, reason: str, detail: str) -> None:
        super().__init__(f"[{prospect_id}] {reason}：{detail}")
        self.prospect_id = prospect_id
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class SequenceStep:
    """序列中的一段跟進。"""

    day: int
    type: str
    prompt: str

    @classmethod
    def from_config(cls, raw: dict) -> "SequenceStep":
        """從 config.yaml 的 sequence 項目建立，欄位缺失即拋錯而非給預設值。"""
        if not isinstance(raw, dict):
            raise SequenceError(f"sequence 項目必須是 mapping，收到 {type(raw).__name__}")
        try:
            day = int(raw["day"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SequenceError(f"sequence 項目缺少合法的 day：{raw!r}") from exc
        step_type = str(raw.get("type") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not step_type or not prompt:
            raise SequenceError(f"sequence 項目缺少 type 或 prompt：{raw!r}")
        if day <= 0:
            raise SequenceError(f"sequence 的 day 必須為正整數，收到 {day}")
        return cls(day=day, type=step_type, prompt=prompt)

    def as_dict(self) -> dict:
        """轉成可 JSON 序列化的 dict。"""
        return {"day": self.day, "type": self.type, "prompt": self.prompt}


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
        raise SequenceError(f"無法解析時間字串：{value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _coerce_days(raw: object, context: str) -> set[int]:
    """把 steps_sent 之類的清單轉成 int 集合，格式錯誤即拋錯。"""
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple, set)):
        raise SequenceError(f"{context} 必須是清單，收到 {type(raw).__name__}")
    try:
        return {int(item) for item in raw}
    except (TypeError, ValueError) as exc:
        raise SequenceError(f"{context} 含非整數項目：{raw!r}") from exc


class SequenceState:
    """跟進進度的持久化狀態：記錄每個潛在客戶已送出哪幾段。

    `persist=False`（mock / dry-run 預設）時完全在記憶體運作，既不讀也不寫，
    讓 `--mock` 每次執行的結果都一模一樣，QA 可重複驗證。
    """

    def __init__(self, path: Path | None = None, persist: bool = False) -> None:
        self._path = Path(path) if path is not None else DEFAULT_STATE_PATH
        self._persist = bool(persist)
        self._data: dict[str, set[int]] = {}
        if self._persist:
            self._load()

    @property
    def path(self) -> Path:
        """狀態檔絕對路徑。"""
        return self._path

    def _load(self) -> None:
        """讀取既有狀態檔；檔案損毀要明確報錯，不可靜默當成空狀態。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SequenceError(f"狀態檔無法讀取或解析：{self._path}") from exc
        if not isinstance(raw, dict):
            raise SequenceError(f"狀態檔格式錯誤（應為 object）：{self._path}")
        for prospect_id, days in raw.items():
            self._data[str(prospect_id)] = _coerce_days(days, f"狀態檔 {prospect_id}")

    def sent_days(self, prospect_id: str) -> set[int]:
        """回傳該潛在客戶已送出的 day 集合。"""
        return set(self._data.get(str(prospect_id), set()))

    def mark_sent(self, prospect_id: str, day: int) -> None:
        """標記某段已送出，`persist=True` 時立即寫回磁碟。"""
        key = str(prospect_id)
        self._data.setdefault(key, set()).add(int(day))
        if self._persist:
            self.save()

    def save(self) -> None:
        """寫回狀態檔（含建立父目錄）。"""
        payload = {key: sorted(value) for key, value in self._data.items()}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            raise SequenceError(f"狀態檔寫入失敗：{self._path}") from exc


class FollowUpSequencer:
    """三段跟進序列的狀態機。"""

    def __init__(
        self,
        steps: Sequence[SequenceStep],
        tz: tzinfo,
        stop_on_reply: bool = True,
        state: SequenceState | None = None,
        active_stage: str = DEFAULT_ACTIVE_STAGE,
    ) -> None:
        if not steps:
            raise SequenceError("sequence 不可為空，至少要有一段跟進")
        self._steps = tuple(sorted(steps, key=lambda item: item.day))
        self._tz = tz
        self._state = state if state is not None else SequenceState(persist=False)
        self._active_stage = str(active_stage or DEFAULT_ACTIVE_STAGE).strip().lower()
        self.forced_overrides: list[str] = []
        # 硬規則：不論外部傳什麼，一律 True。只留下紀錄供上層發 AMBER。
        if stop_on_reply is not True:
            self.forced_overrides.append(
                f"stop_on_reply 被要求設為 {stop_on_reply!r}，已強制覆寫為 True"
            )
        self._stop_on_reply = True

    @property
    def steps(self) -> tuple[SequenceStep, ...]:
        """依 day 排序後的序列。"""
        return self._steps

    @property
    def is_stop_on_reply_enabled(self) -> bool:
        """永遠為 True——這是本模組不可停用的硬規則。"""
        return self._stop_on_reply

    @property
    def state(self) -> SequenceState:
        """底層進度狀態。"""
        return self._state

    def has_replied(self, prospect: dict) -> bool:
        """判斷客戶是否已回覆（三個訊號任一成立即視為已回覆）。

        故意採寬鬆判定：寧可少發一封，也不要誤發給已回覆的客戶。
        """
        if bool(prospect.get("has_replied")):
            return True
        if prospect.get("replied_at"):
            return True
        return str(prospect.get("stage") or "").strip().lower() == "replied"

    def sent_days(self, prospect: dict) -> set[int]:
        """合併「CRM 帶來的 steps_sent」與「本機狀態檔」的已送出紀錄。"""
        prospect_id = str(prospect.get("id") or "")
        days = _coerce_days(prospect.get("steps_sent"), f"{prospect_id} 的 steps_sent")
        return days | self._state.sent_days(prospect_id)

    def next_step(self, prospect: dict) -> SequenceStep | None:
        """回傳下一段尚未送出的跟進；全部送完則回 None。"""
        sent = self.sent_days(prospect)
        for step in self._steps:
            if step.day not in sent:
                return step
        return None

    def due_at(self, prospect: dict, step: SequenceStep) -> datetime:
        """計算某段跟進的到期時間 = 提案寄出時間 + N 天。"""
        raw = prospect.get("proposal_sent_at")
        if not raw:
            raise SequenceError(
                f"{prospect.get('id')!r} 缺少 proposal_sent_at，無法計算 Day {step.day} 到期時間"
            )
        return parse_iso(raw, self._tz) + timedelta(days=step.day)

    def _blocking_reason(self, prospect: dict) -> tuple[str, str] | None:
        """回傳阻擋序列推進的原因；沒有阻擋則回 None。

        `has_replied` 一定放在最前面檢查——它的優先權高於任何其他條件。
        """
        if self.has_replied(prospect):
            return (
                HALT_REPLIED,
                "客戶已回覆，依 stop_on_reply 硬規則立即中止整個序列",
            )
        stage = str(prospect.get("stage") or "").strip().lower()
        if stage in CLOSED_STAGES:
            return (HALT_STAGE_CLOSED, f"CRM 階段為 {stage}，序列已結案不再推進")
        if stage != self._active_stage:
            return (
                HALT_STAGE_INACTIVE,
                f"CRM 階段 {stage or '(空白)'} 非 {self._active_stage}，不啟動跟進",
            )
        return None

    def evaluate(self, prospect: dict, now: datetime) -> dict:
        """判定單一潛在客戶此刻該做什麼，回傳決策 dict。"""
        blocked = self._blocking_reason(prospect)
        if blocked is not None:
            return self._halt(prospect, blocked[0], blocked[1])
        step = self.next_step(prospect)
        if step is None:
            return self._halt(prospect, HALT_SEQUENCE_COMPLETE, "三段跟進已全部送出")
        try:
            due = self.due_at(prospect, step)
        except SequenceError as exc:
            return self._halt(prospect, HALT_BAD_DATA, str(exc))
        if now < due:
            return self._halt(
                prospect, HALT_NOT_DUE, f"Day {step.day} 於 {due.isoformat()} 才到期"
            )
        return self._send_decision(prospect, step, due)

    def plan(self, prospects: Iterable[dict], now: datetime) -> list[dict]:
        """批次判定，回傳與輸入同順序的決策清單。"""
        return [self.evaluate(prospect, now) for prospect in prospects]

    def assert_can_send(
        self,
        prospect: dict,
        reply_checker: Callable[[dict], bool] | None = None,
    ) -> None:
        """**每一次實際發送前**都必須呼叫的最後一道閘門。

        `reply_checker` 讓正式環境改成即時查 CRM / 收件匣，而不是沿用排程當下
        的快照。排程與送出之間可能相隔數十分鐘，客戶完全可能在這段空窗回信。
        """
        checker = reply_checker if reply_checker is not None else self.has_replied
        if checker(prospect):
            raise SequenceHalted(
                str(prospect.get("id") or "<unknown>"),
                HALT_REPLIED,
                "發送前複查偵測到客戶已回覆，本次發送中止",
            )

    def mark_sent(self, prospect: dict, step: SequenceStep) -> None:
        """記錄某段已送出，避免重複發送。"""
        self._state.mark_sent(str(prospect.get("id") or ""), step.day)

    def _halt(self, prospect: dict, reason: str, detail: str) -> dict:
        """組出中止決策。"""
        return {
            "prospect_id": str(prospect.get("id") or "<unknown>"),
            "name": str(prospect.get("name") or ""),
            "company": str(prospect.get("company") or ""),
            "action": ACTION_HALT,
            "reason": reason,
            "detail": detail,
            "step": None,
            "due_at": None,
        }

    def _send_decision(self, prospect: dict, step: SequenceStep, due: datetime) -> dict:
        """組出「該發送」決策。"""
        return {
            "prospect_id": str(prospect.get("id") or "<unknown>"),
            "name": str(prospect.get("name") or ""),
            "company": str(prospect.get("company") or ""),
            "action": ACTION_SEND,
            "reason": f"Day {step.day} {step.type} 已到期",
            "detail": f"到期時間 {due.isoformat()}",
            "step": step.as_dict(),
            "due_at": due.isoformat(),
        }


def build_steps(raw_sequence: object) -> list[SequenceStep]:
    """把 config.yaml 的 sequence 區段轉成 SequenceStep 清單。"""
    if not isinstance(raw_sequence, (list, tuple)) or not raw_sequence:
        raise SequenceError("config.yaml 的 sequence 必須是非空清單")
    steps = [SequenceStep.from_config(item) for item in raw_sequence]
    days = [step.day for step in steps]
    if len(set(days)) != len(days):
        raise SequenceError(f"sequence 的 day 不可重複：{days}")
    return steps
