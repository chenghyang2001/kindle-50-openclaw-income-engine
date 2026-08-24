# demo01 — 晨間情報簡報（Morning Intelligence Briefing）

> 來源：《The OpenClaw Income Engine》第 03 章（Level 1 一人公司引擎）＋第 04 章（一人公司實作深入）
> 階段：奪回早晨 ｜ 部署時間 <60 分鐘 ｜ 自主權 `READ_ONLY`

每天 06:00，代理人抓取行事曆、信件與新聞來源，交給 Claude 統整成**一份**結構化簡報，
06:30 送到客戶手機。收件者 90 秒讀完，就知道今天最重要的三件事。

---

## Before / After

| 面向 | Before（人工） | After（代理人） |
| --- | --- | --- |
| 每天早上耗時 | 40–70 分鐘在 6 個分頁之間切換 | **90 秒**讀完一份簡報 |
| 資訊來源 | 行事曆、信箱、報表、3 個新聞網站分開看 | 單一入口，已排序、已去重 |
| 排序依據 | 誰的信最新、誰喊得最大聲 | **行事曆優先**：今天要開的會決定一切排序 |
| 遺漏風險 | 重要信件埋在 30 封未讀裡 | 與今日會議相關的寄件者自動升級為 VIP |
| 開始工作的時間 | 09:20（先「搞清楚狀況」） | 08:30（一坐下就知道要做什麼） |
| 心理負擔 | 整天擔心漏掉什麼 | 沒寫進簡報的就是今天不重要 |
| 每月回收 | — | **35 小時／月** |

---

## Financial Model

| 項目 | 數字 |
| --- | --- |
| 客戶端每月回收工時 | 35 hrs |
| 以 $75/hr 計價的價值 | **$2,625／月** |
| Setup 一次性費用 | **$300** |
| 月費 | **$75／月** |
| 客戶第一年成本 | $300 + $75 × 12 = **$1,200** |
| 客戶第一年價值 | $2,625 × 12 = **$31,500** |
| 客戶 ROI | **約 26 倍** |
| 你的部署時間 | **< 60 分鐘** |
| 你的邊際成本 | Claude API 每月約 $2–4（每日一次呼叫，1,200 tokens 上限） |

> 定價邏輯：月費只佔客戶回收價值的 **2.9%**。客戶不是在買軟體，是在買回每天 40 分鐘。
> 這也是為什麼不該用「幾個 API 呼叫」來定價——價格錨定在**回收的時間**，不是成本。

**打包升級路徑**：本模組 + demo02（收件匣清零）+ demo05（評價監控）+ demo09（銷售報表）
＝「快速啟動方案」$995 setup + $200/mo（單賣加總 $1,300 + $335/mo）。
打包後單價較低，但成交率約 3 倍、部署成本降 60%。

---

## 客戶見證

> 「以前每天早上花 40 分鐘搞清楚狀況。現在我下火車時，已經確切知道今天什麼最重要。」
>
> — **Marcus Webb**，律師事務所合夥人

---

## Client Pitch 話術

> 「每天以一份專屬的情報簡報開始——您的日曆、數據、新聞——在隔夜自動彙整，
> 並在您咖啡泡好前發送到您的手機。」

搭配使用的三個追問（照順序問，不要跳）：

1. 「您每天早上花多久，才真正搞清楚今天要幹嘛？」（讓對方自己說出 30–60 分鐘）
2. 「那段時間您的時薪怎麼算？」（把痛點換算成金額，對方自己會算出 $2,000+/月）
3. 「如果一份 90 秒的簡報能省掉那段時間，一個月 $75 划算嗎？」（成交）

---

## 三個不可妥協的設計

### 1. 行事曆權重最高

今日行程決定整份簡報的排序。與今日會議有關的「小事」，優先級高於無關的「大新聞」。
實作上還有一層自動加權：**寄件者若出現在今日會議的與會名單，該信自動升級為 VIP**
（見 `main.py` 的 `apply_calendar_weighting`），所以 live 模式不必另外維護 VIP 清單。

行事曆是唯一「失敗即中止」的來源——沒有行程資料的簡報等於沒有價值，
因此走 `Diagnostics.red`，不會產出半殘的簡報。

### 2. 90 秒法則（280–320 字）

| 門檻 | 設定 | 行為 |
| --- | --- | --- |
| 目標區間 | 280–320 字 | 正常 |
| 硬上限 | 400 字 | 觸發 AMBER `briefing_too_long`，提示強化提示詞後重跑 |

字數計算方式：中日文一字算一字，連續英數字串算一個字，標點不計入（`main.count_words`）。

### 3. 30 分鐘緩衝

