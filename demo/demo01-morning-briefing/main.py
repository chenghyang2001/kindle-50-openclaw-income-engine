"""demo01 — 晨間情報簡報（The OpenClaw Income Engine 第 03/04 章）。

流程：06:00 抓行事曆 / 信件 / 新聞 → Claude 統整成單一結構化簡報 →
06:30 發送 → 收件者 90 秒讀完。

三個不可妥協的設計：
1. **行事曆權重最高**：今日行程決定排序，行事曆失敗一律 RED 中止。
2. **90 秒法則**：目標 280–320 字，超過 400 字觸發 AMBER ``briefing_too_long``。
3. **30 分鐘緩衝**：``execute_at`` 與 ``deliver_at`` 絕不可設在同一分鐘，
   間隔低於 ``min_buffer_minutes`` 觸發 AMBER ``delayed_briefing``。

自主權：``READ_ONLY``。本模組只產出簡報，永遠不代替使用者回信或對外承諾。
"""

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
# 先掛上 demo/（取得 _shared）與模組自身目錄（取得 sources），禁止硬編碼絕對路徑。
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import LLMClient  # noqa: E402
from _shared.notifier import Notifier, NotifierError  # noqa: E402
from sources import SourceError, fetch_events, fetch_headlines, fetch_messages  # noqa: E402

MODULE_NAME = "demo01-morning-briefing"
DEFAULT_CONFIG_PATH = MODULE_DIR / "config.yaml"
DEFAULT_SECTIONS = ("HEADLINE", "TOP_3_PRIORITIES", "KEY_MEETINGS", "KPI_DELTA", "NEWS_ITEMS")
# LLMClient 離線時回傳的佔位字串前綴，用來判斷要不要改走本地範本。
MOCK_PLACEHOLDER_PREFIX = "[MOCK]"
# 用來查詢自主權層級的代表收件人；READ_ONLY 下對任何人都不得發送。
OWNER_PLACEHOLDER = "owner@example.com"
IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}
EMPTY_MARK = "無"

# 字數計算：中日文一字算一字，連續英數字串算一個字，標點不計入。
# 範圍依序為：CJK 擴充 A、CJK 基本區、日文平假名與片假名。
CJK_PATTERN = re.compile("[㐀-䶿一-鿿぀-ヿ]")
ASCII_WORD_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+\-]*")


