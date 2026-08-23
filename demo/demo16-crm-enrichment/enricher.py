"""外部資料豐富化引擎：把 CRM 的缺漏欄位補齊，但**絕不覆蓋既有資料**。

三條安全鐵律（本模組存在的理由，違反任何一條都比不做自動化更糟）：

1. **查無資料 ≠ 空值。** 外部查不到就保留 CRM 原值，並把狀態標成
   ``enrichment_failed``。絕不用空字串、``None`` 或推估值覆蓋既有正確資料。
2. **衝突不自動覆蓋。** 外部值與 CRM 既有非空值不一致時，一律**保留 CRM 值**，
   把外部值記成待審建議。書中的痛點是「充滿過期或錯誤數據的 CRM 比沒有 CRM 更糟」，
   而讓機器用第三方推估值蓋掉業務親自問到的答案，正是製造那種 CRM 的最快方法。
3. **部分失敗不中斷。** 單一資料源掛掉只影響它負責的欄位，其餘照跑，
   最後在報表標示「N 個來源無回應」（同 demo09 的降級設計）。

補充：只有列在 ``target_fields`` 白名單的欄位允許被自動補值，且
``protected_fields``（owner / email / lifecycle_stage）連空白都不碰——
業務歸屬被機器改錯是追不回來的，而且沒有人會發現。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, Sequence

#: 這些字面值在 CRM 裡都代表「這格沒填」，不代表「值就是這串字」。
#: 客戶的 CRM 匯出常見 "N/A" / "-" / "未知"，當成真值會讓評分把垃圾算進分數。
BLANK_TOKENS = frozenset(
    {"", "-", "--", "n/a", "na", "none", "null", "unknown", "未知", "待確認"}
)

#: 外部 API 最小呼叫間隔（秒）。Companies House 與 Apollo 對高頻請求直接回 429，
#: 被 ban 一次要等 24 小時，因此設定值低於此值會被強制拉回來。
MIN_RATE_LIMIT_SECONDS = 1.0

ACTION_FILLED = "filled"
ACTION_CONFIRMED = "confirmed"
ACTION_CONFLICT = "conflict_kept"
ACTION_NO_DATA = "no_data"
ACTION_PROTECTED = "protected"

STATUS_ENRICHED = "enriched"
STATUS_NO_CHANGE = "no_change"
STATUS_FAILED = "enrichment_failed"

#: CSV 報告欄位（書中 Output：產出完整的豐富化 CSV 報告）
CSV_COLUMNS = (
    "contact_id",
    "company",
    "domain",
    "status",
    "score",
    "band",
    "grade",
    "is_stale",
    "days_since_contact",
    "filled_fields",
    "conflict_fields",
    "missing_inputs",
    "failed_providers",
)


class ProviderError(RuntimeError):
    """單一外部資料源取數失敗。訊息必須指出是哪個來源、哪個檔案、什麼原因。"""


class DiagnosticsLike(Protocol):
    """只用到 ``Diagnostics.amber()``，用 Protocol 讓測試能塞假物件。"""

    def amber(self, symptom: str, fix: str) -> None: ...


# --------------------------------------------------------------------------
# 值的判讀與比較
# --------------------------------------------------------------------------


def is_blank(value: Any) -> bool:
    """判斷這格是不是「沒填」。

    ``0`` 與 ``False`` 一律**不算**空白（防禦性開發 7d）：用 ``if value`` 判斷會把
    員工數 0 當成缺值然後拿外部值蓋掉；但「員工數 0」是需要人去查的荒謬值，
    不是給機器覆蓋的空格。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in BLANK_TOKENS
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _as_decimal(value: Any) -> Decimal | None:
    """能轉成數字就轉，否則回 None（用於數值欄位的等值比較）。"""
    try:
        return Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _as_token_set(value: Any) -> frozenset[str]:
    """把清單型欄位（如 tech_stack）正規化成小寫集合，順序不影響比較。"""
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return frozenset(str(item).strip().lower() for item in items if not is_blank(item))


