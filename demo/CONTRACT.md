# `_shared/` API 契約（Contract-First）

> **這份契約是凍結的。** `_shared/` 的實作者與 10 個 demo 的實作者都必須嚴格遵守，不得自行更動簽名。
> 若你認為契約有缺陷，**照契約寫完**，然後在 Manifest 的 NOTES 提出，由主 Claude 裁決。

---

## 0. 專案規範（所有檔案適用）

| 規則 | 要求 |
| --- | --- |
| 編碼 | 一律 UTF-8。檔案讀寫必須明確 `encoding="utf-8"` |
| 註解 / docstring | **繁體中文** |
| 路徑 | 禁止硬編碼 `C:\Users\...`。用 `pathlib.Path(__file__).parent` 或 `Path.home()` |
| 金鑰 | 禁止字串常數。一律 `os.environ.get(...)` 並檢查缺失 |
| 縮排 | 4 空格 |
| 命名 | `snake_case`；布林用 `is_` / `has_` / `can_` 開頭；常數全大寫 |
| 錯誤處理 | 禁止裸 `except:`。要具體例外類型 + 明確訊息 |
| 函式長度 | > 30 行就拆 |
| import 順序 | 標準庫 → 第三方 → 本地 |
| 型別註記 | 公開函式一律加 type hints |

**Python 版本**：3.10+（可用 `X | None` 語法）
**第三方依賴**：只允許 `PyYAML`、`pytest`。其餘一律用標準庫（`urllib.request` 而非 `requests`）。

---

## 1. `_shared/autonomy.py`

```python
from enum import Enum

class AutonomyLevel(Enum):
    """自主權階梯三段式（第 04 章核心安全設計）"""
    READ_ONLY = "read_only"            # 只分類與分析，絕不觸碰來源、絕不外送
    DRAFT = "draft"                    # 建立草稿，必須人工審查後送出（預設）
    SUPERVISED_AUTO = "supervised_auto"  # 僅自動送給白名單，其餘降級為 DRAFT


class AutonomyError(ValueError):
    """自主權設定違規"""


class AutonomyGate:
    def __init__(
        self,
        level: AutonomyLevel = AutonomyLevel.DRAFT,
        approved_senders: list[str] | None = None,
        days_in_draft: int = 0,
    ) -> None:
        """
        level:            要求的自主權層級，預設 DRAFT
        approved_senders: SUPERVISED_AUTO 的白名單（email 或 domain，如 "@acme.com"）
        days_in_draft:    已在草稿模式運行的天數

        規則（必須實作）:
        1. level=SUPERVISED_AUTO 但 approved_senders 為空 -> 拋 AutonomyError
        2. level=SUPERVISED_AUTO 但 days_in_draft < 14 -> 不拋錯，但 self.warnings
           追加一則警告字串（書中鐵律：兩週 + 客戶簽核前不得全自動）
        """

    @property
    def warnings(self) -> list[str]:
        """累積的警告訊息（供 diagnostics AMBER 使用）"""

    def effective_level(self, recipient: str) -> AutonomyLevel:
        """
        回傳針對此收件人的實際層級。
        SUPERVISED_AUTO 且 recipient 命中白名單 -> SUPERVISED_AUTO
        SUPERVISED_AUTO 但未命中           -> DRAFT（降級）
        其餘                                -> 原 level
        白名單比對規則（2026-08-24 修訂，安全性強化）：
        1. 白名單項目以 "@" 開頭（例如 "@acme.com"）-> 網域比對，
           recipient 必須以該字串結尾才命中
        2. 白名單項目不以 "@" 開頭 -> 一律視為完整 email，只做精確相等比對
        3. 兩者皆不分大小寫

        為何不再允許裸網域結尾比對：
        白名單若寫 "acme.com"，"boss@evil-acme.com" 會誤判命中。
        攻擊者只要註冊含目標網域的網域名即可讓系統自動回信。
        故裸字串一律降為精確比對，網域比對必須明寫 "@"。
        """

    def can_send(self, recipient: str) -> bool:
        """effective_level(recipient) is SUPERVISED_AUTO 時才回 True"""
```

---

## 2. `_shared/diagnostics.py`