@dataclass
class BriefingContext:
    """整趟流程共用的執行脈絡，避免每個函式都要傳一長串參數。"""

    config: dict
    base_dir: Path
    is_mock: bool
    diagnostics: Diagnostics
    generated_at: datetime


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（介面依 CONTRACT.md §6 統一）。"""
    parser = argparse.ArgumentParser(
        prog=MODULE_NAME,
        description="晨間情報簡報：抓行事曆／信件／新聞，產出 90 秒讀完的結構化簡報",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--mock", action="store_true", help="離線模式（預設）：只讀 mock/*.json，零憑證零網路"
    )
    mode_group.add_argument(
        "--live", action="store_true", help="串真實 API；缺憑證會明確報錯，不會偷偷退回 mock"
    )
    parser.add_argument("--dry-run", action="store_true", help="跑完整流程但不實際發送")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default=None,
        help="發送通道，未指定時採用 config 的 runtime.notify_channel",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG_PATH), help="設定檔路徑，預設為同目錄 config.yaml"
    )
    return parser


def resolve_mock(args: argparse.Namespace) -> bool:
    """只有明確指定 --live 才走真實 API，其餘一律離線。"""
    return not bool(getattr(args, "live", False))


def resolve_channel(args: argparse.Namespace, config: dict) -> str:
    """命令列 --notify 優先，其次讀設定檔，最後退回 console。"""
    runtime_config = config.get("runtime", {}) or {}
    return str(getattr(args, "notify", None) or runtime_config.get("notify_channel", "console"))


# --------------------------------------------------------------------------- #
# 排程緩衝（30 分鐘鐵律）
# --------------------------------------------------------------------------- #
def parse_clock(value: str) -> int:
    """把 HH:MM 轉成當日分鐘數。"""
    try:
        parsed = datetime.strptime(str(value).strip(), "%H:%M")
    except ValueError as exc:
        raise ValueError(f"時間格式必須是 HH:MM，收到：{value!r}") from exc
    return parsed.hour * 60 + parsed.minute


def schedule_buffer_minutes(execute_at: str, deliver_at: str) -> int:
    """計算執行到發送之間的緩衝分鐘數；跨午夜自動補一天。"""
    delta = parse_clock(deliver_at) - parse_clock(execute_at)
    return delta + 24 * 60 if delta < 0 else delta


def check_schedule(schedule_config: dict, diagnostics: Diagnostics) -> int:
    """驗證 30 分鐘緩衝：抓取與發送設在同一分鐘等於保證送出空簡報。"""
    execute_at = str(schedule_config.get("execute_at", "06:00"))
    deliver_at = str(schedule_config.get("deliver_at", "06:30"))
    minimum = int(schedule_config.get("min_buffer_minutes", 20) or 20)
    buffer_minutes = schedule_buffer_minutes(execute_at, deliver_at)
    if buffer_minutes < minimum:
        diagnostics.amber(
            symptom="delayed_briefing",
            fix=(
                f"execute_at({execute_at}) 與 deliver_at({deliver_at}) 只差 {buffer_minutes} 分鐘，"
                f"低於 {minimum} 分鐘門檻；請把 execute_at 提早並開啟 retry_on_timeout"
            ),
        )
    return buffer_minutes


# --------------------------------------------------------------------------- #
# 資料收集
# --------------------------------------------------------------------------- #
def build_context(args: argparse.Namespace) -> BriefingContext:
    """讀設定檔並組出執行脈絡。"""
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = (Path.cwd() / config_path).resolve()
    return BriefingContext(
        config=load_config(config_path),
        base_dir=config_path.parent,
        is_mock=resolve_mock(args),
        # exit_on_red=False：run() 不做 sys.exit，改讓 RedAlert 冒泡給 main() 決定退出碼。
        diagnostics=Diagnostics(MODULE_NAME, exit_on_red=False),
        generated_at=datetime.now(),
    )


def collect_sources(context: BriefingContext) -> dict:
    """依序抓三個來源；行事曆放第一個，失敗即中止整份簡報。"""
    sources_config = context.config.get("sources", {}) or {}
    shared_args = (context.base_dir, context.is_mock, context.diagnostics)
    return {
        "calendar": fetch_events(sources_config.get("calendar", {}) or {}, *shared_args),
        "email": fetch_messages(sources_config.get("email", {}) or {}, *shared_args),
        "news": fetch_headlines(sources_config.get("news", {}) or {}, *shared_args),
    }


def apply_calendar_weighting(bundle: dict) -> None:
    """行事曆權重最高的具體落實：寄件者出現在今日與會名單就升級為 VIP。

    這一步讓 live 模式不必另外維護 VIP 名單——今天要跟你開會的人，
    今天寄的信本來就比其他信重要。
    """
    attendees = {
        person.lower() for event in bundle["calendar"] for person in event["attendees"] if person
    }
    domains = {person.split("@")[-1] for person in attendees if "@" in person}
    for message in bundle["email"]:
        sender = message["from"].lower()
        if sender in attendees or (message["domain"] and message["domain"] in domains):
            message["is_vip"] = True


# --------------------------------------------------------------------------- #
# 五個輸出區塊
# --------------------------------------------------------------------------- #
def build_sections(bundle: dict, config: dict) -> dict:
    """組出 5 個固定區塊，順序不可調換。"""
    briefing_config = config.get("briefing", {}) or {}
    max_priorities = int(briefing_config.get("max_priorities", 3) or 3)
    max_meetings = int(briefing_config.get("max_meetings", 3) or 3)
    max_news = int(briefing_config.get("max_news", 3) or 3)
    return {
        "HEADLINE": [build_headline(bundle)],
        "TOP_3_PRIORITIES": build_priorities(bundle, max_priorities),
        "KEY_MEETINGS": build_meetings(bundle["calendar"], max_meetings),
        "KPI_DELTA": build_kpi_delta(config.get("kpi", {}) or {}),
        "NEWS_ITEMS": build_news(bundle["news"], max_news),
    }


def build_headline(bundle: dict) -> str:
    """一句話定調今天，最多 30 字。"""
    events = bundle["calendar"]
    pending = sum(1 for msg in bundle["email"] if msg["is_vip"] and msg["needs_reply"])
    if not events:
        return f"今日無排定會議，先清掉 {pending} 封待回關鍵信件。"
    first = events[0]
    return f"今天 {len(events)} 場會議，重心在 {first['start']} {first['title']}；{pending} 封關鍵信待回。"


def build_priorities(bundle: dict, limit: int) -> list[str]:
    """TOP_3_PRIORITIES：先排要準備的高重要性會議，再補待回的關鍵信件。"""
    items: list[str] = []
    for event in bundle["calendar"]:
        if len(items) >= limit:
            break
        if event["importance"] == "high" and event["prep_note"]:
            items.append(f"{event['start']} 前備妥：{event['prep_note']}")
    for message in bundle["email"]:
        if len(items) >= limit:
            break
        if message["is_vip"] and message["needs_reply"]:
            items.append(f"回覆 {message['from']}：{message['summary']}")
    if not items:
        items.append("今日無急迫項目，投入一項推進中的長期工作。")
    return items[:limit]


def build_meetings(events: list[dict], limit: int) -> list[str]:
    """KEY_MEETINGS：依時間排序，最多 limit 場，附上要先確認的事。"""
    lines: list[str] = []
    for event in events[:limit]:
        detail = event["location"] or EMPTY_MARK
        if event["prep_minutes"]:
            detail = f"{detail}，需 {event['prep_minutes']} 分準備"
        lines.append(f"{event['start']} {event['title']}｜{detail}")
    return lines


def build_kpi_delta(kpi_config: dict) -> list[str]:
    """KPI_DELTA：只寫變化量，沒變化的指標直接省略以節省字數。"""
    if not kpi_config.get("enabled", True):
        return []
    lines = [_format_metric(metric) for metric in kpi_config.get("metrics", []) or []]
    return [line for line in lines if line]


def _format_metric(metric: dict) -> str:
    """把單一指標格式化成「名稱：現值（↑變化量，判讀）」。"""
    try:
        current = float(metric.get("current", 0))
        previous = float(metric.get("previous", 0))
    except (TypeError, ValueError):
        return ""
    delta = current - previous
    if delta == 0:
        return ""
    arrow = "↑" if delta > 0 else "↓"
    is_improving = (delta < 0) if metric.get("lower_is_better", False) else (delta > 0)
    verdict = "改善" if is_improving else "需注意"
    name = str(metric.get("name", ""))
    unit = str(metric.get("unit", ""))
    return f"{name}：{_format_number(current)} {unit}（{arrow}{_format_number(abs(delta))}，{verdict}）"


def _format_number(value: float) -> str:
    """整數就不要拖小數點，省字數。"""
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"


def build_news(items: list[dict], limit: int) -> list[str]:
    """NEWS_ITEMS：影響力高的優先，最多 limit 則。"""
    ranked = sorted(items, key=lambda item: IMPACT_ORDER.get(item.get("impact", "low"), 3))
    return [f"{item['title']}｜{item.get('topic', '一般')}" for item in ranked[:limit]]


# --------------------------------------------------------------------------- #
# 簡報生成
# --------------------------------------------------------------------------- #
def load_prompt(path: Path) -> str:
    """讀取提示詞檔（提示詞是資產，一律獨立成檔不內嵌字串）。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(f"找不到提示詞檔案：{path}") from exc


