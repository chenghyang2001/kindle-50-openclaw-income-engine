"""快速啟動方案一鍵執行器 — 第 04 章「把 4 個自動化合併成 1 個體驗」。

依客戶的早晨時序跑完四個模組：#1 早報 → #2 收件匣 → #9 報表 → #5 評價，
最後輸出一份**客戶視角**的整合摘要，而不是四份工程 log。

三個不可妥協的設計：

1. **模組隔離載入**。``demo01-morning-briefing`` 與 ``demo09-sales-report``
   各自有一個名為 ``sources`` 的頂層套件。若用 ``sys.path.insert`` + ``import main``
   依序載入，``sys.modules`` 的快取會讓先載入的 ``sources`` 勝出，
   後載入的模組拿到的是**別人的資料源**——程式不會報錯，只會產出錯誤資料。
   因此一律以 ``importlib.util.spec_from_file_location`` 搭配唯一模組名載入，
   並在載入結束後把該 demo 目錄下的模組全部逐出 ``sys.modules``。

2. **部分失敗不中斷**。任一模組拋例外（甚至 ``Diagnostics`` 紅色警報觸發的
   ``sys.exit``）都只記為該模組 ``status="failed"``，其餘模組照跑。
   客戶要知道「四件事裡有一件沒做」，而不是整個早晨一片空白。

3. **鍵名防禦性正規化**。四個模組的 ``run()`` 回傳鍵名並不統一
   （``module_id`` vs ``module``、``warnings`` vs ``autonomy_warnings``
   vs ``autonomy.warnings``），一律用 ``.get()`` 取值，絕不假設鍵存在。

用法：
    python run_all.py --mock                     # 零憑證、零網路
    python run_all.py --mock --notify telegram   # 四份輸出 + 整合摘要推 Telegram
    python run_all.py --dry-run                  # 跑完流程但不發送（客戶驗收前空跑）
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

BUNDLE_DIR = Path(__file__).resolve().parent
DEMO_ROOT = BUNDLE_DIR.parent

# 先掛 demo/（取得 _shared）與本目錄（取得 pricing_calculator），禁止硬編碼絕對路徑。
sys.path.insert(0, str(DEMO_ROOT))
sys.path.insert(0, str(BUNDLE_DIR))

from _shared.notifier import Notifier, NotifierError  # noqa: E402

from pricing_calculator import (  # noqa: E402
    QUICKSTART_MONTHLY,
    QUICKSTART_SETUP,
    format_money,
)

BUNDLE_NAME = "快速啟動方案（The Quick-Start Experience）"
SUMMARY_SUBJECT = "快速啟動方案｜今日整合摘要"
MODULE_MARKERS = ("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩")


@dataclass(frozen=True)
class BundleModule:
    """打包方案中的一個模組。``state_file_name`` 只有支援 --state-file 的模組才填。"""

    module_id: str
    module_name: str
    dir_name: str
    morning_slot: str
    state_file_name: str | None = None


# 執行順序刻意照客戶的早晨時序排，不是照模組編號排。
BUNDLE_MODULES: tuple[BundleModule, ...] = (
    BundleModule("01", "晨間情報簡報", "demo01-morning-briefing", "06:30 起床後第一眼"),
    BundleModule("02", "收件匣清零代理", "demo02-inbox-zero", "07:00 打開信箱前"),
    BundleModule("09", "每日銷售與進度報表", "demo09-sales-report", "07:30 咖啡還沒喝完"),
    BundleModule(
        "05", "客戶評價監控", "demo05-review-monitor", "08:00 出門前", ".seen.json"
    ),
)
MODULES_BY_ID: dict[str, BundleModule] = {item.module_id: item for item in BUNDLE_MODULES}


# --------------------------------------------------------------------------- #
# 模組隔離載入
# --------------------------------------------------------------------------- #
def is_within(path: Path, parent: Path) -> bool:
    """判斷檔案是否位於指定目錄底下（跨磁碟或無法解析時一律回 False）。"""
    try:
        return path.resolve().is_relative_to(parent.resolve())
    except (OSError, ValueError):
        return False


def purge_demo_modules(known_before: set[str], demo_dir: Path) -> None:
    """把本次載入帶進 ``sys.modules``、且檔案位於該 demo 目錄內的模組全部移除。

    這是整支腳本最關鍵的一段：不清掉的話，demo01 的 ``sources`` 會留在快取裡，
    demo09 再 ``import sources`` 時直接拿到行事曆／信件／新聞的抓取函式，
    而不是 CRM／Shopify／Stripe——報表會「成功」產出，數字卻完全是錯的。
    """
    for name in list(sys.modules):
        if name in known_before:
            continue
        origin = getattr(sys.modules.get(name), "__file__", None)
        if origin and is_within(Path(origin), demo_dir):
            del sys.modules[name]


def load_demo_main(spec: BundleModule) -> ModuleType:
    """以唯一模組名載入某個 demo 的 ``main.py``，載入後還原 sys.path 與 sys.modules。"""
    demo_dir = DEMO_ROOT / spec.dir_name
    main_path = demo_dir / "main.py"
    if not main_path.is_file():
        raise FileNotFoundError(f"找不到模組進入點：{main_path}")

    path_snapshot = list(sys.path)
    known_before = set(sys.modules)
    # demo 自己的 main.py 也會做 sys.path.insert，但這裡先掛好可確保
    # 它的區域套件（sources / classifier / monitor）在 exec 期間解析得到。
    sys.path.insert(0, str(DEMO_ROOT))
    sys.path.insert(0, str(demo_dir))
    try:
        return exec_module_from_path(f"bundle_demo{spec.module_id}_main", main_path)
    finally:
        sys.path[:] = path_snapshot
        purge_demo_modules(known_before, demo_dir)


def exec_module_from_path(unique_name: str, main_path: Path) -> ModuleType:
    """用唯一名稱載入指定路徑的模組。已持有回傳的物件，因此之後從快取移除也安全。"""
    module_spec = importlib.util.spec_from_file_location(unique_name, main_path)
    if module_spec is None or module_spec.loader is None:
        raise ImportError(f"無法建立模組規格：{main_path}")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[unique_name] = module
    module_spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 結果正規化（防禦性取值）
# --------------------------------------------------------------------------- #
def as_str_list(value: object) -> list[str]:
    """把任意值收斂成字串清單；不是清單就回空清單，不猜、不硬轉。"""
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def coerce_int(value: object) -> int:
    """把任意值收斂成整數；轉不動就回 0（缺這個鍵不該讓整份摘要爆掉）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def extract_warnings(payload: dict) -> list[str]:
    """從三個已知位置撈警告，撈不到就回空清單。

    鍵名對照（實際差異，非假設）：
      demo01 -> autonomy_warnings
      demo02 -> autonomy.warnings（巢狀）
      demo05 -> warnings
      demo09 -> 無警告鍵，改以 failed_sources 代表「這份報表只有部分資料」
    """
    found: list[str] = as_str_list(payload.get("autonomy_warnings"))
    found += as_str_list(payload.get("warnings"))
    autonomy = payload.get("autonomy")
    if isinstance(autonomy, dict):
        found += as_str_list(autonomy.get("warnings"))
    for failure in payload.get("failed_sources") or []:
        if isinstance(failure, dict):
            name = failure.get("display_name") or failure.get("source_id") or "未知資料源"
            found.append(f"資料源 {name} 無回應：{failure.get('reason', '未提供原因')}")
    return list(dict.fromkeys(found))


