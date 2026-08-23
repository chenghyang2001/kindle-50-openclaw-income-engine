"""全漏斗業務管線引擎：階段狀態機 + SLA 監控 + 鏈路序列排程。

本檔刻意與 `main.py` 分離，因為全漏斗自動化真正的風險不在「產文」，
而在三件事，這三件事都必須能被單獨測試：

1. **非法階段轉移**：CRM webhook 送來的事件不見得可信（重送、亂序、人為誤點）。
   `discovery -> closed_won` 這種跳過提案的轉移必須被擋下並留紀錄，
   而不是讓下游鏈路對著一筆狀態錯亂的交易繼續動作。
2. **SLA 超時**：Enrichment 的 `<2 小時` 是硬性門檻（apxG_p07）。超時要「叫」，
   不可靜默——靜默的 SLA 等於沒有 SLA。
3. **`halt_on_reply`**：潛在客戶一回覆就必須中止追蹤序列。與 demo10 同樣採
   「建構子強制覆寫」策略：設定檔寫 false 也不生效，只會留下 override 紀錄
   供上層發 AMBER。誤發給已回覆客戶的傷害不可逆，不交給設定檔決定。

設計上的兩個關鍵決定：

- **鏈路（chain）是統一抽象**：Enrichment / Proposal / Follow-Up / Onboarding /
  Renurture 五條鏈路都描述成「錨點時間 + 若干 step（第 N 天）」。因此排程、
  去重、SLA 計算只有一份實作，不會五條鏈各寫一套而各自長出 bug。
- **到期判定與發送前複查是兩道獨立閘門**：排程當下沒回覆，不代表輪到實際
  送出時還沒回覆——中間隔著 LLM 生成時間。故每一次送出前都要再查一次。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone, tzinfo
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 狀態檔一律放模組目錄下，禁止硬編碼使用者路徑
MODULE_DIR = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = MODULE_DIR / "state" / "pipeline_state.json"

# ---------------------------------------------------------------- 階段常數 --
STAGE_LEAD = "lead_captured"
STAGE_DISCOVERY = "discovery"
STAGE_PROPOSAL_SENT = "proposal_sent"
STAGE_CLOSED_WON = "closed_won"
STAGE_CLOSED_LOST = "closed_lost"

# 合法轉移表。closed_lost -> lead_captured 是刻意保留的：
# 90 天重新培育序列跑完後，交易可以重新回到漏斗頂端。
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STAGE_LEAD: frozenset({STAGE_DISCOVERY, STAGE_CLOSED_LOST}),
    STAGE_DISCOVERY: frozenset({STAGE_PROPOSAL_SENT, STAGE_CLOSED_LOST}),
    STAGE_PROPOSAL_SENT: frozenset({STAGE_CLOSED_WON, STAGE_CLOSED_LOST}),
    STAGE_CLOSED_WON: frozenset(),
    STAGE_CLOSED_LOST: frozenset({STAGE_LEAD}),
}

# 進入各階段的必要欄位（點號代表巢狀路徑）。缺欄位就不准進站，
# 避免「階段是 proposal_sent 但根本沒有提案」這種資料在下游炸開。
STAGE_ENTRY_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    STAGE_LEAD: ("email", "source"),
    STAGE_DISCOVERY: ("enrichment.score",),
    STAGE_PROPOSAL_SENT: ("proposal_id", "proposal_sent_at"),
    STAGE_CLOSED_WON: ("proposal_id", "closed_at"),
    STAGE_CLOSED_LOST: ("closed_at",),
}

# 追蹤型鏈路：這些鏈路的 halt_on_reply 不可由設定檔關閉
CHASE_CHAINS = ("follow_up", "renurture")

# 決策動作
ACTION_RUN = "run"
ACTION_HALT = "halt"

# 中止 / 拒絕原因（對外回報用的穩定字串鍵，測試會直接斷言）
HALT_REPLIED = "replied"
HALT_SEQUENCE_COMPLETE = "sequence_complete"
HALT_NOT_DUE = "not_due"
HALT_BAD_DATA = "bad_data"
HALT_NO_CHAIN = "no_chain"

REJECT_UNKNOWN_DEAL = "unknown_deal"
REJECT_STALE_EVENT = "stale_event"
REJECT_ILLEGAL_TRANSITION = "illegal_transition"
REJECT_ENTRY_UNMET = "entry_conditions_unmet"


class PipelineError(ValueError):
    """管線設定或資料格式錯誤。"""


class IllegalTransitionError(PipelineError):
    """非法的階段轉移（例如跳過提案直接成交）。"""

    def __init__(self, from_stage: str, to_stage: str) -> None:
        allowed = ", ".join(sorted(ALLOWED_TRANSITIONS.get(from_stage, frozenset()))) or "（終態）"
        super().__init__(
            f"非法階段轉移 {from_stage!r} -> {to_stage!r}；{from_stage!r} 的合法下一站：{allowed}"
        )
        self.from_stage = from_stage
        self.to_stage = to_stage


class SequenceHalted(RuntimeError):
    """發送前複查判定必須中止（最常見的原因是客戶已回覆）。"""

    def __init__(self, deal_id: str, reason: str, detail: str) -> None:
        super().__init__(f"[{deal_id}] {reason}：{detail}")
        self.deal_id = deal_id
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------- 小工具 --
def resolve_timezone(
    name: str,
    fallback_offset_hours: int = 8,
) -> tuple[tzinfo, str | None]:
    """取得時區物件，取不到就退回固定偏移。回傳 `(tzinfo, 警告或 None)`。

    Windows 與精簡容器沒有系統 IANA 時區資料庫，`zoneinfo` 需要額外的 tzdata
    套件；本專案第三方依賴只允許 PyYAML 與 pytest，因此改為明確降級 + 警告，
    而不是讓整支程式在啟動時就掛掉。
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
        raise PipelineError(f"無法解析時間字串：{value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def to_decimal(value: object, field: str) -> Decimal:
    """金額一律走 Decimal（禁止 float），格式錯誤要明確報錯。"""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise PipelineError(f"{field} 不是合法金額：{value!r}") from exc


def dig(payload: object, dotted: str) -> object:
    """依點號路徑取巢狀值，任一層缺失即回 None。"""
    current = payload
    for part in dotted.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _coerce_days(raw: object, context: str) -> set[int]:
    """把 steps_sent 之類的清單轉成 int 集合，格式錯誤即拋錯。"""
    if raw is None:
        return set()
    if not isinstance(raw, (list, tuple, set)):
        raise PipelineError(f"{context} 必須是清單，收到 {type(raw).__name__}")
    try:
        return {int(item) for item in raw}
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"{context} 含非整數項目：{raw!r}") from exc


