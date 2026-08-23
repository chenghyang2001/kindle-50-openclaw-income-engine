"""demo25 — 動態客戶媒合引擎（房地產 / B2B 批發）主流程。

由 Property Portal Webhook 觸發（或排程每 15 分鐘掃一次）：
掃描新上架物件 → 與所有註冊買方／批發商的**客觀**結構化條件加權比對 →
達門檻即發送個人化推薦並記錄去重 → 低詢問度物件另外產出 Vendor Pricing Pack。

本模組有三條不能退讓的紀律：

1. **法遵優先於媒合**（公平住房法）：任何疑似受保護特徵的條件欄位一出現，
   整支程式拒絕執行並回傳非 0 退出碼。不是「略過該欄位繼續跑」——
   靜默略過會讓違規條件看起來有生效，是比當場失敗更危險的狀態。
2. **同一買方對同一物件只通知一次**：去重狀態檔是這個模組的信任基礎。
   買方條件一變更，該買方的去重紀錄整批清空，讓既有物件重新走一次比對。
3. **對外送出前必經內部通訊預檢**（apxG_p03 全域安全閥）：
   `--dry-run` 本身就是預檢模式；非 dry-run 時程式會先自行跑一次不觸網的
   通道預檢，失敗即紅色警報中止。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402

from audit import AuditError, AuditLog  # noqa: E402
from matcher import (  # noqa: E402
    Buyer,
    ComplianceError,
    Listing,
    MatchResult,
    MatchingCriteria,
    MatchingError,
    TIER_STRONG,
    apply_criteria_change,
    assert_criteria_compliant,
    is_already_notified,
    is_low_enquiry,
    load_state,
    mark_notified,
    minutes_since_listed,
    save_state,
    score_match,
)

MODULE_ID = "25"
MODULE_NAME = "動態客戶媒合引擎"
MODULE_SLUG = "demo25-client-matching"

NOTIFY_CHANNELS = ("console", "telegram", "gmail", "line", "whatsapp")
DEFAULT_CONFIG = _DEMO_DIR / "config.yaml"
RECOMMENDATION_PROMPT = _DEMO_DIR / "prompts" / "recommendation_email.md"
RECOMMENDATION_FIXTURE = _DEMO_DIR / "mock" / "recommendation_fixture.md"
VENDOR_PROMPT = _DEMO_DIR / "prompts" / "vendor_pricing_pack.md"
VENDOR_FIXTURE = _DEMO_DIR / "mock" / "vendor_pack_fixture.md"

CONTEXT_NOTE = (
    "收件人是房地產買方或 B2B 批發商。只描述物件客觀事實與買方自填的客觀條件；"
    "任何涉及種族、宗教、家庭狀況、身心障礙等受保護特徵的描述都違反公平住房法。"
)

DELIVERY_SENT = "sent"
DELIVERY_DRAFT = "draft"
DELIVERY_DRY_RUN = "dry_run"


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT.md §6，另加本模組專屬三個）"""
    parser = argparse.ArgumentParser(prog=MODULE_SLUG, description=f"{MODULE_NAME}（房地產 / B2B）")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True,
                      help="離線模式：讀 mock/ 的物件與買方資料，不觸網、不呼叫 API（預設）")
    mode.add_argument("--live", dest="mock", action="store_false",
                      help="串接真實物件／買方 API 與 Claude API")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="跑完整流程但不發送推薦、不更新去重狀態檔（同時作為對外通訊安全閥）")
    parser.add_argument("--notify", choices=NOTIFY_CHANNELS, default=None,
                        help="通知管道，未指定時採用 config.yaml 的 runtime.notify_channel")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="設定檔路徑（預設為本目錄的 config.yaml）")
    parser.add_argument("--state-file", dest="state_file", default=None,
                        help="通知去重狀態檔路徑，未指定時採用 config.yaml 的 state.state_file")
    parser.add_argument("--audit-file", dest="audit_file", default=None,
                        help="稽核軌跡 JSONL 路徑，未指定時採用 config.yaml 的 state.audit_file")
    parser.add_argument("--now", dest="now", default=None,
                        help="以指定的 ISO 8601 時間作為「現在」（用於重現 60 分鐘時效判定）")
    parser.add_argument("--buyers-file", dest="buyers_file", default=None,
                        help="覆寫 mock 買方資料路徑（測試法遵閘門時指向違規範例檔）")
    return parser


