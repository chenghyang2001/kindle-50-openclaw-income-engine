"""demo17 — 每週績效儀表板（模組 #17）。

每週一 07:00 把六套系統（GA4 / Google Ads / Meta / CRM / Xero / QuickBooks）
的數字合成一份管理層讀得懂的週報：週對週比較、四週移動平均、RAG 燈號、
異常標記，以及三個基於數據的本週行動建議。

**這個模組的靈魂是「部分失敗」**：六個資料源裡掛掉任何一個，報表都會標上
「⚠️ 部分資料：Meta Ads 無回應」並**照常發出**，該資料源負責的 KPI 標成
「無資料」而不是補 0，同時走 `Diagnostics.amber` 通知維運端去修。整份失敗
等於客戶當週沒有數據可開會——那正是導入這個代理人要消滅的舊狀態。

**第二個核心是「首週」**：剛上線時沒有上週資料，所有 WoW 一律 `None`、
燈號一律灰、行動建議留空。絕不拿 0 當上週值去算出假的 +100%。

用法：

    python main.py --mock                          # 零憑證、零網路跑完
    python main.py --mock --notify telegram        # 推到 Telegram
    python main.py --mock --dry-run                # 產出但不發送、不寫狀態
    python main.py --mock --state-file ~/w17.json  # 累積歷史（跑完寫回該檔）
    python main.py --live                          # 串真實 API（缺憑證會明確報錯退出）
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

MODULE_DIR = Path(__file__).resolve().parent
# demo/ 進 sys.path 才能匯入 _shared；demo17 自己也要進，
# 這樣 pytest 從別的目錄呼叫時仍找得到 aggregator。
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient, LLMError  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

from aggregator import (  # noqa: E402
    MetricResult,
    RagStatus,
    WeeklyDashboard,
    WeekRecord,
    append_week,
    build_dashboard,
    collect,
    load_history,
    load_metrics_map,
    load_thresholds,
    partial_banner,
    resolve_week_id,
    save_history,
)

MODULE_NAME = "demo17-weekly-dashboard"

#: 第 04 章：附在 system prompt 尾端可減少約 40% 不相關輸出。
CONTEXT_NOTE = (
    "這是每週一早上寄給客戶管理層的績效週報，讀者是決策者而不是工程師。"
    "只陳述輸入 JSON 中實際存在的數字；wow_pct 為 null 代表沒有可比較的基準，"
    "必須據實說明，不得推估、補值或用 0 代替。"
)

#: 提示詞讀不到時的最低限度後備。刻意保留而不是直接失敗——
#: 數字表格本體已經有價值，不該因為少一段 AI 敘述就讓客戶當週收不到週報。
FALLBACK_PROMPTS = {
    "dashboard": "你是營運分析師。用繁體中文 200-320 字摘要以下週績效數字，缺失資料源需據實說明。",
    "focus_actions": "你是營運顧問。依以下數據列出 3 個本週具體行動建議，每則 40 字內。",
}

#: 星期代碼 → 中文顯示。用來把 config 的 deliver_day 轉成報表上的字樣。
WEEKDAY_LABELS = {
    "monday": "週一",
    "tuesday": "週二",
    "wednesday": "週三",
    "thursday": "週四",
    "friday": "週五",
    "saturday": "週六",
    "sunday": "週日",
}

#: RAG 燈號 → 報表符號與說明。灰燈不是綠燈，字樣必須讓人一眼看出差別。
RAG_MARKS = {
    RagStatus.GREEN: ("🟢", "符合預期"),
    RagStatus.AMBER: ("🟡", "需要關注"),
    RagStatus.RED: ("🔴", "需要立即處理"),
    RagStatus.UNKNOWN: ("⚪", "無比較基準"),
}


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（契約 §6 的統一介面，外加 --state-file）。"""
    parser = argparse.ArgumentParser(
        prog="demo17-weekly-dashboard",
        description="每週績效儀表板：六源聚合、週對週比較、RAG 燈號、異常標記、部分失敗降級。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mock",
        dest="mock",
        action="store_true",
        default=True,
        help="離線模式，讀 mock/*.json、不呼叫任何 API（預設）",
    )
    mode.add_argument(
        "--live",
        dest="mock",
        action="store_false",
        help="串接真實 API；缺憑證會明確報錯退出，不會靜默退回 mock",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="跑完整流程並印出週報，但不發送、也不寫回歷史狀態檔",
    )
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default=None,
        help="發送通道；未指定時取 config 的 runtime.notify_channel（預設 console）",
    )
    parser.add_argument(
        "--config",
        default=str(MODULE_DIR / "config.yaml"),
        help="設定檔路徑（預設為本目錄的 config.yaml）",
    )
    parser.add_argument(
        "--state-file",
        dest="state_file",
        default=None,
        help=(
            "歷史狀態檔路徑：優先從此檔讀上週資料，跑完（非 dry-run）寫回此檔。"
            "未指定時只讀 config 的 history.mock_file，不寫任何檔案"
        ),
    )
    return parser


