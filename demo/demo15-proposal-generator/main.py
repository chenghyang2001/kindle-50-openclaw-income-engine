"""demo15 提案與報價生成器 —— 主流程（Level 2 代理商，自動化模組 #15）。

探索會議結束 → 讀 CRM 交易與會議筆記 → 依 RATE_CARD 產出 A/B/C 三個投資選項
→ LLM 撰寫敘事（**不含任何金額**）→ 組成「草稿・待核准」提案 → 交人工覆核。

書中原始流程：讀取探索會議筆記 → 自動研究客戶公司新聞 → 依 RATE_CARD 生成服務選項
→ 產出專屬排版文件 → **依核准模式**發送電子簽署（本模組永遠停在「待人工核准」）。

用法：
    python main.py --mock                       # 零憑證零網路跑完整流程
    python main.py --mock --dry-run             # 跑完但不寫檔、不寫帳、不通知
    python main.py --mock --autonomy supervised_auto   # 白名單客戶可自動寄出提案草稿
    python main.py --mock --as-of 2026-08-24    # 固定報價日，產出可重現
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
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
from pricing import (  # noqa: E402
    PricingError,
    QuoteEngine,
    RateCard,
    format_money,
    to_decimal,
)
from proposal_builder import (  # noqa: E402
    ProposalDocument,
    ProposalError,
    build_proposal,
    generate_narrative,
)

MODULE_LABEL = "demo15-proposal-generator"
DELIVERY_AUTO = "auto_send_to_client"
DELIVERY_INTERNAL = "internal_review_only"


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT.md §6，另加 4 個本模組專屬旗標）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_LABEL, description="提案與報價生成器（Level 2 模組 #15）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實 Claude API 與 CRM 匯出檔")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不寫檔、不寫帳、不通知")
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
    parser.add_argument("--state-file", default=None, help="提案台帳路徑（預設同目錄 .proposals.json）")
    parser.add_argument("--output-dir", default=None, help="提案 Markdown 輸出目錄")
    parser.add_argument("--as-of", default=None, help="報價日 YYYY-MM-DD（預設今天），供重現用")
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


def _resolve_as_of(args: argparse.Namespace) -> date:
    """決定報價日。給了 --as-of 就用它（報價有效期限才能被測試重現）。"""
    raw = getattr(args, "as_of", None)
    if not raw:
        return datetime.now(timezone.utc).date()
    try:
        return date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(f"--as-of 必須是 YYYY-MM-DD，收到 {raw!r}") from exc


def _resolve_signature_config(config: dict[str, Any], diag: Diagnostics) -> dict[str, Any]:
    """取出簽署設定並**強制**人工核准。設定檔想關掉這道閘門是不被允許的。"""
    signature = dict(config.get("signature") or {})
    if not signature.get("require_human_approval", True) or not signature.get("never_auto_send", True):
        diag.amber(
            "config.signature 試圖關閉電子簽署的人工核准閘門",
            "已強制忽略該設定；此閘門不可由設定檔關閉（客戶簽署即成立合約）",
        )
    signature["require_human_approval"] = True
    signature["never_auto_send"] = True
    return signature


def _load_deals(config: dict[str, Any], is_mock: bool, diag: Diagnostics) -> list[dict[str, Any]]:
    """取得待處理的 CRM 交易。mock 讀本地 JSON；live 讀 CRM 匯出檔。"""
    if is_mock:
        mock_cfg = config.get("mock") or {}
        path = MODULE_DIR / str(mock_cfg.get("deals_path", "mock/deals.json"))
    else:
        export_path = str((config.get("crm") or {}).get("export_path", "")).strip()
        if not export_path:
            diag.red(
                "live 模式找不到 CRM 交易來源",
                "config.yaml 的 crm.export_path 未設定",
                "填入 CRM 匯出的 JSON 路徑（Deals + Meeting Notes + Company Profile），或改用 --mock",
            )
            return []
        path = Path(export_path).expanduser()
    return _read_deals_file(path)


def _read_deals_file(path: Path) -> list[dict[str, Any]]:
    """讀交易 JSON。格式錯誤一律當場報錯，不回空清單假裝「今天沒有案子」。"""
    if not path.is_file():
        raise FileNotFoundError(f"找不到交易資料檔：{path.resolve()}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"交易資料不是合法 JSON：{path.resolve()}｜{exc}") from exc
    deals = payload.get("deals") if isinstance(payload, dict) else payload
    if not isinstance(deals, list):
        raise ValueError(f"交易資料格式錯誤，應為 list 或含 deals 欄位的 dict：{path.resolve()}")
    return deals


def _requested_discount(deal: dict[str, Any]) -> Decimal | None:
    """讀取業務手動要求的折扣率；沒填就回 None（走級距表）。"""
    raw = deal.get("requested_discount_rate")
    if raw is None:
        return None
    return to_decimal(raw, f"deal[{deal.get('deal_id')}].requested_discount_rate")


def _fixture_path(deal: dict[str, Any], config: dict[str, Any], is_mock: bool) -> Path | None:
    """mock 模式的敘事 fixture 路徑；live 模式回 None（真的呼叫 Claude）。"""
    if not is_mock:
        return None
    mock_cfg = config.get("mock") or {}
    narrative_dir = MODULE_DIR / str(mock_cfg.get("narrative_dir", "mock/narratives"))
    return narrative_dir / str(deal.get("narrative_fixture", f"{deal.get('deal_id')}.md"))


def _build_document(
    deal: dict[str, Any],
    engine: QuoteEngine,
    llm: LLMClient,
    gate: AutonomyGate,
    config: dict[str, Any],
    context: dict[str, Any],
) -> ProposalDocument:
    """為單一交易產出一份提案草稿。"""
    options = engine.build_options(
        list(deal.get("requested_services") or []), _requested_discount(deal)
    )
    drafting = config.get("drafting") or {}
    narrative = generate_narrative(
        deal=deal,
        llm=llm,
        prompt_path=MODULE_DIR / str(drafting.get("prompt_path", "prompts/draft_proposal.md")),
        fixture=context["fixture_of"](deal),
        max_tokens=int(drafting.get("max_tokens", 1800)),
    )
    recipient = str(deal.get("contact_email", ""))
    return build_proposal(
        deal=deal,
        options=options,
        narrative=narrative,
        drafting_config=drafting,
        signature_config=context["signature"],
        quote_date=context["as_of"],
        valid_until=engine.valid_until(context["as_of"]),
        delivery_mode=DELIVERY_AUTO if gate.can_send(recipient) else DELIVERY_INTERNAL,
    )


def _process_deals(
    deals: list[dict[str, Any]],
    engine: QuoteEngine,
    llm: LLMClient,
    gate: AutonomyGate,
    config: dict[str, Any],
    context: dict[str, Any],
    diag: Diagnostics,
) -> tuple[list[ProposalDocument], list[dict[str, str]]]:
    """逐筆產出提案，並把每種例外轉成明確的 AMBER，不靜默略過任何一筆。"""
    documents: list[ProposalDocument] = []
    failed: list[dict[str, str]] = []
    for deal in deals:
        deal_id = str(deal.get("deal_id", "UNKNOWN"))
        try:
            document = _build_document(deal, engine, llm, gate, config, context)
        except (PricingError, ProposalError, OSError) as exc:
            diag.amber(f"{deal_id} 提案生成失敗：{exc}", "修正 CRM 資料或 RATE_CARD 後重跑此筆")
            failed.append({"deal_id": deal_id, "reason": str(exc)})
            continue
        _report_document(document, diag)
        documents.append(document)
    return documents, failed


def _report_document(document: ProposalDocument, diag: Diagnostics) -> None:
    """把單份提案的品質訊號轉成 GREEN / AMBER。"""
    if document.redactions:
        diag.amber(
            f"{document.proposal_id} 敘事中出現 {len(document.redactions)} 處模型自行寫出的金額："
            f"{'、'.join(document.redactions)}",
            "已遮蔽為佔位字串；請檢查提示詞是否被繞過，價格一律以 RATE_CARD 計算結果為準",
        )
    if document.missing_sections:
        diag.amber(
            f"{document.proposal_id} 缺少必要段落：{'、'.join(document.missing_sections)}",
            "在 prompts/draft_proposal.md 強調段落標題必須逐字輸出",
        )
    for issue in document.issues:
        diag.amber(f"{document.proposal_id} {issue}", "轉交業務主管人工核價後再送出")
    if not document.issues and not document.redactions:
        diag.green(f"{document.proposal_id} 草稿完成（{document.status_label}）")


# --------------------------------------------------------------------------- #
# 台帳與輸出
# --------------------------------------------------------------------------- #
def _resolve_state_file(args: argparse.Namespace, config: dict[str, Any]) -> Path:
    """決定台帳路徑：CLI 覆寫優先，否則取 config.state.ledger_file（相對於模組目錄）。"""
    if args.state_file:
        return Path(args.state_file).expanduser().resolve()
    name = str((config.get("state") or {}).get("ledger_file", ".proposals.json"))
    path = Path(name).expanduser()
    return path if path.is_absolute() else (MODULE_DIR / path)


def _load_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """讀既有台帳（proposal_id -> 紀錄）。檔案不存在或壞掉都回空 dict 但不吞錯訊息。"""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[AMBER] [{MODULE_LABEL}] 台帳無法讀取，視為空台帳：{path}｜{exc}", file=sys.stderr)
        return {}
    entries = payload.get("proposals", []) if isinstance(payload, dict) else []
    return {str(entry.get("proposal_id")): entry for entry in entries if entry.get("proposal_id")}


def _ledger_entry(document: ProposalDocument) -> dict[str, Any]:
    """台帳只記可稽核的關鍵欄位，不存整份 Markdown（避免台帳膨脹成幾 MB）。"""
    recommended = document.recommended_option
    return {
        "proposal_id": document.proposal_id,
        "deal_id": document.deal_id,
        "client_name": document.client_name,
        "status": document.status,
        "quote_date": document.quote_date,
        "valid_until": document.valid_until,
        "recommended_tier": document.recommended_tier,
        "recommended_setup_total": f"{recommended.setup_total:.2f}",
        "recommended_monthly_total": f"{recommended.monthly_total:.2f}",
        "currency": document.currency,
        "requires_human_pricing": document.requires_human_pricing,
        "signature_status": document.signature_request["status"],
        "signature_is_sent": document.signature_request["is_sent"],
        "delivery_mode": document.delivery_mode,
        "generated_at": document.generated_at,
    }


def _save_ledger(path: Path, ledger: dict[str, dict[str, Any]]) -> Path:
    """把台帳寫回磁碟（UTF-8，排序後輸出以利 diff）。"""
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "proposal_count": len(ledger),
        "proposals": [ledger[key] for key in sorted(ledger)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _mark_regenerated(
    documents: list[ProposalDocument], ledger: dict[str, dict[str, Any]], diag: Diagnostics
) -> None:
    """同一天對同一筆交易重跑會覆寫舊草稿——標記出來，避免覆蓋掉人工已改過的版本。"""
    for document in documents:
        if document.proposal_id in ledger:
            document.is_regenerated = True
            diag.amber(
                f"{document.proposal_id} 今日已產生過草稿，本次為重新生成",
                "若舊草稿已由人工修改，請先備份 output/ 內的檔案再重跑",
            )


def _resolve_output_dir(args: argparse.Namespace, config: dict[str, Any]) -> Path:
    """決定輸出目錄：CLI 覆寫優先，否則取 config.output.dir（相對於模組目錄）。"""
    raw = args.output_dir or str((config.get("output") or {}).get("dir", "output"))
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (MODULE_DIR / path)


def _write_documents(
    documents: list[ProposalDocument], output_dir: Path, config: dict[str, Any]
) -> list[str]:
    """把每份提案寫成 Markdown。PDF 轉檔交由下游排版工具，本模組不引入額外依賴。"""
    template = str((config.get("output") or {}).get("filename_template", "{proposal_id}.md"))
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for document in documents:
        path = output_dir / template.format(proposal_id=document.proposal_id)
        path.write_text(document.markdown, encoding="utf-8")
        written.append(str(path))
    return written


# --------------------------------------------------------------------------- #
# 摘要與通知
# --------------------------------------------------------------------------- #
def _build_summary(
    config: dict[str, Any],
    documents: list[ProposalDocument],
    failed: list[dict[str, str]],
    autonomy_level: str,
) -> str:
    """組出給人看的處理摘要（通知內文）。金額一律取自報價引擎的計算結果。"""
    module = config.get("module") or {}
    counts = Counter(document.status for document in documents)
    lines = [
        f"【{module.get('name', '提案與報價生成器')}】模組 {module.get('id', '15')} 執行結果",
        f"提案總數：{len(documents)}｜可送審 {counts.get('draft_pending_approval', 0)}"
        f"｜需人工核價 {counts.get('needs_pricing_review', 0)}｜失敗 {len(failed)}",
        f"自主權層級：{autonomy_level}｜電子簽署：0 份送出（一律待人工核准）",
        "",
    ]
    for document in documents:
        recommended = document.recommended_option
        lines.append(
            f"[{document.status}] {document.proposal_id}｜{document.client_name}"
            f"｜推薦 {recommended.tier_name}"
            f"｜建置 {format_money(recommended.setup_total, document.currency)}"
            f"｜月費 {format_money(recommended.monthly_total, document.currency)}"
            f"｜有效至 {document.valid_until}"
        )
    lines.extend(f"[failed] {item['deal_id']}｜{item['reason']}" for item in failed)
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
    return notifier.send(summary_text, subject="提案草稿產出結果 - demo15")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    is_mock = not bool(getattr(args, "live", False))
    config = load_config(Path(args.config))
    diag = Diagnostics(MODULE_LABEL, exit_on_red=False)
    gate = _build_gate(config, args, diag)
    engine = QuoteEngine(config.get("pricing"), RateCard(config.get("rate_card")))
    context = {
        "as_of": _resolve_as_of(args),
        "signature": _resolve_signature_config(config, diag),
        "fixture_of": lambda deal: _fixture_path(deal, config, is_mock),
    }
    llm = LLMClient(mock=is_mock, context_note=(config.get("llm") or {}).get("context_note"))
    deals = _load_deals(config, is_mock, diag)
    documents, failed = _process_deals(deals, engine, llm, gate, config, context, diag)

    state_file = _resolve_state_file(args, config)
    ledger = _load_ledger(state_file)
    _mark_regenerated(documents, ledger, diag)
    written: list[str] = []
    output_dir = _resolve_output_dir(args, config)
    if not args.dry_run and documents:
        written = _write_documents(documents, output_dir, config)
        ledger.update({doc.proposal_id: _ledger_entry(doc) for doc in documents})
        _save_ledger(state_file, ledger)

    summary_text = _build_summary(config, documents, failed, gate.level.value)
    notified = _notify(args, config, summary_text, diag)
    paths = {"state_file": state_file, "output_dir": output_dir, "output_files": written}
    return _build_result(args, config, gate, diag, documents, failed, summary_text, notified, paths)


def _build_result(
    args: argparse.Namespace,
    config: dict[str, Any],
    gate: AutonomyGate,
    diag: Diagnostics,
    documents: list[ProposalDocument],
    failed: list[dict[str, str]],
    summary_text: str,
    notified: bool,
    paths: dict[str, Any],
) -> dict[str, Any]:
    """組出 run() 的回傳結構（鍵名沿用 CONTRACT §6 技術債附註建議的 6 個標準鍵）。"""
    return {
        "module_id": str((config.get("module") or {}).get("id", "15")),
        "module_name": str((config.get("module") or {}).get("name", "提案與報價生成器")),
        "mode": "mock" if not getattr(args, "live", False) else "live",
        "dry_run": bool(args.dry_run),
        "autonomy_level": gate.level.value,
        "proposal_count": len(documents),
        "proposals": [document.to_dict() for document in documents],
        "needs_pricing_review": [d.proposal_id for d in documents if d.requires_human_pricing],
        "signature_requests": [d.signature_request for d in documents],
        "signatures_sent": sum(1 for d in documents if d.signature_request["is_sent"]),
        "redaction_count": sum(len(d.redactions) for d in documents),
        "failed": failed,
        "state_file": str(paths["state_file"]),
        "output_dir": str(paths["output_dir"]),
        "output_files": list(paths["output_files"]),
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
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
