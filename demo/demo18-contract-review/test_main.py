"""demo18 三個測試（CONTRACT §8）：happy / edge / integration。

一律離線執行：不呼叫任何真實 API，合約全部來自 mock/ 目錄，
狀態檔一律以 --state-file 指到 tmp_path，跑完測試不會在模組目錄留下 state/。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo_main  # noqa: E402
from _shared.llm_client import LLMError  # noqa: E402
from classifier import Verdict  # noqa: E402
from extractor import load_contract, parse_money, verify_verbatim  # noqa: E402

MOCK_DIR = MODULE_DIR / "mock"


def _args(tmp_path: Path, **overrides: Any):
    """建出預設 CLI 參數；關掉 exit_on_red 讓紅色警報改拋例外而非結束行程。"""
    args = demo_main.build_parser().parse_args([])
    args.exit_on_red = False
    args.state_file = str(tmp_path / ".reviewed.json")
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """把條款判定攤成 clause_id -> assessment，方便逐條斷言。"""
    return {item["clause_id"]: item for item in result["review"]["assessments"]}


def _has_disclaimer(result: dict[str, Any]) -> bool:
    """免責聲明必須同時出現在結果欄位與備忘錄全文中（法遵必要，不可省略）。"""
    return "不構成法律意見" in result["disclaimer"] and "不構成法律意見" in result["memo"]


def test_happy_path(tmp_path: Path) -> None:
    """標準合約：14 條全部 Standard、零紅旗、零缺失，且每一則引文都是原文逐字。"""
    result = demo_main.run(_args(tmp_path, contract=str(MOCK_DIR / "contract_standard.json")))
    counts = result["review"]["counts"]

    assert result["mode"] == "mock"
    assert result["contract"]["contract_id"] == "MSA-2026-0311-NORTHGATE"
    assert len(result["review"]["assessments"]) == 14, "CLAUSE_LIBRARY 必須是 14 種標準條款"
    assert counts[Verdict.STANDARD.value] == 14
    assert counts[Verdict.DEVIATION.value] == 0
    assert counts[Verdict.MISSING.value] == 0
    assert counts[Verdict.RED_FLAG.value] == 0
    assert result["review"]["has_red_flag"] is False
    assert result["escalations"] == []
    assert result["review"]["needs_review_count"] == 0

    # 逐字引用鐵律：每一則引文都必須能在合約原文中精確比對到
    source = load_contract(MOCK_DIR / "contract_standard.json").full_text
    for item in result["review"]["assessments"]:
        assert item["is_verbatim_verified"] is True
        assert verify_verbatim(item["quote"], source), f"{item['clause_id']} 引文非原文逐字複製"

    # 金額基準走 Decimal：責任上限 GBP 120,000 恰等於年度金額 1 倍 → 通過
    assert parse_money("shall not exceed GBP 120,000. Neither") == parse_money("£120,000")
    assert _has_disclaimer(result), "備忘錄與結果都必須帶法律免責聲明"


def test_edge_case_verbatim_and_missing_clauses(tmp_path: Path) -> None:
    """邊界：缺漏三條關鍵保護條款必須被抓到；改寫過的引文必須驗證失敗。"""
    result = demo_main.run(_args(tmp_path, contract=str(MOCK_DIR / "contract_missing.json")))
    assessments = _by_id(result)
    counts = result["review"]["counts"]

    # 缺失條款必須被點名，且不可被誤判為通過
    missing_ids = {"data_protection", "insurance", "non_solicitation"}
    assert counts[Verdict.MISSING.value] == len(missing_ids)
    for clause_id in missing_ids:
        item = assessments[clause_id]
        assert item["verdict"] == Verdict.MISSING.value
        assert item["quote"] is None, "缺失條款不得產生任何引文"
        assert item["findings"], "缺失條款必須附上應補上的標準立場"
    assert any("缺失" in warning for warning in result["review"]["warnings"])
    assert result["amber_count"] >= 1

    # 逐字驗證：原文照抄過關；改一個字（Ninety→Sixty）就必須失敗
    document = load_contract(MOCK_DIR / "contract_missing.json")
    quote = assessments["payment_terms"]["quote"]
    assert verify_verbatim(quote, document.full_text)
    assert not verify_verbatim(quote.replace("thirty", "sixty"), document.full_text)
    assert not verify_verbatim("The Client shall pay promptly.", document.full_text)
    assert not verify_verbatim("", document.full_text), "空引文不得算通過"


def test_integration_red_flag_escalation_and_autonomy(tmp_path: Path) -> None:
    """整合：紅旗升級 + _shared 的 autonomy 降級 / diagnostics amber / console notifier。"""
    config = yaml.safe_load((MODULE_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"].update(
        {
            "autonomy": "supervised_auto",
            "approved_senders": ["@lawfirm.example"],
            "days_in_draft": 3,  # 未滿 14 天 → AutonomyGate 應發出警告
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    args = _args(
        tmp_path,
        config=str(config_path),
        notify="console",
        contract=str(MOCK_DIR / "contract_redflag.json"),
    )
    result = demo_main.run(args)
    assessments = _by_id(result)

    # 書中明列的兩條硬性紅線都必須命中，且引文是原文逐字切片
    rule_ids = {item["rule_id"] for item in result["escalations"]}
    assert rule_ids == {"unlimited_liability", "background_ip_assignment"}
    source = load_contract(MOCK_DIR / "contract_redflag.json").full_text
    for item in result["escalations"]:
        assert item["bypass_memo"] is True, "紅旗必須繞過常規備忘錄"
        assert item["recipient"] == "senior.partner@lawfirm.example"
        assert verify_verbatim(item["matched_text"], source)
    assert assessments["limitation_of_liability"]["verdict"] == Verdict.RED_FLAG.value
    assert assessments["intellectual_property"]["verdict"] == Verdict.RED_FLAG.value
    assert result["is_alert_sent"] is True

    # _shared 整合：白名單內自動送出、白名單外降級草稿、未滿 14 天累積警告
    actions = {item["recipient"]: item["action"] for item in result["deliveries"]}
    assert actions["reviewing.associate@lawfirm.example"] == "auto_sent"
    assert actions["counterparty.counsel@vendor.example"] == "draft", "白名單外必須降級為草稿"
    assert result["autonomy_warnings"], "未滿 14 天應累積自主權警告"
    assert result["amber_count"] >= 2

    # --state-file 必須真的生效，且第二次執行同一份合約時抑制重複警報
    assert result["state_file"] == str(tmp_path / ".reviewed.json")
    assert Path(result["state_file"]).is_file()
    second = demo_main.run(args)
    assert all(item["is_suppressed"] for item in second["escalations"])
    assert second["is_alert_sent"] is False


def test_main_catches_llm_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--live 模式下 CLI 逾時等狀況會拋 LLMError；main() 必須吃下來變成 exit code 1，
    而不是讓 raw traceback 砸給使用者（demo11 既有慣例的補齊）。
    """

    def _raise_llm_error(args) -> dict[str, Any]:
        raise LLMError("模擬 CLI 逾時")

    monkeypatch.setattr(demo_main, "run", _raise_llm_error)
    monkeypatch.setattr(sys, "argv", ["main.py"])

    exit_code = demo_main.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "錯誤：" in captured.err
    assert "模擬 CLI 逾時" in captured.err
