"""demo25 動態客戶媒合引擎 —— 3 個測試（happy / edge / integration）。

edge case 是本模組的兩條命脈，一個測試同時釘住：
1. **公平住房法遵閘門**：條件含受保護特徵時必須拒絕執行並拋 ComplianceError，
   不得靜默略過該欄位後照常推薦。
2. **通知去重與條件變更重比對**：同一買方對同一物件不重複打擾；
   買方條件一改，該買方的去重紀錄清空、既有物件重新推薦。
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

_DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.llm_client import LLMError  # noqa: E402


def _load(module_name: str, filename: str):
    """以絕對路徑載入本 demo 的模組。

    十個 demo 都有同名的 main.py，一次跑整個 demo/ 目錄時
    plain import 會抓到別的 demo 的同名模組，因此這裡固定綁死檔案路徑。
    """
    spec = importlib.util.spec_from_file_location(module_name, _DEMO_DIR / filename)
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# 順序不可調換：main 內部會 import matcher / audit，必須先佔住 sys.modules
matcher = _load("matcher", "matcher.py")
audit = _load("audit", "audit.py")
client_matching = _load("main", "main.py")


def _args(tmp_path: Path, *extra: str):
    """組出離線模式的 CLI 參數；狀態檔與稽核檔一律指向 tmp，避免污染 repo"""
    argv = [
        "--mock",
        "--state-file", str(tmp_path / "notifications.json"),
        "--audit-file", str(tmp_path / "audit.jsonl"),
    ]
    return client_matching.build_parser().parse_args(argv + list(extra))


def _seeded_state(tmp_path: Path) -> Path:
    """把 mock/state_seed.json 複製到 tmp 當作「上一次執行留下的狀態」"""
    target = tmp_path / "notifications.json"
    shutil.copyfile(_DEMO_DIR / "mock" / "state_seed.json", target)
    return target


def test_happy_path(tmp_path):
    """全新狀態：5 物件 × 5 買方 = 25 組比對，8 封達門檻推播、分級與時效皆正確"""
    result = client_matching.run(_args(tmp_path))

    assert result["module_id"] == "25"
    assert result["mode"] == "mock"
    assert (result["listings"], result["buyers"], result["evaluated"]) == (5, 5, 25)

    scores = {(m["listing_id"], m["buyer_id"]): m for m in result["matches"]}
    # 完全命中：三個 hard + 三個 soft 全中 → 100 分、Perfect、高優先級
    perfect = scores[("L-001", "B-101")]
    assert (perfect["score"], perfect["tier"]) == ("100.00", "perfect")
    assert perfect["is_high_priority"] is True
    # 部分命中：hard 全中、soft 中 1 → (9+1)/12 = 83.33，Strong 但仍達門檻 80
    partial = scores[("L-005", "B-101")]
    assert (partial["score"], partial["tier"], partial["is_pushable"]) == ("83.33", "strong", True)
    assert partial["is_high_priority"] is False
    # ⚠️ 規格衝突區：Strong 從 75 起算但門檻是 80 → 75.00 分被標 strong 卻不推播
    gap = scores[("L-005", "B-102")]
    assert (gap["score"], gap["tier"], gap["is_pushable"]) == ("75.00", "strong", False)
    # 完全不命中：總價與房型皆不符
    miss = scores[("L-003", "B-101")]
    assert (miss["tier"], miss["is_pushable"]) == ("below", False)

    pushed = {(n["listing_id"], n["buyer_id"]) for n in result["notifications"]}
    assert pushed == {
        ("L-001", "B-101"), ("L-001", "B-104"), ("L-001", "B-105"),
        ("L-002", "B-102"), ("L-004", "B-103"),
        ("L-005", "B-101"), ("L-005", "B-104"), ("L-005", "B-105"),
    }
    # 逾 60 分鐘時效的兩筆必須被點名，不得靜默通過
    assert {b["listing_id"] for b in result["sla_breaches"]} == {"L-002", "L-004"}
    # 分支 B：低詢問度物件產出降價談判包
    assert [p["listing_id"] for p in result["vendor_packs"]] == ["L-004"]
    # 推薦內容只能出現客觀條件，不得夾帶任何受保護特徵字眼
    for notification in result["notifications"]:
        assert "族" not in notification["text"]
        assert "宗教" not in notification["text"]


def test_edge_case_compliance_gate_and_dedup(tmp_path):
    """edge：受保護特徵條件被擋 + 已通知過的不重複打擾 + 條件變更觸發重比對"""
    # (1) 法遵閘門 —— 四種違規條件（家庭狀況／族裔／身心障礙／白名單外）皆須拒絕執行
    violations = json.loads(
        (_DEMO_DIR / "mock" / "buyers_noncompliant.json").read_text(encoding="utf-8")
    )["buyers"]
    allowed = matcher.DEFAULT_ALLOWED_CRITERIA_FIELDS
    for raw in violations:
        with pytest.raises(matcher.ComplianceError) as excinfo:
            matcher.assert_criteria_compliant(
                raw["criteria"], allowed, source=f"買方 {raw['buyer_id']}"
            )
        assert raw["buyer_id"] in str(excinfo.value)
    # 大小寫與連字號不得成為繞過白名單的後門
    assert matcher.detect_protected_fields(["Buyer-Race", "NO_CHILDREN"]) == [
        "Buyer-Race", "NO_CHILDREN"
    ]
    # 整支流程層級：違規資料進來就整批中止，不做半套推薦
    with pytest.raises(matcher.ComplianceError):
        client_matching.run(
            _args(tmp_path, "--buyers-file", "mock/buyers_noncompliant.json")
        )
    assert not (tmp_path / "notifications.json").exists(), "違規時不得寫出任何去重狀態"

    # (2) 去重 + 條件變更 —— 帶著上一次的狀態重跑
    state_path = _seeded_state(tmp_path)
    result = client_matching.run(_args(tmp_path))

    # B-104 的指紋未變且已通知過 L-001 → 必須被去重擋下
    assert {(d["listing_id"], d["buyer_id"]) for d in result["suppressed_duplicates"]} == {
        ("L-001", "B-104")
    }
    # B-105 的指紋是舊值 → 條件已變更 → 清空紀錄，L-001 重新推薦
    assert result["criteria_changed_buyers"] == ["B-105"]
    pushed = {(n["listing_id"], n["buyer_id"]) for n in result["notifications"]}
    assert ("L-001", "B-105") in pushed
    assert ("L-001", "B-104") not in pushed
    assert len(result["notifications"]) == 7

    # (3) 去重是持久的：同樣的輸入再跑一次，一封都不該重複送出
    again = client_matching.run(_args(tmp_path))
    assert again["notifications"] == []
    assert len(again["suppressed_duplicates"]) == 8
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(saved["notified"]) == 8


def test_integration_autonomy_audit_and_console_notify(tmp_path, capsys):
    """整合：supervised_auto 未命中白名單 → 降級 draft + AMBER，且稽核軌跡完整落地"""
    config = yaml.safe_load((_DEMO_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"].update(
        {
            "autonomy": "supervised_auto",
            "approved_senders": ["@wholesale.example"],
            "days_in_draft": 0,
            "notify_channel": "console",
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # AutonomyGate 的降級規則本身：只有批發商網域在白名單內
    gate = AutonomyGate(
        level=AutonomyLevel.SUPERVISED_AUTO,
        approved_senders=["@wholesale.example"],
        days_in_draft=0,
    )
    assert gate.effective_level("sarah.chen@wholesale.example") is AutonomyLevel.SUPERVISED_AUTO
    assert gate.effective_level("yalin.chen@buyers.example") is AutonomyLevel.DRAFT
    assert gate.warnings, "未滿 14 天就開 supervised_auto 必須留下警告"

    result = client_matching.run(_args(tmp_path, "--config", str(config_path)))

    delivery = {n["buyer_id"]: n["delivery"] for n in result["notifications"]}
    assert delivery["B-104"] == "sent", "白名單內的批發商可自動送出"
    assert delivery["B-101"] == "draft", "白名單外一律降級為草稿"
    assert result["notify_channel"] == "console"
    assert result["warnings"], "自主權警告必須回傳給呼叫端"
    # 2 筆逾時效 + 1 筆缺 Calendly 連結 + 至少 1 筆自主權警告
    assert result["amber_count"] >= 4

    # 稽核軌跡：JSONL 真的落地，且關鍵事件齊全
    entries = audit.read_audit(tmp_path / "audit.jsonl")
    events = [entry["event"] for entry in entries]
    for required in (
        "run_started", "compliance_check_passed", "preflight_passed",
        "notification_sent", "sla_breach", "vendor_pack_generated", "run_completed",
    ):
        assert required in events, f"稽核軌跡缺少事件 {required}"
    assert len({entry["run_id"] for entry in entries}) == 1, "同一次執行必須共用 run_id"
    # 每一筆推播都要留下判斷依據，事後才稽核得出「為什麼推薦給這個人」
    sent = next(e for e in entries if e["event"] == "notification_sent")
    assert sent["matched_fields"] and "score" in sent and "channel" in sent

    printed = capsys.readouterr().out
    assert "【草稿・待人工核准】" in printed


def test_main_catches_llm_error(monkeypatch, capsys):
    """--live 模式下 CLI 逾時等狀況會拋 LLMError；main() 必須吃下來變成 exit code 1，
    而不是讓 raw traceback 砸給使用者（demo16 既有慣例的補齊）。
    """

    def _raise_llm_error(args):
        raise LLMError("模擬 CLI 逾時")

    monkeypatch.setattr(client_matching, "run", _raise_llm_error)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    exit_code = client_matching.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "錯誤：" in captured.err
    assert "模擬 CLI 逾時" in captured.err
