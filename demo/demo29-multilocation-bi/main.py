"""demo29 — 多據點商業智慧匯總（Level 3 企業級，自動化 #29）。

每週一 07:00 把各據點異質系統（Shopify / Square / POS）的數字標準化、算出
Composite Score、分成 Top Tier / Core / Focus Required、偵測偏離 4 週滾動基準線
超過 15% 的異常，然後**依角色分送兩種完全不同的報表**：

* **總部（`--role hq`）**：Full Pack —— 全網加總、跨據點排名、同儕中位數比較。
* **店長（`--role site_manager --site <id>`）**：Own-site only —— 只有自己那一家。

**本模組的靈魂是資料層的權限隔離**：店長視野下，其他據點的 API 從頭到尾不會被
呼叫，他拿到的資料集裡根本不存在別家的數字（不是「有但沒印出來」）。權限設定
缺失或角色未知時一律退回最小權限（own-site only），不會預設成 Full Pack。
每一次資料存取都寫一行 JSONL 稽核軌跡：誰、何時、看了哪些據點。

用法：

    python main.py --mock                                   # 預設店長視野（config 指定）
    python main.py --mock --role hq                         # 總部 Full Pack
    python main.py --mock --role site_manager --site khh-boai
    python main.py --mock --role hq --dry-run               # 產出但不發送
    python main.py --mock --audit-file /tmp/a.jsonl --state-file /tmp/b.json
    python main.py --live --role hq                         # 串真實 API（缺憑證明確退出）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

MODULE_DIR = Path(__file__).resolve().parent
# demo/ 進 sys.path 才能匯入 _shared；本目錄也要進，
# 這樣 pytest 從別的工作目錄呼叫時仍找得到 rollup / access_control / audit。
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

import audit  # noqa: E402
import rollup  # noqa: E402
from access_control import (  # noqa: E402
    PACK_FULL,
    AccessScope,
    LeakDetected,
    Role,
    assert_no_leak,
    hidden_identifiers,
    resolve_scope,
)

MODULE_NAME = "demo29-multilocation-bi"

#: 第 04 章：附在 system prompt 尾端可減少約 40% 不相關輸出。
CONTEXT_NOTE = (
    "這是每週一早上自動發送的多據點營運報表。輸入 JSON 已依讀者權限裁切："
    "pack 為 own_site_only 時，讀者是單一據點店長，絕對不得提及任何其他據點、"
    "不得提及全網家數、不得暗示相對排名。只陳述 JSON 中實際存在的數字。"
)

#: 提示詞檔讀不到時的最低限度後備。刻意保留而不是直接失敗——
#: 表格本體已經有價值，不該因為少一段 AI 敘述就讓高階會議當天沒有數據。
FALLBACK_PROMPT = (
    "你是多據點營運分析師。用繁體中文 200-320 字摘要以下匯總數字。"
    "pack 為 own_site_only 時只准談這一家據點，不得提及其他據點或全網家數。"
)


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（契約 §6 的統一介面 + 本模組的權限／稽核旗標）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_NAME,
        description="多據點商業智慧匯總：異質來源標準化、Composite Score 分級、兩級權限視野。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True,
                      help="離線模式，讀 mock/*.json、不呼叫任何 API（預設）")
    mode.add_argument("--live", dest="mock", action="store_false",
                      help="串接真實 API；缺憑證會明確報錯退出，不會靜默退回 mock")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="跑完整流程並印出報表，但不發送、也不更新基準線狀態檔")
    parser.add_argument("--notify", choices=list(Notifier.SUPPORTED), default=None,
                        help="發送通道；未指定時取 config 的 runtime.notify_channel")
    parser.add_argument("--config", default=str(MODULE_DIR / "config.yaml"),
                        help="設定檔路徑（預設為本目錄的 config.yaml）")
    parser.add_argument("--role", choices=[role.value for role in Role], default=None,
                        help="檢視角色：hq（Full Pack）或 site_manager（Own-site only）；"
                             "未指定時取 config 的 access.default_role，仍無法辨識則退回最小權限")
    parser.add_argument("--site", dest="site_id", default=None,
                        help="店長視野要看的據點 id；店長角色未指定或指定不存在的據點一律拒絕存取")
    parser.add_argument("--actor", default=None,
                        help="操作者識別（寫進稽核軌跡的 actor 欄位）")
    parser.add_argument("--state-file", dest="state_file", default=None,
                        help="4 週滾動基準線狀態檔路徑，未指定時取 config 的 baseline.state_file")
    parser.add_argument("--audit-file", dest="audit_file", default=None,
                        help="JSONL 稽核軌跡路徑，未指定時取 config 的 audit.file")
    return parser


def resolve_path(raw: str | Path) -> Path:
    """相對路徑一律相對本模組目錄解析，不受呼叫端的工作目錄影響。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (MODULE_DIR / path)


