"""demo03 — 會議紀錄與行動提取 主流程（第 03/04 章）。

流程：會議結束 → Webhook 收逐字稿 → 提取摘要 / 決策 / 指定負責人的行動項目
      → 5 分鐘內發送給所有與會者 → 推播至專案管理工具。

`--mock` 為預設模式：零憑證、零網路，讀 mock/ 目錄的逐字稿跑完整條流程。
"""

from __future__ import annotations

import argparse
import json
import sys
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

from extractor import ActionExtractor, ExtractionResult, load_transcript  # noqa: E402
from webhook_server import collect_transcripts  # noqa: E402

MODULE_NAME = "demo03-meeting-actions"


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT §6，另加本模組專屬三個）。"""
    parser = argparse.ArgumentParser(
        prog="demo03-meeting-actions",
        description="會議逐字稿 → 摘要 / 決策 / 指定負責人的行動清單",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", dest="mock", action="store_false", help="串接真實 API 與 Webhook")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="跑完流程但不實際發送")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="發送管道，預設 console",
    )
    parser.add_argument("--config", default=None, help="設定檔路徑，預設同目錄 config.yaml")
    parser.add_argument("--transcript", default=None, help="指定逐字稿 JSON（覆寫 config 的 mock.transcript）")
    parser.add_argument("--serve", action="store_true", help="--live 時啟動 Webhook 等待逐字稿回呼")
    parser.add_argument("--json", dest="json_out", action="store_true", help="把結果 dict 以 JSON 印到 stdout")
    # exit_on_red 不開放 CLI 設定：測試需要拋 RedAlert 而非讓行程退出
    parser.set_defaults(exit_on_red=True)
    return parser


def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，確保任何 cwd 下執行結果一致。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (MODULE_DIR / path)


def _load_transcripts(
    args: argparse.Namespace, config: dict[str, Any], diagnostics: Diagnostics
) -> list[dict[str, Any]]:
    """取得待處理的逐字稿：mock 讀檔，live 走 Webhook。取不到走紅色警報。"""
    explicit = args.transcript or (config.get("mock") or {}).get("transcript")
    if args.mock or args.transcript:
        path = _resolve_path(explicit) if explicit else None
        if path is None or not path.is_file():
            diagnostics.red("no_transcripts", "Webhook URL 無法公開存取", "確認網路狀態與權限")
            return []
        return [load_transcript(path)]
    if not args.serve:
        diagnostics.red("no_transcripts", "Webhook URL 無法公開存取", "確認網路狀態與權限")
        return []
    webhook = config.get("webhook") or {}
    transcripts = collect_transcripts(
        host=str(webhook.get("host", "127.0.0.1")),
        port=int(webhook.get("port", 8093)),
        path=str(webhook.get("path", "/meeting-transcript")),
        wait_seconds=int(webhook.get("wait_seconds", 300)),
        max_body_bytes=int(webhook.get("max_body_bytes", 1_048_576)),
    )
    if not transcripts:
        diagnostics.red("no_transcripts", "Webhook URL 無法公開存取", "確認網路狀態與權限")
    return transcripts


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


def plan_deliveries(
    transcript: dict[str, Any], gate: AutonomyGate, is_dry_run: bool
) -> list[dict[str, Any]]:
    """為每位與會者決定是自動送出還是留成草稿（自主權階梯的落地點）。"""
    deliveries: list[dict[str, Any]] = []
    for attendee in transcript.get("attendees", []):
        recipient = str(attendee.get("email", ""))
        if is_dry_run:
            action = "dry_run"
        else:
            action = "auto_sent" if gate.can_send(recipient) else "draft"
        deliveries.append(
            {
                "recipient": recipient,
                "name": str(attendee.get("name", "")),
                "action": action,
                "effective_level": gate.effective_level(recipient).value,
            }
        )
    return deliveries


def _format_action_line(index: int, item: dict[str, Any]) -> str:
    owner = item["owner"] or "⚠ 未指定負責人（請人工補上，系統不猜測）"
    due = item["due_hint"] or "未提及期限"
    return (
        f"{index}. [{owner}] {item['text']}\n"
        f"   期限：{due}｜信心：{item['confidence']:.2f}｜出處：第 {item['line']} 句（{item['speaker']}）"
    )


def render_message(
    result: ExtractionResult, deliveries: list[dict[str, Any]], config: dict[str, Any]
) -> str:
    """組出寄給與會者的行動清單全文。"""
    module = config.get("module") or {}
    quality = result.quality
    lines = [f"📋 {module.get('name', MODULE_NAME)}｜{result.title}", "", "【摘要】", result.summary, "", "【決策】"]
    lines.extend([f"- {item}" for item in result.decisions] or ["- （本場未拍板任何決策）"])
    lines.extend(["", f"【行動項目】共 {len(result.action_items)} 項"])
    lines.extend(
        [_format_action_line(i, item.to_dict()) for i, item in enumerate(result.action_items, start=1)]
        or ["（本場沒有任何明確承諾。模糊討論不會被寫成行動項目。）"]
    )
    lines.extend(
        [
            "",
            "【品質預期】",
            f"逐字稿判定：{quality['profile']}（重疊發言比例 {quality['overlap_ratio']:.0%}）"
            f"｜預期準確率 {quality['accuracy_min']:.0%}-{quality['accuracy_max']:.0%}",
        ]
    )
    lines.extend([f"⚠ {warning}" for warning in result.warnings])
    lines.extend(["", "【發送】", *[f"- {d['name']} <{d['recipient']}>：{d['action']}" for d in deliveries]])
    return "\n".join(lines)


def dispatch(
    notifier: Notifier, message: str, subject: str, deliveries: list[dict[str, Any]], is_dry_run: bool
) -> bool:
    """實際送出。DRAFT 狀態一樣會送到營運者的管道，但主旨標明待審。"""
    if is_dry_run:
        print(message)
        return False
    is_draft = not any(item["action"] == "auto_sent" for item in deliveries)
    prefix = "[草稿待審]" if is_draft else "[已自動發送]"
    return notifier.send(message, subject=f"{prefix} {subject}")


def process_transcript(
    transcript: dict[str, Any],
    extractor: ActionExtractor,
    gate: AutonomyGate,
    notifier: Notifier,
    config: dict[str, Any],
    is_dry_run: bool,
) -> dict[str, Any]:
    """處理單一場會議，回傳可供測試斷言的結果 dict。"""
    result = extractor.extract(transcript)
    deliveries = plan_deliveries(transcript, gate, is_dry_run)
    message = render_message(result, deliveries, config)
    delivered = dispatch(notifier, message, result.title, deliveries, is_dry_run)
    payload = result.to_dict()
    payload.update({"deliveries": deliveries, "message": message, "delivered": delivered})
    return payload


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config_path = _resolve_path(args.config) if args.config else MODULE_DIR / "config.yaml"
    required_env = [] if args.mock else ["ANTHROPIC_API_KEY"]
    config = load_config(config_path, required_env=required_env)
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=args.exit_on_red)
    transcripts = _load_transcripts(args, config, diagnostics)
    llm_config = config.get("llm") or {}
    extractor = ActionExtractor(
        config=config,
        llm=LLMClient(
            mock=args.mock,
            model=str(llm_config.get("model", "claude-sonnet-5")),
            context_note=llm_config.get("context_note"),
        ),
        diagnostics=diagnostics,
    )
    gate = build_gate(config, diagnostics)
    notifier = Notifier(channel=args.notify)
    meetings = [
        process_transcript(item, extractor, gate, notifier, config, args.dry_run)
        for item in transcripts
    ]
    diagnostics.green(f"已處理 {len(meetings)} 場會議")
    return {
        "module": config.get("module") or {},
        "mode": "mock" if args.mock else "live",
        "notify_channel": args.notify,
        "is_dry_run": bool(args.dry_run),
        "meetings": meetings,
        "amber_count": diagnostics.amber_count,
        "autonomy_warnings": list(gate.warnings),
    }


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    if args.json_out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    total_actions = sum(len(meeting["action_items"]) for meeting in result["meetings"])
    unassigned = sum(meeting["unassigned_count"] for meeting in result["meetings"])
    print(
        f"\n完成：{len(result['meetings'])} 場會議｜{total_actions} 項行動"
        f"（{unassigned} 項未指定負責人）｜amber {result['amber_count']} 則"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
