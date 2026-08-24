"""llm_client 的 claude CLI headless 化測試（複雜度：complex，22 個 mock 測試 + 1 個預設跳過的整合測試）。

全部用 `unittest.mock.patch` 模擬 `shutil.which` 與 `subprocess.run`，
絕對不真的呼叫 `claude` CLI（會產生真實 API 用量、拖慢測試、且在沒裝 CLI 的機器上會失敗）。

唯一例外是檔案最後一個整合測試：預設用 `pytest.mark.skipif` 跳過，
只有環境變數 `OPENCLAW_RUN_LIVE_CLI_TESTS=1` 存在時才真的打本機 `claude` CLI，
用來當 `--safe-mode` 之類安全旗標未來被意外刪掉的回歸防線
（這次的 MUST_FIX 就是因為 22 個 mock 測試全部繞過了 subprocess.run 才完全沒被抓到）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))  # 讓 `from _shared...` 可解析
sys.path.insert(0, str(MODULE_DIR))  # 讓「直接以腳本執行本檔」的 fallback import 也能用

from _shared.diagnostics import Diagnostics, RedAlert  # noqa: E402
from _shared.llm_client import CLI_COMMAND, LLMClient, LLMError  # noqa: E402
import _shared.llm_client as llm_client_module  # noqa: E402


def _use_non_exiting_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    """把模組內的 Diagnostics 換成 exit_on_red=False 版本，讓 red() 拋例外而非 sys.exit。"""
    monkeypatch.setattr(
        llm_client_module,
        "Diagnostics",
        lambda name: Diagnostics(name, exit_on_red=False),
    )


def _cli_result(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
    """組出一個 subprocess.CompletedProcess 形狀的 mock 結果。"""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


def _ok_json(text: str = "hello", input_tokens: int = 10, output_tokens: int = 20) -> str:
    """組出一段合法的 `claude -p --output-format json` 回應。"""
    return json.dumps(
        {
            "result": text,
            "is_error": False,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
        ensure_ascii=False,
    )


# 1. mock=True 建構子不需要呼叫 shutil.which，也不會呼叫 subprocess.run
def test_mock_init_skips_cli_lookup() -> None:
    with patch("shutil.which") as mock_which, patch("subprocess.run") as mock_run:
        client = LLMClient(mock=True)
        assert client.is_mock is True
        mock_which.assert_not_called()
        mock_run.assert_not_called()


# 2. mock=True 時 complete() 有 fixture 就讀 fixture 檔內容
def test_mock_complete_reads_fixture(tmp_path: Path) -> None:
    fixture = tmp_path / "reply.txt"
    fixture.write_text("固定回覆內容", encoding="utf-8")
    client = LLMClient(mock=True)
    assert client.complete("system", "user", fixture=fixture) == "固定回覆內容"


# 3. mock=True 時 complete() 沒 fixture 回傳 "[MOCK] <system前40字>"
def test_mock_complete_without_fixture_returns_placeholder() -> None:
    client = LLMClient(mock=True)
    result = client.complete("這是系統提示詞開頭", "user")
    assert result == "[MOCK] 這是系統提示詞開頭"


# 4. mock=False 建構子在 shutil.which 回傳 None 時會走 Diagnostics.red
def test_live_init_raises_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_non_exiting_diagnostics(monkeypatch)
    with patch("shutil.which", return_value=None) as mock_which:
        with pytest.raises(RedAlert):
            LLMClient(mock=False)
    mock_which.assert_called_once_with(CLI_COMMAND)


# 5. mock=False 建構子在 shutil.which 找得到路徑時成功建立，不拋例外
def test_live_init_succeeds_when_cli_found() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD") as mock_which:
        client = LLMClient(mock=False)
        assert client.is_mock is False
    mock_which.assert_called_once_with(CLI_COMMAND)


# 6. live 模式成功呼叫一次：complete() 回傳值等於 "result" 欄位內容
def test_live_complete_returns_result_field() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch("subprocess.run", return_value=_cli_result(stdout=_ok_json("這是回覆"))):
        assert client.complete("system prompt", "user prompt") == "這是回覆"


# 7. 驗證呼叫 subprocess.run 時傳入的 argv 與 input= 參數正確
def test_live_complete_passes_expected_argv_and_input() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False, model="claude-sonnet-5")
    with patch("subprocess.run", return_value=_cli_result(stdout=_ok_json())) as mock_run:
        client.complete("system prompt", "user prompt")
    argv = mock_run.call_args.args[0]
    kwargs = mock_run.call_args.kwargs
    assert "-p" in argv
    assert "--safe-mode" in argv  # 沒這旗標會洩漏 cwd 的 CLAUDE.md/git status，見 code review MUST_FIX
    assert "--model" in argv
    assert "--system-prompt" in argv
    assert "--tools" in argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert kwargs["input"] == "user prompt"


# 8. --system-prompt 帶的值是 _compose_system(system) 的結果（含 CONTEXT_NOTE）
def test_live_complete_system_prompt_includes_context_note() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False, context_note="這是備註")
    with patch("subprocess.run", return_value=_cli_result(stdout=_ok_json())) as mock_run:
        client.complete("原始系統提示詞", "user prompt")
    argv = mock_run.call_args.args[0]
    system_prompt = argv[argv.index("--system-prompt") + 1]
    assert system_prompt == "原始系統提示詞\n\nCONTEXT_NOTE:\n這是備註"


# 9. live 模式成功後 total_tokens 屬性正確累加 usage.input_tokens / output_tokens
def test_live_complete_accumulates_total_tokens() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch(
        "subprocess.run",
        return_value=_cli_result(stdout=_ok_json(input_tokens=15, output_tokens=25)),
    ):
        client.complete("system", "user")
    assert client.total_tokens == {"input": 15, "output": 25}


# 10. 連續呼叫 complete() 兩次，total_tokens 是兩次的加總（不是覆蓋）
def test_live_complete_total_tokens_accumulate_across_calls() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch(
        "subprocess.run",
        return_value=_cli_result(stdout=_ok_json(input_tokens=10, output_tokens=20)),
    ):
        client.complete("system", "user")
        client.complete("system", "user")
    assert client.total_tokens == {"input": 20, "output": 40}


# 11. is_error=true（即使 returncode=0）要拋 LLMError
def test_live_complete_raises_on_is_error_true() -> None:
    body = json.dumps({"result": "", "is_error": True, "usage": {}}, ensure_ascii=False)
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch("subprocess.run", return_value=_cli_result(stdout=body, returncode=0)):
        with pytest.raises(LLMError):
            client.complete("system", "user")


# 12. returncode != 0 要拋 LLMError，訊息裡要看得到 stderr 內容的線索
def test_live_complete_raises_on_nonzero_returncode() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch(
        "subprocess.run",
        return_value=_cli_result(stdout="", stderr="認證已過期", returncode=1),
    ):
        with pytest.raises(LLMError, match="認證已過期"):
            client.complete("system", "user")


# 13. subprocess.TimeoutExpired 在 max_retries 次數內重試，且重試間有指數退避
def test_live_complete_retries_on_timeout_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False, max_retries=2)
    timeout_exc = subprocess.TimeoutExpired(cmd="claude", timeout=60)
    with patch(
        "subprocess.run",
        side_effect=[timeout_exc, timeout_exc, _cli_result(stdout=_ok_json())],
    ):
        client.complete("system", "user")
    assert sleep_calls == [1.0, 2.0]


# 14. TimeoutExpired 重試 max_retries 次全部失敗後，最終拋 LLMError（訊息含「重試」字樣）
def test_live_complete_raises_after_exhausting_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False, max_retries=2)
    timeout_exc = subprocess.TimeoutExpired(cmd="claude", timeout=60)
    with patch("subprocess.run", side_effect=timeout_exc):
        with pytest.raises(LLMError, match="重試"):
            client.complete("system", "user")


# 15. TimeoutExpired 重試第 2 次成功：subprocess.run 總共被呼叫 2 次，且拿到正確結果
def test_live_complete_succeeds_on_second_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False, max_retries=3)
    timeout_exc = subprocess.TimeoutExpired(cmd="claude", timeout=60)
    with patch(
        "subprocess.run",
        side_effect=[timeout_exc, _cli_result(stdout=_ok_json("重試後成功"))],
    ) as mock_run:
        result = client.complete("system", "user")
    assert result == "重試後成功"
    assert mock_run.call_count == 2


# 16. stdout 不是合法 JSON（空字串或亂碼）-> 拋 LLMError，訊息裡看得到部分原始內容
def test_live_complete_raises_on_invalid_json() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch("subprocess.run", return_value=_cli_result(stdout="不是JSON的亂碼내용")):
        with pytest.raises(LLMError, match="不是JSON的亂碼"):
            client.complete("system", "user")


# 17. JSON 合法但缺 "result" 欄位 -> 拋 LLMError
def test_live_complete_raises_on_missing_result_field() -> None:
    body = json.dumps({"is_error": False, "usage": {}}, ensure_ascii=False)
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch("subprocess.run", return_value=_cli_result(stdout=body)):
        with pytest.raises(LLMError, match="result"):
            client.complete("system", "user")


# 18. max_retries=0 時只嘗試一次（TimeoutExpired 立刻拋錯，不重試）
def test_live_complete_max_retries_zero_tries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep_calls: list[float] = []
    monkeypatch.setattr("time.sleep", lambda seconds: sleep_calls.append(seconds))
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False, max_retries=0)
    timeout_exc = subprocess.TimeoutExpired(cmd="claude", timeout=60)
    with patch("subprocess.run", side_effect=timeout_exc) as mock_run:
        with pytest.raises(LLMError):
            client.complete("system", "user")
    assert mock_run.call_count == 1
    assert sleep_calls == []


# 19. max_tokens<=0 在 live 模式下依然要在 complete() 一開始就拋 LLMError
def test_live_complete_rejects_non_positive_max_tokens() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch("subprocess.run") as mock_run:
        with pytest.raises(LLMError):
            client.complete("system", "user", max_tokens=0)
    mock_run.assert_not_called()


# 20. system 或 user 不是字串時，live 模式一樣要在最前面拋 LLMError
def test_live_complete_rejects_non_string_inputs() -> None:
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch("subprocess.run") as mock_run:
        with pytest.raises(LLMError):
            client.complete(system=123, user="user")  # type: ignore[arg-type]
    mock_run.assert_not_called()


# 21. live 模式成功時 .usage.jsonl 有被追加一行、mode 欄位是 "live"
def test_live_complete_appends_usage_log_with_live_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log_path = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENCLAW_USAGE_LOG", str(log_path))
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False)
    with patch("subprocess.run", return_value=_cli_result(stdout=_ok_json())):
        client.complete("system", "user")
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["mode"] == "live"


# 22. model 參數確實原封不動傳進 --model 的值
def test_live_complete_passes_custom_model_to_argv() -> None:
    custom_model = "claude-haiku-4-5-20251001"
    with patch("shutil.which", return_value="C:/fake/claude.CMD"):
        client = LLMClient(mock=False, model=custom_model)
    with patch("subprocess.run", return_value=_cli_result(stdout=_ok_json())) as mock_run:
        client.complete("system", "user")
    argv = mock_run.call_args.args[0]
    assert argv[argv.index("--model") + 1] == custom_model


# 23. 整合測試（預設 SKIPPED）：真的呼叫一次本機 claude CLI，驗證 --safe-mode
#     確實生效——回應不能混進 cwd 的 CLAUDE.md／git status／其他專案指示。
#     只有設定 OPENCLAW_RUN_LIVE_CLI_TESTS=1 才會真的執行，避免一般 `pytest` 跑測試
#     時意外燒到使用者真實的 Claude 用量、也避免拖慢一般測試套件。
@pytest.mark.skipif(
    os.environ.get("OPENCLAW_RUN_LIVE_CLI_TESTS") != "1",
    reason="只在 OPENCLAW_RUN_LIVE_CLI_TESTS=1 時真的呼叫本機 claude CLI，避免燒真實用量",
)
def test_live_cli_safe_mode_does_not_leak_project_context() -> None:
    client = LLMClient(mock=False, timeout=60)
    result = client.complete(
        system="You are a test assistant. Reply with exactly the word: OK",
        user="ping",
    )
    # 回應要短小精悍，不能夾帶 CLAUDE.md／git status／專案路徑等本機上下文內容
    assert len(result) < 200
    leaked_markers = ("git status", "CLAUDE.md", "session-end", str(Path.cwd()))
    lowered = result.lower()
    for marker in leaked_markers:
        assert marker.lower() not in lowered, f"--safe-mode 疑似失效，回應洩漏了：{marker!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
