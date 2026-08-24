"""demo08 — 競品價格監控警報（主流程）。

每日 07:00 由排程器呼叫一次：抓取競品頁面價格 → 與狀態檔中的基準價比對 →
變動幅度達到閾值就產生晨間警報，並把本次價格寫回狀態檔作為明天的基準。

本模組最重要的一條紀律：**解析失敗必須警報，不可靜默跳過**。
網站改版讓選擇器失效是最常見的無聲故障——監控器看起來每天都在跑、
每天都回報「無異常」，但其實它早就什麼都沒看到。
因此任何解析失敗都會走 `Diagnostics.amber`，並在報告中標明「N 個目標解析失敗」。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier  # noqa: E402

from comparator import (  # noqa: E402
    ComparatorError,
    PriceChange,
    compare,
    load_baselines,
    resolve_baseline,
    save_baselines,
    summarise,
    to_percent,
)
from scraper import FetchResult, ScraperError, scrape_targets  # noqa: E402

MODULE_NAME = "demo08-price-monitor"
NOTIFY_CHANNELS = ("console", "telegram", "gmail", "line", "whatsapp")
DEFAULT_CONFIG = _DEMO_DIR / "config.yaml"
ALERT_PROMPT = _DEMO_DIR / "prompts" / "alert_summary.md"
ALERT_FIXTURE = _DEMO_DIR / "mock" / "alert_summary_fixture.md"
CONTEXT_NOTE = "讀者是中小企業老闆，早上只有 60 秒。解析失敗不等於沒有變動。"

DELIVERY_SENT = "sent"
DELIVERY_DRAFT = "draft"
DELIVERY_DRY_RUN = "dry_run"


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器（旗標依 CONTRACT.md §6）"""
    parser = argparse.ArgumentParser(
        prog="demo08-price-monitor", description="競品價格監控警報"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", dest="mock", action="store_true", default=True,
                      help="離線模式：讀本地 HTML 快照，不觸網、不呼叫 API（預設）")
    mode.add_argument("--live", dest="mock", action="store_false",
                      help="串接真實競品網站與 Claude API")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="跑完整流程但不發送警報、也不更新狀態檔")
    parser.add_argument("--notify", choices=NOTIFY_CHANNELS, default=None,
                        help="通知管道，未指定時採用 config.yaml 的 runtime.notify_channel")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="設定檔路徑（預設為本目錄的 config.yaml）")
    parser.add_argument("--state-file", dest="state_file", default=None,
                        help="基準價狀態檔路徑，未指定時採用 config.yaml 的 monitor.state_file")
    return parser


def _resolve_path(raw: str | Path) -> Path:
    """相對路徑一律相對於本 demo 目錄解析，避免受呼叫端的工作目錄影響"""
    path = Path(raw)
    return path if path.is_absolute() else (_DEMO_DIR / path)


def _build_gate(runtime: dict, diagnostics: Diagnostics) -> AutonomyGate:
    """依 config 的 runtime 區塊建立自主權閘門，並把警告轉成 AMBER"""
    raw_level = str(runtime.get("autonomy", "draft"))
    try:
        level = AutonomyLevel(raw_level)
    except ValueError:
        diagnostics.red(
            symptom=f"runtime.autonomy 值不合法：{raw_level!r}",
            cause="設定檔寫了不存在的自主權層級",
            fix="改為 read_only / draft / supervised_auto 其中之一",
        )
        raise

    gate = AutonomyGate(
        level=level,
        approved_senders=list(runtime.get("approved_senders") or []),
        days_in_draft=int(runtime.get("days_in_draft", 0) or 0),
    )
    for warning in gate.warnings:
        diagnostics.amber(symptom=warning, fix="維持 draft 直到滿 14 天且客戶已簽核")
    return gate


def _collect_prices(results: list[FetchResult]) -> dict[str, Decimal]:
    """只取解析成功的價格；解析失敗的目標絕不寫進狀態檔汙染基準"""
    return {item.name: item.price for item in results if item.price is not None}


def _report_failures(diagnostics: Diagnostics, failures: list[FetchResult]) -> list[dict[str, str]]:
    """把每個解析失敗都轉成 AMBER 警示，並回傳可序列化的失敗清單"""
    payload: list[dict[str, str]] = []
    for item in failures:
        diagnostics.amber(
            symptom=f"{item.name} 解析失敗：{item.failure_reason}",
            fix=f"開啟 {item.url} 確認版面，更新 config.yaml 的 selector",
        )
        payload.append(
            {"name": item.name, "url": item.url, "reason": str(item.failure_reason)}
        )
    return payload


