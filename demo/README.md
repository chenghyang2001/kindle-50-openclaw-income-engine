# OpenClaw 一人公司引擎 — 10 模組 Demo

《The OpenClaw Income Engine》第 03 章（Level 1 一人公司引擎）與第 04 章（一人公司實作深入）的**可執行實作**。

10 個自動化模組，每一個同時是：

- **你自己的生產力工具**（回收時間）
- **你賣給客戶的服務商品**（產生營收）

---

## 快速上手

```bash
# 1. 安裝依賴（只有 PyYAML 與 pytest）
python -m pip install -r requirements.txt

# 2. 挑一個模組，零憑證離線跑
cd demo01-morning-briefing
python main.py --mock

# 3. 把結果推到你的 Telegram
python main.py --mock --notify telegram

# 4. 跑測試
python -m pytest test_main.py -v
```

**不需要任何 API 金鑰即可跑完整流程。** `--mock` 是預設模式。

---

## 三種執行模式

| 旗標 | 行為 | 用途 |
| --- | --- | --- |
| `--mock`（預設） | 讀 `mock/` 假資料，不呼叫任何外部 API | 開發、教學、客戶簡報 |
| `--live` | 串真實 API；**缺憑證直接報錯退出，絕不靜默降級** | 正式運行 |
| `--dry-run` | 跑完整流程但不實際發送 | 上線前驗證 |

搭配 `--notify {console,telegram,gmail,line,whatsapp}` 決定輸出管道，預設 `console`。

---

## 模組總覽

| ID | 模組 | 目錄 | 部署 | 回收 | Setup | 月費 | 階段 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| #1 | 晨間情報簡報 | [`demo01-morning-briefing`](demo01-morning-briefing/) | <60m | 35 hrs/mo | $300 | $75 | 奪回早晨 |
| #2 | 收件匣清零代理 | [`demo02-inbox-zero`](demo02-inbox-zero/) | <90m | 33 hrs/mo | $400 | $100 | 奪回早晨 |
| #3 | 會議紀錄與行動提取 | [`demo03-meeting-actions`](demo03-meeting-actions/) | <60m | 11 hrs/mo | $350 | $85 | 無縫營運 |
| #4 | 社群媒體內容排程 | [`demo04-social-scheduler`](demo04-social-scheduler/) | <90m | 26 hrs/mo | $350 | $90 | 品牌與聲量 |
| #5 | 客戶評價監控 | [`demo05-review-monitor`](demo05-review-monitor/) | <60m | 11 hrs/mo | $300 | $80 | 品牌與聲量 |
| #6 | 發票處理與費用分類 | [`demo06-invoice-processor`](demo06-invoice-processor/) | <90m | 8 hrs/mo | $350 | $85 | 無縫營運 |
| #7 | 預約排程器 | [`demo07-booking-scheduler`](demo07-booking-scheduler/) | <75m | 10 hrs/mo | $300 | $75 | 無縫營運 |
| #8 | 競品價格監控警報 | [`demo08-price-monitor`](demo08-price-monitor/) | <60m | 11 hrs/mo | $280 | $70 | 業務增長 |
| #9 | 每日銷售與進度報表 | [`demo09-sales-report`](demo09-sales-report/) | <60m | 11 hrs/mo | $300 | $80 | 業務增長 |
| #10 | 客戶跟進序列自動化 | [`demo10-followup-sequence`](demo10-followup-sequence/) | <90m | 12 hrs/mo | $350 | $90 | 業務增長 |
| | **合計** | | | **168 hrs/mo** | **$3,280** | **$830** | |

### ⚠️ 關於「回收時數」的兩種口徑

書中封面寫「每月回收 **40–60 小時**」，但模組矩陣加總是 **168 hrs/mo**。這兩個數字口徑不同：

- **168 hrs/mo** = 矩陣值，是**賣給客戶時的價值主張**（客戶端可回收的理論上限）
- **40–60 hrs/mo** = 你**自己身上實際能回收的**（不是每個模組對你都全額適用）

對外提案時請選定口徑並說明基準，不要混用。

---

## 打包方案（第 04 章核心商業洞察）

`bundle-quickstart/` 把 **#1 + #2 + #5 + #9** 合併成「快速啟動方案」：

| | 單獨銷售（Features） | 快速啟動方案（Experience） |
| --- | --- | --- |
| 定價 | $1,300 setup + $335/mo | **$995 setup + $200/mo** |
| 客戶反應 | 「這是額外工具嗎？值得付月費嗎？」 | 「週一醒來，信箱已整理，報表在手機，生意在你睡覺時已運作」 |
| 部署時間成本 | 基準 | **↓ 60%** |
| 單日參與營收 | 基準 | **↑ 3 倍** |