def values_match(left: Any, right: Any) -> bool:
    """兩個值是否代表同一件事（大小寫、千分位、清單順序都不算差異）。

    比較放寬是刻意的：把 ``"12000000.00"`` 與 ``12000000`` 判成衝突，
    會讓人工審查佇列被無意義的雜訊灌爆，真正的衝突反而被淹沒。
    """
    if isinstance(left, (list, tuple, set)) or isinstance(right, (list, tuple, set)):
        return _as_token_set(left) == _as_token_set(right)
    left_num, right_num = _as_decimal(left), _as_decimal(right)
    if left_num is not None and right_num is not None:
        return left_num == right_num
    return str(left).strip().lower() == str(right).strip().lower()


def display_value(value: Any) -> str:
    """把值排成人看得懂的字串，供 dry-run 變更計畫使用。"""
    if is_blank(value):
        return "（空白）"
    if isinstance(value, (list, tuple, set)):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return f"「{value}」"


# --------------------------------------------------------------------------
# 資料結構
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderResult:
    """單一外部資料源成功取回的整份資料。

    fields:  這個來源被授權提供哪些欄位（config 明列，不由回傳內容決定，
             免得資料源改版後突然開始供應它不該碰的欄位）。
    records: 以公司網域（小寫）為索引鍵的紀錄表。
    """

    provider_id: str
    display_name: str
    fields: tuple[str, ...]
    records: dict[str, dict[str, Any]]

    def lookup(self, contact: dict[str, Any]) -> dict[str, Any]:
        """先用網域找，找不到再退回 contact_id。都沒有就回空 dict（＝查無資料）。"""
        domain = str(contact.get("domain", "")).strip().lower()
        by_domain = self.records.get(domain)
        if isinstance(by_domain, dict):
            return by_domain
        by_id = self.records.get(str(contact.get("contact_id", "")).strip().lower())
        return by_id if isinstance(by_id, dict) else {}


@dataclass(frozen=True)
class ProviderFailure:
    """單一資料源失敗的紀錄，會出現在報表的「N 個來源無回應」與 CSV 中。"""

    provider_id: str
    display_name: str
    reason: str


@dataclass(frozen=True)
class FieldDecision:
    """對單一欄位的處置決定。**只有 action == filled 會真的寫回 CRM。**"""

    field_name: str
    action: str
    crm_value: Any
    external_value: Any
    provider: str | None
    note: str

    @property
    def is_write(self) -> bool:
        """是否會真的改動 CRM 欄位。"""
        return self.action == ACTION_FILLED

    @property
    def is_conflict(self) -> bool:
        """是否為需要人工判斷的衝突。"""
        return self.action == ACTION_CONFLICT

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {
            "field": self.field_name,
            "action": self.action,
            "crm_value": _jsonable(self.crm_value),
            "external_value": _jsonable(self.external_value),
            "provider": self.provider,
            "note": self.note,
        }


def _jsonable(value: Any) -> Any:
    """Decimal 轉字串保精度，其餘型別原樣送出。"""
    return str(value) if isinstance(value, Decimal) else value


@dataclass(frozen=True)
class EnrichedContact:
    """一位聯絡人的豐富化結果。``record`` 是「打算寫回 CRM 的樣子」。"""

    contact_id: str
    company: str
    domain: str
    crm_record: dict[str, Any]
    record: dict[str, Any]
    decisions: tuple[FieldDecision, ...]
    matched_providers: tuple[str, ...]
    failed_providers: tuple[str, ...]
    status: str

    @property
    def is_failed(self) -> bool:
        """外部完全查不到這家公司。此時 record 的目標欄位與 crm_record 完全相同。"""
        return self.status == STATUS_FAILED

    @property
    def filled(self) -> tuple[FieldDecision, ...]:
        """本次會真的寫回的欄位。"""
        return tuple(item for item in self.decisions if item.is_write)

    @property
    def conflicts(self) -> tuple[FieldDecision, ...]:
        """外部與 CRM 不一致、已保留 CRM 值、待人工判斷的欄位。"""
        return tuple(item for item in self.decisions if item.is_conflict)

    def to_dict(self) -> dict[str, Any]:
        """給報表、CSV 與 LLM 用的結構。"""
        return {
            "contact_id": self.contact_id,
            "company": self.company,
            "domain": self.domain,
            "status": self.status,
            "matched_providers": list(self.matched_providers),
            "failed_providers": list(self.failed_providers),
            "filled_fields": [item.field_name for item in self.filled],
            "conflict_fields": [item.field_name for item in self.conflicts],
            "decisions": [item.to_dict() for item in self.decisions],
        }