def _load_state(state_path: Path, diagnostics: Diagnostics) -> dict:
    """讀取基準狀態檔；損毀時走紅色警報，不靜默退回 config 種子值。"""
    try:
        return load_baselines(state_path)
    except ComparatorError as exc:
        diagnostics.red(
            symptom=f"基準狀態檔無法讀取：{exc}",
            cause="狀態檔在寫入過程中被中斷，或被手動改壞",
            fix=f"刪除或還原 {state_path} 後重跑（首次執行會改用 config 的 baseline_price）",
        )
        raise


def _compare_all(
    targets: list[dict], results: list[FetchResult], stored: dict, threshold: Decimal
) -> list[PriceChange]:
    """把抓到價格的目標逐一與基準比對"""
    configured = {str(t.get("name")): t.get("baseline_price") for t in targets}
    changes: list[PriceChange] = []
    for item in results:
        if item.price is None:
            continue
        baseline = resolve_baseline(item.name, configured.get(item.name), stored)
        changes.append(compare(item.name, item.url, baseline, item.price, threshold))
    return changes


def _build_llm_input(
    changes: list[PriceChange], failures: list[dict[str, str]], threshold: Decimal
) -> str:
    """組出餵給提示詞的結構化資料（BREACHES / STABLE / FAILURES 三段）"""
    breaches = [c for c in changes if c.is_breach]
    stable = [c for c in changes if not c.is_breach]

    def _line(change: PriceChange) -> str:
        return (
            f"- {change.name}｜基準 {change.baseline_price} → 現價 {change.current_price}"
            f"｜{change.delta_percent}%｜{change.direction}"
        )

    sections = [f"THRESHOLD_PERCENT: {threshold}", "BREACHES:"]
    sections += [_line(c) for c in breaches] or ["- （無）"]
    sections.append("STABLE:")
    sections += [_line(c) for c in stable] or ["- （無）"]
    sections.append("FAILURES:")
    sections += [f"- {f['name']}｜{f['reason']}" for f in failures] or ["- （無）"]
    return "\n".join(sections)


def _render_alert(
    client: LLMClient, changes: list[PriceChange], failures: list[dict[str, str]],
    threshold: Decimal, is_mock: bool,
) -> str:
    """呼叫 LLM 把比對結果寫成人話警報；mock 模式讀 fixture 不花錢"""
    system = ALERT_PROMPT.read_text(encoding="utf-8")
    user = _build_llm_input(changes, failures, threshold)
    fixture = ALERT_FIXTURE if is_mock else None
    return client.complete(system=system, user=user, max_tokens=800, fixture=fixture)


def _build_report(
    config: dict, changes: list[PriceChange], failures: list[dict[str, str]], alert_text: str
) -> str:
    """組出最終要發送的報告全文"""
    stats = summarise(changes)
    monitor = config.get("monitor", {})
    header = (
        f"【競品價格監控 · {monitor.get('run_at', '07:00')}】"
        f"{datetime.now().astimezone():%Y-%m-%d}\n"
        f"監控 {stats['compared'] + len(failures)} 個目標｜"
        f"{stats['breaches']} 個超過 {monitor.get('threshold_percent', 5)}% 閾值｜"
        f"{len(failures)} 個目標解析失敗"
    )
    table = "\n".join(
        f"  {'⚠️' if c.is_breach else '  '} {c.name}："
        f"{c.baseline_price} → {c.current_price}（{c.delta_percent}%）"
        for c in changes
    )
    blocks = [header, "", alert_text.strip(), "", "── 原始比對 ──", table]
    if failures:
        blocks += ["", f"── {len(failures)} 個目標解析失敗（非「無變動」，請人工確認）──"]
        blocks += [f"  ✗ {f['name']}：{f['reason']}" for f in failures]
    return "\n".join(blocks)