def build_user_payload(bundle: dict, config: dict) -> str:
    """組出送給 Claude 的使用者訊息；行事曆放最前面（權重最高）。"""
    blocks = [
        "## 今日行事曆（權重最高，先看這段）",
        *_calendar_lines(bundle["calendar"]),
        "",
        "## 未讀信件",
        *_email_lines(bundle["email"]),
        "",
        "## KPI",
        *_kpi_lines(config),
        "",
        "## 新聞",
        *_news_lines(bundle["news"]),
    ]
    return "\n".join(blocks)


def _calendar_lines(events: list[dict]) -> list[str]:
    """把行事曆事件攤成給模型看的純文字行。"""
    if not events:
        return [f"- {EMPTY_MARK}"]
    return [
        f"- {event['start']}-{event['end']} {event['title']}"
        f"｜重要性 {event['importance']}"
        f"｜與會 {'、'.join(event['attendees']) or EMPTY_MARK}"
        f"｜準備 {event['prep_note'] or EMPTY_MARK}"
        for event in events
    ]


def _email_lines(messages: list[dict]) -> list[str]:
    """把信件攤成給模型看的純文字行，VIP 與待回旗標直接標出來。"""
    if not messages:
        return [f"- {EMPTY_MARK}"]
    return [
        f"- [{'VIP' if msg['is_vip'] else '一般'}]"
        f"[{'待回' if msg['needs_reply'] else '免回'}] "
        f"{msg['from']}｜{msg['subject']}｜{msg['summary']}"
        for msg in messages
    ]


def _kpi_lines(config: dict) -> list[str]:
    """把 KPI 變化量攤成給模型看的純文字行。"""
    lines = build_kpi_delta(config.get("kpi", {}) or {})
    if not lines:
        return [f"- {EMPTY_MARK}"]
    return [f"- {line}" for line in lines]