# --------------------------------------------------------------------------
# 設定與前置檢查
# --------------------------------------------------------------------------


def validate_deliver_at(raw: Any) -> str:
    """驗證 deliver_at 是 HH:MM。格式錯就拋錯，不套預設值——
    悄悄改成 07:00 會讓「為什麼週報沒在我設定的時間送出」變成無解懸案。"""
    text = str(raw).strip()
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError as exc:
        raise ValueError(f"report.deliver_at 必須是 HH:MM 格式，收到 {raw!r}") from exc


def validate_deliver_day(raw: Any) -> str:
    """驗證 deliver_day 是英文星期名。附錄 F 預設週一，但客戶的週會時間各不相同。"""
    text = str(raw).strip().lower()
    if text not in WEEKDAY_LABELS:
        raise ValueError(
            f"report.deliver_day 必須是 {', '.join(WEEKDAY_LABELS)} 之一，收到 {raw!r}"
        )
    return text


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


def resolve_history_path(args: argparse.Namespace, config: dict) -> Path:
    """決定這次要讀哪一份歷史。

    給了 `--state-file` 且該檔存在就讀它；否則讀 config 的唯讀種子檔。
    這個兩段式是刻意的：`python main.py --mock` 不該把版控中的 mock/history.json
    寫髒，但真正部署時又必須有地方累積歷史。
    """
    if args.state_file:
        state_path = Path(args.state_file).expanduser()
        if state_path.exists():
            return state_path
    history_cfg = config.get("history") or {}
    return MODULE_DIR / str(history_cfg.get("mock_file", "mock/history.json"))


# --------------------------------------------------------------------------
# 數值排版
# --------------------------------------------------------------------------


def format_value(value: Decimal | None, unit: str, currency: str) -> str:
    """依單位把數值排成人看得懂的字串。"""
    if value is None:
        return "無資料"
    if unit == "money":
        return f"{currency} {value:,.2f}"
    if unit == "percent":
        return f"{value:,.2f}%"
    if unit == "ratio":
        return f"{value:,.2f}"
    if unit == "seconds":
        return f"{value:,.0f} 秒"
    return f"{value:,.0f}"


def format_change(pct: Decimal | None) -> str:
    """把變化率排成 +5.0% / -20.0%；無基準時明寫「無上週資料」。"""
    if pct is None:
        return "無上週資料"
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct}%"


def _metric_line(item: MetricResult, currency: str) -> str:
    """單一 KPI 的一行。取不到值時仍然列出，並寫明原因。"""
    mark = RAG_MARKS[item.rag][0]
    if not item.is_available:
        return f"  {mark} {item.spec.display_name}：無資料 —— {item.unavailable_reason}"

    parts = [
        f"  {mark} {item.spec.display_name}：{format_value(item.value, item.spec.unit, currency)}",
        f"WoW {format_change(item.wow_pct)}",
    ]
    if item.moving_avg is not None:
        parts.append(
            f"四週均 {format_value(item.moving_avg, item.spec.unit, currency)}"
            f"（{format_change(item.vs_avg_pct)}）"
        )
    if item.is_anomaly:
        parts.append("⚠️ 異常")
    return "｜".join(parts)


def _block_lines(dashboard: WeeklyDashboard) -> list[str]:
    """所有區塊的明細。"""
    lines: list[str] = []
    for block in dashboard.blocks:
        mark, label = RAG_MARKS[block.rag]
        suffix = "（部分資料）" if block.is_partial else ""
        lines.append(f"【{block.spec.display_name}】{mark} {label}{suffix}")
        lines.extend(_metric_line(item, dashboard.currency) for item in block.metrics)
        lines.append("")
    return lines


