"""demo01 晨間情報簡報的三個測試（happy / edge / integration）。

全部離線執行：不呼叫 Anthropic API、不連 Google、不抓 RSS。
"""

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))
sys.path.insert(0, str(MODULE_DIR))

import main  # noqa: E402
from sources import email_source  # noqa: E402

CONFIG_PATH = MODULE_DIR / "config.yaml"
MOCK_FILENAMES = (("calendar", "calendar.json"), ("email", "emails.json"), ("news", "news.json"))


def parse_args(argv: list[str]) -> argparse.Namespace:
    """用正式的 parser 解析參數，確保測試走的是真正的 CLI 介面。"""
    return main.build_parser().parse_args(argv)


def build_config(tmp_path: Path, mutate: Callable[[dict], None] | None = None) -> Path:
    """複製正式 config.yaml 到暫存目錄，把相對路徑改成絕對路徑後套用覆寫。

    設定檔搬到 tmp_path 之後 base_dir 就跟著變，因此 prompt 與 mock 路徑
    必須先絕對化，否則會找不到檔案。
    """
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    config["briefing"]["prompt_file"] = (MODULE_DIR / "prompts" / "briefing.md").as_posix()
    for source_name, filename in MOCK_FILENAMES:
        config["sources"][source_name]["mock_file"] = (MODULE_DIR / "mock" / filename).as_posix()
    if mutate is not None:
        mutate(config)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return config_path


def test_happy_path() -> None:
    """標準 mock 輸入：三個來源都讀到、5 區塊齊全、字數守住 90 秒法則。"""
    result = main.run(parse_args(["--mock"]))

    assert result["mode"] == "mock"
    assert result["source_counts"] == {"calendar": 5, "email": 12, "news": 8}
    assert list(result["sections"]) == list(main.DEFAULT_SECTIONS)
    assert len(result["sections"]["TOP_3_PRIORITIES"]) == 3
    assert len(result["sections"]["KEY_MEETINGS"]) == 3
    assert len(result["sections"]["NEWS_ITEMS"]) == 3
    # 30 分鐘緩衝鐵律：06:00 執行、06:30 發送。
    assert result["schedule_buffer_minutes"] == 30
    assert result["is_within_hard_limit"] is True
    assert 0 < result["word_count"] <= result["target_word_max"] + 80
    for section_name in main.DEFAULT_SECTIONS:
        assert section_name in result["briefing"]


def test_edge_case_empty_sources(tmp_path: Path) -> None:
    """邊界：三個來源全空（連假、零會議、feed 全掛）仍要產出可讀簡報，不可崩潰。"""
    empty_payloads = {"calendar.json": "events", "emails.json": "messages", "news.json": "items"}
    for filename, list_key in empty_payloads.items():
        (tmp_path / filename).write_text(json.dumps({list_key: []}), encoding="utf-8")

    def use_empty_sources(config: dict) -> None:
        for source_name, filename in MOCK_FILENAMES:
            config["sources"][source_name]["mock_file"] = (tmp_path / filename).as_posix()

    config_path = build_config(tmp_path, use_empty_sources)
    result = main.run(parse_args(["--mock", "--dry-run", "--config", str(config_path)]))

    assert result["source_counts"] == {"calendar": 0, "email": 0, "news": 0}
    assert result["sections"]["KEY_MEETINGS"] == []
    assert result["sections"]["NEWS_ITEMS"] == []
    # 沒有急迫項目時仍要給一條可執行的建議，而不是空區塊。
    assert len(result["sections"]["TOP_3_PRIORITIES"]) == 1
    assert main.EMPTY_MARK in result["briefing"]
    assert result["word_count"] > 0
    assert result["is_dry_run"] is True
    assert result["is_delivered"] is False


