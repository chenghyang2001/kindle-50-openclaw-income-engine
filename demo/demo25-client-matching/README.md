# demo25 — 動態客戶媒合引擎（房地產 / B2B 批發）

> 《The OpenClaw Income Engine》第 07 章 · 附錄 G · 模組 **#25** · Level 3 企業級
> 來源頁：`ch07_p08`、`apxG_p12`
> 部署 **1 週**｜內部回收 **（原簡報未提供）**｜售價 **$3,500 setup + $1,400/mo**

物件一上架，60 分鐘內掃過所有註冊買方／批發商的結構化條件，
達門檻的直接收到一封**像專屬經紀人寫的**推薦信——而不是入口網站的系統警報。

---

## Before / After

| | Before（靠業務記憶力） | After（媒合引擎） |
| --- | --- | --- |
| **資料橋接** | Listings 與 Client Criteria 兩個資料庫之間的 Database Bridge **是斷的** | 每次上架自動跑完整笛卡兒積比對 |
| **匹配率** | **10–15%**（業務當天想得起來的那幾個客戶） | **100%** 條件匹配 |
| **通知速度** | **1–3 天** | **60 分鐘內** |
| **重複打擾** | 同一組客戶被不同業務重複通知，或根本沒人通知 | 去重狀態檔保證「一買方一物件一次」 |
| **條件變更** | 客戶改了預算沒人記得回頭翻舊物件 | 條件指紋一變，既有物件自動重跑一輪 |
| **冷門物件** | 掛在架上等，直到屋主自己來問 | 低詢問度自動產出 Vendor Pricing Pack |
| **損失** | 每一個未成功的媒合，都是流失的營收 | 漏接歸零 |

**這個模組真正的價值不是速度，是把「靠業務記得住」換成「系統一定記得」。**

---

## 🔴 公平住房法遵（本模組最重要的一節）

> **本節是實作補充，原著 36 張投影片未提及。但房地產媒合若不做這件事，
> 系統跑得越好、違法風險越高——這不是可選項。**

房地產推薦在多國受 **公平住房法**（美國 Fair Housing Act、英國 Equality Act 等）
規範。媒合條件**絕不可**直接或間接使用受保護特徵：

種族 · 膚色 · 宗教 · 國籍 · 性別 · 性傾向 · 婚姻與家庭狀況 ·
是否有子女 · 身心障礙 · 年齡 · 收入來源

### 本模組的三道防線

| 防線 | 位置 | 行為 |
| --- | --- | --- |
| **1. 欄位白名單** | `config.yaml` 的 `matching.allowed_criteria_fields` | 只有列舉出來的客觀條件可以進入媒合。白名單外的欄位一律拒絕。 |
| **2. 受保護詞根偵測** | `matcher.PROTECTED_ATTRIBUTE_TOKENS` | 欄位名（英／中）含受保護詞根即命中。大小寫與連字號先正規化，`Buyer-Race`／`NO_CHILDREN` 都擋得住。 |
| **3. 提示詞紅線** | `prompts/recommendation_email.md` | 明令不得描述社區人口組成（steering），只描述物件客觀事實。 |

### 命中時的行為：**拒絕執行，不是略過欄位**

```bash
$ python main.py --mock --buyers-file mock/buyers_noncompliant.json
法遵違規，已拒絕執行：買方 B-901（違規範例 A（家庭狀況））的 criteria 含疑似受保護特徵欄位
['no_children']：公平住房法禁止以種族／宗教／家庭狀況／身心障礙等特徵做媒合，已拒絕執行
$ echo $?
1
```

**為什麼不「略過該欄位繼續跑」**：靜默略過會讓違規條件看起來「有生效」，
業務以為系統照他填的條件在篩，實際上沒有——出事時既無法證明系統沒用它，
也無法證明業務沒用它。當場失敗、留下稽核紀錄，是唯一站得住腳的處理。

### 白名單設計的注意事項