def normalize_result(module_id: str, raw: object) -> dict:
    """把各模組風格不一的回傳值收斂成統一結構。

    絕不假設任何鍵存在：``raw`` 可能是 dict、None，甚至是模組作者改過的其他型別。
    """
    payload = raw if isinstance(raw, dict) else {}
    spec = MODULES_BY_ID.get(str(module_id))
    fallback_name = spec.module_name if spec else ""
    return {
        "module_id": str(payload.get("module_id") or payload.get("module") or module_id),
        "module_name": str(payload.get("module_name") or fallback_name),
        "status": "ok",
        "warnings": extract_warnings(payload),
        "amber_count": coerce_int(payload.get("amber_count")),
        "raw": payload,
        "error": None,
    }


def failed_result(spec: BundleModule, error: str) -> dict:
    """單一模組失敗時的統一結構；欄位與 normalize_result 完全一致。"""
    return {
        "module_id": spec.module_id,
        "module_name": spec.module_name,
        "status": "failed",
        "warnings": [],
        "amber_count": 0,
        "raw": {},
        "error": error,
    }


# --------------------------------------------------------------------------- #
# 單一模組執行
# --------------------------------------------------------------------------- #
def build_module_argv(spec: BundleModule, args: argparse.Namespace) -> list[str]:
    """把 run_all 的旗標翻譯成各 demo 的 CLI 參數（介面依 CONTRACT.md §6）。"""
    argv = ["--live"] if getattr(args, "live", False) else ["--mock"]
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    argv += ["--notify", str(args.notify)]
    state_dir = getattr(args, "state_dir", None)
    if state_dir and spec.state_file_name:
        argv += ["--state-file", str(Path(state_dir).expanduser() / f"{spec.dir_name}{spec.state_file_name}")]
    return argv