```python
from enum import Enum

class Severity(Enum):
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class RedAlert(RuntimeError):
    """紅色警報：系統停擺，需立即介入"""


class Diagnostics:
    def __init__(self, module_name: str, exit_on_red: bool = True) -> None: ...

    def red(self, symptom: str, cause: str, fix: str) -> None:
        """
        印出紅色警報（symptom / cause / fix 三欄）。
        exit_on_red=True  -> sys.exit(1)
        exit_on_red=False -> 拋 RedAlert（供測試使用）
        """

    def amber(self, symptom: str, fix: str) -> None:
        """印出琥珀色警示到 stderr，流程繼續。"""

    def green(self, message: str) -> None:
        """印出正常訊息。"""

    @property
    def amber_count(self) -> int: ...
```

**書中已知症狀對照（實作時內建為常數 dict `KNOWN_SYMPTOMS`）：**

| Key | Severity | Cause | Fix |
| --- | --- | --- | --- |
| `api_key_invalid` | RED | Key 過期或未存入設定檔 | 重新輸入並驗證 |
| `no_whatsapp_msg` | RED | Twilio 憑證錯誤 | 檢查沙盒驗證清單 |
| `oauth_error` | RED | Gmail token 7 天限制過期 | 設定永久 Refresh token |
| `no_transcripts` | RED | Webhook URL 無法公開存取 | 確認網路狀態與權限 |
| `briefing_too_long` | AMBER | 輸出超過 400 字 | 提示詞強制「最高 320 字，無情刪減」 |
| `spam_misclassification` | AMBER | VIP 網域未設定 | 更新 VIP_SENDERS.domains 並重掃過去 7 天 |
| `tone_mismatch` | AMBER | 缺語氣樣本 | TONE_EXAMPLES 加入 3-5 封真實信件 |
| `delayed_briefing` | AMBER | API 限流 | Cron 提早 20 分 + retry_on_timeout: true |

---

## 3. `_shared/llm_client.py`

```python
class LLMError(RuntimeError): ...


class LLMClient:
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
        mock=False  -> 用 shutil.which("claude") 解析本機已登入的 Claude Code CLI，
                       缺少則透過 Diagnostics.red 退出（2026-08-24 修訂：不再讀
                       ANTHROPIC_API_KEY，改走 claude -p 呼叫使用者的 Max 訂閱，
                       不消耗 API Credits）
        context_note-> 附加到 system prompt 尾端的 CONTEXT_NOTE 段落
                       （第 04 章：可減少 40% 不相關輸出）
        """

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 2000,
        fixture: str | Path | None = None,
    ) -> str:
        """
        mock=True 時：讀 fixture 檔案內容回傳；fixture 為 None 則回傳固定佔位字串
                     "[MOCK] <system 前 40 字>"
        mock=False 時：以子行程呼叫本機 claude CLI 的 headless 模式（claude -p），
                      逾時走指數退避重試 max_retries 次；非逾時的失敗
                      （非 0 exit code／回應 JSON 的 is_error=true／JSON 格式錯誤／
                      缺 result 欄位）一律不重試，直接拋 LLMError
                      （2026-08-24 修訂：max_tokens 保留在簽名裡供型別驗證，
                      但不會傳給 CLI——headless 模式沒有對應的 token 上限旗標）
        用量一律追加一行 JSON 到用量記錄檔（2026-08-24 修訂）：
                      路徑優先序 = 環境變數 OPENCLAW_USAGE_LOG > demo/ 目錄下的 .usage.jsonl
                      （以 config_loader.project_root() 推算，**不可用 cwd**）
                      理由：cwd 相對會在使用者從其他目錄執行時，把記錄檔亂丟進不相干的 repo
                      注意：live 模式的 input_tokens/output_tokens 現在讀自 claude -p 回應
                      JSON 的 usage 欄位，語意已不同於舊版 Anthropic Messages API 的計費
                      token（不含 prompt cache 的 cache_creation/cache_read token 數，
                      output_tokens 則含 thinking token），不可再拿來對照舊版的用量報表。
        """

    @property
    def total_tokens(self) -> dict[str, int]:
        """{"input": N, "output": M}"""
```

**CLI 呼叫細節（`mock=False` 時，2026-08-24 修訂，取代舊版直連 Anthropic API）**

