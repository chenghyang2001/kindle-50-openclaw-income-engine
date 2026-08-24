# 模組 #22 — 全漏斗業務自動化（Full Sales Pipeline Automation）

> **交易不是輸給競爭對手，是輸給「會議之間的沉默」。**
> Level 3 企業級｜部署 **1 週**｜售價 **$4,500 setup + $2,000/mo**
> 來源：第07章 ch07_p05、附錄G apxG_p06–p07

---

## 〇、數字來源與三處必要聲明

在往下讀之前，先把三件會被誤讀的事講清楚。

### 1. 定價以「模組頁」為準

| 來源 | 建置費 | 月費 |
| --- | --- | --- |
| **第07章 Automation Matrix（ch07_p03）+ 附錄G 模組頁（apxG_p06）** | **$4,500** | **$2,000/mo** |
| 附錄G Commercial Matrix（apxG_p20） | $8,000–$10,000 | $4,000–$6,000 |

apxG_p20 把 **#21–#30 十個模組全部**列成同一組 `$8,000-$10,000` + `$4,000-$6,000` +
`Deploy in 1-2 weeks`——**十欄同值，是模板佔位**，不是逐案報價。
本模組的 `config.yaml` 採用模組頁的 $4,500 / $2,000，Commercial Matrix 只當作
**銷售話術用的價格帶**（見第七節）。

### 2. 「內部回收」＝（原簡報未提供）

36 張投影片中**沒有任何一頁**提供「顧問自己的內部建置工時回收」數字。
本模組不推估、不換算、不借用其他模組的數字。
`config.yaml` 的 `recovered_hours_per_month` 一律寫 **`null`**（不是 `0`）。
`0` 是一個合法數值，任何沒讀註解的下游程式都會把它當成「回收 0 小時」照算下去——
那就是用預設值靜默掩蓋遺失。`null` 會逼下游在使用前先判空。

### 3. 本頁所有 ROI 數字都是「客戶端節省」

`交易週期縮短 30-50%`、`轉換率 +8-12%`、`業務行政時間 -60%`、`$380,000 管線價值`
——這四個數字全部是**客戶企業**的成效，不是服務商的收入或省時。
兩者混用是 Level 3 提案最常見的失真來源，本模組在報表與 README 都標清楚。

---

## 一、四階段漏斗鏈（apxG_p06）

本模組不是一個工具，而是一個**編排者（Pipeline Orchestrator）**：
它自己不寫信、不算分數，而是依 CRM 即時狀態決定「現在該叫哪一條既有自動化鏈路」。

```
   CRM Stage Change Event (Webhook)          Cron Velocity Check
                 │                                    │
                 └──────────────┬─────────────────────┘
                                ▼
                   ┌────────────────────────┐
                   │  Pipeline Orchestrator │  stage_map 路由 + SLA 監控
                   └───────────┬────────────┘
       ┌───────────────┬───────┴───────┬───────────────┬──────────────┐
       ▼               ▼               ▼               ▼              ▼
 lead_captured     discovery      proposal_sent    closed_won    closed_lost
       │               │               │               │              │
  Enrichment       Proposal        Follow-Up       Onboarding    90-day
    (#12)           Engine          Sequence         Chain       Re-Nurture
                    (#15)            (#10)           (#13)        （3 封）
  SLA <2 小時      SLA <2 小時     5-touch          Day 0-30      Day 30/60/90
  評分+寫回 CRM    提案綱要        halt_on_reply    歡迎包        不推銷
```