`preferred_postcodes` 本身是客觀欄位，但**若由業務代填來排除特定社區，
即構成 redlining（以地理界線間接實現種族歧視）**。
營運規則：郵遞區號偏好只能由買方本人在自助表單填寫，不得由業務代填或代改。
本模組在技術上擋不住這件事，只能靠稽核軌跡讓它可被查核——
`state/audit.jsonl` 的每一筆 `notification_sent` 都記錄了當次使用的條件欄位。

---

## 核心流程

```
Property Portal Webhook（或每 15 分鐘排程）
  ↓
載入   物件清單 + 註冊買方／批發商條件
  ↓
🔴 法遵閘門   config 比對欄位 + 每位買方 criteria 過白名單 + 受保護詞根偵測
  ↓              └─ 命中 → 拒絕執行（exit 1），不做半套推薦
安全閥  對外通訊預檢（apxG_p03；--dry-run 本身即預檢模式）
  ↓
條件變更  買方 criteria 指紋比對 → 有變 → 清空該買方去重紀錄，既有物件重跑
  ↓
比對    物件 × 買方全比對，Hard 3x / Soft 1x 加權評分
  ↓        ├─ 分數 >= 80 且未通知過 → 推播
  ↓        ├─ 分數 >= 80 但通知過   → **去重擋下**（記稽核，不打擾）
  ↓        └─ 分數 < 80             → 不推播（75–79 記為 near-miss）
分級    90+ Perfect → 附看屋預約連結 + 簡訊標記｜75–89 Strong → 標準信件
  ↓
時效    上架 > 60 分鐘才通知 → AMBER + 稽核事件（仍照常通知）
  ↓
分支 B  低詢問度物件（7 日詢問 <= 2 且上架 >= 14 天）→ Vendor Pricing Pack
  ↓
落帳    去重狀態檔 + append-only 稽核軌跡
```

---

## 快速上手

```bash
# 零憑證、零網路跑完整流程（讀 mock/ 的 5 物件 × 5 買方）
python main.py --mock

# 再跑一次 —— 8 封全部被去重擋下，一封都不重複送
python main.py --mock

# 跑完流程但不發送、不寫去重狀態檔（同時就是對外通訊的安全閥）
python main.py --mock --dry-run

# 指定狀態檔與稽核檔（排程環境建議放在 demo 目錄外）
python main.py --mock --state-file ~/openclaw/demo25.json --audit-file ~/openclaw/demo25-audit.jsonl

# 驗證法遵閘門真的會擋（預期 exit code 1）
python main.py --mock --buyers-file mock/buyers_noncompliant.json

# 重現特定時點的 60 分鐘時效判定
python main.py --mock --now 2026-08-24T08:50:00+08:00

# 串真實物件／買方 API 與 Claude API（缺 ANTHROPIC_API_KEY 會明確報錯）
python main.py --live --notify telegram

# 測試
python -m pytest test_main.py -v
```

### 旗標

| 旗標 | 說明 |
| --- | --- |
| `--mock` / `--live` | 離線 mock 資料 vs 真實 API（互斥，預設 `--mock`） |
| `--dry-run` | 跑完流程但不發送、不更新狀態檔 |
| `--notify` | `console` / `telegram` / `gmail` / `line` / `whatsapp` |
| `--config` | 設定檔路徑 |
| `--state-file` | 通知去重狀態檔路徑 |
| `--audit-file` | 稽核軌跡 JSONL 路徑 |
| `--now` | 以指定 ISO 8601 時間作為「現在」（重現時效判定） |
| `--buyers-file` | 覆寫 mock 買方資料路徑（測法遵閘門用） |

### 退出碼

| 碼 | 意義 |
| --- | --- |
| `0` | 全部推薦皆在時效內、無任何警示 |
| `2` | 流程完成，但有 AMBER（逾 60 分鐘時效／缺預約連結／自主權警告） |
| `1` | **法遵違規**、紅色警報或致命錯誤，本次沒有任何推薦送出 |

