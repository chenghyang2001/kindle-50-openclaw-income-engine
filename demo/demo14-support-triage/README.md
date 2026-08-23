# demo14 — 多渠道客服分流（Multi-Channel Support Triage）

> 模組 #14｜Level 2 代理商基礎｜分類：客戶體驗 CX / Ops & CX｜部署 **1 Day**
> 內部回收 **每週 25+ 小時**｜回應時間 **3-4 小時 → 10 分鐘內**
> 客戶售價 **$800 首付 + $190/月**（附錄 F；第 05 章另有一組較高定價，見下方「定價來源不一致」）

即時監控 Gmail / Intercom / WhatsApp / Instagram 四個渠道，把每一則進件依附錄 F p12 的
決策樹分流：能查得到答案的**兩分鐘內自動回覆**，涉及金錢、情緒或法律的**一律交給真人**。

---

## Before / After

| | Before（人工） | After（代理程式） |
| --- | --- | --- |
| 收件方式 | 不斷切換 Gmail、Intercom、WhatsApp、Instagram 四個收件匣 | 四個渠道統一收件，單一渠道故障不影響其他渠道 |
| **回應時間** | **3-4 小時**，忙起來到隔天 | **10 分鐘內**；FAQ 與訂單查詢 **2 分鐘內** |
| 漏看風險 | 平日還好，週末與夜間常整批漏看 | 每則進件都被分類、都有去處，去重機制確保不重複也不遺漏 |
| 常見問題 | 每次重打一次答案，講法每個人不一樣 | 比對 33 條**已審核**的知識庫條目，答案逐字一致 |
| 訂單查詢 | 開後台查、複製貼上、再回信 | 自動查訂單系統，回覆物流商、追蹤編號與預計送達 |
| 退款與爭議 | 誰先看到誰回，容易講出不該講的話 | **DO NOT PROCESS**：只給 Holding Response 草稿，強制升級真人 |
| 客訴與 VIP | 埋在一般訊息裡排隊 | Immediate Flag，**高優先**升級卡片排在值班頻道最前面 |
| 每週耗時 | 約 25+ 小時（客服團隊合計） | 約 4 小時（純審查升級件與知識庫維護） |

---

## 分流決策樹（逐字實作 apxF_p12 — Triage Decision Logic）

```
Inbound Message（Gmail / Intercom / WhatsApp / Instagram）
        │
        ├─ 去重（.handled.json 記錄已處理的 channel:message_id）
        │
        v
Intent Classification（意圖判定 — 規則式，LLM 不得推翻）
        │
   ┌────┴───────────────┬──────────────────┬────────────────────┐
   │                    │                  │                    │
退款 / 爭議        負面情緒 / VIP / 法務   訂單狀態              常見問題
Refund/Dispute     Negative/VIP/Legal     Order Status          FAQ
   │                    │                  │                    │
DO NOT PROCESS      Immediate Flag     Query Shopify API   Search knowledge_base
（Holding Response） │                  │                    │
   │                    │             查得到？              命中 >= 2 關鍵字？
   │                    │              ├─ 是 → Auto-Reply    ├─ 是 → Auto-Reply
   │                    │              │      精確物流更新   │      標準解答
   │                    │              └─ 否 ↓               └─ 否 ↓
   v                    v                    v                    v
Escalate to Slack   Escalate to Slack   Escalate to Slack    Escalate to Slack
                    （High Priority）    （查無訂單，不編造） （不猜答案）
```

**判斷順序就是安全順序。** 一則訊息同時命中退款訊號與 FAQ 關鍵字時，永遠走退款路徑。
決策樹沒有涵蓋的訊息落入 `unknown`，也一律升級——**不確定時往真人倒，不往自動化倒**。

### Slack Escalation Payload（格式逐字照抄 apxF_p12）

```
*[PRIORITY] Support Escalation - {CHANNEL} - {CATEGORY}*
> Customer: {NAME} | Query: [1-sentence summary]
```

實際輸出範例：

```
*[HIGH] Support Escalation - GMAIL - REFUND_DISPUTE*
> Customer: 許鎮宇 | Query: 要求退款
> Holding Response（待真人確認後送出）：許鎮宇 您好，我們已收到您關於退款／款項爭議的來訊…
```

摘要句刻意**不經過 LLM**：升級卡片是安全關鍵路徑，API 掛掉時它仍然必須送得出去。

---

## 🔴 安全鐵律一：`never_claim_human` — 誠實揭露 AI 身分

**自動回覆絕不可讓客戶以為自己在跟真人對話。** 這條規則有三層實作：

