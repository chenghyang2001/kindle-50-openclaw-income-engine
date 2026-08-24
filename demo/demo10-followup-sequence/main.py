"""模組 #10 — 客戶跟進序列自動化（主流程）。

書中數據：部署 <90 分鐘｜回收 12 hrs/mo（$960）｜售價 $350 setup + $90/mo
核心價值：把「提案寄出 → 忘記跟進 → 客戶被競爭對手接走」的漏水桶補起來，
轉換率從 18% 拉到 25%+。

流程：
    CRM 階段變成 proposal_sent
        → Day 3  輕度確認
        → Day 7  提供相關案例研究
        → Day 14 最終確認
    任何一步之前，只要客戶已回覆，整個序列立即中止。

安全設計：
    1. `stop_on_reply` 是不可停用的硬規則。config 改成 false 也會被強制覆寫
       為 true，並發出 Diagnostics AMBER 警告。
    2. 回覆檢查跑兩次：排程判定時一次，**每一封信實際送出前再一次**。
    3. 自主權預設 DRAFT。SUPERVISED_AUTO 必須配白名單，未命中一律降級為草稿。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, tzinfo
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402
from sequencer import (  # noqa: E402
    ACTION_SEND,
    FollowUpSequencer,
    SequenceError,
    SequenceHalted,
    SequenceState,
    build_steps,
    parse_iso,
    resolve_timezone,
)

MODULE_LABEL = "demo10-followup-sequence"
LIVE_REQUIRED_ENV: tuple[str, ...] = ()

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出
CONTEXT_NOTE = (
    "這是 B2B 提案後的跟進信。收件人是已經看過提案的決策者，"
    "不要重複介紹公司，不要用行銷術語，不要施壓。目標是讓對方願意回一句話。"
)

# 草稿摘要預覽寬度：讓操作者不用開檔案就能大致判斷信件內容是否需要修改
_SUMMARY_PREVIEW_WIDTH = 40
_TRUNCATION_SUFFIX = "…"


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6）。"""
    parser = argparse.ArgumentParser(
        prog="demo10-followup-sequence",
        description="客戶跟進序列自動化：Day 3 / Day 7 / Day 14，客戶一回覆即中止",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實 Anthropic API")
    parser.add_argument("--dry-run", action="store_true", help="跑完流程但不實際發送")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="通知管道，預設 console",
    )
    parser.add_argument(
        "--config",
        default=str(MODULE_DIR / "config.yaml"),
        help="設定檔路徑，預設同目錄 config.yaml",
    )
    return parser


