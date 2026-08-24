"""RAG 診斷矩陣（紅 / 琥珀 / 綠）— 第 04 章維運設計。

書中把所有故障分成兩類：**紅色**代表系統停擺（沒修就沒有產出，必須立刻停下來），
**琥珀色**代表輸出品質下降（流程照跑，但要記錄下來改提示詞或設定）。
本模組把那張表格固化成 `KNOWN_SYMPTOMS`，讓 10 個 demo 對同一症狀給出同一句對策。
"""

from __future__ import annotations

import functools
import sys
from enum import Enum
from typing import Any, Callable, NoReturn, TextIO, TypeVar

_T = TypeVar("_T")


class Severity(Enum):
    """RAG（Red / Amber / Green）三級嚴重度。"""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"


class RedAlert(RuntimeError):
    """紅色警報：系統停擺，需立即介入"""


# 書中已知症狀對照表。severity 存字串（Severity 的 value），
# 讓整張表維持純字串結構，方便 demo 直接序列化進報表或 JSON。
KNOWN_SYMPTOMS: dict[str, dict[str, str]] = {
    "api_key_invalid": {
        "severity": Severity.RED.value,
        "symptom": "API Key Invalid",
        "cause": "Key 過期或未存入設定檔",
        "fix": "重新輸入並驗證",
    },
    "no_whatsapp_msg": {
        "severity": Severity.RED.value,
        "symptom": "收不到 WhatsApp 訊息",
        "cause": "Twilio 憑證錯誤",
        "fix": "檢查沙盒驗證清單",
    },
    "oauth_error": {
        "severity": Severity.RED.value,
        "symptom": "OAuth Error",
        "cause": "Gmail token 7 天限制過期",
        "fix": "設定永久 Refresh token",
    },
    "no_transcripts": {
        "severity": Severity.RED.value,
        "symptom": "收不到逐字稿",
        "cause": "Webhook URL 無法公開存取",
        "fix": "確認網路狀態與權限",
    },
    "briefing_too_long": {
        "severity": Severity.AMBER.value,
        "symptom": "簡報過長",
        "cause": "輸出超過 400 字",
        "fix": "提示詞強制「最高 320 字，無情刪減」",
    },
    "spam_misclassification": {
        "severity": Severity.AMBER.value,
        "symptom": "重要信件被誤判為垃圾信",
        "cause": "VIP 網域未設定",
        "fix": "更新 VIP_SENDERS.domains 並重掃過去 7 天",
    },
    "tone_mismatch": {
        "severity": Severity.AMBER.value,
        "symptom": "語氣不像本人",
        "cause": "缺語氣樣本",
        "fix": "TONE_EXAMPLES 加入 3-5 封真實信件",
    },
    "delayed_briefing": {
        "severity": Severity.AMBER.value,
        "symptom": "簡報延遲送達",
        "cause": "API 限流",
        "fix": "Cron 提早 20 分 + retry_on_timeout: true",
    },
}

_SEPARATOR = "=" * 68


