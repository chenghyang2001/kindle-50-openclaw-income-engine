# 模組 #11 — SEO 內容引擎（SEO Content Engine）

> 每週一早上 08:00，Agent 已經把三篇對題的文章草稿放進 CMS 待審。
> 出處：《The OpenClaw Income Engine》第 05 章 ch05_p04 + 附錄F apxF_p05，Level 2「代理商基礎」行銷擴張／前台增長象限。

| 項目 | 數據 | 來源 |
| --- | --- | --- |
| 部署時間 | 1 Day（本專案以 480 分鐘計） | ch05_p04 / apxF_p05（兩來源一致） |
| 客戶每週回收 | 15+ 小時（≈ 65 小時/月） | ch05_p04 |
| 客戶內部價值 | $4,125–$4,875 /月 | ch05_p04 |
| 建置費 | **$750**（premium tier：$1,500） | apxF_p05 ／ ch05_p04 |
| 月費 | **$200**（premium tier：$400） | apxF_p05 ／ ch05_p04 |
| 預設自主權 | `DRAFT`（推送 CMS 前必須人工過目） | 第 04 章安全設計 |
| 觸發 | `cron 0 8 * * 1`（每週一 08:00） | apxF_p04 的 `context.json` 逐字 |

> ⚠️ **定價在原著兩處不一致。** 第 05 章寫 $1,500 + $400/月，附錄F 寫 $750 首付 + $200/月。
> 本模組**以附錄F 為主線報價**，第 05 章那組列為 premium tier 並存，兩者都可在 `config.yaml` 讀到、
> 都會被 `main.py` 的財務模型算出來。原著沒有說明哪一組才是正確的，本專案不做取捨、不做平均。

---

## Before / After

| | Before（沒有這個模組） | After（部署之後） |
| --- | --- | --- |
| 找題目 | 開 **5 個工具**分別匯出關鍵字報表，人工比對 | Agent 自動抓 **Top 12 關鍵字**，依排名 8-20 規則挑出 3 個 |
| 產草稿 | 寫大綱給外包寫手，等 **5-7 天** | 同一次執行就產出 1500 字草稿含 FAQ 結構 |
| 內部連結 | 靠編輯記憶，常常漏連或連錯 | 比對既有頁面清單，附上錨點文字與擺放位置建議 |
| 上架 | 手動貼進 CMS、逐篇補 meta | 直接產出 CMS payload（slug / excerpt / 分類 / Markdown 全備） |
| 每月成本 | 外包內容行銷 **$3,000–$5,000** | $750 建置 + $200/月（premium tier $1,500 + $400） |
| 沒做的下場 | 「沒有內容，競爭對手就會默默吞噬你的搜尋排名」（ch05_p04） | 每週固定三篇，排名 8-20 的字被逐週推進第一頁 |

### 為什麼是「排名 8-20」

`prompt.txt` 原文（apxF_p04）：`Selection criteria: prefer keywords where the site ranks position 8-20.`

這條規則寫死在 `keyword_planner.py`，**不交給 LLM 每週重新判斷**：

| 排名區間 | 意義 | 本模組的處理 |
| --- | --- | --- |
| 1–7 | 已在前段，再寫一篇的增量最小 | `TIER_ALREADY_RANKING`，最後才考慮 |
| **8–20** | Google 已認可相關性，只差臨門一腳 | `TIER_STRIKING`，優先選 |
| 21+ / 無排名 | 多半不是內容問題（是站權重與結構） | `TIER_LONG_TAIL`，只在甜蜜區不夠時補位 |

同一層內再用機會分數排序：`曝光量 × (1 − 難度/100) × 排名接近度`。
接近度讓排名 9 的字排在排名 19 之前——越靠近第一頁，補一篇就推上去的機率越高。

---

## 系統大腦：`context.json` / `prompt.txt` 的對應

附錄F p04 是 Level 2 十個模組**共通的技術底層頁**：一個檔管環境與規則，一個檔管角色與任務。
本專案用等價但更適合版控的結構實作，語意一一對應：

| 書中檔案 | 本模組對應 | 內容 |
| --- | --- | --- |
| `context.json`（環境與規則） | **`config.yaml`** | `trigger.schedule: "0 8 * * 1"`、`search_data.provider`、`content_settings.articles_per_week: 3`、`SEED_TOPICS`、選字門檻、定價 |
| `prompt.txt`（角色與任務） | **`prompts/*.md`** | `topic_selection.md`（PHASE 1 角色與判準）、`article_drafting.md`（PHASE 2 結構與輸出格式） |
| 整合金鑰（API Tokens） | `*_env` 欄位 | `GSC_PROPERTY_URL` / `WORDPRESS_API_BASE` / `WORDPRESS_APP_PASSWORD`，只存**變數名稱**，值一律走環境變數 |

書中 `context.json` 範例的三個欄位在 `config.yaml` 逐字保留：

```yaml
trigger:
  type: cron
  schedule: "0 8 * * 1"
search_data:
  provider: google_search_console
content_settings:
  articles_per_week: 3
```

