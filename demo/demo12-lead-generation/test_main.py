"""模組 #12 的三個測試（happy / edge / integration）。

integration 測試是本模組最重要的一個：它同時驗證兩條安全機制真的生效——
1. `require_unsubscribe` 即使在 config 被關掉也會被強制覆寫回 True
2. 命中抑制名單的線索，不論分數多高都不得出現在任何外送或草稿中
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta, timezone as dt_timezone
from pathlib import Path

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo12  # noqa: E402
from lead_scorer import (  # noqa: E402
    BLOCK_CONSENT_REQUIRED,
    BLOCK_SUPPRESSED,
    BLOCK_UNLAWFUL_SOURCE,
    ENRICH_INCOMPLETE,
    HALT_CADENCE_COMPLETE,
    HALT_NOT_DUE,
    REJECT_BELOW_THRESHOLD,
)

BASE_CONFIG_PATH = MODULE_DIR / "config.yaml"
SUPPRESSED_LEAD_ID = "L-2005"
WHITELISTED_LEAD_ID = "L-2001"


@pytest.fixture(autouse=True)
def _freeze_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """把時區解析換成固定 UTC+8，讓測試不受本機有無 tzdata 影響。

    `resolve_timezone()` 在缺 IANA 時區資料庫的機器（Windows 預設即是）上會
    找不到 Asia/Taipei，降級並發出一則 AMBER——這與被測的業務邏輯完全無關，
    卻會污染 `amber_count`。時區不是這些測試要驗的東西，直接把變因移除。
    """
    monkeypatch.setattr(
        demo12,
        "resolve_timezone",
        lambda name, fallback_offset_hours=8: (dt_timezone(timedelta(hours=8)), None),
    )


def _load_base_config() -> dict:
    """讀出正式 config.yaml 作為各測試的基底。"""
    return yaml.safe_load(BASE_CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, config: dict) -> Path:
    """把改過的設定寫成臨時 config.yaml，避免測試污染正式設定。"""
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def _run(config_path: Path | None = None, notify: str = "console") -> dict:
    """以 --mock 跑一次主流程。"""
    argv = ["--mock", "--notify", notify]
    if config_path is not None:
        argv += ["--config", str(config_path)]
    return demo12.run(demo12.build_parser().parse_args(argv))


def _ids(entries: list) -> set[str]:
    """取出一組結果項目的 lead_id。"""
    return {str(item["lead_id"]) for item in entries}


def _by_id(entries: list) -> dict[str, dict]:
    """以 lead_id 為鍵建立索引。"""
    return {str(item["lead_id"]): item for item in entries}


def test_happy_path() -> None:
    """標準 mock 輸入：9 筆線索分流到六個 bucket，分數與管線價值精確吻合。"""
    result = _run()

    assert result["module_id"] == "12"
    assert result["mode"] == "mock"
    assert result["total_leads"] == 9
    assert result["require_unsubscribe"] is True
    assert result["is_sender_identity_complete"] is True
    # 預設自主權為 DRAFT，因此不該有任何自動送出
    assert result["sent"] == []

    # --- 分流結果 -------------------------------------------------------
    assert _ids(result["drafted"]) == {"L-2001", "L-2002"}
    assert _ids(result["enrichment_queue"]) == {"L-2004"}
    assert _ids(result["rejected"]) == {"L-2003"}
    assert _ids(result["blocked"]) == {"L-2005", "L-2006", "L-2007"}
    assert _ids(result["skipped"]) == {"L-2008", "L-2009"}

    # --- 評分正確性（Decimal 精確比對，不是近似值）-----------------------
    cards = _by_id(result["scorecards"])
    assert cards["L-2001"]["score"] == "100.00" and cards["L-2001"]["band"] == "hot"
    assert cards["L-2002"]["score"] == "65.00" and cards["L-2002"]["band"] == "warm"
    assert cards["L-2003"]["score"] == "0.00" and cards["L-2003"]["band"] == "cold"
    assert cards["L-2008"]["score"] == "93.75"
    assert result["band_counts"] == {"hot": 6, "warm": 1, "cold": 2}

    # --- 各 bucket 的原因鍵 ---------------------------------------------
    assert _by_id(result["rejected"])["L-2003"]["reason"] == REJECT_BELOW_THRESHOLD
    assert _by_id(result["enrichment_queue"])["L-2004"]["reason"] == ENRICH_INCOMPLETE
    assert _by_id(result["enrichment_queue"])["L-2004"]["missing_fields"] == [
        "industry",
        "employee_count",
        "tech_stack",
    ]
    blocked = _by_id(result["blocked"])
    assert blocked["L-2005"]["reason"] == BLOCK_SUPPRESSED
    assert blocked["L-2006"]["reason"] == BLOCK_UNLAWFUL_SOURCE
    assert blocked["L-2007"]["reason"] == BLOCK_CONSENT_REQUIRED
    skipped = _by_id(result["skipped"])
    assert skipped["L-2008"]["reason"] == HALT_CADENCE_COMPLETE
    assert skipped["L-2009"]["reason"] == HALT_NOT_DUE

    # --- 外聯階段與內文 --------------------------------------------------
    drafted = _by_id(result["drafted"])
    assert drafted["L-2001"]["stage_day"] == 0
    assert drafted["L-2001"]["stage_type"] == "icebreaker"
    assert drafted["L-2001"]["max_chars"] == 150
    assert drafted["L-2002"]["stage_day"] == 4
    assert drafted["L-2002"]["stage_type"] == "value_proof"
    # 法定的識別與退訂區塊由程式附加，必須每封都在
    for item in result["drafted"]:
        assert "請點此退訂" in item["body"]
        assert "https://example-demo.invalid/unsubscribe" in item["body"]
        assert "您的聯絡資訊來源：" in item["body"]

    # --- 管線價值（含尚未到期的合格線索，全程 Decimal）-------------------
    assert result["pipeline_value_usd"] == "125000.75"
    # 被阻擋 / 不合格 / 待補資料的線索不得計入管線價值
    assert "52000" not in result["pipeline_value_usd"]


def test_edge_case_empty_lead_list(tmp_path: Path) -> None:
    """邊界：當日沒有任何新線索時要安靜跑完，不可拋例外也不可誤報警示。"""
    empty_path = tmp_path / "leads_empty.json"
    empty_path.write_text("[]", encoding="utf-8")

    config = _load_base_config()
    config["mock"]["leads"] = str(empty_path)
    config_path = _write_config(tmp_path, config)

    result = _run(config_path)

    assert result["total_leads"] == 0
    assert result["sent"] == []
    assert result["drafted"] == []
    assert result["enrichment_queue"] == []
    assert result["rejected"] == []
    assert result["blocked"] == []
    assert result["skipped"] == []
    assert result["scorecards"] == []
    assert result["band_counts"] == {"hot": 0, "warm": 0, "cold": 0}
    # 空管線的價值是 0.00，不是空字串也不是 None
    assert result["pipeline_value_usd"] == "0.00"
    # 空清單不是異常，不該產生任何 AMBER（時區已凍結，此處若非 0 必來自業務邏輯）
    assert result["amber_count"] == 0
    assert result["warnings"] == []
    assert result["require_unsubscribe"] is True
    # 摘要仍要能組出來（不可因為空清單而崩在字串格式化）
    assert result["module_name"] in demo12._summarise(result)


def test_integration_compliance_hard_rule_and_autonomy_downgrade(tmp_path: Path) -> None:
    """整合：退訂硬規則 + 抑制名單零外送 + 自主權白名單降級。"""
    config = _load_base_config()
    # 刻意把法定的退訂機制關掉，程式必須拒絕接受
    config["compliance"]["require_unsubscribe"] = False
    # 開全自動，但白名單只放 L-2001 的網域
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["@northwind-apps.com"]
    config["runtime"]["days_in_draft"] = 30
    config_path = _write_config(tmp_path, config)

    result = _run(config_path)

    # 1. require_unsubscribe 被強制覆寫，且透過 Diagnostics 發出 AMBER
    assert result["require_unsubscribe"] is True
    assert result["amber_count"] >= 1
    assert any("require_unsubscribe" in warning for warning in result["warnings"])

    # 2. 最重要的一條：命中抑制名單的線索不得出現在任何外送或草稿中
    delivered = _ids(result["sent"]) | _ids(result["drafted"])
    assert SUPPRESSED_LEAD_ID not in delivered
    blocked_by_suppression = {
        item["lead_id"] for item in result["blocked"] if item["reason"] == BLOCK_SUPPRESSED
    }
    assert SUPPRESSED_LEAD_ID in blocked_by_suppression
    # 高分不等於可以寄：被擋下的這筆仍然是 90 分的 Hot Lead
    assert _by_id(result["blocked"])[SUPPRESSED_LEAD_ID]["score"] == "90.00"
    assert _by_id(result["blocked"])[SUPPRESSED_LEAD_ID]["band"] == "hot"

    # 3. 自主權：命中白名單才自動送出，其餘一律降級為草稿
    assert _ids(result["sent"]) == {WHITELISTED_LEAD_ID}
    assert _ids(result["drafted"]) == {"L-2002"}
    assert result["sent"][0]["autonomy"] == "supervised_auto"
    assert all(item["autonomy"] == "draft" for item in result["drafted"])
    # 自動送出的信一樣要帶法定的退訂與識別區塊
    assert "請點此退訂" in result["sent"][0]["body"]

    # 4. mock 模式不落地狀態檔，QA 可重複執行得到相同結果
    assert result["is_state_persisted"] is False

    # 5. 結果必須可 JSON 序列化（供 CRM 回寫 / 法遵稽核留存）
    assert json.loads(json.dumps(result, ensure_ascii=False))["total_leads"] == 9