# --------------------------------------------------------------------------
# 設定與前置檢查
# --------------------------------------------------------------------------


def validate_deliver_at(raw: Any) -> str:
    """驗證 deliver_at 是 HH:MM。格式錯就拋錯，不套預設值——
    悄悄改成 07:00 會讓「為什麼報表沒在我設定的時間送出」變成無解懸案。"""
    text = str(raw).strip()
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError(f"report.deliver_at 必須是 HH:MM 格式，收到 {raw!r}") from exc


def to_threshold(raw: Any, field_name: str) -> Decimal:
    """把 config 的門檻值轉成 Decimal；不合法直接拋錯，不套預設值靜默掩蓋。"""
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 不是合法數值：{raw!r}") from exc


def ensure_live_env(config: dict, diagnostics: Diagnostics) -> None:
    """`--live` 時檢查必要環境變數；缺任何一個都走紅色警報退出。"""
    required = (config.get("live") or {}).get("required_env") or []
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="憑證未設定或未匯入目前的 shell",
            fix=f"設定 {', '.join(missing)} 後重跑；或改用 --mock 離線驗證流程",
        )


def build_gate(runtime_cfg: dict, diagnostics: Diagnostics) -> AutonomyGate:
    """依 config 建立自主權閘門；設定有問題一律降級成 DRAFT 並記琥珀燈。"""
    raw_level = str(runtime_cfg.get("autonomy", "draft")).strip().lower()
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        diagnostics.amber(
            f"未知的自主權設定 {raw_level!r}，本次降級為 draft",
            "runtime.autonomy 只接受 read_only / draft / supervised_auto",
        )
        level = AutonomyLevel.DRAFT

    try:
        gate = AutonomyGate(
            level=level,
            approved_senders=list(runtime_cfg.get("approved_senders") or []),
            days_in_draft=int(runtime_cfg.get("days_in_draft", 0) or 0),
        )
    except AutonomyError as exc:
        diagnostics.amber(
            f"自主權設定違規，本次降級為 draft：{exc}",
            "supervised_auto 必須提供非空的 approved_senders",
        )
        gate = AutonomyGate(level=AutonomyLevel.DRAFT)

    for warning in gate.warnings:
        diagnostics.amber(warning, "維持 draft 直到連續穩定運行滿 14 天且客戶簽核")
    return gate


def load_registry(config: dict) -> tuple[list[dict[str, Any]], str, str]:
    """讀 LOCATION_REGISTRY，回傳 (據點清單, 幣別, 期間標籤)。

    這是**未經裁切**的全網名冊，只能用於兩件事：算出可見集合、以及算出
    「不可見識別碼」清單供洩漏掃描。任何取數迴圈都不得直接吃它。
    """
    path = resolve_path(str((config.get("registry") or {}).get("file", "mock/registry.json")))
    payload = rollup.load_json_object(path, "據點名冊")
    locations = payload.get("locations")
    if not isinstance(locations, list) or not locations:
        raise ValueError(f"名冊缺少非空的 locations 陣列：{path}")
    currency = str(payload.get("currency") or (config.get("report") or {}).get("currency", "TWD"))
    return [dict(item) for item in locations], currency, str(payload.get("period_label", "本期"))


# --------------------------------------------------------------------------
# 基準線狀態檔
# --------------------------------------------------------------------------


def load_windows(
    baseline_cfg: dict, state_path: Path, diagnostics: Diagnostics
) -> dict[str, dict[str, list[str]]]:
    """讀基準線視窗：狀態檔優先，不存在時以 mock 種子檔起始（首次執行的正常路徑）。"""
    if state_path.exists():
        return rollup.load_baseline_windows(state_path)

    seed_path = resolve_path(str(baseline_cfg.get("seed_file", "mock/baselines.json")))
    diagnostics.green(f"首次執行：以 {seed_path.name} 作為 4 週滾動基準線種子")
    return rollup.load_baseline_windows(seed_path)


