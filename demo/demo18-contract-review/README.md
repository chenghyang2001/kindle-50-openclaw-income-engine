# demo18 — 合約審查與條款提取（Contract Review & Clause Extraction）

> 模組 #18｜Level 2 代理商基礎｜營運法務 / 後台風險
> 部署 **1 Day**（附錄F 註記：**需 90 分鐘深度配置**）｜每份合約回收 **5 小時**
> 售價 **$1,800 setup + $500/mo**（第05章與附錄F **兩來源完全一致**，直接採用）

上傳合約 → 瞬間提取所有標準條款 → 標示偏離程度 → **4-8 分鐘內**產出結構化風險備忘錄。
紅旗條款**繞過常規備忘錄**，直接發送緊急警報給資深合夥人。

> ⚠ **先讀「法律免責聲明」一節再往下看。本工具不構成法律意見。**

---

## 法律免責聲明（實作補充，非原著內容）

**本工具不構成法律意見（This tool does not provide legal advice）。**

- 本模組的輸出**僅供初步篩選（preliminary screening）**，用途是把助理律師從機械性的
  條款尋找工作中解放出來，**不是**取代法律判斷。
- 四分類判定（`Standard` / `Deviation` / `Missing` / `Red Flag`）是**規則比對的結果**，
  不是對合約效力、可執行性或商業風險的法律評價。
- **任何簽署決定前，必須由合格法律專業人員（qualified legal professional）逐條審閱原文。**
- 判定為 `Standard` **不代表**該條款對貴方有利、合法或可執行，只代表它符合貴方自訂的
  `CLAUSE_LIBRARY` 基準立場字樣。
- 本工具不建立律師-委任人關係，不承擔任何因採用其輸出而生的責任。
- 導入前請確認合約檔案的處理符合貴所的保密義務與資料處理政策
  （本模組 `--mock` 為零外送；`--live` 會把合約全文送往 LLM API，導入前必須做隱私影響評估）。

此節為**實作補充**：原簡報並未提供任何免責或合規文字（見「與原簡報的差異」）。
在法務場景交付自動化工具而不附免責聲明，是法遵上不可接受的疏漏，故由本實作補上。

---

## Before / After

| | Before（人工） | After（本模組） |
| --- | --- | --- |
| 條款提取 | 助理律師閱讀 **30-100 頁**，手動尋找關鍵條款 | 逐字提取 14 種標準條款，附條號可回溯 |
| 單份耗時 | 提取 **6 小時** + 寫備忘錄 **45 分鐘** = 總計 **5-8 小時** | **4-8 分鐘**產出結構化備忘錄 |
| 成本 | 消耗 **$600–$900** 計費工時的機械性勞動 | 每份合約回收 **5 小時**可計費產能 |
| 偏離判斷 | 靠個人記憶中的「我們通常怎麼寫」 | 對照 `CLAUSE_LIBRARY` 的成文基準立場 |
| 缺失條款 | 最容易漏——沒寫的東西不會跳出來提醒你 | 全文搜尋後仍未命中即標 `Missing`，附應補字詞 |
| 紅線條款 | 埋在第 47 頁，可能到用印前才發現 | `trigger_immediately_if` 命中即繞過備忘錄，直送資深合夥人 |
| 團隊容量 | 受限於資深人力 | 相同人數下合約處理容量提升 **3-4 倍** |

---

## 三條鐵律（違反即為重大缺陷）

### 鐵律一：逐字引用（Quote verbatim）

備忘錄中出現的每一段合約文字，都是**原文的精確子字串**，不是摘要、不是改寫、
不是「整理得更通順」的版本。

實作上用兩層保證：

1. **結構上不可能改寫**：引文一律以 `text[start:end]` 從原文切片產生，程式碼裡
   沒有任何一條路徑會「組裝」條款文字。
2. **事後驗證當保險絲**：`verify_verbatim(quote, source)` 對原文做精確子字串比對，
   **驗不過就把引文丟掉**（欄位留 `None`），並記 amber + 標 `needs_human_review`。
   備忘錄該欄會印出「未取得通過逐字驗證的引文，系統不輸出未驗證文字」。

`--live` 模式下語言模型提出的引文一樣要回原文驗證，驗不過直接丟棄並記 amber
（同 demo03 的承諾閘門、demo06 的模糊掃描：**寧可漏一條，不可捏造一條**）。

### 鐵律二：四分類，只有四種