def _resolve_path(value: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，杜絕硬編碼使用者路徑。"""
    path = Path(value)
    return path if path.is_absolute() else MODULE_DIR / path


def _load_json(value: str | Path, expected: str) -> list:
    """讀取 mock JSON 清單，格式錯誤要明確報錯。"""
    path = _resolve_path(value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SequenceError(f"{expected} 無法讀取或解析：{path}") from exc
    if not isinstance(payload, list):
        raise SequenceError(f"{expected} 應為 JSON 陣列：{path}")
    return payload


def _require_live_env(diagnostics: Diagnostics) -> None:
    """--live 缺憑證要明確報錯退出，絕不靜默降級回 mock。"""
    missing = [key for key in LIVE_REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="金鑰未設定或未載入目前的 shell session",
            fix=f"設定 {', '.join(missing)} 後重跑，或改用 --mock 離線驗證",
        )


def _enforce_stop_on_reply(safety: dict, diagnostics: Diagnostics) -> list[str]:
    """強制 stop_on_reply=True，設定被改掉就發 AMBER 警告。"""
    warnings: list[str] = []
    raw = safety.get("stop_on_reply", True)
    if raw is not True:
        message = (
            f"config.safety.stop_on_reply 被設為 {raw!r}，已強制覆寫為 true"
        )
        warnings.append(message)
        diagnostics.amber(
            symptom=message,
            fix="本模組不允許停用回覆中止；請把 config.yaml 改回 stop_on_reply: true",
        )
    return warnings


def _build_sequencer(
    config: dict,
    diagnostics: Diagnostics,
    persist: bool,
) -> tuple[FollowUpSequencer, tzinfo, list[str]]:
    """依設定組出序列狀態機，回傳 (sequencer, 時區, 警告清單)。"""
    safety = config.get("safety") or {}
    warnings = _enforce_stop_on_reply(safety, diagnostics)
    tz, tz_warning = resolve_timezone(
        safety.get("timezone", "Asia/Taipei"),
        int(safety.get("timezone_fallback_offset_hours", 8)),
    )
    if tz_warning:
        warnings.append(tz_warning)
        diagnostics.amber(symptom=tz_warning, fix="安裝 tzdata 套件或改用固定偏移設定")
    sequencer = FollowUpSequencer(
        steps=build_steps(config.get("sequence")),
        tz=tz,
        stop_on_reply=safety.get("stop_on_reply", True),
        state=SequenceState(persist=persist),
        active_stage=(config.get("crm") or {}).get("active_stage", "proposal_sent"),
    )
    warnings.extend(sequencer.forced_overrides)
    return sequencer, tz, warnings


def _build_gate(config: dict, diagnostics: Diagnostics) -> tuple[AutonomyGate, list[str]]:
    """建立自主權閘門；設定違規時降級為 DRAFT 而非讓程式崩潰。"""
    runtime = config.get("runtime") or {}
    raw_level = str(runtime.get("autonomy") or "draft").strip().lower()
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        diagnostics.amber(
            symptom=f"未知的 autonomy 設定 {raw_level!r}",
            fix="改用 read_only / draft / supervised_auto 其中之一；本次已降級為 draft",
        )
        level = AutonomyLevel.DRAFT
    gate = AutonomyGate(
        level=level,
        approved_senders=list(runtime.get("approved_senders") or []),
        days_in_draft=int(runtime.get("days_in_draft") or 0),
    )
    for warning in gate.warnings:
        diagnostics.amber(symptom=warning, fix="滿 14 天並取得客戶簽核後再開全自動")
    return gate, list(gate.warnings)


def _resolve_now(config: dict, tz: tzinfo, is_mock: bool) -> datetime:
    """決定「現在」：mock 用設定的基準時間，確保結果可重現。"""
    if not is_mock:
        return datetime.now(tz)
    raw = str((config.get("mock") or {}).get("today") or "").strip()
    return parse_iso(raw, tz) if raw else datetime.now(tz)


def _pick_case_study(prospect: dict, case_studies: list) -> dict | None:
    """依產業挑選最相關的案例研究；沒有命中就退回第一篇。"""
    if not case_studies:
        return None
    industry = str(prospect.get("industry") or "").strip()
    for case in case_studies:
        industries = [str(item).strip() for item in (case.get("industries") or [])]
        if industry and industry in industries:
            return case
    return case_studies[0]


def _read_prompt(relative: str) -> str:
    """讀取提示詞檔（提示詞是資產，一律獨立成 .md，不內嵌在 .py）。"""
    path = _resolve_path(relative)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SequenceError(f"提示詞檔無法讀取：{path}") from exc


def _compose_message(
    llm: LLMClient,
    prospect: dict,
    step: dict,
    case_study: dict | None,
) -> str:
    """呼叫 LLM 產生這一段跟進的信件內容。"""
    system = _read_prompt(str(step["prompt"]))
    payload = {
        "prospect": {
            "name": prospect.get("name"),
            "company": prospect.get("company"),
            "industry": prospect.get("industry"),
            "pain_point": prospect.get("pain_point"),
            "proposal_value_usd": prospect.get("proposal_value_usd"),
            "proposal_sent_at": prospect.get("proposal_sent_at"),
        },
        "step": {"day": step.get("day"), "type": step.get("type")},
        "case_study": case_study,
    }
    user = json.dumps(payload, ensure_ascii=False, indent=2)
    return llm.complete(system=system, user=user, max_tokens=800)


def _record(
    decision: dict,
    prospect: dict,
    body: str,
    autonomy: AutonomyLevel,
    case_study: dict | None,
) -> dict:
    """把一次成功產出的跟進整理成回報用的 dict。"""
    return {
        "prospect_id": decision["prospect_id"],
        "name": decision["name"],
        "company": decision["company"],
        "email": str(prospect.get("email") or ""),
        "step_day": decision["step"]["day"],
        "step_type": decision["step"]["type"],
        "due_at": decision["due_at"],
        "autonomy": autonomy.value,
        "case_study_id": (case_study or {}).get("id"),
        "body": body,
    }


def _halt_entry(decision: dict) -> dict:
    """把中止決策整理成回報用的 dict。"""
    return {
        "prospect_id": decision["prospect_id"],
        "name": decision["name"],
        "company": decision["company"],
        "reason": decision["reason"],
        "detail": decision["detail"],
    }


def _process_one(
    decision: dict,
    prospect: dict,
    context: dict,
) -> tuple[str, dict]:
    """處理單一「該發送」決策，回傳 (bucket, 紀錄)。

    bucket 為 "sent" / "drafted" / "halted"。
    """
    sequencer: FollowUpSequencer = context["sequencer"]
    try:
        # 第二道閘門：實際送出前再查一次是否已回覆
        sequencer.assert_can_send(prospect)
    except SequenceHalted as exc:
        return "halted", {
            "prospect_id": exc.prospect_id,
            "name": decision["name"],
            "company": decision["company"],
            "reason": exc.reason,
            "detail": exc.detail,
        }
    case_study = None
    if decision["step"]["type"] == "case_study":
        case_study = _pick_case_study(prospect, context["case_studies"])
    body = _compose_message(context["llm"], prospect, decision["step"], case_study)
    email = str(prospect.get("email") or "")
    gate: AutonomyGate = context["gate"]
    level = gate.effective_level(email)
    can_send = gate.can_send(email) and not context["dry_run"]
    entry = _record(decision, prospect, body, level, case_study)
    if can_send:
        context["notifier"].send(text=body, subject=_subject(decision, prospect))
        sequencer.mark_sent(prospect, _step_obj(sequencer, decision))
        return "sent", entry
    return "drafted", entry


def _step_obj(sequencer: FollowUpSequencer, decision: dict):
    """由決策中的 day 取回對應的 SequenceStep 物件。"""
    day = int(decision["step"]["day"])
    for step in sequencer.steps:
        if step.day == day:
            return step
    raise SequenceError(f"決策指向不存在的 day：{day}")


def _subject(decision: dict, prospect: dict) -> str:
    """組出信件主旨。"""
    company = decision["company"] or prospect.get("company") or ""
    return f"[跟進 Day {decision['step']['day']}] {company} — {decision['step']['type']}"


def _first_line(text: str, width: int = _SUMMARY_PREVIEW_WIDTH) -> str:
    """取信件內容第一行的前 width 字元，用於草稿摘要預覽。"""
    stripped = (text or "").strip()
    head = stripped.splitlines()[0] if stripped else ""
    return head if len(head) <= width else head[:width] + _TRUNCATION_SUFFIX


def _summarise(result: dict) -> str:
    """組出給操作者的摘要文字。"""
    lines = [
        f"【{result['module_name']}】{result['reference_now']}（{result['mode']} 模式）",
        f"潛在客戶 {result['total_prospects']} 位｜自動送出 {len(result['sent'])}"
        f"｜待審草稿 {len(result['drafted'])}｜中止 {len(result['halted'])}",
        f"stop_on_reply：{'啟用（不可停用）' if result['stop_on_reply'] else '異常'}",
    ]
    for item in result["drafted"]:
        lines.append(
            f"  [草稿] {item['name']}（{item['company']}）Day {item['step_day']}"
            f"｜{len(item['body'])} 字元｜{_first_line(item['body'])}"
        )
    for item in result["sent"]:
        lines.append(f"  [已送] {item['name']}（{item['company']}）Day {item['step_day']}")
    for item in result["halted"]:
        lines.append(f"  [中止] {item['name']}（{item['company']}）— {item['reason']}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config = load_config(_resolve_path(args.config))
    diagnostics = Diagnostics(MODULE_LABEL)
    is_mock = not args.live
    if not is_mock:
        _require_live_env(diagnostics)
    persist = bool(args.live) and not args.dry_run
    sequencer, tz, warnings = _build_sequencer(config, diagnostics, persist)
    gate, gate_warnings = _build_gate(config, diagnostics)
    warnings.extend(gate_warnings)
    mock_cfg = config.get("mock") or {}
    prospects = _load_json(mock_cfg.get("prospects", "mock/prospects.json"), "prospects")
    case_studies = _load_json(
        mock_cfg.get("case_studies", "mock/case_studies.json"), "case_studies"
    )
    context = {
        "sequencer": sequencer,
        "gate": gate,
        "llm": LLMClient(mock=is_mock, context_note=CONTEXT_NOTE),
        "notifier": Notifier(channel=args.notify),
        "case_studies": case_studies,
        "dry_run": bool(args.dry_run),
    }
    now = _resolve_now(config, tz, is_mock)
    buckets = _run_sequence(prospects, now, context)
    return _build_result(config, args, now, prospects, buckets, warnings, diagnostics)


def _run_sequence(prospects: list, now: datetime, context: dict) -> dict[str, list]:
    """跑完整個序列判定 + 產文，回傳三個 bucket。"""
    buckets: dict[str, list] = {"sent": [], "drafted": [], "halted": []}
    sequencer: FollowUpSequencer = context["sequencer"]
    index = {str(item.get("id") or ""): item for item in prospects}
    for decision in sequencer.plan(prospects, now):
        if decision["action"] != ACTION_SEND:
            buckets["halted"].append(_halt_entry(decision))
            continue
        prospect = index[decision["prospect_id"]]
        bucket, entry = _process_one(decision, prospect, context)
        buckets[bucket].append(entry)
    return buckets


def _build_result(
    config: dict,
    args: argparse.Namespace,
    now: datetime,
    prospects: list,
    buckets: dict[str, list],
    warnings: list[str],
    diagnostics: Diagnostics,
) -> dict:
    """組出統一的回傳結構。"""
    module = config.get("module") or {}
    metrics = config.get("metrics") or {}
    return {
        "module_id": str(module.get("id", "10")),
        "module_name": str(module.get("name", "客戶跟進序列自動化")),
        "mode": "mock" if not args.live else "live",
        "dry_run": bool(args.dry_run),
        "notify_channel": args.notify,
        "timezone": str((config.get("safety") or {}).get("timezone", "")),
        "reference_now": now.isoformat(),
        "stop_on_reply": True,
        "total_prospects": len(prospects),
        "sent": buckets["sent"],
        "drafted": buckets["drafted"],
        "halted": buckets["halted"],
        "warnings": warnings,
        "amber_count": diagnostics.amber_count,
        "baseline_conversion_rate": metrics.get("baseline_conversion_rate"),
        "target_conversion_rate": metrics.get("target_conversion_rate"),
    }


def main() -> int:
    """解析參數 -> run() -> 印出/發送結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (SequenceError, FileNotFoundError, OSError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    summary = _summarise(result)
    print(summary)
    # console 管道等同上面的 print，再送一次只會讓輸出重複
    if not args.dry_run and args.notify != "console":
        Notifier(channel=args.notify).send(text=summary, subject="跟進序列執行摘要")
    return 0


if __name__ == "__main__":
    sys.exit(main())