排程器請把 `2` 當成「有事要看」而不是「壞掉」；`1` 一定要有人處理。

### 排程

```cron
*/15 * * * *  cd /path/to/demo25-client-matching && python main.py --live --notify telegram
```

正式環境建議改接 Property Portal Webhook 即時觸發——15 分鐘排程的最壞情況
是上架後 15 分鐘才開始跑，會吃掉 60 分鐘時效預算的四分之一。

---

## 媒合評分（逐字實作自 `apxG_p12`）

```yaml
matching_criteria:
  hard_match_fields: ["max_price", "min_bedrooms", "property_type"]
  soft_match_fields: ["preferred_postcodes", "required_features", "min_floor_area"]
  match_score_threshold: 80
  hard_match_weight: 3
  soft_match_weight: 1
```

**分數 = 命中權重 ÷ 可得權重 × 100**

3 個 Hard（各 3 分）+ 3 個 Soft（各 1 分）= 滿分 12 → 100 分。

| 情境 | 權重 | 分數 | 分級 | 推播？ |
| --- | --- | --- | --- | --- |
| 全中 | 9 + 3 | **100.00** | Perfect | ✅ 附看屋連結 + 簡訊 |
| Hard 全中、Soft 中 2 | 9 + 2 | **91.67** | Perfect | ✅ 附看屋連結 + 簡訊 |
| Hard 全中、Soft 中 1 | 9 + 1 | **83.33** | Strong | ✅ 標準信件 |
| Hard 全中、Soft 全失 | 9 + 0 | **75.00** | Strong | ❌ **低於門檻 80** |
| Hard 缺 2、Soft 中 1 | 3 + 1 | **33.33** | below | ❌ |

**買方沒填的欄位不計入分母**：沒填代表「不在意」，計入會讓填得少的買方無故被扣分。
一個有效條件都沒填的買方分數為 0 且不推播（避免「無條件 = 全部命中」的荒謬結果）。

### ⚠️ 已知規格衝突：Strong 從 75 起算，門檻卻是 80

`apxG_p12` 同一頁同時給了 `match_score_threshold: 80` 與「Strong **75-89**」。
兩者相衝：**75.00–79.99 分會被標為 Strong 卻不推播**。

本模組**照字面實作、不自行裁定**：`tier` 照 75/90 分段，`is_pushable` 照 80 門檻。
mock 資料刻意保留三組落在這個縫裡的比對（如 `L-005 × B-102 = 75.00`），
它們會出現在報告的「逼近門檻」統計與 Vendor Pricing Pack 的落差診斷中，
而不是消失不見。

若客戶要求統一，改 `config.yaml` 的 `strong_score_min` 或 `match_score_threshold` 即可，
不需要動程式碼。**安全注意**：規格明載 `match_score_threshold: 80` 是推播門檻，
低於此值不得主動推播（避免騷擾買家名單），調低前請先評估退訂率。

---

## 通知去重與條件變更重比對

去重狀態檔（`state/notifications.json`）長這樣：

```json
{
  "version": 1,
  "notified": {
    "B-104|L-001": { "notified_at": "...", "score": "100.00", "tier": "perfect" }
  },
  "criteria_fingerprints": {
    "B-104": "<買方條件的 SHA-256>"
  }
}
```

| 規則 | 行為 |
| --- | --- |
| 同一買方 × 同一物件 | 只通知一次；第二次起走 `notification_suppressed_duplicate` 稽核事件 |
| 買方條件變更 | 指紋不符 → **清空該買方的整批去重紀錄** → 既有物件全部重新比對 |
| 首次見到的買方 | 沒有舊指紋，**不算變更**（否則新買方會被誤記成「條件剛改」） |
| 只有真的送達才記去重 | `is_notified` 為 False（發送失敗）時不寫入，避免推薦其實沒發出去卻被永久擋掉 |
| 狀態檔損毀 | **明確拋錯**，不靜默重置——靜默重置會讓所有買方在同一天被重複通知一輪 |
| 寫入方式 | 先寫 `.tmp` 再 `replace()`，中途中斷不會留下半截 JSON |

