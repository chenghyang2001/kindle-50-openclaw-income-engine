"""極簡逐字稿 Webhook 接收器（僅 --live --serve 使用）。

為什麼用標準庫 http.server 而不是 Flask / FastAPI：
客戶端只需要一個 POST 端點，多裝一個 web framework 會讓「<60 分鐘完成部署」
的承諾破功，也多綁一條要長期維護的相依供應鏈。這裡的量級（一場會議一次回呼）
標準庫綽綽有餘。

正式對外時請放在 nginx / Cloudflare Tunnel 之後並加上共享密鑰驗證，
http.server 本身沒有 TLS 也沒有速率限制。
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_MAX_BODY_BYTES = 1_048_576


class TranscriptCollector:
    """收集回呼進來的逐字稿，收滿 expected 份就結束等待。"""

    def __init__(self, expected: int = 1) -> None:
        if expected < 1:
            raise ValueError(f"expected 必須 >= 1，收到 {expected}")
        self._expected = expected
        self._items: list[dict[str, Any]] = []

    def add(self, transcript: dict[str, Any]) -> None:
        self._items.append(transcript)

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self._items)

    @property
    def is_complete(self) -> bool:
        return len(self._items) >= self._expected


def validate_payload(payload: Any) -> str | None:
    """檢查回呼內容是否為可用的逐字稿；回傳錯誤訊息，通過則回 None。"""
    if not isinstance(payload, dict):
        return "payload 必須是 JSON 物件"
    if not payload.get("transcript_id"):
        return "缺少 transcript_id"
    utterances = payload.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        return "缺少非空的 utterances 陣列"
    return None


def make_handler(
    collector: TranscriptCollector,
    path: str,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> type[BaseHTTPRequestHandler]:
    """產生綁定特定 collector 的 handler 類別（避免用全域變數傳狀態）。"""

    class TranscriptHandler(BaseHTTPRequestHandler):
        server_version = "OpenClawMeetingHook/1.0"

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 規定的名稱
            """健康檢查端點，讓客戶自己驗證 webhook 對外可達。"""
            if self.path.rstrip("/") == "/healthz":
                self._respond(200, {"status": "ok"})
                return
            self._respond(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 規定的名稱
            if self.path.rstrip("/") != path.rstrip("/"):
                self._respond(404, {"error": f"未知路徑：{self.path}"})
                return
            payload = self._read_json_body()
            if payload is None:
                return
            problem = validate_payload(payload)
            if problem is not None:
                self._respond(400, {"error": problem})
                return
            collector.add(payload)
            self._respond(202, {"status": "accepted", "transcript_id": payload["transcript_id"]})

        def _read_json_body(self) -> Any:
            """讀取並解析 body；任何問題都直接回應錯誤碼並回傳 None。"""
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                self._respond(411, {"error": "缺少 Content-Length"})
                return None
            try:
                length = int(raw_length)
            except ValueError:
                self._respond(400, {"error": f"Content-Length 非數字：{raw_length}"})
                return None
            if length > max_body_bytes:
                self._respond(413, {"error": f"body 超過上限 {max_body_bytes} bytes"})
                return None
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._respond(400, {"error": f"JSON 解析失敗：{exc}"})
                return None

        def _respond(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """預設會印到 stderr 且格式冗長，這裡收斂成單行。"""
            print(f"[webhook] {self.address_string()} {format % args}", file=sys.stderr)

    return TranscriptHandler


def collect_transcripts(
    host: str,
    port: int,
    path: str,
    expected: int = 1,
    wait_seconds: int = 300,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
) -> list[dict[str, Any]]:
    """啟動伺服器等待逐字稿回呼，收滿或逾時就關閉並回傳已收到的內容。"""
    collector = TranscriptCollector(expected=expected)
    server = ThreadingHTTPServer((host, port), make_handler(collector, path, max_body_bytes))
    server.timeout = 5  # 每次 handle_request 最多阻塞 5 秒，才能定期檢查逾時
    deadline = time.monotonic() + wait_seconds
    print(f"[webhook] 等待逐字稿：http://{host}:{port}{path}（{wait_seconds} 秒）", file=sys.stderr)
    try:
        while not collector.is_complete and time.monotonic() < deadline:
            server.handle_request()
    except KeyboardInterrupt:
        print("[webhook] 使用者中斷等待", file=sys.stderr)
    finally:
        server.server_close()
    return collector.items
