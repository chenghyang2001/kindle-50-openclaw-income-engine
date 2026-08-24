"""demo28 — 預測性品管與階層報告鏈（模組 #28，Level 3 企業級）。

輪詢 MES（Plex / SAP）取得各產線各班別的量測與不良數，套用 SPC 控制圖
（UCL / Mean / LCL）與 Nelson Rules，在**瑕疵產生前 3–5 個班次**發出趨勢警告，
再把同一份底層資料整理成四階報告鏈：

    Shift End  →  Daily 06:00  →  Weekly  →  Monthly Board PDF Pack
    現場領班       營運總監/廠長     品質經理     董事會

**本模組的靈魂是「異常不會被平均掉」**：任何一階偵測到的品質警報，
上面每一階都看得見。聚合只影響敘述怎麼寫，永遠不影響警報要不要出現。
四階之間有硬性檢查（`chain.assert_no_alert_dropped`），漏一則就整條鏈失敗。

用法：

    python main.py --mock                      # 零憑證、零網路跑完
    python main.py --mock --tier monthly       # 只產董事會報告包
    python main.py --mock --dry-run            # 跑完流程但不發送
    python main.py --mock --state-file /tmp/s.json --audit-file /tmp/a.jsonl
    python main.py --live                      # 串真實 API（缺憑證會明確報錯退出）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MODULE_DIR = Path(__file__).resolve().parent
# demo/ 進 sys.path 才能匯入 _shared；demo28 自己也要進，
# 這樣 pytest 從別的目錄呼叫時仍找得到 aggregator / chain / audit。
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient, LLMError  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402

import aggregator  # noqa: E402
import chain as chain_mod  # noqa: E402
from audit import AuditTrail, resolve_audit_path  # noqa: E402
from aggregator import (  # noqa: E402
    PlantAnalysis,
    QualityAlert,
    SourceError,
    alert_from_dict,
    count_by_severity,
)
from chain import ChainContext, ChainResult, ReportTier, TierReport  # noqa: E402

MODULE_NAME = "demo28-qc-reporting"

#: 第 04 章：附在 system prompt 尾端可減少約 40% 不相關輸出。
CONTEXT_NOTE = (
    "這是製造業品管的階層報告鏈。每一階的讀者不同（領班／廠長／品質經理／董事會），"
    "但底層是同一份 MES 資料。只陳述輸入 JSON 中實際存在的數字；"
    "缺漏的產線與 null 欄位一律據實說明，不得推估、補值或用平均值掩蓋任何一則警報。"
)

#: 讀不到提示詞檔時的最低限度後備。報告表格本體已有價值，
#: 不該因為少一段 AI 敘述就讓廠長當天收不到晨報。
FALLBACK_PROMPT = (
    "你是製造業品管分析師。用繁體中文摘要以下品質資料，"
    "缺漏資料需據實說明，且不得因平均值正常而略過任何警報。"
)

#: 各通道在 `--live` 送出前必須存在的憑證環境變數（全域安全閥用）。
CHANNEL_REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "console": (),
    "gmail": (),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID_CHENGHYANG2001BOT"),
    "line": ("LINE_CHANNEL_TOKEN",),
    "whatsapp": ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"),
}

#: --tier 的合法值。all 代表四階全出（實務上四階各有自己的 cron）。
TIER_CHOICES = tuple(tier.value for tier in ReportTier) + ("all",)


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（契約 §6 的統一介面 + 本模組的稽核／狀態旗標）。"""
    parser = argparse.ArgumentParser(
        prog="demo28-qc-reporting",
        description="預測性品管與階層報告鏈：SPC + Nelson Rules + 四階報告鏈（班/日/週/月）。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True,
                      help="離線模式，讀 mock/*.json、不呼叫任何 API（預設）")
    mode.add_argument("--live", dest="mock", action="store_false",
                      help="串接真實 MES / LLM API；缺憑證會明確報錯退出，不會靜默退回 mock")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="跑完整流程並印出報告，但不實際發送")
    parser.add_argument("--notify", choices=list(Notifier.SUPPORTED), default=None,
                        help="發送通道；未指定時取 config 的 runtime.notify_channel")
    parser.add_argument("--config", default=str(MODULE_DIR / "config.yaml"),
                        help="設定檔路徑（預設為本目錄的 config.yaml）")
    _add_module_flags(parser)
    return parser


