"""demo24 三個測試（CONTRACT §8）：happy / edge / integration。

一律離線執行：不呼叫任何真實 API，申請資料全部來自 mock/ 目錄。
狀態檔與稽核日誌一律指向 pytest 的 tmp_path，不污染模組目錄下的 state/。

edge case 刻意選「匿名化完整性」：這是全模組倫理風險最高的一步，
mock 資料中每一份履歷都刻意帶著姓名 / 性別 / 年齡 / 國籍 / 照片 / 畢業年份，
測試要證明它們在評分前真的一個都不剩。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo_main  # noqa: E402
from _shared.config_loader import load_config  # noqa: E402
from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from anonymiser import (  # noqa: E402
    IdentityVault,
    RevealNotAuthorisedError,
    anonymise_all,
    collect_protected_values,
    verify_anonymisation,
)
from audit import AuditLog  # noqa: E402
from scorer import DECISION_DISQUALIFIED, DECISION_HOLD, DECISION_REJECT, DECISION_SHORTLIST  # noqa: E402

MOCK_APPLICATIONS = MODULE_DIR / "mock" / "applications.json"
BASE_TIME = "2026-08-24T09:00:00+00:00"
# 49 小時後：跨過 config 的 rejection.delay_hours = 48
AFTER_DELAY = "2026-08-26T10:00:00+00:00"


def _args(tmp_path: Path, **overrides: Any):
    """建出預設 CLI 參數，並關掉 exit_on_red 讓紅色警報改拋例外而非結束行程。"""
    args = demo_main.build_parser().parse_args([])
    args.exit_on_red = False
    args.state_file = str(tmp_path / "state.json")
    args.audit_file = str(tmp_path / "audit.jsonl")
    args.now = BASE_TIME
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _decisions(result: dict[str, Any]) -> dict[str, int]:
    """統計各處置分支的件數。"""
    counts: dict[str, int] = {}
    for row in result["candidates"]:
        counts[row["decision"]] = counts.get(row["decision"], 0) + 1
    return counts


def test_happy_path(tmp_path: Path) -> None:
    """7 份履歷跑完三分支：短名單 3、保留 1、延遲拒絕 1、不符合 2，前 20% 收到面試邀請。"""
    result = demo_main.run(_args(tmp_path))

    assert result["mode"] == "mock"
    assert result["received_count"] == 7
    assert _decisions(result) == {
        DECISION_SHORTLIST: 3,
        DECISION_HOLD: 1,
        DECISION_REJECT: 1,
        DECISION_DISQUALIFIED: 2,
    }

    # 前 20%：ceil(7 × 20%) = 2 個名額，且必須是分數最高的兩位短名單成員
    assert len(result["invited"]) == 2
    assert result["invited"] == result["shortlist"][:2]
    invited_rows = [row for row in result["candidates"] if row["invited_to_video_interview"]]
    assert all(len(row["interview_questions"]) == 4 for row in invited_rows)

    # 年資 1 年與無工作權者一律 disqualified，不因分數高而進入排名
    disqualified = [row for row in result["candidates"] if row["decision"] == DECISION_DISQUALIFIED]
    assert all(row["disqualifiers"] for row in disqualified)

    # 鐵律 3：短名單報告只出現匿名識別碼
    assert all(identifier.startswith("CAND-") for identifier in result["shortlist"])
    payload = load_config(MODULE_DIR / "config.yaml")  # 只為了拿 anonymisation 設定
    assert payload["bias_mitigation"]["shortlist_presentation"] == "identifiers_only"
    assert "Amara" not in result["report"] and "Rosa" not in result["report"]

    # 拒絕信排入 48 小時後，本次不送出
    assert len(result["pending_rejections"]) == 1
    assert result["dispatched_rejections"] == []

    # 49 小時後重跑：同一封信到期送出，佇列清空，且不會重複排程
    later = demo_main.run(_args(tmp_path, now=AFTER_DELAY))
    assert len(later["dispatched_rejections"]) == 1
    assert later["pending_rejections"] == []
    assert later["audit_chain_problems"] == []


def test_edge_case_anonymisation_leaves_no_identity_string(tmp_path: Path) -> None:
    """邊界：匿名化後的資料中，不得殘留任何原始姓名 / 受保護欄位字串。"""
    config = load_config(MODULE_DIR / "config.yaml")
    raw = json.loads(MOCK_APPLICATIONS.read_text(encoding="utf-8"))
    applications = raw["applications"]
    vault = IdentityVault()
    anonymised = anonymise_all(applications, config, vault)

    remove_fields = config["anonymisation"]["remove_fields"]
    for original, cleaned in zip(applications, anonymised):
        haystack = repr(cleaned.fields).lower()
        # 1. 受保護欄位本身必須整個消失（含 application_id，ATS 主鍵不隨匿名件外流）
        for field_name in remove_fields:
            assert field_name not in cleaned.fields
        assert "application_id" not in cleaned.fields
        # 2. 姓名的每一段（含 preferred_name）都不得出現在任何欄位的內容裡
        for part in str(original["name"]).split() + [str(original["preferred_name"])]:
            assert part.lower() not in haystack, f"{cleaned.identifier} 殘留姓名片段 {part}"
        # 3. 其餘受保護值（國籍 / 族裔 / 信箱 / 電話 / 校名 / 畢業年份…）一併掃過
        assert verify_anonymisation(cleaned, collect_protected_values(original, config["anonymisation"])) == []

    # 4. 識別碼可重現、彼此互異，且不含原始 application_id
    identifiers = [item.identifier for item in anonymised]
    assert len(set(identifiers)) == len(identifiers)
    assert all(app["application_id"] not in ident for app, ident in zip(applications, identifiers))

    # 5. 鐵律 4：沒有招募經理具名核准就取不出真實身分
    with pytest.raises(RevealNotAuthorisedError):
        vault.reveal(identifiers[0], "")
    revealed = vault.reveal(identifiers[0], "Nadia Farrell", "面試安排")
    assert revealed["name"] == applications[0]["name"]

    # 6. 空批次不應炸掉，也不應留下任何殘留
    assert anonymise_all([], config, IdentityVault()) == []
    assert tmp_path.exists()  # tmp_path 僅用於隔離，本測試不寫檔


def test_integration_shared_gate_diagnostics_and_audit(tmp_path: Path) -> None:
    """整合：AutonomyGate 降級 + Diagnostics amber + Notifier console + 稽核鏈 + 安全閥。"""
    config = load_config(MODULE_DIR / "config.yaml")
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["dispatch@ats.example.com"]
    config_path = tmp_path / "config-supervised.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = demo_main.run(_args(tmp_path, config=str(config_path)))

    # 1. AutonomyGate：未滿 14 天就開全自動 → 產生警告並轉成 amber，但流程繼續
    assert result["warnings"], "supervised_auto 未滿 14 天必須留下警告"
    assert result["amber_count"] >= 1

    # 2. 白名單命中者維持 SUPERVISED_AUTO，未命中者降級為 DRAFT
    gate = demo_main.build_gate(config, Diagnostics("test-gate", exit_on_red=False))
    assert gate.can_send("dispatch@ats.example.com") is True
    assert gate.can_send("recruiting-manager@example.com") is False

    # 3. Notifier console：報告確實送出（console 永遠成功）
    assert result["notify_channel"] == "console"
    assert result["delivered"] is True

    # 4. 稽核鏈完整，且四個關鍵事件都在
    entries = AuditLog(tmp_path / "audit.jsonl", "demo24-hr-screening").read_all()
    events = {row["event"] for row in entries}
    assert {"anonymisation_completed", "rejection_scheduled", "screening_completed"} <= events
    assert result["audit_chain_problems"] == []

    # 5. 全域安全閥：沒先跑過 --dry-run 就要對外送出 → 紅色警報擋下
    with pytest.raises(RedAlert):
        demo_main.run(_args(tmp_path, config=str(config_path), notify="telegram"))
