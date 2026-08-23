# demo27 — 法務文件分析與合規監控

> 模組 #27｜Level 3 企業級 · 法務部門｜部署 **1 週**
> 售價 **$4,000 setup + $1,800/mo**
> 來源：第 07 章 `ch07_p10` + 附錄G `apxG_p15`

> 「**『We just missed the window』is the most expensive compliance cost.**」

每日監控三大來源（政府公告 / 合約庫 + 執照庫 / 內部政策），
合約與執照在到期前 **120、60、14 天**自動發出三階段警告，
依 **三級 Escalation Matrix** 路由到法務長、法遵官或責任經理，
每一次判定都追加寫進三份 **CSV 稽核台帳**——年度合規稽核從「數天」縮短到「數小時」。

---

## ⚠️ 法律免責（請先讀這一節）

**本工具不構成法律意見。** 它的輸出僅供合規團隊**初步篩選**用。

- 系統計算出的**到期日**只是來源檔欄位的日期算術，不代表任何契約或法規上的有效期限。
- 系統列出的**義務**只是來源條款的逐字節錄，不代表對該條款的法律解讀。
- 系統標示的**風險等級**只是排序用的工程分級，不代表法律風險評估。
- 上述每一項的最終判定，**必須由合格法律專業人員確認**。

這段免責同時寫死在 `main.py` 的 `DISCLAIMER` 常數與每一份輸出報告的開頭，
不是靠使用者記得——**因為使用者不會記得**。

---

## Before / After

| | Before（人工） | After（本模組） |
| --- | --- | --- |
| 合約續約窗口 | 靠 Outlook 提醒與某個人的記憶 | 到期前 **120 / 60 / 14 天**三階段自動警告 |
| 執照到期 | 「應該還沒到吧？」 | 每日重掃，逾期當天即列 `overdue` |
| 內部政策審查 | 一年想起來一次 | 依 `review_cycle_days` 自動算逾期天數並旗標 |
| 法規變更 | 靠訂閱電子報 + 有空才讀 | 每日拉政府 RSS/API，逐字節錄 + 標示關聯物件 |
| 升級路徑 | 「這個要跟誰講？」 | 三級矩陣寫死：Critical → 法務長/法遵官雙通道 |
| 稽核舉證 | 翻信箱、翻雲端硬碟 | 三份 CSV 台帳，每列含時間戳 + 來源依據 + 條款原文 |
| 年度合規稽核 | **數天** | **數小時**（`ch07_p10`） |
| 「我們不知道」 | 承認監控系統失靈 | 台帳能證明每天都有掃、掃到什麼、通知了誰 |

---

## 三大監控來源（`apxG_p15` 逐字實作）

| 來源 | 功能 | 產物 |
| --- | --- | --- |
| **Regulatory**（政府 RSS / API） | Monitoring & Impact Assessment | 影響初篩報告（等級**只採用公告自述**） |
| **Contracts**（合約庫） | Deadline Tracking & Alerting | `contract_inventory.csv` |
| **Licences**（執照庫） | Deadline Tracking & Alerting | `licence_inventory.csv` |
| **Policies**（內部政策） | Overdue Review Flagging | `policy_register.csv` |

**警告時程**：到期前 **120 / 60 / 14 天**（`config.yaml` 的 `monitoring.warning_days`）。

---

## 三級 Escalation Matrix

| 級別 | 通道 | 通知對象 | 觸發（本實作的門檻） |
| --- | --- | --- | --- |
| **Critical** | **Slack + Email（雙通道）** | 法務長 / 法遵官 | 已逾期、到期 ≤ 14 天、公告自述 critical |
| **High** | Slack + Email | 責任經理 | 到期 ≤ 60 天、**需人工複核**（條款看不懂 / 缺欄位） |
| **Standard** | 僅 Email | 合規信箱 | 到期 ≤ 120 天、公告自述 standard |

一筆物件同時命中多個條件時，**取最高級別**——法遵寧可往上報，不往下壓。

### ⚠️ 門檻是推導值，不是原文

SPEC（`apxG_p15`）明訂了三級的**通道與通知對象**，但**沒有**明訂「哪個天數落在哪一級」。
上表右欄由 120 / 60 / 14 三階段警告時程推導而來，寫在 `config.yaml` 的
`escalation.rules`，屬**工程預設值**。導入客戶時必須由該客戶的法務長書面確認後才可沿用。

### 安全注意：Critical 必須雙通道

SPEC 原文：「Critical 級別必須雙通道（Slack + Email）同時通知，單一通道失效即漏報」。

本模組把這條做成**硬檢查**，不是註解：

- `escalation.load_matrix()` 發現 `critical.requires_dual_channel` 不是 `true`
  或通道少於兩個 → 直接 `EscalationError`，設定檔想關掉也關不掉。
- 送出前的 `dry_run_probe()` 若判定任一通道不可用，該通知標成
  `incomplete_dual_channel`，**不會**當成「部分成功」。
- `--live` 模式下只要出現一則不可雙通道送達的 Critical → **紅色警報停機**，
  並要求人工立即通知法務長 / 法遵官。寧可停，不可漏。