---

## 稽核軌跡（`audit.py`）

`state/audit.jsonl`，**append-only JSONL**，每次執行共用一個 `run_id`：

| 事件 | 何時寫 |
| --- | --- |
| `run_started` | 載入資料後、法遵檢查前 |
| `compliance_check_passed` | 白名單與受保護詞根雙關卡通過 |
| `preflight_passed` / `preflight_failed` | 對外通訊預檢（非 dry-run 時） |
| `criteria_changed` | 偵測到買方條件變更並清空去重紀錄 |
| `notification_sent` | 每封推薦，含分數／分級／命中與未命中欄位／管道／時效 |
| `notification_suppressed_duplicate` | 去重擋下 |
| `match_below_threshold` | 逼近門檻（Strong）卻未達推播標準 |
| `sla_breach` | 逾 60 分鐘時效 |
| `vendor_pack_generated` | 產出降價談判包 |
| `run_completed` | 收尾統計 |

**為什麼自行實作而不放進 `_shared/`**：`_shared/` 是十個 demo 共用的**凍結契約**，
本模組的稽核義務源自公平住房法，是 Level 3 房地產專屬需求；
放進 `_shared/` 會逼另外九個模組一起改。

**為什麼 JSONL 不用 JSON 陣列**：稽核軌跡的價值在於不能被改。JSONL 每行獨立，
中途當掉最多壞掉最後一行；JSON 陣列要重寫整個檔案，一次中斷整份失效。
保留欄位（`ts` / `run_id` / `module` / `event`）不允許被呼叫端覆寫，否則軌跡可被偽造。

---

## 設定（`config.yaml`）

| 欄位 | 說明 |
| --- | --- |
| `matching.match_score_threshold` | 推播門檻，預設 `80`（規格值） |
| `matching.perfect_score_min` / `strong_score_min` | 分級門檻，預設 `90` / `75`（規格值） |
| `matching.require_all_hard_matches` | `true` 時任一 Hard 條件未命中即不推播。**預設 `false` = 純加權**（照規格字面），此旗標為實作補充 |
| `matching.notify_sla_minutes` | 通知時效，預設 `60`（規格值） |
| `matching.allowed_criteria_fields` | 🔴 法遵白名單，見上方專節 |
| `vendor_pack.*` | 分支 B 觸發條件：7 日詢問數 <= `2` 且上架 >= `14` 天 |
| `engagement.calendly_url` | 高優先級推薦附加的看屋預約連結（走環境變數） |
| `sources.mock_now` | mock 模式的固定「現在」，讓時效判定可重現 |

金額與坪數在 YAML 中一律寫成**字串**（`"26800000"`），避免 YAML 轉 float 掉精度；
程式內全程 `decimal.Decimal`，不經 float。

---

## Financial Model

### 客戶端

| 項目 | 數字 |
| --- | --- |
| 一次性建置 | **$3,500** |
| 每月訂閱 | **$1,400** |
| 部署時間 | **1 週** |
| 匹配率 | **100%** 條件匹配（人工 **10–15%**） |
| 通知速度 | **60 分鐘內**（人工 **1–3 天**） |
| 首年成本 | $3,500 + $16,800 = **$20,300** |

ROI 口徑：原簡報以**匹配率與速度**論證價值，未給出金額化的節省數字。
按房仲業常見的成交佣金結構，只要一年多成交一件因漏接而流失的案子，
首年成本即回收——但這是**推論，不是原簡報數字**，提案時請用客戶自己的平均佣金試算。

### ⚠️ 定價口徑差異（Level 3 全域裁決）

