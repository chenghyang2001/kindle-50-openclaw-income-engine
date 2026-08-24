"""demo02 測試：happy / edge / integration 三案（CONTRACT §8）。

全部離線執行，不呼叫任何真實 API。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

_DEMO_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_DEMO_DIR.parent))
sys.path.insert(0, str(_DEMO_DIR))

import main as inbox_zero  # noqa: E402

BASE_CONFIG = _DEMO_DIR / "config.yaml"


def _write_config(tmp_path: Path, runtime: dict | None = None,
                  emails_path: Path | None = None) -> Path:
    """複製正式設定檔到暫存目錄，把相對路徑改成絕對路徑後套用覆寫值。

    路徑改絕對是必要的：模組以設定檔所在目錄解析相對路徑，
    設定檔搬到 tmp_path 之後，相對路徑就會指向不存在的地方。
    """
    cfg = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    cfg["inbox"]["mock_emails"] = str(emails_path or (_DEMO_DIR / "mock" / "emails.json"))
    cfg["inbox"]["tone_examples"] = str(_DEMO_DIR / "mock" / "tone_examples.json")
    cfg["prompts"] = {key: str(_DEMO_DIR / value)
                      for key, value in cfg["prompts"].items()}
    cfg["runtime"].update(runtime or {})
    target = tmp_path / "config.yaml"
    target.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return target


def _run(config_path: Path, extra: list[str] | None = None) -> dict:
    """用 CLI 介面組出參數並執行主流程。"""
    argv = ["--mock", "--config", str(config_path)] + (extra or [])
    return inbox_zero.run(inbox_zero.build_parser().parse_args(argv))


def test_happy_path(tmp_path: Path) -> None:
    """標準 60 封 mock 收件匣：分類數量、草稿數量、審閱時間估算都要對得上。"""
    result = _run(_write_config(tmp_path))

    assert result["counts"] == {"total": 60, "vip": 8, "fyi": 30, "spam": 22}
    assert result["mode"] == "mock"
    # 預設 DRAFT：8 封 VIP 全部只建草稿，一封都不自動送出。
    assert result["autonomy"]["configured"] == "draft"
    assert result["actions"] == {"draft": 8, "auto_send": 0}
    assert all(draft["dispatched"] is False for draft in result["drafts"])
    # 3.0 + 1.5 * 8 = 15.0 分鐘，對應書中「總審閱時間 15 分鐘」的承諾。
    assert result["review_minutes_estimate"] == 15.0
    # AUTO_UNSUBSCRIBE 預設關閉，22 封垃圾信只列出來給人看。
    assert result["auto_unsubscribe"] == {"requested": False, "enabled": False}
    assert len(result["unsubscribe_candidates"]) == 22
    assert all(item["action"] == "review_only"
               for item in result["unsubscribe_candidates"])
    # 冒充 VIP 主旨（urgent + invoice）的釣魚信要被抓出來人工覆核。
    assert len(result["suspected_misclassifications"]) >= 1


def test_edge_case_dirty_inbox(tmp_path: Path) -> None:
    """髒資料：缺欄位、空字串、emoji、超長內文都不可中斷整批作業。"""
    emails_path = tmp_path / "edge_emails.json"
    emails_path.write_text(json.dumps([
        {"from": "", "subject": "", "body": "", "received_at": ""},
        {"from": "no-angle-brackets@example.com"},
        {"from": "測試 🚀 <emoji.sender@example.com>",
         "subject": "重複的標點！！！？？？ 🚀🚀🚀",
         "body": "換行\n\n\t制表符與 emoji 🚀 混雜", "received_at": "2026-08-23T10:00:00+08:00"},
        {"from": "Long <long@example.com>", "subject": "x" * 500,
         "body": "y" * 20000, "received_at": "2026-08-23T11:00:00+08:00"},
    ], ensure_ascii=False), encoding="utf-8")

    result = _run(_write_config(tmp_path, emails_path=emails_path), ["--dry-run"])

    assert result["counts"]["total"] == 4
    assert result["counts"]["vip"] == 0
    assert result["drafts"] == []
    assert all(row["category"] in {"VIP", "FYI", "SPAM"}
               for row in result["classified"])
    # 整批沒有任何 VIP，代表 VIP_SENDERS 很可能設錯，必須升琥珀色警示。
    assert result["amber_count"] >= 1
    assert result["notified"] is False


def test_integration_supervised_auto_downgrade(tmp_path: Path) -> None:
    """SUPERVISED_AUTO 只放行白名單，非白名單收件人一律降級為 DRAFT。"""
    config_path = _write_config(tmp_path, runtime={
        "autonomy": "supervised_auto",
        "approved_senders": ["@northwind-retail.com"],
        "days_in_draft": 21,
    })

    result = _run(config_path, ["--dry-run"])

    assert result["autonomy"]["configured"] == "supervised_auto"
    # 8 封 VIP 中只有 1 封來自白名單網域，其餘 7 封必須降級。
    assert result["actions"] == {"draft": 7, "auto_send": 1}

    whitelisted = [d for d in result["drafts"]
                   if d["to"].endswith("@northwind-retail.com")]
    others = [d for d in result["drafts"]
              if not d["to"].endswith("@northwind-retail.com")]
    assert len(whitelisted) == 1
    assert whitelisted[0]["effective_level"] == "supervised_auto"
    assert whitelisted[0]["action"] == "auto_send"
    assert others and all(d["effective_level"] == "draft" for d in others)
    assert others and all(d["action"] == "draft" for d in others)
    # --dry-run：判斷照做，但一封都不真的送出。
    assert all(d["dispatched"] is False for d in result["drafts"])


def _completed(stdout: str) -> subprocess.CompletedProcess:
    """組出一個 subprocess.CompletedProcess，模擬 gws 呼叫的成功回應。"""
    return subprocess.CompletedProcess(args=["gws"], returncode=0, stdout=stdout, stderr="")


def test_gws_json_replaces_literal_gws_with_resolved_path() -> None:
    """happy path：argv[0] 是字面 "gws" 時，_gws_json 用 shutil.which 解析出的完整路徑取代它，

    subprocess.run 實際收到的 argv[0] 不能還是裸字串 "gws"（Windows shell=False 下無副檔名
    的裸指令名不會被 CreateProcess 自動補上 PATHEXT，會直接 FileNotFoundError）。
    """
    diag = MagicMock()
    resolved = "C:\\fake\\npm\\gws.CMD"
    with (
        patch("main.shutil.which", return_value=resolved) as mock_which,
        patch("main.subprocess.run", return_value=_completed('{"ok": true}')) as mock_run,
    ):
        result = inbox_zero._gws_json(
            ["gws", "gmail", "users", "messages", "list", "--params", "{}"], diag
        )
    mock_which.assert_called_once_with("gws")
    argv = mock_run.call_args.args[0]
    assert argv[0] == resolved
    assert argv[1:] == ["gmail", "users", "messages", "list", "--params", "{}"]
    assert result == {"ok": True}
    diag.red.assert_not_called()


def test_gws_json_falls_back_and_raises_redalert_when_which_cannot_resolve() -> None:
    """edge/error：shutil.which 解析不到時原樣保留 "gws"，讓既有 FileNotFoundError 分支自然觸發並拋 RedAlert。"""
    diag = MagicMock()
    with (
        patch("main.shutil.which", return_value=None),
        patch("main.subprocess.run", side_effect=FileNotFoundError("no such file")),
    ):
        with pytest.raises(inbox_zero.RedAlert):
            inbox_zero._gws_json(["gws", "gmail", "users", "messages", "list"], diag)
    diag.red.assert_called_once()


def test_fetch_live_emails_end_to_end_with_resolved_gws_path() -> None:
    """integration：_fetch_live_emails 端對端跑一次（list + get 兩階段），確認兩次呼叫都用解析後的完整路徑。"""
    diag = MagicMock()
    resolved = "C:\\fake\\npm\\gws.CMD"
    listing_json = json.dumps({"messages": [{"id": "m1"}]})
    detail_json = json.dumps({
        "payload": {
            "headers": [
                {"name": "From", "value": "vip@example.com"},
                {"name": "Subject", "value": "測試主旨"},
                {"name": "Date", "value": "2026-01-01"},
            ],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    })
    cfg = {"inbox": {"live_query": "is:unread newer_than:1d", "live_max_results": 10}}
    with (
        patch("main.shutil.which", return_value=resolved),
        patch(
            "main.subprocess.run",
            side_effect=[_completed(listing_json), _completed(detail_json)],
        ) as mock_run,
    ):
        emails = inbox_zero._fetch_live_emails(cfg, diag)
    assert len(emails) == 1
    assert emails[0]["from"] == "vip@example.com"
    assert emails[0]["subject"] == "測試主旨"
    assert mock_run.call_count == 2
    for call in mock_run.call_args_list:
        assert call.args[0][0] == resolved
