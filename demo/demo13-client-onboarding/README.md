# demo13 — 客戶入職工作流（Client Onboarding Workflow）

> Level 2 代理商基礎．自動化模組 **#13**
> 來源：《The OpenClaw Income Engine》第 05 章 p06 + 附錄 F p10
> 分類：客戶體驗 CX／Ops & CX｜部署時間：**1 Day**（兩來源一致）

交易階段一轉為 **Closed Won**，代理程式在 **60 秒內**寄出以專屬客戶經理名義具名的
個人化歡迎包與 Calendly 啟動會議連結，接著自動跑完 **Day 0 → Day 30** 的完整序列。
老闆在最忙的那一週，什麼都不用記。

---

## Before / After

| | Before（人工入職） | After（自動化入職） |
| --- | --- | --- |
| 歡迎信 | 簽約後 **1–3 天**才想起來寄 | 成交後 **60 秒內**送出 |
| 歡迎信內容 | 通用範本，客戶看得出來是群發 | `WELCOME_PACK_TEMPLATE` 動態插入客戶資料 + 銷售筆記重點 |
| 署名 | 「客戶服務團隊」 | **專屬客戶經理具名**，客戶只要對一個人 |
| Kick-off 會議 | 手動來回信件對時間，平均 4–6 封 | 自動生成**對應負責人**的 Calendly 連結，客戶自己挑 |
| 內部同步 | 業務口頭交接，交付團隊常常不知道成交了 | 自動通知 Slack **#new-clients**，含方案與窗口 |
| 第 7 天 | 沒人記得 | 自動送出主動關心（三個問題，回一句話就好） |
| 第 30 天 | 想起來時已經第 45 天 | 自動送出成效回顧邀約 |
| 重複觸發 | CRM webhook 重送 → 客戶收到兩封歡迎信 | 狀態檔去重，**同一階段永遠只交付一次** |
| 資料缺漏 | 直接寄出「親愛的 ，你好」 | **卡關並列出缺哪些欄位**，後續階段一律不啟動 |
| **每位客戶耗時** | **8 小時** | 接近 0（只剩補件與開會） |
| 90 天流失風險 | 混亂入職讓流失機率**增加 3 倍** | 序列化體驗，客戶從 Day 1 就覺得自己是唯一的客戶 |

---

## Financial Model

### 定價（採用附錄 F）

原著**兩個來源的定價不一致**，本模組以**附錄 F** 為準：

| 來源 | 設置費 | 月費 | 本模組採用 |
| --- | --- | --- | --- |
| 附錄 F p10 | $900 | **$180 / 月** | ✅ 預設（`module.client_monthly_price`） |
| 第 05 章 p06 | $900 | **$250 / 月** | 保留為 `module.premium_tier` |

設置費兩來源一致（$900），**只有月費不同**。第 05 章的 $250/月在 `config.yaml`
以 `premium_tier` 保留，作為含更多客製化階段時的高階方案報價；
選用時把 `client_monthly_price` 改成 250 即可，程式邏輯完全不動。
附錄 F p17 的商業決策矩陣也把 #13 標為 **$180/mo × 快速配置（<2 hrs）**，
與附錄 F 的定價自洽，這是採用附錄 F 的理由。

### 客戶端 ROI（預設方案）

| 項目 | 數值 | 來源 |
| --- | --- | --- |
| 部署時間 | **1 Day** | 書中原文 |
| 每位客戶回收工時 | **8 小時** | 書中原文 |
| 每月新客戶數 | 4 位 | **本 demo 假設值，原簡報未提供** |
| 內部時薪基準 | $75 / hr | **本 demo 假設值，原簡報未提供** |
| 每月回收工時 | 32 小時 | = 8 × 4 |
| 每月回收價值 | **$2,400** | = 32 × $75 |
| 客戶收費 | **$900 建置 + $180 / 月** | 附錄 F |
| 客戶首月淨值 | $2,400 − $900 − $180 = **+$1,320** | 計算 |
| 客戶第 12 個月累計淨值 | $28,800 − $900 − $2,160 = **+$25,740** | 計算 |
| 投資回收期 | $900 ÷ ($2,400 − $180) ≈ **0.41 個月** | 計算 |

高階方案（$250/月）：首月淨值 **+$1,250**，第 12 個月累計 **+$24,900**。

所有金額在程式中一律以 `decimal.Decimal` 計算（`onboarding.financial_summary()`），
全程禁用 `float`——財務尾差會累積成對不上的帳。

### 書中另一個價值口徑（無法換算）

書中還給了一個非工時的口徑：「避免早期流失，價值為 **3–6 倍年度合約**」。
原簡報**未提供年度合約金額**，因此無法換算成具體數字，此處如實留白，不做推估。

---

## 客戶見證

> **（原簡報未提供）**

34 張投影片（第 05 章 15 頁 + 附錄 F 19 頁）中**完全沒有出現人名、職稱或客戶引述**。
本欄位一律留白，不編造。

---

## Client Pitch（話術）

原文（附錄 F p10，英文原文照抄）：