---

## 自主權：為什麼預設是 DRAFT

SEO 文章掛上客戶網域就是**對外發言**，而且是會被 Google 索引、長期留存的那種。
一篇含錯誤數據的文章撤下來之後，快取與轉載還會留很久——撤稿成本遠高於每週一次的人工審閱。

想開啟 `SUPERVISED_AUTO`，必須同時滿足：

1. 已在 `DRAFT` 模式穩定運行 **14 天以上**（書中鐵律）
2. `approved_senders` 明確列出可自動發布的**種子主題**
3. 客戶書面簽核

本模組的白名單放的是「主題群」而不是收件人。例如客戶只信任盤點類文章可以自動發布：

```yaml
runtime:
  autonomy: supervised_auto
  approved_senders: ["庫存盤點"]
  days_in_draft: 30
```

其餘主題的文章會自動降級為 `draft`，CMS payload 的 `status` 也跟著降級。

### 【待填：…】不是 bug，是設計

離線組稿與提示詞都要求：**任何需要具體數字、客戶名稱、專案時程的位置，一律寫成 `【待填：說明要填什麼】`**。

書中把這個模組定位成「取代每月 $3k–$5k 的外包內容團隊」，而外包稿最大的風險不是文筆，
是**編出來的數字**。與其讓模型猜一個看起來合理的數值，不如把那格留成編輯的工作清單——
週一簡報會直接告訴你這週有幾處待填。

---

## 快速上手

```bash
# 離線跑完整流程（零憑證、零網路）
python main.py --mock

# 跑完但不發通知、不寫狀態檔
python main.py --mock --dry-run

# 狀態檔寫到別處，不污染工作樹
python main.py --mock --state-file /tmp/demo11-state.json

# 推到 Telegram
python main.py --mock --notify telegram

# 串真實 API；缺憑證會明確報錯，不會偷偷退回 mock
python main.py --live

# 跑測試
python -m pytest test_main.py -v
```

### 檔案結構

```
demo11-seo-content-engine/
├── README.md              本檔
├── config.yaml            = 書中的 context.json（觸發、門檻、SEED_TOPICS、定價）
├── main.py                主流程（載入 -> Phase 1 選題 -> Phase 2 草擬 -> 自主權 -> 通知）
├── keyword_planner.py     Phase 1：Top 12 抓取、排名 8-20 篩選、機會分數、冷卻期
├── content_generator.py   Phase 2：H2 大綱、1500 字草稿、FAQ、內部連結、CMS payload
├── prompts/               = 書中的 prompt.txt（角色與任務，獨立成檔不內嵌）
│   ├── topic_selection.md
│   └── article_drafting.md
├── mock/
│   ├── search_console.json  GSC 成效匯出（20 個字，含各種會被門檻擋下的樣本）
│   ├── site_pages.json      既有頁面清單（內部連結比對用）
│   └── brand_profile.json   品牌檔（定位、讀者、語氣支柱、禁用詞、專業筆記）
└── test_main.py           3 個測試（happy / 排名 8-20 邊界 / _shared 整合）
```

### 客戶要準備的三份東西

| 項目 | 誰給 | 多久 | 說明 |
| --- | --- | --- | --- |
| GSC 存取權 | 客戶，一次 | 5 分鐘 | 目前用成效報表匯出檔；API 直連見「已知限制」 |
| `SEED_TOPICS` | 一起討論，一次 | 30 分鐘 | 唯一需要長期維護的欄位，決定 Agent 往哪個方向擴長尾字 |
| `brand_profile.json` | 客戶，一次 | 40 分鐘 | 定位、讀者、語氣支柱、禁用詞、**專業筆記**（角度全部從這裡推導，不憑空發明） |

---

## Financial Model

### 對客戶（單一客戶視角，主線報價：附錄F）

| 項目 | 數字 |
| --- | --- |
| 每月回收工時 | 65 小時（15+ 小時/週 × 4.33 週） |
| 換算價值（$75/hr） | **$4,875** /月（正好對上 ch05_p04 的 $4,125–$4,875 上緣） |
| 原本的外包支出 | $3,000–$5,000 /月 |
| 客戶支出 | $750 建置 + $200 /月 |
| 首月淨效益 | $4,875 − $950 = **$3,925** |
| 次月起淨效益 | $4,875 − $200 = **$4,675** |
| 投資回收期 | 不到一週 |

### 對客戶（premium tier，第 05 章報價）

| 項目 | 數字 |
| --- | --- |
| 客戶支出 | $1,500 建置 + $400 /月 |
| 首月淨效益 | $4,875 − $1,900 = **$2,975** |
| 次月起淨效益 | $4,875 − $400 = **$4,475** |

> 兩組報價都由 `main._financials()` 以 `Decimal` 計算（全檔禁用 `float`：報價會直接寫進提案）。
> 什麼時候用 premium tier：客戶已有內容團隊、要求每月 12 篇以上、或需要多語系／多站點時。