def _deliver(report: str, *, channel: str, gate: AutonomyGate, recipient: str,
             is_dry_run: bool, subject: str) -> tuple[str, bool]:
    """依自主權層級決定送出或留為草稿，回傳 (delivery, is_notified)"""
    if is_dry_run:
        return DELIVERY_DRY_RUN, False
    if gate.can_send(recipient):
        return DELIVERY_SENT, Notifier(channel).send(report, subject=subject)
    # 未取得自動送出授權：降級為草稿，只印在本機供人工過目，不推到對外管道
    drafted = f"【草稿・待人工核准】收件人 {recipient} 未取得自動送出授權\n\n{report}"
    return DELIVERY_DRAFT, Notifier("console").send(drafted, subject=subject)


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程並回傳結果 dict（不做 sys.exit，交給 main() 決定退出碼）"""
    diagnostics = Diagnostics(MODULE_NAME, exit_on_red=False)
    required_env = None
    config = load_config(_resolve_path(args.config), required_env=required_env)

    monitor = config.get("monitor", {})
    runtime = config.get("runtime", {})
    targets = list(config.get("targets") or [])
    if not targets:
        diagnostics.red(
            symptom="config.yaml 沒有任何監控目標",
            cause="targets 區塊為空或缺漏",
            fix="至少填入一組 {name, url, selector, baseline_price}",
        )

    threshold = to_percent(monitor.get("threshold_percent", 5))
    state_path = _resolve_path(args.state_file or monitor.get("state_file", "state/baselines.json"))
    results = scrape_targets(
        targets,
        is_mock=args.mock,
        mock_dir=_resolve_path(monitor.get("mock_pages_dir", "mock/pages")),
        interval_seconds=float(monitor.get("request_interval_seconds", 1)),
        timeout=int(monitor.get("request_timeout_seconds", 15)),
        user_agent=str(monitor.get("user_agent", "OpenClawPriceMonitor/1.0")),
    )

    failures = _report_failures(diagnostics, [r for r in results if not r.is_parsed])
    stored = _load_state(state_path, diagnostics)
    changes = _compare_all(targets, results, stored, threshold)

    gate = _build_gate(runtime, diagnostics)
    client = LLMClient(mock=args.mock, context_note=CONTEXT_NOTE)
    alert_text = _render_alert(client, changes, failures, threshold, args.mock)
    report = _build_report(config, changes, failures, alert_text)

    channel = args.notify or str(runtime.get("notify_channel", "console"))
    delivery, is_notified = _deliver(
        report,
        channel=channel,
        gate=gate,
        recipient=str(runtime.get("alert_recipient", "")),
        is_dry_run=args.dry_run,
        subject=f"競品價格警報｜{len([c for c in changes if c.is_breach])} 筆超閾值",
    )
    if not args.dry_run:
        save_baselines(state_path, _collect_prices(results))

    if not failures:
        diagnostics.green(f"{len(changes)}/{len(targets)} 個目標解析成功")
    return _build_result(
        config, changes, failures, alert_text, report,
        delivery=delivery, is_notified=is_notified, channel=channel,
        threshold=threshold, state_path=state_path, diagnostics=diagnostics,
    )


def _build_result(
    config: dict, changes: list[PriceChange], failures: list[dict[str, str]],
    alert_text: str, report: str, *, delivery: str, is_notified: bool, channel: str,
    threshold: Decimal, state_path: Path, diagnostics: Diagnostics,
) -> dict[str, Any]:
    """組出供測試斷言與下游使用的結果 dict（全部欄位皆可 JSON 序列化）"""
    monitor = config.get("monitor", {})
    return {
        "module": str(config.get("module", {}).get("id", "08")),
        "run_at": str(monitor.get("run_at", "07:00")),
        "threshold_percent": str(threshold),
        "checked": len(changes) + len(failures),
        "parsed": len(changes),
        "failed": len(failures),
        "failures": failures,
        "changes": [c.as_dict() for c in changes],
        "breaches": [c.as_dict() for c in changes if c.is_breach],
        "stats": summarise(changes),
        "alert_text": alert_text,
        "report": report,
        "delivery": delivery,
        "notified": is_notified,
        "notify_channel": channel,
        "amber_count": diagnostics.amber_count,
        "state_file": str(state_path),
    }


def main() -> int:
    """解析參數 → run() → 印出結果 → 回傳退出碼。

    退出碼約定（讓排程器能分辨「壞掉」與「有事要看」）：
        0 = 全部目標解析成功
        2 = 流程完成，但有目標解析失敗（AMBER，需人工確認選擇器）
        1 = 紅色警報或致命錯誤，本次監控沒有結果
    """
    args = build_parser().parse_args()
    try:
        result = run(args)
    except RedAlert as exc:
        print(f"紅色警報：{exc}", file=sys.stderr)
        return 1
    except (ComparatorError, ScraperError, FileNotFoundError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    # dry-run 不經 Notifier，報告要自己印出來才看得到
    if result["delivery"] == DELIVERY_DRY_RUN:
        print(result["report"])
    print(
        f"\n完成：{result['parsed']}/{result['checked']} 解析成功｜"
        f"{len(result['breaches'])} 筆超閾值｜{result['failed']} 筆解析失敗｜"
        f"delivery={result['delivery']}",
        file=sys.stderr,
    )
    return 2 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
