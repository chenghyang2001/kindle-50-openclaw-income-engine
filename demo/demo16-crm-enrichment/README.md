# demo16 — CRM 數據豐富化與評分（CRM Enrichment & Lead Scoring）

> 模組 #16 ｜ Level 2 代理商基礎 ｜ 分類：銷售轉化 / Sales & Conversion
> 部署 1 Day ｜ 內部回收 15 hrs/week ｜ 複雜度：快速配置（< 2 hrs，apxF_p17）

每晚 **2AM** 掃描 CRM：透過外部資料源補齊缺漏欄位、計算 ICP 分數並排序、
撈出 **90 天**沒人聯絡的高分機會，最後產出一份豐富化 CSV 報告與變更計畫。

---

## Before / After

| | Before（人工） | After（Agent） |
| --- | --- | --- |
| 公司規模欄位 | 留白，因為「等一下再查」而永遠留白 | 每晚自動補齊，來源可追溯到是哪個資料庫給的 |
| 資料清理 | 季度大掃除耗費 **3 天**，依然清理不完 | 每晚增量處理；近 30 天內處理過的自動跳過 |
| 業務怎麼決定打給誰 | **隨機挑選**客戶撥打 | 依 ICP 分數排序，最熱的永遠浮在管線最頂端 |
| 久未聯絡的機會 | 沉在名單底部，沒人記得 | 自動標記「135 天未聯絡」並列為 reengagement 目標 |
| 管線預測準確率 | **55%** | **75%+**（書中數據） |
| 外部資料查不到時 | 人工填一個「大概」的值，之後沒人知道那是猜的 | 保留原值、標 `enrichment_failed`，**絕不猜** |
| 外部資料與 CRM 打架時 | 誰後改誰贏 | 保留 CRM 值、外部值進待審清單 |
| 每週耗時 | 約 **15 小時** | 0 小時（每晚 2AM 自動執行） |

---

## 這個模組最重要的一條規則：**只補空格，不覆蓋既有資料**

書中對這個模組的痛點描述是：

> 「充滿過期或錯誤數據的 CRM 比沒有 CRM 更糟。手動輸入數據是任何依賴它的系統中最脆弱的一環。」

一個會自動覆蓋的豐富化代理人，正是製造那種 CRM 最快的方法。
因此 `enricher.py` 只允許四種處置，其中**只有一種會動到資料**：

| 情境 | 處置 | 會寫入嗎 |
| --- | --- | --- |
| CRM 該欄空白，外部有值 | `filled` — 補入，並記下是哪個來源給的 | ✅ 唯一會寫入的情況 |
| CRM 有值，外部值相同 | `confirmed` — 什麼都不做 | ❌ |
| CRM 有值，外部值不同 | `conflict_kept` — **保留 CRM 值**，外部值進人工待審清單 | ❌ |
| 外部查無此欄位 | `no_data` — 保留 CRM 原值 | ❌ |
| 欄位在 `protected_fields` | `protected` — 連空白都不碰（owner / email / lifecycle_stage） | ❌ |

外部**完全**查不到這家公司時，整筆標成 `enrichment_failed`，
所有欄位維持原狀，並在報表與 CSV 上誠實標出來。

### 為什麼衝突不自動採用外部值

mock 資料裡的 **Vellum Legal（C-1004）** 就是真實世界最常見的那種錯：
Apollo 把同名的另一家公司對上了，宣稱它有 **800 人 / $45,000,000 營收 / 產業是 Software**，
而 CRM 裡是業務親自問到的 **40 人 / $6,500,000 / 法律服務**。

- 自動覆蓋的結果：這筆從 **29.3 分（Cool）** 一路跳到 Hot，業務照著錯的名單打電話。
- 本模組的結果：保留 CRM 值、三個衝突欄位進待審清單，並附上「這不是誤差，是誤配」的判讀。

**降級，但絕不假裝；補值，但絕不覆蓋。**

### 空值判定的一個細節

`is_blank()` 把 `""` / `"N/A"` / `"-"` / `"未知"` / 空清單 / `None` 視為空白，
但 **`0` 與 `False` 一律不算空白**。用 `if value` 判斷會把「員工數 0」當成缺值然後蓋掉——
但員工數 0 是需要人去查的荒謬值，不是給機器覆蓋的空格。

---

## 先看再決定：`--dry-run` 的變更計畫

寫回客戶的 CRM 是不可逆動作。第一次上線時沒有人該憑信任按下去，
所以 `--dry-run` 會逐欄印出**哪些欄位、從什麼值變成什麼值、來源是誰**：