def maybe_update_windows(
    windows: dict[str, dict[str, list[str]]],
    snapshots: Iterable[rollup.SiteSnapshot],
    baseline_cfg: dict,
    scope: AccessScope,
    state_path: Path,
    is_dry_run: bool,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """決定要不要把本期實績推進滾動視窗，並回傳可序列化的說明。

    店長視野預設不寫入：基準線是全網的正式紀錄，讓每個店長各自寫入會產生互相
    覆蓋的競態，而且店長對他店視窗本來就沒有寫入的正當性。
    """
    weeks = int(baseline_cfg.get("window_weeks", 4) or 4)
    if is_dry_run:
        return {"updated": False, "reason": "dry-run", "path": str(state_path)}
    if bool(baseline_cfg.get("update_requires_hq", True)) and scope.role is not Role.HQ:
        return {"updated": False, "reason": "baseline_update_requires_hq", "path": str(state_path)}

    try:
        rollup.save_baseline_windows(state_path, rollup.update_windows(windows, snapshots, weeks), weeks)
    except OSError as exc:
        diagnostics.amber(
            f"基準線狀態檔寫入失敗：{exc}",
            f"確認 {state_path.parent} 可寫；本期報表仍有效，但下期基準線不會前進",
        )
        return {"updated": False, "reason": f"write_failed: {exc}", "path": str(state_path)}
    return {"updated": True, "reason": "ok", "path": str(state_path)}


# --------------------------------------------------------------------------
# 報表排版
# --------------------------------------------------------------------------


def _money(value: Decimal | None, currency: str) -> str:
    """金額格式化；缺值印破折號而不是 0。"""
    return "—" if value is None else f"{currency} {rollup.quantize_money(value):,.2f}"


def _count(value: Decimal | None) -> str:
    """筆數／人次格式化（整數呈現）。"""
    return "—" if value is None else f"{value.quantize(Decimal('1')):,}"


def _index_text(indices: dict[str, Decimal | None], name: str) -> str:
    """指數欄位；無基準線時印「無基準」而不是 100。"""
    value = indices.get(name)
    return "無基準" if value is None else f"指數 {value}"


def _site_block(score: rollup.SiteScore, currency: str) -> list[str]:
    """單一據點的明細區塊。"""
    composite = "—" if score.composite is None else str(score.composite)
    lines = [
        f"  ▸ {score.display_name}（{score.region}）"
        f"｜Composite {composite}｜{rollup.TIER_LABELS.get(score.tier, score.tier)}",
        f"      營收 {_money(score.metrics.revenue, currency)}"
        f"（{_index_text(score.indices, 'revenue')}）"
        f"｜交易 {_count(score.metrics.transactions)} 筆"
        f"（{_index_text(score.indices, 'transactions')}）",
        f"      客單價 {_money(score.metrics.average_ticket, currency)}"
        f"（{_index_text(score.indices, 'average_ticket')}）"
        f"｜人時營收 {_money(score.metrics.revenue_per_labour_hour, currency)}"
        f"（{_index_text(score.indices, 'revenue_per_labour_hour')}）",
    ]
    lines.extend(f"      {item}" for item in score.anomalies)
    return lines


def _network_block(result: rollup.RollupResult) -> list[str]:
    """全網總計區塊（僅 Full Pack）。"""
    totals = result.network_totals
    if totals is None:
        return []
    return [
        "全網總計",
        f"  營收：{_money(totals['revenue'], result.currency)}",
        f"  交易筆數：{_count(totals['transactions'])} 筆"
        f"｜來客數：{_count(totals['footfall'])} 人次"
        f"｜工時：{_count(totals['labour_hours'])} 小時",
        "",
    ]


def _ranking_block(result: rollup.RollupResult) -> list[str]:
    """跨據點排名區塊（僅 Full Pack）。"""
    if result.ranking is None:
        return []
    lines = ["跨據點排名（Composite Score，基準線 = 100）"]
    for item in result.ranking:
        delta = item.get("vs_peer_median_pct")
        suffix = "" if delta is None else f"｜對同儕中位數 {delta}%"
        lines.append(
            f"  {item['rank']}. {item['display_name']}（{item['region']}）"
            f"　{item['composite_score']}　{rollup.TIER_LABELS.get(item['tier'], item['tier'])}{suffix}"
        )
    if result.peer_median_composite is not None:
        lines.append(f"  同儕中位數：{result.peer_median_composite}")
    lines.append("")
    return lines


def _header_lines(
    result: rollup.RollupResult, scope: AccessScope, deliver_at: str, weekday: str, timezone: str
) -> list[str]:
    """報表抬頭：視野、部分資料橫幅、排定發送時間。"""
    if scope.pack == PACK_FULL:
        view = f"視野：Full Pack（總部）｜可見據點 {len(scope.visible_site_ids)} 家"
    elif scope.is_denied:
        view = "視野：拒絕存取（權限設定缺失或角色未知，已退回最小權限）"
    else:
        view = f"視野：Own-site only（單店視野）｜據點：{_own_site_name(result, scope)}"

    lines = [f"🏢 多據點商業智慧匯總｜{result.period_label}", view]
    banner = rollup.partial_banner(result.failures)
    if banner:
        # 橫幅放最上方：讀者在看到任何排名之前，就要知道這份排名不完整。
        lines.append(banner)
        lines.append("（以下數字不含上列據點，請勿據此下修目標或究責）")
    lines.append(f"排定發送：{weekday} {deliver_at}（{timezone}）")
    lines.append("─" * 40)
    return lines


def _own_site_name(result: rollup.RollupResult, scope: AccessScope) -> str:
    """店長視野的據點顯示名稱；取不到就退回 site_id。"""
    for score in result.scores:
        return score.display_name
    for failure in result.failures:
        return failure.display_name
    return str(scope.requested_site_id or "（未指定）")


def render_report_text(
    result: rollup.RollupResult,
    scope: AccessScope,
    deliver_at: str,
    weekday: str,
    timezone: str,
    narrative: str,
) -> str:
    """把 RollupResult 排成可直接發送的純文字報表。內容已受權限裁切。"""
    lines = _header_lines(result, scope, deliver_at, weekday, timezone)
    lines.extend(_network_block(result))
    lines.extend(_ranking_block(result))

    if scope.is_denied:
        lines.append("本次可見據點為空集合，未產出任何營運數字。")
        lines.append(f"原因：{scope.denial_reason}")
        return "\n".join(lines)

    lines.append("據點明細")
    for score in result.scores:
        lines.extend(_site_block(score, result.currency))
    for failure in result.failures:
        lines.append(f"  ⚠️ {failure.display_name}：無回應——{failure.reason}")

    lines.append("")
    lines.append(
        "異常標記：無，各項指標都在基準線容忍區間內" if result.anomaly_count == 0
        else f"異常標記：共 {result.anomaly_count} 條（門檻：偏離 4 週滾動基準線 15%）"
    )
    lines.append("")
    lines.append("AI 敘述摘要")
    lines.append(f"  {narrative}")
    return "\n".join(lines)


def build_subject(result: rollup.RollupResult, scope: AccessScope) -> str:
    """通知主旨：視野與部分資料狀態放最前面，手機通知列被截斷也讀得到重點。"""
    prefix = "⚠️ 部分資料 " if result.is_partial else ""
    view = "Full Pack" if scope.pack == PACK_FULL else "Own-site"
    return f"{prefix}多據點匯總 {result.period_label}｜{view}｜異常 {result.anomaly_count} 條"


# --------------------------------------------------------------------------
# LLM 敘述
# --------------------------------------------------------------------------


def load_prompt(config: dict, diagnostics: Diagnostics) -> str:
    """讀 prompts/rollup.md；讀不到就用後備提示詞並記琥珀燈。"""
    rel = (config.get("prompts") or {}).get("rollup", "prompts/rollup.md")
    path = resolve_path(str(rel))
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.amber(
            f"讀不到提示詞檔 {path}，改用後備提示詞：{exc}",
            "確認 prompts/rollup.md 是否隨部署一起複製過去",
        )
        return FALLBACK_PROMPT


def write_narrative(
    result: rollup.RollupResult,
    scope: AccessScope,
    config: dict,
    is_mock: bool,
    diagnostics: Diagnostics,
) -> str:
    """呼叫 LLM 把數字寫成敘述。mock 模式回傳佔位字串，零成本。

    拒絕存取時**完全不呼叫 LLM**：沒有內容可寫，而 `--live` 下那會是一次
    白花錢又白白把 payload 送出去的外部呼叫。
    """
    if scope.is_denied:
        return "（本次無可見據點，未產生敘述）"
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    return client.complete(
        system=load_prompt(config, diagnostics),
        user=result.to_json(),
        max_tokens=800,
    )


# --------------------------------------------------------------------------
# 全域安全閥：對外呼叫前的 --dry-run 內部通訊測試（apxG_p03）
# --------------------------------------------------------------------------


def preflight_check(
    text: str,
    channel: str,
    scope: AccessScope,
    audit_log: audit.AuditLog,
    is_leak_free: bool,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """任何對外 API 呼叫之前，先在內部把整條路徑空跑一次。

    五項檢查全過才允許外送。console 通道是本機列印、不算對外呼叫，因此就算
    檢查未過仍會印出（但一樣記琥珀燈並留下稽核）；其餘通道未過一律不送。
    """
    checks = {
        "scope_resolved": not scope.is_denied,
        "payload_non_empty": bool(text.strip()),
        "payload_scoped": is_leak_free,
        "audit_trail_healthy": audit_log.is_healthy and bool(audit_log.events),
        "channel_supported": channel in Notifier.SUPPORTED,
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        diagnostics.amber(
            f"對外通訊前置測試未通過：{', '.join(failed)}",
            "修正後重跑；安全閥設計上寧可不送，也不送出未經權限裁切或無稽核佐證的報表",
        )
    return {"passed": not failed, "checks": checks, "failed_checks": failed}


# --------------------------------------------------------------------------
# 發送
# --------------------------------------------------------------------------


def pick_recipients(report_cfg: dict, scope: AccessScope) -> list[str]:
    """依角色挑收件人。拒絕存取時回空清單——沒有內容就沒有收件人。"""
    if scope.is_denied:
        return []
    key = "hq_recipients" if scope.role is Role.HQ else "site_manager_recipients"
    return [str(item) for item in (report_cfg.get(key) or [])]


def _delivery(delivered: bool, channel: str, reason: str, approved: list, held: list) -> dict:
    """統一的發送結果結構。"""
    return {
        "delivered": delivered,
        "channel": channel,
        "reason": reason,
        "approved_recipients": approved,
        "held_recipients": held,
    }


def deliver(
    text: str,
    subject: str,
    channel: str,
    recipients: list[str],
    gate: AutonomyGate,
    preflight: dict[str, Any],
    is_dry_run: bool,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """依 dry-run、安全閥與自主權階梯決定要不要真的送出。"""
    if is_dry_run:
        diagnostics.green("--dry-run：報表已產出但未發送")
        return _delivery(False, channel, "dry-run", [], list(recipients))

    if channel == "console":
        # 印在本機終端不算「對外發送」，因此不受自主權閘門與安全閥的阻擋。
        ok = Notifier("console").send(text, subject=subject)
        return _delivery(ok, channel, "console-output", list(recipients), [])

    if not preflight["passed"]:
        return _delivery(False, channel, "preflight_failed", [], list(recipients))

    approved = [item for item in recipients if gate.can_send(item)]
    held = [item for item in recipients if item not in approved]
    if not approved:
        diagnostics.green("自主權為 draft：報表已產出為草稿，等待人工審核後送出")
        return _delivery(False, channel, "autonomy_draft", [], held)

    ok = Notifier(channel).send(text, subject=subject)
    return _delivery(ok, channel, "sent" if ok else "notifier-failed", approved, held)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _setup_scope(
    args: argparse.Namespace, config: dict, registry: list[dict], diagnostics: Diagnostics
) -> AccessScope:
    """解析本次執行的可見範圍。CLI 旗標優先於 config 預設值。"""
    access_cfg = config.get("access") or {}
    return resolve_scope(
        role_raw=args.role or access_cfg.get("default_role"),
        site_raw=args.site_id or access_cfg.get("default_site_id"),
        actor_raw=args.actor or access_cfg.get("default_actor"),
        registry_site_ids=[str(entry.get("site_id", "")) for entry in registry],
        diagnostics=diagnostics,
    )


def _build_audit_log(args: argparse.Namespace, config: dict) -> audit.AuditLog:
    """建立稽核寫入器（路徑：CLI > config）。"""
    audit_cfg = config.get("audit") or {}
    path = resolve_path(str(args.audit_file or audit_cfg.get("file", "audit/access.jsonl")))
    return audit.AuditLog(
        path=path,
        module_name=MODULE_NAME,
        fail_closed=bool(audit_cfg.get("fail_closed", True)),
    )


def _run_rollup(
    config: dict, scope: AccessScope, registry: list[dict], currency: str, period: str,
    windows: dict, diagnostics: Diagnostics,
) -> tuple[rollup.RollupResult, list[rollup.SiteSnapshot]]:
    """取數 -> 基準線 -> 評分。**只吃裁切過的名冊**，這是隔離的執行點。"""
    visible_entries = scope.filter_registry(registry)
    snapshots, failures = rollup.collect_sites(visible_entries, MODULE_DIR, diagnostics)

    baseline_cfg = config.get("baseline") or {}
    baselines = rollup.resolve_baselines(
        scope.filter_mapping(windows),
        [snapshot.site_id for snapshot in snapshots],
        int(baseline_cfg.get("min_weeks", 4) or 4),
        diagnostics,
    )
    tiers = (config.get("composite") or {}).get("tiers") or {}
    result = rollup.build_rollup(
        snapshots=snapshots,
        failures=failures,
        baselines=baselines,
        weights=rollup.load_weights((config.get("composite") or {}).get("weights")),
        top_tier_min=to_threshold(tiers.get("top_tier_min", 110), "composite.tiers.top_tier_min"),
        core_min=to_threshold(tiers.get("core_min", 90), "composite.tiers.core_min"),
        deviation_pct=to_threshold(
            (config.get("anomaly") or {}).get("deviation_pct", 15), "anomaly.deviation_pct"
        ),
        pack=scope.pack,
        currency=currency,
        period_label=period,
        include_cross_site=scope.can_see_ranking,
    )
    return result, snapshots


def _verify_no_leak(
    payloads: Iterable[Any], scope: AccessScope, registry: list[dict], diagnostics: Diagnostics
) -> bool:
    """縱深防禦：輸出前掃描是否混入不可見據點的識別碼或店名。

    回傳 False 時安全閥會擋下外送。這裡刻意不 re-raise：外洩已經被攔住了，
    讓流程留下完整的稽核與診斷紀錄，比直接崩潰更有助於事後追查。
    """
    tokens = hidden_identifiers(scope, registry)
    if not tokens:
        return True
    try:
        for payload in payloads:
            assert_no_leak(payload, tokens)
    except LeakDetected as exc:
        diagnostics.amber(
            f"輸出洩漏掃描攔截：{exc}",
            "檢查 rollup / render 是否繞過了 AccessScope.filter_registry()；本次一律不外送",
        )
        return False
    return True


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程並回傳結果 dict（供測試斷言）。本函式不呼叫 sys.exit。"""
    diagnostics = Diagnostics(MODULE_NAME)
    config = load_config(resolve_path(args.config))
    if not args.mock:
        ensure_live_env(config, diagnostics)

    report_cfg = config.get("report") or {}
    deliver_at = validate_deliver_at(report_cfg.get("deliver_at", "07:00"))
    registry, currency, period = load_registry(config)

    audit_log = _build_audit_log(args, config)
    scope = _setup_scope(args, config, registry, diagnostics)
    audit_log.record(audit.EVENT_ACCESS_RESOLVED, scope=scope, mode="mock" if args.mock else "live")

    baseline_cfg = config.get("baseline") or {}
    state_path = resolve_path(str(args.state_file or baseline_cfg.get("state_file", "state/baselines.json")))
    windows = load_windows(baseline_cfg, state_path, diagnostics)

    result, snapshots = _run_rollup(config, scope, registry, currency, period, windows, diagnostics)
    audit_log.record(
        audit.EVENT_DATA_ACCESS,
        scope=scope,
        sites_returned=[score.site_id for score in result.scores],
        sites_unavailable=[failure.site_id for failure in result.failures],
        cross_site_analytics=scope.can_see_ranking,
    )

    narrative = write_narrative(result, scope, config, args.mock, diagnostics)
    text = render_report_text(
        result, scope, deliver_at, str(report_cfg.get("deliver_weekday", "Monday")),
        str(report_cfg.get("timezone", "Asia/Taipei")), narrative,
    )
    payload = result.to_dict()
    is_leak_free = _verify_no_leak([payload, text, narrative], scope, registry, diagnostics)

    delivery = _deliver_stage(
        args, config, text, result, scope, audit_log, is_leak_free, diagnostics
    )
    state = maybe_update_windows(
        windows, snapshots, baseline_cfg, scope, state_path, bool(args.dry_run), diagnostics
    )
    audit_log.record(audit.EVENT_STATE_UPDATE, scope=scope, **state)

    return _build_result(
        args, config, payload, scope, audit_log, text, narrative,
        delivery, state, is_leak_free, deliver_at, diagnostics,
    )


def _deliver_stage(
    args: argparse.Namespace,
    config: dict,
    text: str,
    result: rollup.RollupResult,
    scope: AccessScope,
    audit_log: audit.AuditLog,
    is_leak_free: bool,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """安全閥 -> 自主權閘門 -> 發送，並把兩者的結論一起寫進稽核軌跡。"""
    runtime_cfg = config.get("runtime") or {}
    channel = args.notify or str(runtime_cfg.get("notify_channel", "console"))
    preflight = preflight_check(text, channel, scope, audit_log, is_leak_free, diagnostics)
    audit_log.record(audit.EVENT_PREFLIGHT, scope=scope, channel=channel, **preflight)

    delivery = deliver(
        text=text,
        subject=build_subject(result, scope),
        channel=channel,
        recipients=pick_recipients(config.get("report") or {}, scope),
        gate=build_gate(runtime_cfg, diagnostics),
        preflight=preflight,
        is_dry_run=bool(args.dry_run),
        diagnostics=diagnostics,
    )
    audit_log.record(audit.EVENT_DELIVERY, scope=scope, **delivery)
    delivery["preflight"] = preflight
    return delivery


def _build_result(
    args: argparse.Namespace,
    config: dict,
    payload: dict[str, Any],
    scope: AccessScope,
    audit_log: audit.AuditLog,
    text: str,
    narrative: str,
    delivery: dict[str, Any],
    state: dict[str, Any],
    is_leak_free: bool,
    deliver_at: str,
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """組出回傳 dict。鍵名採用契約 §6 技術債註記建議的 6 個標準欄位。"""
    module_cfg = config.get("module") or {}
    result = dict(payload)
    result.update({
        "module_id": str(module_cfg.get("id", "29")),
        "module_name": str(module_cfg.get("name", MODULE_NAME)),
        "mode": "mock" if args.mock else "live",
        "dry_run": bool(args.dry_run),
        "warnings": list(_collect_warnings(scope, payload)),
        "amber_count": diagnostics.amber_count,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "deliver_at": deliver_at,
        "access": scope.to_dict(),
        "audit": {
            "file": str(audit_log.path),
            "run_id": audit_log.run_id,
            "event_count": len(audit_log.events),
            "is_healthy": audit_log.is_healthy,
        },
        "leak_scan_passed": is_leak_free,
        "baseline_state": state,
        "report_text": text,
        "narrative": narrative,
        "delivery": delivery,
    })
    return result


def _collect_warnings(scope: AccessScope, payload: dict[str, Any]) -> list[str]:
    """把要讓呼叫端看到的降級訊息集中起來。"""
    warnings: list[str] = []
    if scope.denial_reason:
        warnings.append(f"權限退回最小視野：{scope.denial_reason}")
    if payload.get("is_partial"):
        names = "、".join(item["display_name"] for item in payload.get("failed_sites", []))
        warnings.append(f"部分資料：{names} 無回應")
    return warnings


def main() -> int:
    """CLI 進入點。回傳 exit code：部分資料仍回 0（報表有產出就算成功）。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (FileNotFoundError, ValueError, audit.AuditError, rollup.RollupError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    if result["delivery"]["channel"] != "console":
        # console 通道已由 Notifier 印過，不重複輸出。
        print(result["report_text"])
    if result["access"]["is_denied"]:
        print(f"\n注意：本次拒絕存取（{result['access']['denial_reason']}）。", file=sys.stderr)
        return 2
    if result["is_partial"]:
        print(
            f"\n注意：本次以部分資料產出（{result['sites_unavailable']} 個據點無回應）。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