def _focus_lines(dashboard: WeeklyDashboard, actions_text: str) -> list[str]:
    """本週行動建議：先列數據依據，再放 AI 寫的敘述。"""
    if not dashboard.focus_actions:
        return [
            "本週行動建議",
            "  （首週或無可比較資料，本週不提出以趨勢為依據的建議）",
        ]

    lines = [f"本週行動建議（依對業務衝擊排序，共 {len(dashboard.focus_actions)} 項）"]
    for index, item in enumerate(dashboard.focus_actions, start=1):
        lines.append(
            f"  {index}. {item.spec.display_name}"
            f"（{format_value(item.value, item.spec.unit, dashboard.currency)}，"
            f"WoW {format_change(item.wow_pct)}）"
        )
    lines.extend(["", "AI 建議說明", f"  {actions_text}"])
    return lines


def _header_lines(dashboard: WeeklyDashboard, report_cfg: dict) -> list[str]:
    """報表抬頭：週次、部分資料橫幅、整體燈號、發送時點、比較基準。"""
    mark, label = RAG_MARKS[dashboard.overall_rag]
    lines = [f"📊 每週績效儀表板｜{dashboard.week_id}"]

    banner = partial_banner(dashboard.failures)
    if banner:
        # 橫幅放最上方：讀者在看到任何數字之前就要知道這份數字不完整。
        lines.append(banner)
        lines.append("（以下數字不含上列資料源，請勿據此調整預算或究責）")

    lines.append(f"整體狀態：{mark} {label}")
    day_label = WEEKDAY_LABELS[validate_deliver_day(report_cfg.get("deliver_day", "monday"))]
    lines.append(
        f"排定發送：{day_label} {validate_deliver_at(report_cfg.get('deliver_at', '07:00'))}"
        f"（{report_cfg.get('timezone', 'Asia/Taipei')}）"
    )
    if dashboard.has_comparison:
        lines.append(
            f"比較基準：{dashboard.comparison_week_id}"
            f"（歷史 {dashboard.history_weeks} 週，移動平均取 {dashboard.moving_average_weeks} 週）"
        )
    else:
        lines.append("比較基準：無——本週為首次執行，所有週對週比較留空，不以 0 代替")
    return lines


def render_report_text(
    dashboard: WeeklyDashboard,
    report_cfg: dict,
    narrative: str,
    actions_text: str,
) -> str:
    """把 WeeklyDashboard 排成可直接發送的純文字週報。"""
    lines = _header_lines(dashboard, report_cfg)
    lines.append("─" * 34)
    lines.extend(_block_lines(dashboard))

    if dashboard.anomalies:
        lines.append(f"異常標記（週對週變動超過門檻，共 {len(dashboard.anomalies)} 項）")
        lines.extend(f"  {item}" for item in dashboard.anomalies)
    else:
        lines.append("異常標記：無，各項指標都在容忍區間內")
    lines.append("")

    lines.extend(_focus_lines(dashboard, actions_text))
    lines.extend(["", "AI 敘述摘要", f"  {narrative}"])
    return "\n".join(lines)


def build_subject(dashboard: WeeklyDashboard) -> str:
    """通知主旨：燈號與週次放最前面，手機通知列被截斷也讀得到重點。"""
    prefix = "⚠️ 部分資料 " if dashboard.is_partial else ""
    mark, label = RAG_MARKS[dashboard.overall_rag]
    return f"{prefix}{mark} 每週績效儀表板 {dashboard.week_id}｜{label}"


# --------------------------------------------------------------------------
# LLM 敘述
# --------------------------------------------------------------------------


def load_prompt(config: dict, key: str, diagnostics: Diagnostics) -> str:
    """讀 prompts/*.md；讀不到就用後備提示詞並記琥珀燈。"""
    default_rel = f"prompts/{key.replace('_', '-')}.md"
    rel = (config.get("prompts") or {}).get(key, default_rel)
    path = MODULE_DIR / str(rel)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.amber(
            f"讀不到提示詞檔 {path}，改用後備提示詞：{exc}",
            f"確認 {default_rel} 是否隨部署一起複製過去",
        )
        return FALLBACK_PROMPTS[key]