```
變更計畫（尚未寫入 CRM）
──────────────────────────────────
  C-1002 Kite Labs
    ✍ industry        （空白） → 「IT Consultancy」｜來源：Companies House
    ✍ employee_count  （空白） → 「60」｜來源：Apollo
  C-1003 Harbour Freight Co：外部查無資料，維持原狀不變更
  C-1004 Vellum Legal
    ⚠ employee_count  保留 CRM 「40」｜Apollo 提供 「800」（不採用）
──────────────────────────────────
合計：14 個欄位待寫入、3 個衝突待人工判斷
```

`--dry-run` 不寫任何檔案（連 CSV 與狀態檔都不寫），純粹讓人看。

---

## 自主權階梯：寫回 CRM 比發信更危險

依第 04 章鐵律，本模組預設 `autonomy: draft`。與其他模組不同的是，
這裡把「寫回 CRM」也交給 `AutonomyGate` 管制（`enrichment.write_target`）：

| 條件 | 行為 |
| --- | --- |
| `--dry-run` | 只印變更計畫，什麼都不寫 |
| `--mock` | 產出 CSV 與狀態檔，但不連線 CRM |
| `autonomy: draft`（預設） | 產出 CSV 與變更計畫，**等人工核可**後才寫 CRM |
| `supervised_auto` + `write_target` 在白名單 + 連續 14 天 | 自動寫回 CRM |

發錯一封信，收件人會告訴你；寫錯一格 CRM，沒有人會發現——直到季度檢討時，
所有人才發現整季的管線預測都建立在錯的數字上。

---

## 評分權重：一個數字都不寫在程式碼裡

`scorer.py` **沒有任何**權重或門檻的常數。全部來自 `config.yaml`：

```yaml
scoring:
  weights:
    industry_match: 30      # SIC 代碼是否在 ICP 白名單
    company_size: 25        # 員工數（低於 min 不加分，達 ideal 給滿分）
    revenue_band: 20        # 年營收級距（全程 Decimal）
    tech_stack: 15          # 技術訊號命中數
    title_seniority: 10     # 職稱層級
  bands: { hot: 75, warm: 50 }   # 書中 SCORE_THRESHOLDS
  stale_days: 90                 # 書中：標記 90 天未聯絡的機會
```

理由很實際：客戶第一週就會想調（每個產業的理想客戶輪廓不一樣）。
數字寫死在 `.py` 就代表每次微調都要動程式碼、重跑 QA、重新部署；
放設定檔，客戶自己改一行、明天早上就看得到新排序。

權重總和**不必等於 100**，總分會除以權重總和再乘 100 正規化。
`weights` 出現未知的項目名稱會**當場報錯**（`ScoringConfigError`）而不是靜默算 0 分——
打錯一個字卻讓整份名單默默排錯序，是這類系統最難查的缺陷。

### 分數與「久未聯絡」是兩個維度

| 欄位 | 來源 | 說明 |
| --- | --- | --- |
| `band` | 分數 | hot（≥75）/ warm（≥50）/ cool |
| `is_stale` | 最後聯絡日 | 超過 `stale_days`（90 天） |
| `grade` | 兩者合併 | 低分**且**久未聯絡 → `stale`（清洗候選） |
| `is_reengagement_target` | 兩者合併 | 高分**且**久未聯絡 → 今天最該打的那通電話 |

高分而久未聯絡**不會**被降級成 Stale。mock 裡的 Orbitra Retail（84.5 分、135 天沒人碰）
正是書中「標記 90 天未聯絡潛在機會」要撈出來的那種名單。

### 缺資料 ≠ 不符合條件

某個評分項目沒有輸入資料時，它得 0 分，但會被記進 `missing_inputs`
並讓整筆標成 `is_low_confidence`。報表與提示詞都明令：
**不可以把「缺資料」寫成「這家不符合 ICP」**。

---

## 部分失敗設計（沿用 demo09 的降級原則）

三個外部資料源（Companies House / Apollo / LinkedIn）任一掛掉時：

1. 其餘來源**照常**供應它們負責的欄位，流程不中斷。
2. 報表最上方強制加橫幅：`⚠️ 1 個來源無回應：Apollo`。
3. 每個失敗來源走 `Diagnostics.amber(symptom, fix)` 進入 RAG 診斷矩陣的琥珀燈。
4. 受影響的聯絡人分數自動下修並標 `is_low_confidence`——**不是**沿用上次的分數假裝沒事。

實測（見 `test_main.py` 整合測試）：Apollo 掛掉時 Kite Labs 從 51.6 分（Warm）
掉到 36.0 分（Cool），因為員工數、營收、技術棧三項全部無輸入。
分數變差是正確行為：拿殘缺資料算出的高分去排電話順序，比沒有分數更危險。

只有系統性失效才走紅燈退出（例如 `--live` 缺憑證——這種情況寧可不跑，
也不能靜默降級成 mock）。

