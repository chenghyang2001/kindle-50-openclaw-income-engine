"""demo16 — CRM 數據豐富化與評分（模組 #16）。

每晚 2AM 掃描 CRM：把缺漏欄位透過外部資料源補齊、算出 ICP 分數並排序、
撈出 90 天沒人碰的高分機會，最後產出一份 CSV 報告與變更計畫。

**這個模組的靈魂是「不覆蓋」**：外部查不到就保留 CRM 原值並標
``enrichment_failed``；外部值與 CRM 不一致就保留 CRM 值、把外部值送人工審查。
書中的痛點是「充滿過期或錯誤數據的 CRM 比沒有 CRM 更糟」——
一個會自動覆蓋的豐富化代理人，是製造那種 CRM 最快的方法。

用法：

    python main.py --mock                       # 零憑證、零網路跑完
    python main.py --mock --dry-run             # 只印變更計畫，不寫任何檔案
    python main.py --mock --notify telegram     # 推到 Telegram
    python main.py --mock --state-file /tmp/s.json --csv-out /tmp/r.csv
    python main.py --live                       # 串真實 API（缺憑證會明確報錯退出）
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

MODULE_DIR = Path(__file__).resolve().parent
# demo/ 進 sys.path 才能匯入 _shared；demo16 自己也要進，
# 這樣 pytest 從別的目錄呼叫時仍找得到 enricher / scorer。
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient, LLMError  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

from enricher import (  # noqa: E402
    CSV_COLUMNS,
    EnrichedContact,
    ProviderFailure,
    collect_providers,
    enrich_all,
    partial_banner,
    render_change_plan,
    to_csv_rows,
)
from scorer import (  # noqa: E402
    ScoreResult,
    days_between,
    load_scoring_rules,
    rank,
    score_all,
)

MODULE_NAME = "demo16-crm-enrichment"

#: 第 04 章：附在 system prompt 尾端可減少約 40% 不相關輸出。
CONTEXT_NOTE = (
    "這是每天早上寄給業務主管的 CRM 豐富化報表，讀者不是工程師。"
    "只陳述輸入 JSON 中實際存在的欄位，查無資料與衝突一律據實說明，"
    "不得推估、補值或用外部值重新推算分數。"
)

#: 提示詞檔讀不到時的最低限度後備。刻意保留而不是直接失敗——
#: 分數表本身已經有價值，不該因為少一段 AI 敘述就讓業務今天收不到名單。
FALLBACK_SUMMARY_PROMPT = (
    "你是營運分析師。用繁體中文 180-300 字摘要以下 CRM 豐富化與評分結果，"
    "查無資料與衝突必須據實說明，不得推估或補值。"
)
FALLBACK_CONFLICT_PROMPT = (
    "你是資料治理助理。用繁體中文說明以下資料衝突，每筆給一個查證動作，"
    "不得判定哪一方正確。"
)

APPLY_DRY_RUN = "dry-run"
APPLY_MOCK = "mock"
APPLY_DRAFT = "autonomy_draft"
APPLY_WRITTEN = "written"


# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（契約 §6 的統一介面）。"""
    parser = argparse.ArgumentParser(
        prog="demo16-crm-enrichment",
        description="CRM 數據豐富化與 ICP 評分：補缺不覆蓋、衝突轉人工、分數全由設定檔決定。",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mock", dest="mock", action="store_true", default=True,
        help="離線模式，讀 mock/*.json、不呼叫任何 API（預設）",
    )
    mode.add_argument(
        "--live", dest="mock", action="store_false",
        help="串接真實 API；缺憑證會明確報錯退出，不會靜默退回 mock",
    )
    parser.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="跑完流程並印出「哪些欄位、從什麼值變成什麼值」，但不寫入任何檔案或 CRM",
    )
    parser.add_argument(
        "--notify", choices=list(Notifier.SUPPORTED), default=None,
        help="發送通道；未指定時取 config 的 runtime.notify_channel（預設 console）",
    )
    parser.add_argument(
        "--config", default=str(MODULE_DIR / "config.yaml"),
        help="設定檔路徑（預設為本目錄的 config.yaml）",
    )
    parser.add_argument(
        "--state-file", dest="state_file", default=None,
        help="豐富化狀態檔路徑（記錄每人上次豐富化時間，供 refresh_days 判斷）",
    )
    parser.add_argument(
        "--csv-out", dest="csv_out", default=None,
        help="CSV 報告輸出路徑（預設取 config 的 output.csv_file）",
    )
    return parser