| 層 | 位置 | 做什麼 |
| --- | --- | --- |
| 1. 提示詞 | `prompts/persona.md` | 明令不得使用真人化措辭、不得虛構人類身分細節、被問就直接承認是 AI |
| 2. 內容層 | `triage.apply_disclosure()` | 每一則對外訊息（含 Holding Response）都強制附加 `persona.ai_disclosure` |
| 3. 送出前 | `triage.find_impersonation()` | 掃描 `human_impersonation_phrases`，**命中就取消自動回覆並改升級真人**，同時發琥珀警示 |

第 3 層是重點：提示詞可以被忽略、知識庫條目可以被改壞，但只要送出前一定會走過那個函式，
冒充真人的句子就出不去。**這是程式層的保證，不是文件層的期待。**

知識庫另外備有 `kb-031` 專門回答「你是機器人還是真人」，答案第一句就承認自己是 AI，
並說明如何轉接真人——被問到身分時，誠實本身就是標準解答。

**為什麼不能妥協：**

- **法規面**：多個市場已要求 AI 互動必須可辨識，冒充真人屬欺瞞性商業行為。
- **平台面**：WhatsApp / Instagram / Intercom 的商用條款皆要求自動化訊息可被識別。
- **信任面**：客戶事後發現自己被機器人假扮的人騙過，損失的不是一次對話，是整個品牌。

---

## 🔴 安全鐵律二：退款與爭議 `DO NOT PROCESS`

涉及**退款、退費、退貨款項、爭議款、拒付、重複扣款、申訴**的訊息，
無論自主權設到多高、無論寄件人是否在白名單內，**一律不自動回覆**，強制升級真人。

**這條規則不吃 `config`。** 比照 demo10 的 `stop_on_reply` 設計：

```yaml
safety:
  refund_dispute_do_not_process: false   # 改成 false 也沒用
  never_claim_human: false               # 同上
```

程式啟動時會把兩者強制覆寫回 `true`，並發出琥珀警示：

```
[AMBER] [demo14-support-triage] 症狀：config.safety.refund_dispute_do_not_process 被設為 False，
        已強制覆寫為 true（退款/爭議一律不自動回覆）｜對策：把 config.yaml 改回 ...: true
```

**為什麼做成改不掉的：** 這兩條不是「偏好設定」，是這個模組能不能合法上線的前提。
做成可設定的旗標，等於把「哪天有人為了衝自動回覆率把它關掉」變成一個選項。
退款爭議的每一句話都可能成為後續金流爭議或法律程序中的公司立場——
這種句子必須由有權限的人簽字，不能由統計模型生成。

**金額門檻的正確理解**：書中 `ESCALATION_RULES` 提到「退款金額過大即轉交真人」。
本實作的 `refund_amount_threshold` 只影響**優先度**（達門檻升為 HIGH），
**不影響是否自動回覆**——因為答案永遠是不自動回覆。門檻低不代表可以讓機器人處理小額退款。

**Holding Response 的定位**：只確認收到、只給真人聯繫時限；不認責、不談金額、不說處理結果。
本實作把它做成「已寫好、待真人一鍵送出」的草稿，附在 Slack 升級卡片內，
而**不是**自動送給客戶——因為「已收到」這三個字送出的時機本身，也可能被解讀為公司態度。

---

## 為什麼分類是規則式的，LLM 只當顧問

`prompts/intent_advisory.md` 的判讀會被記錄在每則結果的 `advisory` 欄位，供客服主管每週回顧
「規則有沒有漏掉什麼」，但**永遠不會改變任何一則訊息的去向**。

| | 規則式分類 | LLM 分類 |
| --- | --- | --- |
| 分類錯 FAQ 的後果 | 回答不夠貼切 | 回答不夠貼切 |
| 分類錯退款爭議的後果 | — | **機器人替公司對一筆爭議款做出回應**（法律風險） |
| 主管能不能複查「為什麼這樣分」 | 能，看命中哪個關鍵字 | 不能 |
| 出錯後能不能當天改掉 | 能，改 config 的關鍵字清單 | 只能改提示詞再祈禱 |

同理，**自動回覆的內容也不由 LLM 生成**：FAQ 回覆逐字來自知識庫條目，
訂單回覆逐欄來自訂單系統。每一則自動回覆都帶 `source` 欄位（`knowledge_base:kb-025` /
`orders:BL-20817`），出事時可以直接追到那一條答案是誰審的。

---

## 自主權階梯與「2 分鐘內回覆」的關係

本模組預設 `autonomy: draft`——自動回覆會產出，但**不會送出**。
要真正做到書中的「2 分鐘內回覆 FAQ」，必須把自主權調到 `supervised_auto` 並填白名單：

```yaml
runtime:
  autonomy: supervised_auto
  approved_senders: ["@existing-customer.com"]   # 網域比對必須以 @ 開頭
  days_in_draft: 14                              # 書中鐵律：先跑滿兩週草稿模式
```