| 來源 | 建置 | 月費 | 部署 |
| --- | --- | --- | --- |
| **模組頁 `ch07_p08` / `apxG_p12`（本模組採用）** | **$3,500** | **$1,400/mo** | 1 週 |
| Commercial Matrix `apxG_p20` | $8,000–$10,000 | $4,000–$6,000/mo | 1–2 週 |

`apxG_p20` 把 #21–#30 **十個模組全部**列為同一組數字，明顯是套用模板佔位值。
**定價以模組頁為準**；Commercial Matrix 視為銷售話術用的「價格帶」。

### 內部回收（顧問端）

**（原簡報未提供）** — 36 張投影片沒有任何一頁提供顧問自己的內部建置工時回收數字，
全部 ROI 數字皆為客戶端節省。`config.yaml` 的 `recovered_hours_per_month` 以 `0` 佔位，
**不推估**。

### 服務商端

| 客戶數 | Setup 收入 | MRR | 年化 |
| --- | --- | --- | --- |
| 1 | $3,500 | $1,400 | $16,800 |
| 5 | $17,500 | $7,000 | $84,000 |
| 10 | $35,000 | $14,000 | $168,000 |

---

## 客戶見證

原簡報 `ch07_p08` 的原文引述：

> **Sarah Chen 案例**：系統被完美改裝應用於 B2B『批發分銷 (Wholesale)』。
> 新產品線發布時，將相關批發商通知率從 **35%** 瞬間提升至 **100%**。

`mock/buyers.json` 的 `B-104` 就是照這個案例建的批發商角色，
同時用來示範去重：`mock/state_seed.json` 已記錄它收過 `L-001`，重跑時必須被擋下。

---

## Client Pitch

> **「將入口網站的冷冰冰警報，升級為專屬經紀人的個人化推薦信。」**
> —— `ch07_p08` 原文

**開場**：你們的 CRM 裡有幾百組買方條件。上一次有物件上架時，
系統通知了其中幾組？

**痛點**：多數答案是「業務當天想得起來的那幾個」——**10–15%**。
剩下 85% 不是不符合，是沒人記得。每一個未成功的媒合，都是流失的營收。

**方案**：$3,500 建置、$1,400/月。上架 **60 分鐘內**，
100% 的條件匹配買方都會收到一封寫著「為什麼這間適合你」的信，
而且同一個人不會被同一間房打擾第二次。

**風險反轉**：第一個月照跑，我們用你們過去 90 天的上架紀錄回放一次，
把「當時應該通知但沒通知」的名單列給你看。名單是空的就退費。

**法遵加值**：所有媒合條件都過公平住房法白名單，每一筆推薦都有稽核軌跡
記錄「依據哪些客觀欄位推給誰」。這不只是自動化，是可被主管機關查核的自動化。

---

## 檔案結構

```
demo25-client-matching/
├── README.md                       # 本檔
├── config.yaml                     # 比對權重 / 門檻 / 法遵白名單 / 資料源
├── main.py                         # 主流程（--mock/--live/--dry-run/--state-file/--audit-file）
├── matcher.py                      # 法遵閘門 + 加權評分 + 去重狀態檔
├── audit.py                        # append-only JSONL 稽核軌跡
├── prompts/
│   ├── recommendation_email.md     # 個人化推薦信（含法遵紅線）
│   └── vendor_pricing_pack.md      # 低詢問度物件的降價談判包
├── mock/
│   ├── listings.json               # 5 物件：完全命中 / 逾時效 / 完全不命中 / 低詢問度 / 部分命中
│   ├── buyers.json                 # 5 買方：含 B2B 批發商與「條件已變更」角色
│   ├── buyers_noncompliant.json    # 🔴 違規條件範例（僅供測試驗證被擋）
│   ├── state_seed.json             # 模擬「上一次執行留下的狀態」，驗證去重
│   ├── recommendation_fixture.md   # mock 模式的推薦信回應（零成本）
│   └── vendor_pack_fixture.md      # mock 模式的降價談判包回應
├── state/                          # 執行後自動建立（notifications.json / audit.jsonl）
└── test_main.py                    # happy / edge（法遵 + 去重）/ integration（自主權 + 稽核）
```

