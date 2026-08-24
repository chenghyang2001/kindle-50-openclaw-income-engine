"""demo24 — 無偏見人資招募篩選管線 主流程（模組 #24，Level 3 企業級）。

流程：ATS Webhook 收件 → **匿名化**（抹除姓名 / 性別 / 年齡 / 國籍 / 照片 / 畢業年份）
      → 匿名化完整性驗證 → 結構化加權評分 → 三分支處置
      （>75 進短名單並抽前 20% 發非同步影片面試｜<40 延遲 48 小時發溫和拒絕信｜
        命中 disqualifier 立即標記不符合）→ 全程寫入雜湊鏈稽核日誌。

`--mock` 為預設模式：零憑證、零網路，讀 mock/applications.json 跑完整條流程。

安全設計三處，改動前請先讀 README 的「已知限制與法遵責任」：
1. 反偏見四鐵律由 `anonymiser.enforce_bias_switches()` 強制，不符即紅色警報停機。
2. 對外送出前必經 `--dry-run` 內部通訊測試（apxG_p03 全域安全閥）。
3. `--reveal` 揭露真實身分必須附 `--approved-by`，且必然留下稽核紀錄。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

from anonymiser import (  # noqa: E402
    AnonymisationError,
    BiasMitigationError,
    IdentityVault,
    RevealNotAuthorisedError,
    anonymise_all,
    enforce_bias_switches,
    verify_batch,
)
from audit import AuditLog  # noqa: E402
from scorer import (  # noqa: E402
    DECISION_DISQUALIFIED,
    DECISION_HOLD,
    DECISION_REJECT,
    DECISION_SHORTLIST,
    CandidateScore,
    criteria_fingerprint,
    format_shortlist,
    rank,
    score_all,
    select_video_interviews,
)

MODULE_NAME = "demo24-hr-screening"
STATE_VERSION = 1


@dataclass
class RunContext:
    """一次執行所需的全部相依。集中建立，方便測試逐項替換。"""

    config: dict[str, Any]
    diagnostics: Diagnostics
    audit: AuditLog
    gate: AutonomyGate
    notifier: Notifier
    llm: LLMClient
    state: dict[str, Any]
    state_path: Path
    now: datetime
    is_mock: bool


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT §6，另加本模組專屬六個）。"""
    parser = argparse.ArgumentParser(
        prog="demo24-hr-screening",
        description="無偏見人資招募篩選管線：匿名化 → 結構化評分 → 三分支處置 → 稽核軌跡",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", dest="mock", action="store_false", help="串接真實 ATS 與 Claude API")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="跑完流程但不實際發送")
    parser.add_argument("--notify", choices=list(Notifier.SUPPORTED), default="console", help="發送管道")
    parser.add_argument("--config", default=None, help="設定檔路徑，預設同目錄 config.yaml")
    parser.add_argument("--applications", default=None, help="申請資料 JSON（覆寫 config 的 mock.applications）")
    parser.add_argument("--state-file", dest="state_file", default=None, help="狀態檔路徑（覆寫 config）")
    parser.add_argument("--audit-file", dest="audit_file", default=None, help="稽核日誌路徑（覆寫 config）")
    parser.add_argument("--now", default=None, help="以指定 ISO-8601 時間為基準（推進 48 小時延遲用）")
    parser.add_argument("--reveal", default=None, help="揭露某匿名識別碼的真實身分（需 --approved-by）")
    parser.add_argument("--approved-by", dest="approved_by", default=None, help="核准揭露的招募經理姓名")
    parser.add_argument("--reveal-reason", dest="reveal_reason", default="", help="揭露事由（寫入稽核）")
    parser.add_argument("--json", dest="json_out", action="store_true", help="把結果 dict 以 JSON 印到 stdout")
    # exit_on_red 不開放 CLI 設定：測試需要拋 RedAlert 而非讓行程退出
    parser.set_defaults(exit_on_red=True)
    return parser


def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，確保任何 cwd 下執行結果一致。"""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (MODULE_DIR / path)


def _resolve_now(raw: str | None) -> datetime:
    """基準時間：`--now` 優先（測試推進 48 小時用），否則取當下 UTC。"""
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValueError(f"--now 不是合法的 ISO-8601 時間：{raw!r}｜{exc}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _default_state() -> dict[str, Any]:
    """全新狀態檔的骨架。"""
    return {
        "version": STATE_VERSION,
        "last_run_at": None,
        "last_dry_run_preflight_at": None,
        "processed_identifiers": [],
        "pending_rejections": [],
        "revealed": {},
    }


def load_state(path: Path) -> dict[str, Any]:
    """讀狀態檔。不存在視為首次執行；內容損壞則明確報錯，不靜默重置。"""
    if not path.is_file():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"狀態檔不是合法 JSON：{path}｜{exc}") from exc
    except OSError as exc:
        raise OSError(f"讀取狀態檔失敗：{path}｜{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"狀態檔頂層必須是物件：{path}")
    merged = _default_state()
    merged.update(data)
    return merged


def save_state(path: Path, state: dict[str, Any]) -> None:
    """寫回狀態檔（UTF-8，縮排 2）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        raise OSError(f"寫入狀態檔失敗：{path}｜{exc}") from exc


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


def build_context(args: argparse.Namespace) -> RunContext:
    """建立本次執行的所有相依（設定、診斷、稽核、自主權、通知、模型、狀態）。"""
    config_path = _resolve_path(args.config) if args.config else MODULE_DIR / "config.yaml"
    # 先讀一次拿到 live.required_env，再讀一次做環境變數驗證。
    # 兩段式是刻意的：required_env 清單本身就寫在設定檔裡，不能硬編碼在程式中。
    config = load_config(config_path)
    if not args.mock:
        config = load_config(config_path, required_env=list((config.get("live") or {}).get("required_env") or []))
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=args.exit_on_red)
    output = config.get("output") or {}
    state_path = _resolve_path(args.state_file or output.get("state_file", "state/screening-state.json"))
    audit_path = _resolve_path(args.audit_file or output.get("audit_file", "state/audit-log.jsonl"))
    llm_config = config.get("llm") or {}
    return RunContext(
        config=config,
        diagnostics=diagnostics,
        audit=AuditLog(audit_path, MODULE_NAME, actor=str(args.approved_by or "system")),
        gate=build_gate(config, diagnostics),
        notifier=Notifier(channel=args.notify, config=config.get("notify")),
        llm=LLMClient(
            mock=args.mock,
            model=str(llm_config.get("model", "claude-sonnet-5")),
            context_note=llm_config.get("context_note"),
        ),
        state=load_state(state_path),
        state_path=state_path,
        now=_resolve_now(args.now),
        is_mock=bool(args.mock),
    )


