"""demo18 — 合約審查與條款提取 主流程（第 05 章 #18 / 附錄F p15-p16）。

流程（Clause Comparison Engine 四步驟）：
    Email 附件 / Drive Watch / CLI 收件 → Step 1 逐字提取 → Step 2 與 CLAUSE_LIBRARY 比對
    → 四分類 Standard / Deviation / Missing / Red Flag → Step 4 紅旗直送資深合夥人，
      其餘彙整為結構化審查備忘錄（高階摘要 + 條款對比表 + 缺失條款建議）。

`--mock` 為預設模式：零憑證、零網路，讀 mock/ 目錄的純文字合約跑完整條流程。

⚠ 法律免責：本工具不構成法律意見，輸出僅供初步篩選。
   任何簽署前必須由合格法律專業人員審閱。詳見 README「法律免責聲明」。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient, LLMError  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

from classifier import (  # noqa: E402
    ClauseClassifier,
    JurisdictionError,
    ReviewResult,
    Verdict,
    build_escalations,
    check_jurisdiction,
)
from extractor import ClauseExtractor, ContractDocument, load_contract  # noqa: E402

MODULE_NAME = "demo18-contract-review"
DISCLAIMER = (
    "本備忘錄由自動化工具產出，僅供初步篩選，不構成法律意見。"
    "簽署前必須由合格法律專業人員審閱。"
)


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT §6，另加本模組專屬四個）。"""
    parser = argparse.ArgumentParser(
        prog="demo18-contract-review",
        description="合約 → 逐字條款提取 → 四分類比對 → 風險備忘錄與紅旗升級",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", dest="mock", action="store_false", help="串接真實 API")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="跑完流程但不發送、不寫狀態檔")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="發送管道，預設 console",
    )
    parser.add_argument("--config", default=None, help="設定檔路徑，預設同目錄 config.yaml")
    parser.add_argument("--contract", default=None, help="指定合約 JSON（覆寫 config 的 mock.contract）")
    parser.add_argument(
        "--state-file",
        default=None,
        help="已審查台帳路徑，預設取自 config 的 state.store_file（相對於模組目錄）。"
        "測試與 CI 請指到暫存目錄，避免污染工作樹。",
    )
    parser.add_argument("--json", dest="json_out", action="store_true", help="把結果 dict 以 JSON 印到 stdout")
    # exit_on_red 不開放 CLI 設定：測試需要拋 RedAlert 而非讓行程退出
    parser.set_defaults(exit_on_red=True)
    return parser


def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，確保任何 cwd 下執行結果一致。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (MODULE_DIR / path)


# --------------------------------------------------------------------------- #
# 已審查台帳（--state-file）：用於抑制同一份合約的重複資深合夥人警報
# --------------------------------------------------------------------------- #
def load_ledger(path: Path) -> dict[str, Any]:
    """讀取台帳。缺檔視為空台帳；內容毀損則拋錯（靜默清空會讓警報重複轟炸）。"""
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"台帳格式錯誤（應為物件）：{path}")
    return data