即使調到 `supervised_auto`，退款爭議、負面情緒、VIP、法務、`unknown` 五類仍然**一則都不會自動送出**——
自主權管的是「可以自動送給誰」，安全鐵律管的是「哪些內容根本不該由機器送」，兩者是 AND 不是 OR。

---

## Financial Model

**客戶端（買方視角）**

| 項目 | 數字 |
| --- | --- |
| 每週省下的時間（書中數字，客服團隊合計） | 25+ hrs |
| 換算每月 | 約 108 hrs（25 × 4.33 週） |
| 時間價值（以 $75/hr 計） | **$8,100 / 月** |
| 首月成本（setup + 月費） | $990 |
| 後續每月成本 | $190 |
| 第一年總成本 | $800 + $190 × 12 = **$3,080** |
| 第一年總價值 | $8,100 × 12 = **$97,200** |
| 回本時間 | **第一週內** |

> ⚠️ 「每週 25+ 小時」是原簡報針對**整個客服團隊**的數字，不是單一人員。
> 客戶只有一位兼職客服時，實際回收會遠低於此，報價時請據實調整，
> 不要把書上的數字直接當成客戶的數字講。

未計入的隱性價值：書中主張「回應時間就是產品特色——10 分鐘內回覆能贏得訂單，
3 小時回覆只會讓客戶猶豫」。轉換率的差異不在上表任何一格裡。

**服務商端（賣方視角）**

| 項目 | 數字 |
| --- | --- |
| 部署工時 | 1 Day（apxF_p17 歸類為「快速配置 < 2 hrs」，兩來源不一致） |
| 首次收入 | $800 |
| 每月經常性收入 | $190 / 客戶 |
| 10 個客戶的經常性收入 | **$1,900 / 月** |
| 每月維護 | 約 30 分鐘 / 客戶（主要是知識庫條目增補） |
| 打包加乘 | 與 #13 客戶入職併售，構成完整的「客戶體驗 CX」模組（apxF_p03 同象限） |

### 定價來源不一致（原著問題，如實記錄）

| 來源 | 首付 / 設置費 | 月費 |
| --- | --- | --- |
| **附錄 F**（本模組採用） | **$800** | **$190** |
| 第 05 章（premium tier） | $1,100 | $300 |

兩個來源對同一模組給了兩組定價，原簡報未說明差異原因。本實作的 `config.yaml`
一律採**附錄 F** 的數字；第 05 章的較高定價在此列為 premium tier 供報價時參考。
兩者相差 37.5%（首付）與 57.9%（月費），差距大到不宜取平均，因此並列不合併。

---

## 客戶見證

**（原簡報未提供）**

> 原著 34 張投影片中完全沒有出現人名、職稱或客戶引述（見 SPEC-11-20「異常 5」）。
> 此欄位刻意留白，**不編造見證**。需要案例時請依第 05 章 p15「落地劇本」的作法：
> 先為自己的業務部署一個月，把自己的數據變成第一個案例研究。

---

## Client Pitch（成交話術）

**英文原文（ch05_p07 / apxF_p11）：**

> "Every customer query answered in minutes across every channel...
> Your customers get a faster experience. Your team gets their week back."

**繁體中文翻譯：**

> 「每一則客戶詢問，在每一個渠道，都在幾分鐘內得到回覆⋯⋯
> 您的客戶得到更快的體驗，您的團隊拿回他們的一整週。」

**接續問句（依對象調整）**

- 給電商老闆：「您上一則週末進來的客訴，是星期幾才被看到的？」
- 給客服主管：「同一個問題，您團隊三個人會給出三種答案嗎？」
- 給重視風險的決策者：「這套**不會**自動處理任何一筆退款。它只會確保那筆退款在兩分鐘內
  出現在值班人員眼前，而不是明天早上。」

---

## 使用方式

```bash
# 離線跑完整流程（零憑證、零網路）
python main.py --mock

# 把升級卡片推到 Telegram
python main.py --mock --notify telegram

# 只跑流程不發送、也不寫入去重狀態（可重複執行）
python main.py --mock --dry-run

# 串接真實渠道（缺憑證會明確報錯，不會靜默退回 mock）
python main.py --live --notify telegram

# 測試
python -m pytest test_main.py -v
```

| 旗標 | 說明 |
| --- | --- |
| `--mock` | 離線模式（預設），讀 `mock/*.json` |
| `--live` | 串真實渠道 API，需要各渠道的 token 環境變數 |
| `--dry-run` | 跑完流程但不發送、不寫狀態檔（重複執行請加這個） |
| `--notify` | `console`（預設）/ `telegram` / `gmail` / `line` / `whatsapp` |
| `--config` | 設定檔路徑，預設同目錄 `config.yaml` |
| `--state-file` | 去重狀態檔路徑，預設同目錄 `.handled.json`（測試會指向暫存目錄） |

