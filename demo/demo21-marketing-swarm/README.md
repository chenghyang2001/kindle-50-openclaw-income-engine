# 模組 #21 — 多智能體行銷協同群（The Multi-Agent Marketing Swarm）

> **Level 3 企業級 · 行銷部門**
> 來源：第 07 章 ch07_p04、附錄 G apxG_p03（Swarm Architecture）、apxG_p04、apxG_p05
> 一句話：**一個完整的 AI 行銷部門。五人團隊的產出，一人的人事成本。**

---

## 1. Before / After

| | Before（人工五人編制） | After（Orchestrator + 5 Sub-agent） |
| --- | --- | --- |
| 編制 | 5 人：Content、Social、Email、Lead Gen、Analytics | 1 個 Marketing Director Agent 統籌 5 個子智能體 |
| 月成本 | **$8,000–$15,000/月** | 月訂閱 $2,500/mo |
| 協作方式 | 各做各的，缺乏連貫；品牌訊息在四個渠道各長一個樣 | `brand_context.yml` 單一真理來源，級聯至全部子智能體 |
| 人類投入 | 全週分散在會議、校稿、催稿 | **每週日審核一份策略備忘錄，20 分鐘** |
| 產出量 | 基準 | 提升 **3–4 倍** |
| 代理商利潤率 | **48%** | **72%** |

**部署時間：1 週**（ch07_p03）

### 五個子智能體與產能配額（apxG_p04）

| 子智能體 | 配額 | 整合對象 |
| --- | --- | --- |
| Content Agent | 8–12 草稿/週 | CMS OAuth（WordPress、Webflow） |
| Social Agent | 28 貼文/週 | 社群排程 API |
| Email Agent | 2–3 活動/週 | 電子報平台 API |
| Lead Gen Agent | 50+ 名單/週 | CRM 名單寫入 API |
| Analytics Agent | 每日 KPI（7 份/週） | GA4 Service Account |

---

## 2. 架構：全域繼承與編排器機制（apxG_p03）

這是整份 Level 3 手冊最重要的架構頁，也是本模組的核心。

```
                 ┌──────────────────────────────┐
                 │   Orchestrator Agent          │
                 │   （Marketing Director）      │
                 │   持有 brand_context.yml      │
                 │   持有 STAGE_MAP              │
                 └──────────────┬───────────────┘
                                │  INHERIT_FROM_ORCHESTRATOR: true
        ┌───────────┬───────────┼───────────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
    Content      Social       Email      Lead Gen    Analytics
```

### 三個機制，逐字實作

1. **單一真理來源（Single Source of Truth）**
   品牌名稱、語氣、可引用數字、禁用詞只存在於 `brand_context.yml`。
   Sub-agent 拿到的是唯讀快照（`InheritedContext`，`frozen=True`），
   **沒有繼承過就呼叫 `execute()` 會直接拋 `SwarmError`** —— 寧可停機，
   也不要讓某個 agent 拿舊品牌資料發文。

2. **Cascading Logic（瞬間級聯）**
   `Orchestrator.update_brand_context(patch, reason)` 做三件事：深層合併 →
   `context_version + 1` → 立刻級聯給所有子智能體。每一份產出都戳上
   `context_version` 與 `context_checksum`，事後稽核可以回答
   「這篇貼文是用哪一版品牌上下文寫的」。

3. **STAGE_MAP 決定誰動**
   `awareness / nurture / conversion / retention` 四個階段各自定義
   `active_agents`。不在名單內的子智能體標記 `skipped_inactive_stage`，
   不呼叫 LLM、不佔配額 —— 階段由編排器決定，子智能體不自作主張。

> 把 `INHERIT_FROM_ORCHESTRATOR` 設成 `false` 會發生什麼：
> 該 agent 拒絕繼承、進入 `cascade.refused` 清單、稽核日誌留痕，
> 並在結果的 `warnings` 明寫「這正是跨渠道品牌衝突的來源」。
> 這個旗標刻意保留可關閉，是為了讓「關掉會怎樣」變成可觀察的事實。