- 執行檔：`shutil.which("claude")` 解析（Windows 上會解析到 `claude.CMD`），透過
  `subprocess.run([...], input=user, capture_output=True, text=True, encoding="utf-8",
  timeout=self.timeout)` 呼叫，**不用 `shell=True`**
- Argv：`[cli_path, "-p", "--safe-mode", "--model", <model>, "--system-prompt", <組合後
  system>, "--tools", "", "--output-format", "json"]`
  - `--safe-mode`：**必要旗標，不可省略**。停用 CLAUDE.md／skills／plugins／hooks／MCP
    servers 等本機客製化；沒有這個旗標，即使 `--tools ""` 停用了內建工具，headless 呼叫
    仍會自動掛載呼叫當下 cwd 的 CLAUDE.md／git status／專案指示，並把內容洩漏進回應文字
    （code review 實測發現此問題，見 `demo/_shared/test_llm_client.py` 第 23 條回歸測試）
  - `--tools ""`：停用所有內建工具執行，維持純文字補全語意
  - `user` 透過 `input=` 從 stdin 餵入，**不進 argv**
- 回應：`result.stdout` 是 JSON，取 `payload["result"]` 當文字內容；`payload["is_error"]`
  為 `true` 或缺 `result` 欄位都視為失敗（`LLMError`，不重試）；`payload["usage"]["input_tokens"]` /
  `["output_tokens"]` 供用量記錄
- 已知取捨：每次呼叫起一個新的 CLI 子行程，單次呼叫耗時（`--safe-mode` 加上後約 1~2 秒
  API 時間，實際 wall time 視環境約數秒）明顯高於舊版直接 HTTP 呼叫；對逐筆呼叫
  `complete()` 的 demo（如批次評分、批次草稿）在大量筆數時會放大總耗時，尚未做平行化

---

## 4. `_shared/notifier.py`

```python
class NotifierError(RuntimeError): ...


class Notifier:
    SUPPORTED = ("console", "telegram", "gmail", "line", "whatsapp")

    def __init__(self, channel: str = "console", config: dict | None = None) -> None:
        """channel 不在 SUPPORTED -> 拋 NotifierError"""

    def send(self, text: str, subject: str | None = None) -> bool:
        """
        回傳是否成功。失敗印出明確錯誤，不可靜默吞掉。

        console  : print 到 stdout（永遠成功）
        telegram : Bot API sendMessage，parse_mode=HTML，
                   disable_web_page_preview=True，
                   **超過 4000 字元自動分段**，段間 sleep(1)
                   憑證：TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID_CHENGHYANG2001BOT
        gmail    : subprocess 呼叫 `gws gmail users messages send`
                   （用 email.mime.text 組 MIME + base64url，參考 tool-commands 規範）
        line     : LINE Messaging API push（LINE_CHANNEL_TOKEN）
        whatsapp : Twilio Messages API（TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM）
        """

    @staticmethod
    def split_message(text: str, limit: int = 4000) -> list[str]:
        """依換行邊界切段，不切斷 HTML 標籤。"""
```

---

## 5. `_shared/config_loader.py`

```python
def load_config(
    path: str | Path,
    required_env: list[str] | None = None,
) -> dict:
    """
    1. 讀 YAML（encoding="utf-8"）
    2. 遞迴展開字串中的 ${ENV_VAR}；環境變數不存在則保留原樣並記錄
    3. required_env 有缺 -> print 缺少清單到 stderr 後 sys.exit(1)
       （不可用預設值靜默掩蓋）
    4. 檔案不存在 -> 拋 FileNotFoundError，訊息含絕對路徑
    """


def project_root() -> Path:
    """回傳 demo/ 的絕對路徑（以本檔位置推算，禁止硬編碼）"""
```

---

## 6. 每個 demo 的 `main.py` 統一介面

```python
def build_parser() -> argparse.ArgumentParser:
    """
    必備旗標：
      --mock       離線模式（預設 True）
      --live       串真實 API（與 --mock 互斥）
      --dry-run    跑完流程但不實際發送
      --notify     {console,telegram,gmail,line,whatsapp}，預設 console
      --config     設定檔路徑，預設同目錄 config.yaml
    """


def run(args: argparse.Namespace) -> dict:
    """執行主流程，回傳結果 dict（供測試斷言）。不做 sys.exit。"""


def main() -> int:
    """解析參數 -> run() -> 印出/發送結果 -> 回傳 exit code"""


if __name__ == "__main__":
    sys.exit(main())
```