### 對你（服務提供者視角，附錄F 報價）

| 客戶數 | 建置收入（一次） | 月經常性收入 | 年化經常性收入 |
| --- | --- | --- | --- |
| 5 | $3,750 | $1,000 | $12,000 |
| 10 | $7,500 | $2,000 | $24,000 |
| 20 | $15,000 | $4,000 | $48,000 |

用 premium tier 報價時，同樣 10 個客戶是 $15,000 建置 + $4,000 MRR（$48,000 ARR）。

apxF_p17 的商業決策矩陣把 #11 放在「中等配置、$200/mo」的位置——不是利潤最高的模組
（那是 #18 合約審查的 $500/mo），但它是**唯一直接對著客戶「營收管道」說話**的內容型模組，
客戶最容易理解、最容易成交。

---

## 客戶見證

**（原簡報未提供）**

《The OpenClaw Income Engine》第 05 章與附錄F 合計 34 張投影片中，**完全沒有出現任何人名、
職稱或客戶引述**。本專案不會為了讓 README 好看而編造見證——這與模組本身「寧可留【待填】
也不編數字」的設計原則是同一條。要填這一欄，請用你自己跑完一個月之後的真實數據
（ch05_p15 落地劇本的第 3 步：The Case Study）。

---

## Client Pitch 話術

### 原文（原著英文，逐字引述）

> 「Publish SEO-optimised content at agency scale — 12 articles per month... at a fraction of
> what a freelance content team would cost.」

### 繁體中文翻譯

> 「以代理商等級的規模發布 SEO 優化內容——每月 12 篇文章，成本只是外包內容團隊的一小部分。」

（`12 articles per month` = 每週 3 篇 × 4 週，正好對上 `content_settings.articles_per_week: 3`。）

### 展開版（三分鐘電話）

1. **戳痛點**：「你現在的內容是外包還是自己寫？外包一個月多少？」（書中基準：$3,000–$5,000）
2. **給對比**：「這套一個月 $200。產出是每週三篇、對著你已經排在第 8 到 20 名的字寫。」
3. **講規則**：「它不亂挑題目。只挑 Google 已經認得你、但還沒排進第一頁的字——那是最快看到位移的一批。」
4. **拆疑慮**：「AI 會不會亂編數字？不會，它遇到需要數字的地方會留【待填】給你的人補。前兩週一定跑草稿模式。」
5. **收尾**：「給我一週的 Search Console 讀取權限，我先跑一次給你看這 12 個字是哪些。」

### 常見異議

| 客戶說 | 回應 |
| --- | --- |
| 「AI 寫的 Google 會不會懲罰？」 | 「Google 罰的是沒有價值的內容，不是產出方式。這套的產出一定要人審過才發布，而且角度全部來自你自己的專業筆記。」 |
| 「我們有法遵用詞限制。」 | 「品牌檔有禁用詞欄位，命中就會在週一簡報標出來，不會偷偷發出去。」 |
| 「一個月 12 篇會不會太多？」 | 「`articles_per_week` 可以調成 1 或 2。這個數字是書中預設，不是硬性規定。」 |
| 「我可以自己用 ChatGPT。」 | 「可以。但你得每週自己匯出 GSC、自己算哪些字在第 8-20 名、自己比對內部連結、自己貼進 CMS。省下的是那個。」 |
| 「能不能全自動發布？」 | 「兩週後可以談，而且是分主題開放。你會先看到它在哪個主題上寫得穩，再決定放行哪一類。」 |

---

## 已知限制

- **GSC API 直連未實作**。目前的資料來源是 Search Console 成效報表的匯出檔（`mock/search_console.json`
  的格式）。`config.yaml` 已備妥 `property_url_env`，但 OAuth 流程與 API 客戶端不在本模組範圍內。
- **CMS 推送未實作**（`cms.push_enabled: false`）。本模組產出的是完整 payload（含 Markdown 全文），
  推進 WordPress REST API 需另行整合。這樣切的理由：推送是一次性的整合工，錯了很好修；
  而選題與草擬是每週都在跑的核心，值得先做穩。
- **SEMrush 難度值由匯出檔提供**。`difficulty` 欄位目前跟著 GSC 匯出檔一起餵進來，
  未串接 SEMrush API。
- **`--mock` 的文章是離線組稿**。`LLMClient` 在 mock 下只回傳佔位字串，
  `content_generator.compose_offline_article()` 會改用品牌檔與大綱樣板組出可讀草稿，
  好讓你看到的是**流程**而不是一堆 `[MOCK]`。離線稿每段都寫滿，字數會落在 2000 上下；
  真實的 1500 字文筆品質要跑 `--live` 才準。
- **狀態檔**：預設寫在同目錄 `.state.json`（已在專案 `.gitignore` 內），
  記錄每個關鍵字最後一次產出的日期，`state.cooldown_days`（預設 90 天）內不會重複選同一題。
  要換位置用 `--state-file`；`--dry-run` 不寫入。