---

## 3. 執行邏輯與防呆機制（apxG_p05）

```
① brand_context.yml
      ↓
② 整合層（CMS / GA4 / Social / CRM API）
   ⚠ 強制安全閥：所有 API 呼叫前必經 --dry-run 內部通訊測試
      ↓
③ 每週日 07:00 生成策略備忘錄   ← approval_required: true（唯一人類監督節點）
      ↓
④ Task Dispatch → 5 條 Agent Action 平行執行
```

### 強制安全閥（apxG_p03 硬規則）

`preflight_dry_run()` 在**任何**對外呼叫之前跑完整內部通訊測試，印出
「將呼叫哪個端點、用什麼方法、送出什麼結構」，但**不實際送出**。
未通過就直接紅色警報中止，不進入 dispatch。

判定條件：

| 檢查 | mock | live |
| --- | --- | --- |
| 整合未登記於 `integrations` | 硬失敗 | 硬失敗 |
| 端點不是 `https://` | 硬失敗 | 硬失敗 |
| 憑證環境變數缺失 | 允許（離線不需憑證） | 硬失敗 |

### 人類審核閘門

策略備忘錄的 `approval_required: true` 是整套流程唯一保留的人類節點。
未核准時：**內容照產，但一律鎖在草稿**，`publish_mode` 不可能是 `auto`，
`status` 全部是 `blocked_pending_approval`。核准必須具名（`--approved-by`），
未具名的 `--approve` 會被紅色警報擋下 —— 稽核軌跡要求每次核准都能追溯到人。

發布權需要**同時**通過兩道閘門：

1. 備忘錄已經人工核准
2. `AutonomyGate` 判定該渠道在白名單內（預設 `draft`，全部降級為草稿）

---

## 4. 稽核軌跡（Level 3 要求）

每次執行把以下動作追加到 JSONL（預設 `audit/swarm-audit.jsonl`）：

`context_cascade` · `white_label_applied` · `preflight_dry_run` ·
`strategy_memo_approved` / `approval_pending` · `agent_dispatch` · `run_completed`

每筆紀錄固定回答五件事：**何時 / 做了什麼 / 對誰 / 依據什麼決定 / 有沒有經過人工核准（誰核准）**。

格式選 JSONL 而非 JSON 陣列：稽核日誌只追加，程式中途被中斷時 JSONL
仍保留前面所有完整的行，JSON 陣列則整份壞掉。

> `audit/` 與 `state/` 是執行期產物。跑測試或 CI 時請用
> `--audit-file` / `--state-file` 指向暫存目錄，避免污染工作樹。

---

## 5. Financial Model

### 客戶端（依各模組頁的逐案報價）

| 項目 | 數字 | 出處 |
| --- | --- | --- |
| 建置費（Setup） | **$5,000** | ch07_p03 / apxG_p04 |
| 月訂閱費 | **$2,500/mo** | apxG_p04 |
| 取代的內部成本 | **$8,000–$15,000/月** | ch07_p04 |
| 客戶每月淨省 | **$5,500–$12,500** | 由 `compute_economics()` 計算 |
| 客戶每年淨省 | **$66,000–$150,000** | 同上 |
| 回收期 | **首月省下的預算即超過建置費** | ch07_p04（程式以最保守的 $5,500 端驗證成立） |
| 產出量 | 提升 **3–4 倍** | ch07_p04 |
| 代理商利潤率 | **48% → 72%** | ch07_p04 |
| 顧問端內部回收工時 | **（原簡報未提供）** | 見下方說明 |

金額一律以 `Decimal` 計算，不用浮點數 —— 報價上的分位誤差在提案現場會很難看。

### ⚠️ 兩個必須講清楚的數字問題