---

## 外部 API 速率保護

`enrichment.rate_limit_seconds` 預設 **1.0 秒**，且低於 1.0 會被**強制拉回**並記琥珀燈。
Companies House 與 Apollo 對高頻請求直接回 429，被 ban 一次要等 24 小時。

`--mock` 模式**不套用**間隔：讀本機 JSON 不是外部呼叫，在那裡 sleep
只會讓每晚的示範與測試白等，保護不到任何人的 API 額度。

---

## 財務模型（Financial Model）

> **定價以附錄 F（apxF_p08）為準。** 第 05 章另有一套較高定價，見下方 premium tier。

| 項目 | 數值 |
| --- | --- |
| 部署時間 | 1 Day（apxF_p17 標示為「快速配置 < 2 hrs」） |
| 客戶每週回收 | **15 小時**（≈ 60 hrs/mo） |
| 回收價值 | **$4,200 /mo**（書中數據，以 $70/hr 計） |
| 管線預測準確率 | 55% → **75%+** |
| 建置費（Setup） | **$850** |
| 月費（MRR） | **$180 /mo** |
| 客戶第一個月成本 | $1,030 |
| 客戶第一個月淨賺 | **+$3,170** |
| 客戶年度淨值 | $50,400 − $3,010 = **+$47,390** |
| 投資回收期 | **不到 8 天** |

客戶端的算式很好講：每月 $180，換回 15 小時/週、$4,200 的價值。
**23 倍回報，第一個月就轉正。**

供給端（你）：部署一次 1 天，之後每月 $180 是近乎純被動收入。
接 10 個客戶 = $1,800/mo 經常性收入，維護成本主要是外部資料源憑證過期時的處理。

### Premium tier（第 05 章定價，與附錄 F 不一致）

| 來源 | Setup | 月費 |
| --- | --- | --- |
| **附錄 F（本模組採用）** | **$850** | **$180 /mo** |
| 第 05 章 | $1,000 | $280 /mo |

⚠️ **原著的兩個來源對同一個模組給出兩套定價**，本專案未做取捨也未取平均，
一律以附錄 F 為準，並在此如實記錄差異。實務上可把第 05 章那組當成
premium tier：包含每週人工審查衝突清單、客製 ICP 權重調校、季度評分模型校準。
若要用 premium 定價，請改 `config.yaml` 的 `module.client_setup_price` / `client_monthly_price`。

---

## 客戶見證

**（原簡報未提供）**

原書第 05 章與附錄 F 共 34 張投影片中，**完全沒有出現任何人名、職稱或客戶引述**。
此欄位刻意留白而不補寫——編造見證是這門生意最快的自毀方式。

---

## Client Pitch（銷售話術）

**英文原文（apxF_p08 / ch05_p09）：**

> 「Your CRM tells your sales team exactly who to call next — every record researched, scored... The data is finally accurate.」

**繁體中文翻譯：**

> 「你的 CRM 會直接告訴業務團隊下一通電話該打給誰——每一筆紀錄都經過查證與評分……
> 資料終於是準的了。」

延伸三句（面對面時接著講）：

1. 「你們現在怎麼決定今天打給誰？如果那個順序是照 ICP 分數排的，成交率會差多少？」
2. 「更重要的是那些**沉在名單底部**的機會——我們會把 90 天沒人碰、但分數很高的撈出來。
   那通常是最容易成交、也最常被漏掉的一群。」
3. 「還有一件事我要先講清楚：這套系統**不會覆蓋**你們既有的資料。
   外部查不到就保留原值並標記，外部值跟你們不一樣就保留你們的、把外部值送人工審。
   我們寧可少補幾格，也不讓機器用推估值蓋掉業務親自問到的答案。」

---

## 執行方式

```bash
# 零憑證、零網路，讀 mock/*.json 跑完整流程
python main.py --mock

# 只看「哪些欄位、從什麼值變成什麼值」，不寫任何檔案
python main.py --mock --dry-run

# 推到 Telegram
python main.py --mock --notify telegram

# 指定狀態檔與 CSV 輸出位置（跨機器部署時建議放在客戶專屬目錄）
python main.py --mock --state-file ~/crm-state.json --csv-out ~/crm-report.csv

# 串真實 API（缺憑證會列出缺哪些變數並退出，不會靜默退回 mock）
python main.py --live

# 測試
python -m pytest test_main.py -v
```

Windows（Git Bash）執行 Python 一律加 `PYTHONUTF8=1`。

---

## 檔案結構