def guard_bias_switches(context: RunContext) -> None:
    """反偏見四鐵律檢查。不符即紅色警報停機——這條防線不接受降級續跑。"""
    try:
        enforce_bias_switches(context.config)
    except BiasMitigationError as exc:
        context.audit.record("bias_switch_breach", {"error": str(exc)})
        context.diagnostics.red(
            str(exc),
            "config.yaml 的 bias_mitigation 被改成非法定值",
            "把四個開關改回 true / true / identifiers_only / true 後重跑",
        )
        raise


def preflight(args: argparse.Namespace, context: RunContext) -> dict[str, Any]:
    """全域安全閥（apxG_p03）：對外送出前必先通過一次 `--dry-run` 內部通訊測試。"""
    safety = context.config.get("safety") or {}
    needs = bool(safety.get("require_dry_run_preflight", True)) and (
        not context.is_mock or args.notify != "console"
    )
    if args.dry_run:
        stamp = context.now.isoformat()
        context.state["last_dry_run_preflight_at"] = stamp
        context.audit.record("preflight_passed", {"channel": args.notify, "at": stamp})
        return {"required": needs, "passed": True, "at": stamp}
    if not needs:
        return {"required": False, "passed": True, "at": context.state.get("last_dry_run_preflight_at")}
    stamp = context.state.get("last_dry_run_preflight_at")
    if not _preflight_is_valid(stamp, context.now, int(safety.get("preflight_valid_hours", 24))):
        context.audit.record("preflight_missing", {"channel": args.notify, "last": stamp})
        context.diagnostics.red(
            "對外通訊未通過 --dry-run 前置測試",
            "安全閥要求任何對外送出前先做一次內部通訊測試（憑證與收件設定可能已被改動）",
            "先執行 `python main.py --dry-run` 通過後再重跑",
        )
    return {"required": True, "passed": True, "at": stamp}


