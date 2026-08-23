"""OpenClaw 十模組 Demo 的共用基礎設施層。

本套件提供 10 個 demo 共用的「地基」元件，業務邏輯一律留在各 demo 目錄：

| 模組 | 職責 |
| --- | --- |
| `autonomy` | 自主權階梯（READ_ONLY / DRAFT / SUPERVISED_AUTO）與白名單降級 |
| `diagnostics` | RAG 診斷矩陣（紅 / 琥珀 / 綠）與書中已知症狀對照表 |
| `llm_client` | Claude Messages API 封裝，預設 mock 模式（零成本開發） |
| `notifier` | 多通道發送（console / telegram / gmail / line / whatsapp） |
| `config_loader` | YAML 載入 + `${ENV_VAR}` 展開 + 必要環境變數驗證 |
| `package` | 把本套件 vendor 進單一 demo，產出可獨立交付的資料夾 |

刻意不在此處 eager import 任何子模組：`config_loader` 需要 PyYAML，
而 `autonomy` / `diagnostics` 是零依賴的純標準庫模組。若這裡先 import 全部，
只想用 `autonomy` 的情境也會被迫安裝 PyYAML。
"""

__all__ = [
    "autonomy",
    "config_loader",
    "diagnostics",
    "llm_client",
    "notifier",
]

__version__ = "1.0.0"
