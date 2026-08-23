# demo23 — 董事會級財務報表自動化（Level 3 · 模組 #23）

> 第 07 章 / 附錄G 模組 #23（Automated Board-Quality Management Accounts /
> 財務報告與預測智能體）。月結 close 之後 **T+1** 交出董事會品質的管理財務報告，
> 業界平均是 **10–14 天**。

| 項目 | 數值 | 來源 |
| --- | --- | --- |
| 部署時間 | 1 週 | ch07_p03 / apxG_p08 |
| 客戶建置費 | **$3,500** | 模組頁（ch07_p03、apxG_p08） |
| 客戶月費 | **$1,500/mo** | 模組頁 |
| 內部回收（顧問自己的工時） | **（原簡報未提供）** | 36 張投影片皆無此數字 |
| 客戶端節省 | 每月省下資深財務 **2–3 天** | apxG_p08 ROI Dashboard |
| 客戶端速度優勢 | 月結後**第 1 天**交付 vs 業界 **10–14 天** | apxG_p08 |
| 決策品質 | 永遠保持 **3 種情境**的滾動預測 | apxG_p08 |

### ⚠️ 定價衝突的處理（Level 3 全域裁決 1）

附錄G 最後的「The Level 3 Commercial Matrix」（apxG_p20）把 #21–#30 **十個模組全部**
列為 `建置報價 $8,000–$10,000` + `月訂閱費 $4,000–$6,000`，明顯是同一組模板佔位值。
本模組一律以**各模組頁的逐案報價**為準（$3,500 + $1,500/mo），
apxG_p20 的價格帶僅視為銷售話術用的區間，不寫進 `config.yaml`。

---

## 1. Before / After

| 階段 | Before（人工） | After（Agent） |
| --- | --- | --- |
| 取數 | 逐一登入 Xero / QuickBooks / Sage，匯出 CSV 再貼進 Excel | 唯讀 API 自動 pull（`accounting.transactions.read` 等） |
| 對預算 | 手工比對預算表，公式常斷 | 依科目代碼自動對應，算出 Variance $ / % |
| 分析 | 資深財務逐項回想成因 | AI 逐條解讀，>5% 強制說明時間差與逆轉時間點 |
| 現金流 | 期末餘額算完就結束 | 自動換算「現金可支應天數」，< 60 天發流動性警報 |
| 預測 | 一份靜態年度預算，季底才更新 | 12 個月 × 3 情境滾動預測，每月更新 |
| 審核 | 口頭 / Email 說「我看過了」 | 核准綁定數字指紋，寫入稽核 JSONL |
| 交付 | 月結後 **10–14 天** | **T+1** 財務總監審核 → **T+3** 董事會發布 |

架構：

```
[唯讀取數]                    [AI 財務長引擎]                [審核閘門]
Xero        ──┐          ┌─ 變異數分析（>5% 標重大）       ┌─ T+1 財務總監審核
QuickBooks  ──┤          ├─ 現金流 + 流動性警報            │   （SLA < 2 小時）
Sage        ──┼─► 聚合 ──┼─ AI 管理階層解讀敘述      ──►  ├─ 未核准 → 草稿浮水印
預算 CSV     ──┤  幣別守衛 ├─ 12 個月 × 3 情境滾動預測      │   董事會零收件
BambooHR    ──┘          └─ 董事會報告四件套               └─ 核准 → T+3 發布
                                                                    │
                                                          [稽核 JSONL 全程留痕]
```

---

## 2. 三條財務鐵律（違反即為重大缺陷）

### 鐵律 1：財務資料源一律唯讀（read scope）

`sources/` 的每個資料源都必須宣告 scope，並在**取數之前**通過
`sources.assert_read_only_scope()`：

| 資料源 | scope（apxG_p08 逐字） |
| --- | --- |
| Xero | `accounting.transactions.read` |
| QuickBooks | `com.intuit.quickbooks.accounting (read)` |
| Sage | `sales_invoices (all read)` |
| 預算檔（`BUDGET_CSV_PATH`） | `local_file.read` |
| BambooHR 薪資 | `employees.payroll.read` |

守衛規則（`sources/__init__.py`）：