def safe_print(text: str, stream: TextIO | None = None) -> None:
    """安全輸出：Windows 主控台常是 cp950，直接印非 cp950 字元會炸 UnicodeEncodeError。

    這裡不去改動全域 stdout/stderr（那是應用程式的職責，不是函式庫的），
    改成印失敗時降級為可編碼字元，確保「診斷訊息本身」永遠不會變成新的故障。

    `stream` 刻意不用 `sys.stdout` 當預設值：預設值在 import 當下就綁定，
    會讓 pytest 的 capsys 之類的替換失效，必須在呼叫時才取用。
    """
    target = stream if stream is not None else sys.stdout
    try:
        print(text, file=target, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(target, "encoding", None) or "utf-8"
        fallback = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(fallback, file=target, flush=True)


class Diagnostics:
    """單一模組的診斷輸出口。每個 demo 建立一個實例，module_name 用於前綴。"""

    def __init__(self, module_name: str, exit_on_red: bool = True) -> None:
        """
        module_name: 出現在每則訊息前綴的模組名稱（如 "demo01-morning-briefing"）
        exit_on_red: True -> red() 直接 sys.exit(1)；False -> 改拋 RedAlert（供測試使用）
        """
        if not isinstance(module_name, str) or not module_name.strip():
            raise ValueError("module_name 必須是非空字串")
        self._module_name = module_name.strip()
        self._exit_on_red = bool(exit_on_red)
        self._amber_count = 0

    @property
    def module_name(self) -> str:
        """診斷訊息的模組前綴。"""
        return self._module_name

    @property
    def exit_on_red(self) -> bool:
        """紅色警報時是否直接結束行程。"""
        return self._exit_on_red

    @property
    def amber_count(self) -> int:
        """本次執行累積的琥珀色警示數量。"""
        return self._amber_count

    def red(self, symptom: str, cause: str, fix: str) -> NoReturn:
        """印出紅色警報（symptom / cause / fix 三欄），然後退出或拋 RedAlert。"""
        message = f"[RED] [{self._module_name}] 症狀：{symptom}"
        safe_print(_SEPARATOR, sys.stderr)
        safe_print(message, sys.stderr)
        safe_print(f"       原因：{cause}", sys.stderr)
        safe_print(f"       對策：{fix}", sys.stderr)
        safe_print(_SEPARATOR, sys.stderr)
        if self._exit_on_red:
            sys.exit(1)
        raise RedAlert(f"{symptom}｜原因：{cause}｜對策：{fix}")

    def amber(self, symptom: str, fix: str) -> None:
        """印出琥珀色警示到 stderr，流程繼續。"""
        self._amber_count += 1
        safe_print(f"[AMBER] [{self._module_name}] 症狀：{symptom}｜對策：{fix}", sys.stderr)

    def green(self, message: str) -> None:
        """印出正常訊息。"""
        safe_print(f"[GREEN] [{self._module_name}] {message}")

    def report(self, symptom_key: str, detail: str | None = None) -> None:
        """依 `KNOWN_SYMPTOMS` 的既定分級處理某個已知症狀。

        detail 會附加在症狀描述之後，用來補充當下的實際數值
        （例：`report("briefing_too_long", "實際 428 字")`）。
        """
        entry = self._lookup(symptom_key)
        symptom = entry["symptom"] if not detail else f"{entry['symptom']}（{detail}）"
        if Severity(entry["severity"]) is Severity.RED:
            self.red(symptom, entry["cause"], entry["fix"])
        else:
            self.amber(f"{symptom}｜原因：{entry['cause']}", entry["fix"])

    @staticmethod
    def _lookup(symptom_key: str) -> dict[str, str]:
        """查表，找不到就明確報錯（避免打錯 key 卻靜默略過警報）。"""
        try:
            return KNOWN_SYMPTOMS[symptom_key]
        except KeyError as exc:
            known = ", ".join(sorted(KNOWN_SYMPTOMS))
            raise KeyError(f"未知的症狀代碼 {symptom_key!r}，可用代碼：{known}") from exc


def diagnose(
    diagnostics: Diagnostics,
    symptom_key: str,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., _T]], Callable[..., _T | None]]:
    """裝飾器：把已知例外自動歸類到紅 / 琥珀（第 04 章 RAG 矩陣）。

    紅色症狀會沿用 `Diagnostics.red` 的行為（退出或拋 RedAlert）；
    琥珀色症狀則印出警示後回傳 None，讓呼叫端自行決定要不要走降級路徑。
    """
    entry = Diagnostics._lookup(symptom_key)  # 裝飾階段就驗證 key，錯字不會拖到執行期才爆
    severity = Severity(entry["severity"])

    def decorator(func: Callable[..., _T]) -> Callable[..., _T | None]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> _T | None:
            try:
                return func(*args, **kwargs)
            except exceptions as exc:
                detail = f"{type(exc).__name__}: {exc}"
                if severity is Severity.RED:
                    diagnostics.red(f"{entry['symptom']}（{detail}）", entry["cause"], entry["fix"])
                else:
                    diagnostics.amber(f"{entry['symptom']}（{detail}）", entry["fix"])
                return None

        return wrapper

    return decorator