def write_narrative(
    dashboard: WeeklyDashboard,
    config: dict,
    is_mock: bool,
    diagnostics: Diagnostics,
) -> str:
    """呼叫 LLM 把數字寫成管理層敘述。mock 模式回傳佔位字串，零成本。"""
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    return client.complete(
        system=load_prompt(config, "dashboard", diagnostics),
        user=dashboard.to_json(),
        max_tokens=900,
    )


def write_focus_actions(
    dashboard: WeeklyDashboard,
    config: dict,
    is_mock: bool,
    diagnostics: Diagnostics,
) -> str:
    """把已排序的 Focus Actions 交給 LLM 寫成具體建議。

    候選 KPI 由 `select_focus_actions` 以數據決定（可重現），LLM 只負責措辭——
    讓模型自己挑「該關注什麼」會讓同一份數字每週得到不同的優先順序。
    """
    if not dashboard.focus_actions:
        return "本週為首次執行或無可比較資料，暫不提出以趨勢為依據的建議。"

    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    return client.complete(
        system=load_prompt(config, "focus_actions", diagnostics),
        user=dashboard.to_json(),
        max_tokens=500,
    )


# --------------------------------------------------------------------------
# 發送
# --------------------------------------------------------------------------


def _split_recipients(gate: AutonomyGate, recipients: Iterable[str]) -> tuple[list[str], list[str]]:
    """依自主權閘門把收件人分成「可自動送出」與「須人工審核」。"""
    approved: list[str] = []
    held: list[str] = []
    for recipient in recipients:
        (approved if gate.can_send(recipient) else held).append(recipient)
    return approved, held


def _delivery_result(
    delivered: bool, channel: str, reason: str, approved: list[str], held: list[str]
) -> dict:
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
    is_dry_run: bool,
    diagnostics: Diagnostics,
) -> dict:
    """依 dry-run 與自主權階梯決定要不要真的送出。"""
    if is_dry_run:
        diagnostics.green("--dry-run：週報已產出但未發送")
        return _delivery_result(False, channel, "dry-run", [], list(recipients))

    if channel == "console":
        # 印在本機終端不算「對外發送」，因此不受自主權閘門管制。
        ok = Notifier("console").send(text, subject=subject)
        return _delivery_result(ok, channel, "console-output", list(recipients), [])

    approved, held = _split_recipients(gate, recipients)
    if not approved:
        level = gate.effective_level(recipients[0] if recipients else "")
        diagnostics.green(f"自主權為 {level.value}：週報已產出為草稿，等待人工審核後送出")
        return _delivery_result(False, channel, "autonomy_draft", [], held)

    ok = Notifier(channel).send(text, subject=subject)
    return _delivery_result(ok, channel, "sent" if ok else "notifier-failed", approved, held)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def assemble_dashboard(
    config: dict,
    args: argparse.Namespace,
    diagnostics: Diagnostics,
) -> tuple[WeeklyDashboard, list[WeekRecord]]:
    """取數 → 讀歷史 → 算出 WeeklyDashboard。回傳 (儀表板, 讀到的歷史)。"""
    thresholds = load_thresholds(config.get("thresholds"))
    blocks = load_metrics_map(config.get("metrics_map"), thresholds, diagnostics)
    snapshots, failures = collect(config.get("sources") or [], MODULE_DIR, diagnostics)

    history_cfg = config.get("history") or {}
    history = load_history(resolve_history_path(args, config))
    report_cfg = config.get("report") or {}

    dashboard = build_dashboard(
        blocks=blocks,
        snapshots=snapshots,
        failures=failures,
        history=history,
        thresholds=thresholds,
        # ISO 週次當後備：所有資料源都掛掉時仍要有個可辨識的週次寫進報表。
        week_id=resolve_week_id(snapshots, datetime.now().strftime("%G-W%V"), diagnostics),
        currency=str(report_cfg.get("currency", "USD")),
        focus_action_count=int(report_cfg.get("focus_action_count", 3)),
        moving_average_weeks=int(history_cfg.get("moving_average_weeks", 4)),
    )
    return dashboard, history


