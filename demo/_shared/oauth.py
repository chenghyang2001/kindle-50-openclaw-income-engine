"""Google OAuth token 讀取 + 自動續期（第 04 章 RAG 矩陣的 `oauth_error` 對策）。

書中把「OAuth Error」列為紅色症狀，對策寫的是「設定永久 Refresh token」——
但光有 refresh_token 還不夠：Google 的 access_token 一小時就過期，
若程式只是把 token 檔裡的 access_token 撈出來直接用，一小時後整條流程就會 RED 中止，
使用者得手動重跑一次 OAuth 授權。本模組把「過期就自己換一張」這件事收斂到單一入口。

**適用範圍**：目前只有 demo01（`GOOGLE_CALENDAR_TOKEN` -> `sources/calendar_source.py`）
接得上這個元件。其餘用到 `token_env` 的模組（demo05/06/14 的環境變數直接存 bearer
token 字串、demo11 存的是 WordPress 應用程式密碼）憑證模型不同，不是「路徑指向 OAuth
token JSON 檔」，暫不適用，見 `CONTRACT.md` §10。

四個刻意的設計取捨：

1. **只用標準庫**。這批交付包要能丟到客戶機器上直接跑，
   少一個 `pip install requests` 就少一通安裝失敗的支援電話。
2. **無從判斷是否過期時不 refresh**。舊格式的 token 檔沒有 `obtained_at` / `expires_in`，
   這時維持既有行為（直接用現有 access_token），讓 API 端的 401 去反映真實狀態——
   拿不確定的資訊去猜「大概過期了」而發出多餘的續期請求，反而是製造新的失敗點。
3. **缺續期憑證一律明確報錯**。過期了卻湊不出 refresh 請求時絕不靜默回傳舊 token
   （專案鐵律：`--live` 缺憑證必須明確退出，靜默降級會讓客戶以為系統還活著）。
4. **本模組不自己呼叫 `diagnostics.red()`**。`Diagnostics.red()` 本身就是終結點
   （`exit_on_red=True` 時 `sys.exit(1)`，否則 `raise RedAlert`）——若這裡先 red 一次、
   呼叫端的 `except OAuthError` 又再處理一次，等同紅色警報喊兩次，或者讓呼叫端的
   except 永遠抓不到東西（因為 RedAlert 已經先冒泡出去，OAuthError 根本輪不到被 raise）。
   所有失敗路徑改成 `raise OAuthError(...)`，把 symptom / cause / fix 三段診斷資訊
   掛在例外物件上，交由呼叫端自行決定何時、用哪個 `Diagnostics` 實例喊出紅色警報
   （見 `sources/calendar_source.py` 與 `CONTRACT.md` §10「呼叫端契約」）。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# 提前 60 秒判定過期：token 剛好卡在到期邊界時，請求送到 Google 那端可能已經失效，
# 寧可早一點換新，也不要賭這一秒的競態。
EXPIRY_SKEW_SECONDS = 60

# 續期請求的逾時秒數。與 Calendar/Gmail 的資料請求分開設定：
# 換 token 是整條流程的前置關卡，卡住比慢一點更糟，給足 30 秒後就該失敗退出。
REFRESH_TIMEOUT_SECONDS = 30

# 組出 refresh_token 授權請求的最小欄位集。缺任何一個都湊不出合法請求。
REQUIRED_REFRESH_FIELDS = ("refresh_token", "client_id", "client_secret", "token_uri")


class OAuthError(RuntimeError):
    """OAuth token 讀取或續期失敗（檔案缺失、格式錯誤、缺憑證、token endpoint 失敗）。

    攜帶 symptom / cause / fix 三段診斷資訊（對應 `Diagnostics.red()` 的三個參數），
    但**不會自己呼叫** `diagnostics.red()`——理由見本模組頂端 docstring 的第 4 條取捨。
    呼叫端捕捉本例外後，應自行取用 `.symptom` / `.cause` / `.fix` 呼叫
    `diagnostics.red(...)`，再視需要轉成自己的領域錯誤（例：demo01 轉 `SourceError`）。

    診斷訊息一律只提檔案路徑與欄位名稱，**絕不帶入任何權杖或密鑰的值**——
    這些訊息會進 stderr、log 與客戶的截圖。
    """

    def __init__(self, message: str, *, symptom: str, cause: str, fix: str) -> None:
        super().__init__(message)
        self.symptom = symptom
        self.cause = cause
        self.fix = fix


def load_access_token(token_path: Path, token_env: str, diagnostics: Any) -> str:
    """讀 OAuth token 檔，必要時用 refresh_token 自動換新，回傳可用的 access_token。

    token_path:  token JSON 檔位置（由呼叫端從 `token_env` 指定的環境變數取得）
    token_env:   環境變數名稱，只用於組出「該去改哪裡」的診斷訊息
    diagnostics: `_shared.diagnostics.Diagnostics`（或任何提供 amber 的替身）

    `diagnostics` 參數之所以還留著，是因為它仍被用在**唯一一條**留在本模組內部
    處理、不需要呼叫端接手的路徑：續期成功但寫回檔案失敗、且 refresh_token
    沒有輪替時，發一次 `diagnostics.amber()` 讓流程繼續（見 `_write_token_file`）。
    除此之外的所有失敗都改成 `raise OAuthError`，不會再呼叫 `diagnostics.red()`。
    """
    token_data = _read_token_file(token_path, token_env)
    access_token = _extract_access_token(token_data, token_path)
    if not _is_expired(token_data):
        return access_token

    response = _refresh_access_token(token_data, token_path, token_env)
    new_access_token = response.get("access_token")
    if not new_access_token:
        # 即使 token 檔裡有可用的 refresh_token，若回應沒有 access_token 欄位，
        # 仍然視為不可用而報錯——refresh_token 只是「能不能換」的憑證，
        # 不是「現在有沒有可用 token」的答案；不擋的話合併時會保留舊的
        # access_token，等於靜默回傳過期權杖，違反第 3 條設計取捨。
        raise OAuthError(
            f"續期回應缺少 access_token：{token_path}",
            symptom="oauth_error",
            cause=f"token endpoint 回應缺少 access_token 欄位，無法完成續期：{token_path}",
            fix="確認 token_uri 指向 Google 的 OAuth token endpoint，並重新執行一次授權流程",
        )

    merged_token_data = _merge_refreshed_token(token_data, response)
    _write_token_file(token_path, token_data, merged_token_data, response, diagnostics)
    return str(new_access_token)


def _read_token_file(token_path: Path, token_env: str) -> dict:
    """讀 token JSON。讀不到、不是合法 JSON、或頂層不是物件都一律 raise OAuthError。"""
    try:
        token_data = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthError(
            f"token 檔無法解析：{token_path}",
            symptom="oauth_error",
            cause=f"{token_env} 指向的 token 檔無法讀取或不是合法 JSON：{token_path}",
            fix="重新執行 Google OAuth 流程產生 token，並確認設定永久 refresh token",
        ) from exc

    if not isinstance(token_data, dict):
        raise OAuthError(
            f"token 檔頂層不是 JSON 物件：{token_path}",
            symptom="oauth_error",
            cause=f"{token_env} 指向的 token 檔頂層不是 JSON 物件：{token_path}",
            fix="重新執行 Google OAuth 流程產生 token，並確認設定永久 refresh token",
        )
    return token_data


def _extract_access_token(token_data: dict, token_path: Path) -> str:
    """取出 access_token。`token` 是早期 google-auth 存檔用的欄位名，一併相容。"""
    access_token = token_data.get("access_token") or token_data.get("token")
    if not access_token:
        raise OAuthError(
            f"token 檔缺少 access_token：{token_path}",
            symptom="oauth_error",
            cause=f"token 檔缺少 access_token 欄位：{token_path}",
            fix="確認 OAuth 流程有帶 offline access，並重新產生 token",
        )
    return str(access_token)


def _is_expired(token_data: dict) -> bool:
    """判斷 access_token 是否已過期（或即將過期）。

    缺 `obtained_at` / `expires_in` 時回傳 False——這代表「無從判斷」而非「還沒過期」。
    舊版流程存下來的 token 檔就沒有這兩個欄位，此時維持既有行為（照用現有 token），
    真的失效就讓 API 端回 401，比憑空猜測再發一次續期請求安全。
    """
    obtained_at = _as_seconds(token_data.get("obtained_at"))
    expires_in = _as_seconds(token_data.get("expires_in"))
    if obtained_at is None or expires_in is None:
        return False
    return obtained_at + expires_in - EXPIRY_SKEW_SECONDS <= time.time()


def _as_seconds(value: Any) -> float | None:
    """把秒數欄位轉成 float。不同工具存出來的 token 檔可能寫成字串（例 "3600"）。

    無法解讀就回 None（交給呼叫端當「無從判斷」處理），不要自作主張補 0——
    補 0 會讓「缺欄位」被誤判成「1970 年就過期了」而觸發不必要的續期。
    """
    if value is None or isinstance(value, bool):  # bool 是 int 的子類，float(True)==1.0 會誤判
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _refresh_access_token(token_data: dict, token_path: Path, token_env: str) -> dict:
    """向 token endpoint 換一張新的 access_token，回傳解析後的原始回應。"""
    _require_refresh_fields(token_data, token_path, token_env)
    request = _build_refresh_request(token_data)
    return _post_refresh(request, token_path)


def _require_refresh_fields(token_data: dict, token_path: Path, token_env: str) -> None:
    """續期所需欄位缺一不可、`token_uri` 必須是 https，缺了就明確中止，
    絕不靜默回傳已過期的 token。
    """
    missing = [field for field in REQUIRED_REFRESH_FIELDS if not token_data.get(field)]
    if missing:
        raise OAuthError(
            f"token 檔缺少續期欄位 {missing}：{token_path}",
            symptom="oauth_error",
            cause=f"access_token 已過期，但 token 檔缺少續期所需欄位 {', '.join(missing)}：{token_path}",
            fix=(
                "重新執行 Google OAuth 流程（務必帶 offline access）產生含 refresh_token 的完整 "
                f"token 檔，再確認 {token_env} 指向該檔案"
            ),
        )

    token_uri = str(token_data["token_uri"])
    if not token_uri.startswith("https://"):
        # token_uri 若不是 https，refresh 請求會把 client_secret 用明文送出去；
        # 這不是「格式不嚴謹」的小事，是憑證外洩風險，一律當成缺憑證處理。
        raise OAuthError(
            f"token_uri 不是 https：{token_path}",
            symptom="oauth_error",
            cause=f"token 檔的 token_uri 不是以 https:// 開頭（{token_uri!r}）：{token_path}",
            fix="token_uri 必須是 https，避免 client_secret 明文外洩；重新產生正確的 token 檔",
        )


def _build_refresh_request(token_data: dict) -> urllib.request.Request:
    """組出 `grant_type=refresh_token` 的 POST 請求（標準 OAuth 2.0 續期流程）。"""
    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": str(token_data["refresh_token"]),
            "client_id": str(token_data["client_id"]),
            "client_secret": str(token_data["client_secret"]),
        }
    ).encode("utf-8")
    return urllib.request.Request(
        str(token_data["token_uri"]),
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )


def _post_refresh(request: urllib.request.Request, token_path: Path) -> dict:
    """送出續期請求並解析回應。任何失敗都 raise OAuthError：沒有新 token 就沒有後續流程。"""
    try:
        with urllib.request.urlopen(request, timeout=REFRESH_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 必須排在 URLError 之前（HTTPError 是它的子類）
        raise OAuthError(
            f"OAuth token 續期失敗：HTTP {exc.code}",
            symptom="oauth_error",
            cause=f"OAuth token 續期失敗，token endpoint 回應 HTTP {exc.code}：{token_path}",
            fix=(
                "400/401 多半是 refresh_token 已被撤銷或 client_secret 不符，"
                "請重新執行一次 Google OAuth 授權流程"
            ),
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OAuthError(
            f"OAuth token 續期失敗：{exc}",
            symptom="oauth_error",
            cause=f"OAuth token 續期失敗，無法連線或解析 token endpoint 回應：{exc}",
            fix="確認網路連線與 DNS，並檢查 token 檔中的 token_uri 是否正確，再重跑一次",
        ) from exc


def _merge_refreshed_token(token_data: dict, response: dict) -> dict:
    """把續期回應合併進原本的 token dict（合併，不是整份覆蓋）。

    Google 的 refresh 回應**通常不含 refresh_token**，直接覆寫整份會把它弄丟，
    下次就再也換不到新 token，等於要使用者重跑整個 OAuth 授權。
    回應中值為 None 的欄位也一併略過，理由相同：不讓一個空值蓋掉手上有效的憑證。
    """
    merged = dict(token_data)
    merged.update({key: value for key, value in response.items() if value is not None})
    merged["obtained_at"] = int(time.time())
    return merged


def _refresh_token_rotated(original_token_data: dict, response: dict) -> bool:
    """判斷續期回應是否帶有與磁碟上不同的新 refresh_token（token 輪替）。

    Google 通常不會在 refresh 回應裡帶 refresh_token，但偶爾會做輪替
    （例如安全性事件後強制換發）。這種情況下若接下來寫檔失敗，磁碟上留的
    舊 refresh_token 其實已經被 Google 作廢，問題不是「這次沒存到、下次
    重新 refresh 一遍就好」的品質降級，而是永久性地弄丟了唯一還有效的憑證。
    """
    new_refresh_token = response.get("refresh_token")
    if not new_refresh_token:
        return False
    return str(new_refresh_token) != str(original_token_data.get("refresh_token", ""))


def _write_token_file(
    token_path: Path,
    original_token_data: dict,
    merged_token_data: dict,
    response: dict,
    diagnostics: Any,
) -> None:
    """原子寫回 token 檔；寫失敗時依「refresh_token 是否輪替」分流處理。

    先寫同目錄暫存檔再 `os.replace()`：直接就地覆寫時若中途當掉（斷電、被砍行程），
    使用者的 refresh_token 就殘缺了，等同要重跑整個 OAuth 授權——這個檔案壞掉的代價
    遠高於「這次沒存到」。暫存檔必須同目錄，`os.replace()` 跨檔案系統不保證原子性。
    暫存檔檔名帶 pid，避免同一台機器上兩個行程並發跑同一個 token_path 時互撞。

    暫存檔權限明確設為 `0o600`：用 `os.open(..., O_CREAT, 0o600)` 而非
    `Path.write_text()`，因為後者建立檔案的權限吃行程 umask（POSIX 常見變 0644），
    `os.replace()` 之後永久的 refresh_token 檔案就會被暫存檔的權限蓋掉，從原本
    可能的 0600 被放寬成同機可讀。Windows 上 `os.chmod` 對這些位元基本無效，
    這是已知限制，不為了 Windows 特別繞路，只要 POSIX 上正確、Windows 上不報錯即可。

    寫檔失敗（`OSError`）時分兩種情況：
    - 這次續期回應帶有「與磁碟不同」的新 refresh_token（token 輪替）：磁碟上舊的
      那顆已被 Google 作廢，寫檔失敗等於永久弄丟唯一還有效的續期憑證，必須
      `raise OAuthError`（不可只發 AMBER 後假裝沒事），交由呼叫端 red 並中止。
    - 沒有輪替：磁碟上原本那顆 refresh_token 仍然有效，這只是單純的寫入品質
      降級，發 `diagnostics.amber()` 讓流程繼續——此刻手上這張新的 access_token
      是有效的，照樣回傳能讓今天的簡報正常送出。
    """
    temp_path = token_path.with_name(f"{token_path.name}.{os.getpid()}.tmp")
    try:
        file_descriptor = os.open(str(temp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(merged_token_data, indent=2, ensure_ascii=False))
        os.replace(temp_path, token_path)
    except OSError as exc:
        _remove_quietly(temp_path)
        if _refresh_token_rotated(original_token_data, response):
            raise OAuthError(
                f"refresh_token 已輪替但寫回檔案失敗：{token_path}",
                symptom="oauth_error",
                cause=(
                    f"refresh_token 已輪替但寫回檔案失敗（{exc}）：{token_path}；"
                    "重新授權前，這份剛換到的 access_token 用完即無法再續期"
                ),
                fix="檢查該檔案與所在目錄的寫入權限，並儘快重新執行一次 Google OAuth 授權流程",
            ) from exc
        diagnostics.amber(
            symptom=f"OAuth token 續期成功但寫回檔案失敗（{token_path}）：{exc}",
            fix="檢查檔案/目錄寫入權限；本次 access_token 仍有效",
        )


def _remove_quietly(path: Path) -> None:
    """清掉殘留的暫存檔。清不掉也不能反過來弄掉主流程，因此吞掉 OSError。"""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