| 分類 | 意義 | 系統行為 |
| --- | --- | --- |
| `Standard` | 通過，符合企業基準立場 | 列入對比表，不需行動 |
| `Deviation` | 偏離基準立場 | 指出商業/法律風險，**提供替代字詞** |
| `Missing` | 合約中完全遺漏的關鍵保護條款 | 附上應補上的標準立場全文 |
| `Red Flag` | 命中 `trigger_immediately_if` 硬性紅線 | **繞過備忘錄**，直送資深合夥人警報 |

判定優先序：`Red Flag` > `Missing` > `Deviation` > `Standard`。

**安全不對稱（本模組最重要的設計決策）**：四個分類裡只有 `Standard` 是「放行」，
其餘三個都會把案子推回人手上。所以「不確定」時**絕對不能判 `Standard`**——
那是唯一會造成假性安心的錯誤。任何無法確認的條款一律依 `review.unresolved_verdict`
降級（預設 `Deviation`）並標 `needs_human_review`。

> 少判一條 `Standard` 只是多花律師五分鐘；多判一條 `Standard` 可能讓公司簽下無上限責任。

觸發降級的情況：引文未通過逐字驗證、僅由內文關鍵字命中（信心 < `confidence_floor`）、
條款全文超過 `quote_max_chars` 而被截斷、由語言模型定位而未命中任何 library 樣式。

### 鐵律三：`trigger_immediately_if` 升級路徑

書中明列**兩條硬性紅線**（`config.yaml` 的 `risk_escalation_rules`，逐條實作，不自行增刪）：

| 規則 | 為什麼是紅線 |
| --- | --- |
| `unlimited_liability`（無上限責任） | 責任不設上限等於把公司全部資產押在單一合約上，任何金額的求償都無法預估 |
| `background_ip_assignment`（背景 IP 轉讓） | 背景 IP 是既有資產，一旦讓與即失去在其他客戶專案重複使用的權利 |

命中後的路徑（Step 4 Action）：

```
Red Flag 命中
   → 該條款判定為 Red Flag（其餘比對結果不再重要）
   → build_escalations() 產生升級項，bypass_memo = true
   → dispatch_alert() 先於備忘錄送出「SENIOR PARTNER ALERT」
   → 警報內文引用「命中原文（逐字）」，不是規則名稱的轉述
```

同一份合約（相同 `contract_id` + 相同全文 SHA256）已警報過時，
`--state-file` 台帳會把後續警報標 `is_suppressed` 不重複發送，
但**判定結果仍保留在輸出中**，讓稽核看得到「當時確實判過紅旗」。

---

## Clause Comparison Engine（附錄F p16 四步驟）

```
Step 1  Extract   從文件中精準提取特定條款全文（Quote verbatim）      → extractor.py
Step 2  Compare   與 CLAUSE_LIBRARY 的標準立場進行逐字與語義比對       → classifier.py
（四分類）        Standard / Deviation / Missing / Red Flag
Step 4  Action    紅旗直送資深合夥人；其餘彙整為結構化備忘錄           → main.py
```

> 原圖標示為 **Step 1 / Step 2 / Step 4，沒有標示 Step 3**，四分類方塊未編號。
> 本實作照原圖如實處理，不自行補號。

前置條件：`JURISDICTION` 必須先配置且與合約管轄權一致（`config.yaml` 的 `jurisdiction`）。
不一致就沒有「標準立場」可言，`check_jurisdiction()` 直接走紅色警報停機，**不做任何比對**。

---

## CLAUSE_LIBRARY（14 種標準條款）

| # | 條款 | 基準立場摘要 |
| --- | --- | --- |
| 1 | Limitation of Liability 責任限制 | 上限不超過求償前 12 個月已付費用（年度金額 1 倍），排除間接損失 |
| 2 | Indemnities 賠償 | 相互賠償，範圍限第三方請求，受責任限制拘束 |
| 3 | Termination 終止 | 任一方得以 30 日書面通知便利終止 |
| 4 | Payment Terms 付款條件 | 無爭議發票 30 日內付款，逾期計息 |
| 5 | Confidentiality 保密 | 雙向保密，期限 5 年 |
| 6 | Intellectual Property 智慧財產權 | 各自保有背景 IP；成果 IP 於付清後移轉 |
| 7 | Data Protection 資料保護 | UK GDPR + DPA 2018，外洩 72 小時內通報 |
| 8 | Governing Law 準據法 | 以配置的管轄權為準據法 |
| 9 | Dispute Resolution 爭議解決 | 先協商 30 日，法院專屬管轄 |
| 10 | Warranties 保證 | 合理技能與注意，驗收後 90 日無重大瑕疵 |
| 11 | Insurance 保險 | 專業責任保險每一請求不低於 £2,000,000 |
| 12 | Assignment 契約讓與 | 非經事前書面同意不得讓與 |
| 13 | Force Majeure 不可抗力 | 標準免責；持續逾 60 日得終止 |
| 14 | Non-Solicitation 禁止招攬 | 期間及終止後 12 個月不得招攬員工 |

