"""呼叫本機已登入的 `claude` CLI headless 模式（`claude -p`）— 預設 mock 模式，零成本開發。

設計取捨：
1. **預設 `mock=True`**。mock 預設不是為了省 Max 訂閱額度（訂閱制本身是固定 $0/次），
   而是讓開發機、CI 環境不需要一個已經互動登入過的 `claude` CLI 也能離線、可重現地測試——
   10 個 demo 開發期間要跑上百次，逼每個環境都先登入一次 CLI 才能測試會拖慢所有人。
2. **只用標準庫**。這批交付包要能丟到客戶機器上直接跑，
   少一個 `pip install requests` 就少一個安裝失敗的支援電話；
   `subprocess` 一樣是標準庫，live 模式現在連 HTTP 都不用打，改成呼叫本機 CLI 子行程，
   走使用者已登入的 Claude Max 訂閱額度，不需要 `ANTHROPIC_API_KEY`。
3. **用量一律落地 `.usage.jsonl`**。沒有帳單資料就沒辦法對客戶報價，
   因此 mock 與 live 都寫入（mock 記字元數、tokens 為 0，不捏造數字）。
   路徑固定在 `demo/`（或 `OPENCLAW_USAGE_LOG` 指定處），不跟著 cwd 跑——
   否則從別的目錄執行就會把記錄檔灑進不相干的專案，且各自獨立無法彙整。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

try:  # 一般情境：以 `_shared` 套件被 demo 匯入
    from .diagnostics import Diagnostics
except ImportError:  # 直接以腳本執行本檔時沒有套件脈絡
    from diagnostics import Diagnostics  # type: ignore[no-redef]

CLI_COMMAND = "claude"
USAGE_LOG_ENV = "OPENCLAW_USAGE_LOG"
USAGE_LOG_NAME = ".usage.jsonl"

# 指數退避的基準秒數：1s -> 2s -> 4s。與舊版 HTTP 重試沿用同一個退避節奏。
BACKOFF_BASE_SECONDS = 1.0


class LLMError(RuntimeError):
    """呼叫 claude CLI 失敗（含重試耗盡、回應格式異常、fixture 缺檔）。"""


class _RetryableError(LLMError):
    """內部用：代表這次失敗值得退避後重試。"""


def project_root() -> Path:
    """demo/ 的絕對路徑（以本檔位置推算，禁止硬編碼）。

    刻意自行推算而不 import `config_loader.project_root()`：那個模組依賴 PyYAML，
    只用 autonomy / diagnostics / llm_client 的情境不該被迫安裝它
    （分層設計，見 `_shared/__init__.py`）。
    """
    return Path(__file__).resolve().parent.parent


def usage_log_path() -> Path:
    """用量記錄檔位置。

    1. 環境變數 OPENCLAW_USAGE_LOG（支援絕對路徑，可把多台機器的記錄集中一處）
    2. 否則 -> demo/.usage.jsonl

    不用 `Path.cwd()`：使用者只要不是剛好站在 demo 目錄下執行，記錄檔就會落在當下的
    任意目錄（實測曾污染不相干的 repo、被 git 列為未追蹤檔），而且各自獨立無法彙整。
    """
    override = os.environ.get(USAGE_LOG_ENV, "").strip()
    if override:
        # 同時支援 ~ 與 %USERPROFILE% / $HOME 兩種寫法，使用者才不必硬編碼絕對路徑
        return Path(os.path.expandvars(override)).expanduser()
    return project_root() / USAGE_LOG_NAME


class LLMClient:
    """呼叫本機 `claude` CLI headless 模式的極簡封裝。"""

    def __init__(
        self,
        mock: bool = True,
        model: str = "claude-sonnet-5",
        context_note: str | None = None,
        max_retries: int = 3,
        timeout: int = 60,
    ) -> None:
        """
        mock=True   -> 不呼叫 CLI，complete() 回傳 fixture 內容（零成本）
        mock=False  -> 解析 PATH 上的 `claude` 指令，缺少則透過 Diagnostics.red 退出
        context_note-> 附加到 system prompt 尾端的 CONTEXT_NOTE 段落
                       （第 04 章：可減少 40% 不相關輸出）
        """
        if max_retries < 0:
            raise LLMError(f"max_retries 不可為負數，收到 {max_retries}")
        if timeout <= 0:
            raise LLMError(f"timeout 必須大於 0 秒，收到 {timeout}")

        self._mock = bool(mock)
        self._model = model
        self._context_note = context_note
        self._max_retries = max_retries
        self._timeout = timeout
        self._diagnostics = Diagnostics("llm_client")
        self._total_tokens: dict[str, int] = {"input": 0, "output": 0}
        self._cli_path = "" if self._mock else self._require_cli()

    def _require_cli(self) -> str:
        """live 模式缺 claude CLI 就走紅色警報退出，絕不靜默降級回 mock。"""
        cli_path = shutil.which(CLI_COMMAND)
        if not cli_path:
            self._diagnostics.red(
                "Claude CLI 未安裝或不在 PATH",
                f"live 模式改用 {CLI_COMMAND} -p 呼叫本機 Max 訂閱，需要能執行到 {CLI_COMMAND} 指令",
                "安裝 Claude Code CLI 並完成一次互動登入（npm install -g @anthropic-ai/claude-code，"
                "再執行一次 claude 完成登入），或改用 --mock 離線模式",
            )
        return cli_path

    @property
    def is_mock(self) -> bool:
        """是否為離線模式。"""
        return self._mock

    @property
    def model(self) -> str:
        """使用的模型代號。"""
        return self._model

    @property
    def total_tokens(self) -> dict[str, int]:
        """{"input": N, "output": M}"""
        return dict(self._total_tokens)

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
        fixture: str | Path | None = None,
    ) -> str:
        """送出一次補全請求並回傳純文字結果。

        mock=True 時讀 fixture 檔案內容；fixture 為 None 則回傳 "[MOCK] <system 前 40 字>"。
        mock=False 時呼叫本機 `claude -p`，逾時走指數退避重試，其餘失敗直接報錯。

        max_tokens 保留在簽名裡是為了維持公開契約（見 CONTRACT.md），但不會傳給 CLI——
        `claude -p` 沒有對應的 token 上限旗標，也沒有其他旗標能湊出等效行為，
        寧可誠實地不做任何事，也不要假裝有限制卻其實沒生效。
        """
        if not isinstance(system, str) or not isinstance(user, str):
            raise LLMError("system 與 user 都必須是字串")
        if max_tokens <= 0:
            raise LLMError(f"max_tokens 必須大於 0，收到 {max_tokens}")

        if self._mock:
            text = self._complete_mock(system, fixture)
            self._record_usage("mock", system, user, text, 0, 0)
            return text

        text, input_tokens, output_tokens = self._run_cli_with_retry(system, user)
        self._total_tokens["input"] += input_tokens
        self._total_tokens["output"] += output_tokens
        self._record_usage("live", system, user, text, input_tokens, output_tokens)
        return text

    def _compose_system(self, system: str) -> str:
        """把 CONTEXT_NOTE 接到 system prompt 尾端（第 04 章：減少 40% 不相關輸出）。"""
        if not self._context_note:
            return system
        return f"{system}\n\nCONTEXT_NOTE:\n{self._context_note}"

    @staticmethod
    def _complete_mock(system: str, fixture: str | Path | None) -> str:
        """離線回應：有 fixture 就讀檔，沒有就回固定佔位字串。"""
        if fixture is None:
            return f"[MOCK] {system[:40]}"
        path = Path(fixture).expanduser()
        if not path.is_file():
            raise LLMError(f"找不到 mock fixture：{path.resolve()}")
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LLMError(f"讀取 mock fixture 失敗：{path.resolve()}｜{exc}") from exc

    def _run_cli_with_retry(self, system: str, user: str) -> tuple[str, int, int]:
        """指數退避重試：總嘗試次數 = max_retries + 1（max_retries=0 代表只試一次）。

        只有「等一下可能會好」的狀況（CLI 逾時）才重試；JSON 解析失敗、
        is_error、非 0 exit code 都是重試也沒用的錯誤，直接 LLMError。
        """
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._run_cli_once(system, user)
            except _RetryableError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
        raise LLMError(f"claude CLI 重試 {self._max_retries} 次後仍失敗：{last_error}")

    def _run_cli_once(self, system: str, user: str) -> tuple[str, int, int]:
        """送出單次 CLI 呼叫。可重試的失敗拋 _RetryableError，其餘拋 LLMError。"""
        composed_system = self._compose_system(system)
        argv = [
            self._cli_path,
            "-p",
            "--safe-mode",  # 停用 CLAUDE.md/skills/plugins/hooks/MCP 等客製化，
            # 否則 --tools "" 只擋得住內建工具，擋不住 cwd 的 CLAUDE.md／專案上下文
            # 被自動掛載進回應（code-reviewer 實測會把 git status、其他專案的
            # 全域指示混進 result），對法遵/財務類 demo 是資料外洩風險；
            # 副作用是把單次呼叫耗時從 9~22 秒降到約 1.4 秒，一併緩解效能疑慮。
            "--model",
            self._model,
            "--system-prompt",
            composed_system,
            "--tools",
            "",  # 停用所有工具，維持純文字補全語意（不讓它意外跑 Bash/Edit）
            "--output-format",
            "json",
        ]
        try:
            result = subprocess.run(
                argv,
                input=user,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise _RetryableError(f"claude CLI 逾時（{self._timeout}s）：{exc}") from exc
        except OSError as exc:
            raise LLMError(f"claude CLI 執行失敗：{exc}") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise LLMError(f"claude CLI 回傳非 0 exit code（{result.returncode}）：{detail}")

        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            preview = (result.stdout or "")[:300]
            raise LLMError(f"claude CLI 輸出不是合法 JSON：{preview}") from exc

        if response.get("is_error"):
            preview = json.dumps(response, ensure_ascii=False)[:300]
            raise LLMError(f"claude CLI 回報執行失敗（is_error=true）：{preview}")

        try:
            text = response["result"]
        except KeyError as exc:
            preview = json.dumps(response, ensure_ascii=False)[:300]
            raise LLMError(f"claude CLI 回應缺少 result 欄位：{preview}") from exc

        usage = response.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        return text, input_tokens, output_tokens

    def _record_usage(
        self,
        mode: str,
        system: str,
        user: str,
        output: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """追加一行 JSON 到 `usage_log_path()`（預設 demo/.usage.jsonl）。

        寫檔失敗只警告不中斷：用量記錄是稽核資料，不該讓它反過來弄掉主要產出。
        mock 模式的 token 一律記 0（不捏造估算值），改用字元數提供規模參考。
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode,
            "model": self._model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "chars_in": len(system) + len(user),
            "chars_out": len(output),
        }
        log_path = usage_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._diagnostics.amber(
                f"用量記錄寫入失敗（{log_path}）：{exc}",
                f"檢查該路徑的寫入權限，或用環境變數 {USAGE_LOG_ENV} 指定其他位置",
            )