| 階段 | 鏈路 | 錨點 | 節點 | 硬性門檻 |
| --- | --- | --- | --- | --- |
| `lead_captured` | Lead Enrichment (#12) | `stage_entered_at` | Day 0 | **SLA < 2 小時** |
| `discovery` | Proposal Engine (#15) | `discovery_call_at` | Day 0 | **SLA < 2 小時**（通話後 2 小時內出提案） |
| `proposal_sent` | Follow-Up Sequence (#10) | `proposal_sent_at` | Day 2 / 5 / 9 / 14 / 21 | **`halt_on_reply`** |
| `closed_won` | Onboarding Chain (#13) | `closed_at` | Day 0（含 30 天節奏） | — |
| `closed_lost` | 90-Day Re-Nurture | `closed_at` | Day 30 / 60 / 90 | **`halt_on_reply`**、前兩封禁 CTA |

---

## 二、Before / After

| | **Before（人工管線）** | **After（Orchestrator）** |
| --- | --- | --- |
| 名單進站 | 業務有空才去查這家公司是誰 | **2 小時內**自動擴充 + 評分 + 寫回 CRM |
| CRM 資料 | 記錄不全，靠業務員記憶 | 通話紀錄自動更新 CRM 欄位 |
| 提案撰寫 | **2-4 天**（常常是「等我這週忙完」） | **2 小時內**產出可簽署的提案綱要 |
| 提案後 | 記得就跟進，不記得就算了 | 自動啟動 **5 節點**追蹤序列 |
| 客戶回覆時 | 忘記關掉排程，又寄一封 → 尷尬 | **偵測到回覆，整條序列立即中止** |
| 成交後 | 空白期，客戶開始懷疑自己的決定 | Day 0 歡迎包 + 30 天啟動節奏 |
| 輸掉的案子 | 標記 Closed-Lost 就再也沒下文 | 90 天重新培育（3 封，不推銷） |
| 漏斗可見度 | 週會時業務口頭報告 | 階段計數 + 在途管線值 + SLA 逾時清單 |
| **結案率** | **14%** | **22%**（Priya Nair 案例預估） |

---

## 三、三條不可協商的安全規則

Level 3 模組直接改寫客戶的 CRM、寄出提案、啟動序列。失敗的代價不是「少省了幾小時」，
而是客戶關係與資料完整性。以下三條在程式層強制，不由設定檔決定。

### 3-1. `halt_on_reply`：追蹤序列不可停用的回覆中止

失敗代價是**不對稱**的：

| 失敗方向 | 後果 | 可逆性 |
| --- | --- | --- |
| 少發一封追蹤 | 損失一次接觸機會 | 可逆（下次補寄即可） |
| 發給已回覆的客戶 | 「我明明回你了」「這是機器人吧」 | **不可逆**（信任破壞無法收回） |

因此本模組比照 demo10 採「**建構子強制覆寫**」：

- `config.yaml` 把 `chains.follow_up.halt_on_reply` 或 `chains.renurture.halt_on_reply`
  設為 `false`，**不會生效**
- 程式強制覆寫為 `true`，透過 `Diagnostics.amber()` 發出警示，寫進 `warnings`，
  並記入 JSONL 稽核軌跡（`action: safety_override`）

**兩道獨立閘門**（缺一不可）：

```
排程判定                              實際送出前
────────                              ──────────
pipeline.plan()                       pipeline.assert_can_send()
  → has_replied() 檢查                  → has_replied() 再檢查一次
  → 判定 Day N 是否到期
                    ↓ 中間隔著 LLM 生成時間（數十秒到數十分鐘）
                      客戶完全可能在這段空窗回信
```

回覆判定採**寬鬆**策略——`has_replied` / `replied_at` / `reply_status == "replied"`
任一成立即中止。寧可少發一封，也不要誤發。

> `chains.onboarding.halt_on_reply` 預設是 `false`，這是刻意的：
> 簽約後的交付流程不是追單，不該因為客戶回過信就停掉歡迎包。

### 3-2. SLA `< 2 小時`：超時要「叫」，不可靜默

`apxG_p07` 對 Enrichment 給的是硬性門檻 `<2小時`。**靜默的 SLA 等於沒有 SLA**，
因此超時同時反映在三個地方，任何一處都不允許被吞掉：

1. `Diagnostics.amber()` → stderr 警示 + `amber_count` 遞增
2. 執行結果的 `sla_breaches` 清單（含逾時分鐘數、截止時間、負責鏈路）
3. JSONL 稽核軌跡（`action: sla_breach`）

SLA 只對「**還沒完成**」的節點計算——已經擴充完的名單不會因為放了三天而一直告警。

### 3-3. 全域安全閥：對外呼叫前必經 `--dry-run` 內部通訊測試

`apxG_p03` 明文要求「所有 API 呼叫前必經 `--dry-run` 內部通訊測試」。本模組把它做成
**可驗證的閘門**，而不是一句文件提醒：

```
python main.py --live --dry-run      # 跑完整流程，印出將呼叫哪些端點與內容，不送出
                                     #   → 在 state file 寫下「設定指紋 + 時間」的收據
python main.py --live                # 啟動前查收據；查不到 → RED 中止
```

指紋只涵蓋 `integrations` 與 `pipeline` 兩個區段：改通知文案不必重跑通訊測試，
但**改端點或階段路由就必須重跑**——否則舊收據等於在替另一份設定背書。

#### 四種模式組合：誰會被呼叫、誰要付錢

`--dry-run` 擋住的是**業務系統端點**，不是 LLM。`--live --dry-run` 下內容是**真的生成**的
（這正是預覽的價值——換成 fixture 就失去意義）。LLM 走本機 `claude` CLI／Max 訂閱 OAuth，
不吃 `ANTHROPIC_API_KEY`、不額外計 API 費用：

| 組合 | LLM 內容生成 | 業務系統送出（CRM / 提案 / 外寄 / 導入） | 需要金鑰 | 成本 |
| --- | --- | --- | --- | --- |
| `--mock` | ❌ 離線 mock 佔位字串 | ❌ 不送出（自主權預設 DRAFT） | 不需要 | **零** |
| `--mock --dry-run` | ❌ 離線 mock 佔位字串 | ❌ 不送出，只印出端點與內容 | 不需要 | **零** |
| `--live --dry-run` | ✅ **實際呼叫本機 claude CLI** | ❌ 不送出，只印出端點與內容 | 不需要（走 Max 訂閱 OAuth） | 計入 Max 訂閱額度 |
| `--live` | ✅ 實際呼叫 | ✅ 依自主權層級實際送出 | `CRM_API_TOKEN` | 計入 Max 訂閱額度 |

- 要**完全零外部呼叫**做流程驗證 → `--mock --dry-run`
- 要**看真實生成內容**但不動客戶系統 → `--live --dry-run`（消耗 Max 訂閱額度，程式會在輸出末尾明講）
- 程式印出的提示會依模式區分這兩類，不會做「不會實際送出」這種涵蓋 LLM 的絕對宣稱

---

## 四、階段狀態機：非法轉移必須擋下

全漏斗自動化最容易被忽略的風險不是產文品質，而是**壞事件**。
CRM webhook 會重送、會亂序，也會因為有人在後台手滑而送出不該發生的轉移。

### 合法轉移表

```
lead_captured ──▶ discovery ──▶ proposal_sent ──▶ closed_won（終態）
      │               │                │
      └───────────────┴────────────────┴──────▶ closed_lost ──▶ lead_captured
                                                （90 天重新培育後可回漏斗頂端）
```

`discovery → closed_won`（跳過提案）這種轉移會被 `IllegalTransitionError` 擋下。

### 進站條件（entry conditions）

轉移合法**還不夠**，還要通過目標階段的必要欄位檢查，避免「階段是 `proposal_sent`
但根本沒有提案」這種資料在下游炸開：

| 階段 | 必要欄位 |
| --- | --- |
| `lead_captured` | `email`、`source` |
| `discovery` | `enrichment.score` |
| `proposal_sent` | `proposal_id`、`proposal_sent_at` |
| `closed_won` | `proposal_id`、`closed_at` |
| `closed_lost` | `closed_at` |

### 四種拒絕原因

| 代碼 | 意義 |
| --- | --- |
| `unknown_deal` | 事件指向不存在的交易 |
| `stale_event` | 事件宣稱的來源階段與 CRM 現況不符（重送或亂序） |
| `illegal_transition` | 轉移本身不合法 |
| `entry_conditions_unmet` | 轉移合法但目標階段的必要欄位不齊 |

**一筆壞事件不會讓整條管線停擺**：被拒事件記入 `rejected_events`、發 AMBER、
寫入稽核軌跡，其餘交易照常處理。交易維持在原階段，等待人工釐清。

---

## 五、稽核軌跡（JSONL）

模組目錄下的 `audit/pipeline_audit.jsonl`（可用 `--audit-file` 改路徑），一行一筆：

```json
{"timestamp":"2026-08-24T09:00:00+08:00","action":"sla_breach","subject":"D-2202",
 "rationale":"Lead Enrichment (#12) SLA 120 分鐘已超時 180 分鐘","is_human_approved":false,
 "module":"demo22-sales-pipeline","run_id":"...","is_dry_run":false,"detail":{...}}
```

| 欄位 | 意義 |
| --- | --- |
| `timestamp` | 事件時間（ISO 8601，含時區） |
| `action` | 動作代碼（穩定字串，供 grep / 匯入 BI） |
| `subject` | 對象（交易 ID、事件 ID 或設定指紋） |
| `rationale` | **決策依據**——稽核軌跡的價值全在這一欄，空值直接拒絕寫入 |
| `is_human_approved` | 是否已取得人工核准（白名單命中＝事前核准） |

動作代碼：`run_started` / `event_rejected` / `sla_breach` / `safety_override` /
`chain_drafted` / `chain_executed` / `chain_halted` / `dry_run_receipt` / `run_completed`

**刻意不寫入的東西**：信件與提案正文一律不落地，只記字數與 SHA256 前 16 碼。
稽核軌跡的保存期通常遠長於客戶通訊內容的保存政策，把正文寫進去等於把合規風險寫進 log。

---

## 六、Financial Model

### 客戶端（買這個模組的人）

| 項目 | 數字 | 來源 |
| --- | --- | --- |
| 一次性建置 | **$4,500** | ch07_p03 / apxG_p06 |
| 每月訂閱 | **$2,000** | apxG_p06 |
| 交易週期縮短 | **30-50%** | apxG_p07 ROI Dashboard |
| 轉換率提升 | **+8-12%** | apxG_p07 ROI Dashboard |
| 業務行政時間 | **-60%** | apxG_p07 ROI Dashboard |
| 年度管線價值增量 | **+$380,000** | Priya Nair 案例 |

**Priya Nair 案例的算法**（14% → 22%）：

| | 結案率 | 年度管線價值增量 |
| --- | --- | --- |
| Before | 14% | — |
| After | 22% | — |
| **差額** | **+8pp** | **+$380,000** |

以 $2,000/月（年 $24,000）換 **$380,000** 的年度管線價值增量 ——
這是本模組在提案桌上最有力的一組數字，且它**完全是客戶端的收益**。

### 服務商端（賣這個模組的人）

| 項目 | 數字 |
| --- | --- |
| 部署時間 | **1 週** |
| 首次交付收入 | $4,500 |
| 每客戶經常性收入 | $2,000/mo |
| 10 個客戶 | **$45,000 一次性 + $20,000/mo** |
| **內部回收工時** | **（原簡報未提供）** |

> 為什麼服務商端沒有 ROI 倍數：本模組的內部工時回收數字原簡報未提供，
> 硬算出來的倍數會是憑空捏造。要對客戶說的是他們的 $380,000，不是我們的想像。

---

## 七、客戶見證

> 「預計將結案率從 **14%** 提升至 **22%**，相當於為企業增加 **$380,000** 的年度銷售管線價值。」
>
> — **Priya Nair**（apxG_p06 / ch07_p05）

> 附錄G 本模組頁以「商業價值（Business Value）」取代 Client Pitch 欄位，
> 內容即上方 Priya Nair 案例。除此之外，原簡報未提供其他客戶見證，故不補充。

---

## 八、Client Pitch（銷售話術）

**開場（價值框架，不談功能）**

> 「您現在的問題不是業務不夠努力，是交易死在**會議與會議之間的沉默**裡。
> 提案要 2-4 天才寫得出來，寫完之後沒人記得跟進。我們把整條漏斗接起來：
> 名單進站 2 小時內完成評分、通話結束 2 小時內出提案、提案寄出後自動跑 5 節點追蹤——
> 而客戶一回信，整條序列立刻停。」

**價格帶話術（apxG_p20 的正確用法）**

Commercial Matrix 的 `$8,000-$10,000` 是**價格帶錨點**，用來讓 $4,500 顯得合理，
不是本模組的報價。實際報價：**$4,500 setup + $2,000/mo**。

**現場對答**

| 客戶疑慮 | 回應 |
| --- | --- |
| 「$2,000/月會不會太貴？」 | 您取代的不是一套軟體，是業務行政的 60% 工時。而 Priya Nair 的案子是 14% → 22%，年度管線價值多了 $380,000。$24,000 換 $380,000，這是提案裡最容易的一頁。 |
| 「AI 會不會亂發信給我的客戶？」 | 預設是草稿模式，全部要您過目才送出。要開自動也必須先跑滿 14 天草稿期、配好白名單，未命中白名單的一律降級成草稿。 |
| 「客戶已經回我了，系統還會繼續寄嗎？」 | 不會，而且這條規則**設定檔關不掉**。系統在每一封信送出前都會再查一次，一偵測到回覆整條序列立即中止。 |
| 「CRM 資料很亂，會不會被搞爛？」 | 每個階段都有進站條件，欄位不齊就不准進站；非法的階段轉移（例如跳過提案直接成交）會被擋下並留下稽核紀錄，不會靜默寫進去。 |
| 「出事了怎麼查？」 | 每一個動作都有 JSONL 稽核軌跡：時間、動作、對象、**決策依據**、是否人工核准。您可以直接匯進 BI。 |
| 「上線前怎麼知道不會出包？」 | 所有對外呼叫前必須先跑 `--dry-run` 內部通訊測試，把要呼叫的端點與內容全印出來給您看過。沒有這張收據，正式模式根本啟動不了。 |

---

## 九、使用方式

```bash
# 離線跑完整流程（零憑證、零網路）
python main.py --mock

# 跑完流程但完全不發送，並印出將呼叫哪些端點與內容（內部通訊測試）
python main.py --mock --dry-run

# 指定狀態檔與稽核檔位置（企業環境常需要寫到共用磁碟）
python main.py --mock --state-file ./state/pipeline_state.json \
                     --audit-file ./audit/pipeline_audit.jsonl

# 推到 Telegram
python main.py --mock --notify telegram

# 串真實 API（缺 CRM_API_TOKEN 會明確報錯，不會靜默降級）
python main.py --live --dry-run     # 必須先跑這一次，取得通行收據
                                    #   注意：此組合的 LLM 內容生成會實際呼叫 API（計費）
python main.py --live

# 測試
python -m pytest test_main.py -v
```

### Mock 資料說明（`mock/deals.json`，10 筆交易 + `mock/crm_events.json`，3 則事件）

| ID | 情境 | 預期結果 |
| --- | --- | --- |
| D-2201 | 名單進站 30 分鐘 | Enrichment 草稿（SLA 內） |
| D-2202 | 名單進站 **5 小時**未擴充 | Enrichment 草稿 + **SLA 超時警報**（逾時 180 分） |
| D-2203 | 已擴充，事件推進到 `discovery` | Proposal 草稿（SLA 內） |
| D-2204 | 通話後 **4 小時**無提案 | Proposal 草稿 + **SLA 超時警報** |
| D-2205 | 提案寄出 3 天未回覆 | 5-touch 的 **Day 2** 草稿 |
| D-2206 | **已回覆**（階段仍是 `proposal_sent`） | **中止**（`replied`）— 安全機制主測案例 |
| D-2207 | 5-touch 全部送完 | 中止（`sequence_complete`） |
| D-2208 | 事件推進到 `closed_won` | Onboarding 歡迎包（不受 `halt_on_reply` 影響） |
| D-2209 | Closed-Lost 已 35 天 | 90 天重新培育 **Day 30** 草稿 |
| D-2210 | 事件想從 `discovery` **直跳 `closed_won`** | **事件被拒**（`illegal_transition`）+ 交易留在 `discovery` 照常出提案 |

`--mock` 的基準時間固定在 `config.yaml` 的 `mock.today`，因此每次執行結果完全相同，
狀態檔也不落地（非 dry-run 時），QA 可重複驗證。

---

## 十、檔案結構

```
demo22-sales-pipeline/
├── README.md              # 本檔
├── config.yaml            # stage_map、五條鏈路、SLA、安全開關、自主權
├── main.py                # 主流程（--mock / --live / --dry-run / --notify
│                          #        / --state-file / --audit-file）
├── pipeline.py            # 階段狀態機 + SLA 監控 + 鏈路排程 + 發送前複查
├── audit.py               # JSONL 稽核軌跡（決策依據 + 人工核准欄位）
├── prompts/
│   ├── enrichment_scoring.md   # Lead Enrichment (#12)
│   ├── proposal_brief.md       # Proposal Engine (#15)
│   ├── followup_touch.md       # 5-touch Follow-Up (#10)
│   ├── onboarding_welcome.md   # Onboarding Chain (#13)
│   └── renurture_letter.md     # 90-Day Re-Nurture
├── mock/
│   ├── deals.json         # 10 筆交易，涵蓋五條鏈路 + SLA 超時 + 已回覆
│   └── crm_events.json    # 3 則階段變更事件（含 1 則非法轉移）
└── test_main.py           # happy / edge / integration
```

---

## 十一、已知限制

- **不含 CRM 寫回**。正式部署需在 `_execute()` 之後把結果推回 CRM；目前只登記
  「將要呼叫哪個端點」，實際 HTTP 呼叫留給部署階段接上（依契約只用 `urllib.request`）。
- **回覆偵測讀的是快照**。mock 模式讀 `deals.json`，正式環境務必傳入
  `assert_can_send(reply_checker=...)`，改為即時查詢 CRM 或收件匣。
- **Cron Velocity Check 未實作為排程器**。本模組提供的是「跑一次」的判定；
  `stage_map` 觸發源之一的定時速度檢查，需由外部 cron / 排程系統呼叫本程式。
- **管線健康度只做到階段計數與在途管線值**。apxG_p07 提到的 pipeline health
  reporting 若要做到週對週變化，需要保留歷史快照（目前狀態檔只存進度與收據）。
- **提示詞為單一語言版本**。多語系客戶需為每個語言複製一組 `prompts/`
  （`mock/deals.json` 的 D-2208 是日本客戶，正式部署要處理這一點）。