1. scope 內出現 `write` / `create` / `update` / `delete` / `post` / `modify` /
   `full` / `admin` / `manage` 任一字樣 → 拋 `ReadOnlyViolation`，**中止整個流程**
   （exit code 2），不是警告、不是降級。
2. scope 未包含 `read` → 同樣拒絕（無法確認唯讀就不取數）。
3. `--live` 的 HTTP 層再擋一次：`fetch_live_json()` 只接受 `GET` / `HEAD`，
   其餘動詞直接 `ReadOnlyViolation`。

**兩道守衛刻意重複**：設定檔寫錯（有人把 write scope 的 token 貼進來）與程式碼寫錯
（未來有人加了一個 POST 呼叫）是兩種不同的錯，各擋各的。
本模組**沒有任何寫入財務系統的程式路徑**——不是「有但沒用到」，是根本不存在。

### 鐵律 2：T+1 財務總監審核才可發送給董事會

- 未核准時，報表一律標示 **「草稿・待財務總監審核」**（`approval.draft_watermark`），
  主旨也帶同樣前綴，且**董事會信箱一封都不會收到**——收件人只剩財務總監。
- 核准動作：`python main.py --mock --approve-as <財務總監信箱>`。
  核准人必須等於 `delivery.fd_email`，不符即拒絕並寫入 `approval_rejected` 稽核事件。
- **核准綁定數字指紋**（`audit.content_fingerprint`，SHA-256 前 16 碼，
  涵蓋整份財務包 + 三情境預測）。任何一個科目金額變動 → 指紋改變 →
  先前核准立即失效（`fingerprint_mismatch`）。否則「先核准一份乾淨的、再換掉數字發出去」
  這條路是敞開的。
- **SLA `< 2 小時`**（apxG_p08）。逾時發琥珀燈並寫入 `approval_sla_breached` 稽核事件，
  逾時期間報表仍維持草稿——逾時的處置是催辦，不是放行。
- 交付時序：T+1 審核 → **T+3** 董事會發布，由期間月底自動推算並印在報表上。

### 鐵律 3：金額一律 `decimal.Decimal`，全檔禁 float，幣別不可混加

- 所有 `mock/*.json` 的金額都以**字串**儲存（`"412500.00"`）。
  `to_decimal()` **主動拒收 float 輸入**並報錯，強迫資料檔守規矩。
- 四捨五入採 `ROUND_HALF_UP`（財務慣例），不用 Decimal 預設的銀行家捨入——
  否則對帳的人會算不出同一個數字。
- **幣別守衛**：`enforce_single_currency()` 在聚合前比對每個資料源的幣別，
  不同幣別的資料源被剔除並標為失敗資料源。**不換算、不相加**——
  匯率是財務政策決定，不是報表程式可以自行假設的東西。

---

## 3. 資料不完整時：比 demo09 醒目數倍的封鎖式警告

demo09（每日銷售報表）的部分失敗設計是標一行「⚠️ 部分資料：Stripe 無回應」照常發出。
**財務報表不能這樣做。** 殘缺的財務數字會被寫進董事會議事錄、拿去做投資決策、
引用到對外揭露；董事會不會因為缺一頁而停止討論，他們會拿手上這幾個數字繼續做決定。

因此本模組在任一資料源失敗時：

```
■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■
⛔ 財務資料不完整 — 本報表不得作為董事會決議或對外揭露依據 ⛔
■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■■
  ⛔ 缺少資料源：QuickBooks｜原因：…
  ⛔ 以下所有金額、變異數與滾動預測皆建立在殘缺基礎上。
  ⛔ 既有的財務總監核准已自動作廢，補齊資料後必須重新審核。
```

1. 橫幅放在**任何一個金額之前**（讀者在看到數字前就知道不能用）。
2. **自動作廢既有核准**（`approval_invalidated` / `partial_data`）——
   財務總監核准的是「那一份完整的數字」，不是這一份殘缺的。
3. 主旨加上 `⛔資料不完整`，手機通知列被截斷也讀得到。
4. 報表照樣產出（維運要看得到缺什麼），但董事會拿不到。

---

## 4. 三情境 12 個月滾動預測（apxG_p09 參數逐字實作）