@dataclass
class _Context:
    """單次執行共用的環境（避免每個 helper 都要傳十個參數）"""

    config: dict
    criteria: MatchingCriteria
    diagnostics: Diagnostics
    audit: AuditLog
    client: LLMClient
    gate: AutonomyGate
    state: dict
    now: datetime
    channel: str
    is_mock: bool
    is_dry_run: bool
    calendly_url: str
    is_sms_enabled: bool
    sla_minutes: int

    @property
    def mode_label(self) -> str:
        """執行模式標籤，供結果 dict 與稽核軌跡共用同一個字串"""
        return "mock" if self.is_mock else "live"


@dataclass
class _Outcome:
    """比對迴圈的產物分桶"""

    notifications: list[dict[str, Any]] = field(default_factory=list)
    suppressed_duplicates: list[dict[str, Any]] = field(default_factory=list)
    near_misses: list[dict[str, Any]] = field(default_factory=list)
    sla_breaches: list[dict[str, Any]] = field(default_factory=list)
    no_match_count: int = 0


# ---------------------------------------------------------------------------
# 環境組裝
# ---------------------------------------------------------------------------

def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律相對於本 demo 目錄解析，避免受呼叫端工作目錄影響"""
    path = Path(raw)
    return path if path.is_absolute() else (_DEMO_DIR / path)


def _build_gate(runtime: dict, diagnostics: Diagnostics) -> AutonomyGate:
    """依 config 的 runtime 區塊建立自主權閘門，並把警告轉成 AMBER"""
    raw_level = str(runtime.get("autonomy", "draft"))
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        diagnostics.red(
            symptom=f"runtime.autonomy 值不合法：{raw_level!r}",
            cause="設定檔寫了不存在的自主權層級",
            fix="改為 read_only / draft / supervised_auto 其中之一",
        )
        raise
    gate = AutonomyGate(
        level=level,
        approved_senders=list(runtime.get("approved_senders") or []),
        days_in_draft=int(runtime.get("days_in_draft", 0) or 0),
    )
    for warning in gate.warnings:
        diagnostics.amber(symptom=warning, fix="維持 draft 直到滿 14 天且客戶已簽核")
    return gate


def _resolve_now(args: argparse.Namespace, sources: dict) -> datetime:
    """決定本次執行的「現在」；mock 模式優先用 config 的固定時間讓結果可重現"""
    raw = args.now or (sources.get("mock_now") if args.mock else None)
    if not raw:
        return datetime.now(timezone.utc)
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchingError(f"--now / sources.mock_now 不是合法的 ISO 8601 時間：{raw!r}") from exc
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _resolve_calendly(config: dict, diagnostics: Diagnostics) -> str:
    """取看屋預約連結；環境變數未設定時記 AMBER 並略過，不把 ${VAR} 字面值寄給買方"""
    raw = str((config.get("engagement") or {}).get("calendly_url", "")).strip()
    if not raw or raw.startswith("${"):
        diagnostics.amber(
            symptom="高優先級推薦缺少看屋預約連結（CALENDLY_VIEWING_URL 未設定）",
            fix="設定環境變數 CALENDLY_VIEWING_URL 後重跑；本次推薦將略過預約連結",
        )
        return ""
    return raw


# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: int) -> Any:
    """--live 模式的資料抓取（標準庫 urllib，不用 requests）"""
    request = urllib.request.Request(url, headers={"accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError) as exc:
        raise MatchingError(f"資料源讀取失敗（{url}）：{exc}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise MatchingError(f"資料源回傳的不是合法 JSON（{url}）：{exc}") from exc


def _read_local_json(path: Path) -> Any:
    """讀本地 mock 檔；讀不到或格式壞掉一律明確拋錯"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MatchingError(f"離線資料讀取失敗（{path}）：{exc}") from exc


