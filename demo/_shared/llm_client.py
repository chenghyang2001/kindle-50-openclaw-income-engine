"""Claude Messages API 封裝 — 預設 mock 模式，零成本開發。

設計取捨：
1. **預設 `mock=True`**。10 個 demo 開發期間跑上百次，若預設打真實 API 會燒掉整包預算。
2. **只用標準庫 `urllib.request`**。這批交付包要能丟到客戶機器上直接跑，
   少一個 `pip install requests` 就少一個安裝失敗的支援電話。
3. **用量一律落地 `.usage.jsonl`**。沒有帳單資料就沒辦法對客戶報價，
   因此 mock 與 live 都寫入（mock 記字元數、tokens 為 0，不捏造數字）。
   路徑固定在 `demo/`（或 `OPENCLAW_USAGE_LOG` 指定處），不跟著 cwd 跑——
   否則從別的目錄執行就會把記錄檔灑進不相干的專案，且各自獨立無法彙整。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # 一般情境：以 `_shared` 套件被 demo 匯入
    from .diagnostics import Diagnostics, KNOWN_SYMPTOMS
except ImportError:  # 直接以腳本執行本檔時沒有套件脈絡
    from diagnostics import Diagnostics, KNOWN_SYMPTOMS  # type: ignore[no-redef]

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
API_KEY_ENV = "ANTHROPIC_API_KEY"
USAGE_LOG_ENV = "OPENCLAW_USAGE_LOG"
USAGE_LOG_NAME = ".usage.jsonl"

# 指數退避的基準秒數：1s -> 2s -> 4s。Anthropic 建議的最小重試間隔即為 1 秒。
BACKOFF_BASE_SECONDS = 1.0
# 這些狀態碼代表「等一下再試會好」，其餘 4xx 是請求本身有問題，重試只是浪費時間與配額。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})


class LLMError(RuntimeError):
    """呼叫 Claude API 失敗（含重試耗盡、回應格式異常、fixture 缺檔）。"""


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
    """Claude Messages API 的極簡封裝。"""

    def __init__(
        self,
        mock: bool = True,
        model: str = "claude-sonnet-5",
        context_note: str | None = None,
        max_retries: int = 3,
        timeout: int = 60,
    ) -> None:
        """
        mock=True   -> 不呼叫 API，complete() 回傳 fixture 內容（零成本）
        mock=False  -> 讀 os.environ["ANTHROPIC_API_KEY"]，缺少則透過 Diagnostics.red 退出
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
        self._api_key = "" if self._mock else self._require_api_key()

    def _require_api_key(self) -> str:
        """live 模式缺金鑰就走紅色警報退出，絕不靜默降級回 mock。"""
        api_key = os.environ.get(API_KEY_ENV, "").strip()
        if not api_key:
            entry = KNOWN_SYMPTOMS["api_key_invalid"]
            self._diagnostics.red(
                f"{entry['symptom']}：環境變數 {API_KEY_ENV} 未設定或為空",
                entry["cause"],
                f"{entry['fix']}（PowerShell: setx {API_KEY_ENV} \"sk-ant-...\"）",
            )
        return api_key

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
        mock=False 時呼叫 Anthropic Messages API，逾時或 5xx 走指數退避重試。
        """
        if not isinstance(system, str) or not isinstance(user, str):
            raise LLMError("system 與 user 都必須是字串")
        if max_tokens <= 0:
            raise LLMError(f"max_tokens 必須大於 0，收到 {max_tokens}")

        if self._mock:
            text = self._complete_mock(system, fixture)
            self._record_usage("mock", system, user, text, 0, 0)
            return text

        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": self._compose_system(system),
            "messages": [{"role": "user", "content": user}],
        }
        response = self._post_with_retry(payload)
        text = self._extract_text(response)
        usage = response.get("usage") or {}
        input_tokens = int(usage.get("input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
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

    def _post_with_retry(self, payload: dict) -> dict:
        """指數退避重試：總嘗試次數 = max_retries + 1（max_retries=0 代表只試一次）。"""
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._post_once(payload)
            except _RetryableError as exc:
                last_error = exc
                if attempt < self._max_retries:
                    time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
        raise LLMError(f"Anthropic API 重試 {self._max_retries} 次後仍失敗：{last_error}")

    def _post_once(self, payload: dict) -> dict:
        """送出單次請求。可重試的失敗拋 _RetryableError，其餘拋 LLMError。"""
        request = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _RetryableError(f"連線失敗或逾時：{exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Anthropic API 回應不是合法 JSON：{body[:200]}") from exc

    @staticmethod
    def _http_error(exc: urllib.error.HTTPError) -> LLMError:
        """把 HTTP 錯誤分類成「值得重試」與「重試也沒用」。"""
        detail = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else ""
        message = f"Anthropic API 回傳 HTTP {exc.code}：{detail or exc.reason}"
        if exc.code in RETRYABLE_STATUS:
            return _RetryableError(message)
        return LLMError(message)

    @staticmethod
    def _extract_text(response: dict) -> str:
        """回傳取 resp["content"][0]["text"]，格式不符時明確報錯而非回空字串。"""
        try:
            return response["content"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            preview = json.dumps(response, ensure_ascii=False)[:200]
            raise LLMError(f"Anthropic API 回應缺少 content[0].text：{preview}") from exc

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