def run_one_module(spec: BundleModule, args: argparse.Namespace) -> dict:
    """跑一個模組並回傳正規化結果。任何失敗都被吸收成 failed，絕不往外炸。"""
    try:
        module = load_demo_main(spec)
        parsed = module.build_parser().parse_args(build_module_argv(spec, args))
        return normalize_result(spec.module_id, module.run(parsed))
    except SystemExit as exc:
        # demo09 的 Diagnostics 預設 exit_on_red=True，紅色警報會直接 sys.exit。
        # 這裡攔下來，才不會讓一個模組的憑證問題把整個早晨帶走。
        return failed_result(spec, f"模組以退出碼 {exc.code} 中止（多半是紅色警報）")
    except Exception as exc:  # noqa: BLE001 — 部分失敗設計，見模組 docstring 第 2 點
        print(traceback.format_exc(), file=sys.stderr)
        return failed_result(spec, f"{type(exc).__name__}: {exc}")


def run_modules(args: argparse.Namespace) -> list[dict]:
    """依早晨時序依序執行四個模組。``--json`` 時把模組輸出導到 stderr 保持 stdout 純淨。"""
    results: list[dict] = []
    for spec in BUNDLE_MODULES:
        if getattr(args, "json", False):
            with contextlib.redirect_stdout(sys.stderr):
                results.append(run_one_module(spec, args))
        else:
            results.append(run_one_module(spec, args))
    return results


# --------------------------------------------------------------------------- #
# 客戶視角摘要
# --------------------------------------------------------------------------- #
def client_highlight(module_id: str, raw: dict) -> str:
    """把模組的原始回傳翻成一句客戶聽得懂的話。取不到值就講得保守一點。"""
    builders = {
        "01": _highlight_briefing,
        "02": _highlight_inbox,
        "09": _highlight_sales,
        "05": _highlight_reviews,
    }
    builder = builders.get(str(module_id))
    return builder(raw) if builder else "已完成。"


def _highlight_briefing(raw: dict) -> str:
    """demo01：一份 90 秒讀完的簡報。"""
    words = coerce_int(raw.get("word_count"))
    priorities = raw.get("sections", {})
    count = len(priorities.get("TOP_3_PRIORITIES", [])) if isinstance(priorities, dict) else 0
    return f"一份 {words} 字的簡報等著你，{count} 件事今天非做不可，90 秒讀完。"


def _highlight_inbox(raw: dict) -> str:
    """demo02：信箱已分類、重要信已有草稿。"""
    counts = raw.get("counts") if isinstance(raw.get("counts"), dict) else {}
    drafts = raw.get("drafts") if isinstance(raw.get("drafts"), list) else []
    return (
        f"{coerce_int(counts.get('total'))} 封信已分類"
        f"（重要 {coerce_int(counts.get('vip'))}／參考 {coerce_int(counts.get('fyi'))}"
        f"／垃圾 {coerce_int(counts.get('spam'))}），"
        f"{len(drafts)} 封回覆草稿已備好，等你按送出。"
    )


def _highlight_sales(raw: dict) -> str:
    """demo09：昨日營收已在手機裡。"""
    totals = raw.get("totals") if isinstance(raw.get("totals"), dict) else {}
    currency = str(raw.get("currency") or "USD")
    revenue = str(totals.get("revenue") or "0")
    partial = "（部分資料源無回應）" if raw.get("is_partial") else ""
    return (
        f"昨日營收 {currency} {revenue}，{coerce_int(totals.get('orders'))} 筆訂單，"
        f"報表已經在你手機裡{partial}。"
    )


def _highlight_reviews(raw: dict) -> str:
    """demo05：新評價已看過，該你出手的已挑出來。"""
    escalations = raw.get("escalations") if isinstance(raw.get("escalations"), list) else []
    drafts = raw.get("drafts") if isinstance(raw.get("drafts"), list) else []
    return (
        f"{coerce_int(raw.get('fetched'))} 則評價掃過，"
        f"{coerce_int(raw.get('new'))} 則是新的，"
        f"{len(drafts)} 份回覆草稿已擬好，"
        f"{len(escalations)} 則負評需要你親自出面。"
    )


def render_module_block(index: int, item: dict) -> list[str]:
    """單一模組在摘要中的區塊。"""
    marker = MODULE_MARKERS[index] if index < len(MODULE_MARKERS) else f"({index + 1})"
    spec = MODULES_BY_ID.get(str(item.get("module_id")))
    slot = spec.morning_slot if spec else ""
    if item.get("status") != "ok":
        return [f"{marker} {item.get('module_name', '')}｜{slot}", f"    ✗ 未完成：{item.get('error')}"]
    lines = [
        f"{marker} {item.get('module_name', '')}｜{slot}",
        f"    {client_highlight(str(item.get('module_id')), item.get('raw') or {})}",
    ]
    lines += [f"    ! {text}" for text in item.get("warnings") or []]
    return lines