### 全域安全閥（`apxG_p03`）

「所有 API 呼叫前必經 `--dry-run` 內部通訊測試。」
`escalation.dry_run_probe()` 就是那道閘門：`--mock` 回報模擬通過（零憑證零網路），
`--live` 檢查每個通道宣告的環境變數（`escalation.channel_env`）是否齊全，缺就標不可用。

---

## 三份 CSV 稽核台帳（本模組的核心）

台帳**就是**稽核證據，因此三條硬規則（實作於 `registry.py`）：

1. **追加式（append-only）**：一律 `mode="a"`，全檔沒有任何一處用 `"w"` 開台帳。
   今天判錯了，明天的更正是**再追加一列**，不是改掉舊列。
2. **每列含時間戳與來源依據**：`recorded_at`（ISO 8601 含時區）+ `run_id`
   - `source_ref`（來源檔#識別碼）+ `evidence_quote`（條款原文逐字）。
3. **標頭一經寫入不得變更**：既有檔標頭與本版欄位不符 → `RegistryError`，
   絕不順手改寫。那會讓整份軌跡失去證據能力。

**未升級的物件一樣入帳**——稽核要看的是「每天都有掃、掃到什麼」。
只記警報，會讓「這段期間系統其實沒在跑」變得無法證明。

| 台帳 | 主要欄位 |
| --- | --- |
| `contract_inventory.csv` | `contract_id` `counterparty` `expiry_date` `days_to_expiry` `warning_stage` `auto_renew` `notice_period_days` `annual_value` `escalation_level` `evidence_quote` |
| `licence_inventory.csv` | `licence_id` `licence_name` `issuing_authority` `jurisdiction` `expiry_date` `days_to_expiry` `warning_stage` `escalation_level` `evidence_quote` |
| `policy_register.csv` | `policy_id` `policy_name` `last_reviewed` `review_cycle_days` `next_review_due` `days_to_review_due` `review_stage` `escalation_level` `evidence_quote` |

三份台帳都用標準庫 `csv` 模組寫入（`encoding="utf-8"`、`newline=""`），
**不引入任何第三方 CSV / Excel / PDF 套件**。

台帳預設寫到模組目錄下的 `registry/`，用 `--registry-dir` 可導到別處
（測試一律導到 `tmp_path`，不污染工作樹）。

---

## 逐字提取，絕不推論

與 demo03 同源的鐵律。系統寧可少報，不可捏造：

| 情境 | 系統行為 |
| --- | --- |
| 條款交叉引用未附上的 Schedule / Annex | `needs_human_review: true`，原文逐字保留，**不用商業慣例補完** |
| 條款判讀信心 < `confidence_floor`（預設 0.75） | 照樣輸出，但標人工複核並升級為 High |
| 來源沒有 `expiry_date` | 留空 + `stage: unknown`，**不從 effective_date + 12 個月推算** |
| 政策沒有 `last_reviewed` | 下次審查日留空，**不推估** |
| 公告未載明影響等級 | `declared_level: null` + 人工複核，**系統不自行判定法規影響** |
| 來源沒指定負責人 | 寫「（來源未指定負責人）」，**不挑一個人頂上** |

`days` 欄算不出來時留**空字串**而不是 `0`——`0` 代表「今天到期」，語意完全不同。

---

## 財務模型

### 客戶端（買方）

| 項目 | 金額 |
| --- | --- |
| 導入費（一次） | **$4,000** |
| 月費 | **$1,800** |
| 第一年總成本 | $4,000 + $1,800 × 12 = **$25,600** |
| 風險消除 | 避免通常高達 **$5k–$500k** 的違規罰款（`apxG_p15` ROI Dashboard） |
| 稽核節省 | 年度合規稽核時間從「**數天**」縮短至「**數小時**」 |

> ROI 不用「回收工時 × 時薪」表達，因為本模組的價值主體是**避免的罰款**與
> **可舉證的稽核軌跡**，把它折算成小時數會低估風險面（也是在對客戶說謊）。

### 服務商端（賣方）

| 項目 | 數字 |
| --- | --- |
| 部署時間 | **1 週** |
| 內部回收工時 | **（原簡報未提供）** |
| 10 個客戶的月經常性收入 | $18,000 |
| 20 個客戶的月經常性收入 | $36,000 |

**「內部回收」為何是空的**：本書 36 張投影片（`ch07_p01`–`p15`、`apxG_p01`–`p21`）
中**沒有任何一頁**提供顧問自己的內部建置工時回收數字，全部 ROI 皆為客戶端節省。
此欄一律標「原簡報未提供」，**不推估**。

### ⚠️ 定價以本頁為準

附錄G 最後的 **The Level 3 Commercial Matrix**（`apxG_p20`）把 #21–#30 十個模組
**全部**列為 `建置報價 [$8,000-$10,000]` + `月訂閱費 [$4,000-$6,000]`——
該頁明顯是套用同一組模板佔位值。

本模組一律採用**模組頁的逐案報價**：**$4,000 setup + $1,800/mo**（`ch07_p03`、`apxG_p15`）。
`apxG_p20` 的價格帶請視為**銷售話術用的區間**，不是本模組的實際報價。