### ⚠️ 已知技術債：`run()` 回傳鍵名未標準化（2026-08-24 記錄）

本契約的 §6 只規定 `run(args) -> dict`，**沒有規定 dict 裡要有哪些鍵**。
結果：4 個獨立 writer 對同一份契約寫程式，鍵名幾乎全部發散。

| 語意 | demo01 | demo02 | demo05 | demo09 |
| --- | --- | --- | --- | --- |
| 模組編號 | `module_id` | `module` | `module_id` | `module` |
| 模組名稱 | `module_name` | 無 | `module_name` | 無 |
| 執行模式 | `mode` | `mode` | `mode` | `is_mock`（bool） |
| 空跑旗標 | `is_dry_run` | `dry_run` | `dry_run` | `dry_run` |
| 自主權警告 | `autonomy_warnings` | `autonomy.warnings`（巢狀） | `warnings` | 無（只有 `failed_sources`） |
| 琥珀計數 | `amber_count` | `amber_count` | `amber_count` | `amber_count` |

**`amber_count` 是唯一四家自發一致的鍵**——因為它是唯一在契約中被明確命名的。
這是「規格留白處必然發散」的實證：不是誰偷懶，是每個人都會用自己覺得合理的方式填空。

**現況處理**：`bundle-quickstart/run_all.py` 的 `normalize_result()` 與 `extract_warnings()`
以防禦性 `.get()` 事後救回，已有測試覆蓋，功能正確。

**為何不回頭改 10 個 demo**：純命名重構，收益（美觀、少一層轉接）不抵風險
（10 個模組 × 重寫 + 重跑 QA，且每次改動都可能引入新缺陷）。轉接層是有測試的，穩定。

**未來若要標準化**，建議把這 6 個鍵寫成 §6 的強制回傳欄位，並在同一次改動中
一起改完 10 個 demo 與 `run_all.py`，不要分批：
`module_id` / `module_name` / `mode` / `dry_run` / `warnings` / `amber_count`

---

**匯入 `_shared` 的方式**（所有 demo 統一）：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _shared.llm_client import LLMClient          # noqa: E402
from _shared.notifier import Notifier             # noqa: E402
from _shared.autonomy import AutonomyGate, AutonomyLevel  # noqa: E402
from _shared.diagnostics import Diagnostics       # noqa: E402
from _shared.config_loader import load_config     # noqa: E402
```

---

## 7. 每個 demo 的 `config.yaml` 最低欄位

```yaml
module:
  id: "01"                     # 模組編號
  name: "晨間情報簡報"
  deploy_minutes: 60           # 書中部署時間
  recovered_hours_per_month: 35
  client_setup_price: 300
  client_monthly_price: 75

runtime:
  autonomy: draft              # read_only | draft | supervised_auto
  approved_senders: []
  days_in_draft: 0
  notify_channel: console

# ...以下為各模組專屬設定
```

---

## 8. 測試要求（`test_main.py`）

每個 demo **3 個測試**（中等複雜度標準）：

```python
def test_happy_path():
    """標準 mock 輸入 -> run() 回傳預期結構"""

def test_edge_case():
    """空輸入 / 極值 / 特殊字元（依模組性質擇一最相關的）"""

def test_integration():
    """與 _shared 的互動：autonomy 降級、diagnostics amber、notifier console"""
```

- 一律 `pytest` 風格，不用 unittest class
- 不得呼叫真實外部 API（測試必須離線可跑）
- 測試檔開頭同樣要 `sys.path.insert` 才能匯入 `_shared`

---

## 9. 檔案清單交付要求

每個 demo 資料夾必須產出：

```
demoNN-xxx/
├── README.md          # Before/After 表 + Financial Model + 客戶見證 + Client Pitch 話術
├── config.yaml        # 依 §7
├── main.py            # 依 §6
├── prompts/*.md       # 系統提示詞（獨立成檔，不內嵌 .py）
├── mock/*.json        # 離線測試資料
└── test_main.py       # 依 §8
```

**驗收**：`python main.py --mock` 必須零憑證、零網路跑完並印出結果。
