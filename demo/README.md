# OpenClaw 一人公司引擎 — 30 模組 Demo

《The OpenClaw Income Engine》第 03/04 章（Level 1 一人公司引擎）、第 05 章 + 附錄F（Level 2 代理商基礎建設）、第 07 章 + 附錄G（Level 3 企業級）的**可執行實作**。

30 個自動化模組，橫跨三個規模層級，每一個同時是：

- **你自己的生產力工具**（回收時間）
- **你賣給客戶的服務商品**（產生營收）

---

## 快速上手

```bash
# 1. 安裝依賴（只有 PyYAML 與 pytest）
python -m pip install -r requirements.txt

# 2. 挑一個模組，零憑證離線跑
cd demo01-morning-briefing
python main.py --mock

# 3. 把結果推到你的 Telegram
python main.py --mock --notify telegram

# 4. 跑測試
python -m pytest test_main.py -v
```

**不需要任何 API 金鑰即可跑完整流程。** `--mock` 是預設模式。

---

## 三種執行模式

| 旗標 | 行為 | 用途 |
| --- | --- | --- |
| `--mock`（預設） | 讀 `mock/` 假資料，不呼叫任何外部 API | 開發、教學、客戶簡報 |
| `--live` | 串真實 API；**缺憑證直接報錯退出，絕不靜默降級** | 正式運行 |
| `--dry-run` | 跑完整流程但不實際發送 | 上線前驗證 |

搭配 `--notify {console,telegram,gmail,line,whatsapp}` 決定輸出管道，預設 `console`。

---

## 模組總覽

### Level 1 — 一人公司引擎（第 03/04 章，#1–#10）

| ID | 模組 | 目錄 | 部署 | 回收 | Setup | 月費 | 階段 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| #1 | 晨間情報簡報 | [`demo01-morning-briefing`](demo01-morning-briefing/) | <60m | 35 hrs/mo | $300 | $75 | 奪回早晨 |
| #2 | 收件匣清零代理 | [`demo02-inbox-zero`](demo02-inbox-zero/) | <90m | 33 hrs/mo | $400 | $100 | 奪回早晨 |
| #3 | 會議紀錄與行動提取 | [`demo03-meeting-actions`](demo03-meeting-actions/) | <60m | 11 hrs/mo | $350 | $85 | 無縫營運 |
| #4 | 社群媒體內容排程 | [`demo04-social-scheduler`](demo04-social-scheduler/) | <90m | 26 hrs/mo | $350 | $90 | 品牌與聲量 |
| #5 | 客戶評價監控 | [`demo05-review-monitor`](demo05-review-monitor/) | <60m | 11 hrs/mo | $300 | $80 | 品牌與聲量 |
| #6 | 發票處理與費用分類 | [`demo06-invoice-processor`](demo06-invoice-processor/) | <90m | 8 hrs/mo | $350 | $85 | 無縫營運 |
| #7 | 預約排程器 | [`demo07-booking-scheduler`](demo07-booking-scheduler/) | <75m | 10 hrs/mo | $300 | $75 | 無縫營運 |
| #8 | 競品價格監控警報 | [`demo08-price-monitor`](demo08-price-monitor/) | <60m | 11 hrs/mo | $280 | $70 | 業務增長 |
| #9 | 每日銷售與進度報表 | [`demo09-sales-report`](demo09-sales-report/) | <60m | 11 hrs/mo | $300 | $80 | 業務增長 |
| #10 | 客戶跟進序列自動化 | [`demo10-followup-sequence`](demo10-followup-sequence/) | <90m | 12 hrs/mo | $350 | $90 | 業務增長 |
| | **小計** | | | **168 hrs/mo** | **$3,280** | **$830** | |

### Level 2 — 代理商基礎建設（第 05 章 + 附錄F，#11–#20）

單日部署、把顧客綁進經常性收入的「基礎建設型」自動化。定價在原著兩個來源（第 05 章 vs 附錄F）常有出入，**下表一律採每個模組 README 記錄的「本實作採用」值**，衝突細節見各模組 README。

