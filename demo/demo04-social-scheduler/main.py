"""模組 #4：社群媒體內容排程 — 主流程。

企業主提交 10 分鐘簡報 -> Agent 產出全平台一週內容 -> 人工審閱 20 分鐘 -> 跨平台排程。

自主權預設 DRAFT：社群貼文一旦發出就是公開的，撤回成本遠高於審閱成本，
因此排程前一律要人過目。這是本模組防止品牌災難的關鍵設計。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parent
# demo/ 在上一層，_shared 從那裡匯入；再把本目錄加進來讓 generator 可被匯入
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyError, AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402

from generator import (  # noqa: E402
    GeneratorError,
    PlatformProfile,
    first_line,
    generate_week,
    load_json_file,
)

MODULE_NAME = "demo04-social-scheduler"

# 第 04 章：CONTEXT_NOTE 可減少約 40% 不相關輸出。
# 社群貼文最危險的幻覺是「編造獎項與客戶名稱」，所以這裡把邊界講死。
CONTEXT_NOTE = (
    "你正在為一家小型企業產出社群貼文。只能使用 brief 與品牌語氣檔提供的事實，"
    "不得杜撰數據、獎項、媒體報導或客戶名稱。不確定的事就不要寫。"
)


def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6）。"""
    parser = argparse.ArgumentParser(
        description="模組 #4：社群媒體內容排程（一份簡報產出全平台一週內容）"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--mock",
        action="store_true",
        default=True,
        help="離線模式，不呼叫真實 API（預設）",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="串接真實 Anthropic API（需要 ANTHROPIC_API_KEY）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="跑完整流程但不實際發送通知",
    )
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="通知管道，預設 console",
    )
    parser.add_argument(
        "--config",
        default=str(_DEMO_DIR / "config.yaml"),
        help="設定檔路徑，預設同目錄 config.yaml",
    )
    return parser


def _resolve(rel: str | Path) -> Path:
    """把設定檔中的相對路徑轉成絕對路徑（禁止硬編碼使用者目錄）。"""
    path = Path(rel)
    return path if path.is_absolute() else _DEMO_DIR / path


def _build_profiles(
    config: dict[str, Any], diagnostics: Diagnostics
) -> list[PlatformProfile]:
    """從設定建立各平台的語氣 profile。沒有平台就沒有內容可產，屬紅色警報。"""
    raw_platforms = config.get("platforms") or []
    if not raw_platforms:
        diagnostics.red(
            symptom="config.yaml 沒有任何 platforms",
            cause="設定檔缺少 platforms 區段或內容為空",
            fix="至少設定一個平台 profile（id / tone / char_limit / schedule_slots）後重跑",
        )
        raise GeneratorError("config.yaml 缺少 platforms 區段")
    try:
        return [PlatformProfile.from_config(item) for item in raw_platforms]
    except GeneratorError as exc:
        diagnostics.red(
            symptom="平台 profile 無法解析",
            cause=str(exc),
            fix="補齊 platforms 區段的必要欄位後重跑",
        )
        raise


def _build_gate(runtime: dict[str, Any], diagnostics: Diagnostics) -> AutonomyGate:
    """建立自主權閘門。任何設定異常都往 DRAFT 降級，絕不往上放寬。"""
    level_name = str(runtime.get("autonomy", "draft"))
    try:
        level = AutonomyLevel(level_name)
    except ValueError:
        diagnostics.amber(
            symptom=f"未知的 autonomy 值 {level_name!r}",
            fix="改用預設的 draft；合法值為 read_only / draft / supervised_auto",
        )
        level = AutonomyLevel.DRAFT
    try:
        return AutonomyGate(
            level=level,
            approved_senders=list(runtime.get("approved_senders") or []),
            days_in_draft=int(runtime.get("days_in_draft", 0)),
        )
    except AutonomyError as exc:
        diagnostics.amber(
            symptom=f"自主權設定違規：{exc}",
            fix="已降級為 DRAFT；補上 approved_senders 後才可開啟 supervised_auto",
        )
        return AutonomyGate(level=AutonomyLevel.DRAFT)


def _apply_autonomy(posts: list[dict[str, Any]], gate: AutonomyGate) -> None:
    """依自主權層級標記每則貼文是「待審草稿」還是「可直接排程」。

    這裡把平台代號當成 recipient 餵給 AutonomyGate：白名單放的是「哪些平台
    已經跑滿觀察期、客戶簽核可以自動排程」，其餘一律降級為草稿。
    """
    for post in posts:
        platform = post["platform"]
        post["status"] = "scheduled" if gate.can_send(platform) else "draft"
        post["effective_autonomy"] = gate.effective_level(platform).value