---

## 客戶見證

> **Marcus Webb** 將此系統作為 **$400/月**的「進階法律監控服務」打包賣給 **35 家**企業客戶。
> — `apxG_p15`

$400 × 35 = **$14,000/月**經常性收入，來自同一套部署。
這正是 Level 3 的定位：**不是節省工時，是改變這家顧問公司能成為什麼**。

---

## Client Pitch（成交話術）

> 「**你的 AI 合規官永遠不會請假。**」

延伸三句（被追問時使用）：

1. **可舉證**：稽核來的時候，您給的不是「我們有在追」，而是一份每天一列、
   含時間戳與條款原文的 CSV 台帳。年度稽核從數天變數小時。
2. **不漏報**：Critical 一定雙通道。任何一個通道掛掉，系統會停下來叫人，
   而不是安靜地只送一半。
3. **不亂講**：條款看不懂、欄位缺值，系統標紅交給您的律師，**絕不自己編一個日期**。
   這份工具不出法律意見——它只是讓您的法務不必再靠記憶力。

---

## 快速上手

```bash
# 零憑證、零網路，跑完整條流程（時間釘在 config.mock.frozen_now）
python main.py --mock

# 台帳與狀態檔導到別的目錄，不污染工作樹
python main.py --mock --registry-dir ~/compliance-ledgers --state-file ~/compliance-state.json

# 把「現在」釘在指定時刻重跑（稽核回溯 / 驗證某天的判定）
python main.py --mock --now 2026-12-01T09:00:00+08:00

# 跑完流程但不發送、不寫台帳與狀態檔
python main.py --mock --dry-run

# 結果轉 JSON（供下游系統接手）
python main.py --mock --json

# 推到 Telegram
python main.py --mock --notify telegram

# 真實模式（需 ANTHROPIC_API_KEY；Critical 通道憑證不齊會直接停機）
python main.py --live --notify gmail

# 測試
python -m pytest test_main.py -v
```

---

## 檔案結構

| 檔案 | 用途 |
| --- | --- |
| `main.py` | CLI 主流程：載設定 → 四來源掃描 → 升級路由 → 台帳入帳 → 發送 |
| `analyser.py` | 到期/逾期判定、三階段警告、逐字條款提取、`needs_human_review` 旗標 |
| `escalation.py` | 三級 Escalation Matrix、雙通道硬檢查、`--dry-run` 內部通訊測試 |
| `registry.py` | 三份 CSV 台帳的 append-only 寫入與標頭防呆 |
| `prompts/regulatory_impact.md` | 法規影響初篩提示詞（禁止自行判定等級） |
| `prompts/clause_review.md` | 合約 / 執照條款判讀提示詞（禁止補完缺失欄位） |
| `config.yaml` | 模組數據、時區、三階段門檻、升級矩陣、台帳路徑、去重設定 |
| `mock/contracts.json` | 7 筆合約：正常 / 120 / 60 / 14 天 / 已逾期 / 條款看不懂 / 缺到期日 |
| `mock/licences.json` | 4 筆執照：正常 / 60 天 / 14 天 / 缺發證機關 |
| `mock/policies.json` | 4 筆政策：週期內 / 逾期 195 天 / 60 天內 / 缺 `last_reviewed` |
| `mock/regulatory_feed.json` | 4 則公告：critical / standard × 2 / 未載明等級 |
| `mock/impact_assessment.md` | LLM 離線 fixture（`--mock` 時 `complete()` 直接讀這份） |
| `test_main.py` | 3 個測試：happy / edge（絕不猜值） / integration（三級路徑 + append-only） |

---

## 已知限制

- **不解析 PDF**：來源一律是合約管理系統匯出的結構化文字（JSON / CSV）。
  引入 PDF 解析會帶來無法離線驗證的二進位依賴，且掃描版合約的文字層品質
  本來就不足以支撐稽核級證據——那該由人先數位化。
- **不做法域判斷**：同一句條款在不同司法管轄區效果不同。系統只記錄
  `jurisdiction` 欄位，不會因為法域不同而改變判定。
- **不追法規全文**：只處理公告 feed 提供的節錄與其自述影響等級。
  公告全文的解讀是律師的工作。
- **通道名稱是邏輯通道**：`slack` / `email` 是升級矩陣的路由標籤，
  本 demo 統一由 `_shared/notifier.py` 輸出（預設 console）。
  正式接線時對應到實際的 Slack webhook 與 Gmail 寄送。
- **去重狀態檔可覆寫**：`.compliance-state.json` 只是「同階段別重複轟炸法務長」的
  節流器，不是稽核證據，因此允許覆寫。**台帳才是 append-only 的那一份。**
- **自主權預設 DRAFT**：要開 `supervised_auto` 必須提供白名單，
  且未滿 14 天會持續發出警告（書中鐵律）。
- **`registry/` 與 `.compliance-state.json` 是執行期產物**，
  正式部署請放到工作樹之外（`--registry-dir` / `--state-file`）並納入備份。
