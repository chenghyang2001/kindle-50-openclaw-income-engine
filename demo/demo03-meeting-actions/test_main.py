"""demo03 三個測試（CONTRACT §8）：happy / edge / integration。

一律離線執行：不呼叫任何真實 API，逐字稿全部來自 mock/ 目錄。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as demo_main  # noqa: E402
from extractor import classify_utterance, extract_due_hint  # noqa: E402

MOCK_DIR = MODULE_DIR / "mock"


def _args(**overrides: Any):
    """建出預設 CLI 參數，並關掉 exit_on_red 讓紅色警報改拋例外而非結束行程。"""
    args = demo_main.build_parser().parse_args([])
    args.exit_on_red = False
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_happy_path() -> None:
    """結構良好的逐字稿：4 項明確承諾全部有負責人，模糊句一律不入清單。"""
    result = demo_main.run(_args(transcript=str(MOCK_DIR / "transcript_clear.json")))
    meeting = result["meetings"][0]

    assert result["mode"] == "mock"
    assert meeting["transcript_id"] == "mtg-2026-08-24-q3-pipeline"
    assert meeting["quality"]["profile"] == "clear"
    assert len(meeting["action_items"]) == 4
    assert {item["owner"] for item in meeting["action_items"]} == {
        "Priya Raman",
        "Marcus Feld",
        "Sofia Lindqvist",
    }
    assert meeting["unassigned_count"] == 0
    assert len(meeting["decisions"]) == 2
    assert extract_due_hint("I will send the checklist by Friday.") == "Friday"

    # 模糊推論必須被丟棄——這是本模組最重要的品質保證
    assert classify_utterance("We should probably revisit pricing at some point.") is None
    assert classify_utterance("Maybe someone could look at the analytics dashboard.") is None
    assert all(
        "probably" not in item["text"] and "Maybe" not in item["text"]
        for item in meeting["action_items"]
    )


def test_edge_case_owner_is_never_guessed() -> None:
    """邊界：有承諾但無人被指名時，owner 必須是 null，且不得挑一個與會者頂上。"""
    result = demo_main.run(_args(transcript=str(MOCK_DIR / "transcript_no_owner.json")))
    meeting = result["meetings"][0]

    assert meeting["action_items"], "無負責人逐字稿仍應擷取到明確承諾"
    assert all(item["owner"] is None for item in meeting["action_items"])
    assert meeting["unassigned_count"] == len(meeting["action_items"])
    assert any("負責人" in warning for warning in meeting["warnings"])
    assert "⚠ 未指定負責人" in meeting["message"]
    assert result["amber_count"] >= 1

    # "Someone needs to chase the vendor" 屬模糊推論，不可被當成承諾
    assert all("Someone needs" not in item["text"] for item in meeting["action_items"])


def test_integration_autonomy_downgrade_and_amber(tmp_path: Path) -> None:
    """整合：_shared 的 autonomy 白名單降級 + diagnostics amber + console notifier。"""
    config = yaml.safe_load((MODULE_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"].update(
        {
            "autonomy": "supervised_auto",
            "approved_senders": ["@triforge.example"],
            "days_in_draft": 3,  # 未滿 14 天 → AutonomyGate 應發出警告
        }
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")

    result = demo_main.run(
        _args(
            config=str(config_path),
            notify="console",
            transcript=str(MOCK_DIR / "transcript_messy.json"),
        )
    )
    actions = {item["recipient"]: item["action"] for item in result["meetings"][0]["deliveries"]}

    assert actions["david@triforge.example"] == "auto_sent"
    assert actions["marcus@vendor-lab.example"] == "draft", "白名單外必須降級為草稿"
    assert result["autonomy_warnings"], "未滿 14 天應累積自主權警告"
    assert result["meetings"][0]["quality"]["profile"] == "messy"
    assert result["amber_count"] >= 1
