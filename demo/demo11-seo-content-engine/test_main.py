"""模組 #11 測試（中等複雜度標準：happy / edge / integration 各一）。

edge case 專門驗「排名 8-20」這條邊界，因為那是本模組唯一一條
**寫錯就會選錯題**的商業規則（附錄F p04 的 prompt.txt 逐字要求），
其餘問題頂多是文章寫得不夠好，可以在審稿階段補救。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main as seo_main  # noqa: E402
from keyword_planner import (  # noqa: E402
    SOURCE_SEED_EXPANSION,
    TIER_ALREADY_RANKING,
    TIER_STRIKING,
    KeywordPlannerError,
    SelectionSettings,
    is_striking_distance,
    load_candidates,
    select_topics,
    tier_of,
)

CONFIG_PATH = MODULE_DIR / "config.yaml"


def _config() -> dict:
    """讀一份設定檔副本供測試改寫。"""
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _args(**overrides: object) -> argparse.Namespace:
    """建立測試用參數；exit_on_red=False 讓紅色警報拋例外而非結束行程。"""
    namespace = argparse.Namespace(
        mock=True,
        live=False,
        dry_run=False,
        notify="console",
        config=str(CONFIG_PATH),
        state_file=None,
        exit_on_red=False,
    )
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def test_happy_path(tmp_path: Path) -> None:
    """標準 mock 輸入：選出排名 8-20 的三個字並產出完整草稿。"""
    config = _config()
    result = seo_main.run(_args(state_file=str(tmp_path / ".state.json")))

    assert result["module_id"] == "11"
    assert result["mode"] == "mock"
    # apxF_p04 的 context.json 逐字：{"trigger": {"type": "cron", "schedule": "0 8 * * 1"}}
    assert result["schedule"] == "0 8 * * 1"
    assert result["articles_planned"] == 3
    assert result["articles_drafted"] == 3
    assert result["stats"]["reviewed_count"] == config["search_data"]["top_keywords"]

    # 機會分數排序的結果：三個都必須落在可攻擊距離內
    assert set(result["selected_keywords"]) == {
        "庫存週轉率 怎麼算",
        "庫存盤點 表格 範本",
        "倉儲管理系統 費用",
    }

    settings = config["content_settings"]
    for row in result["articles"]:
        assert 8 <= row["position"] <= 20, "選中的字必須在排名 8-20 的甜蜜區"
        assert row["section_count"] == settings["h2_sections"]
        assert row["faq_count"] == settings["faq_questions"]
        assert row["link_count"] >= settings["internal_links"]["min_per_article"]
        assert settings["min_words"] <= row["word_count"] <= settings["max_words"]
        # 需要具體數字的位置一律留標記，不可讓模型自行編造
        assert row["placeholder_count"] > 0
        assert row["cms_payload"]["status"] == "draft"
        assert row["cms_payload"]["content_markdown"].startswith("# ")

    # 曝光量／難度門檻確實有把字擋下來，而且擋下的原因要說得出來
    assert any("難度" in item for item in result["keywords_rejected"])

    # 自主權預設 DRAFT：推送 CMS 前一律待審
    assert result["requested_autonomy"] == "draft"
    assert result["drafts"] == 3
    assert result["scheduled"] == 0
    assert not [item for item in result["warnings"] if "禁用詞" in item]

    # 金額一律 Decimal 計算後輸出字串：65 小時 × $75 = $4,875
    financials = result["financials"]
    assert financials["monthly_value"] == "4875.00"
    assert financials["client_setup_price"] == "750.00"
    assert financials["client_monthly_price"] == "200.00"
    assert financials["recurring_net"] == "4675.00"
    # 第05章的 premium 報價並存，兩套都要算得出來
    assert financials["premium_monthly_price"] == "400.00"

    assert Path(result["state_file"]).is_file(), "非 dry-run 應寫回狀態檔"


def test_edge_case_striking_distance_boundary(tmp_path: Path) -> None:
    """邊界：排名 8-20 的上下界、空資料來源、以及全數在冷卻期時的補位行為。"""
    settings = SelectionSettings(
        position_min=8,
        position_max=20,
        min_impressions=120,
        max_difficulty=55,
        top_keywords=12,
        articles_per_week=3,
    )

    # 邊界值含頭含尾，差 0.1 就出局；沒有排名資料一律不算
    assert is_striking_distance(8.0, 8, 20) is True
    assert is_striking_distance(20.0, 8, 20) is True
    assert is_striking_distance(7.9, 8, 20) is False
    assert is_striking_distance(20.1, 8, 20) is False
    assert is_striking_distance(None, 8, 20) is False

    config = _config()
    seeds = [str(item) for item in config["content_settings"]["seed_topics"]]
    modifiers = [str(item) for item in config["content_settings"]["long_tail_modifiers"]]
    candidates = load_candidates(MODULE_DIR / "mock" / "search_console.json", seeds)

    by_query = {item.query: item for item in candidates}
    assert tier_of(by_query["庫存週轉率 怎麼算"], settings) == TIER_STRIKING
    # 排名 1-7 已經在前段，增量最小，優先層必須排在可攻擊字之後
    assert tier_of(by_query["北原倉儲科技"], settings) == TIER_ALREADY_RANKING

    # 資料來源為空 -> 明確拋錯，不可回傳空清單讓下游誤以為「本週沒題目」
    empty_file = tmp_path / "empty.json"
    empty_file.write_text('{"rows": []}', encoding="utf-8")
    with pytest.raises(KeywordPlannerError):
        load_candidates(empty_file, seeds)

    # 全部關鍵字都在冷卻期 -> 用 SEED_TOPICS 擴展字補位，且必須留下警告
    outcome = select_topics(
        candidates, settings, {item.query for item in candidates}, seeds, modifiers
    )
    assert len(outcome["selected"]) == 3
    assert all(item.source == SOURCE_SEED_EXPANSION for item in outcome["selected"])
    assert any("長尾擴展" in item for item in outcome["warnings"])
    assert any("冷卻期" in item for item in outcome["warnings"])


def test_integration_autonomy_and_diagnostics(tmp_path: Path) -> None:
    """與 _shared 的互動：autonomy 白名單降級、diagnostics amber、notifier console。"""
    config = _config()
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["庫存盤點"]
    # 只跑 3 天就開全自動：不擋，但一定要留下書中鐵律的警告
    config["runtime"]["days_in_draft"] = 3

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = seo_main.run(
        _args(config=str(config_path), state_file=str(tmp_path / ".state.json"))
    )

    whitelisted = [row for row in result["articles"] if row["seed_topic"] == "庫存盤點"]
    others = [row for row in result["articles"] if row["seed_topic"] != "庫存盤點"]
    assert whitelisted and all(row["status"] == "publish" for row in whitelisted)
    # 白名單外的主題必須降級成草稿，不可跟著一起自動發布
    assert others and all(row["status"] == "draft" for row in others)
    assert all(row["effective_autonomy"] == "draft" for row in others)
    assert all(row["cms_payload"]["status"] == "draft" for row in others)

    assert any("SUPERVISED_AUTO" in item for item in result["warnings"])
    assert result["amber_count"] >= 1

    # console 通道一定送得出去，摘要要看得到選題、稿件與提醒三個區塊
    assert result["notified"] is True
    assert "週一內容簡報" in result["summary_text"]
    assert "庫存盤點 表格 範本" in result["summary_text"]
    assert "審閱時要處理的提醒" in result["summary_text"]
