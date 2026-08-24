"""模組 #21 測試（中等複雜度標準：happy / edge / integration 各一）。

integration 那則專門驗兩個「做錯就整套失效」的機制：
1. **全域繼承級聯**：Orchestrator 一改，五個 Sub-agent 立刻同版本；
   旗標關掉的 agent 拿不到上下文，且不准偷用舊資料。
2. **人類審核閘門**：備忘錄未核准時，內容照產但一律鎖在草稿。

edge 那則挑「每週日 07:00」的時間邊界，因為排程差一秒就會算到上一週的備忘錄，
而備忘錄識別碼一錯，核准紀錄就對不上。時區一律注入固定時差，不依賴 tzdata。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

import main as swarm_main  # noqa: E402
from _shared.llm_client import LLMError  # noqa: E402
from audit import AuditLog, read_entries  # noqa: E402
from orchestrator import (  # noqa: E402
    AgentSpec,
    Orchestrator,
    SubAgent,
    SwarmError,
    current_memo_slot,
    resolve_timezone,
)

# 固定時差，測試不依賴系統的 tzdata 是否安裝
TAIPEI = timezone(timedelta(hours=8))
# 2026-09-09 是星期三，對應的最近一次備忘錄時點是 2026-09-06（週日）07:00
FIXED_NOW = "2026-09-09T10:30:00+08:00"
EXPECTED_MEMO_ID = "MEMO-2026-09-06-torresmark"

# 五個子智能體的 fixture 產能總和：10 + 28 + 3 + 63 + 7
EXPECTED_DELIVERABLES = 111


def _args(tmp_path: Path, **overrides: object) -> argparse.Namespace:
    """建立測試用參數。

    exit_on_red=False 讓紅色警報拋 RedAlert 而不是結束行程；
    state / audit 一律指向 tmp_path，避免測試污染工作樹。
    """
    namespace = argparse.Namespace(
        mock=True,
        live=False,
        dry_run=False,
        notify="console",
        config=str(_DEMO_DIR / "config.yaml"),
        state_file=str(tmp_path / "state.json"),
        audit_file=str(tmp_path / "audit.jsonl"),
        approve=False,
        approved_by=None,
        stage=None,
        now=FIXED_NOW,
        exit_on_red=False,
    )
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def _isolate_usage_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 LLMClient 的用量記錄導到暫存目錄，別寫進 demo/.usage.jsonl。"""
    monkeypatch.setenv("OPENCLAW_USAGE_LOG", str(tmp_path / "usage.jsonl"))


def _make_agent(agent_id: str, inherit: bool = True) -> SubAgent:
    """建立最小可用的 Sub-agent（測試繼承機制用，不碰檔案系統）。"""
    return SubAgent(
        AgentSpec.from_config(
            {
                "id": agent_id,
                "display_name": agent_id,
                "quota_min": 1,
                "quota_max": 1,
                "integration": "cms",
                "INHERIT_FROM_ORCHESTRATOR": inherit,
            }
        )
    )