> 「Every new client feels like your only client from Day 1... and a structured
> check-in sequence that keeps them engaged.」

繁體中文翻譯：

> 「每一位新客戶，從第 1 天起都會覺得自己是你唯一的客戶……
> 再加上一套結構化的追蹤序列，讓他們持續投入。」

補一句成交用的對比：客戶現在每簽一個新客戶要燒掉 **8 小時**在寄信、對時間、追進度上，
而且這 8 小時**永遠落在最忙的那一週**；混亂的入職會讓客戶在 90 天內流失的機率增加 **3 倍**。
導入後這 8 小時歸零，月費只要 **$180**。

附錄 F p17 的定位總結：
> 「合約審查（#18）是高難度高回報的利潤中心；**客戶入職（#13）則是快速建立客戶信任的穩健入門款**。」

---

## 執行方式

```bash
# 離線重播 CRM 觸發佇列（零憑證、零網路）
python main.py --mock

# 跑完流程但不發通知、不寫狀態檔
python main.py --mock --dry-run

# 把入職狀態檔寫到別處（CI / QA 建議，避免在原始碼目錄留下產物）
python main.py --mock --state-file /tmp/demo13-state.json

# 串真實通道
python main.py --live --notify telegram

# 測試
pytest test_main.py -v
```

---

## 架構

```
demo13-client-onboarding/
├── main.py             # CLI + 觸發處理 + 階段推進 + 彙整輸出
├── state_machine.py    # 入職狀態機（凍結轉移白名單）+ 冪等帳本 + 狀態持久化
├── onboarding.py       # 階段規格載入、資料完整性檢查、樣板渲染、財務模型
├── config.yaml         # ONBOARDING_SEQUENCE / WELCOME_PACK_TEMPLATE / 定價 / Calendly
├── prompts/
│   ├── welcome_pack.md # Day 0 歡迎信潤飾（live 模式）
│   └── checkin.md      # Day 7 / Day 30 追蹤信潤飾（live 模式）
├── mock/
│   └── clients.json    # 五筆 CRM 觸發：全新 / 進行中 / 已完成 / 重複觸發 / 資料缺漏
└── test_main.py        # happy / edge / integration
```

### 入職狀態機

```
NEW ──DEAL_CLOSED_WON──► CLOSED_WON ──SEND_WELCOME_PACK──► WELCOME_SENT
    ──SCHEDULE_KICKOFF──► KICKOFF_SCHEDULED ──HOLD_KICKOFF──► KICKOFF_HELD
    ──SEND_DAY7_CHECKIN──► DAY7_DONE ──SEND_DAY30_REVIEW──► DAY30_DONE
    ──COMPLETE──► COMPLETED
```

只有 `TRANSITIONS` 白名單列出的 `(狀態, 事件)` 組合合法，其餘一律拋 `StateTransitionError`。
入職流程的災難幾乎都長成「在錯的階段做了對的事」——第 7 天的關心信在客戶還沒收到歡迎信
之前就寄出去，比不寄還傷。讓它當場爆掉比事後跟客戶道歉便宜得多。

### ONBOARDING_SEQUENCE（Day 0 → Day 30）

| Day | 階段 | 交付物 | 必要欄位 |
| --- | --- | --- | --- |
| 0 | 歡迎包 | 個人化歡迎信 + 《歡迎手冊》+ Slack `#new-clients` 通知 | 公司、聯絡人、email、銷售筆記、客戶經理姓名/email |
| 0 | 啟動會議預約連結 | 對應負責人的 Calendly 連結 | email、客戶經理 Calendly slug |
| 3 | 啟動會議 | 會議紀錄與後續行動項 | 已預約時間、客戶經理 |
| 7 | 主動關心 | 三個問題的短信，回一句話即可 | email、客戶經理 |
| 30 | 成效回顧 | 回顧邀約 + 下一階段優先順序 | email、客戶經理、Calendly slug |

> Day 3 的啟動會議日**原簡報未指定**（原文只給 Day 0 → Day 30 的時間軸）。
> 本 demo 取 Day 3，代理商依客戶節奏改 `config.yaml` 即可，不需動程式碼。

### 冪等性（本模組的核心）

CRM webhook 會重送、排程會重跑、同一批次也可能出現重複的 `client_id`。
「重複寄一封歡迎信」不是小瑕疵——它會當場毀掉「你是我的專屬窗口」這個承諾。
因此本模組有**三道防線**：

| 防線 | 位置 | 擋掉什麼 |
| --- | --- | --- |
| 1. 批次內去重 | `main._process_trigger` 的 `seen` 集合 | 同一輪執行中重複出現的 `client_id`（webhook retry） |
| 2. 冪等帳本 | `ClientOnboarding.completed_stages` | 跨執行的重複交付：記錄每個階段**確實交付的時間戳** |
| 3. 狀態轉移白名單 | `state_machine.TRANSITIONS` | 流程錯亂：在 `WELCOME_SENT` 狀態再次觸發 `SEND_WELCOME_PACK` |

