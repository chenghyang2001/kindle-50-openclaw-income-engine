# 模組 #30 — 白牌「AI 營運部」客戶經銷方案

> **That is not an automation. That is a business model.**
> 部署 **2 週**（全書唯一非 1 週的模組）｜客戶 **$8,000–$10,000 setup + $4,000–$6,000/mo**
> 下游子客戶 **$1,000–$2,000/月/家**｜分潤 **20-30% 經銷商 / 70-80% 基礎設施提供者**
> 內部回收工時：**（原簡報未提供）**

這是全書最後一個模組，也是商業模式的頂點（ch07_p15）。
前 29 個模組賣的是自動化；這一個賣的是**基礎設施授權**——
客戶變成經銷商，掛自己的品牌把系統轉售給他的上下游供應鏈，
你退到幕後當「隱形的引擎室（Invisible engine room）」。

---

## 一、線性 vs 指數：這個模組要解決的到底是什麼

```
Before（線性成長）
  You ──→ Client A ──(Referral)──→ Client B
                                     │
                              Restart from zero ✕
  「沒有槓桿。你為每一個新客戶從零建置，收入受限於你的時間。」

After（一對多指數槓桿）
  You ──→ Infrastructure Layer ──→ Client A (Reseller)
                                        ├──→ Sub-Client 1
                                        ├──→ Sub-Client 2
                                        ├──→ Sub-Client 3
                                        ├──→ Sub-Client 4
                                        └──→ Sub-Client 5   （5-10 家）
  第一個白牌客戶產生 5 個子客戶 = $10k–$15k/月經常性收入，無額外業務開發成本
```

**關鍵洞察**：你沒有多做一份工，卻多了五份收入——因為賣掉的不是工時，是基礎設施的使用權。

---

## 二、Before / After

| | **Before（逐案交付）** | **After（白牌基礎設施）** |
| --- | --- | --- |
| 交付單位 | 一個客戶一次專案 | 一套基礎設施授權給經銷商 |
| 新客戶成本 | 從零建置，重跑一次完整流程 | 經銷商自行銷售，你只多開一個 namespace |
| 收入結構 | 專案費 + 單一月費 | Setup + 月費 + **每個子客戶的 70-80% 分潤** |
| 品牌 | 你的品牌 | **經銷商的品牌**（你完全隱形） |
| 業務開發 | 你自己跑 | 經銷商用他既有的供應鏈關係跑 |
| 時間與收入 | 線性綁死 | **解耦**（Decoupled MRR） |
| 資料邊界 | 單一客戶，不用想 | **多租戶隔離是生死線** |
| 你的角色 | 承包商 | 基礎設施提供者 |

---

## 三、本模組最重要的技術要求：多租戶隔離

規格原文（apxG_p19）：
`Client A = [RESELLER_SLUG]/[SUB_CLIENT_SLUG_A]`、`Client B = [RESELLER_SLUG]/[SUB_CLIENT_SLUG_B]`，
「**確保資料絕對隔離**」。

### 為什麼這件事比其他 29 個模組的任何要求都嚴重

一般模組出錯，是**你和你的客戶**之間的事。
白牌模組出錯，外洩的是**經銷商的客戶資料**——毀掉的是兩層信譽：
經銷商對他客戶的承諾，以及你對經銷商的承諾。而且兩層都不可逆。

### 實作的四條規則（`tenancy.py`）

| 規則 | 實作位置 | 行為 |
| --- | --- | --- |
| 所有資料存取都必須帶 namespace | `TenantStore.read(actor, target)` | 刻意不提供「不帶 actor」的讀取方法。留一條後門，日後就一定有人走 |
| 跨租戶一律拒絕並留痕 | `TenantStore._deny()` | 先寫稽核（severity=red）再拋 `IsolationViolation` |
| 解析失敗 / 缺失 / 跳脫字元 → 拒絕，**不修正** | `parse_namespace()` | 連 `.strip()` 和 `.lower()` 都不做。修正等於幫攻擊者把畸形輸入補成合法輸入 |
| 路徑組合防跳脫 | `safe_child_path()` | 先組合、`resolve()`、再驗證仍在允許根目錄內（`is_relative_to`） |

### 隔離是**兩層**比對，不是只看子客戶名稱

