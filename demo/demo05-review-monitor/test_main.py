"""demo05 測試（3 個：happy / edge / integration）。

一律離線可跑：不呼叫任何真實 API，去重狀態檔一律指向 pytest 的 tmp_path，
避免污染模組目錄下的 .seen.json。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import monitor as review_monitor  # noqa: E402


def _load_main() -> Any:
    """以唯一模組名載入 main.py，避免整個 repo 一起跑 pytest 時與其他 demo 的 main 撞名。"""
    spec = importlib.util.spec_from_file_location("demo05_main", MODULE_DIR / "main.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"無法載入 {MODULE_DIR / 'main.py'}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo05_main"] = module
    spec.loader.exec_module(module)
    return module


demo05 = _load_main()


def _run(tmp_path: Path, extra: list[str] | None = None) -> dict[str, Any]:
    """跑一次 mock 流程，狀態檔隔離在 tmp_path。"""
    argv = ["--mock", "--notify", "console", "--state-file", str(tmp_path / ".seen.json")]
    args = demo05.build_parser().parse_args(argv + (extra or []))
    return demo05.run(args)


def test_happy_path(tmp_path: Path) -> None:
    """標準 mock 輸入 -> run() 回傳預期結構，且每則新評價都有草稿。"""
    result = _run(tmp_path)

    assert result["module_id"] == "05"
    assert result["mode"] == "mock"
    assert result["fetch_errors"] == []
    assert result["poll_interval_hours"] == 6
    # google 6 則 + trustpilot 4 則（amazon 在 config 中 enabled: false）
    assert result["fetched"] == 10
    assert result["new"] == result["fetched"]
    assert result["skipped_duplicates"] == 0
    assert len(result["drafts"]) == result["new"]
    assert all(draft["reply"] for draft in result["drafts"])
    assert Path(result["state_file"]).is_file()


def _write_edge_fixtures(tmp_path: Path) -> Path:
    """寫一份臨時設定 + mock 評價檔，內含一則星等無法解析的評價，回傳設定檔路徑。

    兩個實作細節：
    1. YAML 是 JSON 的超集，直接 dump JSON 就是合法 YAML，測試不必自己依賴 pyyaml。
    2. mock_file 給絕對路徑：monitor 以 module_dir() 為基底做 join，pathlib 遇到絕對路徑
       會直接採用該路徑，因此臨時檔可以放在 tmp_path 而不污染模組目錄。
    """
    reviews_path = tmp_path / "reviews_edge.json"
    reviews = [
        {"review_id": "e-000", "author": "欄位改版", "rating": "not-a-number", "title": "星等欄位改版", "text": "平台改版後 rating 變成字串。"},
        {"review_id": "e-001", "author": "王小姐", "rating": 1, "title": "沒收到貨", "text": "付款兩週還沒出貨也沒人回。"},
        {"review_id": "e-002", "author": "Ken", "rating": 5, "title": "很好", "text": "出貨快，包裝完整。"},
    ]
    reviews_path.write_text(json.dumps({"reviews": reviews}, ensure_ascii=False), encoding="utf-8")
    config = {
        "module": {"id": "05", "name": "客戶評價監控與回覆草擬"},
        "runtime": {"autonomy": "draft", "approved_senders": [], "days_in_draft": 0},
        "monitor": {
            "poll_interval_hours": 6,
            "escalation_threshold_stars": 2,
            "escalation_minutes": 30,
            "fetch_delay_seconds": 1.0,
            "digest_max_items": 10,
            "platforms": [{"id": "google", "enabled": True, "mock_file": str(reviews_path)}],
        },
        "brand_voice": {
            "brand_name": "Brightleaf Living",
            "profile_file": "mock/brand_voice.json",
            "tone": "溫暖、務實、負責",
            "signature": "— Sarah Chen，Brightleaf Living 客戶體驗團隊",
            "max_words": 120,
            "banned_phrases": [],
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    return config_path


def test_edge_case_unknown_rating_and_duplicates(tmp_path: Path) -> None:
    """邊界一：星等無法解析（rating=0）。邊界二：第二次執行時全部命中去重。"""
    # 單元層：星等是字串一律壓成 0，不可靜默當成好評讓負評消失
    broken = review_monitor.normalize_review({"review_id": "x-1", "rating": "not-a-number"}, "google")
    assert broken["rating"] == 0

    config_path = _write_edge_fixtures(tmp_path)
    first = _run(tmp_path, ["--config", str(config_path)])

    assert first["fetched"] == 3
    assert first["new"] == 3
    unknown_key = "google:e-000"
    escalation_keys = {item["key"] for item in first["escalations"]}
    routine_keys = {item["key"] for item in first["routine"]}
    # 這三條斷言是刻意的：未知星等不等於負評。強制把解析失敗推去 escalation 會造成警報疲勞
    # ——平台改版一次就轟炸使用者數十則，他關掉通知之後，真正的 1 星負評才會被漏掉。
    # 未知星等走一般路徑但絕不可被丟棄，並且必須留下 amber 讓維運看見。
    assert unknown_key not in escalation_keys
    assert unknown_key in routine_keys
    assert first["amber_count"] >= 1
    # 對照組：真正的 1 星仍然走升級路徑，證明上面的區隔是設計而非漏改
    assert escalation_keys == {"google:e-001"}

    # 未知星等也必須出現在老闆會看的 digest 裡（amber 只進 stderr，老闆不會去翻 log）
    digest = demo05.format_digest(first["routine"], "客戶評價監控", 10)
    assert "星等無法判讀" in digest
    assert "星等未知" in digest

    # 邊界二：同一組資料再跑一次，全部命中去重
    second = _run(tmp_path, ["--config", str(config_path)])
    assert second["fetched"] == first["fetched"]
    assert second["new"] == 0
    assert second["skipped_duplicates"] == first["fetched"]
    assert second["escalations"] == []
    assert second["routine"] == []
    assert second["drafts"] == []
    assert second["notified"]["escalation_sent"] == 0
    assert second["notified"]["digest_sent"] is False


def test_integration_dual_path(tmp_path: Path) -> None:
    """整合：1-2 星走 escalation 路徑、3-5 星走一般路徑，且 autonomy 停在 DRAFT。"""
    result = _run(tmp_path)

    escalations = result["escalations"]
    routine = result["routine"]
    # mock 資料中恰有 1 則 1 星與 1 則 2 星
    assert len(escalations) == 2
    assert sorted(item["rating"] for item in escalations) == [1, 2]
    assert all(item["rating"] >= 3 for item in routine)
    assert len(routine) == result["fetched"] - len(escalations)
    assert result["escalation_threshold_stars"] == 2
    assert result["escalation_minutes"] == 30

    # 雙軌通知：負評逐則單獨推播，一般評價只發一則彙整
    assert result["notified"]["escalation_sent"] == len(escalations)
    assert result["notified"]["escalation_pending"] == 0
    assert result["notified"]["digest_sent"] is True

    # 自主權：公開回覆一律停在 DRAFT，任何草稿都不得標記為可自動送出
    assert result["autonomy_level"] == "draft"
    assert all(draft["autonomy"] == "draft" for draft in result["drafts"])
    assert all(draft["can_send"] is False for draft in result["drafts"])

    # 負評的草稿也必須存在（審查後由人工送出）
    escalation_keys = {item["key"] for item in escalations}
    drafted_keys = {draft["key"] for draft in result["drafts"] if draft["is_escalation"]}
    assert drafted_keys == escalation_keys