| ID | 模組 | 目錄 | 部署 | 回收 | 建置費 | 月費 |
| --- | --- | --- | --- | --- | --- | --- |
| #11 | SEO 內容引擎 | [`demo11-seo-content-engine`](demo11-seo-content-engine/) | 1 天 | 15+ hrs/週 | $750 | $200 |
| #12 | 潛在客戶生成管線 | [`demo12-lead-generation`](demo12-lead-generation/) | 1 天 | 20+ hrs/週 | $900 | $220 |
| #13 | 客戶入職工作流 | [`demo13-client-onboarding`](demo13-client-onboarding/) | 1 天 | 8 hrs/客戶 | $900 | $180 |
| #14 | 多渠道客服分流 | [`demo14-support-triage`](demo14-support-triage/) | 1 天 | 25+ hrs/週 | $800 | $190 |
| #15 | 提案與報價生成器 | [`demo15-proposal-generator`](demo15-proposal-generator/) | 1 天 | 3 hrs/提案 | $1,000 | $200 |
| #16 | CRM 數據豐富化與評分 | [`demo16-crm-enrichment`](demo16-crm-enrichment/) | 1 天（<2hr 快速配置） | 15 hrs/週 | $850 | $180 |
| #17 | 每週績效儀表板 | [`demo17-weekly-dashboard`](demo17-weekly-dashboard/) | 5–6 hr（全書最高複雜度） | 12 hrs/mo | $1,200 | $250 |
| #18 | 合約審查與條款提取 | [`demo18-contract-review`](demo18-contract-review/) | 1 天（90 分鐘深度配置） | 5 hrs/合約 | $1,800 | $500 |
| #19 | 活動與研討會跟進序列 | [`demo19-event-followup`](demo19-event-followup/) | 1 天 | 20 hrs/活動 | $900 | $240 |
| #20 | 供應商通訊與訂單追蹤 | [`demo20-vendor-tracking`](demo20-vendor-tracking/) | 1 天 | 18 hrs/週 | $1,000 | $270 |

### Level 3 — 企業級（第 07 章 + 附錄G，#21–#30）

多週部署、跨系統整合（ERP/CRM/MES/IoT）的企業級自動化。**原著 36 張投影片沒有任何一頁提供「顧問自己的內部建置工時回收」數字**——各模組 README 一律誠實標記「（原簡報未提供）」，不做推估；ROI 全部是客戶端節省。

| ID | 模組 | 目錄 | 部署 | 內部回收 | 建置費 | 月費 |
| --- | --- | --- | --- | --- | --- | --- |
| #21 | 多智能體行銷協同群 | [`demo21-marketing-swarm`](demo21-marketing-swarm/) | 1 週 | 原簡報未提供 | $5,000 | $2,500 |
| #22 | 全漏斗業務自動化 | [`demo22-sales-pipeline`](demo22-sales-pipeline/) | 1 週 | 原簡報未提供 | $4,500 | $2,000 |
| #23 | 董事會級財務報表自動化 | [`demo23-financial-reporting`](demo23-financial-reporting/) | 1 週 | 原簡報未提供 | $3,500 | $1,500 |
| #24 | 無偏見人資招募篩選管線 | [`demo24-hr-screening`](demo24-hr-screening/) | 1 週 | 原簡報未提供 | $3,200 | $1,200 |
| #25 | 動態客戶媒合引擎 | [`demo25-client-matching`](demo25-client-matching/) | 1 週 | 原簡報未提供 | $3,500 | $1,400 |
| #26 | 電商庫存與定價最佳化 | [`demo26-inventory-pricing`](demo26-inventory-pricing/) | 1 週 | 原簡報未提供 | $3,800 | $1,600 |
| #27 | 法務文件分析與合規監控 | [`demo27-compliance-monitor`](demo27-compliance-monitor/) | 1 週 | 原簡報未提供 | $4,000 | $1,800 |
| #28 | 預測性品管與階層報告鏈 | [`demo28-qc-reporting`](demo28-qc-reporting/) | 1 週 | 原簡報未提供 | $4,200 | $1,900 |
| #29 | 多據點商業智慧匯總 | [`demo29-multilocation-bi`](demo29-multilocation-bi/) | 1 週 | 原簡報未提供 | $4,500 | $2,000 |
| #30 | 白牌「AI 營運部」客戶經銷方案 | [`demo30-whitelabel-reseller`](demo30-whitelabel-reseller/) | 2 週（全書唯一非 1 週） | 原簡報未提供 | $8,000–$10,000 | $4,000–$6,000 + 子客戶 70–80% 分潤 |