def test_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """標準 mock 輸入 + 已核准：五個子智能體全數派工且產能落在配額內。"""
    _isolate_usage_log(monkeypatch, tmp_path)
    result = swarm_main.run(
        _args(tmp_path, approve=True, approved_by="Elena Torres")
    )

    assert result["module_id"] == "21"
    assert result["mode"] == "mock"
    assert result["stage"] == "conversion"
    assert result["approval"]["memo_id"] == EXPECTED_MEMO_ID
    assert result["approval"]["is_approved"] is True
    assert result["approval"]["approved_by"] == "Elena Torres"

    # 強制安全閥必須在任何產出之前通過（mock 模式不需要憑證）
    assert result["preflight"]["passed"] is True
    assert result["preflight"]["checked"] == 5
    assert all(check["endpoint"].startswith("https://") for check in result["preflight"]["checks"])

    # 五個 agent 全數派工，產能與 apxG_p04 的配額一致
    assert result["totals"]["agents_total"] == 5
    assert result["totals"]["agents_dispatched"] == 5
    assert result["totals"]["deliverables"] == EXPECTED_DELIVERABLES
    assert all(action["is_within_quota"] for action in result["actions"])
    assert all(action["status"] == "dispatched" for action in result["actions"])
    assert not any(action["guardrail_violations"] for action in result["actions"])

    # 每一份產出都戳上同一版品牌上下文 —— 這是級聯真的發生過的證據
    versions = {action["context_version"] for action in result["actions"]}
    checksums = {action["context_checksum"] for action in result["actions"]}
    assert versions == {result["context_version"]}
    assert checksums == {result["context_checksum"]}
    assert result["cascade"]["refused"] == []
    assert result["cascade"]["desynced"] == []

    # 金額一律 Decimal 字串，且書中「首月省下即超過建置費」的說法要成立
    assert result["economics"]["client_setup_price"] == "5000"
    assert result["economics"]["monthly_net_saving_low"] == "5500"
    assert result["economics"]["is_first_month_payback"] is True
    # 原簡報未提供顧問端內部回收工時，不得推估
    assert result["economics"]["recovered_hours_per_month"] is None

    # 稽核軌跡必須落地且涵蓋關鍵動作
    actions_logged = read_entries(tmp_path / "audit.jsonl")
    logged_names = {entry["action"] for entry in actions_logged}
    assert {"context_cascade", "preflight_dry_run", "strategy_memo_approved",
            "agent_dispatch", "run_completed"} <= logged_names
    approved_entries = [entry for entry in actions_logged if entry["is_human_approved"]]
    assert approved_entries and all(
        entry["approved_by"] == "Elena Torres" for entry in approved_entries
    )


def test_edge_case() -> None:
    """時間邊界：週日 07:00 前後一秒會落在不同週的備忘錄，且時區缺失要能降級。"""
    exact = datetime(2026, 9, 6, 7, 0, 0, tzinfo=TAIPEI)
    assert current_memo_slot(exact, "SUN", "07:00") == exact

    # 早一秒 -> 本週的排程還沒發生，必須退回上一個週日
    one_second_early = exact - timedelta(seconds=1)
    assert current_memo_slot(one_second_early, "SUN", "07:00") == exact - timedelta(days=7)

    # 週中任一時間都應對應到本週已發生的那次排程
    midweek = datetime(2026, 9, 9, 10, 30, tzinfo=TAIPEI)
    assert current_memo_slot(midweek, "SUN", "07:00") == exact

    # 星期代號寫錯要明確拋錯，不可安靜跳過（排程錯一天等於整週備忘錄對不上）
    with pytest.raises(SwarmError):
        current_memo_slot(midweek, "SUNDAY", "07:00")

    # tzdata 缺失時退回固定時差並留下警告，而不是整支程式掛掉
    tz, warning = resolve_timezone("Not/AZone", fallback_utc_offset_hours=8)
    assert warning is not None
    assert datetime(2026, 9, 6, tzinfo=tz).utcoffset() == timedelta(hours=8)


