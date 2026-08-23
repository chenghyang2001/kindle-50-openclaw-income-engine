# demo29 — 多據點商業智慧匯總

> 自動化 #29 ｜ Level 3 企業級 ｜ 營運部門 · BI ｜ 部署 1 週 ｜ `BI` `SaaS` `CRM`
> 來源頁：ch07_p12、apxG_p17

各據點的 Shopify / Square / POS 數字每週一 07:00 自動標準化、算出 Composite Score、
分成 **Top Tier / Core / Focus Required**、比對 4 週滾動基準線標出偏離 15% 的異常，
然後**依角色送出兩種完全不同的報表**：總部拿到 Full Pack，店長只拿到自己那一家。

---

## Before / After

| | Before（人工） | After（Agent） |
| --- | --- | --- |
| 總部彙整耗時 | 每週 **2 天**下載 **8–15 個檔案**合併清理 | 0 分鐘（週一 07:00 自動送達） |
| 資料狀態 | 各站點孤立（Silos），欄位口徑各自為政 | 異質 API 自動標準化成同一組指標 |
| 週一高階會議 | **無當週數據可看**，只能討論上週的上週 | 抵達前已讀完，直接討論 |
| 決策時點 | 週二下午才拿得到分析 | 週一 07:00，多出 **30+ 小時**決策優勢 |
| 據點比較 | 手動排序試算表，口徑一變就對不上 | Composite Score 自動排名 + 同儕中位數比較 |
| 異常發現 | 月底對帳才發現某店掉了兩成 | 偏離 4 週基準線 **15%** 當週就標出來 |
| 店長看到什麼 | 常常是「整份 Excel 寄給所有人」 | **Own-site only**，看不到任何他店數字 |
| 單一據點系統掛掉 | 通常整份彙整停擺，當週沒報表 | 標記「⚠️ 部分資料：台南東區店 無回應」後**照常發出** |

---

## 本模組的核心：兩級權限視野（apxG_p17）

| 角色 | 旗標 | 視野 | 看得到 |
| --- | --- | --- | --- |
| 總部（HQ） | `--role hq` | **Full Pack** | 全網加總、跨據點排名、同儕中位數、各店分級與異常 |
| 店長（Site Manager） | `--role site_manager --site <id>` | **Own-site only** | 只有自己那一家的指標、分級與異常 |

### 這是資料隔離要求，不是 UI 便利性

三條實作規則，缺一條這個模組就沒有賣點：

1. **權限判定在資料層，發生在取數之前。**
   `access_control.resolve_scope()` 先算出可見據點集合，`AccessScope.filter_registry()`
   再把名冊裁掉，`rollup.collect_sites()` 只吃裁切後的名冊。店長視野下，
   **其他據點的 API 從頭到尾不會被呼叫**——他拿到的資料集裡根本不存在別家的數字，
   而不是「有但沒印出來」。
   只在 render 層過濾等於把他店營運數據放進同一份記憶體物件、同一份 LLM prompt、
   同一份稽核附件裡，一次 bug 就外洩。

2. **權限設定缺失或角色未知，一律退回最小權限。**
   未知角色 → 視為店長；店長沒指定據點或指定了不存在的據點 → 可見集合為**空集合**
   （deny-all）。安全預設值必須是「什麼都看不到」。
   `config.yaml` 的 `access.default_role` 也刻意設成 `site_manager`：
   **Full Pack 必須靠 `--role hq` 明示取得**，忘記帶旗標的排程不會把全網數據送出去。

3. **輸出前再驗一次（縱深防禦）。**
   `assert_no_leak()` 把即將送出的 payload、報表文字與 AI 敘述序列化後，掃描是否出現
   任何不可見據點的識別碼或店名。規則 1 已經保證不會發生，規則 3 負責在未來有人
   改壞規則 1 時當場攔下，而不是安靜地把別店數字寄給店長。掃描未過 → 安全閥擋下外送。

### 演算法層也必須支援隔離

Composite Score 刻意**對自身基準線正規化，不對同儕正規化**：

```
index(指標) = 本期值 / 該店自身 4 週滾動基準線 × 100      # 基準線 = 100
Composite  = Σ(權重 × index) / Σ(權重)
```

若改用同儕 min-max 正規化，店長要算出自己的分數就必須先拿到全網數字——
權限隔離會在演算法層被架空。改成自身基準線後，**同一家店在總部視野與店長視野
算出的分數完全一致**，且零跨店資料流動。

跨據點排名與同儕中位數則反過來：它們是由他店數字算出來的，
`RollupResult.ranking` / `peer_median_composite` / `network_totals`
在店長視野一律是 `None`——**存在那個物件裡就已經是外洩**。

