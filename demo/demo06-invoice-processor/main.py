"""demo06 發票處理與費用分類 —— 主流程（第 03 章模組 06）。

The Eyes（Email Inbox）→ The Brain（OpenClaw + Claude）→ The Hands（Xero / QuickBooks）。
發票抵達後 60 秒內完成：提取欄位 -> 驗稅 -> 對應科目 -> 發布 -> 標準化命名存檔。

用法：
    python main.py --mock                 # 零憑證零網路跑完整流程
    python main.py --mock --dry-run       # 跑完但不發布也不通知
    python main.py --mock --autonomy draft  # 降級為草稿模式（不自動入帳）
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402
from accounting import AccountingPoster, PostingResult  # noqa: E402
from extractor import ExtractedInvoice, extract_invoice, load_mock_invoices  # noqa: E402

MODULE_LABEL = "demo06-invoice-processor"


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT.md §6，另加 --autonomy 覆寫）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_LABEL, description="發票處理與費用分類（第 03 章模組 06）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實 Claude API 與會計系統")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不發布、不通知")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default=None,
        help="通知通道，預設讀 config.runtime.notify_channel",
    )
    parser.add_argument(
        "--config", default=str(MODULE_DIR / "config.yaml"), help="設定檔路徑，預設同目錄 config.yaml"
    )
    parser.add_argument(
        "--autonomy",
        choices=[level.value for level in AutonomyLevel],
        default=None,
        help="覆寫 config.runtime.autonomy（read_only / draft / supervised_auto）",
    )
    return parser


def _build_gate(config: dict[str, Any], args: argparse.Namespace, diag: Diagnostics) -> AutonomyGate:
    """依 config + CLI 覆寫建立自主權閘門，並把警告轉成 AMBER。"""
    runtime = config.get("runtime") or {}
    level_name = str(getattr(args, "autonomy", None) or runtime.get("autonomy", "draft")).lower()
    try:
        level = AutonomyLevel(level_name)
    except ValueError as exc:
        raise ValueError(f"自主權層級不合法：{level_name}") from exc
    gate = AutonomyGate(
        level=level,
        approved_senders=list(runtime.get("approved_senders") or []),
        days_in_draft=int(runtime.get("days_in_draft", 0)),
    )
    for warning in gate.warnings:
        diag.amber(warning, "維持 draft 直到滿 14 天並取得客戶書面簽核")
    return gate


def _load_records(config: dict[str, Any], is_mock: bool, diag: Diagnostics) -> list[dict[str, Any]]:
    """取得待處理發票文字。mock 讀 JSON；live 讀郵件閘道轉出的 .txt（本模組不解析 PDF）。"""
    if is_mock:
        mock_cfg = config.get("mock") or {}
        return load_mock_invoices(MODULE_DIR / str(mock_cfg.get("invoices_path", "mock/invoices.json")))
    text_dir = str((config.get("inbox") or {}).get("text_dir", "")).strip()
    if not text_dir:
        diag.red(
            "live 模式找不到收件夾",
            "config.yaml 的 inbox.text_dir 未設定",
            "設定郵件閘道輸出的純文字目錄（PDF 由閘道先轉文字），或改用 --mock",
        )
        return []
    return [
        {"filename": path.with_suffix(".pdf").name, "raw_text": path.read_text(encoding="utf-8")}
        for path in sorted(Path(text_dir).expanduser().glob("*.txt"))
    ]


def _extract_all(
    records: list[dict[str, Any]], config: dict[str, Any], is_mock: bool, diag: Diagnostics
) -> list[ExtractedInvoice]:
    """逐張提取，並把信心不足 / 外幣兩種情況轉成 AMBER 警示。"""
    settings = config.get("extraction") or {}
    llm = None if is_mock else LLMClient(mock=False, context_note=(config.get("llm") or {}).get("context_note"))
    prompt_path = MODULE_DIR / str(settings.get("prompt_path", "prompts/extract_invoice.md"))
    invoices: list[ExtractedInvoice] = []
    for record in records:
        invoice = extract_invoice(record, settings, llm=llm, prompt_path=prompt_path if llm else None)
        if invoice.needs_review:
            diag.amber(
                f"{invoice.filename} 提取信心不足：{'；'.join(invoice.issues)}",
                "人工覆核後手動入帳；掃描品質不佳請要求廠商重寄電子 PDF",
            )
        elif invoice.is_foreign_currency:
            diag.amber(
                f"{invoice.filename} 為外幣（{invoice.currency}）",
                "確認會計系統的匯率來源，或於 config.yaml 補上換算設定",
            )
        else:
            diag.green(f"{invoice.filename} -> {invoice.standard_name}")
        invoices.append(invoice)
    return invoices


def _summarise_totals(results: list[PostingResult]) -> dict[str, str]:
    """只加總「真的入帳」的金額，依幣別分開（不同幣別不可混加）。"""
    totals: dict[str, Decimal] = {}
    for result in results:
        if result.status != "posted" or result.amount is None:
            continue
        totals[result.currency] = totals.get(result.currency, Decimal("0")) + Decimal(result.amount)
    return {currency: f"{amount:.2f}" for currency, amount in sorted(totals.items())}


def _build_summary(
    config: dict[str, Any],
    results: list[PostingResult],
    totals: dict[str, str],
    autonomy_level: str,
) -> str:
    """組出給人看的處理摘要（通知內文）。"""
    module = config.get("module") or {}
    counts = Counter(result.status for result in results)
    lines = [
        f"【{module.get('name', '發票處理與費用分類')}】模組 {module.get('id', '06')} 處理結果",
        f"發票總數：{len(results)}｜已入帳 {counts.get('posted', 0)}"
        f"｜草稿 {counts.get('draft', 0)}｜待覆核 {counts.get('needs_review', 0)}"
        f"｜乾跑 {counts.get('dry_run', 0)}｜失敗 {counts.get('failed', 0)}",
        f"自主權層級：{autonomy_level}",
        "入帳金額：" + ("、".join(f"{cur} {amt}" for cur, amt in totals.items()) or "無"),
        "",
    ]
    for result in results:
        amount = f"{result.currency} {result.amount}" if result.amount else "金額未確定"
        detail = result.standard_name or result.reason
        lines.append(
            f"[{result.status}] {result.filename}｜{result.account_code} {result.account_name}"
            f"｜{amount}｜{detail}"
        )
    return "\n".join(lines)


def _notify(
    args: argparse.Namespace, config: dict[str, Any], summary_text: str, diag: Diagnostics
) -> bool:
    """發送摘要。--dry-run 一律不送，回傳是否真的送出。"""
    if args.dry_run:
        diag.green("--dry-run：略過通知發送")
        return False
    runtime = config.get("runtime") or {}
    channel = args.notify or str(runtime.get("notify_channel", "console"))
    notifier = Notifier(channel=channel, config=config.get("notify") or {})
    return notifier.send(summary_text, subject="發票處理結果 - demo06")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    is_mock = not bool(getattr(args, "live", False))
    config = load_config(Path(args.config))
    diag = Diagnostics(MODULE_LABEL, exit_on_red=False)
    gate = _build_gate(config, args, diag)

    records = _load_records(config, is_mock, diag)
    invoices = _extract_all(records, config, is_mock, diag)

    accounting_cfg = config.get("accounting") or {}
    poster = AccountingPoster(
        accounting_cfg,
        mock=is_mock,
        dry_run=bool(args.dry_run),
        diagnostics=diag,
        output_path=MODULE_DIR / str(accounting_cfg.get("mock_output", "mock/posted.json")),
    )
    recipient = str(accounting_cfg.get("system_recipient", ""))
    can_post = gate.can_send(recipient)
    autonomy_level = gate.effective_level(recipient).value
    results = poster.post_batch(invoices, can_post=can_post)
    posted_path = poster.flush()

    totals = _summarise_totals(results)
    summary_text = _build_summary(config, results, totals, autonomy_level)
    notified = _notify(args, config, summary_text, diag)
    counts = Counter(result.status for result in results)
    return {
        "module": str((config.get("module") or {}).get("id", "06")),
        "mode": "mock" if is_mock else "live",
        "accounting_system": poster.system,
        "autonomy_level": autonomy_level,
        "can_post": can_post,
        "invoice_count": len(invoices),
        "extracted": [invoice.to_dict() for invoice in invoices],
        "postings": [result.to_dict() for result in results],
        "needs_review": [inv.filename for inv in invoices if inv.needs_review],
        "posted_count": counts.get("posted", 0),
        "skipped_count": counts.get("needs_review", 0),
        "failed_count": counts.get("failed", 0),
        "totals_by_currency": totals,
        "posted_path": str(posted_path) if posted_path else None,
        "amber_count": diag.amber_count,
        "warnings": list(gate.warnings),
        "summary_text": summary_text,
        "notified": notified,
    }


def main() -> int:
    """解析參數 -> run() -> 印出/發送結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    if not result["notified"]:
        # console 通道已在 Notifier 內印過，避免重複輸出。
        print(result["summary_text"])
    return 1 if result["failed_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
