"""oauth.py 的 token 自動續期測試（複雜度：medium，6 個測試案例）。

案例清單：
1. happy path：token 未過期 -> 直接回傳，不發網路請求
2. edge：token 過期 -> 自動續期成功，且保留原 refresh_token
3. error：續期被 token endpoint 回 HTTP 400 -> raise 帶診斷欄位的 OAuthError
4. MUST_FIX #2 情境 a：refresh_token 輪替 + 寫回失敗 -> 必須 raise OAuthError（不可只 AMBER）
5. MUST_FIX #2 情境 b：refresh_token 未輪替 + 寫回失敗 -> 維持 AMBER，仍回傳新 access_token
6. 端對端：真實 `_shared.diagnostics.Diagnostics` 收到 OAuthError 的診斷欄位後，
   確實冒出 `RedAlert`（驗證呼叫端契約：oauth.py 不自己 red，呼叫端接手後行為正確）

全部用 `monkeypatch` 攔掉 `urllib.request.urlopen` 與（第 4/5 項）`os.open`，**絕不打真實網路**——
真的呼叫 Google token endpoint 會消耗使用者的 refresh_token 配額、
在沒有憑證的 CI 機器上必然失敗，而且測試結果會隨網路狀況飄移。

token 檔一律用 pytest 的 `tmp_path` 現造，測完自動清掉，
不碰使用者機器上真正的 OAuth 憑證檔。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

import pytest

MODULE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(MODULE_DIR.parent))  # 讓 `from _shared...` 可解析
sys.path.insert(0, str(MODULE_DIR))  # 讓「直接以腳本執行本檔」的 fallback import 也能用

import _shared.oauth as oauth_module  # noqa: E402
from _shared.oauth import OAuthError, load_access_token  # noqa: E402

TOKEN_ENV = "GOOGLE_CALENDAR_TOKEN"
TOKEN_URI = "https://oauth2.example.invalid/token"


class _FakeDiagnostics:
    """極簡診斷替身：只記錄呼叫內容，`.red()` 呼叫後正常 return（不像真實
    `Diagnostics` 會 `sys.exit` 或 `raise RedAlert`）。

    oauth.py 現在**不會自己呼叫** `.red()`（見 MUST_FIX #1 的重新設計），
    所以本替身留著 `red_calls` 只是為了讓測試斷言「oauth.py 確實沒有呼叫過
    diagnostics.red()」，真正驗證「呼叫端 red 之後會不會冒出 RedAlert」的
    是案例 6，那裡改用真實的 `Diagnostics`。
    """

    def __init__(self) -> None:
        self.red_calls: list[dict[str, str]] = []
        self.amber_calls: list[dict[str, str]] = []

    def red(self, symptom: str, cause: str, fix: str) -> None:
        self.red_calls.append({"symptom": symptom, "cause": cause, "fix": fix})

    def amber(self, symptom: str, fix: str) -> None:
        self.amber_calls.append({"symptom": symptom, "fix": fix})


class _FakeResponse:
    """模擬 `urlopen()` 回傳的物件：支援 with 語法與 read()。"""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        return False


def _write_token(token_path: Path, **overrides: Any) -> dict:
    """寫一份完整的 token 檔到 tmp_path，回傳寫入的內容。"""
    payload: dict[str, Any] = {
        "access_token": "現有的存取權杖",
        "refresh_token": "長期有效的續期權杖",
        "client_id": "fake-client-id.apps.googleusercontent.invalid",
        "client_secret": "fake-client-secret",
        "token_uri": TOKEN_URI,
        "obtained_at": int(time.time()),
        "expires_in": 3600,
    }
    payload.update(overrides)
    token_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def _broken_os_open(*args: Any, **kwargs: Any) -> int:
    """模擬磁碟寫入從一開始就失敗（例如權限不足、磁碟已滿）。"""
    raise OSError("模擬磁碟寫入失敗")


# 1. happy path：token 還在有效期內 -> 直接回傳現有 access_token，
#    完全不發網路請求，也不動 token 檔（省一次來回，也避免無謂的寫入風險）。
def test_valid_token_returns_directly_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    _write_token(token_path)
    original_content = token_path.read_text(encoding="utf-8")

    def _forbidden_urlopen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("token 未過期時不該發出任何網路請求")

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", _forbidden_urlopen)
    diagnostics = _FakeDiagnostics()

    assert load_access_token(token_path, TOKEN_ENV, diagnostics) == "現有的存取權杖"
    assert token_path.read_text(encoding="utf-8") == original_content
    assert diagnostics.red_calls == []
    assert diagnostics.amber_calls == []


# 2. edge：token 已過期 -> 自動續期。重點是 Google 的續期回應「不含 refresh_token」，
#    必須確認原本的 refresh_token 沒被覆蓋掉（弄丟就要重跑整個 OAuth 授權）。
def test_expired_token_refreshes_and_preserves_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    stale_obtained_at = int(time.time()) - 7200  # 兩小時前拿的，expires_in=3600 早就過期
    _write_token(token_path, access_token="過期的存取權杖", obtained_at=stale_obtained_at)
    captured: dict[str, Any] = {}

    def _fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        captured["timeout"] = timeout
        # Google 實際的續期回應就是這個形狀：只給新的 access_token，不重發 refresh_token
        return _FakeResponse(
            json.dumps({"access_token": "全新的存取權杖", "expires_in": 3600, "token_type": "Bearer"})
        )

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", _fake_urlopen)
    diagnostics = _FakeDiagnostics()

    assert load_access_token(token_path, TOKEN_ENV, diagnostics) == "全新的存取權杖"
    assert captured["url"] == TOKEN_URI
    assert "grant_type=refresh_token" in captured["body"]
    assert diagnostics.red_calls == []
    assert diagnostics.amber_calls == []

    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "全新的存取權杖"
    assert saved["refresh_token"] == "長期有效的續期權杖"  # 沒被續期回應覆蓋掉
    assert saved["obtained_at"] > stale_obtained_at  # 下次才判斷得出新的到期時間


# 3. error：token 已過期但續期被 token endpoint 回 HTTP 400 ->
#    必須 raise 帶 symptom/cause/fix 的 OAuthError，絕不可靜默回傳那張已經過期的舊 token。
#    oauth.py 不再自己呼叫 diagnostics.red()（見 MUST_FIX #1），改由呼叫端接手，
#    因此這裡驗證的是「例外物件本身攜帶的診斷欄位」，而不是 diagnostics 有沒有被呼叫。
def test_refresh_http_error_raises_oauth_error_with_diagnostic_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    _write_token(
        token_path, access_token="過期的存取權杖", obtained_at=int(time.time()) - 7200
    )

    def _fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", None, None)

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", _fake_urlopen)
    diagnostics = _FakeDiagnostics()

    with pytest.raises(OAuthError) as exc_info:
        load_access_token(token_path, TOKEN_ENV, diagnostics)

    exc = exc_info.value
    assert exc.symptom == "oauth_error"
    assert "400" in exc.cause
    assert diagnostics.red_calls == []  # oauth.py 本身不再呼叫 diagnostics.red()
    assert diagnostics.amber_calls == []
    # 例外訊息、cause、fix 都不得洩漏任何權杖值（會進 stderr / log / 客戶截圖）
    assert "過期的存取權杖" not in str(exc)
    assert "過期的存取權杖" not in exc.cause
    assert "長期有效的續期權杖" not in exc.cause
    assert "長期有效的續期權杖" not in exc.fix


# 4. MUST_FIX #2 情境 a：續期回應帶「與磁碟不同」的新 refresh_token（輪替）且寫回失敗
#    -> 磁碟上舊的 refresh_token 已被 Google 作廢，寫檔失敗等於永久弄丟唯一還有效的
#    續期憑證，必須 raise OAuthError，不可只發 AMBER 後假裝新 access_token 能救回一切。
def test_rotated_refresh_token_write_failure_raises_oauth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    _write_token(token_path, obtained_at=int(time.time()) - 7200)

    def _fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        return _FakeResponse(
            json.dumps(
                {
                    "access_token": "全新的存取權杖",
                    "refresh_token": "被輪替的新續期權杖",
                    "expires_in": 3600,
                }
            )
        )

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(oauth_module.os, "open", _broken_os_open)
    diagnostics = _FakeDiagnostics()

    with pytest.raises(OAuthError) as exc_info:
        load_access_token(token_path, TOKEN_ENV, diagnostics)

    exc = exc_info.value
    assert exc.symptom == "oauth_error"
    assert "輪替" in exc.cause
    assert diagnostics.amber_calls == []  # 這條路徑不該被降級成 AMBER
    # 寫入從一開始就失敗，磁碟上的舊 token 檔應該完全沒被動過
    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "長期有效的續期權杖"
    assert saved["access_token"] == "現有的存取權杖"


# 5. MUST_FIX #2 情境 b：續期回應「沒有」新 refresh_token（Google 最常見的形狀），
#    磁碟上原本那顆仍然有效 -> 寫回失敗只是單純的品質降級，維持 AMBER，
#    仍要回傳新換到的 access_token，讓今天的流程能繼續跑下去。
def test_non_rotated_refresh_token_write_failure_still_returns_new_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "token.json"
    _write_token(token_path, obtained_at=int(time.time()) - 7200)

    def _fake_urlopen(request: Any, timeout: float | None = None) -> _FakeResponse:
        return _FakeResponse(json.dumps({"access_token": "全新的存取權杖", "expires_in": 3600}))

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(oauth_module.os, "open", _broken_os_open)
    diagnostics = _FakeDiagnostics()

    result = load_access_token(token_path, TOKEN_ENV, diagnostics)

    assert result == "全新的存取權杖"
    assert len(diagnostics.amber_calls) == 1
    assert "寫回檔案失敗" in diagnostics.amber_calls[0]["symptom"]
    assert diagnostics.red_calls == []


# 6. 端對端：真實 Diagnostics 收到 OAuthError 攜帶的 symptom/cause/fix 後，
#    exit_on_red=False 時必須確實冒出 RedAlert（不是被吞掉，也不是繼續往下跑）。
#    這裡模擬的正是 sources/calendar_source.py 的 _fetch_live_events() 呼叫端邏輯，
#    因為那才是本專案唯一真正捕捉 OAuthError 的地方（見 CONTRACT.md §10 呼叫端契約）。
def test_oauth_error_propagates_as_red_alert_via_real_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from _shared.diagnostics import Diagnostics, RedAlert

    token_path = tmp_path / "token.json"
    # 讓 token 過期且缺 refresh_token，直接命中 _require_refresh_fields 的失敗路徑
    _write_token(token_path, obtained_at=int(time.time()) - 7200, refresh_token="")

    def _forbidden_urlopen(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("缺 refresh_token 時不該真的發出續期請求")

    monkeypatch.setattr(oauth_module.urllib.request, "urlopen", _forbidden_urlopen)
    diagnostics = Diagnostics(module_name="test-oauth", exit_on_red=False)

    def _caller_like_calendar_source() -> None:
        """比照 calendar_source.py 的 except OAuthError 區塊。"""
        try:
            load_access_token(token_path, TOKEN_ENV, diagnostics)
        except OAuthError as exc:
            diagnostics.red(symptom=exc.symptom, cause=exc.cause, fix=exc.fix)
            # diagnostics.red() 一定會先 raise RedAlert，這行理論上執行不到；
            # 留著只是避免函式隱含回傳 None（呼應 calendar_source.py 的真實寫法）。
            raise AssertionError("不該執行到這裡：diagnostics.red() 必須先中止")  # pragma: no cover

    with pytest.raises(RedAlert):
        _caller_like_calendar_source()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
