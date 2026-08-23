# 快速啟動方案（The Quick-Start Experience）

> 來源：《The OpenClaw Income Engine》第 04 章「Bundling Economics: Features vs. Experience」

把 4 個自動化合併成**一個體驗**，而不是 4 個功能。

---

## 方案內容

| BOT | 模組 | 目錄 |
| --- | --- | --- |
| #1 | 早報（Morning Briefing） | [`../demo01-morning-briefing`](../demo01-morning-briefing/) |
| #2 | 收件匣（Inbox Zero） | [`../demo02-inbox-zero`](../demo02-inbox-zero/) |
| #5 | 評論（Review Monitor） | [`../demo05-review-monitor`](../demo05-review-monitor/) |
| #9 | 報表（Sales Report） | [`../demo09-sales-report`](../demo09-sales-report/) |

**售價：$995 設定費 + $200/月**

---

## 為什麼打包比拆賣好

| | 單獨銷售（Features） | 快速啟動方案（Experience） |
| --- | --- | --- |
| 定價 | $1,300 setup + $335/mo | **$995 setup + $200/mo** |
| 客戶心理 | 「這是額外工具嗎？值得付月費嗎？」 | 「週一醒來，信箱已整理，報表在手機，生意在你睡覺時已運作」 |
| 部署時間成本 | 基準 | **↓ 60%** |
| 單日參與營收 | 基準 | **↑ 3 倍** |
| 單客首年營收 | $5,320（若賣得掉） | $3,395（成交率高得多） |

### 這裡有個反直覺的地方

打包後**單價更低**（setup 少 $305，月費少 $135），單客首年營收也更低。那為什麼要打包？

因為 **3 倍成交率**：

```
拆賣：1 成交 × $5,320  = $5,320
打包：3 成交 × $3,395  = $10,185   ← 淨勝
```

再加上部署成本降 60%，實際利潤差距更大。

**核心洞察**：你賣的不是折扣，是**降低客戶的決策摩擦**。
客戶不想評估 4 個工具值不值得，他只想知道「我的週一早晨會變成什麼樣」。

---

## 使用方式

```bash
# 一次跑完 4 個模組（mock 模式）
python run_all.py --mock

# 推到 Telegram
python run_all.py --mock --notify telegram

# 依客戶產業算報價
python pricing_calculator.py --industry ecommerce --modules 1,2,5,9
```

---

## 交付時間軸（Sarah Chen 案例實證）

第 04 章的真實案例，**Sarah 總投入時間只有 6 小時**：

```
TUE ────────► WED-THU ────────► FRI ────────► SAT ────────► SUN
發現通話       測試環境部署      客戶驗收運行   系統首次      客戶訊息：
(45 mins)     與空跑           與解說        獨立運行      「這真是太不可思議了」
```

| 里程碑 | 收款 |
| --- | --- |
| 提案接受 | **$995 設定費付清** |
| 客戶驗收（FRI） | **$200/月 開始生效** |
| 單客首年預估營收 | **$3,395** |

### 每一天實際要做什麼

| 日 | 動作 | 對應指令 |
| --- | --- | --- |
| **TUE** | 45 分鐘發現通話。**不要提任何系統名稱**，只問他的早晨長什麼樣 | 用 `proposal-template.md` 的訪談題目 |
| **WED-THU** | 在測試環境部署 4 個模組並空跑 | `run_all.py --dry-run` |
| **FRI** | 客戶驗收。當著他的面跑一次，講解每個輸出 | `run_all.py --live` |
| **SAT** | 系統首次無人值守獨立運行 | 排程 cron |
| **SUN** | 客戶自己發現它在運作 | — |

---

## 話術核心：不提及任何系統名稱

Sarah Chen 的成交話術（第 04 章原文）：

> 「想像一下在週一早晨醒來，發現你的收件匣已經被分類好，銷售報告已經在你的手機裡……在你打開筆電之前，一切已經準備就緒。這就是我要為你打造的。」

**注意這段話裡沒有出現**：OpenClaw、Claude、API、自動化、Agent、Webhook。

一個字都沒有。

---

## 領導者行動清單（第 04 章收尾）

- ✅ **包裝大於建置** — 產生最多營收的不是做最多系統的人，而是最會包裝的人
- ✅ **框架為體驗** — 客戶買的不是功能列表，而是「早晨醒來生意已在運作」的安心感
- ✅ **建立標準化文件** — 記錄配置與恢復時間，用真實數據取代預測
- ✅ **隨時準備綑綁提案** — 在客戶問價錢之前，提案範本就必須就緒

---

## 相關檔案

- [`proposal-template.md`](proposal-template.md) — 客戶提案範本（含發現通話題目、資料處理披露、驗收條款）
- [`run_all.py`](run_all.py) — 一鍵跑完 4 模組
- [`pricing_calculator.py`](pricing_calculator.py) — 依產業與模組組合算報價
