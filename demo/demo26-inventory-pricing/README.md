# demo26 — 電商庫存與定價最佳化

> 《The OpenClaw Income Engine》第 07 章 · 附錄 G · 自動化 **#26** · **Level 3 企業級**
> 部署 **1 週**｜內部回收 **（原簡報未提供）**｜售價 **$3,800 setup + $1,600/mo**
> 部門：營運部門 · E-com｜技術堆疊標籤：`ECOM` `CRM` `SaaS`
> 來源頁：ch07_p09、apxG_p13、apxG_p14

每天一次，Agent 掃過所有在售 SKU：算流速、算可售天數、分五級狀態，
再把「庫存水位 × 對手定價」丟進決策矩陣，產出**調價草稿**與**滯銷品清倉企劃**。

它不會自己按下改價鍵。**它只會在你按之前，把該知道的事全部算好。**

---

## Before / After

| | Before（人工盯場） | After（預測引擎） |
| --- | --- | --- |
| **反應速度** | 「等人類注意到趨勢時，趨勢已經發生**三週**了」 | 每日監控，**Day 1 → Day 14 就發出警報** |
| **滯銷處理** | 季末大清倉，一次性破壞利潤 | 滯銷達門檻天數即觸發 `promotional_brief_generator` |
| **缺貨處理** | 熱銷品**缺貨才手動叫貨** | 可售天數低於補貨點就先示警（Day 1 起算） |
| **定價依據** | 憑印象、憑客戶抱怨 | 內部銷售流速 × 外部需求訊號 × 對手價，三者交叉 |
| **改價風險** | 人手動改，改錯就是虧本賣 | 三道安全閥（不低於成本／幅度上限／一律 DRAFT） |
| **可稽核性** | 「當初為什麼降這個價？」沒人記得 | 每一筆決策落 JSONL 稽核軌跡，含雜湊串鏈 |

---

## ⚠️ 原著衝突與採用理由（滯銷天數）

原著兩處數字不一致，如實記錄：

| 來源頁 | 原文 | 天數 |
| --- | --- | --- |
| **ch07_p09**（第 07 章模組頁） | 「滯銷品在第 **21** 天觸發促銷」 | 21 |
| **apxG_p14**（附錄 G 規格頁） | 「滯銷 **14 天以上** → 觸發 `promotional_brief_generator`」 | 14 |

**本專案採用 14 天。**

理由（主 Claude 裁決）：附錄 G 是**逐模組的工程規格表**（同一頁同時給出
`pricing_rules` JSON、STATUS 五級分類與觸發條件），第 07 章模組頁是**銷售敘事**用的
Before/After 時間軸。當敘事頁與規格頁衝突時，以規格頁為準——這與 Level 2 的取捨原則一致。

**天數可設定**：`config.yaml` 的 `inventory.slow_mover_days`。
想照第 07 章的說法跑就改成 `21`，程式不會有任何硬編碼假設。

---

## Level 3 全域裁決在本模組的落地

| 裁決 | 本模組怎麼做 |
| --- | --- |
| **定價以模組頁為準** | 採 `$3,800 setup + $1,600/mo`（ch07_p03 / apxG_p13）。apxG_p20 的 Commercial Matrix 把 #21–#30 十欄**全部**列為 `$8,000-$10,000` + `$4,000-$6,000`，該表為銷售話術用的價格帶模板，非本模組實價 |
| **內部回收未提供不推估** | `config.yaml` 的 `recovered_hours_per_month` 固定為 `null`，README 一律寫「（原簡報未提供）」。36 張投影片的 ROI 數字**全部是客戶端節省**，沒有一頁提到顧問自己的內部回收工時 |
| **客戶見證原文引述** | 見下方「客戶見證」，逐字引用 Sarah Chen 案例，未補充任何原簡報沒有的細節 |
| **全域安全閥** | `--live` 模式在**任何對外 API 呼叫之前**強制執行 `_preflight_dry_run()` 內部通訊測試（apxG_p03：「所有 API 呼叫前必經 `--dry-run` 內部通訊測試」） |
| **稽核軌跡** | 模組目錄下的 `audit/pricing_audit.jsonl`，由自建的 `audit.py` 寫入，**未改動 `_shared/`** |

---

## 🔒 定價安全鐵律（本模組最重要的一節）

自動調價是全套 30 個自動化裡風險最高的動作。
一個小數點錯誤就可能虧本賣光整批庫存，而且**錯誤是不可逆的**——
訂單成立之後，你沒辦法回頭跟客人說「抱歉那個價格是 bug」。

因此本模組的預設立場是**不動作**，並設下三道閘門：

### 閘門 1：絕不低於成本價（程式層硬檢查）