# --------------------------------------------------------------------------
# 取數（含部分失敗與 rate limit）
# --------------------------------------------------------------------------


def load_provider(entry: dict[str, Any], base_dir: Path) -> ProviderResult:
    """讀取單一資料源的 mock 檔。任何失敗都收斂成 ProviderError 交給上層降級。"""
    provider_id = str(entry.get("id", "")).strip()
    display_name = str(entry.get("display_name") or provider_id or "未命名資料源")
    if not provider_id:
        raise ProviderError("資料源設定缺少 id 欄位")

    path = base_dir / str(entry.get("mock_file", ""))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProviderError(f"{display_name} 無法讀取資料檔 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProviderError(f"{display_name} 的資料檔 JSON 解析失敗 {path}：{exc}") from exc

    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, dict):
        raise ProviderError(f"{display_name} 的資料檔缺少 records 物件：{path}")

    return ProviderResult(
        provider_id=provider_id,
        display_name=display_name,
        fields=tuple(str(name) for name in (entry.get("fields") or [])),
        records={str(key).strip().lower(): value for key, value in records.items()},
    )


def resolve_rate_limit(raw: Any, diagnostics: DiagnosticsLike | None = None) -> float:
    """把設定值收斂成合法的呼叫間隔；太小就拉回 1 秒並記琥珀燈。"""
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        _amber(diagnostics, f"rate_limit_seconds 不是數字（{raw!r}）", "本次改用 1.0 秒")
        return MIN_RATE_LIMIT_SECONDS
    if seconds < MIN_RATE_LIMIT_SECONDS:
        _amber(
            diagnostics,
            f"rate_limit_seconds={seconds} 低於外部 API 安全下限",
            f"已強制拉回 {MIN_RATE_LIMIT_SECONDS} 秒；被 429 封鎖一次要等 24 小時",
        )
        return MIN_RATE_LIMIT_SECONDS
    return seconds