def _assemble_result(
    config: dict[str, Any],
    week: dict[str, Any],
    gate: AutonomyGate,
    brief: dict[str, Any],
) -> dict[str, Any]:
    """把產出與設定組成回傳結果（供測試斷言與通知渲染）。"""
    module = config.get("module") or {}
    content = config.get("content") or {}
    runtime = config.get("runtime") or {}
    posts = week["posts"]
    return {
        "module_id": str(module.get("id", "04")),
        "module_name": str(module.get("name", "社群媒體內容排程")),
        "business_name": str(brief.get("business_name", "（未命名）")),
        "week_of": str(brief.get("week_of", "")),
        "requested_autonomy": str(runtime.get("autonomy", "draft")),
        "platforms": week["platforms"],
        "posts": posts,
        "total_posts": len(posts),
        "drafts": sum(1 for post in posts if post["status"] == "draft"),
        "scheduled": sum(1 for post in posts if post["status"] == "scheduled"),
        "brief_minutes": int(content.get("brief_minutes", 10)),
        "review_minutes": int(content.get("review_minutes", 20)),
        "warnings": list(week["warnings"]) + list(gate.warnings),
    }


def format_summary(result: dict[str, Any]) -> str:
    """把一週內容渲染成人可讀的審閱清單（也是通知內文）。"""
    lines = [
        f"📅 {result['module_name']}｜{result['business_name']}｜{result['week_of']} 當週",
        f"共 {result['total_posts']} 則（待審草稿 {result['drafts']} / 已排程 "
        f"{result['scheduled']}），預估審閱 {result['review_minutes']} 分鐘",
    ]
    posts_by_platform: dict[str, list[dict[str, Any]]] = {}
    for post in result["posts"]:
        posts_by_platform.setdefault(post["platform"], []).append(post)
    for platform in result["platforms"]:
        lines.append("")
        lines.append(
            f"— {platform['display_name']}（{platform['posts']} 則，上限 "
            f"{platform['char_limit']} 字元）"
        )
        for post in posts_by_platform.get(platform["id"], []):
            lines.append(
                f"  [{post['status']}] {post['scheduled_for']}｜"
                f"{post['char_count']} 字元｜{first_line(post['text'])}"
            )
    if result["warnings"]:
        lines.append("")
        lines.append("⚠️ 審閱時要處理的提醒：")
        lines.extend(f"  - {warning}" for warning in result["warnings"])
    return "\n".join(lines)


def _deliver(
    summary: str, args: argparse.Namespace, diagnostics: Diagnostics
) -> bool:
    """送出審閱清單。dry-run 只印不送，通道建不起來就退回 console。"""
    if getattr(args, "dry_run", False):
        diagnostics.green("dry-run：已產出一週內容，未發送通知")
        print(summary)
        return False
    channel = getattr(args, "notify", "console")
    try:
        notifier = Notifier(channel=channel)
    except NotifierError as exc:
        diagnostics.amber(
            symptom=f"通知管道 {channel} 無法建立：{exc}",
            fix="已改用 console 輸出；檢查憑證與 channel 名稱",
        )
        notifier = Notifier(channel="console")
    return notifier.send(summary, subject="一週社群內容草稿待審")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    # exit_on_red 讓測試能用 RedAlert 例外驗證紅色路徑，正式執行維持直接退出
    diagnostics = Diagnostics(
        MODULE_NAME, exit_on_red=bool(getattr(args, "exit_on_red", True))
    )
    config = load_config(args.config)
    content = config.get("content") or {}
    profiles = _build_profiles(config, diagnostics)
    gate = _build_gate(config.get("runtime") or {}, diagnostics)
    brief = load_json_file(_resolve(content.get("brief_file", "mock/brief.json")))
    brand_voice = load_json_file(
        _resolve(content.get("brand_voice_file", "mock/brand_voice.json"))
    )
    client = LLMClient(
        mock=not bool(getattr(args, "live", False)), context_note=CONTEXT_NOTE
    )
    week = generate_week(
        client,
        profiles,
        brief,
        brand_voice,
        _DEMO_DIR,
        max_attempts=int(content.get("max_regeneration_attempts", 2)),
        min_tone_examples=int(content.get("min_tone_examples", 3)),
    )
    _apply_autonomy(week["posts"], gate)
    result = _assemble_result(config, week, gate, brief)
    for warning in result["warnings"]:
        diagnostics.amber(symptom=warning, fix="人工審閱時處理，確認後再排程")
    result["amber_count"] = diagnostics.amber_count
    result["summary_text"] = format_summary(result)
    result["notified"] = _deliver(result["summary_text"], args, diagnostics)
    return result


def main() -> int:
    """解析參數 -> run() -> 印出結果 -> 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except (GeneratorError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1
    print(
        f"\n✅ 完成：{result['total_posts']} 則貼文、"
        f"{len(result['platforms'])} 個平台、"
        f"待審草稿 {result['drafts']} 則、警告 {len(result['warnings'])} 則"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