---

## Composite Score 與分級

權重寫在 `config.yaml`，不寫死在程式碼：

| 指標 | 權重 | 口徑 |
| --- | --- | --- |
| 營收 | 40 | 淨額（已扣退款） |
| 交易筆數 | 20 | 成交筆數 |
| 客單價 | 15 | 營收 / 交易筆數 |
| 來客轉換率 | 15 | 交易筆數 / 來客數 |
| 人時營收 | 10 | 營收 / 工時 |

| 分級 | 門檻 | 意義 |
| --- | --- | --- |
| **Top Tier** | Composite ≥ 110 | 明顯優於自身基準線 |
| **Core** | 90 ≤ Composite < 110 | 穩定運行 |
| **Focus Required** | Composite < 90 | 需要本週就介入 |
| Unrated | 無基準線 | 新店或歷史不足，不硬套假分數 |

**異常偵測**：任一指標偏離自身 4 週滾動基準線超過 **15%**（apxG_p17 明訂門檻）即標記，
**正負皆標**——突然暴衝多半是重複計算、活動檔期或單筆特大交易，
只抓下跌會讓「看起來很好」的資料錯誤永遠沒人查。

**Composite 以「已四捨五入到 1 位小數的 index」加權平均**：這樣報表上列出的每個
index 和最後的總分對得起來。用未捨入值會出現「照著表格算卻得不到那個總分」的客訴，
對一份要拿去開高階會議的報表是致命的。

---

## 部分失敗（比照 demo09：降級，但絕不假裝）

`mock/site-tnn-east.json` 刻意保留一份「來源系統回 error」的樣本，讓部分失敗路徑
每次執行都真的被走過。任一據點取數失敗時：

- 報表**最上方**強制加橫幅：`⚠️ 部分資料：台南東區店 無回應`
- 橫幅下一行明寫「請勿據此下修目標或究責」
- 失敗據點仍列在明細裡（標 ⚠️ 與失敗原因），不會從報表上消失
- 缺數的據點**不會被當成 0 加總**，也不會被以缺值推進基準線視窗
  （那會讓基準線被一次故障永久污染）
- 走 `Diagnostics.amber(symptom, fix)` 進 RAG 診斷矩陣的琥珀燈，維運端知道要修
- 報表照常在週一 07:00 送出

整份失敗等於重現「週一會議沒有數據」——那正是導入這個代理人要消滅的舊狀態。

**店長視野下，他店的無回應狀態同樣不可見**：那也是他店的營運資訊。

---

## 稽核軌跡（JSONL）

每次執行對 `audit/access.jsonl` 追加數行，欄位固定，可直接餵 `jq` 或 SIEM：

```json
{"ts":"2026-08-24T07:00:03+00:00","run_id":"9f2c…","module":"demo29-multilocation-bi",
 "event":"data_access","actor":"mgr.xinyi@example.com","role":"site_manager",
 "pack":"own_site_only","visible_site_ids":["tpe-xinyi"],
 "sites_returned":["tpe-xinyi"],"sites_unavailable":[],"cross_site_analytics":false}
```

五種事件：`access_resolved` / `data_access` / `preflight` / `delivery` / `state_update`。
時間一律 **UTC**（多據點跨時區，用本地時間會對不起來）。

**Fail-closed**：寫不進稽核檔預設視為致命（`audit.fail_closed: true`）。
本模組的賣點就是「店長看不到他店數據」，而唯一能事後舉證的只有這份軌跡；
稽核寫不進去卻照樣發報表，等於宣稱「我們有紀錄」但實際沒有——比沒有紀錄更糟。
需要在唯讀檔案系統（容器 / CI）上跑時才改成 `false`，並自行承擔「這次執行無法舉證」。

> 依裁決自行實作於本模組的 `audit.py`，**未改動 `_shared/`**：稽核欄位是本模組
> 權限模型專屬的，硬塞進共用層會逼其他 9 個模組接受一組它們用不到的欄位。

---

## 全域安全閥：對外呼叫前的 `--dry-run` 內部通訊測試（apxG_p03）

任何對外 API 呼叫之前，先在內部把整條路徑空跑一次，五項全過才允許外送：

| 檢查 | 沒過代表什麼 |
| --- | --- |
| `scope_resolved` | 權限沒解出來，沒有內容可送 |
| `payload_non_empty` | 報表是空的 |
| `payload_scoped` | 洩漏掃描攔到不可見據點的識別資訊 |
| `audit_trail_healthy` | 這次執行無法被稽核佐證 |
| `channel_supported` | 通道名稱不在 `Notifier.SUPPORTED` |