mock 資料刻意讓兩個經銷商底下都有一個 `north-mill`：

```
acme-ops/north-mill        ← North Mill Manufacturing
brightpath-ai/north-mill   ← North Mill Logistics（完全無關的另一家公司）
```

只比對子客戶 slug 的實作會把這兩家混在一起。
`TenantStore.read()` 依序檢查經銷商層級 → 是否指定子客戶 → 子客戶層級，三關都過才讀檔，
讀完還要驗資料檔內宣告的 `namespace` 與請求一致（防資料被錯放到別人的目錄）。

### 「拒絕執行並發 RED」的兩種情境（刻意不同）

| 來源 | 行為 | 理由 |
| --- | --- | --- |
| `--tenant` 旗標 | **RED + 結束行程**，且不可用設定關掉 | 這是驅動本次執行的**身分**。把畸形身分當萬用字元是最糟的失敗模式 |
| `mock/access_attempts.json` 演練清單 | 拒絕該筆 + 稽核 red + AMBER 提示，流程繼續 | 那份清單是**刻意重放的敵意輸入**，目的就是證明每一筆都被擋下並留痕。擋一筆就中斷反而看不到完整演練結果 |

正式環境要改成「一違規就中斷 + 呼叫值班」時，把 `safety.halt_on_isolation_violation` 設為 `true`。

---

## 四、品牌層：整組抽換，不是貼補丁

架構沿用 #21 的 Swarm 機制（apxG_p03）：
Orchestrator 持有 `brand_context.yml`，所有子智能體 `INHERIT_FROM_ORCHESTRATOR: true`，
對品牌上下文的更新會**瞬間級聯**到所有輸出——單一真理來源。

```
brand_context.yml（提供者的預設品牌 + BRAND_SUBSTITUTION_PROTOCOL）
        │
        ├── acme-ops       brand_overrides ──→ Acme Operations Cloud（綠色系，製造物流語氣）
        └── brightpath-ai  brand_overrides ──→ BrightPath Operations Suite（靛色系，醫療語氣）
```

兩個防呆設計：

1. **覆蓋白名單**：只有 `brand_substitution_protocol.fields` 列出的欄位可被經銷商覆蓋。
   否則經銷商能透過設定檔改到品牌以外的行為（例如分潤比例）。
2. **外洩掃描**：每一份對外月報都掃 `forbidden_leaks`（`OpenClaw` / `Income Engine` /
   `infrastructure provider` / `基礎設施提供者`）。命中即 AMBER + 稽核紀錄。
   白牌承諾的是「顧問是隱形的引擎室」，提供者的名字漏出去，經銷商的客戶關係就沒了。

---

## 五、⚠️ 原著數字衝突與判定

### 5a. 分潤比例：20-300% vs 20-30%

| 來源 | 印出的數字 |
| --- | --- |
| ch07_p14 | 經銷商 (Client A) 抽取 **20-300%** 維護費 |
| apxG_p19 | **20-30% 經銷商** / **70-80% 基礎設施提供者** |

**判定：採用 20-30% / 70-80%。**

理由有三，任何一條單獨成立即可推翻 20-300%：

1. **算術**：ch07_p14 同一頁的對側寫「你獲得 70-80%」。兩邊必須相加為 100%，
   20-300% 與 70-80% 無論怎麼取值都湊不出 100%。
2. **語意**：分潤是「把一筆月費分成兩份」，任一方的份額不可能超過 100%。
3. **一致性**：apxG_p19 是專門講分潤模型的頁面（SLA 與利潤分配模型），
   ch07_p14 是章節摘要頁。專門頁的數字優先於摘要頁。

`revenue_share.py` 的 `SplitPolicy.validate()` 把這條判定寫死成程式檢查：
比例可以在 `config.yaml` 調整，但 `reseller_pct + provider_pct` **不等於 100 就拒絕執行**。
落在 20-30% 帶外不擋（合約可談），但一定發 AMBER 要求回查經銷商協議。

### 5b. 定價：模組頁 vs Commercial Matrix

apxG_p20 的「Level 3 Commercial Matrix」把 #21–#30 **十個模組全部**列為
`建置報價 [$8,000-$10,000]` + `月訂閱費 [$4,000-$6,000]` + `Deploy in 1-2 weeks`——
十欄數值完全相同，明顯是**模板佔位值**。

