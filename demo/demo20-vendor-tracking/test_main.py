"""demo20 供應商通訊與訂單追蹤 —— 3 個測試（happy / edge / integration）。

edge case 挑的是本模組最核心的兩件事：**逾期偵測**與**多幣別金額不混加**。
時區一律以固定 UTC 偏移注入，測試不依賴這台機器有沒有安裝 tzdata
（Windows 沒有系統 tz database，`ZoneInfo("Asia/Taipei")` 在裸環境會失敗）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import yaml

_DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.llm_client import LLMError  # noqa: E402


def _load(module_name: str, filename: str):
    """以絕對路徑載入本 demo 的模組。

    10 個 demo 都有同名的 main.py，一次跑整個 demo/ 目錄時
    plain import 會抓到別的 demo 的同名模組，因此這裡固定綁死檔案路徑。
    """
    spec = importlib.util.spec_from_file_location(module_name, _DEMO_DIR / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# 順序不可調換：main 內部會 import orders / supplier_mail，必須先佔住 sys.modules
orders = _load("orders", "orders.py")
supplier_mail = _load("supplier_mail", "supplier_mail.py")
tracker = _load("main", "main.py")

FIXED_TZ = timezone(timedelta(hours=8))
THRESHOLDS = {
    "unacknowledged_po_hours": 24,
    "chase_before_eta_hours": 48,
    "ship_before_eta_hours": 72,
    "deliver_after_eta_grace_hours": 24,
}
TEST_SUPPLIERS = [
    {"id": "acme", "name": "Acme Works", "domain": "@acme.example",
     "contact": "sales@acme.example"},
]


def _args(tmp_path: Path, config_path: Path | None = None, extra: list[str] | None = None):
    """組出離線模式的 CLI 參數，狀態檔一律指向 tmp 避免污染 repo"""
    argv = ["--mock", "--state-file", str(tmp_path / "orders.json")]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return tracker.build_parser().parse_args(argv + (extra or []))


def _by_po(items: list[dict]) -> dict[str, dict]:
    """把結果清單依 po_number 轉成 dict 方便斷言"""
    return {item["po_number"]: item for item in items}


def _make_track(po_number: str, *, currency: str, amount: str, placed_at: datetime,
                eta: datetime, stage: str) -> orders.OrderTrack:
    """組出一張測試用的 PO 追蹤結果（時區由呼叫端的 datetime 決定）"""
    registry = orders.build_supplier_registry(TEST_SUPPLIERS)
    order = orders.build_order(
        {
            "po_number": po_number, "supplier_id": "acme", "description": "測試品項",
            "amount": amount, "currency": currency,
            "placed_at": placed_at.isoformat(), "eta": eta.isoformat(),
        },
        registry, FIXED_TZ, ["JPY"],
    )
    return orders.OrderTrack(order=order, stage=stage)


def test_happy_path(tmp_path):
    """標準 mock 輸入：7 張 PO → 3 張逾期、1 張進入追蹤窗、3 封回覆解析失敗"""
    result = tracker.run(_args(tmp_path))

    assert result["module_id"] == "20"
    assert result["mode"] == "mock"
    assert result["dry_run"] is False
    assert len(result["orders"]) == 7

    # 狀態機：階段完全由供應商郵件推導，不由 ERP 欄位給定
    stages = {order["po_number"]: order["stage"] for order in result["orders"]}
    assert stages == {
        "PO-20260801": "shipped",
        "PO-20260805": "placed",
        "PO-20260812": "acknowledged",
        "PO-20260814": "placed",
        "PO-20260716": "delivered",
        "PO-20260722": "delivered",
        "PO-20260818": "acknowledged",
    }
    # 出貨通知帶來的改期要被採用（催辦對照的是供應商最新的承諾）
    assert _by_po(result["orders"])["PO-20260801"]["revised_eta"] == "2026-08-25T18:00:00+08:00"

    # 警報依嚴重度排序，最嚴重的逾期未到貨排第一
    assert [alert["po_number"] for alert in result["alerts"]] == [
        "PO-20260812", "PO-20260814", "PO-20260805", "PO-20260801",
    ]
    alerts = _by_po(result["alerts"])
    assert alerts["PO-20260812"]["kind"] == "overdue_delivery"
    assert alerts["PO-20260812"]["hours_late"] == "40.00"
    assert alerts["PO-20260801"]["kind"] == "pre_eta_reminder"
    assert alerts["PO-20260801"]["is_overdue"] is False
    assert result["overdue_count"] == 3

    # 催辦信預設一律草稿，且逐張 PO 都拿到 LLM 產出的信件本文
    chasers = _by_po(result["chasers"])
    assert set(chasers) == {"PO-20260812", "PO-20260814", "PO-20260805", "PO-20260801"}
    assert {c["status"] for c in result["chasers"]} == {"draft"}
    assert "PO-20260812" in chasers["PO-20260812"]["body"]

    # 計分卡：準時率一律對照原始承諾交期，改期不算準時
    scorecard = {row["supplier_name"]: row for row in result["scorecard"]}
    assert scorecard["Nordwind Components GmbH"]["on_time_rate"] == "100.0"
    assert scorecard["Delta Circuit Supply"]["on_time_rate"] == "0.0"
    assert scorecard["Nordwind Components GmbH"]["avg_acknowledgement_hours"] == "7.42"
    assert scorecard["Kaohsiung Precision Works"]["avg_acknowledgement_hours"] == "25.50"

    # 催辦紀錄已寫回狀態檔，明天同一張 PO 才不會被重複催
    state = json.loads(Path(result["state_file"]).read_text(encoding="utf-8"))
    assert set(state["chasers"]) == set(chasers)
    assert state["chasers"]["PO-20260812"]["count"] == 1


def test_edge_case_overdue_detection_and_multi_currency(tmp_path):
    """edge case：逾期偵測與多幣別。時區以固定偏移注入，不依賴環境的 tzdata。"""
    # 1. 時區載不到時退回固定偏移並留下警告，不靜默改用機器本地時間
    resolved_tz, warning = orders.resolve_timezone("Invalid/Zone_For_Test", 8)
    assert resolved_tz == FIXED_TZ
    assert warning is not None and "無法載入" in warning

    now = datetime(2026, 8, 24, 9, 0, tzinfo=FIXED_TZ)
    overdue = _make_track(
        "PO-900001", currency="USD", amount="8750.00",
        placed_at=datetime(2026, 8, 12, 9, 0, tzinfo=FIXED_TZ),
        eta=datetime(2026, 8, 22, 17, 0, tzinfo=FIXED_TZ), stage=orders.STAGE_ACKNOWLEDGED,
    )
    unacknowledged = _make_track(
        "PO-900002", currency="TWD", amount="486000.00",
        placed_at=datetime(2026, 8, 21, 13, 0, tzinfo=FIXED_TZ),
        eta=datetime(2026, 9, 5, 12, 0, tzinfo=FIXED_TZ), stage=orders.STAGE_PLACED,
    )
    on_schedule = _make_track(
        "PO-900003", currency="JPY", amount="1450000",
        placed_at=datetime(2026, 8, 18, 9, 0, tzinfo=FIXED_TZ),
        eta=datetime(2026, 9, 12, 12, 0, tzinfo=FIXED_TZ), stage=orders.STAGE_ACKNOWLEDGED,
    )

    hit = orders.evaluate_order(overdue, now, THRESHOLDS)
    assert hit is not None and hit.kind == orders.ALERT_OVERDUE_DELIVERY
    assert hit.hours_late == Decimal("40.00")

    late_ack = orders.evaluate_order(unacknowledged, now, THRESHOLDS)
    assert late_ack is not None and late_ack.kind == orders.ALERT_UNACKNOWLEDGED
    # 68 小時已過，扣掉 24 小時門檻 = 逾期 44 小時
    assert late_ack.hours_late == Decimal("44.00")

    # 剛好踩在門檻上不算逾期（判定用嚴格大於，避免整點誤報）
    exactly_on_limit = now - timedelta(hours=24)
    boundary = _make_track(
        "PO-900004", currency="TWD", amount="1000.00", placed_at=exactly_on_limit,
        eta=datetime(2026, 9, 30, 12, 0, tzinfo=FIXED_TZ), stage=orders.STAGE_PLACED,
    )
    assert orders.evaluate_order(boundary, now, THRESHOLDS) is None
    assert orders.evaluate_order(on_schedule, now, THRESHOLDS) is None

    # 2. 多幣別：四種幣別各自成列，絕不互相加總，JPY 不強加兩位小數
    totals = orders.totals_by_currency([overdue, unacknowledged, on_schedule], only_open=True)
    assert totals == {"JPY": "1450000", "TWD": "486000.00", "USD": "8750.00"}

    # 3. 完整流程的未結金額同樣依幣別分開（TWD 兩張相加 486000 + 92000）
    result = tracker.run(_args(tmp_path))
    assert result["outstanding_by_currency"] == {
        "EUR": "12480.00", "JPY": "1450000", "TWD": "578000.00", "USD": "8750.00",
    }
    assert Decimal(result["outstanding_by_currency"]["TWD"]) == Decimal("578000.00")


def test_integration_autonomy_downgrade_and_parse_failure_alerts(tmp_path, capsys):
    """與 _shared 的互動：supervised_auto 白名單降級、解析失敗轉 AMBER、console 送出"""
    config = yaml.safe_load((_DEMO_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"].update({
        "autonomy": "supervised_auto",
        "approved_senders": ["@delta-circuit.example"],
        "days_in_draft": 0,
        "notify_channel": "console",
    })
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # AutonomyGate 的降級規則本身
    gate = AutonomyGate(
        level=AutonomyLevel.SUPERVISED_AUTO,
        approved_senders=["@delta-circuit.example"],
        days_in_draft=0,
    )
    assert gate.effective_level("sales@delta-circuit.example") is AutonomyLevel.SUPERVISED_AUTO
    assert gate.effective_level("kaigai@osaka-metal.example") is AutonomyLevel.DRAFT
    assert gate.warnings, "未滿 14 天就開 supervised_auto 必須留下警告"

    result = tracker.run(_args(tmp_path, config_path))

    # 只有白名單內的供應商會自動送出，其餘一律降級為草稿
    statuses = {c["po_number"]: c["status"] for c in result["chasers"]}
    assert statuses["PO-20260812"] == "sent"
    assert statuses["PO-20260805"] == "draft"
    assert statuses["PO-20260814"] == "draft"
    # mock 模式不真的寄信，只做自主權判定
    assert all(c["is_transmitted"] is False for c in result["chasers"])
    assert result["warnings"], "自主權警告必須出現在 run() 的回傳值中"

    # 3 封解析失敗必須全部現形，不可被當成「這些 PO 沒有更新」
    failures = {f["email_id"]: f for f in result["parse_failures"]}
    assert set(failures) == {"e004", "e005", "e012"}
    assert failures["e004"]["kind"] == supplier_mail.FAILURE_NO_PO
    assert failures["e005"]["kind"] == supplier_mail.FAILURE_DOMAIN_MISMATCH
    assert failures["e012"]["kind"] == supplier_mail.FAILURE_UNCLASSIFIED
    # 冒名網域的「確認」不得寫進狀態機：該 PO 仍是未確認並被判逾期
    assert _by_po(result["orders"])["PO-20260805"]["stage"] == "placed"
    # 3 筆解析失敗 + 至少 1 筆「未滿 14 天」自主權警告
    assert result["amber_count"] >= 4

    # console 通道確實送出，且報告把解析失敗寫在標頭
    assert result["notify_channel"] == "console"
    assert result["notified"] is True
    printed = capsys.readouterr().out
    assert "3 封供應商回覆解析失敗" in printed
    assert "不等於「沒有更新」" in printed


def test_main_catches_llm_error(monkeypatch, capsys):
    """--live 模式下 LLM 呼叫逾時等狀況會拋 LLMError；main() 必須吃下來變成 exit code 1，
    而不是讓 raw traceback 砸給使用者（demo11 既有慣例的補齊）。
    """

    def _raise_llm_error(args):
        raise LLMError("模擬 CLI 逾時")

    monkeypatch.setattr(tracker, "run", _raise_llm_error)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    exit_code = tracker.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "錯誤：" in captured.err
    assert "模擬 CLI 逾時" in captured.err
