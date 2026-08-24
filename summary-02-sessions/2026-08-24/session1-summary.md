# Session 1 — 2026-08-24

## 完成事項

### 文件同步

- 更新根目錄 `CLAUDE.md` 與 `demo/README.md`：反映 `demo/` 從「10 模組」擴充到「30 模組」（Level 1 #1-10、Level 2 #11-20、Level 3 #21-30）的現況，補上 `SPEC-11-20.md` / `SPEC-21-30.md` 連結與各層級模組總覽表

### `_shared/llm_client.py` 從直連 Anthropic API 改成呼叫本機 `claude -p`（三輪 code-writer → code-qa → code-reviewer 鐵律）

- **架構決策**：`--live` 模式不再讀 `ANTHROPIC_API_KEY` 直打 `api.anthropic.com`（燒 API Credits），改用 `subprocess` 呼叫本機已登入的 `claude` CLI headless 模式（`claude -p`），走使用者 Claude Max 訂閱（$0）
- **修掉 bug #1**：`--tools ""` 只擋內建工具、擋不住 CLAUDE.md／git status／專案上下文被自動掛載進回應（code-reviewer 實測發現，回應內容逐字引用了呼叫當下的 git status）。修法：argv 加 `--safe-mode`
- **修掉 bug #2**（比 bug #1 更隱蔽，是主 Claude 用 demo01 真實 `prompts/briefing.md` 端對端測試時才發現）：`shutil.which("claude")` 在 Windows 上解析到 `claude.CMD`（npm shim，非真執行檔），Windows 執行 `.CMD` 會多一層 `cmd.exe` 轉呼叫，`cmd.exe` 把 system prompt 裡的 `<`、`>` 角括號佔位符（demo 提示詞幾乎都這樣寫）當重導向運算子誤判，導致 `--system-prompt` 失效、`--output-format json` 也失效。修法：新增 `_resolve_real_executable()`，在 Windows 上把 `.cmd`/`.bat` shim 解析成背後真正的 `.exe`（`...\node_modules\@anthropic-ai\claude-code\bin\claude.exe`），解析失敗一律安全退回原路徑
- 新增 `demo/_shared/test_llm_client.py`：30 個測試（29 個 mock/白箱 + 1 個預設 SKIPPED 的真實 CLI 整合測試，`OPENCLAW_RUN_LIVE_CLI_TESTS=1` 才會執行）
- 同步更新 `demo/CONTRACT.md` §3 反映新的 CLI 呼叫細節

### 真實環境串接（Google Calendar + Gmail）

- **Google Calendar OAuth**：沿用既有的 `jessica-459902` GCP OAuth client（原本為 AutoRead-GoogleBook 建立），走過一次 Authorization Code + loopback callback 流程，取得 `calendar.readonly` scope 的 access_token/refresh_token，存於 `~/.openclaw/google_calendar_token.json`；`GOOGLE_CALENDAR_TOKEN` 環境變數已用 `setx` 永久設定指向該檔
- **`gws`（Google Workspace CLI，`@googleworkspace/cli`）**：全域 npm 安裝；過程中修了一個環境問題（`gws.exe` 缺 Windows Universal CRT 動態函式庫 `api-ms-win-crt-heap-l1-1-0.dll`，用 winget 裝 `Microsoft.VCRedist.2015+.x64` 解決）；已用 `gws auth login --readonly --services gmail` 完成 OAuth 登入，帳號 `chenghyang2001@gmail.com`，有 refresh_token
- **修掉 bug #3**：`subprocess.run(["gws", ...], shell=False)` 在 Windows 用裸指令名（無副檔名）不會自動套用 PATHEXT 解析，直接 `FileNotFoundError`。修法：改用 `shutil.which("gws")` 解析出的完整路徑，掃過整個 `demo/` 目錄後確認此模式出現在 3 處，一次修完：
  - `demo/_shared/notifier.py`（`--notify gmail`，30 個 demo 共用）
  - `demo/demo01-morning-briefing/sources/email_source.py`
  - `demo/demo02-inbox-zero/main.py`
  - 新增/補齊 9 個測試（3 檔 × happy/edge/integration），走 code-writer → code-qa（複雜度 medium，未派 reviewer）