def _add_module_flags(parser: argparse.ArgumentParser) -> None:
    """本模組專屬旗標：報告階層、狀態檔、稽核檔、時間注入。"""
    parser.add_argument("--tier", choices=list(TIER_CHOICES), default=None,
                        help="要產出並發送哪一階報告；未指定時取 config 的 chain.notify_tier")
    parser.add_argument("--state-file", dest="state_file", default=None,
                        help="狀態檔路徑（記錄上次處理到哪一班、哪些警報尚未結案）")
    parser.add_argument("--audit-file", dest="audit_file", default=None,
                        help="JSONL 稽核軌跡路徑；優先序 CLI > OPENCLAW_QC_AUDIT_LOG > config")
    parser.add_argument("--no-audit", dest="audit_enabled", action="store_false", default=True,
                        help="不落地稽核軌跡（僅在記憶體累積）。正式環境不應使用")
    parser.add_argument("--as-of", dest="as_of", default=None,
                        help="以此時間點為基準產生報告（ISO 8601），供測試注入固定時間")
    parser.add_argument("--timezone", dest="timezone_name", default=None,
                        help="覆寫 config 的 chain.timezone（IANA 名稱，如 Asia/Taipei）")


# --------------------------------------------------------------------------
# 時間與設定
# --------------------------------------------------------------------------


def resolve_timezone(name: str, offset_hours: int, diagnostics: Diagnostics) -> tzinfo:
    """解析 IANA 時區。抓不到時退回設定的固定偏移，並記琥珀燈。

    為什麼不直接退回 UTC：本模組的 06:00 晨報是硬性排程，
    悄悄改用 UTC 會讓報告整整早 8 小時送達，而且沒有人會知道原因。
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        diagnostics.amber(
            f"找不到時區 {name}（{exc}），本次改用固定偏移 UTC+{offset_hours}",
            "在此機器安裝 tzdata（pip install tzdata）以取得完整 IANA 時區資料庫",
        )
        return timezone(timedelta(hours=offset_hours), f"UTC+{offset_hours}")


def resolve_as_of(raw: str | None, tz: tzinfo) -> datetime:
    """決定報告基準時間：CLI --as-of > config chain.as_of > 現在時刻。"""
    if not raw:
        return datetime.now(tz)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(f"--as-of / chain.as_of 不是合法的 ISO 8601 時間：{raw!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def ensure_live_env(config: dict, diagnostics: Diagnostics) -> None:
    """`--live` 時檢查必要環境變數；缺任何一個都走紅色警報退出。"""
    required = (config.get("live") or {}).get("required_env") or []
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="MES 或 LLM 憑證未設定，或未匯入目前的 shell",
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
            days_in_draft=int(runtime_cfg.get("days_in_draft", 0)),
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


# --------------------------------------------------------------------------
# 取數
# --------------------------------------------------------------------------


def collect_sources(
    source_configs: Sequence[dict[str, Any]],
    specs: dict[str, aggregator.LineSpec],
    base_dir: Path,
    diagnostics: Diagnostics,
) -> tuple[list[aggregator.ShiftRecord], list[aggregator.LineOutage]]:
    """逐一輪詢 MES 資料源。單一 MES 失聯只降級，不中斷整條報告鏈。

    但「降級」不等於「當作沒事」：失聯的 MES 底下每一條登錄產線都會被
    標成 LineOutage，最終變成 CRITICAL 警報往上浮到董事會。
    """
    records: list[aggregator.ShiftRecord] = []
    outages: list[aggregator.LineOutage] = []

    for entry in source_configs:
        source_id = str(entry.get("id", "")).strip() or "unknown"
        mock_path = base_dir / str(entry.get("mock_file", ""))
        try:
            got_records, got_outages = aggregator.load_source(mock_path, source_id, specs)
        except SourceError as exc:
            diagnostics.amber(
                f"{entry.get('display_name', source_id)} 取數失敗：{exc}",
                f"檢查 {source_id} 的 MES 連線與憑證後重跑；本次該來源產線一律標為資料缺漏",
            )
            outages.extend(_outages_for_source(specs, entry, str(exc)))
            continue
        records.extend(got_records)
        outages.extend(got_outages)

    return records, outages


def _outages_for_source(
    specs: dict[str, aggregator.LineSpec], entry: dict[str, Any], reason: str
) -> list[aggregator.LineOutage]:
    """整個 MES 取數失敗時，把該系統底下所有登錄產線標成資料缺漏。"""
    system = str(entry.get("system") or entry.get("id") or "")
    source_id = str(entry.get("id", "")).strip() or "unknown"
    return [
        aggregator.LineOutage(
            line_id=spec.line_id,
            line_name=spec.line_name,
            source_id=source_id,
            reason=f"{system} MES 取數失敗：{reason}",
            last_seen_at=None,
        )
        for spec in specs.values()
        if spec.mes_system == system
    ]


def load_history(path: Path, diagnostics: Diagnostics) -> dict[str, Any]:
    """讀上期基準值。讀不到時回空 dict——週對週／月對月會顯示「—」而不是推估。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.amber(
            f"讀不到歷史基準檔 {path}：{exc}",
            "週對週與月對月比較本次以「—」呈現；請確認 history_file 是否隨部署複製過去",
        )
        return {}


