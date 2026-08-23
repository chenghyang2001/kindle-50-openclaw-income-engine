# OpenClaw 10 模組 Demo 建置計畫

> 來源：《The OpenClaw Income Engine》第 03 章（Level 1 一人公司引擎）+ 第 04 章（一人公司實作深入）
> 建立日期：2026-08-24
> 狀態：**計畫已確認，尚未實作**

---

## 0. 已拍板的決策

| # | 決策 | 選擇 | 理由 |
| --- | --- | --- | --- |
| 1 | 範圍 | **全部 10 個模組** | 一次建立完整服務型錄 |
| 2 | 執行方式 | **Mock 優先 + 真實 API 可選** | `--mock` 免憑證離線跑完整流程；`--live` 才串真實 API |
| 3 | 通知管道 | **多通道抽象，預設 Telegram** | 書中用 WhatsApp（英國市場），台灣改 Telegram/LINE 更實際 |

### 架構原則（不可違反）

1. **混合式**：`_shared/` 放基礎設施，`demoNN-*/` 放業務邏輯。`package.py` 可把 `_shared/` vendor 進單一 demo 產出獨立交付版。
2. **三模式旗標**：每個 `main.py` 都支援 `--mock`（預設）、`--live`、`--dry-run`。
3. **絕不靜默降級**：`--live` 缺憑證要明確報錯退出，不可偷偷退回 mock。
4. **提示詞是資產**：一律獨立成 `prompts/*.md`，不內嵌在 `.py` 字串裡。
5. **自主權階梯是預設安全網**：任何會對外送出內容的模組，預設一律 `DRAFT` 模式。

---

## 1. 目錄結構

```
demo/
├── PLAN.md                            # 本檔
├── README.md                          # 總覽 + 模組對照表 + 快速上手
├── requirements.txt
├── .env.example
│
├── _shared/                           # Phase 0 地基（複雜度：複雜）
│   ├── __init__.py
│   ├── llm_client.py
│   ├── notifier.py
│   ├── autonomy.py
│   ├── config_loader.py
│   ├── diagnostics.py
│   └── package.py
│
├── demo01-morning-briefing/           # #1 晨間情報簡報
├── demo02-inbox-zero/                 # #2 收件匣清零代理
├── demo03-meeting-actions/            # #3 會議紀錄與行動提取
├── demo04-social-scheduler/           # #4 社群媒體內容排程
├── demo05-review-monitor/             # #5 客戶評價監控與回覆草擬
├── demo06-invoice-processor/          # #6 發票處理與費用分類
├── demo07-booking-scheduler/          # #7 預約排程器（原 WhatsApp 排程器）
├── demo08-price-monitor/              # #8 競品價格監控警報
├── demo09-sales-report/               # #9 每日銷售與進度報表
├── demo10-followup-sequence/          # #10 客戶跟進序列自動化
│
└── bundle-quickstart/                 # 打包層
    ├── README.md
    ├── proposal-template.md
    ├── pricing_calculator.py
    └── run_all.py
```

### 每個 demo 的固定六件套

| 檔案 | 內容 | 鐵律 |
| --- | --- | --- |
| `README.md` | Before/After 表、Financial Model、客戶見證、Client Pitch 話術 | 豁免 |
| `config.yaml` | 排程、閾值、字數上限、VIP 清單 | **受管** |
| `prompts/*.md` | 系統提示詞、人設、輸出結構 | 豁免 |
| `main.py` | 主流程 | **受管** |
| `mock/*.json` | 離線測試資料 | 豁免 |
| `test_main.py` | 3 個測試（happy / edge / integration） | **受管** |

---

## 2. `_shared/` 元件規格

### 2.1 `autonomy.py` — 自主權階梯（第 04 章核心）

```
READ_ONLY          只分類與情緒分析，絕不觸碰來源資料
    ↓
DRAFT（預設）       建立草稿，必須人工審查後送出
    ↓
SUPERVISED_AUTO    僅自動發送給 approved_senders 白名單，其餘降級為 DRAFT
```

**強制設計**：

- 預設值一律 `DRAFT`
- `SUPERVISED_AUTO` 必須傳入非空的 `approved_senders`，否則拋 `ValueError` 並降級為 `DRAFT`
- 提供 `days_in_draft_mode` 檢查：未滿 14 天呼叫 `SUPERVISED_AUTO` 要發出警告（書中鐵律）