**反直覺之處**：打包後單價**更低**，但成交率 3 倍、部署成本降 6 成，總淨值反而更高。
你賣的不是折扣，是**降低客戶的決策摩擦**。

---

## 架構

```
demo/
├── PLAN.md            # 建置計畫（模組規格、Session 拆分、驗收標準）
├── CONTRACT.md        # _shared API 契約（凍結，不得擅改簽名）
├── requirements.txt
├── .env.example
│
├── _shared/           # 基礎設施層（10 個 demo 共用）
│   ├── autonomy.py         # 自主權階梯：READ_ONLY → DRAFT → SUPERVISED_AUTO
│   ├── diagnostics.py      # RAG 診斷矩陣：RED 停擺 / AMBER 品質降級
│   ├── llm_client.py       # Claude API 封裝（mock 模式零成本）
│   ├── notifier.py         # 多通道通知（Telegram/Gmail/LINE/WhatsApp/Console）
│   ├── config_loader.py    # YAML + 環境變數驗證
│   └── package.py          # 打包成可獨立交付的單一 demo
│
├── demo01-morning-briefing/ ... demo10-followup-sequence/
│   ├── README.md      # Before/After + 財務模型 + Client Pitch 話術
│   ├── config.yaml
│   ├── main.py
│   ├── prompts/*.md   # 提示詞獨立成檔（這是核心資產，不內嵌程式碼）
│   ├── mock/*.json
│   └── test_main.py
│
└── bundle-quickstart/ # 打包層
```

### 為什麼是「混合式」而非 10 個完全獨立的資料夾

書中的商業模式是「**單品可賣、也能打包**」，架構必須對得上：

- **共用 `_shared/`**：10 個 demo 都要呼叫 Claude、發通知、走自主權階梯。複製 10 份 = 改一個 bug 要改 10 次
- **業務邏輯各自獨立**：交付客戶時要能單獨打包
- **`package.py` 補上最後一哩**：把 `_shared/` vendor 進單一 demo 目錄，產出可獨立執行的交付版

```bash
python _shared/package.py demo01-morning-briefing --out dist/
```

---

## 兩個貫穿全專案的安全設計

### 1. 自主權階梯（`_shared/autonomy.py`）

```
READ_ONLY          只分類與分析，絕不觸碰來源、絕不外送
    ↓
DRAFT（預設）       建立草稿，必須人工審查後送出
    ↓
SUPERVISED_AUTO    僅自動送給白名單，其餘一律降級為 DRAFT
```

**強制規則**：

- 預設值一律 `DRAFT`
- `SUPERVISED_AUTO` 白名單為空 → 拋 `AutonomyError`
- 草稿模式未滿 14 天就開全自動 → 發出警告（第 04 章鐵律：**兩週 + 客戶明確簽核前，絕不啟用全自動發送**）

### 2. RAG 診斷矩陣（`_shared/diagnostics.py`）

| 級別 | 行為 | 範例 |
| --- | --- | --- |
| 🔴 **RED** | 記錄後 `sys.exit(1)` | API Key 失效、OAuth 過期、Webhook 不可存取 |
| 🟠 **AMBER** | 記錄警告，流程繼續 | 簡報超長、Spam 誤判、語氣不符、簡報延遲 |
| 🟢 **GREEN** | 正常 | — |

**設計原則**：品質降級不該讓系統停擺，但也**絕不可靜默通過**。

---

## 與原著的差異

| 項目 | 原著 | 本專案 | 理由 |
| --- | --- | --- | --- |
| 通知管道 | WhatsApp（Twilio） | **Telegram**（預設，多通道可切） | 作者面向英國市場；台灣 WhatsApp 滲透率低 |
| 模組 #7 名稱 | WhatsApp 自動排程器 | 預約排程器 | 同上，通道無關化 |
| Gmail 存取 | 自建 OAuth | 走已登入的 `gws` CLI | 書中把「Gmail token 7 天過期」列為紅色警報，繞開最省事 |
| 開發成本 | 未提 | `--mock` 不呼叫 API | 反覆測試不該產生 API 帳單 |

---

## 環境需求

- Python **3.10+**（使用 `X | None` 型別語法）
- 依賴只有 **PyYAML** 與 **pytest**，HTTP 一律走標準庫 `urllib.request`
- Windows / macOS / Linux 皆可（檔案 I/O 全部明確 `encoding="utf-8"`）

環境變數請參考 `.env.example`。**`--mock` 模式一個都不需要。**

---

## 相關文件

- [`PLAN.md`](PLAN.md) — 完整建置計畫、模組逐一規格、驗收標準
- [`CONTRACT.md`](CONTRACT.md) — `_shared` API 契約
- [`bundle-quickstart/`](bundle-quickstart/) — 打包方案與客戶提案範本
- [`../pdf/`](../pdf/) — 原著 16 份章節簡報
