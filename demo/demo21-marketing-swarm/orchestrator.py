"""蜂群編排器（Swarm Orchestrator）— apxG_p03 全域繼承與編排器機制。

架構一句話：**Orchestrator 持有唯一的 brand_context.yml 與 STAGE_MAP，
五個 Sub-agent 帶 INHERIT_FROM_ORCHESTRATOR: true 級聯繼承，
任何對 Orchestrator 的更新會瞬間 Cascade 到所有子智能體。**

這裡實作三個原簡報逐字要求的機制：

1. **單一真理來源（Single Source of Truth）**
   Sub-agent 沒有自己的品牌資料，只有一份唯讀的 InheritedContext。
   沒有繼承過就呼叫 execute() 會直接拋錯 —— 寧可停機也不要讓某個
   agent 拿舊品牌資料發文，那是跨渠道品牌衝突的成因。

2. **Cascading Logic**
   Orchestrator.update_brand_context() 深層合併 -> 版本 +1 -> 立刻級聯。
   每個產出都戳上 context_version 與 context_checksum，事後稽核可以
   回答「這篇貼文是用哪一版品牌上下文寫的」。

3. **強制安全閥（--dry-run）**
   preflight_dry_run() 在任何對外 API 呼叫之前跑完整內部通訊測試，
   印出「將要呼叫哪些外部端點、送出什麼」但不實際送出。沒過就不准 dispatch。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from audit import AuditLog

# 星期代號對應 Python datetime.weekday()（週一 = 0）
WEEKDAY_CODES: dict[str, int] = {
    "MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6,
}

# Sub-agent 產出狀態
STATUS_DISPATCHED = "dispatched"
STATUS_BLOCKED = "blocked_pending_approval"
STATUS_SKIPPED = "skipped_inactive_stage"

# 強制安全閥的檢查結果
CHECK_OK = "ok"
CHECK_MISSING_CREDENTIAL = "missing_credential"
CHECK_UNKNOWN_INTEGRATION = "unknown_integration"
CHECK_INSECURE_ENDPOINT = "insecure_endpoint"


class SwarmError(RuntimeError):
    """蜂群架構違規或 Sub-agent 產出無法解析。"""


# ---------------------------------------------------------------------------
# 時間工具（一律用 zoneinfo；tzdata 缺失時退回固定時差，測試不依賴系統時區庫）
# ---------------------------------------------------------------------------
def resolve_timezone(
    name: str, fallback_utc_offset_hours: int = 8
) -> tuple[tzinfo, str | None]:
    """回傳 (時區, 警告訊息或 None)。

    精簡版 Python（Windows 常見）不一定裝了 tzdata，此時退回固定時差而不是
    整支程式掛掉 —— 但要留下警告，因為固定時差不處理日光節約時間。
    """
    try:
        return ZoneInfo(name), None
    except (ZoneInfoNotFoundError, ValueError) as exc:
        offset = timezone(timedelta(hours=fallback_utc_offset_hours))
        warning = (
            f"時區 {name} 無法載入（{exc}），已退回固定 UTC"
            f"{fallback_utc_offset_hours:+d}；安裝 tzdata 套件可恢復日光節約處理"
        )
        return offset, warning


def parse_clock(value: str) -> dt_time:
    """把 07:00 這種字串解析成 time 物件。"""
    try:
        hour_text, minute_text = str(value).strip().split(":", 1)
        return dt_time(hour=int(hour_text), minute=int(minute_text))
    except (AttributeError, ValueError) as exc:
        raise SwarmError(f"無法解析時間字串 {value!r}，預期格式 HH:MM") from exc


def current_memo_slot(now: datetime, weekday_code: str, clock: str) -> datetime:
    """回傳「不晚於 now 的最近一次備忘錄排程時點」。

    apxG_p05 的排程是每週日 07:00。取「最近一次已發生的時點」而非「下一次」，
    因為備忘錄識別碼要對應本週正在執行的計畫，不是還沒生成的下週計畫。
    """
    code = str(weekday_code).strip().upper()
    if code not in WEEKDAY_CODES:
        raise SwarmError(
            f"未知的星期代號 {weekday_code!r}，合法值：{sorted(WEEKDAY_CODES)}"
        )
    target = datetime.combine(now.date(), parse_clock(clock), tzinfo=now.tzinfo)
    target -= timedelta(days=(now.weekday() - WEEKDAY_CODES[code]) % 7)
    if target > now:
        target -= timedelta(days=7)
    return target


# ---------------------------------------------------------------------------
# 繼承載體
# ---------------------------------------------------------------------------
def context_checksum(payload: dict[str, Any]) -> str:
    """對品牌上下文取指紋，用來證明 Sub-agent 拿到的確實是同一份。"""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """深層合併（patch 覆蓋 base），回傳新 dict 不動原物件。

    白牌覆寫只想改 brand.name 時不該把整個 brand 區段砍掉重寫，
    因此 dict 對 dict 要遞迴合併而不是整段取代。
    """
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class InheritedContext:
    """Sub-agent 收到的唯讀品牌上下文快照。

    frozen 是刻意的：Sub-agent 不能就地改品牌資料，要改只能回頭改
    Orchestrator 再級聯下來。這是「單一真理來源」在型別層的落實。
    """

    version: int
    checksum: str
    payload: dict[str, Any]
    generation: int

    @property
    def brand_name(self) -> str:
        """品牌名稱（Sub-agent 唯一合法的取用來源）。"""
        return str((self.payload.get("brand") or {}).get("name", ""))

    @property
    def tenant_slug(self) -> str:
        """租戶代號：本模組的單一租戶識別碼，用於產出與稽核日誌的來源標記。

        與 demo30 的兩層 namespace（reseller / sub-client）不相容，兩者不可互換；
        差異見 demo30 README 第 4a 節。
        """
        return str(self.payload.get("tenant_slug", ""))

    @property
    def banned_terms(self) -> list[str]:
        """品牌護欄的禁用詞。"""
        return list((self.payload.get("guardrails") or {}).get("banned_terms") or [])

    @property
    def tone_examples(self) -> list[str]:
        """語氣樣本（少於門檻會觸發 tone_mismatch 琥珀警示）。"""
        return list((self.payload.get("voice") or {}).get("tone_examples") or [])

    def stage(self, stage_id: str) -> dict[str, Any]:
        """取出 STAGE_MAP 中某個階段的設定。"""
        stage_map = self.payload.get("STAGE_MAP") or {}
        found = stage_map.get(stage_id)
        if not isinstance(found, dict):
            raise SwarmError(
                f"STAGE_MAP 沒有階段 {stage_id!r}，可用階段：{sorted(stage_map)}"
            )
        return found


# ---------------------------------------------------------------------------
# Sub-agent
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentSpec:
    """Sub-agent 的靜態設定（來自 config.yaml 的 swarm.agents）。"""

    agent_id: str
    display_name: str
    prompt_file: str
    fixture_file: str
    quota_min: int
    quota_max: int | None
    quota_unit: str
    integration: str
    inherit_from_orchestrator: bool

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> AgentSpec:
        """從 config.yaml 的一則 agent 設定建立 spec。"""
        agent_id = str(raw.get("id") or "").strip()
        if not agent_id:
            raise SwarmError("swarm.agents 有一項缺少 id")
        quota_max = raw.get("quota_max")
        return cls(
            agent_id=agent_id,
            display_name=str(raw.get("display_name") or agent_id),
            prompt_file=str(raw.get("prompt_file") or ""),
            fixture_file=str(raw.get("fixture_file") or ""),
            quota_min=int(raw.get("quota_min") or 0),
            quota_max=None if quota_max is None else int(quota_max),
            quota_unit=str(raw.get("quota_unit") or "件/週"),
            integration=str(raw.get("integration") or ""),
            # 逐字保留原簡報的旗標名稱；false 代表該 agent 自行維護品牌資料
            inherit_from_orchestrator=bool(raw.get("INHERIT_FROM_ORCHESTRATOR", True)),
        )

    @property
    def quota_label(self) -> str:
        """人可讀的配額標籤，如 8-12 草稿/週、50+ 名單/週。"""
        if self.quota_max is None:
            return f"{self.quota_min}+ {self.quota_unit}"
        if self.quota_max == self.quota_min:
            return f"{self.quota_min} {self.quota_unit}"
        return f"{self.quota_min}-{self.quota_max} {self.quota_unit}"

    def is_within_quota(self, produced: int) -> bool:
        """產能是否落在配額區間內。"""
        if produced < self.quota_min:
            return False
        return self.quota_max is None or produced <= self.quota_max


class SubAgent:
    """五個子智能體的共用實作。差異全部由 spec + 提示詞決定，不開五個子類別。"""

    # 原簡報 apxG_p03 匯流排上的旗標，逐字保留。實例可由 config 覆寫。
    INHERIT_FROM_ORCHESTRATOR: bool = True

    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self.INHERIT_FROM_ORCHESTRATOR = spec.inherit_from_orchestrator
        self._context: InheritedContext | None = None

    @property
    def agent_id(self) -> str:
        """子智能體代號。"""
        return self.spec.agent_id

    @property
    def context(self) -> InheritedContext:
        """目前持有的品牌上下文。沒繼承過就拋錯，絕不回退成空 dict。"""
        if self._context is None:
            raise SwarmError(
                f"{self.spec.display_name} 尚未繼承 brand_context，"
                "Orchestrator 必須先 cascade() 才能 dispatch"
            )
        return self._context

    @property
    def has_context(self) -> bool:
        """是否已經繼承過（供級聯測試斷言）。"""
        return self._context is not None

    def inherit(self, context: InheritedContext) -> bool:
        """接收 Orchestrator 級聯下來的上下文。

        旗標為 false 的 agent 拒絕繼承（模擬「自行維護品牌資料」的舊架構），
        回傳 False 讓 Orchestrator 記錄品牌衝突風險。
        """
        if not self.INHERIT_FROM_ORCHESTRATOR:
            return False
        self._context = context
        return True

    def is_synced_with(self, context: InheritedContext) -> bool:
        """持有的上下文是否與 Orchestrator 當前版本一致（checksum 比對）。"""
        return self._context is not None and self._context.checksum == context.checksum

    def build_task(self, memo: dict[str, Any], stage: dict[str, Any]) -> dict[str, Any]:
        """組出要餵給 LLM 的任務描述（live 模式的 user prompt 內容）。"""
        context = self.context
        return {
            "agent_id": self.agent_id,
            "tenant_slug": context.tenant_slug,
            "brand_name": context.brand_name,
            "context_version": context.version,
            "stage": stage.get("label", ""),
            "directive": stage.get("directive", ""),
            "primary_kpi": stage.get("primary_kpi", ""),
            "quota": self.spec.quota_label,
            "week_of": memo.get("week_of", ""),
            "objectives": memo.get("objectives", []),
            "assignment": (memo.get("agent_tasks") or {}).get(self.agent_id, ""),
            "banned_terms": context.banned_terms,
        }

    def execute(
        self,
        client: Any,
        memo: dict[str, Any],
        stage: dict[str, Any],
        module_dir: Path,
    ) -> dict[str, Any]:
        """執行本 agent 的一週任務，回傳解析後的產出。

        mock 模式下 LLMClient 會直接讀 fixture_file，因此離線零成本；
        live 模式下 fixture 只是「不存在」，走真實 API。兩條路徑共用同一份提示詞。
        """
        prompt = read_text_file(module_dir / self.spec.prompt_file)
        task = self.build_task(memo, stage)
        fixture = module_dir / self.spec.fixture_file
        raw = client.complete(
            system=prompt,
            user=json.dumps(task, ensure_ascii=False, indent=2),
            max_tokens=2000,
            fixture=fixture if fixture.is_file() else None,
        )
        output = parse_json_output(raw, f"{self.spec.display_name} 產出")
        # context_version 由執行當下的繼承狀態戳上，不從 fixture 讀 ——
        # 否則級聯是否真的發生就無從驗證。
        output["context_version"] = self.context.version
        output["context_checksum"] = self.context.checksum
        return output


# ---------------------------------------------------------------------------
# 檔案與 JSON 工具
# ---------------------------------------------------------------------------
def read_text_file(path: Path) -> str:
    """讀取 UTF-8 文字檔，缺檔給出絕對路徑。"""
    if not path.is_file():
        raise SwarmError(f"找不到檔案：{path.resolve()}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SwarmError(f"讀取失敗：{path.resolve()}｜{exc}") from exc


def parse_json_output(raw: str, label: str) -> dict[str, Any]:
    """把 LLM 回傳的文字解析成 dict。

    live 模式下模型偶爾會用 ```json 圍欄包住輸出，這裡先剝除再解析；
    仍失敗就拋錯，不猜測、不用空 dict 掩蓋。
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SwarmError(f"{label}不是合法 JSON：{exc}｜前 120 字：{text[:120]}") from exc
    if not isinstance(parsed, dict):
        raise SwarmError(f"{label}必須是 JSON 物件，實際為 {type(parsed).__name__}")
    return parsed


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
class Orchestrator:
    """行銷總監智能體（Marketing Director Agent）。

    它是唯一持有 brand_context 與 STAGE_MAP 的角色，也是唯一有權更新它們的角色。
    """

    def __init__(
        self,
        brand_context: dict[str, Any],
        audit: AuditLog,
        display_name: str = "Marketing Director Agent",
    ) -> None:
        self._payload = dict(brand_context)
        self._audit = audit
        self._display_name = display_name
        self._generation = 0
        self._agents: list[SubAgent] = []
        self._context = self._snapshot()

    def _snapshot(self) -> InheritedContext:
        """依當前 payload 產生新的唯讀快照。"""
        return InheritedContext(
            version=int(self._payload.get("context_version", 1)),
            checksum=context_checksum(self._payload),
            payload=dict(self._payload),
            generation=self._generation,
        )

    @property
    def context(self) -> InheritedContext:
        """Orchestrator 當前持有的上下文（唯一真理）。"""
        return self._context

    @property
    def display_name(self) -> str:
        """編排器顯示名稱。"""
        return self._display_name

    @property
    def agents(self) -> list[SubAgent]:
        """已註冊的 Sub-agent 清單。"""
        return list(self._agents)

    def register(self, agent: SubAgent) -> None:
        """註冊一個 Sub-agent 到匯流排上。"""
        self._agents.append(agent)

    def cascade(self, reason: str) -> dict[str, list[str]]:
        """把當前上下文瞬間級聯給所有 Sub-agent。

        回傳 {"inherited": [...], "refused": [...]}。refused 是旗標被設成 false
        的 agent —— 那代表它自帶一份品牌資料，是跨渠道品牌衝突的來源，
        必須在稽核日誌留痕。
        """
        inherited: list[str] = []
        refused: list[str] = []
        for agent in self._agents:
            (inherited if agent.inherit(self._context) else refused).append(agent.agent_id)
        self._audit.record(
            action="context_cascade",
            target=f"swarm/{self._context.tenant_slug or 'default'}",
            rationale=reason,
            details={
                "context_version": self._context.version,
                "context_checksum": self._context.checksum,
                "generation": self._context.generation,
                "inherited": inherited,
                "refused": refused,
            },
        )
        return {"inherited": inherited, "refused": refused}

    def update_brand_context(
        self, patch: dict[str, Any], reason: str
    ) -> InheritedContext:
        """更新品牌上下文：深層合併 -> 版本 +1 -> 立刻級聯。

        這就是 apxG_p03 的 Cascading Logic 三步驟。版本號一定要遞增，
        否則 checksum 相同的兩份內容無法在稽核上區分先後。
        """
        if not patch:
            return self._context
        self._payload = deep_merge(self._payload, patch)
        self._payload["context_version"] = int(self._payload.get("context_version", 1)) + 1
        self._generation += 1
        self._context = self._snapshot()
        self.cascade(reason)
        return self._context

    def desynced_agents(self) -> list[str]:
        """列出上下文與 Orchestrator 不同步的 agent（正常情況應為空）。"""
        return [
            agent.agent_id
            for agent in self._agents
            if not agent.is_synced_with(self._context)
        ]

    def active_agents(self, stage_id: str) -> list[SubAgent]:
        """依 STAGE_MAP 決定本階段哪些 agent 要動。"""
        allowed = set(self._context.stage(stage_id).get("active_agents") or [])
        return [agent for agent in self._agents if agent.agent_id in allowed]


# ---------------------------------------------------------------------------
# 強制安全閥：所有對外 API 呼叫前必經的內部 --dry-run 通訊測試（apxG_p03）
# ---------------------------------------------------------------------------
def _check_integration(
    agent: SubAgent, integrations: dict[str, Any]
) -> dict[str, Any]:
    """對單一 agent 的整合端點做一次通訊前檢查（不實際送出）。"""
    spec = agent.spec
    entry = integrations.get(spec.integration)
    base = {
        "agent_id": spec.agent_id,
        "integration": spec.integration,
        "display_name": "",
        "endpoint": "",
        "method": "",
        "required_env": [],
        "missing_env": [],
        "payload_preview": {},
        "status": CHECK_UNKNOWN_INTEGRATION,
    }
    if not isinstance(entry, dict):
        return base
    required = [str(name) for name in (entry.get("env_vars") or [])]
    missing = [name for name in required if not _env_present(name)]
    endpoint = str(entry.get("endpoint", ""))
    base.update(
        display_name=str(entry.get("display_name", spec.integration)),
        endpoint=endpoint,
        method=str(entry.get("method", "POST")),
        auth=str(entry.get("auth", "")),
        required_env=required,
        missing_env=missing,
        payload_preview=_payload_preview(agent),
    )
    if not endpoint.startswith("https://"):
        base["status"] = CHECK_INSECURE_ENDPOINT
    elif missing:
        base["status"] = CHECK_MISSING_CREDENTIAL
    else:
        base["status"] = CHECK_OK
    return base


def _env_present(name: str) -> bool:
    """檢查環境變數是否存在且非空（只判斷有無，值絕不進入日誌）。"""
    return bool(os.environ.get(name))


def _payload_preview(agent: SubAgent) -> dict[str, Any]:
    """組出「將要送出什麼」的預覽。只放結構與計數，不放內容全文。"""
    context = agent.context if agent.has_context else None
    return {
        "tenant_slug": context.tenant_slug if context else "",
        "context_version": context.version if context else None,
        "action": f"publish::{agent.agent_id}",
        "expected_items": agent.spec.quota_min,
        "contains_pii": False,
    }


def preflight_dry_run(
    agents: list[SubAgent],
    integrations: dict[str, Any],
    is_mock: bool,
    audit: AuditLog,
) -> dict[str, Any]:
    """強制安全閥：跑完整通訊測試但不實際送出任何請求。

    通過條件：
    - 沒有未知整合、沒有非 https 端點（這兩項無論 mock 或 live 都是硬錯）
    - live 模式下額外要求所有憑證環境變數都存在（mock 模式不需要憑證）
    """
    checks = [_check_integration(agent, integrations) for agent in agents]
    hard_fail = [
        check for check in checks
        if check["status"] in (CHECK_UNKNOWN_INTEGRATION, CHECK_INSECURE_ENDPOINT)
    ]
    credential_gaps = [
        check for check in checks if check["status"] == CHECK_MISSING_CREDENTIAL
    ]
    is_passed = not hard_fail and (is_mock or not credential_gaps)
    audit.record(
        action="preflight_dry_run",
        target="integrations",
        rationale="apxG_p03 強制安全閥：所有 API 呼叫前必經 --dry-run 內部通訊測試",
        details={
            "mode": "mock" if is_mock else "live",
            "checked": len(checks),
            "passed": is_passed,
            "hard_fail": [check["agent_id"] for check in hard_fail],
            "credential_gaps": [check["agent_id"] for check in credential_gaps],
        },
    )
    return {
        "passed": is_passed,
        # is_mock 必須跟著回傳：報告要據此區分「哪一類請求真的不會送出」。
        # --live --dry-run 只擋業務系統，LLM 仍會實際呼叫並產生費用。
        "is_mock": is_mock,
        "checks": checks,
        "hard_fail": hard_fail,
        "credential_gaps": credential_gaps,
    }


def format_preflight_report(preflight: dict[str, Any]) -> str:
    """把安全閥結果印成人可讀報告：呼叫誰、用什麼方法、送出什麼。"""
    verdict = "PASS" if preflight["passed"] else "BLOCKED"
    lines = [f"🔒 強制安全閥 --dry-run 內部通訊測試：{verdict}"]
    for check in preflight["checks"]:
        preview = json.dumps(check["payload_preview"], ensure_ascii=False)
        lines.append(
            f"  [{check['status']}] {check['agent_id']} -> "
            f"{check['method']} {check['endpoint']}"
        )
        lines.append(f"      將送出：{preview}")
        if check["missing_env"]:
            lines.append(f"      缺少憑證環境變數：{', '.join(check['missing_env'])}")
    lines.extend(_preflight_disclosure(bool(preflight.get("is_mock", True))))
    return "\n".join(lines)


def _preflight_disclosure(is_mock: bool) -> list[str]:
    """揭露這次 dry-run 到底「不送」什麼。

    刻意分成兩種說法：--mock --dry-run 確實是零網路零成本，可以講滿；
    但 --live --dry-run 只擋下社群 / Email / CRM 等業務系統，策略備忘錄與
    五個 Sub-agent 的內容生成**仍會實際呼叫 Anthropic API 並產生費用**。
    這裡不把兩者混為一談 —— 使用者若以為 dry-run 一律免費，會被帳單嚇到。

    為什麼不乾脆讓 dry-run 一律走 mock LLM：dry-run 的價值就是預覽
    「真的會發出去的內容」。換成 fixture 就失去意義了，所以保留行為、講清楚代價。
    """
    if is_mock:
        return ["  （以上為 dry-run 預覽：本次為離線模式，LLM 與業務系統都沒有實際呼叫，零成本）"]
    return [
        "  （以上為 dry-run 預覽：不會對社群 / Email / CRM 等業務系統送出任何內容）",
        "  ⚠️ 注意：--live 模式下 LLM 內容生成仍會實際呼叫 Anthropic API，會產生費用。",
        "     若要完全零外部呼叫、零成本，請改用 --mock --dry-run。",
    ]


# ---------------------------------------------------------------------------
# 策略備忘錄 + 人類審核節點（apxG_p05）
# ---------------------------------------------------------------------------
def generate_strategy_memo(
    client: Any,
    orchestrator: Orchestrator,
    module_dir: Path,
    memo_config: dict[str, Any],
    slot: datetime,
) -> dict[str, Any]:
    """每週日 07:00 產生策略備忘錄（approval_required: true）。"""
    context = orchestrator.context
    prompt = read_text_file(module_dir / str(memo_config.get("prompt_file", "")))
    fixture = module_dir / str(memo_config.get("fixture_file", ""))
    user_payload = {
        "brand_name": context.brand_name,
        "tenant_slug": context.tenant_slug,
        "context_version": context.version,
        "slot_iso": slot.isoformat(timespec="seconds"),
        "stage_map": list((context.payload.get("STAGE_MAP") or {}).keys()),
    }
    raw = client.complete(
        system=prompt,
        user=json.dumps(user_payload, ensure_ascii=False, indent=2),
        max_tokens=2000,
        fixture=fixture if fixture.is_file() else None,
    )
    memo = parse_json_output(raw, "策略備忘錄")
    memo["memo_id"] = f"MEMO-{slot.date().isoformat()}-{context.tenant_slug or 'default'}"
    memo["generated_at"] = slot.isoformat(timespec="seconds")
    memo["context_version"] = context.version
    memo["context_checksum"] = context.checksum
    memo["approval_required"] = bool(memo_config.get("approval_required", True))
    return memo


def find_banned_terms(text: str, banned_terms: list[str]) -> list[str]:
    """回傳文字中命中的禁用詞（品牌護欄，來自單一真理來源）。"""
    return [term for term in banned_terms if term and term in text]


def _deliverable_text(output: dict[str, Any]) -> str:
    """把一個 agent 產出的所有可見文字串起來供護欄掃描。"""
    parts: list[str] = []
    for sample in output.get("samples") or []:
        if isinstance(sample, dict):
            parts.extend(str(value) for value in sample.values())
        else:
            parts.append(str(sample))
    return "\n".join(parts)


def _agent_action(
    agent: SubAgent,
    output: dict[str, Any],
    is_approved: bool,
    can_publish: bool,
    effective_autonomy: str,
) -> dict[str, Any]:
    """把單一 agent 的產出整理成回傳結構。"""
    spec = agent.spec
    produced = int(output.get("produced") or len(output.get("samples") or []))
    violations = find_banned_terms(_deliverable_text(output), agent.context.banned_terms)
    return {
        "agent_id": spec.agent_id,
        "display_name": spec.display_name,
        "integration": spec.integration,
        "inherit_from_orchestrator": agent.INHERIT_FROM_ORCHESTRATOR,
        "context_version": output.get("context_version"),
        "context_checksum": output.get("context_checksum"),
        "produced": produced,
        "quota": spec.quota_label,
        "is_within_quota": spec.is_within_quota(produced),
        "samples": list(output.get("samples") or []),
        "guardrail_violations": violations,
        "status": STATUS_DISPATCHED if is_approved else STATUS_BLOCKED,
        "publish_mode": "auto" if (is_approved and can_publish and not violations) else "draft",
        "effective_autonomy": effective_autonomy,
    }


def dispatch_agents(
    orchestrator: Orchestrator,
    stage_id: str,
    memo: dict[str, Any],
    client: Any,
    module_dir: Path,
    gate: Any,
    audit: AuditLog,
    is_approved: bool,
    approved_by: str | None,
) -> list[dict[str, Any]]:
    """Task Dispatch -> 五條 Agent Action 平行執行（本地以序列模擬）。

    未經人類核准時仍會產出內容，但一律標記 blocked_pending_approval 且
    publish_mode 不可能是 auto —— 「未核准不可發布」在這裡是硬條件。
    """
    stage = orchestrator.context.stage(stage_id)
    active_ids = set(stage.get("active_agents") or [])
    actions: list[dict[str, Any]] = []
    for agent in orchestrator.agents:
        if agent.agent_id not in active_ids:
            actions.append(_skipped_action(agent, stage_id))
            continue
        output = agent.execute(client, memo, stage, module_dir)
        action = _agent_action(
            agent,
            output,
            is_approved=is_approved,
            can_publish=gate.can_send(agent.agent_id),
            effective_autonomy=gate.effective_level(agent.agent_id).value,
        )
        audit.record(
            action="agent_dispatch",
            target=f"{orchestrator.context.tenant_slug}/{agent.agent_id}",
            rationale=(
                f"STAGE_MAP[{stage_id}] 指派；備忘錄 {memo.get('memo_id')} "
                f"{'已核准' if is_approved else '未核准（僅產草稿）'}"
            ),
            is_human_approved=is_approved,
            approved_by=approved_by,
            details={
                "produced": action["produced"],
                "quota": action["quota"],
                "is_within_quota": action["is_within_quota"],
                "context_version": action["context_version"],
                "publish_mode": action["publish_mode"],
                "guardrail_violations": action["guardrail_violations"],
            },
        )
        actions.append(action)
    return actions


def _skipped_action(agent: SubAgent, stage_id: str) -> dict[str, Any]:
    """本階段未啟用的 agent：不呼叫 LLM、不佔配額，但要在結果中留痕。"""
    return {
        "agent_id": agent.agent_id,
        "display_name": agent.spec.display_name,
        "integration": agent.spec.integration,
        "inherit_from_orchestrator": agent.INHERIT_FROM_ORCHESTRATOR,
        "context_version": agent.context.version if agent.has_context else None,
        "context_checksum": agent.context.checksum if agent.has_context else None,
        "produced": 0,
        "quota": agent.spec.quota_label,
        "is_within_quota": True,
        "samples": [],
        "guardrail_violations": [],
        "status": STATUS_SKIPPED,
        "publish_mode": "draft",
        "effective_autonomy": "",
        "skip_reason": f"STAGE_MAP[{stage_id}] 未啟用本 agent",
    }