# --------------------------------------------------------------------------
# 狀態檔
# --------------------------------------------------------------------------


def resolve_state_path(cli_path: str | None, config_path: str | None) -> Path:
    """決定狀態檔位置：CLI > config > 模組預設。一律相對模組目錄，不用 cwd。"""
    for candidate in (cli_path, config_path):
        if candidate:
            path = Path(str(candidate)).expanduser()
            return path if path.is_absolute() else (MODULE_DIR / path)
    return MODULE_DIR / "state" / "qc-state.json"


def load_state(path: Path, diagnostics: Diagnostics) -> dict[str, Any]:
    """讀狀態檔。不存在視為首次執行（回空狀態），不是錯誤。"""
    if not path.exists():
        return {"version": 1, "open_alerts": [], "last_shift_by_line": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.amber(
            f"狀態檔 {path} 讀取失敗：{exc}，本次以空狀態執行",
            "確認檔案未被其他程序寫壞；未結案警報的沿用本次會中斷",
        )
        return {"version": 1, "open_alerts": [], "last_shift_by_line": {}}


def carry_forward_alerts(
    state: dict[str, Any], current_ids: set[str], diagnostics: Diagnostics
) -> list[QualityAlert]:
    """把上次執行留下、本次沒有重新偵測到的未結案警報帶進來。

    為什麼要沿用：MES 只回傳最近幾個班別，昨天那條 3σ 超限在今天的資料裡
    已經不存在了。若不沿用，那則警報會在報告上「自己痊癒」——
    但沒有人結過案，實體上的不良品還在倉庫裡。
    """
    revived: list[QualityAlert] = []
    for raw in state.get("open_alerts") or []:
        try:
            alert = alert_from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            diagnostics.amber(
                f"狀態檔中的警報紀錄格式錯誤，已跳過：{exc}",
                "檢查 state file 是否被手動編輯過；必要時刪除後重建",
            )
            continue
        if alert.alert_id not in current_ids:
            revived.append(alert.as_carry_forward())
    return revived


def save_state(
    path: Path, plant: PlantAnalysis, alerts: Sequence[QualityAlert], stamp: str,
    diagnostics: Diagnostics,
) -> bool:
    """把本次未結案警報與各線最後處理班別寫回狀態檔。"""
    payload = {
        "version": 1,
        "updated_at": stamp,
        "open_alerts": [alert.to_dict() for alert in alerts],
        "last_shift_by_line": {
            line.spec.line_id: line.shifts[-1].record.shift_id
            for line in plant.lines
            if line.shifts
        },
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except OSError as exc:
        diagnostics.amber(
            f"狀態檔 {path} 寫入失敗：{exc}",
            "未結案警報下次執行不會被沿用，請盡快修復寫入權限",
        )
        return False


# --------------------------------------------------------------------------
# LLM 敘述
# --------------------------------------------------------------------------


def load_prompt(config: dict, key: str, diagnostics: Diagnostics) -> str:
    """讀該階的提示詞檔；讀不到就用後備提示詞並記琥珀燈。"""
    rel = (config.get("prompts") or {}).get(key, f"prompts/{key}.md")
    path = MODULE_DIR / str(rel)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.amber(
            f"讀不到提示詞檔 {path}，改用後備提示詞：{exc}",
            f"確認 prompts/{key}.md 是否隨部署一起複製過去",
        )
        return FALLBACK_PROMPT


#: 四階各自對應的提示詞 config 鍵。
PROMPT_KEYS: dict[ReportTier, str] = {
    ReportTier.SHIFT_END: "shift_end",
    ReportTier.DAILY: "daily",
    ReportTier.WEEKLY: "weekly",
    ReportTier.MONTHLY: "monthly",
}


def narrative_payload(report: TierReport, plant: PlantAnalysis) -> str:
    """組出餵給該階提示詞的 JSON。各階拿到的欄位刻意不同——
    班長不需要跨線預測，董事會不需要逐點量測值。"""
    base: dict[str, Any] = {
        "tier": report.tier.value,
        "period_key": report.period_key,
        "period_label": report.period_label,
        "aggregate": report.aggregate,
        "alert_counts": count_by_severity(report.alerts),
        "alerts": [alert.to_dict() for alert in report.alerts],
        "outages": [
            {"line_id": o.line_id, "line_name": o.line_name, "reason": o.reason}
            for o in plant.outages
        ],
    }
    if report.tier is not ReportTier.SHIFT_END:
        base["forecasts"] = [line.forecast.to_dict() for line in plant.lines]
    if report.tier is ReportTier.SHIFT_END:
        base["shift"] = _shift_payload(report, plant)
    return json.dumps(base, ensure_ascii=False, indent=2)


def _shift_payload(report: TierReport, plant: PlantAnalysis) -> dict[str, Any]:
    """班末報告要逐點量測與逐條 Nelson 判定。"""
    for shift in plant.shifts:
        if shift.record.shift_id == report.period_key:
            return shift.to_dict()
    return {}


def write_narratives(
    reports: Sequence[TierReport],
    plant: PlantAnalysis,
    config: dict,
    is_mock: bool,
    diagnostics: Diagnostics,
) -> dict[str, str]:
    """為每一份要發送的報告產生 AI 敘述。mock 模式回傳佔位字串，零成本。"""
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    prompts: dict[ReportTier, str] = {}
    narratives: dict[str, str] = {}
    for report in reports:
        tier = report.tier
        if tier not in prompts:
            prompts[tier] = load_prompt(config, PROMPT_KEYS[tier], diagnostics)
        narratives[report.period_key] = client.complete(
            system=prompts[tier],
            user=narrative_payload(report, plant),
            max_tokens=900,
        )
    return narratives


# --------------------------------------------------------------------------
# 全域安全閥：對外送出前的內部通訊測試
# --------------------------------------------------------------------------


def preflight(channel: str, config: dict, diagnostics: Diagnostics) -> dict[str, Any]:
    """對外 API 呼叫前的 `--dry-run` 內部通訊測試（apxG_p03 全域安全閥）。

    只做**不觸網**的檢查：通道名稱合法、Notifier 可建構、憑證環境變數齊全、
    訊息長度可被安全分段。任何一項失敗都拒絕送出，而不是「先送送看」。
    """
    checks: list[dict[str, Any]] = []
    missing = [name for name in CHANNEL_REQUIRED_ENV.get(channel, ()) if not os.environ.get(name)]
    checks.append({"name": "credentials_present", "passed": not missing, "missing_env": missing})

    constructed = True
    try:
        Notifier(channel, (config.get("runtime") or {}).get("notifier_config"))
    except NotifierError as exc:
        constructed = False
        diagnostics.amber(
            f"通道 {channel} 無法建構：{exc}",
            "檢查 --notify 參數或 runtime.notify_channel 是否為支援的通道",
        )
    checks.append({"name": "channel_constructible", "passed": constructed})

    passed = all(check["passed"] for check in checks)
    if not passed and missing:
        diagnostics.amber(
            f"通道 {channel} 缺少憑證：{', '.join(missing)}，本次不對外送出",
            f"設定 {', '.join(missing)} 後重跑；報告已產出可人工轉發",
        )
    return {"channel": channel, "passed": passed, "checks": checks}


# --------------------------------------------------------------------------
# 發送
# --------------------------------------------------------------------------


def render_delivery_text(report: TierReport, narrative: str) -> str:
    """報告本體 + AI 敘述。敘述放在最後，數字先於文字。"""
    return f"{report.body_markdown}\n\n### AI 敘述摘要\n\n{narrative}\n"


def _delivery_result(
    delivered: bool, channel: str, reason: str, sent: list[str], held: list[str]
) -> dict[str, Any]:
    return {
        "delivered": delivered,
        "channel": channel,
        "reason": reason,
        "sent_reports": sent,
        "held_reports": held,
    }


def deliver(
    reports: Sequence[TierReport],
    narratives: dict[str, str],
    channel: str,
    recipients: Sequence[str],
    gate: AutonomyGate,
    is_dry_run: bool,
    preflight_result: dict[str, Any],
    diagnostics: Diagnostics,
) -> dict[str, Any]:
    """依 dry-run、安全閥與自主權階梯決定要不要真的送出。"""
    keys = [report.period_key for report in reports]
    if is_dry_run:
        diagnostics.green("--dry-run：四階報告已產出但未發送")
        return _delivery_result(False, channel, "dry-run", [], keys)

    if channel == "console":
        # 印在本機終端不算「對外發送」，因此不受自主權閘門與安全閥管制。
        notifier = Notifier("console")
        for report in reports:
            notifier.send(render_delivery_text(report, narratives.get(report.period_key, "")),
                          subject=report.subject)
        return _delivery_result(True, channel, "console-output", keys, [])

    if not preflight_result.get("passed"):
        return _delivery_result(False, channel, "preflight_failed", [], keys)

    approved = [r for r in recipients if gate.can_send(r)]
    if not approved:
        diagnostics.green("自主權為 draft：四階報告已產出為草稿，等待人工審核後送出")
        return _delivery_result(False, channel, "autonomy_draft", [], keys)

    return _send_all(reports, narratives, channel, keys)


def _send_all(
    reports: Sequence[TierReport], narratives: dict[str, str], channel: str, keys: list[str]
) -> dict[str, Any]:
    """實際送出每一份報告，逐份記錄成功與否。"""
    notifier = Notifier(channel)
    sent, failed = [], []
    for report in reports:
        text = render_delivery_text(report, narratives.get(report.period_key, ""))
        (sent if notifier.send(text, subject=report.subject) else failed).append(report.period_key)
    reason = "sent" if not failed else "partially-sent"
    return _delivery_result(bool(sent), channel, reason, sent, failed)


# --------------------------------------------------------------------------
# 報告挑選
# --------------------------------------------------------------------------


def select_reports(result: ChainResult, tier_name: str) -> list[TierReport]:
    """依 --tier 挑出要發送的報告。all 代表四階全發。"""
    if tier_name == "all":
        return list(result.all_reports)
    return list(result.tier(ReportTier(tier_name)))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run(args: argparse.Namespace) -> dict:
    """執行主流程並回傳結果 dict（供測試斷言）。本函式不呼叫 sys.exit。"""
    diagnostics = Diagnostics(MODULE_NAME)
    config = load_config(Path(args.config).expanduser())
    if not args.mock:
        ensure_live_env(config, diagnostics)

    chain_cfg = config.get("chain") or {}
    tz = resolve_timezone(
        str(args.timezone_name or chain_cfg.get("timezone", "Asia/Taipei")),
        int(chain_cfg.get("utc_offset_hours", 8)),
        diagnostics,
    )
    as_of = resolve_as_of(args.as_of or (chain_cfg.get("as_of") if args.mock else None), tz)
    trail = _open_audit(args, config, tz, diagnostics)
    trail.record("run_started", mode="mock" if args.mock else "live",
                 dry_run=bool(args.dry_run), as_of=as_of.isoformat(timespec="seconds"))

    plant, history, plant_name = _ingest(config, diagnostics, as_of, trail)
    state_path = resolve_state_path(args.state_file, (config.get("state") or {}).get("path"))
    state = load_state(state_path, diagnostics)
    carried = carry_forward_alerts(state, {a.alert_id for a in plant.alerts}, diagnostics)

    context = _chain_context(config, as_of, tz, history, carried, plant_name)
    result = chain_mod.build_chain(plant, context)
    _audit_chain(trail, result)

    reports = select_reports(result, str(args.tier or chain_cfg.get("notify_tier", "daily")))
    narratives = write_narratives(reports, plant, config, args.mock, diagnostics)
    delivery = _deliver_reports(args, config, reports, narratives, diagnostics, trail)

    all_alerts = tuple(aggregator.dedupe_alerts(list(plant.alerts) + carried))
    state_saved = save_state(state_path, plant, all_alerts, as_of.isoformat(timespec="seconds"),
                             diagnostics)
    trail.record("run_finished", delivered=delivery["delivered"], reason=delivery["reason"],
                 alert_count=len(all_alerts))
    return _build_result(args, config, as_of, tz, plant, result, reports, narratives, delivery,
                         all_alerts, carried, trail, state_path, state_saved, diagnostics)


def _open_audit(
    args: argparse.Namespace, config: dict, tz: tzinfo, diagnostics: Diagnostics
) -> AuditTrail:
    """建立稽核軌跡。寫入失敗會走琥珀燈，不會靜默。"""
    audit_cfg = config.get("audit") or {}
    path = resolve_audit_path(args.audit_file, audit_cfg.get("log_path"), MODULE_DIR)
    enabled = bool(args.audit_enabled) and bool(audit_cfg.get("enabled", True))
    return AuditTrail(
        path=path,
        module_name=MODULE_NAME,
        tz=tz,
        enabled=enabled,
        on_write_error=lambda message: diagnostics.amber(
            f"稽核軌跡寫入失敗：{message}",
            "本次執行在合規上視為稽核不完整，請修復寫入權限後重跑",
        ),
    )


def _ingest(
    config: dict, diagnostics: Diagnostics, as_of: datetime, trail: AuditTrail
) -> tuple[PlantAnalysis, dict[str, Any], str]:
    """讀產線登錄 + 輪詢 MES + 跑完整 SPC 分析。"""
    registry_cfg = config.get("registry") or {}
    lines_path = MODULE_DIR / str(registry_cfg.get("lines_file", ""))
    specs = aggregator.load_line_specs(lines_path)
    history = load_history(MODULE_DIR / str(registry_cfg.get("history_file", "")), diagnostics)

    records, outages = collect_sources(config.get("sources") or [], specs, MODULE_DIR, diagnostics)
    trail.record("mes_polled", shift_records=len(records),
                 outage_lines=[o.line_id for o in outages])

    plant = aggregator.analyse_plant(
        specs=specs,
        records=records,
        outages=outages,
        thresholds=aggregator.load_thresholds(config.get("spc"), config.get("thresholds")),
        trend_cfg=aggregator.load_trend_config(config.get("trend")),
        period_key=as_of.strftime("%Y-%m"),
        detected_at=as_of.isoformat(timespec="seconds"),
    )
    for alert in plant.alerts:
        trail.record("alert_detected", **alert.to_dict())
    return plant, history, aggregator.load_plant_name(lines_path)


def _chain_context(
    config: dict, as_of: datetime, tz: tzinfo, history: dict[str, Any],
    carried: Sequence[QualityAlert], plant_name: str,
) -> ChainContext:
    """組出建鏈脈絡。"""
    chain_cfg = config.get("chain") or {}
    esc_cfg = config.get("escalation") or {}
    return ChainContext(
        plant_name=plant_name,
        as_of=as_of,
        timezone_name=str(getattr(tz, "key", chain_cfg.get("timezone", "Asia/Taipei"))),
        daily_deliver_at=str(chain_cfg.get("daily_deliver_at", "06:00")),
        weekly_deliver_at=str(chain_cfg.get("weekly_deliver_at", "Mon 07:00")),
        monthly_deliver_at=str(chain_cfg.get("monthly_deliver_at", "1st 09:00")),
        history=history,
        carry_forward=tuple(carried),
        enforce_no_suppression=bool(esc_cfg.get("enforce_no_suppression", True)),
        banner_when_average_looks_fine=bool(esc_cfg.get("banner_when_average_looks_fine", True)),
    )


def _audit_chain(trail: AuditTrail, result: ChainResult) -> None:
    """把四階各自看見的警報數寫進稽核軌跡——這就是「異常沒被吃掉」的證據。"""
    for tier in ReportTier:
        trail.record(
            "tier_built",
            tier=tier.value,
            report_count=len(result.tier(tier)),
            alert_ids=sorted(result.alert_ids(tier)),
        )


def _deliver_reports(
    args: argparse.Namespace, config: dict, reports: Sequence[TierReport],
    narratives: dict[str, str], diagnostics: Diagnostics, trail: AuditTrail,
) -> dict[str, Any]:
    """跑安全閥 → 自主權閘門 → 發送，並把三個環節都寫進稽核軌跡。"""
    runtime_cfg = config.get("runtime") or {}
    channel = args.notify or str(runtime_cfg.get("notify_channel", "console"))
    check = preflight(channel, config, diagnostics)
    trail.record("preflight", **check)

    delivery = deliver(
        reports=reports,
        narratives=narratives,
        channel=channel,
        recipients=[str(r) for r in (runtime_cfg.get("recipients") or [])],
        gate=build_gate(runtime_cfg, diagnostics),
        is_dry_run=bool(args.dry_run),
        preflight_result=check,
        diagnostics=diagnostics,
    )
    delivery["preflight"] = check
    trail.record("delivery", **{k: v for k, v in delivery.items() if k != "preflight"})
    return delivery


def _build_result(
    args: argparse.Namespace, config: dict, as_of: datetime, tz: tzinfo,
    plant: PlantAnalysis, result: ChainResult, reports: Sequence[TierReport],
    narratives: dict[str, str], delivery: dict[str, Any],
    all_alerts: Sequence[QualityAlert], carried: Sequence[QualityAlert],
    trail: AuditTrail, state_path: Path, state_saved: bool, diagnostics: Diagnostics,
) -> dict:
    """組出 run() 的回傳結構（鍵名依契約 §6 技術債附註的建議欄位）。"""
    module_cfg = config.get("module") or {}
    return {
        "module_id": str(module_cfg.get("id", "28")),
        "module_name": str(module_cfg.get("name", MODULE_NAME)),
        "mode": "mock" if args.mock else "live",
        "dry_run": bool(args.dry_run),
        "warnings": list(_gate_warnings(config)),
        "amber_count": diagnostics.amber_count,
        "as_of": as_of.isoformat(timespec="seconds"),
        "timezone": str(getattr(tz, "key", str(tz))),
        "plant": plant.to_dict(),
        "chain": result.to_dict(),
        "tier_alert_ids": {tier.value: sorted(result.alert_ids(tier)) for tier in ReportTier},
        "alerts": [alert.to_dict() for alert in all_alerts],
        "alert_counts": count_by_severity(all_alerts),
        "carry_forward": [alert.alert_id for alert in carried],
        "delivered_reports": [report.period_key for report in reports],
        # dry-run 時 deliver() 從未呼叫 Notifier，main() 得靠這份完整內容自己印出來，
        # 不能只靠 delivered_reports 的 period_key 清單（那只是標題，不是報告本體）。
        "rendered_reports": {
            report.period_key: render_delivery_text(report, narratives.get(report.period_key, ""))
            for report in reports
        },
        "narratives": narratives,
        "delivery": delivery,
        "audit": trail.summary(),
        "state": {"path": str(state_path), "saved": state_saved},
    }


def _gate_warnings(config: dict) -> list[str]:
    """把自主權閘門的警告帶進回傳結構（契約建議的 warnings 欄位）。"""
    runtime_cfg = config.get("runtime") or {}
    try:
        gate = AutonomyGate(
            level=AutonomyLevel(str(runtime_cfg.get("autonomy", "draft")).strip().lower()),
            approved_senders=list(runtime_cfg.get("approved_senders") or []),
            days_in_draft=int(runtime_cfg.get("days_in_draft", 0)),
        )
    except (AutonomyError, ValueError):
        return ["自主權設定違規或無法辨識，本次已降級為 draft"]
    return list(gate.warnings)


def main() -> int:
    """CLI 進入點。回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (LLMError, FileNotFoundError, ValueError, SourceError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    except chain_mod.AlertSuppressionError as exc:
        # 這是唯一「寧可整條鏈失敗也不送出」的情況：報告會說謊。
        print(f"報告鏈完整性檢查失敗：{exc}", file=sys.stderr)
        return 2

    if result["dry_run"]:
        # dry-run 時 deliver() 從未呼叫 Notifier，四階報告內容要在這裡自己印出來，
        # 否則 --dry-run 只會看到 stderr 的統計摘要，完整報告內容整個消失。
        for key in result["delivered_reports"]:
            print(f"--- {key} ---")
            print(result["rendered_reports"].get(key, ""))
    elif result["delivery"]["channel"] != "console":
        # console 通道已經由 Notifier 印過，這裡只印標題避免重複；
        # 非 console 通道的完整內容已經送到外部管道，不在 stdout 重複印一次。
        for key in result["delivered_reports"]:
            print(f"--- {key} ---")
    _print_footer(result)
    return 0


def _print_footer(result: dict) -> None:
    """在 stderr 印出本次執行的品質與稽核摘要。"""
    counts = result["alert_counts"]
    print(
        f"\n[{result['module_id']}] 未結案警報："
        f"重大 {counts['critical']}／主要 {counts['major']}／次要 {counts['minor']}"
        f"｜四階均可見：{'是' if _all_tiers_see_all(result) else '否'}"
        f"｜稽核軌跡 {result['audit']['entry_count']} 筆"
        f"（寫入失敗 {result['audit']['write_failures']} 次）",
        file=sys.stderr,
    )


def _all_tiers_see_all(result: dict) -> bool:
    """檢查班末偵測到的警報是否在日／週／月三階都看得見。"""
    tiers = result["tier_alert_ids"]
    shift_ids = set(tiers["shift_end"])
    return all(shift_ids <= set(tiers[name]) for name in ("daily", "weekly", "monthly"))


if __name__ == "__main__":
    sys.exit(main())
