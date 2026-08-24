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

### `complete_json()`（2026-08-24 新增）

demo06 端對端驗證實測：`prompts/extract_invoice.md` 明講「不得包 Markdown 程式碼圍籬」，
但直接呼叫底層 `claude -p` CLI 6 次仍有**5 次**把回應包在 ` ```json ... ``` ` 圍籬裡。
任何要求 Claude 嚴格輸出 JSON 的呼叫端，若直接 `json.loads(complete(...))`，多數情況
會解析失敗（`Expecting value: line 1 column 1`）——這不是防禦假設情境，是防禦已經
實測會發生、且發生機率是多數的情境。

方法簽名：`complete_json(system, user, max_tokens=2000, fixture=None) -> dict`

行為：

1. 呼叫 `complete(system, user, max_tokens, fixture)` 取得原始文字
2. 依序做兩層清理：
   - 剝除 markdown 圍籬（開頭圍籬可能帶語言標籤如 ` ```json `，結尾純 ` ``` ` 行）
   - 保底：取第一個 `{` 到最後一個 `}` 之間的內容（純剝圍籬不夠——Claude 有時
     圍籬前後還會加一句人類語言說明，即使系統提示詞明講不要）
3. `json.loads(清理後文字)`：解析失敗 -> `LLMError`（訊息含清理前後各 150 字元片段）；
   解析成功但不是 dict -> `LLMError`（訊息含實際型別名稱）
4. 回傳解析好的 dict

**契約（呼叫端必讀）**：`complete_json()` 假設傳入內容已經確定要當 JSON 解析
（live 模式，或 mock 模式配合合法 JSON fixture）。mock 模式若沒給 `fixture`，
`complete()` 回傳的 `"[MOCK] ..."` 佔位字串**不會**被特殊處理，會自然落入
`LLMError`——呼叫端若要走「LLM 是可選加值、沒有就用確定性邏輯」的模式
（例如 demo03 的設計），必須自行先呼叫 `complete()` 判斷
`raw.startswith("[MOCK]")`，再決定要不要接著呼叫 `complete_json()`。

**目前接上的模組**：demo06（`extractor.py` 的 `parse_with_llm()`）。
demo03／demo18 仍是各自的 `_safe_json()` 本地實作（行為類似但錯誤處理契約不同：
失敗時回傳 `None` 而非拋例外），這輪不強制遷移，之後評估要不要統一。

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

> `_shared/` 還有第六個元件 `oauth.py`，因既有交叉引用不能順延編號，章節排在最後，見 **§10**。

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

---

## 10. `_shared/oauth.py`（2026-08-24 新增，2026-08-24 修訂：OAuthError 攜帶診斷資訊）

> **為什麼編號在這裡而不是接在 §5 之後**：`§6`（`main.py` 介面）／`§7`（`config.yaml`）／`§8`（測試要求）
> 已被 30 個 demo 的 `.py` 與 `README.md` 交叉引用十餘處。把新章節插進 §5 後會讓那些引用全數失效，
> 代價遠大於「`_shared/` 章節不連號」。新增章節一律往後接。

Google OAuth 的 `access_token` 一小時過期。共有 5 個模組（demo01/05/06/11/14）用同一套
`token_env` 模式讀 token，但目前只有 demo01 的憑證模型（`token_env` 指向一份 OAuth token
JSON 檔）真的接得上這個元件（見本節最後「適用範圍」）；refresh 邏輯收斂到 `_shared/`，
避免各自實作五份。