未過 → 不外送、記琥珀燈、`delivery.reason = "preflight_failed"`。
`console` 通道是本機列印、不算對外呼叫，因此不受阻擋（但一樣留下稽核與診斷）。

---

## 財務模型（Financial Model）

| 項目 | 數值 | 來源 |
| --- | --- | --- |
| 部署時間 | **1 週** | ch07_p03、apxG_p17 |
| 客戶建置費（Setup） | **$4,500** | ch07_p12 模組頁 |
| 客戶月費 | **$2,000 /mo** | ch07_p12 模組頁 |
| 客戶端節省 | 消除每週 **2 天**的總部數據合併時間 | apxG_p17 ROI Dashboard |
| 客戶端決策優勢 | **30+ 小時**（週一 07:00 vs 週二下午） | apxG_p17 ROI Dashboard |
| 內部回收（顧問自己的工時回收） | **（原簡報未提供）** | 見下方說明 |

### ⚠️ 定價差異：以模組頁為準，不採 Commercial Matrix

附錄G 最後的 **The Level 3 Commercial Matrix（apxG_p20）** 把 #21–#30 **十個模組全部**
列為 `建置報價 [$8,000-$10,000]` + `月訂閱費 [$4,000-$6,000]` + `Deploy in 1-2 weeks`。
十欄同值，研判為**套用同一組模板佔位值**。

本模組**一律採用模組頁（ch07_p12 / apxG_p17）的逐案報價 $4,500 + $2,000/mo**，
`config.yaml` 的 `module.client_setup_price` / `client_monthly_price` 即為此值。
apxG_p20 的價格帶僅供銷售話術參考，不寫入設定。

### ⚠️「內部回收」原簡報無資料

36 張投影片中**沒有任何一頁**提供「顧問自己的內部建置工時回收」數字，
全部 ROI 數字皆為**客戶端節省**。因此 `config.yaml` 的 `recovered_hours_per_month`
一律為 `null` + `recovered_hours_note: "（原簡報未提供）"`，**不從客戶端數字反推**。

---

## 客戶見證

**（原簡報未提供，原簡報該欄誤植為 #27 的法務案例）**

ch07_p12（#29）的客戶見證欄位所印內容與 #27 完全相同（同為 Marcus Webb 的
$400/月「進階法律監控服務」案例），研判為簡報排版誤植——一個多據點零售 BI 模組
不會拿法律監控服務當見證。本模組**不引用該則見證**，也**不編造替代品**。

---

## Client Pitch（銷售話術）

> 「在您的領導團隊做出任何決定之前，每個據點的表現已一覽無遺。」
> — ch07_p12 原文

延伸三句（面對面時接著講）：

1. 「你們現在週一早上有當週數據可看嗎？還是在看上週的上週？」
2. 「總部每週花兩天合併 8 到 15 個檔案——那兩天的分析師人力，換成什麼會更值得？」
3. 「而且店長只會看到自己那一家。這不是介面設定，是資料層的隔離——
    他的報表裡根本不存在別家的數字，而且每一次存取都有稽核軌跡可查。」

---

## 執行方式

```bash
# 零憑證、零網路：預設是店長視野（config 的 access.default_role）
python main.py --mock

# 總部 Full Pack（必須明示，不會靠預設值取得）
python main.py --mock --role hq

# 指定其他據點的店長視野
python main.py --mock --role site_manager --site khh-boai

# 跑完流程但不發送、也不更新基準線狀態檔
python main.py --mock --role hq --dry-run

# 自訂稽核軌跡與基準線狀態檔位置
python main.py --mock --role hq --audit-file /tmp/audit.jsonl --state-file /tmp/base.json

# 串真實 API（缺憑證會列出缺哪些變數並退出，不會靜默退回 mock）
python main.py --live --role hq

# 測試
python -m pytest test_main.py -v
```

Exit code：`0` 正常（含部分資料）／`1` 設定或資料錯誤／`2` 拒絕存取。

---

## 檔案結構