1. **定價以本模組頁為準。**
   附錄 G 最後的「The Level 3 Commercial Matrix」（apxG_p20）把 #21–#30
   **十個模組全部**印成 `建置報價 $8,000-$10,000` + `月訂閱費 $4,000-$6,000`，
   明顯是套用同一組模板的佔位值。本模組實作與計算一律採用各模組頁的
   **$5,000 + $2,500/mo**；`$8,000-$10,000` / `$4,000-$6,000` 僅保留在
   `config.yaml` 的 `commercial_matrix_*` 欄位，當成銷售話術用的價格帶。

2. **「內部回收」欄位原簡報全部無資料。**
   36 張投影片沒有任何一頁提供「顧問自己的內部建置工時回收」數字，
   全部 ROI 數字皆為**客戶端節省**。因此 `recovered_hours_per_month` 為
   `null`，README 標記「（原簡報未提供）」，**不推估**。
   對客戶引用上表數字時務必說明：這些是**客戶端**省下的成本，
   不是顧問端的獲利率。

---

## 6. 客戶見證

**（原簡報未提供具名見證。）**

ch07_p04 只給了「真實情境」的量化結果，沒有可具名引述的客戶：

> 將代理商利潤率從 **48%** 提升至 **72%**。

本模組不編造見證。需要見證素材時，正確做法是等第一個客戶跑滿一季後，
用稽核日誌的真實數字回頭跟客戶要授權，而不是先寫一段好聽的話。

---

## 7. Client Pitch 話術

### 開場（一句話定位）

> 「一個完整的 AI 行銷部門。五人團隊的產出，一人的人事成本。」

### 對「這樣會不會失控」的回答

> 「整套流程只有**一個**地方需要你點頭：每週日早上七點，你會收到一份策略備忘錄。
> 你花 20 分鐘看完、按核准，這週的內容才會出去。沒按核准，五個智能體照樣把東西做好，
> 但一件都不會發布 —— 這不是設定，是程式層的硬條件。」

### 對「AI 會不會亂講話」的回答

> 「品牌事實只存在一份檔案裡。五個智能體都是從那份檔案繼承下來的，
> 沒有自己的記憶。你改一個字，五個渠道同一秒全部跟著改。
> 這就是為什麼多渠道經營最常見的『同一件事四種說法』在這裡不會發生。」

### 對「會不會誤打到我的正式系統」的回答

> 「任何對外呼叫之前，系統會先跑一次內部通訊測試，把『要打哪個端點、送什麼』
> 全部印出來但不真的送。這關沒過，後面一步都不會執行。」

### 收尾（價格框架，ch07_p02）

> 「聽起來很貴？當你取代的是一個 $8,000–$15,000/月 的部門時，
> 建置費在第一個月就已經被省下來的預算蓋過去了。」

---

## 8. 使用方式

```bash
# 離線跑完整流程（零憑證、零網路）
python main.py --mock

# 空跑：跑完整流程，印出將呼叫哪些外部端點、送出什麼，但不發送
python main.py --mock --dry-run

# 空跑但用真實模型生成內容：業務系統不送出，但 LLM 會實際呼叫（有費用）
python main.py --live --dry-run

# 核准本週策略備忘錄（必須具名）後才會放行發布
python main.py --mock --approve --approved-by "Elena Torres"

# 指定行銷階段（需存在於 brand_context.yml 的 STAGE_MAP）
python main.py --mock --stage nurture

# 不污染工作樹：狀態與稽核日誌都寫到暫存目錄
python main.py --mock --state-file /tmp/state.json --audit-file /tmp/audit.jsonl

# 串接真實 API（需要 ANTHROPIC_API_KEY 與各整合的憑證環境變數）
python main.py --live --notify telegram
```

### 旗標