**為什麼違規範例另外開一個檔**：`python main.py --mock` 必須零憑證跑完並印出結果
（契約 §9 驗收條件）。違規資料若混進 `buyers.json`，預設執行會直接 exit 1。
分開存放後，兩件事都成立：預設執行是綠的，法遵閘門也有資料可驗。

---

## 設計決策（為什麼這樣寫）

### 1. 法遵閘門在最前面，而且是硬失敗

法遵檢查排在載入資料之後、**任何比對與對外通訊之前**。
理由見上方專節：靜默略過違規欄位比當場失敗更危險。

### 2. 未命中條件必須寫進推薦信

`prompts/recommendation_email.md` 強制第二段列出 `UNMATCHED_CRITERIA`。
隱瞞落差能拉高開信率，但會在看屋現場爆炸，
而且讓「100% 匹配」這個賣點變成謊言。

### 3. 去重只在「真的送達」時寫入

`_deliver` 回傳的 `is_notified` 為 False（例如 Telegram API 失敗）時不記去重。
否則一次網路故障會讓那批買方**永久**收不到那些物件。

### 4. `--dry-run` 就是安全閥

`apxG_p03` 要求「所有 API 呼叫前必經 `--dry-run` 內部通訊測試」。
本模組的實作：非 dry-run 執行會先跑一次不觸網的通道預檢
（建構 Notifier + 走一次 `split_message`），失敗即紅色警報中止。

### 5. 預設 `draft` 自主權

推薦信是直接寄到買方信箱的對外通訊，誤送成本高且受法規檢視。
預設 `draft`：推薦只印在本機等業務過目。
要開 `supervised_auto` 必須填 `approved_senders` 白名單，
且書中鐵律要求先在草稿模式跑滿 14 天——未滿會留下 AMBER 警告。

### 6. `run()` 回傳鍵採契約建議的標準命名

契約 §6 的「已知技術債」段落建議把
`module_id` / `module_name` / `mode` / `dry_run` / `warnings` / `amber_count`
定為標準回傳欄位。本模組是新寫的，直接照建議命名，
讓 `bundle-quickstart/run_all.py` 的轉接層不必為它多寫一組 `.get()` 分支。

---

## 已知限制

| 限制 | 說明 |
| --- | --- |
| 比對是 O(物件 × 買方) | 5×5 = 25 組沒問題；上萬買方時需先用 Hard 條件做索引預篩 |
| `required_features` 是全有全無 | 要求 3 項特色只中 2 項算完全未命中，不給部分分數 |
| 簡訊未實際串接 | 高優先級只在通知內容標記「＋同步標記發送簡訊提醒」，未接簡訊供應商 |
| Calendly 連結是單一固定網址 | 未依物件或業務分流；環境變數未設定時記 AMBER 並略過 |
| mock 模式所有推薦信內容相同 | 讀同一份 fixture，零成本但無法看出個人化差異；`--live` 才會逐封生成 |
| 每封推薦各呼叫一次 LLM | `--live` 時成本與推播數量成正比，大名單請先評估 |
| 郵遞區號 redlining 擋不住 | 技術上無法分辨是買方自填還是業務代填，只能靠稽核軌跡事後查核 |
| 時區依賴 `listed_at` 的標註 | 無時區資訊的時間戳一律視為 UTC，資料源請務必帶時區 |

---

## 依賴

- Python 3.10+
- PyYAML（設定檔）／pytest（測試）
- `_shared/`：`llm_client` / `notifier` / `autonomy` / `diagnostics` / `config_loader`
- 其餘一律標準庫（`urllib.request` / `decimal` / `hashlib` / `json` / `dataclasses`）