def _load_dataset(
    sources: dict, *, is_mock: bool, mock_path: str | Path, url_key: str, collection: str
) -> list[dict[str, Any]]:
    """載入物件或買方資料集，回傳原始 dict 陣列"""
    if is_mock:
        raw = _read_local_json(_resolve_path(mock_path))
    else:
        url = str(sources.get(url_key, "")).strip()
        if not url or url.startswith("${"):
            raise MatchingError(f"--live 模式需要設定 sources.{url_key}，目前值為 {url!r}")
        raw = _fetch_json(url, int(sources.get("request_timeout_seconds", 15)))
    records = raw.get(collection) if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise MatchingError(f"資料源的 {collection} 必須是陣列，收到 {type(records).__name__}")
    return [item for item in records if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# 法遵閘門 + 安全閥
# ---------------------------------------------------------------------------

def _guard_compliance(
    criteria: MatchingCriteria, buyers: list[Buyer], allowed: list[str], audit: AuditLog
) -> None:
    """config 的比對欄位與每一位買方的條件都要過白名單；違規即拋錯中止整次執行"""
    assert_criteria_compliant(criteria.all_fields, allowed, source="config.yaml 的 matching 區塊")
    for buyer in buyers:
        assert_criteria_compliant(
            buyer.criteria, allowed, source=f"買方 {buyer.buyer_id}（{buyer.name}）的 criteria"
        )
    audit.record(
        "compliance_check_passed",
        checked_buyers=len(buyers),
        criteria_fields=list(criteria.all_fields),
        allowed_fields=list(allowed),
    )


def _preflight(ctx: _Context) -> None:
    """全域安全閥（apxG_p03）：對外送出前先跑一次不觸網的內部通訊預檢"""
    probe_text = f"[PREFLIGHT] {MODULE_SLUG} 內部通訊測試｜run_id={ctx.audit.run_id}"
    try:
        probe = Notifier(ctx.channel)
        segments = Notifier.split_message(probe_text)
    except NotifierError as exc:
        ctx.audit.record("preflight_failed", channel=ctx.channel, reason=str(exc))
        ctx.diagnostics.red(
            symptom=f"通知管道 {ctx.channel!r} 預檢失敗：{exc}",
            cause="config.runtime.notify_channel 或 --notify 指定了不支援的管道",
            fix=f"改用 {', '.join(Notifier.SUPPORTED)} 其中之一後重跑",
        )
        return
    ctx.audit.record("preflight_passed", channel=probe.channel, segments=len(segments))


# ---------------------------------------------------------------------------
# 推薦內容組裝
# ---------------------------------------------------------------------------

def _money(value: Decimal) -> str:
    """金額格式化（千分位、無小數）；全程 Decimal，不經 float"""
    return f"NT$ {value:,.0f}"


def _listing_facts(listing: Listing) -> str:
    """物件客觀事實條列（只有物件本身的屬性，不含任何買方特徵）"""
    features = "、".join(sorted(listing.features)) or "（未標註）"
    return (
        f"{listing.bedrooms} 房 {listing.bathrooms} 衛｜{listing.floor_area} 坪｜"
        f"屋齡 {listing.property_age_years} 年｜郵遞區號 {listing.postcode}｜"
        f"類型 {listing.property_type}｜特色 {features}"
    )


def _build_recommendation_input(
    listing: Listing, buyer: Buyer, match: MatchResult, minutes: int
) -> str:
    """組出餵給推薦信提示詞的結構化資料"""
    matched = "、".join(match.hard_hits + match.soft_hits) or "（無）"
    unmatched = "、".join(match.hard_misses + match.soft_misses) or "（無）"
    return "\n".join(
        [
            f"BUYER_NAME: {buyer.name}",
            f"MATCH_SCORE: {match.score}",
            f"MATCH_TIER: {match.tier}",
            f"LISTING_TITLE: {listing.title}",
            f"LISTING_PRICE: {listing.price}",
            f"LISTING_FACTS: {_listing_facts(listing)}",
            f"MATCHED_CRITERIA: {matched}",
            f"UNMATCHED_CRITERIA: {unmatched}",
            f"MINUTES_SINCE_LISTED: {minutes}",
            f"IS_HIGH_PRIORITY: {str(match.is_high_priority).lower()}",
        ]
    )


def _render_recommendation(
    ctx: _Context, listing: Listing, buyer: Buyer, match: MatchResult, minutes: int
) -> str:
    """呼叫 LLM 把媒合結果寫成個人化推薦信；mock 模式讀 fixture 不花錢"""
    system = RECOMMENDATION_PROMPT.read_text(encoding="utf-8")
    user = _build_recommendation_input(listing, buyer, match, minutes)
    fixture = RECOMMENDATION_FIXTURE if ctx.is_mock else None
    return ctx.client.complete(system=system, user=user, max_tokens=600, fixture=fixture)


def _engagement_block(ctx: _Context, match: MatchResult) -> list[str]:
    """高優先級（Perfect 90+）附加看屋預約連結與簡訊提示（分支 A）"""
    if not match.is_high_priority:
        return []
    lines = ["", "── 看屋預約（高優先級） ──"]
    lines.append(f"  {ctx.calendly_url}" if ctx.calendly_url else "  （預約連結未設定，請業務人工補上）")
    if ctx.is_sms_enabled:
        lines.append("  ＋同步標記發送簡訊提醒")
    return lines


def _compose_notification(
    ctx: _Context, listing: Listing, buyer: Buyer, match: MatchResult, body: str, minutes: int
) -> str:
    """組出最終要送出的推薦通知全文"""
    unmatched = "、".join(match.hard_misses + match.soft_misses) or "（無，條件全數符合）"
    lines = [
        f"您好，{buyer.name}：",
        "",
        body.strip(),
        "",
        "── 物件資訊 ──",
        f"  編號：{listing.listing_id}｜{listing.title}",
        f"  總價：{_money(listing.price)}",
        f"  規格：{_listing_facts(listing)}",
        "",
        "── 媒合依據（僅使用您填寫的客觀條件） ──",
        f"  分數：{match.score}（{match.tier}）｜上架後 {minutes} 分鐘內通知",
        f"  命中條件：{'、'.join(match.hard_hits + match.soft_hits) or '（無）'}",
        f"  條件落差：{unmatched}",
    ]
    return "\n".join(lines + _engagement_block(ctx, match))


def _deliver(ctx: _Context, text: str, subject: str, recipient: str) -> tuple[str, bool]:
    """依自主權層級決定送出或留為草稿，回傳 (delivery, is_notified)"""
    if ctx.is_dry_run:
        return DELIVERY_DRY_RUN, False
    if ctx.gate.can_send(recipient):
        return DELIVERY_SENT, Notifier(ctx.channel).send(text, subject=subject)
    # 未取得自動送出授權：降級為草稿，只印在本機供業務過目，不推到對外管道
    drafted = f"【草稿・待人工核准】收件人 {recipient} 未取得自動送出授權\n\n{text}"
    return DELIVERY_DRAFT, Notifier("console").send(drafted, subject=subject)


# ---------------------------------------------------------------------------
# 比對迴圈
# ---------------------------------------------------------------------------

def _record_non_push(ctx: _Context, outcome: _Outcome, listing: Listing, match: MatchResult) -> None:
    """未達推播門檻：Strong 段（75-79）記為 near-miss 供降價談判包使用，其餘只計數"""
    if match.tier == TIER_STRONG:
        outcome.near_misses.append(
            {
                "listing_id": listing.listing_id,
                "buyer_id": match.buyer_id,
                "score": str(match.score),
                "gap_fields": list(match.hard_misses + match.soft_misses),
            }
        )
        ctx.audit.record(
            "match_below_threshold",
            listing_id=listing.listing_id,
            buyer_id=match.buyer_id,
            score=str(match.score),
            tier=match.tier,
            threshold=str(ctx.criteria.match_score_threshold),
        )
        return
    outcome.no_match_count += 1


def _record_duplicate(ctx: _Context, outcome: _Outcome, listing: Listing, match: MatchResult) -> None:
    """去重：同一買方對同一物件不重複打擾"""
    outcome.suppressed_duplicates.append(
        {"listing_id": listing.listing_id, "buyer_id": match.buyer_id, "score": str(match.score)}
    )
    ctx.audit.record(
        "notification_suppressed_duplicate",
        listing_id=listing.listing_id,
        buyer_id=match.buyer_id,
        score=str(match.score),
    )


def _check_sla(ctx: _Context, outcome: _Outcome, listing: Listing, buyer: Buyer, minutes: int) -> bool:
    """通知時效檢查（apxG_p12：上架 60 分鐘內）；逾時記 AMBER + 稽核事件但仍照常通知"""
    if minutes <= ctx.sla_minutes:
        return True
    outcome.sla_breaches.append(
        {"listing_id": listing.listing_id, "buyer_id": buyer.buyer_id, "minutes": minutes}
    )
    ctx.diagnostics.amber(
        symptom=f"{listing.listing_id} → {buyer.buyer_id} 上架後 {minutes} 分鐘才通知"
                f"（規格要求 {ctx.sla_minutes} 分鐘內）",
        fix="縮短排程間隔或改接 Property Portal Webhook 即時觸發",
    )
    ctx.audit.record(
        "sla_breach",
        listing_id=listing.listing_id,
        buyer_id=buyer.buyer_id,
        minutes_since_listed=minutes,
        sla_minutes=ctx.sla_minutes,
    )
    return False


def _push(ctx: _Context, outcome: _Outcome, listing: Listing, buyer: Buyer, match: MatchResult) -> None:
    """產生並送出一封個人化推薦，成功送達才寫入去重紀錄"""
    minutes = minutes_since_listed(listing, ctx.now)
    is_within_sla = _check_sla(ctx, outcome, listing, buyer, minutes)
    body = _render_recommendation(ctx, listing, buyer, match, minutes)
    text = _compose_notification(ctx, listing, buyer, match, body, minutes)
    subject = f"【專屬推薦】{listing.title}｜媒合 {match.score} 分"
    delivery, is_notified = _deliver(ctx, text, subject, buyer.email)
    # 只有真的送達（sent 或已印給人看的 draft）才記去重，避免推薦其實沒發出去卻永久被擋
    if is_notified:
        mark_notified(
            ctx.state, buyer.buyer_id, listing.listing_id,
            score=match.score, tier=match.tier, at=ctx.now,
        )
    ctx.audit.record(
        "notification_sent", listing_id=listing.listing_id, buyer_id=buyer.buyer_id,
        score=str(match.score), tier=match.tier, is_high_priority=match.is_high_priority,
        delivery=delivery, is_notified=is_notified, channel=ctx.channel,
        minutes_since_listed=minutes, is_within_sla=is_within_sla,
        matched_fields=list(match.hard_hits + match.soft_hits),
        unmatched_fields=list(match.hard_misses + match.soft_misses),
    )
    outcome.notifications.append(
        {
            "listing_id": listing.listing_id, "buyer_id": buyer.buyer_id,
            "buyer_name": buyer.name, "score": str(match.score), "tier": match.tier,
            "is_high_priority": match.is_high_priority, "delivery": delivery,
            "is_notified": is_notified, "minutes_since_listed": minutes,
            "is_within_sla": is_within_sla, "text": text,
        }
    )


def _evaluate(
    ctx: _Context, listings: list[Listing], buyers: list[Buyer], matches: list[MatchResult]
) -> _Outcome:
    """物件 × 買方全比對；達門檻且未通知過才推播"""
    outcome = _Outcome()
    for listing in listings:
        for buyer in buyers:
            match = score_match(listing, buyer, ctx.criteria)
            matches.append(match)
            if not match.is_pushable:
                _record_non_push(ctx, outcome, listing, match)
            elif is_already_notified(ctx.state, buyer.buyer_id, listing.listing_id):
                _record_duplicate(ctx, outcome, listing, match)
            else:
                _push(ctx, outcome, listing, buyer, match)
    return outcome


def _sync_buyer_criteria(ctx: _Context, buyers: list[Buyer]) -> list[str]:
    """買方條件變更偵測：指紋一變就清空該買方去重紀錄，讓既有物件重新比對"""
    changed: list[str] = []
    for buyer in buyers:
        if not apply_criteria_change(ctx.state, buyer):
            continue
        changed.append(buyer.buyer_id)
        ctx.audit.record(
            "criteria_changed", buyer_id=buyer.buyer_id,
            action="cleared_notification_history_for_rematch",
        )
    return changed


# ---------------------------------------------------------------------------
# 分支 B：低詢問度物件 → Vendor Pricing Pack
# ---------------------------------------------------------------------------

def _render_vendor_pack(ctx: _Context, listing: Listing, near_misses: list[dict[str, Any]]) -> str:
    """呼叫 LLM 產出給屋主的降價談判包；mock 模式讀 fixture"""
    gap_fields: list[str] = []
    for item in near_misses:
        gap_fields.extend(item["gap_fields"])
    summary = (
        f"{len(near_misses)} 位買方分數逼近門檻但未達標，"
        f"主要落差欄位：{'、'.join(sorted(set(gap_fields))) or '（無）'}"
    )
    user = "\n".join(
        [
            f"LISTING_TITLE: {listing.title}",
            f"LISTING_PRICE: {listing.price}",
            f"DAYS_ON_MARKET: {listing.days_on_market}",
            f"ENQUIRIES_LAST_7_DAYS: {listing.enquiries_last_7_days}",
            f"LISTING_FACTS: {_listing_facts(listing)}",
            f"NEAR_MISS_SUMMARY: {summary}",
        ]
    )
    system = VENDOR_PROMPT.read_text(encoding="utf-8")
    fixture = VENDOR_FIXTURE if ctx.is_mock else None
    return ctx.client.complete(system=system, user=user, max_tokens=600, fixture=fixture)


def _build_vendor_packs(
    ctx: _Context, listings: list[Listing], outcome: _Outcome
) -> list[dict[str, Any]]:
    """掃出低詢問度物件並逐一產生 Vendor Pricing Pack"""
    settings = ctx.config.get("vendor_pack") or {}
    max_enquiries = int(settings.get("max_enquiries_last_7_days", 2))
    min_days = int(settings.get("min_days_on_market", 14))
    packs: list[dict[str, Any]] = []
    for listing in listings:
        if not is_low_enquiry(listing, max_enquiries=max_enquiries, min_days_on_market=min_days):
            continue
        near = [m for m in outcome.near_misses if m["listing_id"] == listing.listing_id]
        text = _render_vendor_pack(ctx, listing, near)
        ctx.audit.record(
            "vendor_pack_generated", listing_id=listing.listing_id,
            days_on_market=listing.days_on_market,
            enquiries_last_7_days=listing.enquiries_last_7_days, near_miss_count=len(near),
        )
        packs.append(
            {
                "listing_id": listing.listing_id, "title": listing.title,
                "days_on_market": listing.days_on_market,
                "enquiries_last_7_days": listing.enquiries_last_7_days, "text": text,
            }
        )
    return packs


# ---------------------------------------------------------------------------
# 報告與結果
# ---------------------------------------------------------------------------

def _build_report(ctx: _Context, outcome: _Outcome, packs: list[dict[str, Any]], counts: dict) -> str:
    """組出人看的執行報告"""
    high = sum(1 for n in outcome.notifications if n["is_high_priority"])
    lines = [
        f"【動態客戶媒合引擎】{ctx.now:%Y-%m-%d %H:%M %z}",
        f"物件 {counts['listings']} 筆 × 買方 {counts['buyers']} 位 = {counts['evaluated']} 組比對",
        f"推播 {len(outcome.notifications)} 封"
        f"（高優先級 {high}）｜去重擋下 {len(outcome.suppressed_duplicates)} 封"
        f"｜逾時效 {len(outcome.sla_breaches)} 封",
        f"門檻 {ctx.criteria.match_score_threshold} 分以下未推播："
        f"逼近門檻 {len(outcome.near_misses)} 組、明顯不符 {outcome.no_match_count} 組",
        "",
        "── 推播明細 ──",
    ]
    lines += [
        f"  {'★' if n['is_high_priority'] else ' '} {n['listing_id']} → {n['buyer_id']}"
        f"（{n['buyer_name']}）｜{n['score']} 分／{n['tier']}｜{n['minutes_since_listed']} 分鐘"
        f"｜{n['delivery']}" for n in outcome.notifications
    ] or ["  （無）"]
    lines += ["", "── 去重擋下（本次不重複打擾） ──"]
    lines += [
        f"  ✗ {d['listing_id']} → {d['buyer_id']}｜{d['score']} 分（先前已通知）"
        for d in outcome.suppressed_duplicates
    ] or ["  （無）"]
    if packs:
        lines += ["", f"── 低詢問度物件 · Vendor Pricing Pack（{len(packs)} 份） ──"]
        for pack in packs:
            lines += [f"  ▸ {pack['listing_id']}｜{pack['title']}", pack["text"].strip(), ""]
    return "\n".join(lines)


def _build_result(
    ctx: _Context, outcome: _Outcome, matches: list[MatchResult], packs: list[dict[str, Any]],
    counts: dict, report: str, changed_buyers: list[str], state_path: Path,
) -> dict[str, Any]:
    """組出供測試斷言與下游使用的結果 dict（全部欄位皆可 JSON 序列化）。

    前六個鍵刻意採用 CONTRACT.md §6「已知技術債」段落建議的標準命名
    （module_id / module_name / mode / dry_run / warnings / amber_count），
    讓 bundle-quickstart 的轉接層不必再為本模組多寫一組 .get() 分支。
    """
    return {
        "module_id": MODULE_ID,
        "module_name": MODULE_NAME,
        "mode": ctx.mode_label,
        "dry_run": ctx.is_dry_run,
        "warnings": list(ctx.gate.warnings),
        "amber_count": ctx.diagnostics.amber_count,
        "run_id": ctx.audit.run_id,
        "now": ctx.now.isoformat(),
        "listings": counts["listings"],
        "buyers": counts["buyers"],
        "evaluated": counts["evaluated"],
        "matches": [m.as_dict() for m in matches],
        "notifications": outcome.notifications,
        "suppressed_duplicates": outcome.suppressed_duplicates,
        "near_misses": outcome.near_misses,
        "no_match_count": outcome.no_match_count,
        "sla_breaches": outcome.sla_breaches,
        "criteria_changed_buyers": changed_buyers,
        "vendor_packs": packs,
        "report": report,
        "notify_channel": ctx.channel,
        "state_file": str(state_path),
        "audit_file": str(ctx.audit.path),
        "audit_events": [entry["event"] for entry in ctx.audit.records],
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _prepare_context(args: argparse.Namespace, config: dict, state_path: Path) -> _Context:
    """把設定檔與旗標組裝成單次執行的環境"""
    diagnostics = Diagnostics(MODULE_SLUG, exit_on_red=False)
    matching = config.get("matching") or {}
    runtime = config.get("runtime") or {}
    state_cfg = config.get("state") or {}
    audit_path = _resolve_path(args.audit_file or state_cfg.get("audit_file", "state/audit.jsonl"))
    return _Context(
        config=config,
        criteria=MatchingCriteria.from_config(matching),
        diagnostics=diagnostics,
        audit=AuditLog(audit_path, MODULE_SLUG),
        client=LLMClient(mock=args.mock, context_note=CONTEXT_NOTE),
        gate=_build_gate(runtime, diagnostics),
        state=load_state(state_path),
        now=_resolve_now(args, config.get("sources") or {}),
        channel=args.notify or str(runtime.get("notify_channel", "console")),
        is_mock=bool(args.mock),
        is_dry_run=bool(args.dry_run),
        calendly_url=_resolve_calendly(config, diagnostics),
        is_sms_enabled=bool((config.get("engagement") or {}).get("is_sms_enabled_for_high_priority")),
        sla_minutes=int(matching.get("notify_sla_minutes", 60)),
    )


def _load_entities(args: argparse.Namespace, sources: dict) -> tuple[list[Listing], list[Buyer]]:
    """載入並驗證物件與買方；任一筆資料不合法就整批拒絕，不做半套比對"""
    listing_rows = _load_dataset(
        sources, is_mock=args.mock, mock_path=sources.get("listings_mock", "mock/listings.json"),
        url_key="listings_url", collection="listings",
    )
    buyer_rows = _load_dataset(
        sources, is_mock=args.mock,
        mock_path=args.buyers_file or sources.get("buyers_mock", "mock/buyers.json"),
        url_key="buyers_url", collection="buyers",
    )
    return (
        [Listing.from_dict(row) for row in listing_rows],
        [Buyer.from_dict(row) for row in buyer_rows],
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程並回傳結果 dict（不做 sys.exit，交給 main() 決定退出碼）"""
    required_env = None if args.mock else ["ANTHROPIC_API_KEY"]
    config = load_config(_resolve_path(args.config), required_env=required_env)
    state_cfg = config.get("state") or {}
    state_path = _resolve_path(args.state_file or state_cfg.get("state_file", "state/notifications.json"))

    ctx = _prepare_context(args, config, state_path)
    sources = config.get("sources") or {}
    listings, buyers = _load_entities(args, sources)
    ctx.audit.record(
        "run_started", mode=ctx.mode_label, is_dry_run=ctx.is_dry_run,
        listings=len(listings), buyers=len(buyers), now=ctx.now.isoformat(),
    )
    # 法遵閘門必須在任何比對／通訊之前——違規時整次執行中止，不做半套推薦
    allowed = [str(f) for f in (config.get("matching") or {}).get("allowed_criteria_fields") or ()]
    _guard_compliance(ctx.criteria, buyers, allowed, ctx.audit)
    if not ctx.is_dry_run:
        _preflight(ctx)

    changed_buyers = _sync_buyer_criteria(ctx, buyers)
    matches: list[MatchResult] = []
    outcome = _evaluate(ctx, listings, buyers, matches)
    packs = _build_vendor_packs(ctx, listings, outcome)
    if not ctx.is_dry_run:
        save_state(state_path, ctx.state)

    counts = {"listings": len(listings), "buyers": len(buyers),
              "evaluated": len(listings) * len(buyers)}
    report = _build_report(ctx, outcome, packs, counts)
    ctx.audit.record(
        "run_completed", notifications=len(outcome.notifications),
        suppressed=len(outcome.suppressed_duplicates), sla_breaches=len(outcome.sla_breaches),
        vendor_packs=len(packs), amber_count=ctx.diagnostics.amber_count,
    )
    if not outcome.sla_breaches:
        ctx.diagnostics.green(f"{len(outcome.notifications)} 封推薦皆在 {ctx.sla_minutes} 分鐘時效內")
    return _build_result(
        ctx, outcome, matches, packs, counts, report, changed_buyers, state_path
    )


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳退出碼。

    退出碼約定（讓排程器分辨「壞掉」與「有事要看」）：
        0 = 全部推薦皆在時效內、無警示
        2 = 流程完成，但有 AMBER（逾時效／缺預約連結／自主權警告）
        1 = 法遵違規、紅色警報或致命錯誤，本次沒有任何推薦送出
    """
    args = build_parser().parse_args()
    try:
        result = run(args)
    except ComplianceError as exc:
        print(f"法遵違規，已拒絕執行：{exc}", file=sys.stderr)
        return 1
    except RedAlert as exc:
        print(f"紅色警報：{exc}", file=sys.stderr)
        return 1
    except (MatchingError, AuditError, NotifierError, FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    print(result["report"])
    print(
        f"\n完成：推播 {len(result['notifications'])} 封｜"
        f"去重 {len(result['suppressed_duplicates'])} 封｜"
        f"逾時效 {len(result['sla_breaches'])} 封｜"
        f"降價談判包 {len(result['vendor_packs'])} 份｜"
        f"稽核 {len(result['audit_events'])} 筆 → {result['audit_file']}",
        file=sys.stderr,
    )
    return 2 if result["amber_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