```
建議價 <= 成本價          -> 一律拒絕，發出 RED，升級人工
建議價 <  成本 x (1+最低毛利) -> 拒絕，發出 AMBER，升級人工
現售價 <= 成本價（負毛利）  -> 該 SKU 完全不進定價流程，直接 RED
```

這一條**不受設定檔影響**：`min_margin_percent` 可以調，但「不得低於成本」寫死在
`pricer._check_rails()` 裡。設定檔能調鬆的東西，遲早會被調鬆。

### 閘門 2：單次變動幅度上限

`config.yaml` 的 `pricing.max_price_change_percent`（預設 **10%**）。
超過即拒絕自動執行並升級人工——例如本模組的 mock 資料中，
`SKU-1006` 積壓 320 天、矩陣建議清理 -20%，就**被這道閘門擋下**，
系統保留「本來想改成 $60.00」供人判斷，但不會自己執行。

還有第二層：`max_price_change_ceiling`（預設 30%）限制
`max_price_change_percent` 本身能被設到多大。**把安全閥調鬆這件事，本身也要被審查。**

### 閘門 3：一律 DRAFT，人工核准才寫回平台

所有通過前兩道閘門的建議，`approval_state` 都是 `DRAFT`。
程式**不存在**「自動寫回 Shopify 售價」的程式碼路徑。
自動化省下的是「發現問題的時間」，不是「決定要不要改價的責任」。

### 附帶的兩道保守規則

| 規則 | 為什麼 |
| --- | --- |
| **缺貨中不調價** | 庫存 0 時該做的是補貨，改價毫無意義 |
| **沒有對手報價就不動價** | 沒有比價依據的調價等於瞎猜 |

---

## STATUS 五級分類（逐字實作 apxG_p14）

```
REORDER_URGENT      : days_on_hand < reorder_point
REORDER_RECOMMENDED : days_on_hand < reorder_point * 1.5
SLOW_MOVER          : velocity in bottom {{SLOW_MOVER_PERCENTILE}}% for {{SLOW_MOVER_DAYS}} days
OVERSTOCK           : days_on_hand > {{OVERSTOCK_DOH}}
HEALTHY             : none of the above
```

**判定順序本身就是規格。** 一個滯銷 30 天但今天剛好賣光的 SKU，
該做的事是「補貨」而不是「打折清倉」——順序調換會讓建議完全相反。

可售天數的兩個特例刻意不用「除以 0 就給個大數字」帶過：

| 情況 | `days_on_hand` | 意義 |
| --- | --- | --- |
| 庫存 0 | `0` | 缺貨，最急 |
| 流速 0 且有庫存 | `None` | 賣不動，可售天數在數學上是無限大 |

---

## 定價 × 庫存決策矩陣（apxG_p14）

| 庫存水位 ＼ 對手定價 | Undercut | Neutral | Above |
| --- | --- | --- | --- |
| **Slow Mover** | **建議降價匹配對手 -1%** | （原圖空白 → HOLD） | 保持現狀 |
| **Fast Mover** | （原圖空白 → HOLD） | 建議促銷 | **建議調漲** |
| **Overstock** | （原圖空白 → HOLD） | 建議清理 | 建議清理 |

原圖留白的格子在 `pricer.DECISION_MATRIX` 中**明確寫成 `HOLD`**，
而不是讓它變成未定義行為——留白處若不填，就會被實作者各自填成不同的東西。

### `pricing_rules`（逐字照抄）

```json
"pricing_rules": {
  "reduce_if": "slow_mover AND competitor_price_below_ours_by_pct > 5",
  "increase_if": "fast_mover AND days_of_stock < 14 AND competitor_price_above_ours",
  "hold_if": "velocity_neutral AND competitor_within_3pct"
}
```

**矩陣與 `pricing_rules` 的關係，原簡報並未言明。** 本專案採取的解讀是：

> 決策矩陣決定「**動作型態**」（降價／調漲／清理／保持現狀）
> `pricing_rules` 決定「**觸發門檻**」（差距要多大才動手）

兩者**都成立**才會產生調價建議，任一不成立則退回 `HOLD`。
這是保守方向的解讀：規格留白處寧可少動一次價，也不要多動一次。

同理，對手價落在「低於我方 3%–5%」這段灰帶時一律視為 `Neutral`（不動作），
因為 `reduce_if` 明寫要「低於我方超過 5%」才降價。

---

## 核心流程

