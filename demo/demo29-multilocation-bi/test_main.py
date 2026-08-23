"""demo29 測試（契約 §8：happy / edge / integration 三個）。

全部離線執行，不呼叫任何真實 API、不觸網。稽核檔與基準線狀態檔一律寫到
pytest 的 tmp_path，順便把 `--audit-file` / `--state-file` 兩個旗標一起驗過。

三個測試的分工：

* happy       — 總部 Full Pack：異質來源標準化、Composite Score、排名、部分失敗降級
* edge        — 未知角色 + 不存在的據點：退回最小權限 -> deny-all，且不得除以零
* integration — **店長視野的資料隔離**（本模組的核心保證）+ 與 `_shared` 的互動
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import audit  # noqa: E402
import main  # noqa: E402
import rollup  # noqa: E402
from access_control import Role, resolve_scope  # noqa: E402

#: 全網五個據點；隔離測試要確認店長看不到後面四個的任何痕跡。
OTHER_SITE_TOKENS = (
    "tpe-zhongshan", "台北中山店",
    "txg-fengjia", "台中逢甲店",
    "khh-boai", "高雄博愛店",
    "tnn-east", "台南東區店",
)


def _args(tmp_path: Path, **overrides) -> argparse.Namespace:
    """組出 run() 需要的 Namespace，預設走 mock + console + 暫存稽核／狀態檔。"""
    base = {
        "mock": True,
        "dry_run": False,
        "notify": "console",
        "config": str(MODULE_DIR / "config.yaml"),
        "role": None,
        "site_id": None,
        "actor": None,
        "state_file": str(tmp_path / "baselines.json"),
        "audit_file": str(tmp_path / "access.jsonl"),
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_happy_path_hq_full_pack(tmp_path):
    """總部視野：四家回報、一家無回應，仍算出全網加總、排名與異常並照常產出。"""
    result = main.run(_args(tmp_path, role="hq", actor="coo@example.com"))

    assert result["module_id"] == "29"
    assert result["mode"] == "mock"
    assert result["pack"] == "full_pack"
    assert result["access"]["role"] == "hq"

    # 三種異質來源（Shopify 淨額 / Square 最小貨幣單位 / POS 日結）標準化後可直接相加
    assert result["sites_reporting"] == 4
    assert result["sites_unavailable"] == 1
    assert result["network_totals"]["revenue"] == "4350000.00"
    assert result["network_totals"]["transactions"] == "11270"

    # Composite Score 與分級（相對自身 4 週基準線，基準線 = 100）
    by_site = {item["site_id"]: item for item in result["sites"]}
    assert by_site["tpe-xinyi"]["composite_score"] == "110.90"
    assert by_site["tpe-xinyi"]["tier"] == rollup.TIER_TOP
    assert by_site["khh-boai"]["composite_score"] == "102.04"
    assert by_site["tpe-zhongshan"]["composite_score"] == "96.92"
    assert by_site["txg-fengjia"]["composite_score"] == "86.20"
    assert by_site["txg-fengjia"]["tier"] == rollup.TIER_FOCUS
    assert by_site["tpe-xinyi"]["indices"]["revenue"] == "116.2"

    # 跨據點排名與同儕比較只在 Full Pack 出現
    assert [item["site_id"] for item in result["ranking"]] == [
        "tpe-xinyi", "khh-boai", "tpe-zhongshan", "txg-fengjia",
    ]
    assert result["ranking"][0]["vs_peer_median_pct"] == "+11.5"
    assert result["peer_median_composite"] == "99.48"

    # 偏離 4 週基準線 > 15% 的異常：信義營收 +16.2%、逢甲營收與人時營收各一條
    assert result["anomaly_count"] == 3
    assert len(by_site["txg-fengjia"]["anomalies"]) == 2

    # 部分失敗降級：報表照發、橫幅置頂、失敗據點仍列在明細中，且走琥珀燈不是紅燈
    assert result["is_partial"] is True
    assert result["failed_sites"][0]["site_id"] == "tnn-east"
    assert "⚠️ 部分資料：台南東區店 無回應" in result["report_text"]
    assert result["amber_count"] >= 1
    assert result["delivery"]["delivered"] is True
    assert result["leak_scan_passed"] is True

    # 只有總部視野會推進 4 週滾動基準線，且視窗只保留最近 4 週
    assert result["baseline_state"]["updated"] is True
    windows = rollup.load_baseline_windows(Path(result["baseline_state"]["path"]))
    assert windows["tpe-xinyi"]["revenue"][-1] == "2150000.00"
    assert len(windows["tpe-xinyi"]["revenue"]) == 4
    # 無回應的據點不得以缺值推進視窗，否則基準線會被一次故障永久污染
    assert windows["tnn-east"]["revenue"] == ["500000", "505000", "515000", "520000"]


def test_edge_case_unknown_role_falls_back_to_deny_all(tmp_path):
    """邊界：角色無法辨識 -> 一律拒絕存取，且不得除以零。

    兩個獨立情境都要驗，因為它們的失敗模式完全不同：
      A. 未知角色 + **有效**據點 -> 仍須 deny-all（`--site` 是呼叫者自己給的參數，
         角色都認不出來就無從得知他有權看哪一店，照著放行等於零驗證）
      B. 未知角色 + 不存在的據點 -> deny-all
    安全預設值必須是「什麼都看不到」；若任一情境放行，整個模組的賣點就沒了。
    """
    # --- 情境 A：未知角色 + 有效據點，仍然什麼都不給 ---
    probing = main.run(_args(tmp_path, role="auditor", site_id="tpe-xinyi"))

    assert probing["access"]["is_denied"] is True
    assert probing["access"]["role_was_unknown"] is True
    assert probing["access"]["visible_site_ids"] == []
    assert probing["sites"] == []
    assert probing["sites_reporting"] == 0
    # 有效店碼也不得換到任何一格數字
    assert "tpe-xinyi" not in probing["report_text"]
    assert "台北信義旗艦店" not in probing["report_text"]
    assert "2,150,000" not in probing["report_text"]
    # 拒絕訊息不可列出合法角色清單，否則等於告訴探測者正確答案
    denial = probing["access"]["denial_reason"]
    assert "無法辨識" in denial
    assert "site_manager" not in denial and "hq" not in denial

    # 這種呼叫可能是設定錯誤，也可能是探測行為 -> 必須在稽核軌跡留痕
    resolved = next(
        item for item in audit.read_events(Path(probing["audit"]["file"]))
        if item["event"] == audit.EVENT_ACCESS_RESOLVED
    )
    assert resolved["role_was_unknown"] is True
    assert resolved["visible_site_ids"] == []

    # --- 情境 B：未知角色 + 不存在的據點 ---
    result = main.run(_args(tmp_path, role="auditor", site_id="ghost-store"))

    assert result["access"]["role"] == Role.SITE_MANAGER.value
    assert result["access"]["is_denied"] is True
    assert result["access"]["visible_site_ids"] == []
    assert result["pack"] == "own_site_only"
    assert any("權限退回最小視野" in item for item in result["warnings"])

    # 空資料集不得炸掉，也不得生出任何數字
    assert result["sites"] == []
    assert result["sites_reporting"] == 0
    assert result["anomaly_count"] == 0
    assert result["network_totals"] is None
    assert result["ranking"] is None
    assert result["peer_median_composite"] is None
    assert result["baseline_state"]["updated"] is False

    # 拒絕存取時完全沒有可見據點名稱可洩漏
    for token in ("tpe-xinyi", "台北信義旗艦店", *OTHER_SITE_TOKENS):
        assert token not in result["report_text"]

    # 防零除：分母為 0 或無基準線時回 None，而不是 0（「沒有基準」不等於「持平」）
    assert rollup.safe_ratio(Decimal("100"), Decimal("0")) is None
    assert rollup.compute_composite({"revenue": None}, {"revenue": Decimal("40")}) is None
    assert rollup.classify_tier(None, Decimal("110"), Decimal("90")) == rollup.TIER_UNRATED

    # 未指定據點的店長同樣是 deny-all，不是「看全部」
    scope = resolve_scope("site_manager", None, "someone", ["tpe-xinyi"])
    assert scope.is_denied is True
    assert scope.can_see_ranking is False
    assert scope.role_was_unknown is False

    # 角色認不出來時，即使據點合法也不得進入店長的正常解析路徑
    unknown = resolve_scope("auditor", "tpe-xinyi", "someone", ["tpe-xinyi"])
    assert unknown.role_was_unknown is True
    assert unknown.is_denied is True
    assert unknown.visible_site_ids == frozenset()
    assert unknown.can_see_ranking is False


def test_integration_site_manager_sees_only_own_site(tmp_path, capsys):
    """整合：店長視野的資料隔離 + 與 `_shared`（Notifier / AutonomyGate / Diagnostics）互動。

    這是本模組的核心保證——店長取得的資料集裡**根本不含**其他據點的數字。
    """
    result = main.run(
        _args(tmp_path, role="site_manager", site_id="tpe-xinyi", actor="mgr.xinyi@example.com")
    )

    assert result["pack"] == "own_site_only"
    assert result["access"]["visible_site_ids"] == ["tpe-xinyi"]
    assert result["sites_reporting"] == 1
    assert result["sites"][0]["site_id"] == "tpe-xinyi"

    # 自身分數與總部算出來的完全一致（正規化基準是自己的 4 週基準線，不是同儕）
    assert result["sites"][0]["composite_score"] == "110.90"
    assert result["anomaly_count"] == 1

    # 跨據點分析在資料層就不存在，不是「有算但沒印」
    assert result["ranking"] is None
    assert result["network_totals"] is None
    assert result["peer_median_composite"] is None

    # 輸出中不得出現任何其他據點的識別碼、店名或數字
    serialised = json.dumps(result, ensure_ascii=False, default=str)
    for token in OTHER_SITE_TOKENS:
        assert token not in result["report_text"], f"報表洩漏了 {token}"
        assert token not in serialised, f"回傳結構洩漏了 {token}"
    for number in ("880,000", "520,000", "800,000", "4,350,000", "96.92", "86.20", "102.04"):
        assert number not in result["report_text"], f"報表洩漏了他店數字 {number}"
    assert result["leak_scan_passed"] is True

    # 他店的無回應狀態同樣不可見（那也是他店的營運資訊）
    assert result["is_partial"] is False
    assert "部分資料" not in result["report_text"]

    # 稽核軌跡：誰、何時、看了哪些據點
    events = audit.read_events(Path(result["audit"]["file"]))
    kinds = [item["event"] for item in events]
    assert audit.EVENT_ACCESS_RESOLVED in kinds and audit.EVENT_DATA_ACCESS in kinds
    access_event = next(item for item in events if item["event"] == audit.EVENT_DATA_ACCESS)
    assert access_event["actor"] == "mgr.xinyi@example.com"
    assert access_event["role"] == "site_manager"
    assert access_event["visible_site_ids"] == ["tpe-xinyi"]
    assert access_event["sites_returned"] == ["tpe-xinyi"]
    assert access_event["cross_site_analytics"] is False
    assert all(item["ts"].endswith("+00:00") for item in events)

    # _shared.Notifier：console 通道實際印出，不是靜默失敗
    assert result["delivery"]["channel"] == "console"
    assert result["delivery"]["delivered"] is True
    assert "多據點商業智慧匯總" in capsys.readouterr().out

    # _shared.AutonomyGate：對外通道在 draft 下一律扣住等人工審核（且安全閥先過關）
    outbound = main.run(
        _args(tmp_path, role="site_manager", site_id="tpe-xinyi", notify="telegram")
    )
    assert outbound["delivery"]["preflight"]["passed"] is True
    assert outbound["delivery"]["delivered"] is False
    assert outbound["delivery"]["reason"] == "autonomy_draft"

    # 店長視野不得推進全網基準線
    assert outbound["baseline_state"]["updated"] is False
    assert outbound["baseline_state"]["reason"] == "baseline_update_requires_hq"