**live 模式需要的環境變數**（缺哪個就報哪個，不給預設值）：
`GMAIL_ACCESS_TOKEN`、`INTERCOM_ACCESS_TOKEN`、`WHATSAPP_ACCESS_TOKEN`、
`INSTAGRAM_ACCESS_TOKEN`、`SHOPIFY_ADMIN_TOKEN`、`ANTHROPIC_API_KEY`，
以及所選通知管道的憑證（如 `TELEGRAM_BOT_TOKEN`）。

---

## 設定重點（`config.yaml`）

| 欄位 | 預設 | 說明 |
| --- | --- | --- |
| `safety.refund_dispute_do_not_process` | `true` | **改不掉**，設 false 會被強制覆寫並發琥珀 |
| `safety.never_claim_human` | `true` | **改不掉**，同上 |
| `persona.ai_disclosure` | 見設定檔 | 每則對外訊息強制附加的 AI 身分揭露句 |
| `persona.human_impersonation_phrases` | 11 條 | 命中即取消自動回覆並升級真人 |
| `persona.holding_response_template` | 見設定檔 | 退款爭議專用；只確認收到，不談金額與責任 |
| `triage.response_target_minutes` | `2` | 書中承諾的 FAQ 回應時限 |
| `triage.reply_max_words` | `100` | 回覆本文字數上限（身分揭露句不計入） |
| `triage.refund_amount_threshold` | `3000` | 達此金額的爭議升為 HIGH；**不影響是否自動回覆** |
| `triage.order_id_pattern` | `\b(BL-\d{5})\b` | 訂單編號正規表達式，第一個捕捉群組即編號 |
| `triage.vip_contacts` | 2 筆 | 命中者一律高優先升級，即使問題本身很單純 |
| `knowledge_base.match_min_score` | `2` | 至少命中幾個關鍵字才算比對成功；未達門檻不猜答案 |
| `runtime.autonomy` | `draft` | 調到 `supervised_auto` 才會真的自動送出（見上方說明） |

---

## 已知限制（預期管理，先講清楚）

| 限制 | 說明 |
| --- | --- |
| 排程不在本模組內 | 程式只負責「跑一輪」。即時性由外部 cron / webhook 觸發，這樣重試與監控都能交給既有基礎設施 |
| 知識庫是關鍵字比對，不是語意搜尋 | 換一種說法問同一件事可能命中不到而升級真人。這是刻意的取捨：**命中規則必須能被主管用肉眼複查**。代價是知識庫要持續補關鍵字 |
| 「2 分鐘」是產出草稿的時間 | 實際送出仍受自主權限制。預設 `draft` 下客戶不會收到任何自動訊息 |
| 委婉的退款請求可能漏掉 | 「我想我可能不需要這組了」不含任何退款關鍵字。`intent_advisory` 提示詞已要求 LLM 標記這類盲點，但它只記錄不路由——需要人週期性回顧後補進關鍵字清單 |
| 多語言未處理 | 關鍵字清單目前只有中文與少量英文。其他語系的客戶訊息多半會落到 `unknown` 而升級真人（安全但沒效率） |
| 查無訂單時不重試 | 訂單編號打錯或訂單屬於其他系統時直接升級真人，不做模糊比對——猜錯訂單等於洩漏其他客戶的資訊 |
| 去重靠 `channel:message_id` | 客戶在同一渠道用兩個 thread 問同一件事，會被當成兩則處理 |
| 狀態檔損毀 | `.handled.json` 讀取失敗時視為首次執行，代價是重覆處理一輪；反過來「靜默當成已處理」會讓真的客訴消失，那是不可接受的失敗方向 |

---

## 檔案結構

```
demo14-support-triage/
├── README.md                     # 本檔
├── config.yaml                   # 渠道、人格、分流訊號、知識庫、安全鐵律
├── main.py                       # 主流程：鐵律強制 + 分流 + 雙路徑發送 + 結果彙整
├── triage.py                     # 決策樹核心：抓取、去重、意圖判定、回覆與升級文案
├── prompts/
│   ├── persona.md                # 客服人格 + never_claim_human + DO NOT PROCESS 鐵律
│   └── intent_advisory.md        # 意圖判定顧問（只記錄，不改變路由）
├── mock/
│   ├── messages_gmail.json       # 4 則（FAQ ×2、訂單 ×1、退款+法務 ×1）
│   ├── messages_intercom.json    # 3 則（FAQ ×1、訂單 ×1、客訴 ×1）
│   ├── messages_whatsapp.json    # 2 則（訂單 ×1、退款 ×1）
│   ├── messages_instagram.json   # 2 則（VIP ×1、無法分類 ×1）
│   ├── knowledge_base.json       # 33 條已審核問答（書中規格 30-50 條）
│   └── orders.json               # 5 筆訂單（Shopify Admin API 的離線替身）
└── test_main.py                  # 3 個測試（happy / edge / integration）
```