> ⚠ **實作補充**：原簡報只給出「**14 種**」這個數量，**沒有列出是哪 14 種**。
> 上表為本實作依英美商務合約慣例補齊的**示範基準**。
> 正式導入時，這 14 條必須由客戶的法務主管逐條改寫成該公司真正的 Standard Position——
> `CLAUSE_LIBRARY` 是「這間公司的立場」，不是「業界通則」，這也是那 90 分鐘深度配置在做的事。

金額型基準（責任上限倍數、最低保額）一律以 `decimal.Decimal` 比對，全程禁止 `float`。

---

## 財務模型

### 客戶端（買方）

書中給的是「每份合約回收 5 小時」與「$600–$900 / 6 小時」的計費工時，
**沒有給每月合約量**。下表以 **每月 4 份合約**為假設（保守值，已標明是實作假設）。

| 項目 | 金額 |
| --- | --- |
| 導入費（一次） | $1,800 |
| 月費 | $500 |
| 第一年總成本 | $1,800 + $500 × 12 = **$7,800** |
| 每份合約回收工時 | 5 hrs |
| 計費費率（書中 $600–$900 / 6 hrs 換算） | 約 **$100–$150 / hr**，下以 $120 計 |
| 每份合約回收價值 | 5 hrs × $120 = **$600** |
| 每月價值（假設 4 份 / 月） | **$2,400** |
| 年度價值 | **$28,800** |
| 第一年淨效益 | $28,800 − $7,800 = **$21,000** |
| 投資回收期 | $1,800 ÷ ($2,400 − $500) ≈ **1 個月** |
| 第一年 ROI | **約 269%** |
| 非金錢效益 | 相同人數下合約處理容量提升 **3-4 倍**（書中數字） |

### 服務商端（賣方）

| 項目 | 數字 |
| --- | --- |
| 首次部署工時 | 1 Day，其中 **90 分鐘**是 `CLAUSE_LIBRARY` 深度配置 |
| 每月維運工時（每客戶） | 約 30 分鐘（新增條款樣式、調整紅線） |
| 10 個客戶的月經常性收入 | **$5,000** |
| 20 個客戶的月經常性收入 | **$10,000** |

附錄F p17 商業決策矩陣定位：**#18 是高難度高回報的利潤中心**
（X 軸部署複雜度高、Y 軸 MRR 潛力最高）。

---

## 客戶見證

（原簡報未提供）

> 原書 34 張投影片中**完全沒有出現人名、職稱或客戶引述**，本模組不編造見證。
> 依書中「落地劇本」，正確的做法是**先為自己的業務部署並跑滿一個月**，
> 再把自己的數據轉成案例研究——`Clients buy outcomes they have seen, not promises they have been told.`

---

## Client Pitch（成交話術）

**原文（apxF）**：

> 「Every contract reviewed, every clause extracted, every risk flagged — in under ten minutes.
> The mechanical work runs itself.」

**繁體中文翻譯**：

> 「每一份合約都審過、每一條條款都提取出來、每一個風險都標記完成——**十分鐘之內**。
> 機械性的工作，讓它自己跑。」

延伸三句（被追問時使用）：

1. **可回溯**：每一條判定都附上逐字原文與條號，任何爭議 10 秒內翻到那一頁。
2. **不編造**：系統驗不過原文的引文一律丟棄留白，寧可讓律師自己翻，也不給一段看起來很像的文字。
3. **不放行**：只有 `Standard` 是通過。系統不確定時一律降級標人工，不會給你假性安心。

---

## 快速上手

```bash
# 零憑證、零網路，跑完整條流程（預設讀 mock/contract_standard.json）
python main.py --mock

# 四種合約情境
python main.py --mock --contract mock/contract_standard.json    # 14 條全部 Standard
python main.py --mock --contract mock/contract_deviation.json   # 6 條 Deviation
python main.py --mock --contract mock/contract_missing.json     # 3 條 Missing
python main.py --mock --contract mock/contract_redflag.json     # 2 條 Red Flag → 資深合夥人警報

# 跑完流程但不發送、不寫台帳
python main.py --mock --dry-run

# 台帳指到別處（CI / 測試必用，避免污染工作樹）
python main.py --mock --state-file /tmp/reviewed.json

# 推到 Telegram、輸出完整 JSON
python main.py --mock --notify telegram --json

# 真實模式（需 ANTHROPIC_API_KEY；會把合約全文送往 LLM API，導入前先做隱私評估）
python main.py --live --contract path/to/contract.json --notify gmail

# 測試
python -m pytest test_main.py -v
```