```
demo29-multilocation-bi/
├── README.md              # 本檔
├── config.yaml            # 權限預設值、稽核設定、權重、門檻、名冊路徑
├── main.py                # 主流程：權限 → 取數 → 匯總 → 洩漏掃描 → 安全閥 → 發送
├── access_control.py      # 兩級權限視野的資料層閘門 + 洩漏掃描
├── audit.py               # JSONL 稽核軌跡（fail-closed）
├── rollup.py              # 異質來源標準化、4 週基準線、Composite Score、分級與異常
├── prompts/
│   └── rollup.md          # 系統提示詞（含 own_site_only 的禁止事項）
├── mock/
│   ├── registry.json      # LOCATION_REGISTRY：5 個據點的異質來源名冊
│   ├── baselines.json     # 4 週滾動基準線種子
│   ├── site-tpe-xinyi.json      # Shopify（台北信義旗艦店）
│   ├── site-tpe-zhongshan.json  # Square（台北中山店，最小貨幣單位整數）
│   ├── site-txg-fengjia.json    # POS（台中逢甲店）
│   ├── site-khh-boai.json       # Shopify（高雄博愛店）
│   └── site-tnn-east.json       # POS（台南東區店，**刻意無回應**）
└── test_main.py           # happy / edge / integration 三個測試
```

執行時會自動建立 `audit/`（稽核軌跡）與 `state/`（基準線狀態）兩個目錄。
兩者都是執行期產物，建議加入專案 `.gitignore`。

### 三種異質來源的標準化口徑

| 來源 | schema | 營收口徑 | 交易 | 來客 | 工時 |
| --- | --- | --- | --- | --- | --- |
| Shopify | `daily_orders[]` | `gross_sales − refunds` | `order_count` | `sessions` | `staff_hours` |
| Square | `payments_summary[]` | `amount_money − refunded_money`，除以 `10^minor_unit_scale` | `payment_count` | `customer_count` | `labor.hours` |
| POS | `business_days[]` | `net_sales`（已是淨額） | `tickets` | `door_count` | `hours_worked` |

三者都收斂成淨額口徑的同一組 `MetricSet`，因此全網加總不會重複計算。
**最小貨幣單位的換算比例由 `registry.json` 的 `minor_unit_scale` 指定，不寫死在程式碼**——
不同幣別、不同版本的 API 回傳精度不一致，寫死一定會錯。

---

## 技術要點

- **金額全程 `Decimal`**：JSON 與 YAML 的數值一律以字串儲存（`"1850000"`），
  避免 float 二進位誤差在跨據點加總時被放大成 `4349999.999999999`。
  收斂用 `ROUND_HALF_UP`（財務慣例），不用 Decimal 預設的銀行家捨入。
- **百分比一律防零除**：分母為 0 或缺值時回 `None` 而不是 0。
  「沒有來客」和「轉換率 0%」是兩件不同的事，混為一談會讓報表說謊。
  無基準線的據點標為 `Unrated`，不硬套一個從 0 除出來的假分數。
- **基準線只由總部視野推進**（`baseline.update_requires_hq: true`）：
  基準線是全網的正式紀錄，讓每個店長各自寫入會產生互相覆蓋的競態，
  而且店長對他店視窗本來就沒有寫入的正當性。狀態檔採「先寫暫存再 replace」。
- **自主權階梯**：報表會對外發送，依第 04 章鐵律預設 `draft`。
  非 console 通道要真的送出，需 `supervised_auto` + 非空白名單 + 連續穩定 14 天。
  console 通道視為本機列印，不受閘門管制。
- **設定錯誤不靜默**：`deliver_at` 格式錯誤、權重出現未知指標、門檻不是數值，
  一律當場拋錯，不悄悄套用預設值。
- **拒絕存取時完全不呼叫 LLM**：沒有內容可寫，而 `--live` 下那會是一次白花錢
  又白白把 payload 送出去的外部呼叫。
- **依賴**：僅 `PyYAML`（`_shared.config_loader`）+ `pytest`（測試），其餘全標準庫。
  無 `requests`，`--live` 的 HTTP 由 `_shared` 的 `urllib.request` 負責。

---

## 已知限制

- 各據點目前皆為 mock 讀檔；`--live` 的真實 Shopify / Square / POS client 需另補，
  但錯誤語意已與線上版一致（取不到就 `RollupError`，不補 0）。
- 權限模型只有兩級（HQ / 店長），與 apxG_p17 規格一致。區經理這類中間層級
  （看得到轄下數店、看不到其他區）尚未實作——多開一級就多一條外洩路徑，
  要加必須連同 `hidden_identifiers()` 的掃描邏輯一起重新驗證。
- `actor` 目前由 CLI／config 提供，未接真實身分驗證（SSO / OIDC）。
  正式部署時 `actor` 與 `role` 應由身分提供者簽發，而非由呼叫端自稱。
- 4 週滾動基準線以「週」為單位推進，未處理季節性（年節、週年慶）。
  檔期期間的偏離標記需要人工判讀。
- 時區僅用於報表顯示，實際排程由外部 cron 負責（建議週一 06:30 觸發，
  留 30 分鐘緩衝，確保 07:00 前送達）。