| 情境 | `pipeline_conversion` | `cost_assumption` |
| --- | --- | --- |
| Base | **1.0** | **flat** |
| Upside | **1.2** | **controlled_growth** |
| Downside | **0.8** | **cost_reduction** |

模型（刻意保持可口頭複述——董事會不會相信講不清楚的預測）：

```
當月營收 = 月經常性營收(MRR) + (加權管道 ÷ 預測月數) × pipeline_conversion
當月成本 = 本期實際成本 × (1 + 月成本增減率) ^ 月序
當月獲利 = 當月營收 - 當月成本
月末現金 = 期末現金餘額 + 累計獲利
```

> ⚠️ **簡報只給了 `cost_assumption` 的標籤，沒有給對應的月成本增減率。**
> `forecast.cost_growth` 的三個數字（flat `0.000`、controlled_growth `+0.015`、
> cost_reduction `-0.020`）是**本實作定義的預設假設**，不是簡報數字，
> 導入時應由該公司財務總監覆寫。這裡明確標示，不冒充成原始資料。

新業務基礎用**加權管道**而非總管道：總管道含大量不會成交的案子，
直接乘轉換率會讓三個情境全部樂觀，downside 情境會比實際的 base 還好看——等於沒有 downside。

---

## 5. 稽核軌跡（JSONL + 雜湊鏈，模組目錄下）

`audit.py` 自行實作（**未改動 `_shared/`**：保存財務核准鏈是本模組專屬需求，
塞進共用層會讓其他 9 個模組背上用不到的依賴）。

| 事件代碼 | 意義 |
| --- | --- |
| `run_started` / `run_finished` | 本次執行起訖（含模式、dry-run） |
| `dry_run_selftest` | 全域安全閥的內部通訊自檢結果 |
| `read_only_scope_verified` / `read_only_scope_violation` | 唯讀 scope 驗證 |
| `source_read` / `source_read_failed` | 每個資料源的取數結果 |
| `board_pack_generated` | 財務包產出（含指紋、重大變異數數量、警示） |
| `approval_requested` | 送審（SLA 起算點） |
| `approval_granted` / `approval_rejected` | 核准 / 核准人不符被拒 |
| `approval_invalidated` | 核准失效（資料不完整或指紋不符） |
| `approval_sla_breached` | 審核逾時 2 小時 |
| `board_dispatch` / `board_dispatch_blocked` | 對董事會發布 / 被閘門擋下 |

寫入失敗一律拋 `AuditError` 並以 exit code 3 中止：**沒有稽核軌跡就不准發報表**。
無法解析的行同樣拋錯，不靜默略過——把一筆紀錄改成垃圾就能讓它消失，等同偽造完整性。

### 雜湊鏈：為什麼「附加寫入」還不夠

鐵律 2 的核准綁定內容指紋鎖住了「核准的是哪一份數字」，但**核准紀錄本身**若只是
純附加的 JSONL，能碰到檔案的人可以把 `approval_granted` 的 `approved_by` 從 A 改成 B，
或把時間往前挪，讓一份逾時的核准看起來合規。前門鎖了，後門沒鎖。

因此每一筆都帶 `seq` / `prev_hash` / `entry_hash`：

```
entry_hash = sha256(prev_hash + 本筆內容的正規化 JSON)
```

| 攻擊 | 偵測方式 |
| --- | --- |
| 改掉某筆的時間戳 / 核准人 / detail | 該筆 `entry_hash` 與內容不符 |
| 整行刪掉，讓事件消失 | 下一筆的 `prev_hash` 接不上前一筆 |
| 把某筆改成無法解析的垃圾 | `read_rows()` 直接拋 `AuditError` 並指出行號 |

介面：

- `AuditLog.verify_chain()`：驗證**記憶體中**本次執行寫入的鏈（證明程式自己產出的鏈接得上）。
- `verify_file(path)` / `AuditLog.verify_file()`：**重讀磁碟**驗證整條鏈，訊息一律帶行號。
- `first_broken_line(path)`：直接回傳**第一個斷鏈的行號**，稽核人員可立即定位竄改位置。
- 開檔時續接既有檔案最後一筆的 `seq` 與 `entry_hash`，讓鏈跨執行累積
  （每次執行重新從 GENESIS 起算等於自己剪斷鏈）。
