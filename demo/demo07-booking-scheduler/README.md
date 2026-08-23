# demo07 — 預約排程器（Booking Scheduler）

> 書中原名「WhatsApp 自動排程器」。本專案改用 **Telegram** 作為預設通道，
> WhatsApp / Twilio 保留為 `config.yaml` 的通道選項。

潛在客戶傳訊息說「想約時間」，代理程式**當場**查即時日曆、給出 3 個時段、
客戶回一個數字就完成預約，之後的改期也由代理程式全權處理。老闆從頭到尾不必打開日曆。

---

## 為什麼改用 Telegram（而非書中的 WhatsApp）

| 考量 | WhatsApp（原書） | Telegram（本專案） |
| --- | --- | --- |
| 台灣使用率 | 滲透率低，B2B 客戶幾乎不用 | Bot 生態成熟，開發者與商務用戶普及 |
| 上線門檻 | Twilio 沙盒需逐一驗證收件號碼，正式帳號要商務審核 | `@BotFather` 三分鐘取得 token，零審核 |
| 訊息成本 | 依模板訊息計價 | 免費 |
| 部署時間 | 審核期不可控，超出書中「< 75 分鐘」的承諾 | 符合 < 75 分鐘 |

程式碼層面兩者是對等的：`Notifier` 同時支援 `telegram` 與 `whatsapp`，
客戶若在東南亞或歐美市場，把 `channel.provider` 改成 `whatsapp` 並填 Twilio 憑證即可，
**狀態機與日曆邏輯完全不動**。

---

## Before / After

| | Before（人工排程） | After（代理程式排程） |
| --- | --- | --- |
| 客戶詢問 | 訊息躺在收件匣，數小時後才看到 | 秒級回應 |
| 查日曆 | 手動切到日曆比對空檔 | 自動查詢即時日曆 |
| 給選項 | 打字給 2-3 個時段 | 自動提供 **3 個**時段，附時區 |
| 等待回覆 | 客戶已讀不回，隔天再追 | 客戶回一個數字即完成 |
| 確認 | 手動建立事件、手動貼會議連結 | 自動寫入日曆 + 發送完整細節 |
| 改期 | 整個流程再跑一次 | 代理程式全權處理，老闆無感 |
| **每次預約的訊息來回** | **5-12 則** | **2-3 則** |
| 重複預約風險 | 兩位客戶同時選中同一格 -> 雙重預約 | 樂觀鎖擋下，自動改提新時段 |

---

## Financial Model

| 項目 | 數值 |
| --- | --- |
| 部署時間 | **< 75 分鐘** |
| 每月回收工時 | **10 小時** |
| 回收價值（$75/hr） | **$750 / 月** |
| 客戶收費 | **$300 建置 + $75 / 月** |
| 客戶首月淨值 | $750 − $375 = **+$375** |
| 客戶第 12 個月累計淨值 | $9,000 − $1,125 = **+$7,875** |
| 投資回收期 | **< 1 個月** |

---

## 客戶見證

> 「以前每週收到 15-20 個批發詢問，光是協調時間就花掉 3 小時。
> 現在 100% 的預約對話都由 Agent 處理，不再因為回覆慢而流失訂單。」
>
> — **Sarah Chen**，Brightleaf Living

---

## Client Pitch（話術）

> 「潛在客戶透過訊息預約。您的代理程式檢查即時日曆、確認時段、發送細節並處理改期
> ——讓您專注於真正需要您的工作。」

補一句成交用的對比：客戶現在每筆預約要來回 5-12 則訊息，導入後是 2-3 則；
一週 15 個詢問，等於每月省下 10 小時，而月費只要 $75。

---

## 執行方式

```bash
# 離線重播三段對話（零憑證、零網路）
python main.py --mock

# 跑完流程但不發通知、不寫狀態檔
python main.py --mock --dry-run

# 把對話狀態檔寫到別處（CI / QA 建議，避免在原始碼目錄留下產物）
python main.py --mock --state-file /tmp/demo07-state.json

# 串真實通道
python main.py --live --notify telegram

# 測試
pytest test_main.py -v
```

---

## 架構