def _preflight_is_valid(stamp: Any, now: datetime, valid_hours: int) -> bool:
    """前置測試是否仍在有效期內。時間戳壞掉一律視為無效（寧可多跑一次）。"""
    if not isinstance(stamp, str) or not stamp:
        return False
    try:
        recorded = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if not recorded.tzinfo:
        recorded = recorded.replace(tzinfo=timezone.utc)
    return now - recorded <= timedelta(hours=valid_hours)


def load_applications(args: argparse.Namespace, context: RunContext) -> dict[str, Any]:
    """取得申請資料。mock 讀檔；live 需由 ATS Webhook 匯出後以 --applications 指定。"""
    explicit = args.applications or (context.config.get("mock") or {}).get("applications")
    path = _resolve_path(explicit) if explicit else None
    if path is None or not path.is_file():
        context.diagnostics.red(
            "取不到申請資料",
            f"找不到申請資料檔：{path if path else '（未指定）'}",
            "用 --applications 指定 ATS 匯出的 JSON，或確認 config 的 mock.applications",
        )
        return {"requisition": {}, "applications": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"申請資料不是合法 JSON：{path}｜{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("applications"), list):
        raise ValueError(f"申請資料格式錯誤，需為 {{'applications': [...]}}：{path}")
    return payload


def anonymise_batch(
    applications: list[dict[str, Any]], context: RunContext
) -> tuple[list[Any], IdentityVault]:
    """匿名化 + 完整性驗證。只要驗出任何殘留的受保護值就紅色警報停機。"""
    vault = IdentityVault()
    anonymised = anonymise_all(applications, context.config, vault)
    leaks = verify_batch(zip(anonymised, applications), context.config)
    context.audit.record(
        "anonymisation_completed",
        {
            "count": len(anonymised),
            "redactions": sum(item.redaction_count for item in anonymised),
            "leaks": len(leaks),
        },
    )
    if leaks:
        context.diagnostics.red(
            f"匿名化驗證失敗：{len(leaks)} 份履歷仍殘留受保護值",
            f"殘留樣本：{sorted(leaks)[:3]}",
            "檢查 anonymisation.free_text_fields 是否涵蓋所有自由文字欄位後重跑",
        )
    return anonymised, vault


def _read_prompt(config: dict[str, Any], key: str) -> str:
    """讀取提示詞檔（提示詞獨立成檔，不內嵌 .py，方便法遵單位單獨審閱）。"""
    raw = (config.get("prompts") or {}).get(key)
    if not raw:
        raise FileNotFoundError(f"config.prompts 缺少 {key}")
    path = _resolve_path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"找不到提示詞檔：{path}")
    return path.read_text(encoding="utf-8")


def _fixture(context: RunContext, key: str) -> Path | None:
    """mock 模式的模型輸出樣本；live 模式回 None（真的打 API）。"""
    if not context.is_mock:
        return None
    raw = ((context.config.get("mock") or {}).get("fixtures") or {}).get(key)
    return _resolve_path(raw) if raw else None


def draft_interview_questions(context: RunContext, score: CandidateScore) -> list[str]:
    """為影片面試產生客製化題目（apxG_p11：4 題）。模型只寫題目，不碰分數。"""
    settings = context.config.get("video_interview") or {}
    wanted = int(settings.get("question_count", 4))
    user = json.dumps(
        {"identifier": score.identifier, "strengths": list(score.strengths()), "gaps": list(score.gaps())},
        ensure_ascii=False,
    )
    text = context.llm.complete(
        system=_read_prompt(context.config, "interview_questions"),
        user=user,
        max_tokens=int((context.config.get("llm") or {}).get("max_tokens", 1200)),
        fixture=_fixture(context, "interview_questions"),
    )
    questions = [line.strip() for line in text.splitlines() if line.strip()]
    if len(questions) < wanted:
        context.diagnostics.amber(
            f"{score.identifier} 的面試題只生出 {len(questions)} 題（應為 {wanted} 題）",
            "檢查 prompts/interview_questions.md 的輸出格式要求",
        )
    return questions[:wanted]