def _news_lines(items: list[dict]) -> list[str]:
    """把新聞攤成給模型看的純文字行。"""
    if not items:
        return [f"- {EMPTY_MARK}"]
    return [f"- [{item.get('impact', 'low')}] {item['source']}：{item['title']}" for item in items]


def produce_briefing_text(context: BriefingContext, bundle: dict, sections: dict) -> str:
    """呼叫 Claude 產生簡報。"""
    briefing_config = context.config.get("briefing", {}) or {}
    llm = LLMClient(
        mock=context.is_mock,
        model=str(briefing_config.get("llm_model", "claude-sonnet-5")),
        context_note=briefing_config.get("context_note"),
    )
    prompt_path = context.base_dir / str(briefing_config.get("prompt_file", "prompts/briefing.md"))
    text = llm.complete(
        system=load_prompt(prompt_path),
        user=build_user_payload(bundle, context.config),
        max_tokens=int(briefing_config.get("llm_max_tokens", 1200) or 1200),
    )
    if context.is_mock and text.lstrip().startswith(MOCK_PLACEHOLDER_PREFIX):
        # LLMClient 離線時回傳的是佔位字串，對示範 90 秒法則沒有價值，
        # 因此改用本地範本渲染出真正的 5 區塊簡報。這是離線模式的明示行為，
        # 不是靜默降級：--live 缺憑證時仍會 RED 中止，絕不會走到這裡。
        return render_offline_briefing(sections, context.config, context.generated_at)
    return text


def render_offline_briefing(sections: dict, config: dict, generated_at: datetime) -> str:
    """離線模式的本地渲染器，輸出與提示詞要求完全相同的 5 區塊結構。"""
    briefing_config = config.get("briefing", {}) or {}
    read_seconds = int(briefing_config.get("read_seconds", 90) or 90)
    deliver_at = str((config.get("schedule", {}) or {}).get("deliver_at", "06:30"))
    lines = [f"晨間情報簡報 ｜ {generated_at:%Y-%m-%d} {deliver_at}（{read_seconds} 秒讀完）", ""]
    for name in briefing_config.get("sections", list(DEFAULT_SECTIONS)):
        lines.append(str(name))
        lines.extend(_render_entries(str(name), sections.get(str(name), [])))
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _render_entries(section_name: str, entries: list[str]) -> list[str]:
    """區塊內容的排版：標題句不加符號、優先事項編號、其餘用短橫線。"""
    if not entries:
        return [EMPTY_MARK]
    if section_name == "HEADLINE":
        return list(entries)
    if section_name == "TOP_3_PRIORITIES":
        return [f"{index}. {text}" for index, text in enumerate(entries, start=1)]
    return [f"- {text}" for text in entries]


# --------------------------------------------------------------------------- #
# 90 秒法則
# --------------------------------------------------------------------------- #
def count_words(text: str) -> int:
    """計算簡報字數：中日文一字算一字，連續英數字串算一個字，標點不計。"""
    return len(CJK_PATTERN.findall(text)) + len(ASCII_WORD_PATTERN.findall(text))


def enforce_word_limit(text: str, briefing_config: dict, diagnostics: Diagnostics) -> dict:
    """90 秒法則檢查；超過硬上限觸發 AMBER ``briefing_too_long``。"""
    word_count = count_words(text)
    hard_limit = int(briefing_config.get("hard_word_limit", 400) or 400)
    target_min = int(briefing_config.get("target_word_min", 280) or 280)
    target_max = int(briefing_config.get("target_word_max", 320) or 320)
    if word_count > hard_limit:
        diagnostics.amber(
            symptom="briefing_too_long",
            fix=(
                f"本次輸出 {word_count} 字，超過 {hard_limit} 字硬上限；"
                f"提示詞需強制「最高 {target_max} 字，無情刪減」並重跑"
            ),
        )
    return {
        "word_count": word_count,
        "target_word_min": target_min,
        "target_word_max": target_max,
        "is_within_hard_limit": word_count <= hard_limit,
        "is_within_target_band": target_min <= word_count <= target_max,
    }


# --------------------------------------------------------------------------- #
# 自主權與發送
# --------------------------------------------------------------------------- #
def build_gate(runtime_config: dict, diagnostics: Diagnostics) -> AutonomyGate:
    """本模組固定 READ_ONLY：只產簡報，不代替使用者回覆任何來源。"""
    gate = AutonomyGate(
        level=AutonomyLevel(str(runtime_config.get("autonomy", "read_only"))),
        approved_senders=list(runtime_config.get("approved_senders", []) or []),
        days_in_draft=int(runtime_config.get("days_in_draft", 0) or 0),
    )
    for warning in gate.warnings:
        diagnostics.amber(symptom="autonomy_warning", fix=warning)
    return gate