`execute_at: "06:00"` 與 `deliver_at: "06:30"` **絕不可設在同一分鐘**。
API 稍有延遲，同分鐘設定的結果就是「空簡報準時送達」——比晚到更糟。
程式啟動時驗證兩者間隔 ≥ `min_buffer_minutes`（預設 20 分），不足即觸發
AMBER `delayed_briefing`。

---

## 輸出結構（5 區塊，順序固定）

```
HEADLINE           一句話定調今天（≤30 字）
TOP_3_PRIORITIES   恰好 3 條，每條都是可執行的動作
KEY_MEETINGS       依時間排序，最多 3 場，附準備事項
KPI_DELTA          只寫變化量，↑/↓ 標方向，沒變化的省略
NEWS_ITEMS         最多 3 則，每則必須接「對我方的影響」
```

人設：**具備完美背景知識、且無法忍受廢話的高效助理**（完整提示詞見 `prompts/briefing.md`）。

---

## 使用方式

```bash
# 離線跑（零憑證、零網路，驗收指令）
PYTHONUTF8=1 python main.py --mock

# 跑完流程但不發送
PYTHONUTF8=1 python main.py --mock --dry-run

# 推到 Telegram
PYTHONUTF8=1 python main.py --mock --notify telegram

# 串真實 API（缺憑證會明確報錯，不會偷偷退回 mock）
PYTHONUTF8=1 python main.py --live --notify telegram

# 測試
PYTHONUTF8=1 python -m pytest test_main.py -v
```

### `--live` 需要的憑證

| 來源 | 需要 | 缺少時 |
| --- | --- | --- |
| Claude | 無（走本機 `claude` CLI／Max 訂閱 OAuth，非 `ANTHROPIC_API_KEY`） | RED `Claude CLI 未安裝或不在 PATH` |
| 行事曆 | `GOOGLE_CALENDAR_TOKEN`（指向 OAuth token JSON） | RED，附 OAuth 修復步驟 |
| 信件 | 已登入的 `gws` CLI | RED `oauth_error` / `gws_cli_missing` |
| 新聞 | 無（公開 RSS） | 單一 feed 失敗走 AMBER，不中斷 |

> 行事曆目前尚未完成 OAuth 授權，`--live` 會明確要求先跑一次授權流程。
> 這是刻意設計：**寧可報錯，也不偷偷退回 mock 讓客戶以為系統還活著。**

---

## 診斷矩陣（本模組會觸發的）

| Key | 級別 | 意義 | 修法 |
| --- | --- | --- | --- |
| `google_calendar_token_missing` | RED | 行事曆 token 未設定 | 跑一次 Google OAuth 並設環境變數 |
| `oauth_error` | RED | token 過期或 `gws` 未登入 | 重新授權（Gmail token 有 7 天限制） |
| `gws_cli_missing` | RED | 找不到 `gws` 指令 | 安裝並登入 Google Workspace CLI |
| `briefing_too_long` | AMBER | 輸出超過 400 字 | 提示詞強制「最高 320 字，無情刪減」 |
| `delayed_briefing` | AMBER | 執行與發送間隔不足 | 提早 `execute_at`，開 `retry_on_timeout` |
| `news_feed_unreachable` | AMBER | 單一 RSS 失效 | 換掉該來源；新聞是最低權重，不中斷 |

---

## 自主權：`READ_ONLY`

本模組**只產出簡報**，永遠不代替使用者回信、不對外承諾任何事。
`AutonomyGate` 固定在 `READ_ONLY`，`can_send()` 對任何收件人都回 `False`。

發送簡報給**使用者本人**不屬於「對外送出」——那是把報告交給報告的擁有者。
真正需要 `DRAFT` / `SUPERVISED_AUTO` 的是 demo02（收件匣清零）與 demo10（跟進序列）。

---

## 檔案結構

```
demo01-morning-briefing/
├── README.md                  本檔
├── config.yaml                排程、字數門檻、來源、KPI
├── main.py                    主流程（CLI 依 CONTRACT.md §6）
├── prompts/briefing.md        系統提示詞（人設 + 5 區塊 + 320 字上限）
├── sources/
│   ├── calendar_source.py     mock JSON ／ Google Calendar API
│   ├── email_source.py        mock JSON ／ gws CLI
│   └── news_source.py         mock JSON ／ RSS（urllib + ElementTree）
├── mock/                      5 筆行程、12 封信、8 則新聞
└── test_main.py               happy / edge / integration
```

### 離線模式的簡報從哪裡來

`--mock` 時 `LLMClient` 不呼叫 API，回傳 `[MOCK] ...` 佔位字串。
這對示範 90 秒法則沒有價值，因此 `main.py` 會改用本地渲染器
`render_offline_briefing()` 依同一套 5 區塊規則產出真正可讀的簡報。

這是**離線模式的明示行為，不是靜默降級**：`--live` 缺憑證時一律 RED 中止，
永遠不會走到這條路徑。