def draft_rejection(context: RunContext, score: CandidateScore) -> str:
    """產生溫和拒絕信。優點一律來自結構化證據，模型不得虛構讚美。"""
    settings = context.config.get("rejection") or {}
    strengths = list(score.strengths())
    if len(strengths) < int(settings.get("mention_strengths_min", 1)):
        context.diagnostics.amber(
            f"{score.identifier} 沒有可引用的具體優點，拒絕信將不列舉",
            "這是誠實的結果，不要為了信件好看而放寬 keywords 命中門檻",
        )
    body = context.llm.complete(
        system=_read_prompt(context.config, "warm_rejection"),
        user=json.dumps({"identifier": score.identifier, "strengths": strengths}, ensure_ascii=False),
        max_tokens=int((context.config.get("llm") or {}).get("max_tokens", 1200)),
        fixture=_fixture(context, "warm_rejection"),
    )
    evidence = [f"- {item}" for item in strengths] or ["- （本次未取得可引用的具體證據，故不列舉，也不虛構）"]
    return "\n".join([body.strip(), "", "【我們實際看到的證據】", *evidence])


def schedule_rejections(context: RunContext, scores: list[CandidateScore], vault: IdentityVault) -> list[dict[str, Any]]:
    """把低分者排入 48 小時後的拒絕信佇列（apxG_p11：刻意延遲，避免秒拒傷雇主品牌）。"""
    delay = int((context.config.get("rejection") or {}).get("delay_hours", 48))
    pending: list[dict[str, Any]] = list(context.state.get("pending_rejections") or [])
    known = {str(item.get("identifier")) for item in pending}
    added: list[dict[str, Any]] = []
    for score in scores:
        if score.decision != DECISION_REJECT or score.identifier in known:
            continue
        entry = {
            "identifier": score.identifier,
            "ats_reference": vault.ats_reference(score.identifier),
            "send_after": (context.now + timedelta(hours=delay)).isoformat(),
            "body": draft_rejection(context, score),
        }
        pending.append(entry)
        added.append(entry)
        context.audit.record("rejection_scheduled", {"identifier": score.identifier, "send_after": entry["send_after"]})
    context.state["pending_rejections"] = pending
    return added


def dispatch_due_rejections(context: RunContext, is_dry_run: bool) -> list[dict[str, Any]]:
    """送出已到期的拒絕信。dry-run 只列出不送；未達自動送出權限則出草稿。"""
    address = str((context.config.get("runtime") or {}).get("ats_dispatch_address", ""))
    can_auto = context.gate.can_send(address)
    pending, dispatched = [], []
    for item in context.state.get("pending_rejections") or []:
        if is_dry_run or not _is_due(item, context.now):
            pending.append(item)
            continue
        prefix = "[已自動發送]" if can_auto else "[草稿待審]"
        delivered = context.notifier.send(str(item.get("body", "")), subject=f"{prefix} 招募結果 {item.get('ats_reference')}")
        context.audit.record(
            "rejection_sent" if can_auto else "rejection_drafted",
            {"identifier": item.get("identifier"), "delivered": delivered, "effective_level": context.gate.effective_level(address).value},
        )
        dispatched.append({**{k: v for k, v in item.items() if k != "body"}, "delivered": delivered, "auto_sent": can_auto})
    context.state["pending_rejections"] = pending
    return dispatched


def _is_due(entry: dict[str, Any], now: datetime) -> bool:
    """到期判定。時間戳壞掉一律視為未到期——寧可晚寄，不可誤寄。"""
    raw = entry.get("send_after")
    if not isinstance(raw, str):
        return False
    try:
        due = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if not due.tzinfo:
        due = due.replace(tzinfo=timezone.utc)
    return due <= now