```
每日排程觸發
  ↓
preflight  --live 模式強制先跑內部 dry-run 通訊測試（提示詞/設定/通道/離線試算/稽核可寫）
  ↓        └─ 任一項不過 → 中止，絕不帶著壞掉的設定去改線上售價
讀取   SKU 快照（Shopify）＋ 對手價 feed（PRICE_WATCH 另行產出）＋ 需求訊號（Trends）
  ↓
分析   流速分位 → 可售天數 → STATUS 五級分類
  ↓
定價   決策矩陣 × pricing_rules → 候選價格
  ↓
安全閥 不低於成本 / 幅度上限 / 缺貨不調價 / 無對手價不調價
  ↓        ├─ 通過 → DRAFT（待人工核准）
  ↓        └─ 擋下 → REJECTED + AMBER/RED，升級人工
促銷   滯銷達門檻天數 → promotional_brief_generator（負毛利品排除，不打折）
  ↓
摘要   Claude 依 prompts/daily_sku_analysis.md 寫成 400 字內的營運摘要
  ↓
稽核   每筆決策追加一行 JSONL（含 SHA-256 串鏈）
  ↓
發送   依自主權層級決定「自動送出」或「留為草稿待核准」
```

---

## 快速上手

```bash
# 零憑證、零網路跑完整流程（讀 mock/ 的三份快照）
python main.py --mock

# 跑完流程但不發送、不寫狀態檔、不落地稽核軌跡
python main.py --mock --dry-run

# 推到 Telegram
python main.py --mock --notify telegram

# 指定狀態檔與稽核檔位置（排程環境建議放在模組外的持久化目錄）
python main.py --mock --state-file /var/lib/openclaw/demo26-state.json \
                      --audit-file /var/log/openclaw/demo26-audit.jsonl

# 串真實 Shopify 與 Claude API（會先強制跑 preflight；缺憑證會明確報錯，不會偷偷退回 mock）
python main.py --live

# 測試
python -m pytest test_main.py -v
```

### 退出碼

| 碼 | 意義 |
| --- | --- |
| `0` | 全部正常，沒有需要人介入的項目 |
| `2` | 流程完成且結果完整，但**有事要看**（調價草稿待核准／安全閥擋下／AMBER／RED 定價違規） |
| `1` | **沒有結果**：設定錯誤、資料源壞掉、preflight 失敗等致命狀況 |

RED 走 `2` 而不是 `1` 是刻意的：本模組的 RED 幾乎都是「某個 SKU 的定價違規」，
報告本身仍然完整可用。判成 `1` 會讓排程器誤以為任務失敗而重跑，
但重跑一百次，那個負毛利商品還是負毛利——**它需要的是人，不是重試。**

### 排程（每日 02:00，庫存日結之後）

```cron
0 2 * * * cd /opt/openclaw/demo26-inventory-pricing && /usr/bin/python3 main.py --live --notify telegram
```

---

## 檔案結構

| 檔案 | 職責 |
| --- | --- |
| `main.py` | 主流程、CLI、preflight 安全閥、報告組裝、通知派送 |
| `analyser.py` | 流速／可售天數／STATUS 五級分類、Shopify 唯讀讀取 |
| `pricer.py` | 3×3 決策矩陣、`pricing_rules`、**三道定價安全閥** |
| `audit.py` | append-only JSONL 稽核軌跡 + SHA-256 串鏈驗證 |
| `prompts/daily_sku_analysis.md` | 每日 SKU 分析摘要提示詞 |
| `prompts/promotional_brief.md` | `promotional_brief_generator` 提示詞 |
| `mock/skus.json` | 8 個 SKU 快照（涵蓋熱銷／正常／滯銷 14 天以上／成本高於售價／缺貨） |
| `mock/competitor_prices.json` | 對手價快照（模擬 `PRICE_WATCH` 輸出） |
| `mock/demand_signals.json` | 需求訊號快照（模擬 Google Trends `TREND_KEYWORDS`） |

### 稽核軌跡格式

每一行是一個事件，帶 `prev_hash` / `entry_hash` 串鏈：

```jsonl
{"schema":1,"ts":"2026-08-24T02:00:01+00:00","run_id":"...","module":"demo26-inventory-pricing","seq":1,"event":"run_started",...}
{"schema":1,...,"seq":2,"event":"pricing_decision","severity":"red","sku_id":"SKU-1004",...}
```

驗證完整性：

```python
from audit import verify_chain
print(verify_chain("audit/pricing_audit.jsonl"))   # (True, "12 筆稽核紀錄串鏈完整")
```

任何一筆被改過、刪除或插入，後續所有 `prev_hash` 都會對不上，
`verify_chain()` 會指出第一個斷點的位置。**能被無痕修改的紀錄不是稽核軌跡。**

---

## 技術規格（apxG_p13 逐字保留）