第 2 道與第 3 道刻意分開：狀態機拋的是 `StateTransitionError`（語意＝流程錯亂，是 bug），
重複觸發拋的是 `DuplicateStageError`（語意＝正常且預期的重送，該靜靜跳過）。
混在一起會讓真正的 bug 被當成雜訊忽略。

外部系統（Gmail / Slack / Drive）去重用的鍵是 `"{client_id}:{stage_key}"`，
同客戶同階段永遠相同，跨執行也不變。

**`--state-file` 就是冪等性的實體憑據：刪掉它，所有客戶都會被重寄一次歡迎信。**
預設寫入 `state/onboarding.json`（以 `Path(__file__).parent` 展開，無硬編碼絕對路徑）；
`--state-file` 可覆寫，沒給時才回頭取 `config.yaml` 的 `state.store_file`；
`--dry-run` 完全不寫入。測試與 CI 一律指到暫存目錄。

### 卡關（Blocked）不會靜默跳過

階段執行前會檢查 `required_fields`。缺欄位時：

1. 該階段標記為 **`BLOCKED`**，並**逐一列出**缺了哪些欄位；
2. 發出 amber 診斷（進 `amber_count`）；
3. **其後所有階段一律標記 `PENDING`（前置未完成）**，不會越過卡關階段往下做；
4. 摘要底部產生「需人工補件」清單，那就是要處理的工單。

空字串與只有空白的字串一律視為缺漏——CRM 匯入常見「欄位存在但沒填」，
若當成有值，寄出去的就是「親愛的 ，你好」這種當場毀掉信任的信。

同理，樣板渲染時未替換的佔位符**保留原樣**（`{contact_name}`）而非換成空字串，
並發出 amber。靜默的空白才是真正會寄出去的那一種錯。

### 四種階段結果

| 結果 | 意思 | 會不會送出訊息 |
| --- | --- | --- |
| `SENT` | 本輪實際交付 | ✅ |
| `SKIPPED_DONE` | 先前已交付，冪等跳過 | ❌ |
| `PENDING` | 時候未到，或前置階段未完成 | ❌ |
| `BLOCKED` | 客戶資料缺漏，必須人工補件 | ❌ |

### 自主權（AutonomyGate）

預設 `draft`：所有信件只建立草稿，等人工審查後才送出。
書中鐵律是「兩週觀察期 + 客戶簽核」之前不得升 `supervised_auto`；
升級後也只有命中 `approved_senders` 白名單的收件人會自動送出，其餘自動降回草稿。
歡迎信是客戶簽約後看到的第一個交付物，這是最不該搶快的一封信。

### 為什麼 console 通道下摘要會出現兩次

與 demo07 相同，這是刻意的：第一次是 `Notifier(channel="console").send()`
——**「要送出的那則訊息」本身**；第二次是 `_render_report()` 的本機執行報告
（摘要 + 本輪實際送出的每一則信件全文 + ROI + amber 數 + 狀態檔位置）。
換成 `--notify telegram` 後第一份會送去 Telegram；不想看到通道那一份就加 `--dry-run`。

---

## 部署 Checklist（1 Day）

- [ ] 在 CRM 建立 webhook：`deal.stage_changed`，過濾條件 `deal_stage == "Closed Won"`（30 分）
- [ ] 確認 CRM 一定會帶出 `client_id`（去重鍵）、聯絡人 email、銷售筆記（30 分）
- [ ] 每位客戶經理建立 Calendly `kickoff-30min` 事件，把 slug 填進 CRM 的負責人欄位（45 分）
- [ ] 依客戶語氣改寫 `config.yaml` 的 `templates`（**不要動程式碼**）（60 分）
- [ ] 依客戶節奏調整 `onboarding.sequence` 的 `day`（啟動會議日、關心日）（15 分）
- [ ] 建立 Slack `#new-clients` 頻道與 webhook（15 分）
- [ ] `python main.py --mock` 確認五種情境都走對路（15 分）
- [ ] `--live --dry-run` 驗證憑證（15 分）
- [ ] 把 `state/onboarding.json` 放進**有備份**的路徑，並排除於版控之外（10 分）
- [ ] 排程：每 1 分鐘跑一次（才守得住「60 秒內寄出」的承諾）（15 分）
- [ ] 觀察期：`runtime.autonomy` 維持 `draft` 至少 14 天，客戶簽核後才升 `supervised_auto`（書中鐵律）

---

## 已知的原著不一致（如實記錄，不做修正）

| 項目 | 第 05 章 | 附錄 F | 本模組處置 |
| --- | --- | --- | --- |
| 模組名稱 | 客戶入職工作流（Client Onboarding Workflow） | 客戶入職自動化（Client Onboarding） | 用第 05 章全名 |
| 月費 | $250 | $180 | 採附錄 F，第 05 章存為 `premium_tier` |
| 設置費 | $900 | $900 | 一致，直接採用 |
| 部署時間 | 1 Day | 1 Day（p17 圖標示為「快速配置 <2 hrs」） | config 取 1 Day = 480 分鐘 |
| 啟動會議日 | 未指定 | 未指定 | 本 demo 取 Day 3，可於 config 調整 |
| 客戶見證 | 無 | 無 | 留白，不編造 |
