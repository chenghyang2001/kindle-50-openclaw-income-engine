# CLAUDE.md — Kindle 50｜The OpenClaw Income Engine

## 專案概述

《The OpenClaw Income Engine》(Soren Ashcroft) 的讀書筆記與**可執行實作**。

兩個部分：

1. **`pdf/`** — 16 份 NotebookLM 生成的章節簡報（純圖片 PDF，**無文字層**）
2. **`demo/`** — 依第 03、04 章實作的 10 個自動化模組，每個都可離線執行

## 目錄結構

```
kindle-50-openclaw-income-engine/
├── CLAUDE.md              # 本檔
├── README.md              # 專案總覽 + PDF 章節對照表
├── .gitignore
│
├── pdf/                   # 16 份章節簡報（圖片 PDF，16:9）
│
└── demo/                  # 10 模組實作
    ├── PLAN.md            # 建置計畫（模組規格、Session 拆分、驗收標準）
    ├── CONTRACT.md        # _shared API 契約（凍結）
    ├── README.md          # demo 總覽 + 快速上手
    ├── requirements.txt
    ├── .env.example
    ├── _shared/           # 基礎設施層
    ├── demo01-morning-briefing/
    ├── ...                # demo02 ~ demo09
    ├── demo10-followup-sequence/
    └── bundle-quickstart/ # 打包方案 + 客戶提案範本
```

## 技術堆疊

| 項目 | 選擇 | 理由 |
| --- | --- | --- |
| Python | **3.10+** | 使用 `X \| None` 型別語法 |
| 第三方依賴 | **只有 PyYAML + pytest** | 降低部署摩擦，客戶端環境不可控 |
| HTTP | **標準庫 `urllib.request`** | 禁止 `requests`，減少依賴 |
| HTML 解析 | **標準庫 `html.parser`** | 禁止 BeautifulSoup / lxml |
| 時區 | **標準庫 `zoneinfo`** | 禁止 pytz |
| 金額運算 | **`decimal.Decimal`** | 財務精度，禁止 float |
| LLM | **`claude-sonnet-5`** | 成本考量，非 Opus |
| 通知 | **Telegram（預設）** | 原著用 WhatsApp，台灣滲透率低 |

## 執行方式

```bash
cd demo
python -m pip install -r requirements.txt

# 任一模組，零憑證離線跑
cd demo01-morning-briefing
python main.py --mock

# 測試
python -m pytest test_main.py -v
```

三種模式：`--mock`（預設，離線）／`--live`（真實 API）／`--dry-run`（跑但不送）。

## 重要架構決策

### 1. 混合式架構（`_shared/` + 獨立業務邏輯）

書中商業模式是「單品可賣、也能打包」，架構要對得上。共用基礎設施避免 10 份重複，
`_shared/package.py` 可把共用層 vendor 進單一 demo 產出獨立交付版。

### 2. Contract-First

`demo/CONTRACT.md` 是**凍結的 API 契約**。修改 `_shared/` 的公開簽名前必須先改契約
並確認所有 demo 的相容性。這份契約讓 11 個模組能並行開發。

### 3. Mock 優先

`--mock` **不呼叫任何外部 API**（含 Claude），用 `mock/*.json` fixture。
理由：開發期反覆測試不該產生 API 帳單；客戶簡報時不會因為網路問題出糗。

**鐵律**：`--live` 缺憑證必須明確報錯退出，**絕不可靜默降級回 mock**。

### 4. 自主權階梯是預設安全網

`_shared/autonomy.py` 實作 `READ_ONLY → DRAFT → SUPERVISED_AUTO` 三段式。
**預設一律 `DRAFT`**。`SUPERVISED_AUTO` 白名單為空會拋 `AutonomyError`。
草稿模式未滿 14 天開全自動會發警告（第 04 章鐵律）。

### 5. RAG 診斷矩陣

`_shared/diagnostics.py`：RED 停擺退出 / AMBER 品質降級但繼續 / GREEN 正常。
原則：**品質降級不該讓系統停擺，但絕不可靜默通過**。

### 6. 提示詞獨立成檔

一律放 `prompts/*.md`，**不內嵌在 `.py` 字串**。
理由：提示詞是本專案的核心商業資產，要能被非工程師閱讀與修改。

## 開發紀律

- 註解與 docstring **繁體中文**
- 檔案 I/O 一律明確 `encoding="utf-8"`（Windows cp950 陷阱）
- 禁止硬編碼 `C:\Users\...`，用 `Path(__file__).parent` 或 `Path.home()`
- 禁止硬編碼金鑰，一律 `os.environ`
- 禁止裸 `except:`
- 公開函式加 type hints
- 函式 > 30 行就拆

## PDF 處理注意

`pdf/` 底下是**純圖片 PDF，無文字層**。`pdftotext` 抽不出東西
（17 頁只有 330 字元，全是空白頁標記）。要讀內容必須用 PyMuPDF 渲染成圖片：

```python
import pymupdf
doc = pymupdf.open(path)
pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.16, 1.16))  # ~1600px 寬
```

1600px 是中文小字可讀性與檔案大小的平衡點。

## 已知的原著數據矛盾

第 03 章模組矩陣的「內部回收時間」加總為 **168 hrs/mo**，但封面與複利效應圖寫
**40–60 hrs/mo**。兩者差近 3 倍。

推測：矩陣值為「客戶端價值主張」，40–60 為「自用實際回收」。原著未說明此區別。
**對外引用時要選定口徑並說明基準**，`demo/README.md` 已註明。

## 相關文件

- `demo/PLAN.md` — 完整建置計畫與各模組規格
- `demo/CONTRACT.md` — `_shared` API 契約
- `demo/bundle-quickstart/proposal-template.md` — 客戶提案範本