**本模組以模組頁（ch07_p13/p14、apxG_p18）為準**，
恰好 #30 的模組頁數字與矩陣相同（$8,000–$10,000 / $4,000–$6,000），因此本模組無實質差異；
但同一份 SPEC 下的其他模組會有落差，Commercial Matrix 只能當「銷售話術用的價格帶」。

### 5c. 內部回收工時

原簡報 36 張投影片（ch07_p01–p15、apxG_p01–p21）中**沒有任何一頁**提供
「顧問自己的內部建置工時回收」數字，全部 ROI 皆為客戶端節省。

`config.yaml` 的 `recovered_hours_per_month` 一律留 `null`，
程式輸出「（原簡報未提供）」。**不推估、不類比其他模組、不填 0。**

### 5d. 客戶見證

**（原簡報未提供具名見證）**。ch07_p13/p14 與 apxG_p18/p19 都沒有客戶引述。
此處不編造任何見證文字。

---

## 六、Financial Model

| 項目 | 金額 | 來源 |
| --- | --- | --- |
| 初始包裝建置費（Setup） | **$8,000–$10,000** | ch07_p03、apxG_p18 |
| 經銷商月費（Reseller retainer） | **$4,000–$6,000/mo** | apxG_p18 |
| 下游子客戶月費 | **$1,000–$2,000/月/家** | ch07_p14 |
| 分潤 — 經銷商 | **20–30%** | apxG_p19（見 5a） |
| 分潤 — 基礎設施提供者 | **70–80%** | apxG_p19 |
| 天花板突破 | 第一個白牌客戶產生 **5 個子客戶 = $10k–$15k/月**經常性收入，無額外業務開發成本 | ch07_p14 |
| 內部回收工時 | **（原簡報未提供）** | — |

### 本模組 mock 資料的實算（`--mock` 可完整重現）

| 租戶 namespace | 子客戶月費 | 經銷商 25% | 提供者 75% |
| --- | ---: | ---: | ---: |
| `acme-ops/north-mill` | 1,500.00 | 375.00 | 1,125.00 |
| `acme-ops/harbor-freight-co` | 2,000.00 | 500.00 | 1,500.00 |
| `brightpath-ai/lakeside-clinic` | 1,200.00 | 300.00 | 900.00 |
| `brightpath-ai/north-mill` | 1,800.00 | 450.00 | 1,350.00 |
| **合計** | **6,500.00** | **1,625.00** | **4,875.00** |

金額一律 `decimal.Decimal`。只對經銷商那份做四捨五入，提供者拿「總額減經銷商」的餘數——
兩邊各自 `quantize()` 會製造分位差，而每月對帳差一分錢，
經銷商就會質疑整套基礎設施的可信度，那正是白牌模式唯一的資產。

---

## 七、SLA 責任分界（分潤線就是責任線）

apxG_p19 把分潤與 SLA 畫在同一張圖上不是排版巧合：**拿多少分潤，就承擔對應那一側的故障。**

| 故障情境 | 責任方 | 為什麼 |
| --- | --- | --- |
| 子客戶抱怨「這不是我要的功能」 | **經銷商（20-30%）** | 需求溝通與期望管理是第一線客戶關係 |
| 子客戶欠費、續約流失 | **經銷商** | 帳務與續約在經銷商合約側 |
| 子客戶提供的來源資料本身就是錯的 | **經銷商** | 資料正確性屬於客戶自身作業 |
| 子客戶要求新增自動化 | **經銷商發起 → 提供者評估** | 銷售在前，交付在後 |
| 系統當機、可用率不足 | **提供者（70-80%）** | 基礎設施維運 |
| 跨租戶資料外洩 | **提供者** | 隔離是基礎設施的核心承諾 |
| 模型輸出品質下降、提示詞退化 | **提供者** | 模型與提示詞由提供者持有 |
| 第三方 API 改版導致整合中斷 | **提供者** | 整合層維護 |
| 品牌層錯置（輸出掛錯品牌） | **提供者** | `BRAND_SUBSTITUTION_PROTOCOL` 由提供者實作 |
| 經銷商自行修改設定造成的異常 | **經銷商** | 覆蓋白名單之外的欄位本來就不開放 |