def deliver_briefing(
    text: str, channel: str, is_dry_run: bool, diagnostics: Diagnostics
) -> bool:
    """把簡報送出；``--dry-run`` 只跑流程不實際發送。"""
    if is_dry_run:
        diagnostics.green(f"dry-run：略過發送（原定通道 {channel}）")
        return False
    is_sent = Notifier(channel=channel).send(text, subject="晨間情報簡報")
    if not is_sent:
        diagnostics.amber(
            symptom="briefing_delivery_failed",
            fix=f"通道 {channel} 發送失敗，請檢查該通道的憑證環境變數後重試",
        )
    return is_sent


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def assemble_result(
    context: BriefingContext,
    bundle: dict,
    sections: dict,
    briefing_text: str,
    gate: AutonomyGate,
    length_report: dict,
    delivery: dict,
) -> dict:
    """組出結構化結果，供測試斷言與呼叫端使用。"""
    schedule_config = context.config.get("schedule", {}) or {}
    module_config = context.config.get("module", {}) or {}
    result = {
        "module_id": str(module_config.get("id", "01")),
        "module_name": str(module_config.get("name", "")),
        "mode": "mock" if context.is_mock else "live",
        "generated_at": context.generated_at.isoformat(timespec="seconds"),
        "execute_at": str(schedule_config.get("execute_at", "06:00")),
        "deliver_at": str(schedule_config.get("deliver_at", "06:30")),
        "source_counts": {name: len(items) for name, items in bundle.items()},
        "sections": sections,
        "briefing": briefing_text,
        "autonomy_level": gate.effective_level(OWNER_PLACEHOLDER).value,
        "can_auto_reply": gate.can_send(OWNER_PLACEHOLDER),
        "autonomy_warnings": list(gate.warnings),
        "amber_count": context.diagnostics.amber_count,
    }
    result.update(length_report)
    result.update(delivery)
    return result


def run(args: argparse.Namespace) -> dict:
    """執行主流程，回傳結果 dict。本函式不做 sys.exit（依 CONTRACT.md §6）。"""
    context = build_context(args)
    buffer_minutes = check_schedule(context.config.get("schedule", {}) or {}, context.diagnostics)
    gate = build_gate(context.config.get("runtime", {}) or {}, context.diagnostics)

    bundle = collect_sources(context)
    apply_calendar_weighting(bundle)
    sections = build_sections(bundle, context.config)

    briefing_text = produce_briefing_text(context, bundle, sections)
    length_report = enforce_word_limit(
        briefing_text, context.config.get("briefing", {}) or {}, context.diagnostics
    )

    channel = resolve_channel(args, context.config)
    is_dry_run = bool(getattr(args, "dry_run", False))
    delivery = {
        "notify_channel": channel,
        "is_dry_run": is_dry_run,
        "is_delivered": deliver_briefing(briefing_text, channel, is_dry_run, context.diagnostics),
        "schedule_buffer_minutes": buffer_minutes,
    }
    return assemble_result(context, bundle, sections, briefing_text, gate, length_report, delivery)


def summary_line(result: dict) -> str:
    """給操作者看的一行摘要（走 stderr，不污染 stdout 的簡報本文）。"""
    return (
        f"[{result['module_id']}] 模式={result['mode']}"
        f"｜字數={result['word_count']}（目標 {result['target_word_min']}-{result['target_word_max']}）"
        f"｜緩衝={result['schedule_buffer_minutes']} 分"
        f"｜AMBER={result['amber_count']}"
        f"｜發送={'是' if result['is_delivered'] else '否'}"
    )


def main() -> int:
    """解析參數 → run() → 輸出／發送 → 回傳 exit code。"""
    args = build_parser().parse_args()
    try:
        result = run(args)
    except RedAlert as exc:
        print(f"紅色警報：{exc}", file=sys.stderr)
        return 1
    except (SourceError, NotifierError, OSError, ValueError) as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 1

    # console 通道已由 Notifier 印到 stdout，這裡只補印非 console 或 dry-run 的情況，
    # 避免同一份簡報被印兩次。
    if result["is_dry_run"] or result["notify_channel"] != "console":
        print(result["briefing"])
    print(summary_line(result), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