### ⚠️ 關於「回收時數」與「定價」的口徑

書中封面寫「每月回收 **40–60 小時**」，但 Level 1 模組矩陣加總是 **168 hrs/mo**。這兩個數字口徑不同：

- **168 hrs/mo** = Level 1 矩陣值，是**賣給客戶時的價值主張**（客戶端可回收的理論上限）
- **40–60 hrs/mo** = 你**自己身上實際能回收的**（不是每個模組對你都全額適用）

Level 2、Level 3 模組的原著簡報也常見**同一模組兩個來源給不同定價**（第 05/07 章 vs 附錄 F/G），各模組 README 都記錄了完整的來源對照表與採用理由（多數採附錄，因其為逐模組的技術與商業規格表，精細度較高）。**對外提案時請以各模組 README 為準，不要只看本表的彙整值。**

---

## 打包方案（第 04 章核心商業洞察）

`bundle-quickstart/` 把 **#1 + #2 + #5 + #9** 合併成「快速啟動方案」：

| | 單獨銷售（Features） | 快速啟動方案（Experience） |
| --- | --- | --- |
| 定價 | $1,300 setup + $335/mo | **$995 setup + $200/mo** |
| 客戶反應 | 「這是額外工具嗎？值得付月費嗎？」 | 「週一醒來，信箱已整理，報表在手機，生意在你睡覺時已運作」 |
| 部署時間成本 | 基準 | **↓ 60%** |
| 單日參與營收 | 基準 | **↑ 3 倍** |

**反直覺之處**：打包後單價**更低**，但成交率 3 倍、部署成本降 6 成，總淨值反而更高。
你賣的不是折扣，是**降低客戶的決策摩擦**。

---

## 架構

```
demo/
├── PLAN.md            # Level 1（#1–#10）建置計畫：模組規格、Session 拆分、驗收標準
├── SPEC-11-20.md       # Level 2（#11–#20）規格萃取：附錄F + 第05章逐頁對照
├── SPEC-21-30.md       # Level 3（#21–#30）規格萃取：附錄G + 第07章逐頁對照
├── CONTRACT.md         # _shared API 契約（凍結，不得擅改簽名）
├── requirements.txt
├── .env.example
│
├── _shared/            # 基礎設施層（30 個 demo 共用）
│   ├── autonomy.py         # 自主權階梯：READ_ONLY → DRAFT → SUPERVISED_AUTO
│   ├── diagnostics.py      # RAG 診斷矩陣：RED 停擺 / AMBER 品質降級
│   ├── llm_client.py       # Claude API 封裝（mock 模式零成本）
│   ├── notifier.py         # 多通道通知（Telegram/Gmail/LINE/WhatsApp/Console）
│   ├── config_loader.py    # YAML + 環境變數驗證
│   └── package.py          # 打包成可獨立交付的單一 demo
│
├── demo01-morning-briefing/ ... demo10-followup-sequence/   # Level 1
├── demo11-seo-content-engine/ ... demo20-vendor-tracking/   # Level 2
├── demo21-marketing-swarm/ ... demo30-whitelabel-reseller/  # Level 3
│   ├── README.md      # Before/After + 財務模型 + Client Pitch 話術（+ Level2/3 常見「來源衝突對照表」）
│   ├── config.yaml
│   ├── main.py
│   ├── prompts/*.md   # 提示詞獨立成檔（這是核心資產，不內嵌程式碼）
│   ├── mock/*.json
│   └── test_main.py
│
└── bundle-quickstart/  # 打包層（目前仍以 Level 1 的 #1/#2/#5/#9 組成）
```