def test_integration_amber_autonomy_and_notifier(tmp_path: Path) -> None:
    """整合：緩衝不足觸發 AMBER、READ_ONLY 禁止自動回覆、console 通道確實送出。"""

    def collapse_buffer(config: dict) -> None:
        # 執行與發送設在同一分鐘 → 違反 30 分鐘緩衝鐵律，應觸發 delayed_briefing。
        config["schedule"]["deliver_at"] = config["schedule"]["execute_at"]

    config_path = build_config(tmp_path, collapse_buffer)
    result = main.run(parse_args(["--mock", "--notify", "console", "--config", str(config_path)]))

    assert result["schedule_buffer_minutes"] == 0
    assert result["amber_count"] >= 1
    assert result["autonomy_level"] == "read_only"
    assert result["can_auto_reply"] is False
    assert result["notify_channel"] == "console"
    assert result["is_delivered"] is True


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    """組出一個 subprocess.CompletedProcess，模擬 gws 呼叫的成功回應。"""
    return subprocess.CompletedProcess(args=["gws"], returncode=returncode, stdout=stdout, stderr="")


def test_run_gws_uses_resolved_path_from_shutil_which() -> None:
    """happy path：shutil.which 解析出完整路徑時，subprocess.run 收到的 argv[0] 是該路徑而非裸字串 "gws"。

    Windows 上裸字串 "gws"（無副檔名）在 shell=False 下不會被 CreateProcess 自動補上
    PATHEXT，直接呼叫會 FileNotFoundError；必須改用 shutil.which 解析出的完整路徑。
    """
    diag = MagicMock()
    resolved = "C:\\fake\\npm\\gws.CMD"
    with (
        patch("sources.email_source.shutil.which", return_value=resolved) as mock_which,
        patch(
            "sources.email_source.subprocess.run", return_value=_completed('{"ok": true}')
        ) as mock_run,
    ):
        result = email_source._run_gws("gws", ["gmail", "users", "messages", "list"], diag)
    mock_which.assert_called_once_with("gws")
    argv = mock_run.call_args.args[0]
    assert argv[0] == resolved
    assert result == {"ok": True}
    diag.red.assert_not_called()


def test_run_gws_falls_back_to_literal_command_when_which_cannot_resolve() -> None:
    """edge/error：shutil.which 解析不到時原樣保留 cli_command，讓既有 FileNotFoundError 分支自然觸發。"""
    diag = MagicMock()
    with (
        patch("sources.email_source.shutil.which", return_value=None),
        patch("sources.email_source.subprocess.run", side_effect=FileNotFoundError("no such file")),
    ):
        with pytest.raises(email_source.SourceError):
            email_source._run_gws("gws", ["gmail", "users", "messages", "list"], diag)
    diag.red.assert_called_once()


def test_fetch_live_messages_end_to_end_with_resolved_gws_path() -> None:
    """integration：_fetch_live_messages 端對端跑一次（list + get 兩階段），確認兩次呼叫都用解析後的完整路徑。"""
    diag = MagicMock()
    resolved = "C:\\fake\\npm\\gws.CMD"
    listing_json = json.dumps({"messages": [{"id": "m1"}]})
    detail_json = json.dumps(
        {
            "payload": {
                "headers": [
                    {"name": "From", "value": "vip@example.com"},
                    {"name": "Subject", "value": "測試主旨"},
                    {"name": "Date", "value": "2026-01-01"},
                ]
            },
            "snippet": "測試摘要",
        }
    )
    with (
        patch("sources.email_source.shutil.which", return_value=resolved),
        patch(
            "sources.email_source.subprocess.run",
            side_effect=[_completed(listing_json), _completed(detail_json)],
        ) as mock_run,
    ):
        messages = email_source._fetch_live_messages(
            {"query": "is:unread newer_than:1d", "max_results": 5}, diag
        )
    assert len(messages) == 1
    assert messages[0]["from"] == "vip@example.com"
    assert messages[0]["subject"] == "測試主旨"
    assert messages[0]["summary"] == "測試摘要"
    assert mock_run.call_count == 2
    for call in mock_run.call_args_list:
        assert call.args[0][0] == resolved