def build_candidate_rows(
    context: RunContext, scores: list[CandidateScore], invited: tuple[str, ...]
) -> list[dict[str, Any]]:
    """組出每位候選人的處置結果（只帶匿名識別碼，絕不帶姓名）。"""
    rows: list[dict[str, Any]] = []
    for score in rank(scores):
        row = score.to_dict()
        row["invited_to_video_interview"] = score.identifier in invited
        if row["invited_to_video_interview"]:
            row["interview_questions"] = draft_interview_questions(context, score)
        rows.append(row)
    return rows


def render_report(
    context: RunContext, requisition: dict[str, Any], rows: list[dict[str, Any]], scores: list[CandidateScore], invited: tuple[str, ...]
) -> str:
    """組出給招募經理的短名單報告。依鐵律 3，本文只出現匿名識別碼。"""
    module = context.config.get("module") or {}
    counts = _decision_counts(scores)
    lines = [
        f"📋 {module.get('name', MODULE_NAME)}｜職缺 {requisition.get('title', '（未指定）')}"
        f"（{requisition.get('id', 'N/A')}）",
        "",
        f"收到申請 {len(scores)} 份｜短名單 {counts[DECISION_SHORTLIST]}｜保留待複核 {counts[DECISION_HOLD]}"
        f"｜延遲拒絕 {counts[DECISION_REJECT]}｜不符合 {counts[DECISION_DISQUALIFIED]}",
        "",
        "【短名單（僅匿名識別碼，身分揭露需招募經理具名核准）】",
    ]
    lines.extend(format_shortlist(scores, invited) or ["（本批無人達到短名單門檻）"])
    lines.extend(["", "【評分依據】", f"條件矩陣指紋：{criteria_fingerprint(context.config)}"])
    lines.extend(_render_evidence(rows))
    lines.extend(["", "【身分揭露】", "confirm → `python main.py --reveal <識別碼> --approved-by \"<招募經理姓名>\"`"])
    return "\n".join(lines)


def _decision_counts(scores: list[CandidateScore]) -> dict[str, int]:
    """各處置分支的件數。"""
    keys = (DECISION_SHORTLIST, DECISION_HOLD, DECISION_REJECT, DECISION_DISQUALIFIED)
    return {key: sum(1 for item in scores if item.decision == key) for key in keys}


def _render_evidence(rows: list[dict[str, Any]]) -> list[str]:
    """短名單成員的命中證據摘要（供招募經理逐項覆核）。"""
    lines: list[str] = []
    for row in rows:
        if row["decision"] != DECISION_SHORTLIST:
            continue
        lines.append(f"- {row['identifier']}：命中 {'、'.join(row['strengths']) or '（無）'}")
        if row["gaps"]:
            lines.append(f"  缺口：{'、'.join(row['gaps'])}")
    return lines


def handle_reveal(
    args: argparse.Namespace, context: RunContext, vault: IdentityVault, scores: list[CandidateScore]
) -> dict[str, Any] | None:
    """鐵律 4：身分揭露是獨立的人工動作，必須具名核准且必然留下稽核紀錄。"""
    if not args.reveal:
        return None
    identifier = str(args.reveal).strip()
    score = next((item for item in scores if item.identifier == identifier), None)
    if score is None:
        raise ValueError(f"本批次沒有識別碼 {identifier}")
    if score.decision != DECISION_SHORTLIST:
        raise ValueError(f"{identifier} 未進入短名單，不得揭露身分（決策：{score.decision}）")
    try:
        record = vault.reveal(identifier, str(args.approved_by or ""), str(args.reveal_reason or ""))
    except RevealNotAuthorisedError as exc:
        context.audit.record("identity_reveal_denied", {"identifier": identifier, "reason": str(exc)})
        raise
    detail = {
        "identifier": identifier,
        "approved_by": record["_reveal_approved_by"],
        "reason": record["_reveal_reason"],
        "ats_reference": record.get("application_id"),
        "at": context.now.isoformat(),
    }
    context.audit.record("identity_revealed", detail, actor=record["_reveal_approved_by"])
    context.state.setdefault("revealed", {})[identifier] = detail
    return {**detail, "name": record.get("name")}


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    context = build_context(args)
    guard_bias_switches(context)
    preflight_result = preflight(args, context)
    payload = load_applications(args, context)
    applications = list(payload.get("applications") or [])
    anonymised, vault = anonymise_batch(applications, context)
    scores = score_all(anonymised, context.config)
    invited = select_video_interviews(scores, len(applications), context.config)
    rows = build_candidate_rows(context, scores, invited)
    schedule_rejections(context, scores, vault)
    dispatched = dispatch_due_rejections(context, bool(args.dry_run))
    report = render_report(context, payload.get("requisition") or {}, rows, scores, invited)
    delivered = _deliver_report(args, context, report)
    reveal = handle_reveal(args, context, vault, scores)
    return _finalise(args, context, payload, rows, invited, dispatched, report, delivered, reveal, preflight_result)