# ---------------------------------------------------------------- 鏈路模型 --
@dataclass(frozen=True)
class ChainStep:
    """鏈路中的一個節點（錨點後第 day 天執行）。"""

    day: int
    type: str
    prompt: str

    @classmethod
    def from_config(cls, raw: object, chain_name: str) -> "ChainStep":
        """從 config.yaml 的 steps 項目建立；欄位缺失即拋錯而非給預設值。"""
        if not isinstance(raw, dict):
            raise PipelineError(f"{chain_name} 的 step 必須是 mapping，收到 {type(raw).__name__}")
        try:
            day = int(raw["day"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PipelineError(f"{chain_name} 的 step 缺少合法的 day：{raw!r}") from exc
        step_type = str(raw.get("type") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        if not step_type or not prompt:
            raise PipelineError(f"{chain_name} 的 step 缺少 type 或 prompt：{raw!r}")
        if day < 0:
            raise PipelineError(f"{chain_name} 的 day 不可為負數，收到 {day}")
        return cls(day=day, type=step_type, prompt=prompt)

    def as_dict(self) -> dict:
        """轉成可 JSON 序列化的 dict。"""
        return {"day": self.day, "type": self.type, "prompt": self.prompt}


@dataclass(frozen=True)
class Chain:
    """一條自動化鏈路（Enrichment / Proposal / Follow-Up / Onboarding / Renurture）。"""

    name: str
    label: str
    anchor_field: str
    steps: tuple[ChainStep, ...]
    sla_minutes: int | None
    halt_on_reply: bool
    is_outbound: bool

    def step_for_day(self, day: int) -> ChainStep:
        """由 day 取回 step 物件，找不到即拋錯。"""
        for step in self.steps:
            if step.day == day:
                return step
        raise PipelineError(f"{self.name} 沒有 day={day} 的 step")


def build_chains(raw_chains: object) -> dict[str, Chain]:
    """把 config.yaml 的 chains 區段轉成 Chain 物件表。"""
    if not isinstance(raw_chains, dict) or not raw_chains:
        raise PipelineError("config.yaml 的 pipeline.chains 必須是非空 mapping")
    chains: dict[str, Chain] = {}
    for name, raw in raw_chains.items():
        chains[str(name)] = _build_one_chain(str(name), raw)
    return chains


def _build_one_chain(name: str, raw: object) -> Chain:
    """建立單一鏈路；追蹤型鏈路的 halt_on_reply 一律強制為 True。"""
    if not isinstance(raw, dict):
        raise PipelineError(f"chains.{name} 必須是 mapping，收到 {type(raw).__name__}")
    steps = tuple(
        sorted(
            (ChainStep.from_config(item, name) for item in (raw.get("steps") or [])),
            key=lambda item: item.day,
        )
    )
    if not steps:
        raise PipelineError(f"chains.{name} 至少要有一個 step")
    days = [step.day for step in steps]
    if len(set(days)) != len(days):
        raise PipelineError(f"chains.{name} 的 day 不可重複：{days}")
    anchor = str(raw.get("anchor") or "").strip()
    if not anchor:
        raise PipelineError(f"chains.{name} 缺少 anchor（錨點時間欄位名）")
    sla = raw.get("sla_minutes")
    return Chain(
        name=name,
        label=str(raw.get("label") or name),
        anchor_field=anchor,
        steps=steps,
        sla_minutes=int(sla) if sla is not None else None,
        # 追蹤型鏈路不看設定值，一律 True（強制覆寫紀錄由 SalesPipeline 收集）
        halt_on_reply=True if name in CHASE_CHAINS else bool(raw.get("halt_on_reply", False)),
        is_outbound=bool(raw.get("outbound", False)),
    )


def build_stage_map(raw_stage_map: object, chains: dict[str, Chain]) -> dict[str, str]:
    """把 `stage_map`（CRM 階段 -> 鏈路）轉成扁平表並驗證引用完整。"""
    if not isinstance(raw_stage_map, dict) or not raw_stage_map:
        raise PipelineError("config.yaml 的 pipeline.stage_map 必須是非空 mapping")
    mapping: dict[str, str] = {}
    for crm_key, raw in raw_stage_map.items():
        if not isinstance(raw, dict):
            raise PipelineError(f"stage_map.{crm_key} 必須是 mapping")
        stage = str(raw.get("stage") or "").strip()
        chain = str(raw.get("chain") or "").strip()
        if stage not in ALLOWED_TRANSITIONS:
            raise PipelineError(f"stage_map.{crm_key} 的 stage {stage!r} 不是已知階段")
        if chain not in chains:
            raise PipelineError(f"stage_map.{crm_key} 引用了未定義的鏈路 {chain!r}")
        mapping[stage] = chain
    return mapping


def collect_forced_overrides(raw_chains: object) -> list[str]:
    """找出「設定檔想關掉追蹤鏈 halt_on_reply」的違規，供上層發 AMBER。"""
    overrides: list[str] = []
    if not isinstance(raw_chains, dict):
        return overrides
    for name in CHASE_CHAINS:
        raw = raw_chains.get(name)
        if isinstance(raw, dict) and raw.get("halt_on_reply", True) is not True:
            overrides.append(
                f"chains.{name}.halt_on_reply 被設為 "
                f"{raw.get('halt_on_reply')!r}，已強制覆寫為 True"
            )
    return overrides


# ---------------------------------------------------------------- 狀態持久化 --
class PipelineState:
    """管線進度與 `--dry-run` 通訊測試收據的持久化狀態。

    `persist=False`（mock 非 dry-run 的預設）時完全在記憶體運作，既不讀也不寫，
    讓 `--mock` 每次執行的結果都一模一樣，QA 可重複驗證。
    """

    def __init__(self, path: Path | None = None, persist: bool = False) -> None:
        self._path = Path(path) if path is not None else DEFAULT_STATE_PATH
        self._persist = bool(persist)
        self._steps: dict[str, dict[str, set[int]]] = {}
        self._receipts: dict[str, str] = {}
        if self._persist:
            self._load()

    @property
    def path(self) -> Path:
        """狀態檔絕對路徑。"""
        return self._path

    @property
    def is_persistent(self) -> bool:
        """是否會實際落地寫檔。"""
        return self._persist

    def _load(self) -> None:
        """讀取既有狀態檔；檔案損毀要明確報錯，不可靜默當成空狀態。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PipelineError(f"狀態檔無法讀取或解析：{self._path}") from exc
        if not isinstance(raw, dict):
            raise PipelineError(f"狀態檔格式錯誤（應為 object）：{self._path}")
        self._receipts = {str(k): str(v) for k, v in (raw.get("dry_run_receipts") or {}).items()}
        for deal_id, chains in (raw.get("steps_sent") or {}).items():
            if not isinstance(chains, dict):
                raise PipelineError(f"狀態檔 {deal_id} 的 steps_sent 格式錯誤")
            self._steps[str(deal_id)] = {
                str(chain): _coerce_days(days, f"狀態檔 {deal_id}.{chain}")
                for chain, days in chains.items()
            }

    def sent_days(self, deal_id: str, chain: str) -> set[int]:
        """回傳某交易在某鏈路上已執行過的 day 集合。"""
        return set(self._steps.get(str(deal_id), {}).get(str(chain), set()))

    def mark_sent(self, deal_id: str, chain: str, day: int) -> None:
        """標記某節點已執行，`persist=True` 時立即寫回磁碟。"""
        per_deal = self._steps.setdefault(str(deal_id), {})
        per_deal.setdefault(str(chain), set()).add(int(day))
        if self._persist:
            self.save()

    def record_dry_run(self, fingerprint: str, when: datetime) -> None:
        """記錄一次成功的 `--dry-run` 內部通訊測試收據。"""
        self._receipts[str(fingerprint)] = when.isoformat()
        if self._persist:
            self.save()

    def dry_run_receipt(self, fingerprint: str) -> str | None:
        """查詢此設定指紋是否已通過 dry-run；沒有則回 None。"""
        return self._receipts.get(str(fingerprint))

    def save(self) -> None:
        """寫回狀態檔（含建立父目錄）。"""
        payload = {
            "steps_sent": {
                deal_id: {chain: sorted(days) for chain, days in chains.items()}
                for deal_id, chains in self._steps.items()
            },
            "dry_run_receipts": dict(self._receipts),
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            raise PipelineError(f"狀態檔寫入失敗：{self._path}") from exc


# ---------------------------------------------------------------- 主狀態機 --
class SalesPipeline:
    """全漏斗業務管線狀態機。"""

    def __init__(
        self,
        chains: dict[str, Chain],
        stage_map: dict[str, str],
        tz: tzinfo,
        state: PipelineState | None = None,
        forced_overrides: Sequence[str] | None = None,
    ) -> None:
        if not chains:
            raise PipelineError("chains 不可為空")
        if not stage_map:
            raise PipelineError("stage_map 不可為空")
        self._chains = dict(chains)
        self._stage_map = dict(stage_map)
        self._tz = tz
        self._state = state if state is not None else PipelineState(persist=False)
        self.forced_overrides: list[str] = list(forced_overrides or [])

    @property
    def chains(self) -> dict[str, Chain]:
        """鏈路表。"""
        return dict(self._chains)

    @property
    def stage_map(self) -> dict[str, str]:
        """階段 -> 鏈路對照表。"""
        return dict(self._stage_map)

    @property
    def state(self) -> PipelineState:
        """底層進度狀態。"""
        return self._state

    # ---- 階段轉移 ----
    @staticmethod
    def validate_transition(from_stage: str, to_stage: str) -> None:
        """驗證階段轉移合法性，非法即拋 IllegalTransitionError。"""
        source = str(from_stage or "").strip()
        target = str(to_stage or "").strip()
        if source not in ALLOWED_TRANSITIONS:
            raise PipelineError(f"未知的來源階段：{source!r}")
        if target not in ALLOWED_TRANSITIONS:
            raise PipelineError(f"未知的目標階段：{target!r}")
        if target not in ALLOWED_TRANSITIONS[source]:
            raise IllegalTransitionError(source, target)

    @staticmethod
    def missing_entry_fields(stage: str, deal: dict) -> list[str]:
        """回傳進入某階段時仍缺少的必要欄位清單。"""
        required = STAGE_ENTRY_REQUIREMENTS.get(str(stage), ())
        return [field for field in required if dig(deal, field) in (None, "")]

    def apply_events(
        self,
        deals: Iterable[dict],
        events: Iterable[dict],
    ) -> tuple[list[dict], list[dict]]:
        """套用 CRM 階段變更事件，回傳 `(更新後交易清單, 被拒事件清單)`。

        被拒事件不會中斷整批處理——一筆壞事件不該讓整條管線停擺，
        但也絕不可靜默略過，故一律回報供上層發 AMBER 並寫入稽核軌跡。
        """
        working = [copy.deepcopy(item) for item in deals]
        index = {str(item.get("id") or ""): item for item in working}
        rejected: list[dict] = []
        for event in events:
            outcome = self._apply_one_event(index, event)
            if outcome is not None:
                rejected.append(outcome)
        return working, rejected

    def _apply_one_event(self, index: dict[str, dict], event: dict) -> dict | None:
        """套用單一事件；成功回 None，失敗回拒絕紀錄。"""
        deal_id = str((event or {}).get("deal_id") or "")
        target = str((event or {}).get("to_stage") or "").strip()
        deal = index.get(deal_id)
        if deal is None:
            return _reject(event, REJECT_UNKNOWN_DEAL, f"找不到交易 {deal_id!r}")
        current = str(deal.get("stage") or "").strip()
        declared = str(event.get("from_stage") or current).strip()
        if declared != current:
            return _reject(
                event, REJECT_STALE_EVENT, f"事件宣稱來源 {declared!r}，實際為 {current!r}"
            )
        try:
            self.validate_transition(current, target)
        except IllegalTransitionError as exc:
            return _reject(event, REJECT_ILLEGAL_TRANSITION, str(exc))
        return self._commit_event(deal, event, current, target)

    def _commit_event(self, deal: dict, event: dict, current: str, target: str) -> dict | None:
        """轉移合法時再驗進站條件，通過才真的改寫交易階段。"""
        candidate = dict(deal)
        candidate.update(dict(event.get("fields") or {}))
        missing = self.missing_entry_fields(target, candidate)
        if missing:
            return _reject(
                event,
                REJECT_ENTRY_UNMET,
                f"進入 {target!r} 缺少必要欄位：{', '.join(missing)}",
            )
        deal.update(dict(event.get("fields") or {}))
        deal["stage"] = target
        deal["previous_stage"] = current
        deal["stage_entered_at"] = event.get("occurred_at") or deal.get("stage_entered_at")
        return None

    # ---- 鏈路排程 ----
    def chain_for(self, deal: dict) -> Chain | None:
        """依交易目前階段取出對應鏈路；階段未對應任何鏈路則回 None。"""
        stage = str(deal.get("stage") or "").strip()
        chain_name = self._stage_map.get(stage)
        return self._chains.get(chain_name) if chain_name else None

    @staticmethod
    def has_replied(deal: dict) -> bool:
        """判斷客戶是否已回覆（三個訊號任一成立即視為已回覆）。

        故意採寬鬆判定：寧可少發一封，也不要誤發給已回覆的客戶。
        """
        if bool(deal.get("has_replied")):
            return True
        if deal.get("replied_at"):
            return True
        return str(deal.get("reply_status") or "").strip().lower() == "replied"

    def sent_days(self, deal: dict, chain: Chain) -> set[int]:
        """合併「CRM 帶來的 steps_sent」與「本機狀態檔」的已執行紀錄。"""
        deal_id = str(deal.get("id") or "")
        raw = (deal.get("steps_sent") or {}).get(chain.name)
        days = _coerce_days(raw, f"{deal_id} 的 steps_sent.{chain.name}")
        return days | self._state.sent_days(deal_id, chain.name)

    def next_step(self, deal: dict, chain: Chain) -> ChainStep | None:
        """回傳鏈路中下一個尚未執行的節點；全部完成則回 None。"""
        done = self.sent_days(deal, chain)
        for step in chain.steps:
            if step.day not in done:
                return step
        return None

    def anchor_at(self, deal: dict, chain: Chain) -> datetime:
        """取得鏈路錨點時間（如提案寄出時間、結案時間）。"""
        raw = deal.get(chain.anchor_field)
        if not raw:
            raise PipelineError(
                f"{deal.get('id')!r} 缺少 {chain.anchor_field}，無法排程 {chain.label}"
            )
        return parse_iso(raw, self._tz)

    def due_at(self, deal: dict, chain: Chain, step: ChainStep) -> datetime:
        """節點到期時間 = 錨點 + N 天（不用絕對日期，序列可隨時重排）。"""
        return self.anchor_at(deal, chain) + timedelta(days=step.day)

    # ---- SLA 監控 ----
    def scan_sla(self, deals: Iterable[dict], now: datetime) -> list[dict]:
        """掃出所有超過 SLA 門檻仍未完成的交易。超時必須「叫」，不可靜默。"""
        breaches: list[dict] = []
        for deal in deals:
            breach = self._sla_for(deal, now)
            if breach is not None:
                breaches.append(breach)
        return breaches

    def _sla_for(self, deal: dict, now: datetime) -> dict | None:
        """單筆交易的 SLA 判定；無 SLA、已完成、資料不全皆回 None。"""
        chain = self.chain_for(deal)
        if chain is None or chain.sla_minutes is None:
            return None
        if self.next_step(deal, chain) is None:
            return None
        try:
            anchor = self.anchor_at(deal, chain)
        except PipelineError:
            return None  # 資料不全由 evaluate() 以 bad_data 回報，不重複告警
        deadline = anchor + timedelta(minutes=chain.sla_minutes)
        if now <= deadline:
            return None
        overdue = int((now - deadline).total_seconds() // 60)
        return {
            "deal_id": str(deal.get("id") or "<unknown>"),
            "company": str(deal.get("company") or ""),
            "stage": str(deal.get("stage") or ""),
            "chain": chain.name,
            "chain_label": chain.label,
            "sla_minutes": chain.sla_minutes,
            "anchor_at": anchor.isoformat(),
            "deadline_at": deadline.isoformat(),
            "overdue_minutes": overdue,
        }

    # ---- 決策 ----
    def evaluate(self, deal: dict, now: datetime) -> dict:
        """判定單筆交易此刻該做什麼，回傳決策 dict。"""
        chain = self.chain_for(deal)
        if chain is None:
            return _halt(deal, None, HALT_NO_CHAIN, f"階段 {deal.get('stage')!r} 未對應任何鏈路")
        # halt_on_reply 的優先權高於任何其他條件，因此放在最前面
        if chain.halt_on_reply and self.has_replied(deal):
            return _halt(deal, chain, HALT_REPLIED, "客戶已回覆，依 halt_on_reply 硬規則中止序列")
        step = self.next_step(deal, chain)
        if step is None:
            return _halt(deal, chain, HALT_SEQUENCE_COMPLETE, f"{chain.label} 所有節點已完成")
        try:
            due = self.due_at(deal, chain, step)
        except PipelineError as exc:
            return _halt(deal, chain, HALT_BAD_DATA, str(exc))
        if now < due:
            return _halt(deal, chain, HALT_NOT_DUE, f"Day {step.day} 於 {due.isoformat()} 才到期")
        return _run(deal, chain, step, due)

    def plan(self, deals: Iterable[dict], now: datetime) -> list[dict]:
        """批次判定，回傳與輸入同順序的決策清單。"""
        return [self.evaluate(deal, now) for deal in deals]

    def assert_can_send(
        self,
        deal: dict,
        chain: Chain,
        reply_checker: Callable[[dict], bool] | None = None,
    ) -> None:
        """**每一次實際送出前**都必須呼叫的最後一道閘門。

        `reply_checker` 讓正式環境改成即時查 CRM / 收件匣，而不是沿用排程當下
        的快照。排程與送出之間可能相隔數十分鐘，客戶完全可能在這段空窗回信。
        """
        if not chain.halt_on_reply:
            return
        checker = reply_checker if reply_checker is not None else self.has_replied
        if checker(deal):
            raise SequenceHalted(
                str(deal.get("id") or "<unknown>"),
                HALT_REPLIED,
                "發送前複查偵測到客戶已回覆，本次發送中止",
            )

    def mark_sent(self, deal: dict, chain: Chain, step: ChainStep) -> None:
        """記錄某節點已執行，避免重複發送。"""
        self._state.mark_sent(str(deal.get("id") or ""), chain.name, step.day)


# ---------------------------------------------------------------- 決策組裝 --
def _halt(deal: dict, chain: Chain | None, reason: str, detail: str) -> dict:
    """組出中止決策。"""
    return {
        "deal_id": str(deal.get("id") or "<unknown>"),
        "contact": str(deal.get("contact") or ""),
        "company": str(deal.get("company") or ""),
        "stage": str(deal.get("stage") or ""),
        "chain": chain.name if chain else None,
        "chain_label": chain.label if chain else None,
        "action": ACTION_HALT,
        "reason": reason,
        "detail": detail,
        "step": None,
        "due_at": None,
    }


def _run(deal: dict, chain: Chain, step: ChainStep, due: datetime) -> dict:
    """組出「該執行」決策。"""
    return {
        "deal_id": str(deal.get("id") or "<unknown>"),
        "contact": str(deal.get("contact") or ""),
        "company": str(deal.get("company") or ""),
        "stage": str(deal.get("stage") or ""),
        "chain": chain.name,
        "chain_label": chain.label,
        "action": ACTION_RUN,
        "reason": f"{chain.label} Day {step.day}（{step.type}）已到期",
        "detail": f"到期時間 {due.isoformat()}",
        "step": step.as_dict(),
        "due_at": due.isoformat(),
    }


def _reject(event: dict, reason: str, detail: str) -> dict:
    """組出被拒事件紀錄。"""
    return {
        "event_id": str((event or {}).get("id") or "<unknown>"),
        "deal_id": str((event or {}).get("deal_id") or "<unknown>"),
        "from_stage": str((event or {}).get("from_stage") or ""),
        "to_stage": str((event or {}).get("to_stage") or ""),
        "reason": reason,
        "detail": detail,
    }


def pipeline_value(deals: Iterable[dict]) -> Decimal:
    """統計仍在漏斗中（未結案）的管線總值，金額一律 Decimal。"""
    total = Decimal("0")
    for deal in deals:
        if str(deal.get("stage") or "") in (STAGE_CLOSED_WON, STAGE_CLOSED_LOST):
            continue
        total += to_decimal(deal.get("amount_usd"), f"{deal.get('id')}.amount_usd")
    return total.quantize(Decimal("0.01"))
