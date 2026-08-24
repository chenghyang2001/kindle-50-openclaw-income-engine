"""demo09 — 每日銷售與進度報表（模組 #9）。

每天 07:00 前把 CRM / Shopify / Stripe 三套系統的數字合成一份團隊看得懂的報表：
算出與目標的差距、標記異常、然後準時送出。

**這個模組的靈魂是「部分失敗」**：單一資料源掛掉時，報表會標上
「⚠️ 部分資料：Stripe 無回應」並**照常發出**，同時走 `Diagnostics.amber`
通知維運端去修。整份失敗等於團隊當天沒有數據可開會——那正是導入這個
代理人要消滅的舊狀態，不該由代理人自己重現一次。

用法：

    python main.py --mock                    # 零憑證、零網路跑完
    python main.py --mock --notify telegram  # 推到 Telegram
    python main.py --mock --dry-run          # 產出但不發送
    python main.py --live                    # 串真實 API（缺憑證會明確報錯退出）
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
# demo/ 進 sys.path 才能匯入 _shared；demo09 自己也要進，
# 這樣 pytest 從別的目錄呼叫時仍找得到 sources / aggregator。
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

import sources  # noqa: E402
from aggregator import (  # noqa: E402
    SalesReport,
    build_report,
    collect,
    load_targets,
    load_thresholds,
    partial_banner,
)

MODULE_NAME = "demo09-sales-report"

#: 第 04 章：附在 system prompt 尾端可減少約 40% 不相關輸出。
CONTEXT_NOTE = (
    "這是每日自動發送給整個團隊的銷售報表，讀者不是工程師。"
    "只陳述輸入 JSON 中實際存在的數字，缺失的資料源一律據實說明，不得推估或補值。"
)

#: 提示詞檔案讀不到時的最低限度後備。刻意保留而不是直接失敗——
#: 表格本體已經有價值，不該因為少一段 AI 敘述就讓團隊當天收不到報表。
FALLBACK_PROMPT = "你是營運分析師。用繁體中文 200-320 字摘要以下每日銷售數字，缺失資料源需據實說明。"


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（契約 §6 的統一介面）。"""
    parser = argparse.ArgumentParser(
        prog="demo09-sales-report",
        description="每日銷售與進度報表：多源聚合、目標差距、異常標記、部分失敗降級。",
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
        help="跑完整流程並印出報表，但不實際發送",
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
    return parser


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


# --------------------------------------------------------------------------
# 報表排版
# --------------------------------------------------------------------------


def _money(value: Decimal, currency: str) -> str:
    return f"{currency} {value:,.2f}"


def _pct(value: Decimal | None) -> str:
    return "—" if value is None else f"{value}%"


def _summary_lines(report: SalesReport) -> list[str]:
    """今日總覽區塊。"""
    cur = report.currency
    gap = report.gap_to_daily_target
    trend = "超前" if gap >= 0 else "落後"
    lines = [
        f"今日營收：{_money(report.total_revenue, cur)}"
        f"（目標 {_money(report.targets.daily_revenue, cur)}，"
        f"達成率 {_pct(report.daily_attainment_pct)}，{trend} {_money(abs(gap), cur)}）",
        f"今日筆數：{report.order_count} 筆",
    ]
    if report.trailing_avg is not None:
        lines.append(
            f"7 日均值：{_money(report.trailing_avg, cur)}"
            f"（今日偏離 {_pct(report.deviation_pct)}）"
        )
    lines.append(
        f"月累計：{_money(report.month_to_date_total, cur)} / "
        f"{_money(report.targets.monthly_revenue, cur)}"
        f"（月進度 {_pct(report.monthly_attainment_pct)}）"
    )
    return lines


def _source_lines(report: SalesReport) -> list[str]:
    """各資料源明細；失敗的資料源同樣列出，不可從報表上消失。"""
    lines = ["各資料源"]
    for snap in report.snapshots:
        extra = "｜".join(f"{k} {v}" for k, v in snap.highlights.items())
        lines.append(
            f"  ✅ {snap.display_name}：{_money(snap.revenue, report.currency)}"
            f"（{snap.order_count} 筆）{'｜' + extra if extra else ''}"
        )
    for fail in report.failures:
        lines.append(f"  ⚠️ {fail.display_name}：無回應——{fail.reason}")
    return lines


def render_report_text(
    report: SalesReport,
    deliver_at: str,
    timezone: str,
    narrative: str,
) -> str:
    """把 SalesReport 排成可直接發送的純文字報表。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [f"📊 每日銷售與進度報表｜{today}"]

    banner = partial_banner(report.failures)
    if banner:
        # 橫幅放最上方：讀者在看到任何數字之前就要知道這份數字不完整。
        lines.append(banner)
        lines.append("（以下數字不含上列資料源，請勿據此下修目標或究責）")

    lines.append(f"排定發送：{deliver_at}（{timezone}）")
    lines.append("─" * 34)
    lines.extend(_summary_lines(report))
    lines.append("")
    lines.extend(_source_lines(report))

    lines.append("")
    if report.anomalies:
        lines.append("異常標記")
        lines.extend(f"  {item}" for item in report.anomalies)
    else:
        lines.append("異常標記：無，各項指標都在容忍區間內")

    lines.append("")
    lines.append("AI 敘述摘要")
    lines.append(f"  {narrative}")
    return "\n".join(lines)


def build_subject(report: SalesReport) -> str:
    """通知主旨：達成率放在最前面，手機通知列被截斷也讀得到重點。"""
    prefix = "⚠️ 部分資料 " if report.is_partial else ""
    return (
        f"{prefix}每日銷售報表 {datetime.now().strftime('%Y-%m-%d')}"
        f"｜達成率 {_pct(report.daily_attainment_pct)}"
    )


# --------------------------------------------------------------------------
# LLM 敘述
# --------------------------------------------------------------------------


def load_prompt(config: dict, diagnostics: Diagnostics) -> str:
    """讀 prompts/report.md；讀不到就用後備提示詞並記琥珀燈。"""
    rel = (config.get("prompts") or {}).get("report", "prompts/report.md")
    path = MODULE_DIR / str(rel)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.amber(
            f"讀不到提示詞檔 {path}，改用後備提示詞：{exc}",
            "確認 prompts/report.md 是否隨部署一起複製過去",
        )
        return FALLBACK_PROMPT


def write_narrative(
    report: SalesReport,
    config: dict,
    is_mock: bool,
    diagnostics: Diagnostics,
) -> str:
    """呼叫 LLM 把數字寫成敘述。mock 模式回傳佔位字串，零成本。"""
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    return client.complete(
        system=load_prompt(config, diagnostics),
        user=report.to_json(),
        max_tokens=800,
    )


# --------------------------------------------------------------------------
# 發送
# --------------------------------------------------------------------------


def _split_recipients(gate: AutonomyGate, recipients: Iterable[str]) -> tuple[list[str], list[str]]:
    """依自主權閘門把收件人分成「可自動送出」與「須人工審核」。"""
    approved, held = [], []
    for recipient in recipients:
        (approved if gate.can_send(recipient) else held).append(recipient)
    return approved, held


def _result(delivered: bool, channel: str, reason: str, approved: list, held: list) -> dict:
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
        diagnostics.green("--dry-run：報表已產出但未發送")
        return _result(False, channel, "dry-run", [], list(recipients))

    if channel == "console":
        # 印在本機終端不算「對外發送」，因此不受自主權閘門管制。
        ok = Notifier("console").send(text, subject=subject)
        return _result(ok, channel, "console-output", list(recipients), [])

    approved, held = _split_recipients(gate, recipients)
    if not approved:
        diagnostics.green(
            f"自主權為 {gate.effective_level(recipients[0] if recipients else '').value}："
            "報表已產出為草稿，等待人工審核後送出"
        )
        return _result(False, channel, "autonomy_draft", [], held)

    ok = Notifier(channel).send(text, subject=subject)
    return _result(ok, channel, "sent" if ok else "notifier-failed", approved, held)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def run(args: argparse.Namespace) -> dict:
    """執行主流程並回傳結果 dict（供測試斷言）。本函式不呼叫 sys.exit。"""
    diagnostics = Diagnostics(MODULE_NAME)
    config = load_config(Path(args.config).expanduser())
    if not args.mock:
        ensure_live_env(config, diagnostics)

    report_cfg = config.get("report") or {}
    deliver_at = validate_deliver_at(report_cfg.get("deliver_at", "07:00"))
    targets_cfg = config.get("targets") or {}

    snapshots, failures = collect(
        config.get("sources") or [], MODULE_DIR, sources.FETCHERS, diagnostics
    )
    report = build_report(
        snapshots=snapshots,
        failures=failures,
        targets=load_targets(
            MODULE_DIR / str(targets_cfg.get("mock_file", "mock/targets.json")), targets_cfg
        ),
        thresholds=load_thresholds(config.get("thresholds")),
        currency=str(report_cfg.get("currency", "USD")),
    )

    narrative = write_narrative(report, config, args.mock, diagnostics)
    text = render_report_text(
        report, deliver_at, str(report_cfg.get("timezone", "Asia/Taipei")), narrative
    )

    runtime_cfg = config.get("runtime") or {}
    channel = args.notify or str(runtime_cfg.get("notify_channel", "console"))
    delivery = deliver(
        text=text,
        subject=build_subject(report),
        channel=channel,
        recipients=[str(r) for r in (report_cfg.get("recipients") or [])],
        gate=build_gate(runtime_cfg, diagnostics),
        is_dry_run=bool(args.dry_run),
        diagnostics=diagnostics,
    )

    result = report.to_dict()
    result.update(
        {
            "module": str((config.get("module") or {}).get("id", "09")),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "deliver_at": deliver_at,
            "is_mock": bool(args.mock),
            "dry_run": bool(args.dry_run),
            "report_text": text,
            "narrative": narrative,
            "delivery": delivery,
            "amber_count": diagnostics.amber_count,
        }
    )
    return result


def main() -> int:
    """CLI 進入點。回傳 exit code：部分資料時回 0（報表有送出就算成功）。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    if result["dry_run"] or result["delivery"]["channel"] != "console":
        # dry-run 時 Notifier 從未被呼叫；非 console 通道也需要主控台自己印。
        print(result["report_text"])
    if result["is_partial"]:
        print(
            f"\n注意：本次以部分資料產出（{len(result['failed_sources'])} 個資料源無回應）。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