### demo01、demo02 端對端真實資料驗證

- **demo01（晨間情報簡報）**：`python main.py --live` 完整跑通——真實抓到今天的 Google Calendar 事件、真實讀到 Gmail 未讀信（含兩則安全警示信件被 AI 正確摘入待辦）、真實抓到 BBC RSS 新聞（CNN feed SSL 失敗正確降級為 AMBER，不中斷全局）
- **demo02（收件匣清零代理）**：`--live --dry-run` 驗證真實讀 22 封未讀信、VIP 分類邏輯正確（虛構書中範例 VIP 名單對不上真實聯絡人，0 誤判），加入使用者提供的真實 email 到 `vip_senders.individuals` 測試（該地址目前無未讀信，故仍 0 匹配，屬預期）；另用 mock 假信件 + 真實 LLM 示範完整 AI 草擬回信流程（合約續約通知 → AI 草擬回覆，並用 `[[待確認：...]]` 標出需人工決定的關鍵問題，不代替使用者做承諾）

## 關鍵技術筆記

1. **Windows subprocess 裸指令名 gotcha**：`subprocess.run(["cmd_name", ...], shell=False)` 在 Windows 上，若 `cmd_name` 沒有副檔名，`CreateProcess` **不會**自動搜尋 PATHEXT（`.CMD`/`.BAT`/`.EXE`），必須先用 `shutil.which()` 解析出完整路徑再傳給 `subprocess.run`。這次在 `claude` 與 `gws` 兩個 CLI 上都踩到同一個坑。
2. **Windows `.CMD` shim + cmd.exe 重新解析 gotcha**（更隱蔽）：npm 全域安裝的工具在 Windows 上實際是 `.CMD` 包裝腳本，執行時會多一層透過 `cmd.exe` 轉呼叫，`cmd.exe` 會把命令列引數裡的 `<`、`>`、`|`、`&`、`^`、`%` 當特殊運算子重新解讀，即使 Python 這邊用 list 傳參數也防不住。修法：找到 `.CMD` shim 背後真正指向的 `.exe`（通常在 `node_modules\<pkg>\bin\<name>.exe`），直接呼叫那個真執行檔繞過 cmd.exe 這層。
3. `gws.exe`（Rust 編譯）在部分 Windows 機器上會缺 Universal CRT DLL，症狀是 `error while loading shared libraries: api-ms-win-crt-*.dll: cannot open shared object file`——這是缺 VC++ Redistributable，不是套件本身壞掉，`winget install Microsoft.VCRedist.2015+.x64` 可解。
4. demo01 的 `--mock` 輸出其實不是 LLM 生成的：`LLMClient(mock=True).complete()` 只回傳 `"[MOCK] ..."` 佔位字串，demo01 的 `produce_briefing_text()` 偵測到這個佔位字串後會改走**本地範本渲染器**（`render_offline_briefing`）產出看起來完整的簡報。demo02 沒有這層本地範本回退，`--mock` 模式下草稿欄位就是裸的 `[MOCK] ...` 字串——要看真實草擬內容需要額外手動組 mock 資料 + `LLMClient(mock=False)` 才行。

## 待辦（技術債，本 session 未處理）

- 30 個 demo 裡有 14 個 `mainXX.py` 仍寫死 `required_env = [..., "ANTHROPIC_API_KEY", ...]` 檢查（demo03/04/08/10/11/12/18/19/20/21/22/25/26/27/30），這些檢查已過時（`llm_client.py` live 模式不再需要這個環境變數），code-writer 在第一輪任務時回報過，尚未處理
- `demo/.env.example` 的 `ANTHROPIC_API_KEY` 說明文字仍寫「`--live` 模式必要」，跟新架構不符，但這個檔案被本機權限設定擋住讀寫，未能更新
- `demo02-inbox-zero/config.yaml` 的 `vip_senders` 仍是書中虛構範例資料（`@northwind-retail.com` 等），只加了使用者一個真實 email 做測試，真正要用時需要完整替換成真實客戶/廠商名單

## 產出檔案