`prompts/reseller_sla_brief.md` 要求對帳說明**依輸入的 `owner` 欄位據實歸屬，不得替任一方緩頰**。

---

## 八、前提門檻：90 天 + 4 個方案

apxG_p18 的前提條件橫幅與 ch07_p14 的觸發門檻是兩道獨立的閘：

| 門檻 | 值 | 意義 |
| --- | --- | --- |
| 穩定運行 | **90+ 天** | 未達門檻不得進入白牌模式 |
| 已部署企業級方案 | **4 個以上** | 「你建立的就不再是零件，而是一個活生生的 AI 營運基礎設施」 |

程式檢查兩者（`_check_whitelabel_gate`），未達標時發 AMBER + 稽核紀錄但不中斷執行——
demo 仍要能跑，而書中把 90 天寫成「前提條件」：跳過它去白牌化，等於拿信譽賭運氣。

---

## 九、全域安全閥：對外呼叫前必經內部 `--dry-run`

apxG_p03 的強制要求：「所有 API 呼叫前必經 `--dry-run` 內部通訊測試」。

本模組的實作（`_selftest`）在**建立任何 LLM / Notifier 之前**執行，全程零網路：

1. 每個選定租戶都以**自己的身分**讀一次自己的資料
2. 確認 namespace 能對上、資料檔存在
3. 確認資料檔內宣告的 `namespace` 與請求一致

任一項失敗 → 稽核 red + `Diagnostics.red` 結束行程，**不准送出任何一個封包**。
`config.safety.require_dry_run_selftest` 設成 `false` 不會被接受，程式強制覆寫為 `true` 並發 AMBER。

---

## 十、稽核軌跡（`audit.py`）

模組目錄下的 append-only JSONL（預設 `audit.jsonl`，可用 `--audit-file` 覆蓋）。

**為何自行實作而不改 `_shared/`**：稽核需求是多租戶白牌特有的，
其餘 29 個模組不需要。把它塞進凍結的共用契約，等於讓所有模組為單一模組轉向。

**為何是 JSONL 而不是單一 JSON 陣列**：稽核日誌只會被追加、不會被重寫。
每一行都是獨立完整的紀錄，程式中途被砍也只損失最後一行，不會讓整份日誌變成無法解析的殘檔。

| 事件 | severity | 何時寫入 |
| --- | --- | --- |
| `whitelabel_gate` | green / amber | 90 天與 4 方案門檻檢查 |
| `revenue_policy_loaded` / `revenue_policy_invalid` | green / amber / red | 分潤政策載入與驗證 |
| `dry_run_selftest` | green / red | 全域安全閥結果 |
| `tenant_read` | green | 每一次成功的同租戶讀取 |
| `cross_tenant_denied` | **red** | 每一次被擋下的跨租戶存取 |
| `namespace_rejected` | **red** | namespace 解析失敗 / 缺失 / 含跳脫字元 |
| `tenant_not_found` | red | `--tenant` 指到不存在的租戶 |
| `whitelabel_report_built` | green / amber | 每份白牌月報產出（amber = 偵測到品牌外洩） |

mock 模式使用固定時鐘（`config.mock.now`），輸出完全可重現。

---

## 十一、使用方式

```bash
# 零憑證、零網路跑完整流程（含隔離演練）
PYTHONUTF8=1 python main.py --mock

# 只跑單一經銷商（所有子客戶）
PYTHONUTF8=1 python main.py --mock --tenant acme-ops

# 只跑單一子客戶
PYTHONUTF8=1 python main.py --mock --tenant acme-ops/north-mill

# 稽核與狀態檔導向別處（不污染模組目錄）
PYTHONUTF8=1 python main.py --mock \
  --audit-file /tmp/demo30-audit.jsonl \
  --state-file /tmp/demo30-state.json

# 跑完流程但不實際發送（也不寫狀態檔）
PYTHONUTF8=1 python main.py --mock --dry-run

# 串真實 API（需 ANTHROPIC_API_KEY；安全閥仍會先跑一次內部 dry-run）
PYTHONUTF8=1 python main.py --live --notify telegram
```

### 旗標