def collect_providers(
    entries: Iterable[dict[str, Any]],
    base_dir: Path,
    diagnostics: DiagnosticsLike | None = None,
    is_mock: bool = True,
    rate_limit_seconds: Any = MIN_RATE_LIMIT_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[list[ProviderResult], list[ProviderFailure]]:
    """逐一取數。**任一來源失敗都不中斷迴圈**——這是部分失敗設計的入口。

    ``is_mock=True`` 時不套用呼叫間隔：讀本機 JSON 不是外部呼叫，
    在這裡 sleep 只會讓每晚的示範與測試白等，保護不到任何人的 API 額度。
    """
    delay = resolve_rate_limit(rate_limit_seconds, diagnostics)
    results: list[ProviderResult] = []
    failures: list[ProviderFailure] = []

    for index, entry in enumerate(entries):
        if index and not is_mock:
            sleeper(delay)
        try:
            results.append(load_provider(entry, base_dir))
        except ProviderError as exc:
            name = str(entry.get("display_name") or entry.get("id") or "未命名資料源")
            failures.append(ProviderFailure(str(entry.get("id", "")), name, str(exc)))
            _amber(
                diagnostics,
                f"{name} 無回應，本次以部分來源進行豐富化",
                f"檢查 {name} 憑證與 API 狀態後重跑；原因：{exc}",
            )
    return results, failures


def _amber(diagnostics: DiagnosticsLike | None, symptom: str, fix: str) -> None:
    """把非致命問題送進診斷矩陣的琥珀燈。"""
    if diagnostics is not None:
        diagnostics.amber(symptom, fix)


# --------------------------------------------------------------------------
# 欄位決策（安全鐵律的實作）
# --------------------------------------------------------------------------


def candidates_for(
    field_name: str, contact: dict[str, Any], results: Sequence[ProviderResult]
) -> list[tuple[str, Any]]:
    """蒐集各來源對這個欄位提供的非空值，依 config 的來源順序排列（前者優先）。"""
    found: list[tuple[str, Any]] = []
    for result in results:
        if field_name not in result.fields:
            continue
        value = result.lookup(contact).get(field_name)
        if not is_blank(value):
            found.append((result.display_name, value))
    return found


def decide_field(
    field_name: str,
    crm_value: Any,
    candidates: Sequence[tuple[str, Any]],
    is_protected: bool = False,
) -> FieldDecision:
    """決定單一欄位怎麼處置。**唯一會寫回 CRM 的分支是「CRM 原本空白」。**"""
    provider, external = candidates[0] if candidates else (None, None)

    if is_protected:
        return FieldDecision(
            field_name, ACTION_PROTECTED, crm_value, external, provider,
            "保護欄位：永不自動寫入，外部值僅供參考",
        )
    if not candidates:
        return FieldDecision(
            field_name, ACTION_NO_DATA, crm_value, None, None,
            "外部查無此欄位，保留 CRM 原值",
        )
    if is_blank(crm_value):
        return FieldDecision(
            field_name, ACTION_FILLED, crm_value, external, provider,
            "CRM 原為空白，補入外部值",
        )
    if values_match(crm_value, external):
        return FieldDecision(
            field_name, ACTION_CONFIRMED, crm_value, external, provider,
            "外部值與 CRM 一致，不需變更",
        )
    return FieldDecision(
        field_name, ACTION_CONFLICT, crm_value, external, provider,
        "外部值與 CRM 既有值不一致：保留 CRM 值，轉人工審查",
    )


def _resolve_status(
    matched_providers: Sequence[str], decisions: Sequence[FieldDecision]
) -> str:
    """沒有任何來源查到這家公司 -> enrichment_failed（既有值一律原封不動）。"""
    if not matched_providers:
        return STATUS_FAILED
    return STATUS_ENRICHED if any(item.is_write for item in decisions) else STATUS_NO_CHANGE


def enrich_contact(
    contact: dict[str, Any],
    results: Sequence[ProviderResult],
    failures: Sequence[ProviderFailure],
    target_fields: Sequence[str],
    protected_fields: Sequence[str],
    enriched_at: str,
) -> EnrichedContact:
    """對單一聯絡人做完整豐富化決策，回傳「打算寫回的樣子」與逐欄理由。"""
    protected = {str(name) for name in protected_fields}
    decisions = tuple(
        decide_field(
            field_name,
            contact.get(field_name),
            candidates_for(field_name, contact, results),
            field_name in protected,
        )
        for field_name in target_fields
    )
    matched = tuple(result.display_name for result in results if result.lookup(contact))

    record = dict(contact)
    for decision in decisions:
        if decision.is_write:
            record[decision.field_name] = decision.external_value

    status = _resolve_status(matched, decisions)
    record["enrichment_status"] = status
    record["last_enriched_at"] = enriched_at
    return EnrichedContact(
        contact_id=str(contact.get("contact_id", "")),
        company=str(contact.get("company", "")),
        domain=str(contact.get("domain", "")),
        crm_record=dict(contact),
        record=record,
        decisions=decisions,
        matched_providers=matched,
        failed_providers=tuple(item.display_name for item in failures),
        status=status,
    )


def enrich_all(
    contacts: Iterable[dict[str, Any]],
    results: Sequence[ProviderResult],
    failures: Sequence[ProviderFailure],
    target_fields: Sequence[str],
    protected_fields: Sequence[str],
    enriched_at: str,
) -> list[EnrichedContact]:
    """批次豐富化。單一聯絡人的處置互不影響，因此不需要例外隔離。"""
    return [
        enrich_contact(contact, results, failures, target_fields, protected_fields, enriched_at)
        for contact in contacts
    ]


# --------------------------------------------------------------------------
# 變更計畫（--dry-run 的核心產出）
# --------------------------------------------------------------------------


def _plan_lines_for(record: EnrichedContact) -> list[str]:
    """單一聯絡人的變更明細：哪些欄位、從什麼值變成什麼值、來源是誰。"""
    lines: list[str] = []
    for decision in record.filled:
        lines.append(
            f"    ✍ {decision.field_name:<16}"
            f"{display_value(decision.crm_value)} → {display_value(decision.external_value)}"
            f"｜來源：{decision.provider}"
        )
    for decision in record.conflicts:
        lines.append(
            f"    ⚠ {decision.field_name:<16}"
            f"保留 CRM {display_value(decision.crm_value)}"
            f"｜{decision.provider} 提供 {display_value(decision.external_value)}（不採用）"
        )
    return lines


def render_change_plan(records: Sequence[EnrichedContact]) -> str:
    """列出「將要修改哪些欄位、從什麼值變成什麼值」，讓人先看再決定。

    這份計畫在 ``--dry-run`` 一定會印。寫回客戶 CRM 是不可逆的動作，
    第一次上線時沒有人該憑信任按下去。
    """
    lines = ["變更計畫（尚未寫入 CRM）", "─" * 34]
    for record in records:
        detail = _plan_lines_for(record)
        if record.is_failed:
            lines.append(f"  {record.contact_id} {record.company}：外部查無資料，維持原狀不變更")
        elif not detail:
            lines.append(f"  {record.contact_id} {record.company}：無變更")
        else:
            lines.append(f"  {record.contact_id} {record.company}")
            lines.extend(detail)

    total_fields = sum(len(record.filled) for record in records)
    total_conflicts = sum(len(record.conflicts) for record in records)
    lines.append("─" * 34)
    lines.append(f"合計：{total_fields} 個欄位待寫入、{total_conflicts} 個衝突待人工判斷")
    return "\n".join(lines)


def partial_banner(failures: Sequence[ProviderFailure]) -> str:
    """產出「⚠️ 2 個來源無回應：LinkedIn、Apollo」橫幅；全部正常時回空字串。

    橫幅放報表最上方：讀者在看到任何分數之前，就要知道這批分數是用不完整的
    資料算出來的，否則會拿殘缺名單去排今天的電話順序。
    """
    if not failures:
        return ""
    names = "、".join(item.display_name for item in failures)
    return f"⚠️ {len(failures)} 個來源無回應：{names}"


def to_csv_rows(
    records: Sequence[EnrichedContact], scores: dict[str, Any]
) -> list[dict[str, Any]]:
    """把結果攤平成 CSV 列（欄位順序見 CSV_COLUMNS）。"""
    rows: list[dict[str, Any]] = []
    for record in records:
        score = scores.get(record.contact_id)
        rows.append(
            {
                "contact_id": record.contact_id,
                "company": record.company,
                "domain": record.domain,
                "status": record.status,
                "score": "" if score is None else str(score.total),
                "band": "" if score is None else score.band,
                "grade": "" if score is None else score.grade,
                "is_stale": "" if score is None else str(score.is_stale).lower(),
                "days_since_contact": (
                    ""
                    if score is None or score.days_since_contact is None
                    else str(score.days_since_contact)
                ),
                "filled_fields": ";".join(item.field_name for item in record.filled),
                "conflict_fields": ";".join(item.field_name for item in record.conflicts),
                "missing_inputs": "" if score is None else ";".join(score.missing_inputs),
                "failed_providers": ";".join(record.failed_providers),
            }
        )
    return rows