**Shopify Integration Steps**
Use existing admin API token (from Automation 9) with scopes
`read_inventory`, `write_inventory`, `read_products`, `write_products`
（原圖 scope 清單重複列出 `write_inventory` 兩次，`config.yaml` 如實照抄）
Update 路徑：`shopify.com/admin -> Apps -> your app -> Edit permissions`

**ShipBob/WMS (3PL) Integration**
Generate API Tokens with scopes `inventory`, `orders`；`SHIPBOB_API_KEY=<key>` in config

**Demand Signal References**

- Google Trends（Unofficial pytrends-equivalent endpoint；`TREND_KEYWORDS` per category）
- Competitor Price（CSS Selectors in `PRICE_WATCH` configuration for element monitoring；
  配置 `COMPETITOR_CHECK_FREQUENCY` 與 pricing rules）

**資料流**
Shopify ↔ AI Optimiser（庫存讀取／定價寫入雙向）；Google Trends → `TREND_KEYWORDS`；
對手價格監控 → `PRICE_WATCH` CSS Selectors；ShipBob/WMS (3PL 數據) ↔ AI Optimiser

### 兩個實作上的架構取捨

1. **對手價由獨立監控器產出，本模組只消費**
   `PRICE_WATCH` 的爬取邏輯不寫在本模組裡（那是 demo08 那一類監控器的職責）。
   抓取與決策拆開，對方網站改版時壞掉的是監控器，不是定價引擎。

2. **需求訊號只進報告，不驅動自動調價**
   Google Trends 是全套資料源中雜訊最多的一個。
   它會出現在報告與促銷企劃的「為什麼建議這樣做」裡，
   但**沒有權力改動線上售價**（`config.yaml`：`is_advisory_only: true`）。

---

## Financial Model

| 項目 | 數字 | 來源 |
| --- | --- | --- |
| 部署時間 | **1 週** | ch07_p03 |
| 客戶建置費 | **$3,800** | ch07_p03、apxG_p13 |
| 客戶月費 | **$1,600/mo** | apxG_p13 |
| 首年單客戶營收 | **$3,800 + $1,600 × 12 = $23,000** | 依上列推算 |
| 顧問內部回收工時 | **（原簡報未提供）** | 36 張投影片皆無此數字，不推估 |

### 客戶端 ROI（ch07_p09 ROI Dashboard，逐字）

| 面向 | 數字 |
| --- | --- |
| 釋放現金 (Cash released) | 減少 **60%** 的庫存報廢 |
| 防止缺貨 (Stockout prevention) | 缺貨事件減少 **70%+** |
| 利潤保護 (Margin protection) | 需求高時動態提價 |

> ⚠️ 銷售定價衝突備註：apxG_p20 的 The Level 3 Commercial Matrix 把 #21–#30
> **十欄全部**列為 `建置報價 [$8,000-$10,000]` + `月訂閱費 [$4,000-$6,000]` +
> `Deploy in 1-2 weeks`，明顯是套用同一組模板值。
> **本模組以模組頁的 $3,800 / $1,600 為準**，Commercial Matrix 視為銷售話術用的價格帶。

---

## 客戶見證

> **Sarah Chen 案例**：預計在下一個季節週期中，將季末清倉庫存從 **$38,000**
> 大幅降至**不到 $8,000**。

（以上為原簡報 ch07_p09 的原文引述，未增補任何細節。）

---

## Client Pitch 話術

> 結合內部銷售流速與外部需求訊號，建立資料驅動的採購與定價防線。

延伸話術（可直接用於提案）：

- 「你現在發現趨勢的速度是**三週**。這套系統是**一天**。」
- 「季末那筆 $38,000 的清倉損失，不是因為你不會賣，是因為你在第 12 週才知道它賣不動。」
- 「它不會自己改你的價格。它只會在你決定改價之前，把成本、對手價、庫存天數、
  需求趨勢全部算好，並且告訴你哪些建議它自己都覺得太冒險——那些會直接標成
  『需人工介入』，附上被哪一條安全規則擋下。」
- 「每一筆定價決策都有稽核軌跡。半年後財務問『這個價當初為什麼降』，
  你有一行帶雜湊的紀錄可以拿出來。」

---

## 合規／稽核要求

原簡報此欄為 **（原簡報未提供）**。

本模組**自行加上**的企業級要求（因為它持有 `write_products` 寫入權限）：

- 每筆定價決策落 JSONL 稽核軌跡，含 SHA-256 串鏈
- 所有調價一律 `DRAFT`，人工核准後才寫回平台
- `--live` 前強制 preflight dry-run 內部通訊測試
- 自主權預設 `draft`，且**不建議**升級為 `supervised_auto`
  （本模組送出的不是通知，是會變成錢的數字）