| 旗標 | 預設 | 說明 |
| --- | --- | --- |
| `--mock` / `--live` | `--mock` | 互斥。`--live` 缺 `ANTHROPIC_API_KEY` 直接 RED 退出，不靜默降級 |
| `--dry-run` | 關 | 跑完流程不發送、不寫狀態檔 |
| `--notify` | `console` | console / telegram / gmail / line / whatsapp |
| `--config` | `./config.yaml` | 設定檔路徑 |
| **`--tenant`** | 全部 | `reseller-slug` 或 `reseller-slug/sub-client-slug`。解析失敗 → RED 結束 |
| **`--state-file`** | `./state.json` | 覆蓋 `config.state.file` |
| **`--audit-file`** | `./audit.jsonl` | 覆蓋 `config.audit.file` |

---

## 十二、檔案結構

```
demo30-whitelabel-reseller/
├── README.md                    # 本檔
├── config.yaml                  # 模組設定（門檻 / 分潤 / 隔離根目錄 / 安全閥）
├── brand_context.yml            # 品牌單一真理來源 + BRAND_SUBSTITUTION_PROTOCOL + STAGE_MAP
├── main.py                      # 主流程
├── tenancy.py                   # 多租戶隔離（namespace / 跨租戶拒絕 / 路徑跳脫防禦）
├── revenue_share.py             # 分潤計算（Decimal，總和強制 100%）
├── audit.py                     # append-only JSONL 稽核軌跡
├── prompts/
│   ├── whitelabel_monthly_report.md   # 白牌月報（署名經銷商，禁提轉售）
│   ├── brand_layer_checklist.md       # BRAND_LAYER 驗收檢查表
│   └── reseller_sla_brief.md          # 分潤與 SLA 對帳說明
├── mock/
│   ├── tenants.json                   # 2 經銷商 × 2 終端客戶
│   ├── access_attempts.json           # 隔離演練清單（含跨租戶與跳脫字元嘗試）
│   └── data/
│       ├── acme-ops/{north-mill,harbor-freight-co}.json
│       └── brightpath-ai/{lakeside-clinic,north-mill}.json
└── test_main.py                 # 3 個測試（happy / edge / integration）
```

---

## 十三、測試

複雜度 **中等**，3 個測試（CONTRACT §8）：

| 測試 | 驗什麼 |
| --- | --- |
| `test_happy_path` | 2 經銷商 × 2 子客戶全數處理、安全閥 4/4 通過、品牌各自抽換、無外洩、稽核與狀態檔落地、內部回收工時如實標示未提供 |
| `test_edge_case_namespace_escape_is_rejected` | `--tenant acme-ops/../brightpath-ai` → RED 結束（exit 1）、稽核留下 `namespace_rejected`、**任何租戶資料都還沒被讀取**、解析器本身也拒絕而非「洗乾淨後放行」 |
| `test_integration_cross_tenant_denied_and_split_exact` | **A 經銷商讀 B 經銷商的資料必須被拒**（4 筆演練全擋 + 直呼資料層再驗一次）、分潤每一筆與合計分毫不差、自主權白名單命中才送出未命中降級草稿、每次拒絕都留 red 稽核 |

```bash
PYTHONUTF8=1 python -m pytest test_main.py -v
```

測試全程離線，不呼叫任何真實外部 API。

---

## 十四、Client Pitch

> 「這不是一個自動化方案。這是一個全新的商業模式。」
> （That is not an automation. That is a business model.）

給經銷商的三句話：

1. **你不用建任何東西。** 系統已經穩定運行 90 天以上，你拿到的是可立即轉售的成品。
2. **它掛的是你的名字。** 從報告抬頭、客服信箱到主題色，全部是你的品牌，你的客戶只認識你。
3. **你賣得越多，你賺得越多，而你不需要多請一個人。** 5 個子客戶就是 $10k–$15k/月，
   而你的成本只有分潤那一份。

給你自己（基礎設施提供者）的一句話：

> 「Automation 30 是整本手冊中最具戰略意義的方案。它徹底打破了『時間與收入的線性關係』，
> 產生無限擴展的基礎設施收入。」（apxG_p21）
>
> 真正的終局不是寫出最好的 Prompt，而是建構複利成長的 AI 基礎設施。

**客戶見證**：（原簡報未提供具名見證）