- 正規化時 `Decimal` 一律轉字串，**寫入與驗證共用同一個 `normalize()`**——
  兩邊用不同方式轉會產生「假斷點」，稽核人員將無法分辨真竄改與假警報。

擋不住「整份檔案被換掉」（那需要外部 WORM 儲存或簽章服務），但財務稽核的真實威脅
模型是內部人的小幅竄改，不是重建整條鏈。

預設路徑 `audit/audit-log.jsonl`、`state/approval.json`（皆已在 repo `.gitignore` 中），
可用 `--audit-file` / `--state-file`（或環境變數 `OPENCLAW_AUDIT_LOG`）覆寫，
多客戶部署時各自獨立。

---

## 6. 全域安全閥：對外 API 呼叫前的 `--dry-run` 內部通訊測試

apxG_p03 的企業級硬性要求：「所有 API 呼叫前必經 `--dry-run` 內部通訊測試」。
`main.selftest()` 在任何取數之前檢查三件事，並寫入稽核：

1. **所有資料源 scope 為唯讀** → 違規即紅色警報中止（任何模式都適用）。
2. **收件人設定完整**（`fd_email` / `board_emails` 皆為合法信箱）→ 避免誤寄。
3. **`--live` 憑證到位**（`live.required_env` 逐一檢查）→ 缺任何一個都退出，
   不會靜默退回 mock。

---

## 7. Financial Model

### 客戶端（單一客戶，12 個月）

| 項目 | 金額 |
| --- | --- |
| 建置費（一次） | $3,500 |
| 月費 × 12 | $18,000 |
| **客戶總支出** | **$21,500** |

節省的量化基礎只有簡報給的「每月省下資深財務 **2–3 天**」。
**日費率簡報未提供**，以下兩組是**本實作的假設值**（非簡報數字），供業務對話時替換：

| 資深財務日成本（假設） | 年節省（24–36 天） | 對比 $21,500 |
| --- | --- | --- |
| $600/天 | $14,400 – $21,600 | 約打平 |
| $900/天 | $21,600 – $32,400 | 淨效益 $100 – $10,900 |

> **不要只賣工時**。這個模組的真正價值在簡報寫得很清楚：
> 「為董事會每月爭取到額外 **8 天**的黃金決策期」，以及永遠保持 3 種情境的滾動預測。
> 8 天決策期的金額價值**簡報未提供**，本文件不推估。

### 服務商端

| 客戶數 | 一次性建置收入 | MRR | 年營收 |
| --- | --- | --- | --- |
| 5 | $17,500 | $7,500 | $107,500 |
| 10 | $35,000 | $15,000 | $215,000 |
| 20 | $70,000 | $30,000 | $430,000 |

**內部回收（顧問自己的建置工時回收）：（原簡報未提供）。** 不推估。

---

## 8. 客戶見證

**（原簡報未提供具名見證。）**

ch07_p06 / apxG_p08 只提供了「真實情境」數據：為董事會每月爭取到額外 **8 天**的
黃金決策期。本文件不編造見證人、公司名或引述。

---

## 9. Client Pitch（銷售話術）

> **「讓您的董事會根據『當前數據』做決策，而不是『上個月的回憶』。」**
> （ch07_p06 逐字）

三個支撐點：

1. **速度**：月結後第 1 天拿到董事會品質的財務包，業界平均 10–14 天。
   等於每月多出 **8 天**可以據以行動的時間。
2. **深度**：不是把數字念一遍，而是逐條解讀變異數；>5% 強制說明是時間差還是永久性差異，
   時間差必須指出逆轉月份。
3. **安全**：所有會計系統唯讀、財務總監核准前董事會拿不到任何東西、
   每一個動作都有稽核軌跡。**這一段通常是財務長最在意的，卻是最少人講的。**

---

## 10. 執行方式