| 旗標 | 說明 |
| --- | --- |
| `--mock` / `--live` | 離線（預設）／串真實 API，互斥 |
| `--dry-run` | 跑完流程但不對業務系統發送，並完整揭露外部呼叫內容（**不涵蓋 LLM 呼叫**，見下表） |
| `--notify` | `console`（預設）/ `telegram` / `gmail` / `line` / `whatsapp` |
| `--config` | 設定檔路徑，預設同目錄 `config.yaml` |
| `--state-file` | 核准狀態檔路徑 |
| `--audit-file` | 稽核日誌 JSONL 路徑 |
| `--approve` / `--approved-by` | 核准本週備忘錄（必須具名） |
| `--stage` | 覆寫本次執行的行銷階段 |
| `--now` | 覆寫當前時間（ISO 8601），供測試取得可重現的排程時點 |

### ⚠️ `--dry-run` 到底「不送」什麼（四種組合）

`--dry-run` 擋的是**業務系統**（社群 / Email / CRM / CMS / GA4），
**不擋 LLM 內容生成**。因此 `--live --dry-run` 仍會實際呼叫 Anthropic API 並產生費用：

| 組合 | LLM 呼叫 | 業務系統送出 | 成本 |
| --- | --- | --- | --- |
| `--mock` | ❌ 讀 fixture | ❌ | 0 |
| `--mock --dry-run` | ❌ 讀 fixture | ❌ | 0 |
| `--live --dry-run` | ✅ **真實呼叫** | ❌ | **有** |
| `--live` | ✅ 真實呼叫 | ✅ | 有 |

**為什麼 `--live --dry-run` 不改成用 mock LLM**：dry-run 的價值就是讓人預覽
**真實生成的內容**再決定要不要送出。換成 fixture 就失去意義了。
所以行為保留，但程式與本文件都明講代價 —— 要完全零外部呼叫、零成本，
請用 `--mock --dry-run`。

程式對應行為：`--mock --dry-run` 印綠色訊息「LLM 與業務系統皆未實際呼叫」；
`--live --dry-run` 改印**琥珀警示**點名「已實際呼叫 Anthropic API，會產生費用」，
並計入 `amber_count`。

---

## 9. 白牌抽換：#30 的共同技術底座

`brand_context.yml` 是**整組可抽換**的單位，這正是 #30 白牌 AI 營運部門的基礎：

```yaml
white_label:
  enabled: true
  tenant_slug: "acme-client-a"
  overrides:
    brand:
      name: "Acme 客戶 A"
    guardrails:
      banned_terms: ["最強", "第一"]
```

`enabled: true` 時，Orchestrator 把 `overrides` 深層合併進品牌上下文 →
版本 +1 → 立刻級聯給五個子智能體。**程式碼、提示詞一行都不用改。**
多租戶隔離沿用同一個 `tenant_slug` 欄位（`[RESELLER_SLUG]/[SUB_CLIENT_SLUG]`）。

---

## 10. 檔案清單

```
demo21-marketing-swarm/
├── README.md              # 本文件
├── config.yaml            # 執行設定（不含任何品牌事實）
├── brand_context.yml      # 單一真理來源（可整組抽換）
├── main.py                # 主流程：四步驟 + 兩道閘門
├── orchestrator.py        # 蜂群編排器：繼承 / 級聯 / 安全閥 / 派工
├── audit.py               # 稽核軌跡（JSONL，只追加）
├── prompts/
│   ├── strategy_memo.md   # Orchestrator 的策略備忘錄提示詞
│   ├── content_agent.md
│   ├── social_agent.md
│   ├── email_agent.md
│   ├── lead_gen_agent.md
│   └── analytics_agent.md
├── mock/                  # 離線 fixture（六份，對應備忘錄與五個 agent）
└── test_main.py           # happy / edge / integration 各一
```

**依賴**：Python 3.10+、PyYAML、pytest。對外呼叫一律用標準庫 `urllib.request`，不用 `requests`。

**驗收**：`python main.py --mock` 零憑證、零網路跑完並印出結果。
