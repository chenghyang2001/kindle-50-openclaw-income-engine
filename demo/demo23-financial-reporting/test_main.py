"""demo23 測試（契約 §8：happy / edge / integration 三個）。

全部離線執行，不呼叫任何真實 API、不寫進模組目錄（狀態檔與稽核檔一律導到 tmp_path）。
時間一律注入固定值（`main.current_time` 被 monkeypatch），時區用 `datetime.timezone`
的固定偏移而不是系統時區——測試結果不可以隨執行機器的地理位置改變。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import audit as audit_mod  # noqa: E402
import board_pack as pack_mod  # noqa: E402
import main  # noqa: E402
from _shared.diagnostics import Diagnostics  # noqa: E402
from sources import PnLLine, SourceError, SourceFacts, quantize_money, to_decimal  # noqa: E402

#: 固定時區（UTC+8 = 台北）。刻意不用 ZoneInfo("Asia/Taipei")：
#: Windows 未安裝 tzdata 時會找不到該時區，測試不該因為執行環境而失敗。
FIXED_TZ = timezone(timedelta(hours=8))
FIXED_NOW = datetime(2026, 8, 1, 9, 0, 0, tzinfo=FIXED_TZ)

FD_EMAIL = "fd@example.com"


def _args(tmp_path: Path, **overrides) -> argparse.Namespace:
    """組出 run() 需要的 Namespace，預設走 mock + console + 暫存狀態/稽核檔。"""
    base = {
        "mock": True,
        "dry_run": False,
        "notify": "console",
        "config": str(MODULE_DIR / "config.yaml"),
        "state_file": str(tmp_path / "approval.json"),
        "audit_file": str(tmp_path / "audit.jsonl"),
        "approve_as": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _freeze(monkeypatch, moment: datetime) -> None:
    """把 main.current_time 固定在指定時間（測試注入固定時區與時間）。"""
    monkeypatch.setattr(main, "current_time", lambda tz: moment)


def _events(path: Path) -> list[str]:
    """讀回稽核 JSONL 的事件代碼清單。"""
    return [entry["event"] for entry in audit_mod.read_events(path)]


def test_happy_path(monkeypatch, tmp_path):
    """財務總監核准後的完整流程：變異數正確、流動性警報觸發、三情境預測產出、准許發布。"""
    _freeze(monkeypatch, FIXED_NOW)
    result = main.run(_args(tmp_path, approve_as=FD_EMAIL))

    assert result["module_id"] == "23"
    assert result["mode"] == "mock"
    assert result["period"] == "2026-07"
    assert result["is_partial"] is False

    # 五行合計：營收 550,700 - 銷貨成本 96,400 - 營業費用 383,450 = 淨利 70,850
    totals = result["board_pack"]["totals"]
    assert totals[pack_mod.TOTAL_REVENUE]["actual"] == "550700.00"
    assert totals[pack_mod.TOTAL_OPEX]["actual"] == "383450.00"
    assert totals[pack_mod.NET_PROFIT]["actual"] == "70850.00"
    assert totals[pack_mod.NET_PROFIT]["variance_amount"] == "-44150.00"
    assert totals[pack_mod.NET_PROFIT]["variance_pct"] == "-38.4"

    # Variance > 5% 的科目：4100 / 5000 / 6200 / 6900 四項
    material = [row["code"] for row in result["board_pack"]["lines"] if row["is_material"]]
    assert material == ["4100", "5000", "6200", "6900"]

    # 期末現金 1,165,700 ÷（598,500 ÷ 30）= 58.4 天 < 60 天門檻 → 流動性警報
    cashflow = result["board_pack"]["cashflow"]
    assert cashflow["days_of_cash"] == "58.4"
    assert cashflow["is_liquidity_alert"] is True
    assert "LIQUIDITY ALERT" in result["report_text"]

    # 三情境 × 12 個月；Base 月營收 = MRR 412,500 +（加權管道 1,660,000 ÷ 12）× 1.0
    scenarios = result["forecast"]["scenarios"]
    assert [item["name"] for item in scenarios] == ["base", "upside", "downside"]
    assert [item["pipeline_conversion"] for item in scenarios] == ["1.0", "1.2", "0.8"]
    assert len(scenarios[0]["months"]) == 12
    assert scenarios[0]["months"][0]["label"] == "2026-08"
    assert scenarios[0]["months"][0]["revenue"] == "550833.33"
    assert scenarios[0]["months"][0]["profit"] == "70983.33"

    # 核准後才准對董事會發布，且浮水印不得出現
    assert result["approval"]["status"] == "approved"
    assert result["approval"]["is_dispatch_allowed"] is True
    assert result["approval"]["approved_by"] == FD_EMAIL
    assert "草稿・待財務總監審核" not in result["report_text"]
    assert result["delivery"]["recipients"] == [
        "chair@example.com",
        "director-a@example.com",
        "director-b@example.com",
    ]

    # 稽核軌跡：核准與發布都必須留痕
    events = _events(Path(result["audit_file"]))
    assert audit_mod.EVENT_APPROVAL_GRANTED in events
    assert audit_mod.EVENT_PACK_GENERATED in events
    assert audit_mod.EVENT_DISPATCH in events
    assert audit_mod.EVENT_DISPATCH_BLOCKED not in events


def test_edge_case_currency_and_precision():
    """邊界：不同幣別不可混加；金額全程 Decimal 且四捨五入採 ROUND_HALF_UP。

    這裡刻意用 0.10 + 0.20：float 會得到 0.30000000000000004，
    在 12 個月滾動預測累加後就是董事會看得到的尾差。
    """
    diagnostics = Diagnostics("demo23-test", exit_on_red=False)
    usd = SourceFacts(
        source_id="xero",
        display_name="Xero",
        scope="accounting.transactions.read",
        currency="USD",
        pnl_lines=(
            PnLLine("4000", "小額營收 A", "revenue", Decimal("0.10")),
            PnLLine("4001", "小額營收 B", "revenue", Decimal("0.20")),
        ),
    )
    eur = SourceFacts(
        source_id="sage",
        display_name="Sage",
        scope="sales_invoices (all read)",
        currency="EUR",
        pnl_lines=(PnLLine("4100", "歐元營收", "revenue", Decimal("999999.99")),),
    )

    kept, rejected = pack_mod.enforce_single_currency([usd, eur], "USD", diagnostics)
    assert [fact.source_id for fact in kept] == ["xero"]
    assert [item.source_id for item in rejected] == ["sage"]
    assert "幣別不符" in rejected[0].reason

    pack = pack_mod.build_board_pack(kept, rejected, "2026-07", "USD", Decimal("5"), 60)
    total_revenue = pack.totals[pack_mod.TOTAL_REVENUE].actual
    assert isinstance(total_revenue, Decimal)
    assert total_revenue == Decimal("0.30")
    assert str(total_revenue) == "0.30"
    # 歐元金額不得以任何形式進入合計或輸出
    assert "999999.99" not in pack.to_json()
    assert pack.is_partial is True

    # 財務四捨五入必須是 ROUND_HALF_UP（銀行家捨入會得到 2.00）
    assert quantize_money(Decimal("2.005")) == Decimal("2.01")
    # float 金額一律拒收，強迫資料檔以字串儲存
    with pytest.raises(SourceError):
        to_decimal(0.1, "xero", "actual")


def test_integration_approval_gate_blocks_board(monkeypatch, tmp_path, capsys):
    """整合：未經財務總監核准 → 董事會拿不到報表；逾時 2 小時 SLA → 琥珀燈 + 稽核事件；
    稽核軌跡的雜湊鏈能抓出「竄改內容」與「整行刪除」兩種事後偽造。

    這是本模組最重要的保證，也涵蓋與 _shared 的互動（Diagnostics.amber、
    Notifier console、AutonomyGate 預設 draft）。
    """
    args = _args(tmp_path)

    # 第一次執行：從未送審 → 產生草稿並寫入待審狀態
    _freeze(monkeypatch, FIXED_NOW)
    first = main.run(args)
    assert first["approval"]["status"] == "pending"
    assert first["approval"]["reason"] == "no_state"
    assert first["approval"]["is_dispatch_allowed"] is False
    assert "草稿・待財務總監審核" in first["report_text"]
    # 董事會信箱一封都不會收到，只有財務總監收到草稿
    assert first["delivery"]["recipients"] == [FD_EMAIL]

    # 三小時後重跑同一份數字：審核 SLA（< 2 小時）已逾時
    _freeze(monkeypatch, FIXED_NOW + timedelta(hours=3))
    second = main.run(args)
    assert second["approval"]["reason"] == "awaiting_fd"
    assert second["approval"]["is_dispatch_allowed"] is False
    assert second["approval"]["requested_at"] == first["approval"]["requested_at"]
    assert second["amber_count"] >= 1

    events = _events(Path(second["audit_file"]))
    assert audit_mod.EVENT_SCOPE_VERIFIED in events
    assert audit_mod.EVENT_APPROVAL_REQUESTED in events
    assert audit_mod.EVENT_DISPATCH_BLOCKED in events
    assert audit_mod.EVENT_APPROVAL_SLA_BREACHED in events
    assert audit_mod.EVENT_DISPATCH not in events

    # 報表仍實際輸出到 console（不是靜默失敗），且帶著草稿浮水印
    assert "草稿・待財務總監審核" in capsys.readouterr().out

    # ── 稽核軌跡雜湊鏈 ──
    # 1) 正常執行（含跨兩次執行續接）後，整條鏈必須完整
    audit_path = Path(second["audit_file"])
    assert audit_mod.verify_file(audit_path) == []
    assert audit_mod.first_broken_line(audit_path) is None

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) >= 6

    # 2) 竄改攻擊：改掉第 3 行的內容但不改它的 entry_hash
    #    （偽造 approved_by / 時間戳的典型手法——這正是核准紀錄的後門）
    tampered_path = tmp_path / "tampered.jsonl"
    forged = json.loads(lines[2])
    forged["actor"] = "attacker@example.com"
    tampered_path.write_text(
        "\n".join(lines[:2] + [json.dumps(forged, ensure_ascii=False)] + lines[3:]) + "\n",
        encoding="utf-8",
    )
    problems = audit_mod.verify_file(tampered_path)
    assert problems, "內容被竄改卻沒有被偵測到"
    assert "第 3 行" in problems[0]
    assert "entry_hash" in problems[0]
    assert audit_mod.first_broken_line(tampered_path) == 3

    # 3) 刪除攻擊：整行砍掉第 3 行（想讓某個事件從稽核中消失）
    deleted_path = tmp_path / "deleted.jsonl"
    deleted_path.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
    problems = audit_mod.verify_file(deleted_path)
    assert problems, "整行刪除卻沒有被偵測到"
    assert "prev_hash 接不上" in problems[0]
    assert audit_mod.first_broken_line(deleted_path) == 3

    # 4) 記憶體中的鏈驗證 + Decimal 正規化（寫入與驗證必須用同一套正規化）
    fresh = audit_mod.AuditLog(tmp_path / "chain.jsonl", "demo23-test", FIXED_TZ)
    fresh.record("t1", {"amount": Decimal("0.30")})
    fresh.record("t2", {"nested": {"values": [Decimal("2.005"), "x"]}})
    assert fresh.verify_chain() == []
    assert fresh.verify_file() == []