```
demo07-booking-scheduler/
├── main.py             # CLI + 對話重播驅動 + 彙整輸出
├── state_machine.py    # 對話狀態機 + 狀態持久化
├── calendar_client.py  # 可用時段查詢 + 建立預約（樂觀鎖）
├── config.yaml         # 時區 / 時段長度 / 營業時間 / 通道
├── prompts/
│   └── conversation.md # 語氣、提供時段、處理衝突與改期的規則
├── mock/
│   ├── calendar.json      # 一週既有行程（週末公休）
│   └── conversations.json # 正常預約 / 要求改期 / 時段衝突
└── test_main.py        # happy / edge / integration
```

### 對話狀態機

```
INQUIRY ──OFFER_SLOTS──► SLOTS_OFFERED ──SELECT_SLOT──► SLOT_SELECTED ──CONFIRM──► CONFIRMED
                              ▲                              │                         │
                              └───────── SLOT_TAKEN ─────────┘                         │
                              ▲                                                        │
                              └──── OFFER_SLOTS ──── RESCHEDULE_REQUESTED ◄──REQUEST_RESCHEDULE
```

只有轉移表列出的 `(狀態, 事件)` 組合合法，其餘一律拋 `StateTransitionError`。
排程系統的 bug 幾乎都長成「在錯的狀態做了對的事」，讓它當場爆掉比事後對帳便宜得多。

### 防重複預約（樂觀鎖）

1. 提供時段時，把日曆當下的 `version` 一併記進對話 context。
2. 客戶選定後寫入日曆，必須帶回同一個 `version`。
3. 期間只要任何人寫入日曆，`version` 就遞增 —— 落後的寫入被 `CalendarConflictError` 擋下。
4. 代理程式收到衝突後：道歉並說明原因 -> **立刻重新提供 3 個時段** -> 狀態退回 `SLOTS_OFFERED`。

比「寫入前再查一次」可靠：後者在兩人同時選中同一格時仍會雙重預約。

### 為什麼 console 通道下摘要會出現兩次

這是刻意的，不是重複輸出的 bug：

| 輸出 | 來源 | 代表什麼 |
| --- | --- | --- |
| 第一次 | `Notifier(channel="console").send()` | **「發給客戶／老闆的那則訊息」本身**。console 是與 telegram / gmail 對等的真實通道，把要送出的內容原樣印出，才能在切換通道前先看清楚會送出什麼 |
| 第二次 | `_render_report()` | **本機執行報告**：摘要 + 每段對話的逐字稿 + 日曆版本 + amber 數 + 狀態檔位置 |

換成 `--notify telegram` 後第一份會送去 Telegram，主控台就只剩執行報告。
不想看到通道那一份就加 `--dry-run`（不發送，仍印報告）。

### 時區

一律標準庫 `zoneinfo`（**不使用 pytz**）。Windows 常態缺少 IANA tzdata，
`ZoneInfo("Asia/Taipei")` 會直接拋 `ZoneInfoNotFoundError`；此時降級為固定 `UTC+8`
並發出 amber 警示（代價：日光節約時間會失準，所以一定要讓人看得見）。
需要完全正確的時區行為就 `pip install tzdata`。

### 狀態檔

預設寫入 `state/conversations.json`，路徑以 `Path(__file__).parent` 展開（無硬編碼絕對路徑）。
`--state-file` 可覆寫（相對路徑一樣以模組目錄為基準，絕對路徑原樣採用）；沒給時才回頭取
`config.yaml` 的 `state.store_file`，CLI 預設值不會把 config 蓋掉。測試與 CI 一律指到暫存目錄。
`--dry-run` 完全不寫入。`--mock` 模式下日曆只在記憶體中變動，不會污染 `mock/calendar.json`，
因此每次 `python main.py --mock` 的輸出都可重現。

---

## 部署 Checklist（< 75 分鐘）

- [ ] `@BotFather` 建立 Bot，取得 `TELEGRAM_BOT_TOKEN`（5 分）
- [ ] 設定 `TELEGRAM_CHAT_ID_CHENGHYANG2001BOT`（5 分）
- [ ] 依客戶實際營業時間改 `config.yaml` 的 `business_hours` 與 `slot_duration_minutes`（10 分）
- [ ] 接上客戶真實日曆來源，取代 `mock/calendar.json`（30 分）
- [ ] `python main.py --mock` 確認流程無誤（5 分）
- [ ] `--live --dry-run` 驗證憑證（5 分）
- [ ] 觀察期：`runtime.autonomy` 維持 `draft` 至少 14 天，客戶簽核後才升 `supervised_auto`（書中鐵律）