def _deliver_report(args: argparse.Namespace, context: RunContext, report: str) -> bool:
    """把短名單報告送給招募經理。dry-run 只印出，不送。"""
    if args.dry_run:
        print(report)
        return False
    recipient = str((context.config.get("runtime") or {}).get("report_recipient", ""))
    prefix = "[已自動發送]" if context.gate.can_send(recipient) else "[草稿待審]"
    return context.notifier.send(report, subject=f"{prefix} 招募短名單")


def _finalise(
    args: argparse.Namespace,
    context: RunContext,
    payload: dict[str, Any],
    rows: list[dict[str, Any]],
    invited: tuple[str, ...],
    dispatched: list[dict[str, Any]],
    report: str,
    delivered: bool,
    reveal: dict[str, Any] | None,
    preflight_result: dict[str, Any],
) -> dict[str, Any]:
    """收尾：寫回狀態、記錄稽核、組出回傳 dict。"""
    context.state["last_run_at"] = context.now.isoformat()
    context.state["processed_identifiers"] = [row["identifier"] for row in rows]
    context.audit.record(
        "screening_completed",
        {"received": len(rows), "invited": list(invited), "fingerprint": criteria_fingerprint(context.config)},
    )
    save_state(context.state_path, context.state)
    context.diagnostics.green(f"已篩選 {len(rows)} 份申請，短名單 {sum(1 for r in rows if r['decision'] == DECISION_SHORTLIST)} 人")
    module = context.config.get("module") or {}
    return {
        "module": module,
        "module_id": str(module.get("id", "24")),
        "module_name": str(module.get("name", MODULE_NAME)),
        "mode": "mock" if args.mock else "live",
        "dry_run": bool(args.dry_run),
        "notify_channel": args.notify,
        "requisition": payload.get("requisition") or {},
        "received_count": len(rows),
        "bias_mitigation": dict(context.config.get("bias_mitigation") or {}),
        "criteria_fingerprint": criteria_fingerprint(context.config),
        "candidates": rows,
        "shortlist": [row["identifier"] for row in rows if row["decision"] == DECISION_SHORTLIST],
        "invited": list(invited),
        "pending_rejections": list(context.state.get("pending_rejections") or []),
        "dispatched_rejections": dispatched,
        "reveal": reveal,
        "preflight": preflight_result,
        "report": report,
        "delivered": delivered,
        "state_file": str(context.state_path),
        "audit_file": str(context.audit.path),
        "audit_chain_problems": context.audit.verify_chain(),
        "warnings": list(context.gate.warnings),
        "amber_count": context.diagnostics.amber_count,
    }


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (LLMError, AnonymisationError, RevealNotAuthorisedError, FileNotFoundError, ValueError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    if args.json_out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        f"\n完成：{result['received_count']} 份申請｜短名單 {len(result['shortlist'])} 人"
        f"｜影片面試 {len(result['invited'])} 人｜待發拒絕信 {len(result['pending_rejections'])} 封"
        f"｜amber {result['amber_count']} 則"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
