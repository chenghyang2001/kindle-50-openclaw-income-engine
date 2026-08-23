"""demo27 — 法務文件分析與合規監控 主流程（第 07 章 / 附錄G #27）。

流程：每日監控三大來源（法規公告 / 合約庫 / 執照庫 / 內部政策）
      → 到期前 120、60、14 天三階段警告
      → 三級 Escalation Matrix 路由（Critical 必須 Slack + Email 雙通道）
      → 三份 CSV 稽核台帳追加式入帳（append-only，永不覆蓋歷史）

`--mock` 為預設模式：零憑證、零網路，讀 mock/ 目錄的純文字 JSON 跑完整條流程。
本模組**不解析 PDF**，來源一律是合約管理系統匯出的結構化文字。

⚠️ 法律免責：本工具不構成法律意見，輸出僅供合規團隊初步篩選。
   到期日、義務與風險判定必須由合格法律專業人員確認。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

import analyser  # noqa: E402
import escalation  # noqa: E402
import registry  # noqa: E402

MODULE_NAME = "demo27-compliance-monitor"
DISCLAIMER = (
    "⚠️ 本報告由自動化系統產生，**不構成法律意見**，僅供合規團隊初步篩選。"
    "到期日、義務與風險判定必須由合格法律專業人員確認。"
)


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT §6，另加本模組專屬四個）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_NAME,
        description="法務文件分析與合規監控：到期三階段警告 + 三級升級 + CSV 稽核台帳",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", dest="mock", action="store_false", help="串接真實法規來源與通知通道")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="跑完流程但不發送、不寫台帳與狀態檔")
    parser.add_argument("--notify", choices=list(Notifier.SUPPORTED), default="console", help="發送管道，預設 console")
    parser.add_argument("--config", default=None, help="設定檔路徑，預設同目錄 config.yaml")
    parser.add_argument("--registry-dir", default=None, help="三份 CSV 台帳輸出目錄（預設同目錄 registry/）")
    parser.add_argument("--state-file", default=None, help="去重狀態檔路徑（預設同目錄 .compliance-state.json）")
    parser.add_argument("--now", default=None, help="把「現在」釘在指定 ISO 8601 時刻（測試/重跑稽核用）")
    parser.add_argument("--json", dest="json_out", action="store_true", help="把結果 dict 以 JSON 印到 stdout")
    # exit_on_red 不開放 CLI 設定：測試需要拋 RedAlert 而非讓行程退出
    parser.set_defaults(exit_on_red=True)
    return parser


def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，確保任何 cwd 下執行結果一致。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (MODULE_DIR / path)


def build_gate(config: dict[str, Any], diagnostics: Diagnostics) -> AutonomyGate:
    """依設定建立自主權閘門；設定違規直接紅色警報，不靜默降級。"""
    runtime = config.get("runtime") or {}
    try:
        gate = AutonomyGate(
            level=AutonomyLevel(str(runtime.get("autonomy", "draft"))),
            approved_senders=list(runtime.get("approved_senders") or []),
            days_in_draft=int(runtime.get("days_in_draft", 0)),
        )
    except (AutonomyError, ValueError) as exc:
        diagnostics.red(
            "autonomy_misconfig",
            f"runtime.autonomy 設定違規：{exc}",
            "SUPERVISED_AUTO 必須搭配非空的 runtime.approved_senders",
        )
        raise
    for warning in gate.warnings:
        diagnostics.amber(warning, "維持 DRAFT 直到滿 14 天且客戶書面簽核")
    return gate


def build_context(config: dict[str, Any], args: argparse.Namespace, diagnostics: Diagnostics) -> tuple[analyser.AnalysisContext, datetime]:
    """組出判定基準（時區 / 現在時刻 / 三階段門檻 / 信心門檻）。"""
    clock_cfg = config.get("clock") or {}
    tz_name = str(clock_cfg.get("timezone", "UTC"))
    tz, tz_warning = analyser.resolve_timezone(tz_name, int(clock_cfg.get("fallback_offset_hours", 0)))
    if tz_warning:
        diagnostics.amber(tz_warning, "在監控主機安裝 tzdata 套件後重跑")
    frozen = args.now or ((config.get("mock") or {}).get("frozen_now") if args.mock else None)
    now = analyser.resolve_now(frozen, tz)
    monitoring = config.get("monitoring") or {}
    context = analyser.AnalysisContext(
        today=now.date(),
        tz_name=tz_name,
        warning_days=tuple(int(day) for day in monitoring.get("warning_days") or (120, 60, 14)),
        confidence_floor=float(monitoring.get("confidence_floor", 0.75)),
        overdue_grace_days=int(monitoring.get("overdue_grace_days", 0)),
        default_policy_cycle_days=int(monitoring.get("default_policy_review_cycle_days", 365)),
    )
    return context, now


def collect_findings(config: dict[str, Any], context: analyser.AnalysisContext) -> list[analyser.Finding]:
    """依序讀四個來源並產出所有稽核發現（合約 / 執照 / 政策 / 法規）。"""
    sources = (config.get("monitoring") or {}).get("sources") or {}
    plan = (
        ("contracts", "contracts", analyser.analyse_contracts),
        ("licences", "licences", analyser.analyse_licences),
        ("policies", "policies", analyser.analyse_policies),
        ("regulatory", "items", analyser.analyse_regulatory),
    )
    findings: list[analyser.Finding] = []
    for config_key, list_key, analyse in plan:
        raw_path = sources.get(config_key)
        if not raw_path:
            raise analyser.AnalyserError(f"config.monitoring.sources 缺少 {config_key!r}")
        records, marker = analyser.load_source(_resolve_path(str(raw_path)), list_key)
        findings.extend(analyse(records, marker, context))
    return findings


def assess_regulatory_impact(
    findings: list[analyser.Finding], config: dict[str, Any], llm: LLMClient
) -> str:
    """把法規公告送給模型做影響初篩；mock 時讀 fixture，全程不連網。"""
    items = [finding for finding in findings if finding.kind == "regulatory"]
    if not items:
        return "（本次沒有新的法規公告）"
    prompts_cfg = config.get("prompts") or {}
    system = _resolve_path(str(prompts_cfg.get("regulatory_impact", "prompts/regulatory_impact.md"))).read_text(
        encoding="utf-8"
    )
    payload = [
        {
            "item_id": finding.record_id,
            "authority": finding.details.get("authority", ""),
            "title": finding.title,
            "impact_level": finding.declared_level,
            "excerpt": finding.evidence,
        }
        for finding in items
    ]
    fixture = (config.get("mock") or {}).get("impact_fixture")
    return llm.complete(
        system=system,
        user=json.dumps(payload, ensure_ascii=False, indent=2),
        max_tokens=int((config.get("llm") or {}).get("max_tokens", 1500)),
        fixture=_resolve_path(str(fixture)) if (llm.is_mock and fixture) else None,
    )


def load_state(path: Path) -> dict[str, str]:
    """讀去重狀態檔，回傳 {發現鍵: 上次已通報的階段}。壞檔不當機，改為重新建立。"""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    notified = payload.get("notified")
    return {str(key): str(value) for key, value in notified.items()} if isinstance(notified, dict) else {}


def save_state(path: Path, notified: dict[str, str], now_iso: str) -> None:
    """覆寫去重狀態檔（狀態檔不是稽核證據，可覆寫；台帳才是 append-only）。"""
    payload = {"version": 1, "last_run_at": now_iso, "notified": notified}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise OSError(f"狀態檔寫入失敗：{path}｜{exc}") from exc


def enforce_safety_valve(
    probes: dict[str, escalation.ChannelProbe],
    notices: list[escalation.Notice],
    is_mock: bool,
    diagnostics: Diagnostics,
) -> None:
    """全域安全閥：對外送出前檢查 --dry-run 內部通訊測試結果。

    Critical 少一個通道就是漏報，live 模式直接紅色警報；mock 模式只留紀錄。
    """
    for probe in probes.values():
        if not probe.is_available:
            diagnostics.amber(f"通道 {probe.channel} 內部通訊測試未通過：{probe.detail}", "補齊該通道憑證後重跑")
    broken = escalation.dual_channel_warnings(notices)
    for warning in broken:
        diagnostics.amber(warning, "補齊 Slack / Email 憑證，或改由人工立即通知法務長")
    if broken and not is_mock:
        diagnostics.red(
            "critical_single_channel",
            "Critical 升級無法雙通道送達（SPEC apxG_p15：單一通道失效即漏報）",
            "補齊 SLACK_WEBHOOK_URL 與 Gmail 憑證後重跑，期間請人工通知法務長 / 法遵官",
        )


def plan_deliveries(
    notices: list[escalation.Notice], gate: AutonomyGate, is_dry_run: bool
) -> list[dict[str, Any]]:
    """為每個通知對象決定自動送出或留成草稿（自主權階梯的落地點）。"""
    deliveries: list[dict[str, Any]] = []
    for notice in notices:
        for recipient in notice.route.recipients:
            if is_dry_run or notice.is_suppressed:
                action = "dry_run" if is_dry_run else "suppressed"
            else:
                action = "auto_sent" if gate.can_send(recipient) else "draft"
            deliveries.append(
                {
                    "record_id": notice.finding.record_id,
                    "level": notice.level,
                    "recipient": recipient,
                    "channels": list(notice.route.channels),
                    "delivery_status": notice.delivery_status,
                    "action": action,
                    "effective_level": gate.effective_level(recipient).value,
                }
            )
    return deliveries


def _summary_lines(findings: list[analyser.Finding], notices: list[escalation.Notice], context: analyser.AnalysisContext) -> list[str]:
    """報告開頭的統計摘要。"""
    counts = {level: sum(1 for notice in notices if notice.level == level) for level in escalation.LEVELS}
    review_count = sum(1 for finding in findings if finding.needs_human_review)
    at_risk = analyser.at_risk_value([finding for finding in findings if finding.kind == "contract"])
    return [
        f"基準日：{context.today.isoformat()}（{context.tz_name}）｜警告階段：{'/'.join(str(day) for day in sorted(context.warning_days, reverse=True))} 天",
        f"掃描物件 {len(findings)} 筆｜升級 {len(notices)} 則"
        f"（critical {counts['critical']}｜high {counts['high']}｜standard {counts['standard']}）",
        f"需人工複核 {review_count} 筆（條款看不懂 / 缺必要欄位一律不猜）",
        f"警告視窗內合約年度金額合計：{at_risk} USD",
    ]


def render_report(
    findings: list[analyser.Finding],
    notices: list[escalation.Notice],
    context: analyser.AnalysisContext,
    briefing: str,
    ledger_rows: dict[str, int],
    config: dict[str, Any],
) -> str:
    """組出寄給合規團隊的完整報告。"""
    module = config.get("module") or {}
    lines = [f"🛡️ {module.get('name', MODULE_NAME)}｜每日合規監控", "", DISCLAIMER, ""]
    lines.extend(_summary_lines(findings, notices, context))
    lines.extend(["", "【升級清單（依級別排序）】"])
    lines.extend([escalation.render_notice(notice) for notice in notices] or ["（本次沒有任何項目達到升級門檻）"])
    lines.extend(["", "【稽核台帳（append-only）】"])
    lines.extend([f"- {kind}：本次追加 {count} 列" for kind, count in sorted(ledger_rows.items())] or ["- （--dry-run：未寫入）"])
    lines.extend(["", "【法規影響初篩】", briefing.strip()])
    return "\n".join(lines)


def dispatch(notifier: Notifier, message: str, subject: str, deliveries: list[dict[str, Any]], is_dry_run: bool) -> bool:
    """實際送出。DRAFT 狀態一樣送到營運者的管道，但主旨標明待審。"""
    if is_dry_run:
        print(message)
        return False
    is_draft = not any(item["action"] == "auto_sent" for item in deliveries)
    prefix = "[草稿待審]" if is_draft else "[已自動發送]"
    return notifier.send(message, subject=f"{prefix} {subject}")


def _write_ledgers(
    findings: list[analyser.Finding],
    notices: list[escalation.Notice],
    config: dict[str, Any],
    registry_dir: Path,
    now: datetime,
) -> tuple[dict[str, int], dict[str, Path]]:
    """把本次所有發現追加進三份 CSV 台帳。"""
    decisions = {
        notice.finding.key: registry.LedgerDecision(level=notice.level, delivery_status=notice.delivery_status)
        for notice in notices
    }
    files_cfg = (config.get("registry") or {}).get("files") or {}
    paths = registry.ledger_paths(registry_dir, files_cfg)
    run_id = f"{MODULE_NAME}-{now.strftime('%Y%m%dT%H%M%S%z')}"
    rows = registry.write_ledgers(findings, decisions, paths, now.isoformat(), run_id)
    return rows, paths


def _resolve_registry_dir(args: argparse.Namespace, config: dict[str, Any]) -> Path:
    """CLI 覆寫優先；沒給才回頭取 config（避免測試把台帳寫進工作樹）。"""
    if args.registry_dir:
        return Path(args.registry_dir).expanduser().resolve()
    return _resolve_path(str((config.get("registry") or {}).get("dir", "registry")))


def _resolve_state_file(args: argparse.Namespace, config: dict[str, Any]) -> Path:
    """CLI 覆寫優先；沒給才回頭取 config。"""
    if args.state_file:
        return Path(args.state_file).expanduser().resolve()
    return _resolve_path(str((config.get("state") or {}).get("store_file", ".compliance-state.json")))


def _next_state(notices: list[escalation.Notice], previous: dict[str, str], is_enabled: bool) -> dict[str, str]:
    """更新去重狀態：只有真的送出去的升級才記進去。"""
    if not is_enabled:
        return dict(previous)
    updated = dict(previous)
    for notice in notices:
        if not notice.is_suppressed and notice.is_deliverable:
            updated[notice.finding.key] = notice.finding.stage
    return updated


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config_path = _resolve_path(args.config) if args.config else MODULE_DIR / "config.yaml"
    config = load_config(config_path, required_env=[] if args.mock else ["ANTHROPIC_API_KEY"])
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=args.exit_on_red)
    context, now = build_context(config, args, diagnostics)
    findings = collect_findings(config, context)
    gate = build_gate(config, diagnostics)

    escalation_cfg = config.get("escalation") or {}
    matrix = escalation.load_matrix(escalation_cfg)
    rules = escalation.load_rules(escalation_cfg)
    probes = escalation.dry_run_probe(matrix, escalation_cfg.get("channel_env") or {}, args.mock)
    state_file = _resolve_state_file(args, config)
    previous = load_state(state_file)
    suppress = bool((config.get("state") or {}).get("suppress_repeat_alerts", True))
    notices = escalation.build_notices(findings, rules, matrix, probes, previous if suppress else {})
    enforce_safety_valve(probes, notices, args.mock, diagnostics)

    llm_cfg = config.get("llm") or {}
    llm = LLMClient(mock=args.mock, model=str(llm_cfg.get("model", "claude-sonnet-5")), context_note=llm_cfg.get("context_note"))
    briefing = assess_regulatory_impact(findings, config, llm)
    return _finalise(args, config, diagnostics, context, now, findings, notices, gate, probes, briefing, state_file, previous)


def _finalise(
    args: argparse.Namespace,
    config: dict[str, Any],
    diagnostics: Diagnostics,
    context: analyser.AnalysisContext,
    now: datetime,
    findings: list[analyser.Finding],
    notices: list[escalation.Notice],
    gate: AutonomyGate,
    probes: dict[str, escalation.ChannelProbe],
    briefing: str,
    state_file: Path,
    previous: dict[str, str],
) -> dict[str, Any]:
    """寫台帳 / 更新狀態 / 組報告 / 發送，最後組出結果 dict。"""
    registry_dir = _resolve_registry_dir(args, config)
    ledger_rows: dict[str, int] = {}
    paths: dict[str, Path] = {}
    if not args.dry_run:
        ledger_rows, paths = _write_ledgers(findings, notices, config, registry_dir, now)
        save_state(state_file, _next_state(notices, previous, True), now.isoformat())
    deliveries = plan_deliveries(notices, gate, args.dry_run)
    message = render_report(findings, notices, context, briefing, ledger_rows, config)
    module = config.get("module") or {}
    subject = f"合規監控 {context.today.isoformat()}｜critical {sum(1 for n in notices if n.level == 'critical')} 則"
    delivered = dispatch(Notifier(channel=args.notify), message, subject, deliveries, args.dry_run)
    for finding in findings:
        if finding.needs_human_review:
            diagnostics.amber(
                f"{finding.kind} {finding.record_id} 需人工複核：{'；'.join(finding.review_reasons)}",
                "由合格法律專業人員確認後補齊來源資料，系統不代為判定",
            )
    diagnostics.green(f"已掃描 {len(findings)} 筆物件，升級 {len(notices)} 則")
    return {
        "module_id": str(module.get("id", "27")),
        "module_name": str(module.get("name", MODULE_NAME)),
        "mode": "mock" if args.mock else "live",
        "dry_run": bool(args.dry_run),
        "notify_channel": args.notify,
        "as_of": now.isoformat(),
        "findings": [_finding_payload(finding) for finding in findings],
        "notices": [_notice_payload(notice) for notice in notices],
        "deliveries": deliveries,
        "ledger_rows": ledger_rows,
        "registry_dir": str(registry_dir),
        "registry_files": {kind: str(path) for kind, path in paths.items()},
        "state_file": str(state_file),
        "channel_probe": {name: {"available": probe.is_available, "detail": probe.detail} for name, probe in probes.items()},
        "needs_human_review_count": sum(1 for finding in findings if finding.needs_human_review),
        "at_risk_value": str(analyser.at_risk_value([f for f in findings if f.kind == "contract"])),
        "regulatory_briefing": briefing,
        "disclaimer": DISCLAIMER,
        "message": message,
        "delivered": delivered,
        "warnings": list(gate.warnings),
        "amber_count": diagnostics.amber_count,
    }


def _finding_payload(finding: analyser.Finding) -> dict[str, Any]:
    """把 Finding 轉成可 JSON 序列化的 dict（Decimal 一律轉字串保精度）。"""
    return {
        "kind": finding.kind,
        "record_id": finding.record_id,
        "title": finding.title,
        "stage": finding.stage,
        "days": finding.days,
        "needs_human_review": finding.needs_human_review,
        "review_reasons": list(finding.review_reasons),
        "evidence": finding.evidence,
        "source_ref": finding.source_ref,
        "confidence": finding.confidence,
        "owner": finding.owner,
        "declared_level": finding.declared_level,
        "amount": None if finding.amount is None else str(finding.amount),
        "details": dict(finding.details),
    }


def _notice_payload(notice: escalation.Notice) -> dict[str, Any]:
    """把 Notice 轉成可 JSON 序列化的 dict。"""
    return {
        "record_id": notice.finding.record_id,
        "kind": notice.finding.kind,
        "level": notice.level,
        "stage": notice.finding.stage,
        "days": notice.finding.days,
        "channels": list(notice.route.channels),
        "channels_ok": list(notice.channels_ok),
        "channels_failed": list(notice.channels_failed),
        "recipients": list(notice.route.recipients),
        "delivery_status": notice.delivery_status,
        "is_suppressed": notice.is_suppressed,
        "needs_human_review": notice.finding.needs_human_review,
    }


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (analyser.AnalyserError, escalation.EscalationError, registry.RegistryError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    if args.json_out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    critical = sum(1 for notice in result["notices"] if notice["level"] == "critical")
    print(
        f"\n完成：{len(result['findings'])} 筆物件｜{len(result['notices'])} 則升級"
        f"（critical {critical}）｜需人工複核 {result['needs_human_review_count']} 筆"
        f"｜amber {result['amber_count']} 則"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