def test_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """與 _shared 及蜂群機制的互動：級聯繼承、審核閘門、autonomy 降級、console 通知。"""
    _isolate_usage_log(monkeypatch, tmp_path)

    # --- 1. 級聯繼承：Orchestrator 一改，所有帶旗標的 agent 立刻同版本 ---
    audit = AuditLog(tmp_path / "unit-audit.jsonl", "21", "swarm", tz=TAIPEI)
    orchestrator = Orchestrator(
        {"context_version": 1, "tenant_slug": "acme", "brand": {"name": "舊品牌"}},
        audit=audit,
    )
    obedient = _make_agent("content")
    rogue = _make_agent("social", inherit=False)
    orchestrator.register(obedient)
    orchestrator.register(rogue)
    cascade = orchestrator.cascade("初始化")

    assert cascade["inherited"] == ["content"]
    assert cascade["refused"] == ["social"]
    assert obedient.context.brand_name == "舊品牌"
    # 旗標關掉的 agent 沒有上下文，且不准回退成空 dict 硬跑
    assert rogue.has_context is False
    with pytest.raises(SwarmError):
        _ = rogue.context

    updated = orchestrator.update_brand_context(
        {"brand": {"name": "新品牌"}}, reason="白牌抽換"
    )
    assert updated.version == 2
    assert obedient.context.brand_name == "新品牌"          # 瞬間級聯
    assert obedient.is_synced_with(orchestrator.context)
    assert orchestrator.desynced_agents() == ["social"]

    # --- 2. 審核閘門：未核准時內容照產，但一律鎖在草稿 ---
    pending = swarm_main.run(_args(tmp_path, dry_run=True))
    assert pending["approval"]["is_approved"] is False
    active = [a for a in pending["actions"] if a["status"] != "skipped_inactive_stage"]
    assert active and all(a["status"] == "blocked_pending_approval" for a in active)
    assert pending["totals"]["auto_publish"] == 0
    assert any("尚未核准" in warning for warning in pending["warnings"])
    assert pending["notified"] is False          # dry-run 不送出

    # dry-run 必須揭露將呼叫哪些外部端點、送出什麼
    printed = capsys.readouterr().out
    assert "強制安全閥" in printed
    assert "https://analyticsdata.googleapis.com" in printed
    assert "expected_items" in printed

    # --- 3. 核准後才放行，且 console 通知確實送出 ---
    approved = swarm_main.run(
        _args(tmp_path, approve=True, approved_by="Elena Torres")
    )
    assert approved["approval"]["is_approved"] is True
    assert approved["totals"]["agents_dispatched"] == 5
    assert approved["notified"] is True

    # --- 4. autonomy：supervised_auto 缺白名單要降級成 DRAFT 並發琥珀警示 ---
    diagnostics = swarm_main.Diagnostics("test-swarm", exit_on_red=False)
    gate = swarm_main._build_gate(
        {"autonomy": "supervised_auto", "approved_senders": [], "days_in_draft": 0},
        diagnostics,
    )
    assert gate.level.value == "draft"
    assert diagnostics.amber_count >= 1
    assert gate.can_send("content") is False


def test_main_catches_llm_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--live 模式下 LLM 呼叫逾時等狀況會拋 LLMError；main() 必須吃下來變成 exit code 1，
    而不是讓 raw traceback 砸給使用者（demo11 既有慣例的補齊）。
    """

    def _raise_llm_error(args: argparse.Namespace) -> dict:
        raise LLMError("模擬 CLI 逾時")

    monkeypatch.setattr(swarm_main, "run", _raise_llm_error)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    exit_code = swarm_main.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "錯誤：" in captured.err
    assert "模擬 CLI 逾時" in captured.err


def test_format_summary_includes_sample_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug 修復：format_summary() 原本只印每個子智能體的產能統計，完全不印
    orchestrator._agent_action() 存進 samples 的實際內容片段。這裡驗證摘要
    文字含有每個有產出的 agent 的樣本預覽（用 _sample_preview() 動態算出
    預期值，避免 mock fixture 文字微調就打壞測試）。
    """
    _isolate_usage_log(monkeypatch, tmp_path)
    result = swarm_main.run(
        _args(tmp_path, approve=True, approved_by="Elena Torres")
    )

    dispatched_with_samples = [a for a in result["actions"] if a["samples"]]
    assert dispatched_with_samples, "測試前提：至少要有一個 agent 產出 samples"
    for action in dispatched_with_samples:
        expected_preview = swarm_main._sample_preview(action["samples"][0])
        assert expected_preview in result["summary_text"]


def test_sample_preview_edge_case_unknown_fields() -> None:
    """邊界：sample 沒有任何已知欄位（title/text/subject/findings/lead_ref）
    時要退回印整包 JSON，不可回傳空字串或拋例外；純字串 sample 也要能處理。
    """
    assert swarm_main._sample_preview({"unexpected_key": "值"}) != ""
    assert swarm_main._sample_preview("純字串樣本") == "純字串樣本"
