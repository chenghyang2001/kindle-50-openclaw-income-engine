"""模組 #30 — 白牌「AI 營運部」客戶經銷方案（主流程）。

書中數據：部署 2 週｜客戶 $8,000–$10,000 setup + $4,000–$6,000/mo
          下游子客戶 $1,000–$2,000/月/家｜分潤 20-30% 經銷商 / 70-80% 提供者

這是全書最後一個模組，也是商業模式的頂點（ch07_p15）：
賣的不再是自動化，而是基礎設施授權——經銷商掛自己的品牌轉售，
你在幕後當「隱形的引擎室」。

三條不可協商的規則（違反任一條，這個商業模式就不成立）：

1. **多租戶隔離**：所有資料存取都必須帶 `[RESELLER_SLUG]/[SUB_CLIENT_SLUG]`
   namespace，跨租戶一律拒絕並留下稽核紀錄。外洩會同時毀掉經銷商與你兩層信譽。
2. **全域安全閥**：任何對外 API 呼叫之前，必先通過內部 `--dry-run` 通訊測試
   （apxG_p03）。測試不過就不准送出任何一個封包。
3. **分潤總和必為 100%**：比例可談，總和不可談。

流程：
    載入設定 → 90 天門檻檢查 → 分潤政策驗證 → 品牌層載入
      → 租戶註冊表建立（此時解析所有 namespace）
      → **內部 dry-run 通訊測試**
      → 隔離演練重放（跨租戶嘗試必須全數被擋）
      → 逐租戶產出白牌月報 + 分潤對帳 → 品牌外洩掃描 → 通知
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402
from audit import AuditLog  # noqa: E402
from revenue_share import (  # noqa: E402
    RevenueShareError,
    SplitPolicy,
    aggregate,
    as_display,
    check_fee_band,
    format_money,
    split_fee,
    to_money,
)
from tenancy import (  # noqa: E402
    IsolationViolation,
    Namespace,
    NamespaceError,
    TenancyError,
    TenantStore,
    build_registry,
    parse_namespace,
)

MODULE_LABEL = "demo30-whitelabel-reseller"
LIVE_REQUIRED_ENV: tuple[str, ...] = ()
NOT_PROVIDED = "（原簡報未提供）"

REPORT_PROMPT = "prompts/whitelabel_monthly_report.md"
SLA_PROMPT = "prompts/reseller_sla_brief.md"

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出。
# 這裡刻意重申租戶邊界——白牌月報最危險的失敗不是寫得爛，是寫到別人的資料。
CONTEXT_NOTE = (
    "這是白牌轉售場景。你只看得到單一租戶的資料，也只能寫這個租戶的事。"
    "報告的署名是經銷商，不是基礎設施提供者。不要提及轉售、授權或分潤。"
)


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6，另加三個本模組專屬旗標）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_LABEL,
        description="白牌 AI 營運部：多租戶隔離 + 品牌層抽換 + 20-30/70-80 分潤",
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
        "--config", default=str(MODULE_DIR / "config.yaml"), help="設定檔路徑，預設同目錄 config.yaml"
    )
    parser.add_argument(
        "--tenant",
        default=None,
        help="只處理指定租戶：'reseller-slug' 或 'reseller-slug/sub-client-slug'",
    )
    parser.add_argument("--state-file", default=None, help="狀態檔路徑，覆蓋 config.state.file")
    parser.add_argument("--audit-file", default=None, help="稽核 JSONL 路徑，覆蓋 config.audit.file")
    return parser


def _resolve_path(value: str | Path) -> Path:
    """相對路徑一律以模組目錄為基準，杜絕硬編碼使用者路徑。"""
    path = Path(value).expanduser()
    return path if path.is_absolute() else MODULE_DIR / path


def _load_json(value: str | Path, label: str) -> Any:
    """讀取 JSON，格式錯誤要明確報錯（帶絕對路徑，方便跨機器追查）。"""
    path = _resolve_path(value)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TenancyError(f"{label} 無法讀取或解析：{path}｜{exc}") from exc


def _read_prompt(relative: str) -> str:
    """讀取提示詞檔（提示詞是資產，一律獨立成 .md，不內嵌在 .py）。"""
    path = _resolve_path(relative)
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TenancyError(f"提示詞檔無法讀取：{path}｜{exc}") from exc


def _fixed_clock(stamp: str) -> Callable[[], str]:
    """mock 模式的固定時鐘，讓稽核輸出可完全重現。"""
    return lambda: stamp


def _build_audit(args: argparse.Namespace, config: dict, is_mock: bool) -> AuditLog:
    """建立稽核日誌。--audit-file 優先於 config.audit.file。"""
    configured = (config.get("audit") or {}).get("file", "audit.jsonl")
    target = _resolve_path(args.audit_file or configured)
    stamp = str((config.get("mock") or {}).get("now") or "").strip()
    clock = _fixed_clock(stamp) if (is_mock and stamp) else None
    return AuditLog(path=target, module=MODULE_LABEL, clock=clock)


def _require_live_env(diagnostics: Diagnostics) -> None:
    """--live 缺憑證要明確報錯退出，絕不靜默降級回 mock。"""
    missing = [key for key in LIVE_REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        diagnostics.red(
            symptom=f"--live 模式缺少環境變數：{', '.join(missing)}",
            cause="金鑰未設定或未載入目前的 shell session",
            fix=f"設定 {', '.join(missing)} 後重跑，或改用 --mock 離線驗證",
        )


def _check_whitelabel_gate(config: dict, diagnostics: Diagnostics, audit: AuditLog) -> dict:
    """檢查白牌化門檻：穩定運行 90+ 天、已部署 4 個以上企業級方案。

    未達門檻不擋執行（demo 仍要能跑），但一定發 AMBER 並留稽核——
    書中把 90 天寫成「前提條件」，跳過它去白牌化等於拿信譽賭運氣。
    """
    section = config.get("whitelabel") or {}
    days_stable = int(section.get("days_stable") or 0)
    min_days = int(section.get("min_stable_days") or 90)
    deployed = int(section.get("deployed_automations") or 0)
    min_deployed = int(section.get("min_enterprise_automations") or 4)
    gate = {
        "days_stable": days_stable,
        "min_stable_days": min_days,
        "deployed_automations": deployed,
        "min_enterprise_automations": min_deployed,
        "is_eligible": days_stable >= min_days and deployed >= min_deployed,
        "warnings": [],
    }
    if days_stable < min_days:
        gate["warnings"].append(f"穩定運行僅 {days_stable} 天，未達 {min_days} 天門檻")
    if deployed < min_deployed:
        gate["warnings"].append(f"已部署 {deployed} 個企業級方案，未達 {min_deployed} 個門檻")
    for message in gate["warnings"]:
        diagnostics.amber(symptom=message, fix="達標後再進入白牌模式，或向經銷商揭露現況")
    audit.record(
        event="whitelabel_gate",
        severity="green" if gate["is_eligible"] else "amber",
        detail={key: gate[key] for key in ("days_stable", "deployed_automations", "is_eligible")},
    )
    return gate


def _build_policy(config: dict, diagnostics: Diagnostics, audit: AuditLog) -> tuple[SplitPolicy, list[str]]:
    """建立分潤政策。總和不為 100 直接 RED——這是不可協商的算術。"""
    try:
        policy = SplitPolicy.from_config(config.get("revenue_share"))
        warnings = policy.validate()
    except RevenueShareError as exc:
        audit.record(event="revenue_policy_invalid", severity="red", detail={"error": str(exc)})
        diagnostics.red(
            symptom=str(exc),
            cause="config.yaml 的 revenue_share 比例不合法",
            fix="調整 reseller_pct / provider_pct，使兩者總和恰為 100",
        )
        raise
    for message in warnings:
        diagnostics.amber(symptom=message, fix="回查經銷商協議中的分潤條款")
    audit.record(
        event="revenue_policy_loaded",
        severity="green" if not warnings else "amber",
        detail={"reseller_pct": str(policy.reseller_pct), "provider_pct": str(policy.provider_pct)},
    )
    return policy, warnings


def _load_brand_context(config: dict) -> dict:
    """載入 brand_context.yml（Orchestrator 持有的單一真理來源）。"""
    relative = (config.get("brand") or {}).get("context_file", "brand_context.yml")
    return load_config(_resolve_path(relative))


def _merge_brand(brand_context: dict, overrides: dict | None) -> dict:
    """把經銷商的品牌覆蓋值套到預設品牌上（整組抽換，非逐處貼補丁）。

    只有 `brand_substitution_protocol.fields` 白名單內的欄位會被覆蓋——
    否則經銷商可以透過設定檔改到品牌以外的行為。
    """
    base = dict(brand_context.get("brand") or {})
    protocol = brand_context.get("brand_substitution_protocol") or {}
    allowed = set(protocol.get("fields") or [])
    ignored = []
    for key, value in (overrides or {}).items():
        if key in allowed:
            base[key] = value
        else:
            ignored.append(key)
    base["_ignored_override_fields"] = ignored
    return base


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


def _select_namespaces(
    args: argparse.Namespace,
    registry: dict[str, dict],
    diagnostics: Diagnostics,
    audit: AuditLog,
) -> list[str]:
    """依 --tenant 篩選要處理的 namespace。

    `--tenant` 是驅動本次執行的身分。解析失敗一律 RED 並結束行程，
    不猜測、不修正、不退回「全部租戶」——把畸形身分當成萬用字元是最糟的失敗模式。
    """
    if not args.tenant:
        return list(registry)
    try:
        scope = parse_namespace(args.tenant, require_sub_client=False)
    except NamespaceError as exc:
        audit.record(
            event="namespace_rejected",
            severity="red",
            namespace=str(args.tenant),
            detail={"source": "--tenant", "error": str(exc)},
        )
        diagnostics.red(
            symptom=str(exc),
            cause="--tenant 指定的 namespace 無法解析或含路徑跳脫字元",
            fix="改用 'reseller-slug' 或 'reseller-slug/sub-client-slug' 格式重跑",
        )
        raise
    selected = _match_scope(scope, registry)
    if not selected:
        audit.record(
            event="tenant_not_found", severity="red", namespace=scope.path, detail={"source": "--tenant"}
        )
        diagnostics.red(
            symptom=f"--tenant {scope.path} 沒有對應的租戶",
            cause="租戶註冊表中不存在此 namespace",
            fix=f"可用租戶：{', '.join(sorted(registry))}",
        )
    return selected


def _match_scope(scope: Namespace, registry: dict[str, dict]) -> list[str]:
    """把 --tenant 的範圍（經銷商層或子客戶層）展開成 namespace 清單。"""
    if scope.is_reseller_scope:
        return [
            path
            for path, entry in registry.items()
            if entry["namespace"].reseller_slug == scope.reseller_slug
        ]
    return [scope.path] if scope.path in registry else []


def _selftest(
    store: TenantStore,
    namespaces: list[str],
    registry: dict[str, dict],
    diagnostics: Diagnostics,
    audit: AuditLog,
) -> dict:
    """全域安全閥（apxG_p03）：對外 API 呼叫前的內部 --dry-run 通訊測試。

    測試內容全部在本機完成、零網路：
    每個租戶都以自己的身分讀一次自己的資料，確認 namespace 能對上、
    資料檔存在且內容宣告的 namespace 一致。任何一項不過就不准送出任何封包。
    """
    checks: list[dict] = []
    for path in namespaces:
        entry = registry[path]
        namespace: Namespace = entry["namespace"]
        try:
            payload = store.read(actor=namespace, target=namespace)
            ok = str(payload.get("namespace")) == namespace.path
            reason = "" if ok else "資料檔 namespace 與請求不符"
        except (TenancyError, IsolationViolation) as exc:
            ok, reason = False, str(exc)
        checks.append({"namespace": path, "passed": ok, "reason": reason})
    failed = [item for item in checks if not item["passed"]]
    audit.record(
        event="dry_run_selftest",
        severity="green" if not failed else "red",
        detail={"total": len(checks), "failed": len(failed)},
    )
    if failed:
        diagnostics.red(
            symptom=f"內部 dry-run 通訊測試失敗 {len(failed)} 項：{failed[0]['namespace']}",
            cause=failed[0]["reason"] or "租戶資料無法以自身身分讀取",
            fix="修好租戶資料與 namespace 對應後重跑；在測試通過前不得發出任何對外呼叫",
        )
    return {"required": True, "total": len(checks), "passed": len(checks) - len(failed), "checks": checks}


def _replay_attempts(
    store: TenantStore,
    attempts: list[dict],
    diagnostics: Diagnostics,
    audit: AuditLog,
    halt_on_violation: bool,
) -> dict:
    """重放隔離演練清單。每一筆都真的走一次 TenantStore，不靠 expected 欄位放水。"""
    results = []
    for attempt in attempts:
        outcome = _replay_one(store, attempt, audit)
        results.append(outcome)
        if outcome["outcome"] == "denied":
            diagnostics.amber(
                symptom=f"隔離演練 {outcome['id']}：{outcome['actor']} → {outcome['target']} 已擋下"
                f"（{outcome['reason']}）",
                fix="這是預期行為；若此嘗試來自正式流程而非演練，請立即追查來源",
            )
    denied = [item for item in results if item["outcome"] == "denied"]
    if denied and halt_on_violation:
        diagnostics.red(
            symptom=f"偵測到 {len(denied)} 次跨租戶存取嘗試",
            cause="halt_on_isolation_violation 已開啟，任何隔離違規都中斷執行",
            fix="追查嘗試來源後再重跑；稽核日誌已記錄每一筆",
        )
    return {
        "total": len(results),
        "allowed": len(results) - len(denied),
        "denied": len(denied),
        "details": results,
    }


def _replay_one(store: TenantStore, attempt: dict, audit: AuditLog) -> dict:
    """重放單一存取嘗試，回傳判定結果。解析失敗與跨租戶都算 denied。"""
    record = {
        "id": str(attempt.get("id") or "?"),
        "actor": str(attempt.get("actor")),
        "target": str(attempt.get("target")),
        "outcome": "denied",
        "reason": "",
    }
    try:
        actor = parse_namespace(attempt.get("actor"))
        target = parse_namespace(attempt.get("target"))
    except NamespaceError as exc:
        record["reason"] = str(exc)
        audit.record(
            event="namespace_rejected",
            severity="red",
            namespace=record["target"],
            actor=record["actor"],
            detail={"source": "access_attempt", "id": record["id"], "error": str(exc)},
        )
        return record
    try:
        store.read(actor=actor, target=target)
    except IsolationViolation as exc:
        record["reason"] = str(exc)
        return record
    except TenancyError as exc:
        record["reason"] = f"資料層錯誤：{exc}"
        return record
    record["outcome"] = "allowed"
    record["reason"] = "同租戶讀取"
    return record


def _scan_brand_leak(text: str, forbidden: list[str]) -> list[str]:
    """掃描對外輸出是否夾帶提供者的身分字串（白牌的致命失敗）。"""
    return [needle for needle in forbidden if needle and needle in text]


def _compose_sub_report(llm: LLMClient, brand: dict, entry: dict, payload: dict) -> str:
    """為單一子客戶產出白牌月報。

    送進 LLM 的 payload **只含這一個租戶**的資料——跨租戶的東西根本不進 context，
    比事後檢查 LLM 有沒有寫錯更可靠。
    """
    system = _read_prompt(REPORT_PROMPT)
    system = system.replace("{{BRAND_DISPLAY_NAME}}", str(brand.get("display_name", "")))
    system = system.replace("{{BRAND_TONE}}", str(brand.get("tone", "")))
    sub = entry["sub_client"]
    user = json.dumps(
        {
            "namespace": payload.get("namespace"),
            "sub_client": {"display_name": sub.get("display_name"), "tier": sub.get("stack_tier")},
            "period": payload.get("period"),
            "metrics": payload.get("metrics"),
            "incidents": payload.get("incidents"),
            "automations_live": payload.get("automations_live"),
        },
        ensure_ascii=False,
        indent=2,
    )
    return llm.complete(system=system, user=user, max_tokens=600)


def _process_sub_client(path: str, context: dict) -> dict:
    """處理單一子客戶：讀資料 → 分潤 → 產月報 → 品牌外洩掃描。"""
    entry = context["registry"][path]
    namespace: Namespace = entry["namespace"]
    payload = context["store"].read(actor=namespace, target=namespace)
    fee = to_money(entry["sub_client"].get("monthly_fee"), f"{path} 的 monthly_fee")
    split = split_fee(fee, context["policy"])
    band_warning = check_fee_band(split["fee"], context["fee_min"], context["fee_max"])
    body = _compose_sub_report(context["llm"], context["brand"], entry, payload)
    leaks = _scan_brand_leak(body, context["forbidden"])
    context["audit"].record(
        event="whitelabel_report_built",
        severity="amber" if leaks else "green",
        namespace=path,
        actor=path,
        detail={"fee": format_money(split["fee"]), "leaks": leaks},
    )
    return {
        "namespace": path,
        "display_name": entry["sub_client"].get("display_name"),
        "stack_tier": entry["sub_client"].get("stack_tier"),
        "period": payload.get("period"),
        "split": split,
        "fee_band_warning": band_warning,
        "brand_leaks": leaks,
        "report": body,
    }


def _compose_sla_brief(llm: LLMClient, reseller: dict, items: list[dict], totals: dict) -> str:
    """為經銷商產出分潤與 SLA 對帳說明（收件人是經銷商本人，不是終端客戶）。"""
    system = _read_prompt(SLA_PROMPT)
    user = json.dumps(
        {
            "reseller": {
                "legal_name": reseller.get("legal_name"),
                "agreement_template": reseller.get("agreement_template"),
            },
            "sub_clients": [
                {
                    "display_name": item["display_name"],
                    "fee": format_money(item["split"]["fee"]),
                    "reseller_share": format_money(item["split"]["reseller"]),
                    "provider_share": format_money(item["split"]["provider"]),
                }
                for item in items
            ],
            "totals": as_display(totals),
        },
        ensure_ascii=False,
        indent=2,
    )
    return llm.complete(system=system, user=user, max_tokens=700)


def _process_reseller(reseller_slug: str, paths: list[str], context: dict) -> dict:
    """處理單一經銷商：品牌抽換 → 逐子客戶產月報 → 分潤合計 → 自主權判定。"""
    reseller = context["registry"][paths[0]]["reseller"]
    brand = _merge_brand(context["brand_context"], reseller.get("brand_overrides"))
    scoped = dict(context, brand=brand)
    items = [_process_sub_client(path, scoped) for path in paths]
    totals = aggregate([item["split"] for item in items])
    brief = _compose_sla_brief(context["llm"], reseller, items, totals)
    recipient = str(brand.get("support_email") or "")
    level = context["gate"].effective_level(recipient)
    can_send = context["gate"].can_send(recipient) and not context["dry_run"]
    if can_send:
        context["notifier"].send(text=brief, subject=f"[{brand.get('display_name')}] 月度營運與對帳")
    return {
        "reseller_slug": reseller_slug,
        "legal_name": reseller.get("legal_name"),
        "brand_display_name": brand.get("display_name"),
        "recipient": recipient,
        "autonomy": level.value,
        "delivery": "sent" if can_send else "drafted",
        "sub_clients": [_public_item(item) for item in items],
        "totals": as_display(totals),
        "totals_decimal": totals,
        "sla_brief": brief,
    }


def _public_item(item: dict) -> dict:
    """把含 Decimal 的內部結構轉成可 JSON 序列化的回報結構。"""
    return {
        "namespace": item["namespace"],
        "display_name": item["display_name"],
        "stack_tier": item["stack_tier"],
        "period": item["period"],
        "monthly_fee": format_money(item["split"]["fee"]),
        "reseller_share": format_money(item["split"]["reseller"]),
        "provider_share": format_money(item["split"]["provider"]),
        "fee_band_warning": item["fee_band_warning"],
        "brand_leaks": item["brand_leaks"],
        "report": item["report"],
    }


def _group_by_reseller(namespaces: list[str], registry: dict[str, dict]) -> dict[str, list[str]]:
    """把 namespace 清單依經銷商分組，保留註冊表原始順序。"""
    grouped: dict[str, list[str]] = {}
    for path in namespaces:
        slug = registry[path]["namespace"].reseller_slug
        grouped.setdefault(slug, []).append(path)
    return grouped


def _collect_item_warnings(resellers: list[dict], diagnostics: Diagnostics) -> list[str]:
    """把子客戶層級的月費與品牌外洩問題收斂成警告清單。"""
    warnings: list[str] = []
    for reseller in resellers:
        for item in reseller["sub_clients"]:
            if item["fee_band_warning"]:
                warnings.append(f"{item['namespace']}：{item['fee_band_warning']}")
                diagnostics.amber(symptom=item["fee_band_warning"], fix="回查經銷商協議的月費條款")
            if item["brand_leaks"]:
                message = f"{item['namespace']} 月報夾帶提供者身分字串：{item['brand_leaks']}"
                warnings.append(message)
                diagnostics.amber(symptom=message, fix="修正提示詞或品牌層後重產，未修正前不得送出")
    return warnings


def _write_state(path: Path, result: dict) -> bool:
    """寫出本次執行狀態（下次可比對期別與分潤）。寫不進去要明講，不靜默略過。"""
    payload = {
        "module_id": result["module_id"],
        "generated_at": result["generated_at"],
        "mode": result["mode"],
        "tenants": {
            item["namespace"]: {
                "period": item["period"],
                "monthly_fee": item["monthly_fee"],
                "provider_share": item["provider_share"],
            }
            for reseller in result["resellers"]
            for item in reseller["sub_clients"]
        },
        "revenue_totals": result["revenue_totals"],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        raise TenancyError(f"狀態檔寫入失敗：{path}｜{exc}") from exc
    return True


def _summarise(result: dict) -> str:
    """組出給操作者的摘要文字。"""
    gate = result["whitelabel_gate"]
    isolation = result["isolation"]
    lines = [
        f"【{result['module_name']}】{result['generated_at']}（{result['mode']} 模式）",
        f"白牌門檻：穩定 {gate['days_stable']}/{gate['min_stable_days']} 天｜"
        f"已部署 {gate['deployed_automations']}/{gate['min_enterprise_automations']} 個｜"
        f"{'達標' if gate['is_eligible'] else '未達標'}",
        f"內部 dry-run 通訊測試：{result['selftest']['passed']}/{result['selftest']['total']} 通過",
        f"隔離演練：{isolation['allowed']} 允許／{isolation['denied']} 拒絕（共 {isolation['total']} 次）",
        f"分潤：經銷商 {result['revenue_policy']['reseller_pct']}%／"
        f"提供者 {result['revenue_policy']['provider_pct']}%",
        f"總月費 {result['revenue_totals']['fee']}｜經銷商 {result['revenue_totals']['reseller']}"
        f"｜提供者 {result['revenue_totals']['provider']}",
        f"內部回收工時：{result['recovered_hours_per_month']}",
    ]
    for reseller in result["resellers"]:
        lines.append(
            f"  [{reseller['delivery']}] {reseller['brand_display_name']}"
            f"（{reseller['reseller_slug']}）子客戶 {len(reseller['sub_clients'])} 家"
            f"｜提供者應收 {reseller['totals']['provider']}"
        )
    for item in isolation["details"]:
        if item["outcome"] == "denied":
            lines.append(f"  [擋下] {item['id']} {item['actor']} → {item['target']}")
    return "\n".join(lines)


def _enforce_selftest_flag(config: dict, diagnostics: Diagnostics) -> list[str]:
    """安全閥不可停用。config 改成 false 也會被強制視為 true 並發 AMBER。"""
    raw = (config.get("safety") or {}).get("require_dry_run_selftest", True)
    if raw is True:
        return []
    message = f"config.safety.require_dry_run_selftest 被設為 {raw!r}，已強制覆寫為 true"
    diagnostics.amber(symptom=message, fix="本模組不允許停用內部 dry-run 通訊測試")
    return [message]


def _build_context(config: dict, args: argparse.Namespace, parts: dict) -> dict:
    """把跑一輪需要的所有元件收成單一 context，避免函式參數列爆炸。"""
    pricing = config.get("pricing") or {}
    protocol = parts["brand_context"].get("brand_substitution_protocol") or {}
    return {
        "registry": parts["registry"],
        "store": parts["store"],
        "policy": parts["policy"],
        "gate": parts["gate"],
        "audit": parts["audit"],
        "brand_context": parts["brand_context"],
        "forbidden": list(protocol.get("forbidden_leaks") or []),
        "llm": LLMClient(mock=not args.live, context_note=CONTEXT_NOTE),
        "notifier": Notifier(channel=args.notify),
        "dry_run": bool(args.dry_run),
        "fee_min": to_money(pricing.get("sub_client_monthly_min", 1000), "sub_client_monthly_min"),
        "fee_max": to_money(pricing.get("sub_client_monthly_max", 2000), "sub_client_monthly_max"),
    }


def run(args: argparse.Namespace) -> dict:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    config = load_config(_resolve_path(args.config))
    diagnostics = Diagnostics(MODULE_LABEL)
    is_mock = not args.live
    audit = _build_audit(args, config, is_mock)
    warnings = _enforce_selftest_flag(config, diagnostics)
    gate_info = _check_whitelabel_gate(config, diagnostics, audit)
    warnings.extend(gate_info["warnings"])
    policy, policy_warnings = _build_policy(config, diagnostics, audit)
    warnings.extend(policy_warnings)
    brand_context = _load_brand_context(config)
    mock_cfg = config.get("mock") or {}
    registry = build_registry(_load_json(mock_cfg.get("tenants", "mock/tenants.json"), "tenants"))
    data_root = _resolve_path((config.get("tenancy") or {}).get("data_root", "mock/data"))
    store = TenantStore(data_root=data_root, audit=audit)
    autonomy_gate, gate_warnings = _build_gate(config, diagnostics)
    warnings.extend(gate_warnings)
    selected = _select_namespaces(args, registry, diagnostics, audit)
    # 安全閥：測試通過之前不得建立任何對外連線（含 LLM 與 Notifier）。
    selftest = _selftest(store, selected, registry, diagnostics, audit)
    if not is_mock:
        _require_live_env(diagnostics)
    isolation = _replay_isolation(config, store, diagnostics, audit)
    parts = {
        "registry": registry,
        "store": store,
        "policy": policy,
        "gate": autonomy_gate,
        "audit": audit,
        "brand_context": brand_context,
    }
    context = _build_context(config, args, parts)
    resellers = [
        _process_reseller(slug, paths, context)
        for slug, paths in _group_by_reseller(selected, registry).items()
    ]
    warnings.extend(_collect_item_warnings(resellers, diagnostics))
    return _build_result(
        config=config,
        args=args,
        diagnostics=diagnostics,
        audit=audit,
        policy=policy,
        gate_info=gate_info,
        selftest=selftest,
        isolation=isolation,
        resellers=resellers,
        warnings=warnings,
    )


def _replay_isolation(config: dict, store: TenantStore, diagnostics: Diagnostics, audit: AuditLog) -> dict:
    """載入並重放隔離演練清單。"""
    relative = (config.get("mock") or {}).get("access_attempts", "mock/access_attempts.json")
    payload = _load_json(relative, "access_attempts")
    attempts = payload.get("attempts") if isinstance(payload, dict) else payload
    halt = bool((config.get("safety") or {}).get("halt_on_isolation_violation", False))
    return _replay_attempts(store, list(attempts or []), diagnostics, audit, halt)


def _build_result(
    config: dict,
    args: argparse.Namespace,
    diagnostics: Diagnostics,
    audit: AuditLog,
    policy: SplitPolicy,
    gate_info: dict,
    selftest: dict,
    isolation: dict,
    resellers: list[dict],
    warnings: list[str],
) -> dict:
    """組出統一的回傳結構，並落狀態檔。"""
    module = config.get("module") or {}
    totals = aggregate([reseller["totals_decimal"] for reseller in resellers])
    state_target = _resolve_path(args.state_file or (config.get("state") or {}).get("file", "state.json"))
    result = {
        "module_id": str(module.get("id", "30")),
        "module_name": str(module.get("name", "白牌 AI 營運部客戶經銷方案")),
        "mode": "mock" if not args.live else "live",
        "dry_run": bool(args.dry_run),
        "notify_channel": args.notify,
        "tenant_filter": args.tenant,
        "generated_at": str((config.get("mock") or {}).get("now") or ""),
        "recovered_hours_per_month": _recovered_hours(module),
        "whitelabel_gate": gate_info,
        "revenue_policy": {
            "reseller_pct": str(policy.reseller_pct),
            "provider_pct": str(policy.provider_pct),
        },
        "selftest": selftest,
        "isolation": isolation,
        "resellers": [_strip_decimal(item) for item in resellers],
        "revenue_totals": as_display(totals),
        "warnings": warnings,
        "amber_count": diagnostics.amber_count,
        "audit_file": str(audit.path),
        "audit_counts": audit.counts(),
        "state_file": str(state_target),
    }
    result["state_written"] = False if args.dry_run else _write_state(state_target, result)
    return result


def _strip_decimal(reseller: dict) -> dict:
    """移除只在內部運算用的 Decimal 欄位，讓結果可直接 json.dumps。"""
    return {key: value for key, value in reseller.items() if key != "totals_decimal"}


def _recovered_hours(module: dict) -> Any:
    """內部回收工時：原簡報 36 張投影片皆未提供，一律標示未提供，禁止推估。"""
    value = module.get("recovered_hours_per_month")
    return NOT_PROVIDED if value is None else value


def main() -> int:
    """解析參數 → run() → 印出/發送結果 → 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (TenancyError, RevenueShareError) as exc:
        print(f"[RED] [{MODULE_LABEL}] {exc}", file=sys.stderr, flush=True)
        return 1
    summary = _summarise(result)
    print(summary)
    print(f"稽核日誌：{result['audit_file']}（{result['audit_counts']}）")
    if result["state_written"]:
        print(f"狀態檔：{result['state_file']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