def save_ledger(path: Path, ledger: dict[str, Any]) -> Path:
    """寫回台帳（UTF-8）。目錄不存在就補建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(ledger, handle, ensure_ascii=False, indent=2, sort_keys=True)
    return path


def is_already_alerted(ledger: dict[str, Any], document: ContractDocument) -> bool:
    """同一 contract_id + 相同全文雜湊且先前已發過紅旗警報，才算重複。"""
    entry = ledger.get(document.contract_id)
    if not isinstance(entry, dict):
        return False
    return entry.get("content_hash") == document.content_hash and bool(
        entry.get("red_flag_rule_ids")
    )


def ledger_entry(document: ContractDocument, result: ReviewResult) -> dict[str, Any]:
    """單筆台帳紀錄：留下雜湊與判定結果，供稽核回溯。"""
    return {
        "content_hash": document.content_hash,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "counts": result.counts,
        "red_flag_rule_ids": [hit.rule_id for hit in result.red_flags],
        "needs_review_count": result.needs_review_count,
    }


# --------------------------------------------------------------------------- #
# 自主權與發送
# --------------------------------------------------------------------------- #
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
    recipients: list[str], gate: AutonomyGate, is_dry_run: bool
) -> list[dict[str, Any]]:
    """為每位備忘錄收件人決定是自動送出還是留成草稿（自主權階梯的落地點）。"""
    deliveries: list[dict[str, Any]] = []
    for recipient in recipients:
        if is_dry_run:
            action = "dry_run"
        else:
            action = "auto_sent" if gate.can_send(recipient) else "draft"
        deliveries.append(
            {
                "recipient": recipient,
                "action": action,
                "effective_level": gate.effective_level(recipient).value,
            }
        )
    return deliveries


# --------------------------------------------------------------------------- #
# 備忘錄與警報文字
# --------------------------------------------------------------------------- #
_VERDICT_ICON = {
    Verdict.STANDARD.value: "✅",
    Verdict.DEVIATION.value: "⚠",
    Verdict.MISSING.value: "🕳",
    Verdict.RED_FLAG.value: "🚩",
}


def _render_clause_block(item: dict[str, Any]) -> str:
    """單一條款在備忘錄中的區塊。引文一律標明是逐字原文。"""
    icon = _VERDICT_ICON.get(item["verdict"], "•")
    head = (
        f"{icon} [{item['verdict']}] {item['name_zh']}（{item['name_en']}）"
        f"｜第 {item['section_ref'] or '—'} 條"
    )
    lines = [head, f"　基準立場：{item['standard_position']}"]
    if item["quote"]:
        lines.append(f"　合約原文（逐字引用）：「{item['quote']}」")
    else:
        lines.append("　合約原文：（未取得通過逐字驗證的引文，系統不輸出未驗證文字）")
    for finding in item["findings"]:
        lines.append(f"　→ 風險：{finding['note']}")
        if finding["suggested_wording"]:
            lines.append(f"　→ 建議字詞：{finding['suggested_wording']}")
    if item["needs_human_review"]:
        lines.append(f"　⚠ 需人工複核：{item['review_reason'] or '系統無法確認，未給予通過判定'}")
    return "\n".join(lines)


def render_memo(
    document: ContractDocument,
    payload: dict[str, Any],
    deliveries: list[dict[str, Any]],
    config: dict[str, Any],
) -> str:
    """組出結構化審查備忘錄（附錄F Output：高階摘要 + 條款對比表 + 缺失條款建議）。"""
    counts = payload["counts"]
    review_cfg = config.get("review") or {}
    lines = [
        f"📄 {review_cfg.get('memo_title', '合約審查備忘錄')}",
        f"合約：{document.title}",
        f"對方：{document.counterparty}｜來源：{document.received_via}｜{document.page_count} 頁",
        f"管轄權：{document.jurisdiction}｜年度金額：{document.currency} {document.annual_value}",
        "",
        "【高階摘要】",
        f"共比對 {len(payload['assessments'])} 條標準條款："
        f"通過 {counts[Verdict.STANDARD.value]}、"
        f"偏離 {counts[Verdict.DEVIATION.value]}、"
        f"缺失 {counts[Verdict.MISSING.value]}、"
        f"紅旗 {counts[Verdict.RED_FLAG.value]}；"
        f"{payload['needs_review_count']} 條需人工複核。",
        "",
        "【條款對比表】",
    ]
    lines.extend(_render_clause_block(item) for item in payload["assessments"])
    lines.extend(["", "【提醒】"])
    lines.extend([f"⚠ {item}" for item in payload["warnings"]] or ["（無）"])
    lines.extend(["", "【發送】"])
    lines.extend(f"- {item['recipient']}：{item['action']}" for item in deliveries)
    lines.extend(["", f"⚖ {DISCLAIMER}"])
    return "\n".join(lines)


def render_alert(document: ContractDocument, escalations: list[dict[str, Any]]) -> str:
    """資深合夥人緊急警報文字（Step 4：繞過常規備忘錄）。"""
    lines = [
        "🚩 SENIOR PARTNER ALERT — 合約命中硬性紅線",
        f"合約：{document.title}（{document.contract_id}）",
        f"對方：{document.counterparty}",
        "",
    ]
    for item in escalations:
        lines.append(f"・{item['label_zh']}（規則 {item['rule_id']}）｜第 {item['section_ref']} 條")
        lines.append(f"　命中原文（逐字）：「{item['matched_text']}」")
    lines.extend(["", "本警報已繞過常規審查備忘錄流程。", f"⚖ {DISCLAIMER}"])
    return "\n".join(lines)


def dispatch(
    notifier: Notifier,
    memo: str,
    subject: str,
    deliveries: list[dict[str, Any]],
    is_dry_run: bool,
) -> bool:
    """實際送出備忘錄。DRAFT 一樣送到營運者管道，但主旨標明待審。"""
    if is_dry_run:
        print(memo)
        return False
    is_draft = not any(item["action"] == "auto_sent" for item in deliveries)
    prefix = "[草稿待審]" if is_draft else "[已自動發送]"
    return notifier.send(memo, subject=f"{prefix} {subject}")


def dispatch_alert(
    notifier: Notifier,
    document: ContractDocument,
    escalations: list[dict[str, Any]],
    is_dry_run: bool,
) -> bool:
    """送出紅旗警報。已抑制（重複合約）或空跑時不發送，但仍回報狀態。"""
    pending = [item for item in escalations if not item["is_suppressed"]]
    if not pending or is_dry_run:
        return False
    return notifier.send(render_alert(document, pending), subject="[緊急｜紅旗] 合約硬性紅線警報")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _load_document(
    args: argparse.Namespace, config: dict[str, Any], diagnostics: Diagnostics
) -> ContractDocument:
    """取得待審合約。取不到走紅色警報（沒有輸入就沒有審查，不能靜默跳過）。"""
    explicit = args.contract or (config.get("mock") or {}).get("contract")
    path = _resolve_path(explicit) if explicit else None
    if path is None or not path.is_file():
        diagnostics.red(
            "no_contract_file",
            f"找不到合約檔案：{path}",
            "用 --contract 指定合約 JSON，或修正 config 的 mock.contract",
        )
        raise FileNotFoundError(f"找不到合約檔案：{path}")
    return load_contract(path)


def _resolve_state_file(args: argparse.Namespace, config: dict[str, Any]) -> Path:
    """CLI 覆寫優先，沒給才回頭取 config（--state-file 預設 None，不會蓋掉 config）。"""
    raw = args.state_file or (config.get("state") or {}).get("store_file", "state/.reviewed.json")
    return _resolve_path(raw)


def _build_review(
    args: argparse.Namespace,
    config: dict[str, Any],
    document: ContractDocument,
    diagnostics: Diagnostics,
) -> ReviewResult:
    """Step 1 + Step 2 + 四分類。管轄權不合直接紅色警報，不做任何比對。"""
    try:
        check_jurisdiction(document, config)
    except JurisdictionError as exc:
        diagnostics.red(
            "jurisdiction_mismatch",
            str(exc),
            "先在 config.yaml 設定正確的 jurisdiction.code 與對應的 CLAUSE_LIBRARY 基準立場",
        )
        raise
    llm_config = config.get("llm") or {}
    extractor = ClauseExtractor(
        config=config,
        llm=LLMClient(
            mock=args.mock,
            model=str(llm_config.get("model", "claude-sonnet-5")),
            context_note=llm_config.get("context_note"),
        ),
        diagnostics=diagnostics,
    )
    classifier = ClauseClassifier(config=config, diagnostics=diagnostics)
    return classifier.classify(document, extractor.extract(document))


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config_path = _resolve_path(args.config) if args.config else MODULE_DIR / "config.yaml"
    required_env: list[str] = []
    config = load_config(config_path, required_env=required_env)
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=args.exit_on_red)
    document = _load_document(args, config, diagnostics)
    result = _build_review(args, config, document, diagnostics)
    payload = result.to_dict()

    state_file = _resolve_state_file(args, config)
    ledger = load_ledger(state_file)
    escalations = build_escalations(result, config, is_already_alerted(ledger, document))
    gate = build_gate(config, diagnostics)
    deliveries = plan_deliveries(
        [str(item) for item in (config.get("output") or {}).get("memo_recipients") or []],
        gate,
        args.dry_run,
    )
    memo = render_memo(document, payload, deliveries, config)
    notifier = Notifier(channel=args.notify, config=config.get("channel") or {})
    is_alert_sent = dispatch_alert(notifier, document, escalations, args.dry_run)
    is_memo_sent = dispatch(notifier, memo, document.contract_id, deliveries, args.dry_run)

    saved_state = None
    if not args.dry_run:
        ledger[document.contract_id] = ledger_entry(document, result)
        saved_state = str(save_ledger(state_file, ledger))
    diagnostics.green(
        f"已審查 {document.contract_id}：{payload['counts']}｜紅旗 {len(result.red_flags)} 項"
    )
    return {
        "module": config.get("module") or {},
        "mode": "mock" if args.mock else "live",
        "notify_channel": args.notify,
        "is_dry_run": bool(args.dry_run),
        "contract": document.to_summary(),
        "review": payload,
        "escalations": escalations,
        "deliveries": deliveries,
        "memo": memo,
        "is_memo_sent": is_memo_sent,
        "is_alert_sent": is_alert_sent,
        "state_file": saved_state,
        "disclaimer": DISCLAIMER,
        "amber_count": diagnostics.amber_count,
        "autonomy_warnings": list(gate.warnings),
    }


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (LLMError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    if args.json_out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    counts = result["review"]["counts"]
    print(
        f"\n完成：{result['contract']['contract_id']}｜"
        f"通過 {counts[Verdict.STANDARD.value]}／"
        f"偏離 {counts[Verdict.DEVIATION.value]}／"
        f"缺失 {counts[Verdict.MISSING.value]}／"
        f"紅旗 {counts[Verdict.RED_FLAG.value]}｜"
        f"待人工 {result['review']['needs_review_count']}｜"
        f"amber {result['amber_count']} 則"
    )
    print(f"⚖ {DISCLAIMER}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