### 為什麼是「混合式」而非 30 個完全獨立的資料夾

書中的商業模式是「**單品可賣、也能打包**」，架構必須對得上：

- **共用 `_shared/`**：30 個 demo 都要呼叫 Claude、發通知、走自主權階梯。複製 30 份 = 改一個 bug 要改 30 次
- **業務邏輯各自獨立**：交付客戶時要能單獨打包
- **`package.py` 補上最後一哩**：把 `_shared/` vendor 進單一 demo 目錄，產出可獨立執行的交付版

```bash
python _shared/package.py demo01-morning-briefing --out dist/
```

---

## 兩個貫穿全專案的安全設計

### 1. 自主權階梯（`_shared/autonomy.py`）

```
READ_ONLY          只分類與分析，絕不觸碰來源、絕不外送
    ↓
DRAFT（預設）       建立草稿，必須人工審查後送出
    ↓
SUPERVISED_AUTO    僅自動送給白名單，其餘一律降級為 DRAFT
```

**強制規則**：

- 預設值一律 `DRAFT`
- `SUPERVISED_AUTO` 白名單為空 → 拋 `AutonomyError`
- 草稿模式未滿 14 天就開全自動 → 發出警告（第 04 章鐵律：**兩週 + 客戶明確簽核前，絕不啟用全自動發送**）

### 2. RAG 診斷矩陣（`_shared/diagnostics.py`）

| 級別 | 行為 | 範例 |
| --- | --- | --- |
| 🔴 **RED** | 記錄後 `sys.exit(1)` | API Key 失效、OAuth 過期、Webhook 不可存取 |
| 🟠 **AMBER** | 記錄警告，流程繼續 | 簡報超長、Spam 誤判、語氣不符、簡報延遲 |
| 🟢 **GREEN** | 正常 | — |

**設計原則**：品質降級不該讓系統停擺，但也**絕不可靜默通過**。

---

## 與原著的差異

| 項目 | 原著 | 本專案 | 理由 |
| --- | --- | --- | --- |
| 通知管道 | WhatsApp（Twilio） | **Telegram**（預設，多通道可切） | 作者面向英國市場；台灣 WhatsApp 滲透率低 |
| 模組 #7 名稱 | WhatsApp 自動排程器 | 預約排程器 | 同上，通道無關化 |
| Gmail 存取 | 自建 OAuth | 走已登入的 `gws` CLI | 書中把「Gmail token 7 天過期」列為紅色警報，繞開最省事 |
| 開發成本 | 未提 | `--mock` 不呼叫 API | 反覆測試不該產生 API 帳單 |
| Level 2/3 定價衝突 | 第 05/07 章與附錄 F/G 常互相矛盾 | 各模組 README 記錄完整來源對照表 + 採用理由，一律不自行平均或猜測 | 避免用「各取一半」偽造出原著沒有的數字 |

---

## 環境需求

- Python **3.10+**（使用 `X | None` 型別語法）
- 依賴只有 **PyYAML** 與 **pytest**，HTTP 一律走標準庫 `urllib.request`
- Windows / macOS / Linux 皆可（檔案 I/O 全部明確 `encoding="utf-8"`）

環境變數請參考 `.env.example`。**`--mock` 模式一個都不需要。**

---

## 相關文件

- [`PLAN.md`](PLAN.md) — Level 1（#1–#10）完整建置計畫、模組逐一規格、驗收標準
- [`SPEC-11-20.md`](SPEC-11-20.md) — Level 2（#11–#20）規格萃取，附錄F + 第 05 章逐頁對照
- [`SPEC-21-30.md`](SPEC-21-30.md) — Level 3（#21–#30）規格萃取，附錄G + 第 07 章逐頁對照
- [`CONTRACT.md`](CONTRACT.md) — `_shared` API 契約
- [`bundle-quickstart/`](bundle-quickstart/) — 打包方案與客戶提案範本
- [`../pdf/`](../pdf/) — 原著 16 份章節簡報
