"""notifier.py 的 gws CLI 路徑解析測試（複雜度：medium，3 個測試案例：happy / edge / integration）。

只補這次改動相關的測試（_send_gmail 改用 shutil.which 解析出的完整路徑呼叫 subprocess.run），
不重新測整份 notifier.py（console/telegram/line/whatsapp 等既有通道邏輯未變動，不在本次範圍）。

全部用 `unittest.mock.patch` 模擬 `shutil.which` 與 `subprocess.run`，
絕對不真的呼叫 `gws` CLI（會動到使用者真實 Gmail 資料、也會拖慢測試）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))  # 讓 `from _shared...` 可解析
sys.path.insert(0, str(MODULE_DIR))  # 讓「直接以腳本執行本檔」的 fallback import 也能用

from _shared.notifier import Notifier, NotifierError  # noqa: E402


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """組出一個 subprocess.CompletedProcess，模擬 gws 呼叫的回應。"""
    return subprocess.CompletedProcess(args=["gws"], returncode=returncode, stdout=stdout, stderr=stderr)


# 1. happy path：shutil.which 解析出完整路徑時，_send_gmail 用該路徑（而非裸字串 "gws"）呼叫 subprocess.run
def test_send_gmail_uses_resolved_gws_path() -> None:
    resolved = "C:\\fake\\npm\\gws.CMD"
    notifier = Notifier(channel="gmail", config={"to": "someone@example.com"})
    with (
        patch("_shared.notifier.shutil.which", return_value=resolved) as mock_which,
        patch("_shared.notifier.subprocess.run", return_value=_completed(0)) as mock_run,
    ):
        ok = notifier._send_gmail("測試內文", "測試主旨")
    mock_which.assert_called_once_with("gws")
    argv = mock_run.call_args.args[0]
    assert argv[0] == resolved
    assert argv[1:5] == ["gmail", "users", "messages", "send"]
    assert ok is True


# 2. edge/error：shutil.which 解析不到 gws 時直接拋 NotifierError，不嘗試用裸字串呼叫 subprocess.run
def test_send_gmail_raises_when_gws_not_found() -> None:
    notifier = Notifier(channel="gmail", config={"to": "someone@example.com"})
    with (
        patch("_shared.notifier.shutil.which", return_value=None),
        patch("_shared.notifier.subprocess.run") as mock_run,
    ):
        try:
            notifier._send_gmail("測試內文", "測試主旨")
            raised = False
        except NotifierError:
            raised = True
    assert raised is True
    mock_run.assert_not_called()


# 3. integration：走公開的 Notifier.send() 端對端跑一次，確認整條 gmail 通道（含 --safe 的錯誤吞掉機制）
#    在 gws 路徑解析換掉之後仍正常運作，回傳 True 且不外洩例外。
def test_notifier_send_gmail_end_to_end_success() -> None:
    resolved = "C:\\fake\\npm\\gws.CMD"
    notifier = Notifier(channel="gmail", config={"to": "someone@example.com"})
    with (
        patch("_shared.notifier.shutil.which", return_value=resolved),
        patch("_shared.notifier.subprocess.run", return_value=_completed(0)) as mock_run,
    ):
        ok = notifier.send("這是通知內文", subject="這是主旨")
    assert ok is True
    argv = mock_run.call_args.args[0]
    assert argv[0] == resolved
    # send() 走公開介面，內部組出的 payload 要能被完整帶到 subprocess.run，
    # 驗證 --json 之後緊接著 base64 編碼過的 raw payload（非空字串）確實有被傳入。
    assert "--json" in argv
    payload = argv[argv.index("--json") + 1]
    assert isinstance(payload, str) and len(payload) > 0


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