```
demo16-crm-enrichment/
├── README.md            # 本檔
├── config.yaml          # 資料源、目標欄位白名單、保護欄位、評分權重、級距門檻
├── main.py              # 主流程：選名單 → 取數 → 豐富化 → 評分 → 變更計畫 → CSV → 發送
├── enricher.py          # 欄位決策（補/確認/衝突/查無）、部分失敗、rate limit、變更計畫
├── scorer.py            # ICP 評分、級距、90 天 stale 判定、排序（零硬編碼權重）
├── prompts/
│   ├── enrichment_summary.md  # 把分數表寫成業務主管看得懂的敘述
│   └── conflict_review.md     # 把衝突清單寫成可直接動手的查證說明
├── mock/
│   ├── crm_contacts.json      # 6 筆聯絡人，涵蓋四種情境（見下表）
│   ├── companies_house.json   # 官方登記：SIC 代碼、產業、國別
│   ├── apollo.json            # 商業資料庫：員工數、營收、技術棧（含一筆刻意誤配）
│   └── linkedin.json          # 職稱
└── test_main.py         # happy / edge / integration 三個測試
```

### mock 資料涵蓋的四種情境

| 聯絡人 | 情境 | 預期結果 |
| --- | --- | --- |
| C-1001 Northwind Analytics | **完整資料**，外部與 CRM 一致 | 全部 `confirmed`，0 寫入，100.0 分 / Hot |
| C-1002 Kite Labs | **缺漏可補**，七個目標欄位全空 | 7 個欄位 `filled`，51.6 分 / Warm |
| C-1003 Harbour Freight Co | **查無資料**，三個來源都沒有這個網域 | `enrichment_failed`，欄位原封不動 |
| C-1004 Vellum Legal | **外部與 CRM 衝突**（Apollo 誤配同名公司） | 3 個 `conflict_kept`，保留 CRM 值，29.3 分 / Cool |
| C-1005 Orbitra Retail | 可補 + **135 天未聯絡** | 7 個 `filled`，84.5 分 / Hot + reengagement 目標 |
| C-1006 Pinebox Studio | 4 天前才豐富化過 | 被 `refresh_days` 跳過，不消耗 API 額度 |

---

## 技術要點

- **金額全程 `Decimal`**：JSON 與 YAML 的金額一律以字串儲存（`"12000000.00"`），
  避免 float 二進位誤差讓營收級距的邊界值忽上忽下。
- **可重現的時間基準**：`schedule.reference_date` 讓示範資料的「90 天未聯絡」永遠算得出同一個答案。
  正式部署設為 `null` 即改用系統當前時間。
- **狀態檔**：`--state-file` 記錄每人上次豐富化時間。判斷是否跳過時取
  「狀態檔」與「CRM 欄位」兩者較新的時間——客戶可能在別的工具裡也做過豐富化，
  只信自己的狀態檔會重複消耗額度。
- **CSV 用 `utf-8-sig`**：客戶多半直接用 Excel 開，沒有 BOM 中文欄位會變亂碼，
  然後他們會認定「這份報表壞掉了」而不是「編碼設定問題」。
- **設定錯誤不靜默**：未知的評分項目、`ideal <= min`、`warm >= hot` 一律當場拋
  `ScoringConfigError`，不套預設值繼續跑。
- **關鍵字比對要求詞界**：職稱與產業關鍵字用詞界比對而非子字串。
  `"Director"` 裡面藏著 `"cto"`（dire-**CTO**-r），一律子字串會把 Director 判成 C-level，
  該筆憑空多拿滿分權重（實測多 4 分，足以跨過 Warm/Hot 分界）。
  中文沒有詞界可用，故僅對 ASCII 詞彙套用此規則。
- **`--dry-run` 一定會印在終端機**：`main()` 以「是否真的送出」而非「通道是不是 console」
  判斷要不要印報表。未送出的情況（dry-run、自主權扣住的草稿、通道失敗）一律印出——
  變更計畫沒人看得到，這個旗標就等於不存在。
- **零硬編碼路徑**：全部以 `Path(__file__).resolve().parent` 推算。

---

## 已知限制

- 三個資料源目前都是 mock 讀檔；`--live` 的真實 API 串接需補上各自的 client
  （Companies House REST / Apollo v1 / LinkedIn 需經合規的資料合作方，
  直接爬取違反其使用條款）。
- 實際寫回 CRM 的 client 尚未實作：`apply_changes` 已完成自主權判斷與計數，
  但 `crm_written=True` 分支目前只做記錄，接上 HubSpot / Salesforce API 即可啟用。
- ICP 評分是規則式線性加權，不含機器學習。書中宣稱的「管線預測準確率 55% → 75%+」
  來自原簡報，本專案未取得可驗證的原始資料，不做背書。
- 衝突目前只保留「權威度最高的那一個外部值」做對照；若三個來源彼此也不一致，
  次要來源的值不會出現在待審清單中。
