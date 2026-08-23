"""模組 #4 測試（中等複雜度標準：happy / edge / integration 各一）。

edge case 專門驗 X 平台的 280 字元硬上限處理，因為那是本模組唯一
「做錯就直接被平台拒收」的規則，其餘格式問題頂多是風格不佳。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest
import yaml

_DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

import main as social_main  # noqa: E402
from generator import (  # noqa: E402
    GeneratorError,
    PlatformProfile,
    truncate_to_limit,
    validate_post,
)

X_PROFILE = PlatformProfile(
    platform_id="x",
    display_name="X",
    tone="簡短、犀利",
    char_limit=280,
    posts_per_week=7,
    prompt_file="prompts/x.md",
    schedule_slots=("MON 09:10",),
    emoji_allowed=False,
    hashtag_max=2,
)


def _args(**overrides: object) -> argparse.Namespace:
    """建立測試用參數；exit_on_red=False 讓紅色警報拋例外而非結束行程。"""
    namespace = argparse.Namespace(
        mock=True,
        live=False,
        dry_run=False,
        notify="console",
        config=str(_DEMO_DIR / "config.yaml"),
        exit_on_red=False,
    )
    for key, value in overrides.items():
        setattr(namespace, key, value)
    return namespace


def test_happy_path() -> None:
    """標準 mock 輸入：全平台一週內容產出且每則都符合平台格式限制。"""
    result = social_main.run(_args())

    assert result["module_id"] == "04"
    assert result["total_posts"] == sum(item["posts"] for item in result["platforms"])
    assert {item["id"] for item in result["platforms"]} == {
        "linkedin",
        "instagram",
        "x",
        "facebook",
    }

    for platform in result["platforms"]:
        assert 5 <= platform["posts"] <= 7, f"{platform['id']} 一週貼文數必須在 5-7 則"

    banned_words = ["最便宜", "保證", "全台第一", "療效", "秒殺"]
    for post in result["posts"]:
        assert post["text"].strip(), "貼文不可為空"
        assert post["char_count"] <= post["char_limit"]
        assert post["scheduled_for"], "每則貼文都要有建議發布時段"
        assert not any(word in post["text"] for word in banned_words)

    # 自主權預設 DRAFT：排程前一律待審，這是本模組的核心安全設計
    assert result["requested_autonomy"] == "draft"
    assert result["drafts"] == result["total_posts"]
    assert result["scheduled"] == 0
    assert result["review_minutes"] == 20


def test_edge_case_x_280_char_limit() -> None:
    """邊界：X 平台 280 字元上限的截斷、剛好等於上限、以及空白輸入。"""
    # 遠超上限 -> 截斷到 280 以內並發出警告
    over_limit = "淺焙不等於酸，這是萃取沒做完的問題。" * 40
    assert len(over_limit) > 280
    fitted, warnings = validate_post(over_limit, X_PROFILE, [])
    assert len(fitted) <= 280
    assert any("超過上限" in warning for warning in warnings)

    # 剛好等於上限 -> 原文不動、不發警告
    exact = "a" * 280
    unchanged, no_warnings = validate_post(exact, X_PROFILE, [])
    assert unchanged == exact
    assert no_warnings == []

    # 未超限的短文不應被 truncate 動到
    assert truncate_to_limit("一句話就講完的重點。", 280) == "一句話就講完的重點。"

    # 空白輸入不可靜默通過，必須拋出可辨識的例外
    with pytest.raises(GeneratorError):
        validate_post("   \n  ", X_PROFILE, [])


def test_integration_autonomy_and_diagnostics(tmp_path: Path) -> None:
    """與 _shared 的互動：autonomy 白名單降級、diagnostics amber、notifier console。"""
    config = yaml.safe_load((_DEMO_DIR / "config.yaml").read_text(encoding="utf-8"))
    config["runtime"]["autonomy"] = "supervised_auto"
    config["runtime"]["approved_senders"] = ["linkedin"]
    config["runtime"]["days_in_draft"] = 30

    # 語氣樣本刻意砍到 1 則，觸發契約中的 tone_mismatch（AMBER）
    voice = json.loads(
        (_DEMO_DIR / "mock" / "brand_voice.json").read_text(encoding="utf-8")
    )
    voice["tone_examples"] = voice["tone_examples"][:1]
    voice_path = tmp_path / "brand_voice.json"
    voice_path.write_text(json.dumps(voice, ensure_ascii=False), encoding="utf-8")
    config["content"]["brand_voice_file"] = str(voice_path)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    result = social_main.run(_args(config=str(config_path)))

    linkedin_posts = [p for p in result["posts"] if p["platform"] == "linkedin"]
    other_posts = [p for p in result["posts"] if p["platform"] != "linkedin"]
    assert linkedin_posts and all(p["status"] == "scheduled" for p in linkedin_posts)
    # 白名單外的平台必須降級為草稿，不可跟著一起自動排程
    assert other_posts and all(p["status"] == "draft" for p in other_posts)
    assert all(p["effective_autonomy"] == "draft" for p in other_posts)

    assert any("語氣樣本" in warning for warning in result["warnings"])
    assert result["amber_count"] >= 1

    # console 通道一定送得出去，且摘要要包含平台名稱與警告區塊
    assert result["notified"] is True
    assert "LinkedIn" in result["summary_text"]
    assert "審閱時要處理的提醒" in result["summary_text"]