| 檔案 | 動作 | 說明 |
| --- | --- | --- |
| `CLAUDE.md` | 修改 | 反映 30 模組現況 |
| `demo/README.md` | 修改 | 反映 30 模組現況，含 Level 1/2/3 三張表 |
| `demo/CONTRACT.md` | 修改 | §3 更新為 `claude -p` CLI 呼叫細節 |
| `demo/_shared/llm_client.py` | 修改 | live 模式改用 `claude -p`，`--safe-mode`，`_resolve_real_executable()` |
| `demo/_shared/test_llm_client.py` | 新增 | 30 個測試 |
| `demo/_shared/notifier.py` | 修改 | `gws` 路徑解析修正 |
| `demo/_shared/test_notifier.py` | 新增 | 3 個測試 |
| `demo/demo01-morning-briefing/sources/email_source.py` | 修改 | `gws` 路徑解析修正 |
| `demo/demo01-morning-briefing/test_main.py` | 修改 | 補 3 個測試 |
| `demo/demo02-inbox-zero/main.py` | 修改 | `gws` 路徑解析修正 |
| `demo/demo02-inbox-zero/test_main.py` | 修改 | 補 3 個測試 |
| `demo/demo02-inbox-zero/config.yaml` | 修改 | 加一個真實 VIP email 供測試 |

## Commits

1. `189d29a` 更新 CLAUDE.md 與 demo/README.md 反映 30 模組現況
2. `a759741` llm_client.py live 模式改用本機 claude -p CLI，不再燒 API Credits
3. `6955dfb` 修正 Windows .CMD shim 導致 system prompt 被 cmd.exe 破壞的問題
4. `5efb06d` 修正 gws 裸指令名稱在 Windows 找不到的問題（3 處）
5. `b23faee` demo02 config.yaml 加入真實 VIP 個人 email 供實測

## HANDOFF（下次 session 優先處理）

### 立即行動

- [ ] 決定要不要清掉 30 個 demo 裡 14 個 `mainXX.py` 殘留的 `ANTHROPIC_API_KEY` 過時檢查（demo03/04/08/10/11/12/18/19/20/21/22/25/26/27/30）
- [ ] 繼續逐一驗證 demo03～demo30 的 `--live` 端對端流程（目前只驗證過 demo01、demo02）
- [ ] `demo02-inbox-zero/config.yaml` 的 `vip_senders` 若要正式使用，需要使用者提供完整的真實客戶/廠商網域與 email 清單

### 進行中（需接續）

- `_shared/llm_client.py` 的 `claude -p` 化已完整驗證通過（含真實端對端呼叫），視為穩定；但效能取捨（單次呼叫比舊版 HTTP 慢，`--safe-mode` 加上後約 1-2 秒 API 時間，逐筆呼叫的 demo 批次跑多筆會放大總耗時）尚未在文件中系統性警示，code-reviewer 有提過但列為 NICE_TO_HAVE 未強制處理
- demo01、demo02 已驗證「真實資料 + 真實 LLM」端對端可用；其餘 28 個 demo 尚未逐一測試，不確定是否也有類似的 gws/其他 CLI 裸指令名問題（這次是掃過 gws 相關呼叫，其他外部 CLI 呼叫模式未全面掃描）

### 注意事項

- `demo/.env.example` 被本機 Claude Code 權限設定擋住 Read/Edit（可能是保護 env 類檔案的規則），如果之後真的需要更新它，要先確認為什麼被擋、是否需要使用者手動處理
- Google Calendar token（`~/.openclaw/google_calendar_token.json`）只有 access_token（約 1 小時過期）+ refresh_token，但 `demo01/sources/calendar_source.py` 目前**沒有自動 refresh 邏輯**，過期後要嘛重跑一次 OAuth 流程，要嘛之後找時間幫 `calendar_source.py` 補上用 refresh_token 換新 access_token 的邏輯
- `gws` 與 Google Calendar OAuth 都是沿用同一組既有的 GCP OAuth client（專案 `jessica-459902`，原本是 `AutoRead-GoogleBook` 專案的憑證），不是為這個 kindle-50 專案新建的獨立 client——如果之後要正式對客戶展示或部署，可能需要考慮建立獨立的 GCP 專案憑證，不要繼續依賴這組個人用途的舊憑證