### 2.2 `diagnostics.py` — RAG 診斷矩陣（第 04 章）

| 級別 | 行為 | 對應症狀 |
| --- | --- | --- |
| RED | 記錄 + `sys.exit(1)` | API Key Invalid、Twilio 憑證錯誤、OAuth 過期、Webhook 不可存取 |
| AMBER | 記錄警告，流程繼續 | 簡報超長、Spam 誤判、語氣不符、簡報延遲 |
| GREEN | 正常 | — |

提供 `@diagnose` decorator，把已知例外自動歸類到紅/琥珀。

### 2.3 `notifier.py` — 多通道發送

| 通道 | 實作 | 憑證 |
| --- | --- | --- |
| `telegram`（預設） | Bot API + urllib | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID_*` |
| `gmail` | `gws gmail users messages send` | 已登入 |
| `line` | Messaging API | `LINE_CHANNEL_TOKEN` |
| `whatsapp` | Twilio | `TWILIO_*` |
| `console` | 印到 stdout（mock 模式用） | — |

**注意**：Telegram 單則 4096 字元上限，超過要自動分段。

### 2.4 `llm_client.py`

- Claude API 封裝，預設 `claude-sonnet-5`（成本考量，非 Opus）
- 支援 `CONTEXT_NOTE` 參數（第 04 章：減少 40% 不相關輸出）
- `retry_on_timeout` + 指數退避
- token 用量記錄到 `.usage.jsonl`
- `--mock` 模式回傳 fixture，不呼叫 API（**零成本開發**）

### 2.5 `config_loader.py`

- YAML 載入 + 環境變數展開（`${VAR}`）
- 啟動時驗證必要環境變數，缺少就列出清單並 `sys.exit(1)`（不用預設值靜默掩蓋）

### 2.6 `package.py`

把 `_shared/` 複製進指定 demo 目錄、改寫 import 路徑、產出可獨立執行的交付包。

---

## 3. 十個模組規格

### demo01 — 晨間情報簡報

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <60min｜回收 35 hrs/mo（$2,625）｜售價 $300 + $75/mo |
| **核心流程** | 06:00 抓 6 來源 → Claude 統整 → 06:30 發送 → 90 秒讀完 |
| **技術規格** | 行事曆權重最高；**280–320 字**；5 區塊；**30 分鐘緩衝**（絕不同分鐘執行與發送） |
| **輸出結構** | `TOP_3_PRIORITIES` / `KEY_MEETINGS` / `KPI_DELTA` / `NEWS_ITEMS` |
| **人設** | 「具備完美背景知識、且無法忍受廢話的高效助理」 |
| **模組** | `sources/calendar_source.py`、`email_source.py`、`news_source.py` |
| **Mock 資料** | 假行事曆 5 筆、假信件 12 封、假新聞 8 則 |
| **自主權** | N/A（唯讀，只產出簡報） |
| **受管檔案** | 6 |

### demo02 — 收件匣清零代理

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <90min｜回收 33 hrs/mo（$2,640）｜售價 $400 + $100/mo |
| **核心流程** | 23:00 分類 VIP/FYI/Spam → 為 VIP 起草回覆 → 早上 10–15 分審批 |
| **技術規格** | `VIP_SENDERS`（domains / individuals / subject_keywords）；`TONE_EXAMPLES` 語氣校準（3–5 封真實信） |
| **自主權** | **主戰場**。預設 `DRAFT`；`AUTO_UNSUBSCRIBE` 前兩週強制 `false` |
| **合規** | README 必須含「內容由第三方 AI 處理」披露範本 |
| **Mock 資料** | 60 封混合信件（VIP 8 / FYI 30 / Spam 22） |
| **受管檔案** | 4 |

### demo03 — 會議紀錄與行動提取

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <60min｜回收 11 hrs/mo（$825）｜售價 $350 + $85/mo |
| **核心流程** | Webhook 收逐字稿 → 提取摘要/決策/**指定負責人** → 5 分鐘內發送 → 推 CRM |
| **技術規格** | **只提取明確陳述的承諾**（`I will...`、`Can you...`），捨棄模糊推論 |
| **品質預期** | 結構良好 85–92%；多人重疊 60–75%（README 要寫明，做預期管理） |
| **Mock 資料** | 3 份逐字稿：清晰版 / 混亂版 / 無負責人版 |
| **受管檔案** | 4 |

### demo04 — 社群媒體內容排程

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <90min｜回收 26 hrs/mo（$1,950）｜售價 $350 + $90/mo |
| **核心流程** | 10 分鐘簡報 → 產出全平台一週內容 → 審閱 20 分 → 跨平台排程 |
| **技術規格** | 每平台獨立語氣 profile（LinkedIn 專業 / IG 輕鬆 / X 簡短） |
| **自主權** | 預設 `DRAFT`（排程前必須人工過目） |
| **受管檔案** | 4 |

### demo05 — 客戶評價監控與回覆草擬

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <60min｜回收 11 hrs/mo（$825）｜售價 $300 + $80/mo |
| **核心流程** | 每 6 小時掃 Google/Trustpilot/Amazon → 情緒評分 → 依品牌語氣草擬 → **1–2 星 30 分內警報** |
| **技術規格** | 平均回應時間 3.2 天 → 4 小時內；負評走升級路徑而非一般通知 |
| **自主權** | 預設 `DRAFT`（公開回覆風險高，不建議自動送出） |
| **受管檔案** | 4 |

### demo06 — 發票處理與費用分類

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <90min｜回收 8 hrs/mo（$640）｜售價 $350 + $85/mo |
| **核心流程** | Email 到 → 解析 PDF → 提取廠商/金額/日期/稅額 → 對應會計科目 → 推 Xero/QuickBooks → 標準化命名存檔 |
| **技術規格** | 全自動 60 秒完成；金額用 `Decimal` 不用 float |
| **Mock 資料** | 5 張假發票 PDF（含 1 張模糊掃描、1 張外幣） |
| **受管檔案** | 4 |

### demo07 — 預約排程器（原 WhatsApp 排程器）

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <75min｜回收 10 hrs/mo（$750）｜售價 $300 + $75/mo |
| **核心流程** | 客戶詢問 → 查即時日曆 → 提供 3 時段 → 客戶選擇 → 自動確認 → 全權處理改期 |
| **技術規格** | 對話狀態機；時區處理；防重複預約（樂觀鎖） |
| **通道** | Telegram（原書 WhatsApp） |
| **受管檔案** | 4 |

### demo08 — 競品價格監控警報

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <60min｜回收 11 hrs/mo（$770）｜售價 $280 + $70/mo |
| **核心流程** | 每日 07:00 抓競品 URL 價格 → 與基準對比 → **超過閾值（如 5%）觸發晨間警報** |
| **技術規格** | 抓取間隔 ≥1 req/s（rate limit）；解析失敗要警報而非靜默跳過 |
| **Mock 資料** | 本地 HTML 快照 6 份（含 1 份改版導致選擇器失效） |
| **受管檔案** | 4 |

### demo09 — 每日銷售與進度報表

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <60min｜回收 11 hrs/mo（$825）｜售價 $300 + $80/mo |
| **核心流程** | 連 CRM/Shopify/Stripe → 算目標差距 → 標異常 → 07:00 準時發送 |
| **技術規格** | 單一資料源掛掉要標「部分資料」而非整份失敗 |
| **受管檔案** | 4 |

### demo10 — 客戶跟進序列自動化

| 項目 | 內容 |
| --- | --- |
| **書中數據** | 部署 <90min｜回收 12 hrs/mo（$960）｜售價 $350 + $90/mo |
| **核心流程** | CRM 階段改變觸發 → Day 3 輕度確認 → Day 7 案例研究 → Day 14 最終確認 |
| **技術規格** | **客戶一回覆立即中止**（最重要的安全機制，誤發會嚴重損害關係） |
| **自主權** | 預設 `DRAFT`；`SUPERVISED_AUTO` 需白名單 |
| **成效指標** | 轉換率 18% → 25%+ |
| **受管檔案** | 4 |

---

## 4. Session 拆分（依三 agent 鐵律的產能）

受管檔案（`.py` / `.yaml`）都要走 `code-writer` → `code-qa` → `code-reviewer`，總計約 **43 個**。分 6 個 session：

| Session | 內容 | 受管檔案 | 複雜度 | reviewer |
| --- | --- | --- | --- | --- |
| **A** | `_shared/` 地基 + repo 文件 | 7 | **複雜** | 必派（20+ 測試） |
| **B** | demo01 + demo02 | 10 | 中等 | 建議派 |
| **C** | demo05 + demo09 | 8 | 中等 | 問 |
| **D** | demo03 + demo04 + demo06 | 12 | 中等 | 問 |
| **E** | demo07 + demo08 + demo10 | 12 | 中等 | 問 |
| **F** | `bundle-quickstart/` + README 總覽 | 2 | 中等 | 問 |

**紀律**：每個 session 結束都要 commit + push，不留半成品跨 session。

---

## 5. 驗收標準

每個 demo 完工的定義：

- [ ] `python main.py --mock` 零憑證跑完，輸出到 console
- [ ] `python main.py --mock --notify telegram` 實際推到你的 Telegram
- [ ] `python -m pytest test_main.py` 三個測試全過
- [ ] `README.md` 含完整 Before/After + 財務模型 + Client Pitch 話術
- [ ] `--live` 缺憑證時明確報錯並列出缺哪些變數
- [ ] 無硬編碼路徑（`Path.home()`）、無硬編碼金鑰（`os.environ`）
- [ ] `code-qa` OVERALL=PASS

全案完工再加：

- [ ] `bundle-quickstart/run_all.py` 一鍵跑完 4 個打包模組
- [ ] `package.py` 能產出可獨立交付的單一 demo 包
- [ ] `demo/README.md` 有 10 模組對照表與定價總表

---

## 6. 已知風險與對策

| 風險 | 對策 |
| --- | --- |
| Claude API 開發成本 | `--mock` 用 fixture 不呼叫 API；真實呼叫才用 `claude-sonnet-5`，不用 Opus |
| Gmail OAuth token 7 天過期（書中紅色警報） | demo02 直接走已登入的 `gws` CLI，不自己做 OAuth |
| Google Calendar token 目前缺失 | demo01 的 calendar 來源先只做 mock，`--live` 明確報錯提示跑 OAuth |
| 43 個受管檔案的 QA 時間 | 分 6 session；簡單檔案評「簡單」只跑 2 個測試，不過度驗證 |
| 書中數據自相矛盾（回收時數矩陣加總 168 hrs 但封面寫 40–60 hrs） | README 註明兩種口徑：矩陣值為「客戶端價值主張」，40–60 為「自用實際回收」 |

---

## 7. 參考：模組總覽與定價總表

| ID | 模組 | 目錄 | 部署 | 回收 | Setup | 月費 | 階段 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| #1 | 晨間情報簡報 | `demo01-morning-briefing` | <60m | 35 hrs/mo | $300 | $75 | 奪回早晨 |
| #2 | 收件匣清零代理 | `demo02-inbox-zero` | <90m | 33 hrs/mo | $400 | $100 | 奪回早晨 |
| #3 | 會議紀錄與行動提取 | `demo03-meeting-actions` | <60m | 11 hrs/mo | $350 | $85 | 無縫營運 |
| #4 | 社群媒體內容排程 | `demo04-social-scheduler` | <90m | 26 hrs/mo | $350 | $90 | 品牌與聲量 |
| #5 | 客戶評價監控 | `demo05-review-monitor` | <60m | 11 hrs/mo | $300 | $80 | 品牌與聲量 |
| #6 | 發票處理與費用分類 | `demo06-invoice-processor` | <90m | 8 hrs/mo | $350 | $85 | 無縫營運 |
| #7 | 預約排程器 | `demo07-booking-scheduler` | <75m | 10 hrs/mo | $300 | $75 | 無縫營運 |
| #8 | 競品價格監控警報 | `demo08-price-monitor` | <60m | 11 hrs/mo | $280 | $70 | 業務增長 |
| #9 | 每日銷售與進度報表 | `demo09-sales-report` | <60m | 11 hrs/mo | $300 | $80 | 業務增長 |
| #10 | 客戶跟進序列自動化 | `demo10-followup-sequence` | <90m | 12 hrs/mo | $350 | $90 | 業務增長 |
| | **合計** | | | **168 hrs/mo** | **$3,280** | **$830** | |

**打包方案（第 04 章）**：#1 + #2 + #5 + #9 合併為「快速啟動方案」→ **$995 setup + $200/mo**
（單賣加總為 $1,300 + $335/mo；打包後單價較低但成交率 3 倍、部署成本降 60%）

---

## 8. 下一步

實作從 **Session A（`_shared/` 地基）** 開始。建議開新 session，第一句話就說：

> 讀 `demo/PLAN.md`，開始 Session A