```bash
# 零憑證、零網路，讀 mock/*.json 跑完整流程（產出「草稿・待財務總監審核」）
python main.py --mock

# 模擬財務總監核准後再跑一次（核准人必須等於 config 的 delivery.fd_email）
python main.py --mock --approve-as fd@example.com

# 跑完流程但不發送、也不寫入核准狀態
python main.py --mock --dry-run

# 指定核准狀態檔與稽核檔（多客戶部署 / 測試環境）
python main.py --mock --state-file ./state/acme.json --audit-file ./audit/acme.jsonl

# 推到 Telegram
python main.py --mock --notify telegram

# 串真實唯讀 API（缺憑證會列出缺哪些變數並退出，不會靜默退回 mock）
python main.py --live

# 測試
python -m pytest test_main.py -v
```

Exit code：`0` 正常（草稿也算正常）、`1` 設定或資料錯誤、
`2` **唯讀鐵律違規**、`3` 稽核軌跡不可用。

---

## 11. 檔案結構

```
demo23-financial-reporting/
├── README.md              # 本檔
├── config.yaml            # 模組設定（含三情境參數、SLA、門檻、唯讀 scope）
├── main.py                # CLI、流程編排、審核閘門、發送
├── board_pack.py          # 取數編排、幣別守衛、變異數、現金流、四件套排版
├── forecaster.py          # 12 個月 × 3 情境滾動預測
├── audit.py               # JSONL 稽核軌跡 + 內容指紋
├── sources/
│   ├── __init__.py        # 唯讀守衛、Decimal 工具、資料結構、註冊表
│   ├── xero_source.py     # 損益實際數（含去年同期）
│   ├── quickbooks_source.py  # 現金流量（含恆等式自檢）
│   ├── sage_source.py     # 管道與應收
│   ├── budget_source.py   # 預算（CSV / JSON 雙格式）
│   └── payroll_source.py  # BambooHR 人事成本
├── prompts/
│   ├── executive_summary.md
│   └── variance_narrative.md
├── mock/
│   ├── xero.json / quickbooks.json / sage.json / budget.json / payroll.json
└── test_main.py           # 3 個測試（happy / edge / integration）
```

---

## 12. 技術要點

- **時區用 `zoneinfo`（禁用 pytz）**。Windows 沒有系統時區資料庫，未安裝 `tzdata`
  時 `ZoneInfo("Asia/Taipei")` 會拋 `ZoneInfoNotFoundError`；此時降級為
  `reporting.fallback_utc_offset_hours` 的固定偏移並記琥珀燈（與 demo07 同一套做法）。
  測試一律注入固定時間與固定偏移，結果不隨執行機器改變。
- **現金流恆等式自檢**：期初 + 營業 + 投資 + 融資 ≠ 期末即拒收該資料源。
  串不起來的現金流送進董事會，比沒有現金流更糟。
- **`run()` 回傳鍵名**採用契約 §6「已知技術債」段落建議的六個標準鍵
  （`module_id` / `module_name` / `mode` / `dry_run` / `warnings` / `amber_count`），
  外加本模組專屬的 `board_pack` / `forecast` / `approval` / `fingerprint` / `audit_file`。
- **未動 `_shared/`**：稽核與審核狀態全部留在本模組目錄。
- **只用標準庫 + PyYAML**：HTTP 走 `urllib.request`，CSV 走 `csv`，雜湊走 `hashlib`。

---

## 13. 已知限制

1. **`--live` 只實作了唯讀 HTTP 骨架**（`sources.fetch_live_json`）；
   各家會計 API 的實際端點與分頁邏輯需在導入時依客戶帳套補上。
   這是刻意的：端點會因客戶的地區版本而異，寫死反而誤導。
2. **預測模型刻意簡化**（線性管道攤平 + 等比成本），適合董事會層級的情境對照，
   不適合取代財務團隊的細部預算模型。
3. **變異數分類（時間差 / 永久性 / 單次性）由 LLM 判斷**，
   提示詞已強制「無法判斷就說無法判斷」，但仍應由財務總監在 T+1 審核時覆核——
   這正是審核閘門存在的理由。
4. **多幣別合併報表未支援**：目前的行為是排除非報表幣別的資料源並標為不完整，
   而不是換算。要支援集團合併需要財務政策決定匯率來源（期末匯率 / 平均匯率），
   屬於另一個模組的範圍。
5. **核准是單人制**（財務總監一人）。需要雙簽或審計委員會會簽的客戶，
   要擴充 `approval` 區塊為多人清單——狀態檔格式已預留 `approved_by` 欄位。
