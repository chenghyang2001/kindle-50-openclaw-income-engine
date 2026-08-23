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
        白名單比對規則：完全相同（不分大小寫），或 recipient 以白名單項目結尾（domain 比對）
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
        mock=True   -> 不呼叫 API，complete() 回傳 fixture 內容（零成本）
        mock=False  -> 讀 os.environ["ANTHROPIC_API_KEY"]，缺少則透過 Diagnostics.red 退出
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
        mock=False 時：呼叫 Anthropic Messages API（用 urllib.request，不用 requests）
                      逾時或 5xx 走指數退避重試 max_retries 次
        用量一律追加一行 JSON 到 <cwd>/.usage.jsonl
        """

    @property
    def total_tokens(self) -> dict[str, int]:
        """{"input": N, "output": M}"""
```

**API 細節（`mock=False` 時）**

- Endpoint: `https://api.anthropic.com/v1/messages`
- Headers: `x-api-key`, `anthropic-version: 2023-06-01`, `content-type: application/json`
- Body: `{"model":..., "max_tokens":..., "system":..., "messages":[{"role":"user","content":...}]}`
- 回傳取 `resp["content"][0]["text"]`

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