def render_summary(result: dict) -> str:
    """客戶視角的整合摘要——這是要唸給客戶聽的，不是工程 log。"""
    lines = [
        "=" * 62,
        f"{BUNDLE_NAME}",
        f"你的週一早晨（{result['mode']} 模式"
        f"{'／空跑' if result['is_dry_run'] else ''}）",
        "=" * 62,
        "",
    ]
    for index, item in enumerate(result["modules"]):
        lines += render_module_block(index, item)
        lines.append("")
    lines += render_footer(result)
    return "\n".join(lines)


def render_footer(result: dict) -> list[str]:
    """收尾：合計數字 + 那句讓客戶點頭的話。"""
    lines = ["-" * 62]
    if result["failed_count"]:
        lines.append(
            f"注意：{result['failed_count']} 個模組執行失敗，"
            f"上方標示 ✗ 的項目今天沒有產出，請先修復再交付給客戶。"
        )
    lines.append(
        f"完成 {result['ok_count']}/{len(result['modules'])} 個模組"
        f"｜警示 {result['total_warnings']} 則"
        f"｜琥珀色診斷 {result['total_amber']} 次"
    )
    lines.append(
        f"方案定價：{format_money(QUICKSTART_SETUP, 'USD')} 設定費 + "
        f"{format_money(QUICKSTART_MONTHLY, 'USD')}/月"
    )
    lines.append("")
    lines.append("這就是你週一早晨會看到的東西——在你打開筆電之前，一切已經準備就緒。")
    lines.append("=" * 62)
    return lines


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    """建立命令列參數解析器（旗標與各 demo 一致，另加 --json / --state-dir）。"""
    parser = argparse.ArgumentParser(
        prog="run_all",
        description="一鍵跑完快速啟動方案的 4 個模組（#1 早報 / #2 收件匣 / #9 報表 / #5 評價）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true", default=True, help="離線模式，零憑證零網路（預設）")
    mode.add_argument("--live", action="store_true", help="串接真實 API（與 --mock 互斥）")
    parser.add_argument("--dry-run", action="store_true", help="跑完整流程但不實際發送")
    parser.add_argument(
        "--notify",
        choices=list(Notifier.SUPPORTED),
        default="console",
        help="各模組與整合摘要的發送通道，預設 console",
    )
    parser.add_argument("--json", action="store_true", help="輸出 JSON 而非人讀摘要")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="把模組的執行狀態檔（例如 demo05 的去重紀錄）改寫到指定目錄",
    )
    return parser


def run(args: argparse.Namespace) -> dict:
    """執行主流程並回傳結果 dict（供測試斷言）。不做 sys.exit。"""
    modules = run_modules(args)
    result = {
        "bundle": BUNDLE_NAME,
        "mode": "live" if getattr(args, "live", False) else "mock",
        "is_dry_run": bool(getattr(args, "dry_run", False)),
        "notify_channel": str(args.notify),
        "modules": modules,
        "ok_count": sum(1 for item in modules if item["status"] == "ok"),
        "failed_count": sum(1 for item in modules if item["status"] != "ok"),
        "total_warnings": sum(len(item["warnings"]) for item in modules),
        "total_amber": sum(item["amber_count"] for item in modules),
        "setup_price": str(QUICKSTART_SETUP),
        "monthly_price": str(QUICKSTART_MONTHLY),
    }
    result["summary_text"] = render_summary(result)
    return result


def deliver_summary(result: dict) -> bool:
    """把整合摘要送出。console 由 stdout 呈現、dry-run 不送，兩者都回 False。"""
    channel = str(result["notify_channel"])
    if result["is_dry_run"] or channel == "console":
        return False
    try:
        return Notifier(channel=channel).send(result["summary_text"], subject=SUMMARY_SUBJECT)
    except NotifierError as exc:
        print(f"整合摘要發送失敗（{channel}）：{exc}", file=sys.stderr)
        return False


def main() -> int:
    """解析參數 → run() → 輸出／發送 → 回傳 exit code（有模組失敗即回 1）。"""
    args = build_parser().parse_args()
    result = run(args)
    result["summary_delivered"] = deliver_summary(result)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(result["summary_text"])
    return 1 if result["failed_count"] else 0


if __name__ == "__main__":
    sys.exit(main())
