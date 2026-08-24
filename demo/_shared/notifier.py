"""多通道通知發送 — console / telegram / gmail / line / whatsapp。

原書用 WhatsApp（英國市場），台灣落地改以 Telegram 為主力、LINE 次之，
但四個外送通道共用同一個 `send()` 介面，換通道只要改 config 一行。

三個設計重點：
1. **不靜默失敗**：任何通道失敗都印出可讀錯誤並回傳 False，絕不假裝送出去了。
2. **憑證只走環境變數**：config 可帶 `${VAR}`，由 config_loader 展開，程式碼裡沒有金鑰。
3. **分段不切斷 HTML**：Telegram 單則有長度上限，分段時會補齊未關閉的標籤，
   否則 `<b>` 被切在段落中間，Telegram API 會直接回 400 而不是「顯示怪怪的」。
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from email.header import Header
from email.mime.text import MIMEText

try:  # 一般情境：以 `_shared` 套件被 demo 匯入
    from .diagnostics import safe_print
except ImportError:  # 直接以腳本執行本檔時沒有套件脈絡
    from diagnostics import safe_print  # type: ignore[no-redef]

TELEGRAM_API_BASE = "https://api.telegram.org"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

TELEGRAM_LIMIT = 4000            # 官方上限 4096，留 96 字元給重開標籤與安全邊界
LINE_LIMIT = 4900                # 官方上限 5000
LINE_MAX_MESSAGES_PER_PUSH = 5   # LINE push 一次最多 5 則
WHATSAPP_LIMIT = 1500            # Twilio 單則上限 1600
DEFAULT_TIMEOUT_SECONDS = 30
SEGMENT_INTERVAL_SECONDS = 1     # 逐段送出的間隔，避免觸發平台限流

TELEGRAM_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID_ENV = "TELEGRAM_CHAT_ID_CHENGHYANG2001BOT"
GMAIL_TO_ENV = "GMAIL_TO"
LINE_TOKEN_ENV = "LINE_CHANNEL_TOKEN"
LINE_TO_ENV = "LINE_TO"
TWILIO_SID_ENV = "TWILIO_ACCOUNT_SID"
TWILIO_TOKEN_ENV = "TWILIO_AUTH_TOKEN"
TWILIO_FROM_ENV = "TWILIO_FROM"
TWILIO_TO_ENV = "TWILIO_TO"

DEFAULT_SUBJECT = "OpenClaw 自動通知"

# 分段時預留給「關閉 + 重開標籤」的空間；純文字（不含 '<'）不需要預留。
_TAG_HEADROOM = 128
_TAG_PATTERN = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9-]*)[^>]*?(/?)\s*>")
_ENTITY_PATTERN = re.compile(r"&#?[A-Za-z0-9]{1,10};")
# HTML void 元素不成對，切段時不必補關閉標籤。
_VOID_TAGS = frozenset({"br", "hr", "img", "input", "meta", "link"})


class NotifierError(RuntimeError):
    """通知發送失敗（缺憑證、平台回錯、外部指令不存在等）。"""


def _post(url: str, data: bytes, headers: dict[str, str], timeout: int) -> str:
    """共用的 HTTP POST。HTTP 錯誤一律轉成帶回應內容的 NotifierError。"""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300] if exc.fp else ""
        raise NotifierError(f"HTTP {exc.code} {exc.reason}｜{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotifierError(f"連線失敗或逾時：{exc}") from exc


def _post_json(url: str, payload: dict, timeout: int, headers: dict[str, str] | None = None) -> str:
    """送出 JSON body。"""
    merged = {"Content-Type": "application/json; charset=utf-8"}
    merged.update(headers or {})
    return _post(url, json.dumps(payload, ensure_ascii=False).encode("utf-8"), merged, timeout)


class Notifier:
    """通知發送器。channel 在建構時決定，之後每次 `send()` 都走同一個通道。"""

    SUPPORTED = ("console", "telegram", "gmail", "line", "whatsapp")

    def __init__(self, channel: str = "console", config: dict | None = None) -> None:
        """channel 不在 SUPPORTED -> 拋 NotifierError"""
        if not isinstance(channel, str):
            raise NotifierError(f"channel 必須是字串，收到 {type(channel).__name__}")
        normalized = channel.strip().lower()
        if normalized not in self.SUPPORTED:
            raise NotifierError(f"不支援的通道 {channel!r}，可用：{', '.join(self.SUPPORTED)}")
        if config is not None and not isinstance(config, dict):
            raise NotifierError(f"config 必須是 dict，收到 {type(config).__name__}")

        self._channel = normalized
        self._config = dict(config or {})
        self._timeout = self._read_timeout()

    def _read_timeout(self) -> int:
        """逾時秒數可由 config 覆寫，格式錯誤就明確拒絕而不是默默用預設值。"""
        raw = self._config.get("timeout", DEFAULT_TIMEOUT_SECONDS)
        try:
            timeout = int(raw)
        except (TypeError, ValueError) as exc:
            raise NotifierError(f"config['timeout'] 必須是整數秒數，收到 {raw!r}") from exc
        if timeout <= 0:
            raise NotifierError(f"config['timeout'] 必須大於 0，收到 {timeout}")
        return timeout

    @property
    def channel(self) -> str:
        """目前使用的通道名稱。"""
        return self._channel

    def send(self, text: str, subject: str | None = None) -> bool:
        """送出通知，回傳是否成功。失敗時印出明確錯誤，不靜默吞掉。"""
        if not isinstance(text, str):
            raise NotifierError(f"text 必須是字串，收到 {type(text).__name__}")
        handlers = {
            "console": self._send_console,
            "telegram": self._send_telegram,
            "gmail": self._send_gmail,
            "line": self._send_line,
            "whatsapp": self._send_whatsapp,
        }
        try:
            return handlers[self._channel](text, subject)
        except (NotifierError, OSError, ValueError, subprocess.SubprocessError) as exc:
            safe_print(f"[ERROR] [notifier:{self._channel}] 發送失敗：{exc}", sys.stderr)
            return False

    # ------------------------------------------------------------------
    # 憑證
    # ------------------------------------------------------------------

    def _credential(self, config_key: str, env_name: str) -> str:
        """憑證優先讀 config（其值通常是 config_loader 展開後的環境變數），其次直接讀環境變數。"""
        raw = self._config.get(config_key)
        value = str(raw).strip() if raw is not None else ""
        if not value or value.startswith("${"):  # 未展開的 ${VAR} 視同缺失
            value = os.environ.get(env_name, "").strip()
        return value

    def _require(self, pairs: list[tuple[str, str, str]]) -> dict[str, str]:
        """一次取齊多個憑證，缺哪些就一次列完，避免使用者補一個跑一次。"""
        values: dict[str, str] = {}
        missing: list[str] = []
        for config_key, env_name, label in pairs:
            value = self._credential(config_key, env_name)
            if not value:
                missing.append(f"{label}（config['{config_key}'] 或環境變數 {env_name}）")
            values[config_key] = value
        if missing:
            raise NotifierError("缺少憑證：" + "；".join(missing))
        return values

    # ------------------------------------------------------------------
    # 各通道實作
    # ------------------------------------------------------------------

    def _send_console(self, text: str, subject: str | None) -> bool:
        """印到 stdout（mock 模式用），永遠成功。"""
        if subject:
            safe_print(f"===== {subject} =====")
        safe_print(text)
        return True

    def _send_telegram(self, text: str, subject: str | None) -> bool:
        """Telegram Bot API sendMessage，parse_mode=HTML，超過 4000 字元自動分段。"""
        creds = self._require([
            ("bot_token", TELEGRAM_TOKEN_ENV, "Telegram Bot Token"),
            ("chat_id", TELEGRAM_CHAT_ID_ENV, "Telegram Chat ID"),
        ])
        body = f"<b>{html.escape(subject)}</b>\n\n{text}" if subject else text
        url = f"{TELEGRAM_API_BASE}/bot{creds['bot_token']}/sendMessage"
        for index, segment in enumerate(self.split_message(body, TELEGRAM_LIMIT)):
            if index:
                time.sleep(SEGMENT_INTERVAL_SECONDS)
            _post_json(url, {
                "chat_id": creds["chat_id"],
                "text": segment,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, self._timeout)
        return True

    def _send_gmail(self, text: str, subject: str | None) -> bool:
        """透過已登入的 gws CLI 寄信，避免自行處理 Gmail OAuth 的 7 天過期陷阱。"""
        recipient = self._credential("to", GMAIL_TO_ENV)
        if not recipient:
            raise NotifierError(f"gmail 缺少收件人（config['to'] 或環境變數 {GMAIL_TO_ENV}）")
        gws_path = shutil.which("gws")
        if gws_path is None:
            raise NotifierError("找不到 gws CLI；請先安裝 Google Workspace CLI 並完成登入")
        payload = json.dumps({"raw": _build_gmail_raw(recipient, subject or DEFAULT_SUBJECT, text)})
        # Windows 上 shutil.which 解析出的是含副檔名的完整路徑（如 xxx\gws.CMD）；
        # subprocess.run 在 shell=False 下不會自動幫裸指令名補上 PATHEXT，
        # 傳裸字串 "gws" 會直接 FileNotFoundError，必須用解析後的完整路徑呼叫。
        result = subprocess.run(
            [gws_path, "gmail", "users", "messages", "send",
             "--params", '{"userId":"me"}', "--json", payload, "--format", "json"],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=self._timeout, check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            raise NotifierError(f"gws 回傳結束碼 {result.returncode}：{detail}")
        return True

    def _send_line(self, text: str, subject: str | None) -> bool:
        """LINE Messaging API push。純文字通道，不吃 HTML，一次 push 最多 5 則。"""
        creds = self._require([
            ("channel_token", LINE_TOKEN_ENV, "LINE Channel Token"),
            ("to", LINE_TO_ENV, "LINE 收件對象 ID"),
        ])
        body = f"{subject}\n\n{text}" if subject else text
        segments = self.split_message(body, LINE_LIMIT)
        headers = {"Authorization": f"Bearer {creds['channel_token']}"}
        for index in range(0, len(segments), LINE_MAX_MESSAGES_PER_PUSH):
            if index:
                time.sleep(SEGMENT_INTERVAL_SECONDS)
            batch = segments[index:index + LINE_MAX_MESSAGES_PER_PUSH]
            messages = [{"type": "text", "text": segment} for segment in batch]
            _post_json(LINE_PUSH_URL, {"to": creds["to"], "messages": messages}, self._timeout, headers)
        return True

    def _send_whatsapp(self, text: str, subject: str | None) -> bool:
        """Twilio Messages API（form-urlencoded + Basic Auth）。"""
        creds = self._require([
            ("account_sid", TWILIO_SID_ENV, "Twilio Account SID"),
            ("auth_token", TWILIO_TOKEN_ENV, "Twilio Auth Token"),
            ("from", TWILIO_FROM_ENV, "Twilio 發送門號"),
            ("to", TWILIO_TO_ENV, "Twilio 收件門號"),
        ])
        basic = base64.b64encode(
            f"{creds['account_sid']}:{creds['auth_token']}".encode("utf-8")
        ).decode("ascii")
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        url = f"{TWILIO_API_BASE}/Accounts/{creds['account_sid']}/Messages.json"
        body = f"{subject}\n\n{text}" if subject else text
        for index, segment in enumerate(self.split_message(body, WHATSAPP_LIMIT)):
            if index:
                time.sleep(SEGMENT_INTERVAL_SECONDS)
            form = urllib.parse.urlencode(
                {"From": creds["from"], "To": creds["to"], "Body": segment}
            ).encode("utf-8")
            _post(url, form, headers, self._timeout)
        return True

    # ------------------------------------------------------------------
    # 分段
    # ------------------------------------------------------------------

    @staticmethod
    def split_message(text: str, limit: int = 4000) -> list[str]:
        """依換行邊界切段，不切斷 HTML 標籤。

        分三步：
        1. 過長的單行先在「不落在標籤或 HTML 實體內部」的位置硬切；
        2. 逐行貪婪打包，隨時追蹤尚未關閉的標籤所需的收尾長度；
        3. 換段時補上關閉標籤，並在下一段開頭原樣重開（保留 href 等屬性）。
        """
        if not isinstance(text, str):
            raise NotifierError(f"text 必須是字串，收到 {type(text).__name__}")
        if limit <= 0:
            raise NotifierError(f"limit 必須大於 0，收到 {limit}")
        if len(text) <= limit:
            return [text]

        headroom = _TAG_HEADROOM if "<" in text else 0
        wrap_limit = max(1, limit - headroom)
        lines: list[str] = []
        for raw_line in text.split("\n"):
            lines.extend(_wrap_line(raw_line, wrap_limit))
        return _pack_lines(lines, limit)


def _build_gmail_raw(recipient: str, subject: str, text: str) -> str:
    """組 RFC 2822 郵件並做 base64url 編碼（Gmail API 的 `raw` 欄位要整封信，不是只有內文）。"""
    message = MIMEText(text, "plain", "utf-8")
    message["To"] = recipient
    message["Subject"] = Header(subject, "utf-8")  # 中文主旨需 RFC 2047 編碼
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def _safe_cut_positions(line: str) -> list[bool]:
    """標出每個切點是否安全。索引 i 代表切成 line[:i] / line[i:]。

    落在 `<...>` 或 `&nbsp;` 這類實體「內部」的切點不安全；
    正好在標籤前後邊界（i == start 或 i == end）則是安全的。
    """
    safe = [True] * (len(line) + 1)
    for pattern in (_TAG_PATTERN, _ENTITY_PATTERN):
        for match in pattern.finditer(line):
            for index in range(match.start() + 1, match.end()):
                safe[index] = False
    return safe


def _safe_cut_index(line: str, limit: int) -> int:
    """回傳 <= limit 的最佳切點：優先切在空白之後，其次切在最靠後的安全位置。"""
    safe = _safe_cut_positions(line)
    upper = min(limit, len(line))
    fallback = 0
    for index in range(upper, 0, -1):
        if not safe[index]:
            continue
        if not fallback:
            fallback = index
        if line[index - 1].isspace():
            return index
    # 整段都在標籤內部（極端畸形輸入）時只能硬切，至少不會無窮迴圈
    return fallback or upper


def _wrap_line(line: str, limit: int) -> list[str]:
    """把過長的一行切成多行，每行長度 <= limit。"""
    if len(line) <= limit:
        return [line]
    pieces: list[str] = []
    rest = line
    while len(rest) > limit:
        cut = _safe_cut_index(rest, limit)
        pieces.append(rest[:cut])
        rest = rest[cut:]
    pieces.append(rest)
    return pieces


def _update_stack(stack: list[tuple[str, str]], line: str) -> list[tuple[str, str]]:
    """依這一行的標籤更新「尚未關閉的標籤堆疊」。每項為 (標籤名, 原始開頭標籤文字)。"""
    updated = list(stack)
    for match in _TAG_PATTERN.finditer(line):
        is_closing, name, is_self_closing = match.group(1), match.group(2).lower(), match.group(3)
        if is_self_closing or name in _VOID_TAGS:
            continue
        if is_closing:
            for index in range(len(updated) - 1, -1, -1):
                if updated[index][0] == name:
                    del updated[index]
                    break
        else:
            updated.append((name, match.group(0)))
    return updated


def _closing_tags(stack: list[tuple[str, str]]) -> str:
    """由內而外關閉所有未關閉的標籤。"""
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _reopen_tags(stack: list[tuple[str, str]]) -> str:
    """在下一段開頭原樣重開標籤（保留 `<a href=...>` 的屬性）。"""
    return "".join(raw for _, raw in stack)


def _pack_lines(lines: list[str], limit: int) -> list[str]:
    """逐行貪婪打包成段，換段時補關閉標籤、下一段開頭重開。"""
    segments: list[str] = []
    buffer: list[str] = []
    stack: list[tuple[str, str]] = []
    reopen = ""
    length = 0

    for line in lines:
        candidate = _update_stack(stack, line)
        added = len(line) + (1 if buffer else 0)
        if buffer and length + added + len(_closing_tags(candidate)) > limit:
            segments.append(reopen + "\n".join(buffer) + _closing_tags(stack))
            reopen = _reopen_tags(stack)
            buffer = []
            length = len(reopen)
            added = len(line)
            candidate = _update_stack(stack, line)
        buffer.append(line)
        length += added
        stack = candidate

    if buffer or not segments:
        segments.append(reopen + "\n".join(buffer) + _closing_tags(stack))
    return segments