---

## 檔案結構

| 檔案 | 用途 |
| --- | --- |
| `main.py` | CLI 主流程：載設定 → 管轄權檢查 → 提取 → 分類 → 升級 → 備忘錄 → 發送 → 台帳 |
| `extractor.py` | **Step 1 Extract**：逐字提取、`verify_verbatim`、`parse_money`（Decimal） |
| `classifier.py` | **Step 2 Compare + 四分類 + Step 4 升級**：紅旗掃描、偏離比對、安全降級 |
| `prompts/extract_clauses.md` | 提取提示詞：明令逐字引用、不得改寫、不確定就回報 |
| `prompts/review_memo.md` | 摘要提示詞：禁止更動分類、禁止提供法律意見 |
| `config.yaml` | `JURISDICTION` / `CLAUSE_LIBRARY`（14 條） / `RISK_ESCALATION_RULES` / 自主權 / 台帳 |
| `mock/contract_standard.json` | 標準合約（14 條全 Standard） |
| `mock/contract_deviation.json` | 偏離合約（付款 90 天、責任上限 3 倍、保額不足、終止 180 天、可自由讓與、禁止招攬 36 個月） |
| `mock/contract_missing.json` | 缺漏合約（完全沒有資料保護 / 保險 / 禁止招攬三條） |
| `mock/contract_redflag.json` | 紅旗合約（無上限責任 + 背景 IP 轉讓） |
| `test_main.py` | 3 個測試：happy / edge（逐字引用 + 缺漏抓到） / integration（紅旗升級 + 自主權） |

---

## 已知限制

- **不解析 PDF / DOCX**：本模組的輸入是已轉成純文字的 `sections` 結構
  （依賴只有 PyYAML + pytest + 標準庫）。轉檔由前置程序負責——把轉檔錯誤與提取錯誤混在一起，
  會讓「引文對不上原文」變成無解懸案，因此刻意切開責任邊界。
- **語義比對是規則比對**：`Step 2` 的「語義」在本實作是 `CLAUSE_LIBRARY` 的樣式集合，
  不是模型推理。對方換一種寫法而樣式沒收錄時，會落到 `Missing` 或降級標人工，
  **不會**被誤判為 `Standard`（安全方向的錯誤）。
- **一個條號只歸一條條款**：同一段文字若同時涉及兩條標準條款，系統只會歸給先命中的那條。
  合併判斷是法律判斷，系統不替律師決定。
- **紅旗以條號為單位傳染**：紅旗命中某條號時，歸屬該條號的條款一律判 `Red Flag`。
  這是刻意的保守設計——那一段已經有硬性紅線，其餘比對結果不再重要。
- **不做日期與金額推算**：只採用合約原文出現的數字。跨幣別換算、生效日推算都不做。
- **金額只取條款中第一個**：一條款出現多個金額時，哪個是上限屬法律判斷，
  系統取第一個並在報表標示，由人複核。
- **自主權預設 DRAFT**：要開 `supervised_auto` 必須提供白名單，且未滿 14 天會持續發出警告。

---

## 與原簡報的差異（如實記錄）

| 項目 | 原簡報 | 本實作 |
| --- | --- | --- |
| 定價 | 第05章與附錄F **完全一致**（$1,800 + $500/月） | 直接採用，無需取捨 |
| 模組名稱 | 第05章「合約審查與條款提取」／附錄F「智能合約審查」 | 採第05章全名 |
| 部署時間 | 1 Day（附錄F 加註「需 90 分鐘深度配置」） | 兩者並記，`config.yaml` 兩個欄位分別記錄 |
| 14 種標準條款 | **只給數量，未列名稱** | 補上示範用的 14 條，並標明必須由客戶法務改寫 |
| 客戶見證 | **完全沒有**（34 頁無任何人名／引述） | 標「（原簡報未提供）」，不編造 |
| 安全/合規 | 只有兩條硬性紅線與 Quote verbatim 要求 | 照實作；**法律免責聲明為本實作補充** |
| 技術棧歸屬 | 附錄F p18 四大類中**未列出 #18** | 如實記錄，不自行歸類 |