```python
class OAuthError(RuntimeError):
    """token 檔不可用或 refresh 失敗，攜帶 symptom / cause / fix 三段診斷資訊。

    本模組**不會自己呼叫** diagnostics.red()——因為 Diagnostics.red() 本身就是
    終結點（exit_on_red=True 時 sys.exit(1)；否則 raise RedAlert）。若 oauth.py
    內部先呼叫一次、呼叫端的 except OAuthError 又再處理一次，等同紅色警報喊兩次，
    或者讓呼叫端的 except 永遠抓不到東西（因為 RedAlert 已經先冒泡出去，
    OAuthError 根本輪不到被 raise）。呼叫端捕捉到本例外後，應自行取用
    .symptom / .cause / .fix 呼叫 diagnostics.red()，再視需要轉成自己的領域錯誤
    （例：demo01 轉 SourceError）。

    診斷訊息一律只提檔案路徑與欄位名稱，絕不帶入任何權杖或密鑰的值。
    """

    def __init__(self, message: str, *, symptom: str, cause: str, fix: str) -> None: ...


def load_access_token(
    token_path: Path,
    token_env: str,
    diagnostics: Any,
) -> str:
    """
    1. 讀 token JSON（encoding="utf-8"）；讀不到／非法 JSON -> raise OAuthError（帶 symptom/cause/fix）
       （JSON 頂層不是物件也歸這條，否則後續 .get() 會 AttributeError）
    2. 取 access_token（舊格式相容 token 欄位）；兩者皆無 -> raise OAuthError
    3. 過期判斷：obtained_at + expires_in - 60（SKEW，提前換避免邊界競態）<= now
       缺 obtained_at 或 expires_in -> 視為無法判斷，直接回傳現有 token（向後相容舊 token 檔）
    4. 未過期 -> 直接回傳，不發任何網路請求
    5. 已過期 -> 以 refresh_token / client_id / client_secret / token_uri POST 換新
       四個欄位任一缺少 -> raise OAuthError
       token_uri 不是以 "https://" 開頭 -> raise OAuthError（避免 client_secret 明文外洩）
       絕不可靜默回傳過期 token（違反 §0「--live 缺憑證必須明確報錯退出」）
    6. refresh 回應缺 access_token -> raise OAuthError
       （不擋的話合併時會保留舊的 access_token，等於靜默回傳過期權杖，違反第 5 條鐵律）
       即使 token 檔裡有可用的 refresh_token，若沒有 access_token 欄位仍然視為不可用
       而報錯——refresh_token 只是「能不能換」的憑證，不是「現在有沒有可用 token」的答案。
    7. refresh 成功 -> 新回應「合併」進原 token dict 後寫回原檔：
       - Google 的 refresh 回應通常不含 refresh_token，必須保留原本那顆
       - 更新 obtained_at 為當下 epoch 秒
       - 原子寫入（同目錄暫存檔 + os.replace，暫存檔檔名帶 pid 避免併發衝突，
         暫存檔權限明確設為 0o600），避免寫到一半當掉毀掉 refresh_token
       - 寫回失敗（OSError）分兩種情況：
         a. 這次回應帶有「與磁碟不同」的新 refresh_token（token 輪替）-> 磁碟上舊的
            那顆已被 Google 作廢，寫檔失敗等於永久弄丟唯一還有效的續期憑證 ->
            raise OAuthError（不可只發 AMBER）
         b. 沒有輪替（磁碟上原本那顆仍有效）-> diagnostics.amber 後仍回傳新 token
            （新 token 本身有效，只是這次沒存到，不該讓整條流程停擺）
    8. 回傳可用的 access_token
    """
```

**呼叫端契約**：`load_access_token()` 只負責湊出診斷資訊並 `raise OAuthError`——
**不會**自己呼叫 `diagnostics.red()`。呼叫端捕捉到 `OAuthError` 後**必須自己呼叫**
`diagnostics.red(symptom=exc.symptom, cause=exc.cause, fix=exc.fix)`（見 demo01 的
`sources/calendar_source.py` 的 `except OAuthError` 分支）。唯一的例外是「續期成功但
寫回檔案失敗、且 refresh_token 未輪替」這條路徑——那條路徑本來就該留在 `oauth.py`
內部處理成 AMBER，不需要呼叫端接手。

> **陷阱（2026-08-24 code review 抓到）**：`diagnostics.red()` 本身即終結流程——
> `exit_on_red=True` 時 `sys.exit`、`False` 時 `raise RedAlert`，回傳型別標註為
> `NoReturn`（見 `_shared/diagnostics.py`）。**`red()` 呼叫之後不應該再寫任何
> 「理論上會執行到」的敘述**（例如緊接著 `raise` 自訂的領域錯誤）——那一行必定是
> 死碼，只會誤導維護者以為上層攔截得到那個型別。`calendar_source.py` 曾經這樣寫
> 並被 review 判 MUST_FIX，修法是直接刪掉那行，讓 `red()` 自己終結流程就好。

**安全規定**：`access_token` / `refresh_token` / `client_secret` 的值一律不得出現在
診斷訊息、log 或例外訊息中；`cause` / `fix` 只能提檔案路徑與欄位名。

**適用範圍（2026-08-24 實查更正）**：本元件只適用「`token_env` 指向一份 OAuth token **JSON 檔**」
的模組，目前**只有 demo01**（`GOOGLE_CALENDAR_TOKEN` -> `sources/calendar_source.py`）。

其餘用到 `token_env` 的模組**接不上**，因為它們的憑證模型不同：

| 模組 | `token_env` 實際存的東西 | 為什麼接不上 |
| --- | --- | --- |
| demo05 `monitor.py` | 環境變數**直接就是 bearer token 字串** | 沒有 refresh_token 可換，無從續期 |
| demo06 `accounting.py` | `XERO_ACCESS_TOKEN` / `QUICKBOOKS_ACCESS_TOKEN`，同上 | 同上；且 Xero/QuickBooks 的續期流程與 Google 不同 |
| demo14 `triage.py` | 環境變數直接就是 token 字串 | 同 demo05 |
| demo11 `config.yaml` | `WORDPRESS_APP_PASSWORD`（應用程式密碼，非 OAuth） | 根本不是 OAuth；且 `push_enabled: false`，推送尚未實作 |

要讓這些模組也享有自動續期，得先把它們的憑證從「環境變數存 token 值」改成「環境變數指向 token JSON 檔」，
那是各模組的獨立改動，不屬於本契約範圍。