def persist_history(
    args: argparse.Namespace,
    config: dict,
    history: list[WeekRecord],
    dashboard: WeeklyDashboard,
    diagnostics: Diagnostics,
) -> dict:
    """把本週結果寫回狀態檔。只有給了 --state-file 且非 dry-run 時才動檔案。"""
    if not args.state_file:
        return {"path": None, "written": False, "reason": "未指定 --state-file，本次不寫歷史"}
    if args.dry_run:
        return {"path": str(args.state_file), "written": False, "reason": "dry-run"}

    path = Path(args.state_file).expanduser()
    keep_weeks = int((config.get("history") or {}).get("keep_weeks", 12))
    try:
        save_history(path, append_week(history, dashboard), keep_weeks)
    except OSError as exc:
        # 寫不進狀態檔不該讓已經算好的週報作廢：報表照發，下週少一個比較基準。
        diagnostics.amber(
            f"無法寫入歷史狀態檔 {path}：{exc}",
            "確認路徑存在且有寫入權限；本週報表仍已產出，但下次無法與本週比較",
        )
        return {"path": str(path), "written": False, "reason": f"寫入失敗：{exc}"}
    return {"path": str(path), "written": True, "reason": "已寫回"}


def run(args: argparse.Namespace) -> dict:
    """執行主流程並回傳結果 dict（供測試斷言）。本函式不呼叫 sys.exit。"""
    diagnostics = Diagnostics(MODULE_NAME)
    config = load_config(Path(args.config).expanduser())
    if not args.mock:
        ensure_live_env(config, diagnostics)

    dashboard, history = assemble_dashboard(config, args, diagnostics)
    narrative = write_narrative(dashboard, config, args.mock, diagnostics)
    actions_text = write_focus_actions(dashboard, config, args.mock, diagnostics)

    report_cfg = config.get("report") or {}
    text = render_report_text(dashboard, report_cfg, narrative, actions_text)

    runtime_cfg = config.get("runtime") or {}
    gate = build_gate(runtime_cfg, diagnostics)
    delivery = deliver(
        text=text,
        subject=build_subject(dashboard),
        channel=args.notify or str(runtime_cfg.get("notify_channel", "console")),
        recipients=[str(item) for item in (report_cfg.get("recipients") or [])],
        gate=gate,
        is_dry_run=bool(args.dry_run),
        diagnostics=diagnostics,
    )
    state = persist_history(args, config, history, dashboard, diagnostics)
    return _assemble_result(config, args, dashboard, gate, diagnostics, text, narrative,
                            actions_text, delivery, state)


def _assemble_result(
    config: dict,
    args: argparse.Namespace,
    dashboard: WeeklyDashboard,
    gate: AutonomyGate,
    diagnostics: Diagnostics,
    text: str,
    narrative: str,
    actions_text: str,
    delivery: dict,
    state: dict,
) -> dict:
    """組出 run() 的回傳值。

    鍵名同時提供契約 §6「未來標準化」建議的 6 個鍵（module_id / module_name /
    mode / dry_run / warnings / amber_count）與本模組專屬欄位，
    好讓 bundle-quickstart 的 normalize_result() 不必再猜。
    """
    module_cfg = config.get("module") or {}
    report_cfg = config.get("report") or {}
    result = dashboard.to_dict()
    result.update(
        {
            "module_id": str(module_cfg.get("id", "17")),
            "module_name": str(module_cfg.get("name", "每週績效儀表板")),
            "module": str(module_cfg.get("id", "17")),
            "mode": "mock" if args.mock else "live",
            "is_mock": bool(args.mock),
            "dry_run": bool(args.dry_run),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "deliver_day": validate_deliver_day(report_cfg.get("deliver_day", "monday")),
            "deliver_at": validate_deliver_at(report_cfg.get("deliver_at", "07:00")),
            "report_text": text,
            "narrative": narrative,
            "focus_actions_text": actions_text,
            "delivery": delivery,
            "state": state,
            "warnings": list(gate.warnings),
            "amber_count": diagnostics.amber_count,
        }
    )
    return result


def main() -> int:
    """CLI 進入點。回傳 exit code：部分資料時仍回 0（週報有送出就算成功）。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (LLMError, FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    if result["delivery"]["channel"] != "console":
        # console 通道已經由 Notifier 印過，不重複輸出。
        print(result["report_text"])
    if result["is_partial"]:
        print(
            f"\n注意：本次以部分資料產出（{len(result['failed_sources'])} 個資料源無回應）。",
            file=sys.stderr,
        )
    if not result["has_comparison"]:
        print("\n注意：本次為首週執行，沒有上週資料可比較。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