# --------------------------------------------------------------------------
# 設定與前置檢查
# --------------------------------------------------------------------------


def ensure_live_env(config: dict[str, Any], diagnostics: Diagnostics) -> None:
    """`--live` 時檢查必要環境變數；缺任何一個都走紅色警報退出。"""
    required = (config.get("live") or {}).get("required_env") or []
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="憑證未設定或未匯入目前的 shell",
            fix=f"設定 {', '.join(missing)} 後重跑；或改用 --mock 離線驗證流程",
        )


def build_gate(runtime_cfg: dict[str, Any], diagnostics: Diagnostics) -> AutonomyGate:
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


def resolve_now(schedule_cfg: dict[str, Any], diagnostics: Diagnostics) -> datetime:
    """決定整次執行的「現在」。設了 reference_date 就用它，讓示範結果可重現。"""
    raw = schedule_cfg.get("reference_date")
    if not raw:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        diagnostics.amber(
            f"schedule.reference_date 格式錯誤（{raw!r}），本次改用系統時間",
            "格式須為 YYYY-MM-DD 或完整 ISO 8601 時間戳",
        )
        return datetime.now(timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def resolve_path(cli_value: str | None, config_value: Any, fallback: str) -> Path:
    """CLI 覆寫優先；沒給才用 config 的相對路徑（以模組目錄為基準）。"""
    if cli_value:
        return Path(cli_value).expanduser().resolve()
    return (MODULE_DIR / str(config_value or fallback)).resolve()


# --------------------------------------------------------------------------
# 讀取與狀態
# --------------------------------------------------------------------------


def load_contacts(path: Path) -> list[dict[str, Any]]:
    """讀 CRM 聯絡人快照。讀不到或格式錯就拋錯——沒有名單就沒有這個模組。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"找不到 CRM 聯絡人檔：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"CRM 聯絡人檔 JSON 解析失敗 {path}：{exc}") from exc

    contacts = payload.get("contacts") if isinstance(payload, dict) else None
    if not isinstance(contacts, list):
        raise ValueError(f"CRM 聯絡人檔缺少 contacts 陣列：{path}")
    return [item for item in contacts if isinstance(item, dict)]


def load_state(path: Path, diagnostics: Diagnostics) -> dict[str, Any]:
    """讀豐富化狀態檔。不存在是正常的（第一次執行）；損毀則記琥珀燈後從空的開始。"""
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.amber(
            f"狀態檔無法讀取或已損毀（{path}）：{exc}",
            "本次視為首次執行；若持續發生請刪除該檔重建",
        )
        return {}
    contacts = payload.get("contacts") if isinstance(payload, dict) else None
    return contacts if isinstance(contacts, dict) else {}


def save_state(
    path: Path, state: dict[str, Any], records: Sequence[EnrichedContact], enriched_at: str
) -> Path:
    """把本次處理過的聯絡人寫回狀態檔（保留未處理者的舊紀錄）。"""
    merged = dict(state)
    for record in records:
        merged[record.contact_id] = {
            "last_enriched_at": enriched_at,
            "status": record.status,
            "filled_fields": [item.field_name for item in record.filled],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "updated_at": enriched_at, "contacts": merged}
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def select_contacts(
    contacts: Sequence[dict[str, Any]],
    state: dict[str, Any],
    refresh_days: int,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """挑出本次要處理的聯絡人；近期已豐富化過的跳過，省下外部 API 額度。

    以「狀態檔」與「CRM 欄位」兩者較新的時間為準：客戶可能在別的工具裡
    也做過豐富化，只信自己的狀態檔會重複消耗額度。
    """
    targets: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for contact in contacts:
        contact_id = str(contact.get("contact_id", ""))
        stamps = [
            contact.get("last_enriched_at"),
            (state.get(contact_id) or {}).get("last_enriched_at"),
        ]
        ages = [days for days in (days_between(item, now) for item in stamps) if days is not None]
        freshest = min(ages) if ages else None
        if freshest is not None and freshest < refresh_days:
            skipped.append(
                {
                    "contact_id": contact_id,
                    "company": str(contact.get("company", "")),
                    "reason": f"{freshest} 天前才豐富化過（refresh_days={refresh_days}）",
                }
            )
        else:
            targets.append(contact)
    return targets, skipped


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> Path:
    """輸出豐富化 CSV 報告（書中 Output 明列的交付物）。

    用 utf-8-sig：客戶多半直接用 Excel 開，沒有 BOM 的話中文欄位會變亂碼，
    然後他們會認定「這份報表壞掉了」而不是「編碼設定問題」。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------
# LLM 敘述
# --------------------------------------------------------------------------


def load_prompt(config: dict[str, Any], key: str, fallback: str, diagnostics: Diagnostics) -> str:
    """讀 prompts/*.md；讀不到就用後備提示詞並記琥珀燈。"""
    rel = (config.get("prompts") or {}).get(key)
    if not rel:
        return fallback
    path = MODULE_DIR / str(rel)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        diagnostics.amber(
            f"讀不到提示詞檔 {path}，改用後備提示詞：{exc}",
            f"確認 prompts/{key}.md 是否隨部署一起複製過去",
        )
        return fallback


def write_narrative(
    payload: dict[str, Any], config: dict[str, Any], is_mock: bool, diagnostics: Diagnostics
) -> str:
    """呼叫 LLM 把分數表寫成敘述。mock 模式回傳佔位字串，零成本。"""
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    return client.complete(
        system=load_prompt(config, "summary", FALLBACK_SUMMARY_PROMPT, diagnostics),
        user=json.dumps(payload, ensure_ascii=False, indent=2),
        max_tokens=800,
    )


def review_conflicts(
    records: Sequence[EnrichedContact],
    config: dict[str, Any],
    is_mock: bool,
    diagnostics: Diagnostics,
) -> str:
    """有衝突才多花一次 LLM 呼叫；沒有衝突時回空字串（不為了排版而燒 token）。"""
    items = [
        {
            "contact_id": record.contact_id,
            "company": record.company,
            **{k: v for k, v in decision.to_dict().items() if k != "note"},
        }
        for record in records
        for decision in record.conflicts
    ]
    if not items:
        return ""
    client = LLMClient(mock=is_mock, context_note=CONTEXT_NOTE)
    return client.complete(
        system=load_prompt(config, "conflict_review", FALLBACK_CONFLICT_PROMPT, diagnostics),
        user=json.dumps(items, ensure_ascii=False, indent=2),
        max_tokens=800,
    )


# --------------------------------------------------------------------------
# 彙整與排版
# --------------------------------------------------------------------------


def build_payload(
    records: Sequence[EnrichedContact],
    scores: dict[str, ScoreResult],
    failures: Sequence[ProviderFailure],
    skipped: Sequence[dict[str, str]],
    scanned: int,
) -> dict[str, Any]:
    """把結果整理成給 LLM 與回傳 dict 共用的 JSON-safe 結構。"""
    bands: dict[str, int] = {}
    for score in scores.values():
        bands[score.band] = bands.get(score.band, 0) + 1
    return {
        "run": {
            "scanned": scanned,
            "processed": len(records),
            "skipped": len(skipped),
            "failed_providers": [item.display_name for item in failures],
        },
        "totals": {
            "fields_filled": sum(len(item.filled) for item in records),
            "conflicts": sum(len(item.conflicts) for item in records),
            "enrichment_failed": sum(1 for item in records if item.is_failed),
            "reengagement_targets": sum(1 for s in scores.values() if s.is_reengagement_target),
        },
        "bands": bands,
        "contacts": [
            {**record.to_dict(), "score": _score_summary(scores.get(record.contact_id))}
            for record in records
        ],
    }


def _score_summary(score: ScoreResult | None) -> dict[str, Any] | None:
    """報表用的精簡分數摘要（完整 breakdown 只放在回傳 dict，不餵給 LLM）。"""
    if score is None:
        return None
    payload = score.to_dict()
    payload.pop("breakdown", None)
    return payload


def _ranked_lines(records: Sequence[EnrichedContact], scores: dict[str, ScoreResult]) -> list[str]:
    """依分數排序的名單；狀態異常的用符號標出來，不從名單上消失。"""
    lookup = {record.contact_id: record for record in records}
    lines = ["今日聯絡順序（分數高者優先）"]
    for score in rank(list(scores.values())):
        record = lookup.get(score.contact_id)
        flags = []
        if score.is_reengagement_target:
            flags.append(f"🔔 {score.days_since_contact} 天未聯絡")
        if record is not None and record.is_failed:
            flags.append("❓ 外部查無資料")
        if record is not None and record.conflicts:
            flags.append(f"⚠️ {len(record.conflicts)} 個欄位衝突待審")
        suffix = "｜".join(flags)
        lines.append(
            f"  {score.total:>5} 分 [{score.grade:<5}] {score.company}"
            f"{'｜' + suffix if suffix else ''}"
        )
    return lines


def render_report_text(
    payload: dict[str, Any],
    records: Sequence[EnrichedContact],
    scores: dict[str, ScoreResult],
    failures: Sequence[ProviderFailure],
    narrative: str,
    conflict_note: str,
    plan: str,
    is_dry_run: bool,
) -> str:
    """把結果排成可直接發送的純文字報表。"""
    run, totals = payload["run"], payload["totals"]
    lines = [f"🗂 CRM 豐富化與評分｜{datetime.now().strftime('%Y-%m-%d')}"]

    banner = partial_banner(failures)
    if banner:
        # 橫幅放最上方：讀者在看到任何分數之前就要知道這批分數的資料不完整。
        lines.append(banner)
        lines.append("（下列分數以不完整的資料計算，請勿據此清洗名單）")

    lines.append("─" * 34)
    lines.append(
        f"掃描 {run['scanned']} 筆｜處理 {run['processed']} 筆｜"
        f"跳過 {run['skipped']} 筆（近期已豐富化）"
    )
    lines.append(
        f"補齊欄位 {totals['fields_filled']} 個｜衝突待審 {totals['conflicts']} 個｜"
        f"外部查無資料 {totals['enrichment_failed']} 人｜沉睡機會 {totals['reengagement_targets']} 人"
    )
    lines.append("")
    lines.extend(_ranked_lines(records, scores))
    lines.append("")
    lines.append(plan if is_dry_run else plan.replace("（尚未寫入 CRM）", "（本次處置）"))

    if conflict_note:
        lines.extend(["", "衝突審查建議", f"  {conflict_note}"])
    lines.extend(["", "AI 敘述摘要", f"  {narrative}"])
    return "\n".join(lines)


def build_subject(payload: dict[str, Any], failures: Sequence[ProviderFailure]) -> str:
    """通知主旨：沉睡機會數放前面，手機通知列被截斷也讀得到重點。"""
    prefix = "⚠️ 部分來源 " if failures else ""
    totals = payload["totals"]
    return (
        f"{prefix}CRM 豐富化 {datetime.now().strftime('%Y-%m-%d')}"
        f"｜補 {totals['fields_filled']} 欄｜沉睡機會 {totals['reengagement_targets']} 人"
    )


# --------------------------------------------------------------------------
# 寫入與發送
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunContext:
    """一次執行的完整脈絡，避免把十幾個參數在函式之間傳來傳去。"""

    args: argparse.Namespace
    config: dict[str, Any]
    diagnostics: Diagnostics
    now: datetime
    records: list[EnrichedContact]
    scores: dict[str, ScoreResult]
    failures: list[ProviderFailure]
    skipped: list[dict[str, str]]
    state: dict[str, Any]
    state_file: Path
    csv_file: Path
    scanned: int


def apply_changes(ctx: RunContext, gate: AutonomyGate) -> dict[str, Any]:
    """決定要不要真的寫回 CRM，以及輸出 CSV 與狀態檔。

    寫回 CRM 是不可逆動作，因此比照「對外送出」交給自主權閘門管制：
    預設 draft 只產出變更計畫，等人審過、連續穩定 14 天且客戶簽核後才放行。
    """
    result: dict[str, Any] = {"crm_written": False, "records_written": 0,
                              "csv_file": None, "state_file": None}
    if ctx.args.dry_run:
        ctx.diagnostics.green("--dry-run：已產出變更計畫，未寫入任何檔案或 CRM")
        return {**result, "reason": APPLY_DRY_RUN}

    result["csv_file"] = str(write_csv(ctx.csv_file, to_csv_rows(ctx.records, ctx.scores)))
    result["state_file"] = str(
        save_state(ctx.state_file, ctx.state, ctx.records, ctx.now.isoformat(timespec="seconds"))
    )

    target = str((ctx.config.get("enrichment") or {}).get("write_target", ""))
    if ctx.args.mock:
        ctx.diagnostics.green("mock 模式：CSV 與狀態檔已產出，但不連線 CRM")
        return {**result, "reason": APPLY_MOCK}
    if not gate.can_send(target):
        ctx.diagnostics.green(
            f"自主權為 {gate.effective_level(target).value}：變更計畫已產出，等待人工核可後寫入 CRM"
        )
        return {**result, "reason": APPLY_DRAFT}

    writable = [record for record in ctx.records if record.filled]
    ctx.diagnostics.green(f"已核可寫回 CRM：{len(writable)} 位聯絡人")
    return {**result, "crm_written": True, "records_written": len(writable),
            "reason": APPLY_WRITTEN}


def deliver(
    text: str, subject: str, channel: str, recipients: Sequence[str],
    gate: AutonomyGate, is_dry_run: bool, diagnostics: Diagnostics,
) -> dict[str, Any]:
    """依 dry-run 與自主權階梯決定要不要真的送出報表。"""
    if is_dry_run:
        diagnostics.green("--dry-run：報表已產出但未發送")
        return _delivery(False, channel, "dry-run", [], list(recipients))
    if channel == "console":
        # 印在本機終端不算「對外發送」，因此不受自主權閘門管制。
        ok = Notifier("console").send(text, subject=subject)
        return _delivery(ok, channel, "console-output", list(recipients), [])

    approved = [item for item in recipients if gate.can_send(item)]
    held = [item for item in recipients if item not in approved]
    if not approved:
        diagnostics.green("自主權未放行：報表已產出為草稿，等待人工審核後送出")
        return _delivery(False, channel, "autonomy_draft", [], held)

    ok = Notifier(channel).send(text, subject=subject)
    return _delivery(ok, channel, "sent" if ok else "notifier-failed", approved, held)


def _delivery(
    delivered: bool, channel: str, reason: str, approved: list[str], held: list[str]
) -> dict[str, Any]:
    return {
        "delivered": delivered,
        "channel": channel,
        "reason": reason,
        "approved_recipients": approved,
        "held_recipients": held,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _build_context(args: argparse.Namespace) -> RunContext:
    """讀設定、挑名單、取外部資料、豐富化、計分——把脈絡準備好。"""
    diagnostics = Diagnostics(MODULE_NAME)
    config = load_config(Path(args.config).expanduser())
    if not args.mock:
        ensure_live_env(config, diagnostics)

    enrich_cfg = config.get("enrichment") or {}
    output_cfg = config.get("output") or {}
    now = resolve_now(config.get("schedule") or {}, diagnostics)

    contacts = load_contacts(MODULE_DIR / str(enrich_cfg.get("contacts_file", "")))
    state_file = resolve_path(args.state_file, output_cfg.get("state_file"), "state/state.json")
    state = load_state(state_file, diagnostics)
    targets, skipped = select_contacts(
        contacts, state, int(enrich_cfg.get("refresh_days", 30)), now
    )

    providers, failures = collect_providers(
        enrich_cfg.get("providers") or [], MODULE_DIR, diagnostics,
        is_mock=bool(args.mock),
        rate_limit_seconds=enrich_cfg.get("rate_limit_seconds", 1.0),
    )
    records = enrich_all(
        targets, providers, failures,
        enrich_cfg.get("target_fields") or [], enrich_cfg.get("protected_fields") or [],
        now.isoformat(timespec="seconds"),
    )
    rules = load_scoring_rules(config.get("scoring"))
    scores = {
        item.contact_id: item
        for item in score_all([record.record for record in records], rules, now)
    }
    return RunContext(
        args=args, config=config, diagnostics=diagnostics, now=now, records=records,
        scores=scores, failures=failures, skipped=skipped, state=state, state_file=state_file,
        csv_file=resolve_path(args.csv_out, output_cfg.get("csv_file"), "state/report.csv"),
        scanned=len(contacts),
    )


def _build_result(ctx: RunContext, payload: dict[str, Any], text: str, plan: str,
                  narrative: str, conflict_note: str, applied: dict[str, Any],
                  delivery: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """組出回傳 dict。鍵名採契約 §6「未來標準化」建議的 6 個共通鍵。"""
    module_cfg = ctx.config.get("module") or {}
    return {
        "module_id": str(module_cfg.get("id", "16")),
        "module_name": str(module_cfg.get("name", MODULE_NAME)),
        "mode": "mock" if ctx.args.mock else "live",
        "dry_run": bool(ctx.args.dry_run),
        "warnings": warnings,
        "amber_count": ctx.diagnostics.amber_count,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "reference_now": ctx.now.isoformat(timespec="seconds"),
        **payload,
        "skipped_contacts": ctx.skipped,
        "failed_providers": [
            {"provider_id": item.provider_id, "display_name": item.display_name,
             "reason": item.reason}
            for item in ctx.failures
        ],
        "is_partial": bool(ctx.failures),
        "scores": {key: value.to_dict() for key, value in ctx.scores.items()},
        "change_plan": plan,
        "report_text": text,
        "narrative": narrative,
        "conflict_review": conflict_note,
        "apply": applied,
        "delivery": delivery,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程並回傳結果 dict（供測試斷言）。本函式不呼叫 sys.exit。"""
    ctx = _build_context(args)
    payload = build_payload(ctx.records, ctx.scores, ctx.failures, ctx.skipped, ctx.scanned)

    narrative = write_narrative(payload, ctx.config, ctx.args.mock, ctx.diagnostics)
    conflict_note = review_conflicts(ctx.records, ctx.config, ctx.args.mock, ctx.diagnostics)
    plan = render_change_plan(ctx.records)
    text = render_report_text(
        payload, ctx.records, ctx.scores, ctx.failures,
        narrative, conflict_note, plan, bool(args.dry_run),
    )

    runtime_cfg = ctx.config.get("runtime") or {}
    gate = build_gate(runtime_cfg, ctx.diagnostics)
    applied = apply_changes(ctx, gate)
    delivery = deliver(
        text=text,
        subject=build_subject(payload, ctx.failures),
        channel=args.notify or str(runtime_cfg.get("notify_channel", "console")),
        recipients=[str(item) for item in ((ctx.config.get("schedule") or {}).get("recipients") or [])],
        gate=gate,
        is_dry_run=bool(args.dry_run),
        diagnostics=ctx.diagnostics,
    )
    return _build_result(
        ctx, payload, text, plan, narrative, conflict_note, applied, delivery, list(gate.warnings)
    )


def main() -> int:
    """CLI 進入點。回傳 exit code：部分來源無回應時仍回 0（名單有產出就算成功）。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (LLMError, FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    if not result["delivery"]["delivered"]:
        # 只有「真的送出去了」才不重印：console 通道送出時 Notifier 已經印過。
        # 未送出的情況（--dry-run、自主權扣住的草稿、通道失敗）一定要印在終端機，
        # 否則 --dry-run 的變更計畫沒有任何人看得到，這個旗標就等於不存在。
        print(result["report_text"])
    if result["is_partial"]:
        print(
            f"\n注意：本次以部分來源產出（{len(result['failed_providers'])} 個來源無回應）。",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
