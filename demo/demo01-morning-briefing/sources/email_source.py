"""信件來源。

- ``is_mock=True``：讀 ``mock/emails.json``，零憑證。
- ``is_mock=False``：走**已登入的 `gws` CLI**，不自己做 OAuth。

為什麼不自己做 OAuth：書中第 04 章把「Gmail token 7 天過期」列為紅色警報，
自建 OAuth 會把這個維運負擔攬在自己身上；改用系統已登入的 gws CLI 可以整包避開。
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import SourceError, read_mock_payload

CLI_TIMEOUT_SECONDS = 60
# 系統寄件者不可能需要回覆，直接排除以免污染 TOP_3_PRIORITIES。
NO_REPLY_PREFIXES = ("no-reply@", "noreply@", "do-not-reply@", "bounce@")


def fetch_messages(
    source_config: dict,
    base_dir: Path,
    is_mock: bool,
    diagnostics: Any,
) -> list[dict]:
    """取得未讀信件，回傳正規化後的清單。"""
    if not source_config.get("enabled", True):
        return []

    if is_mock:
        raw_messages = read_mock_payload(
            base_dir, source_config.get("mock_file", "mock/emails.json"), "messages"
        )
        return [normalize_message(item) for item in raw_messages]

    return _fetch_live_messages(source_config, diagnostics)


def normalize_message(raw: dict) -> dict:
    """把離線 JSON 的信件轉成主流程使用的統一結構。"""
    sender = str(raw.get("from", ""))
    return {
        "id": str(raw.get("id", "")),
        "from": sender,
        "domain": sender.split("@")[-1].lower() if "@" in sender else "",
        "received": str(raw.get("received", "")),
        "subject": str(raw.get("subject", "（無主旨）")),
        "is_vip": bool(raw.get("is_vip", False)),
        "needs_reply": bool(raw.get("needs_reply", False)),
        "summary": str(raw.get("summary", "")),
    }


def _fetch_live_messages(source_config: dict, diagnostics: Any) -> list[dict]:
    """live 模式：用 gws CLI 先列出訊息 id，再逐封取 metadata。"""
    cli_command = str(source_config.get("cli_command", "gws"))
    max_results = int(source_config.get("max_results", 20) or 20)
    list_params = json.dumps(
        {
            "userId": "me",
            "q": str(source_config.get("query", "is:unread newer_than:1d")),
            "maxResults": max_results,
        },
        ensure_ascii=False,
    )
    listing = _run_gws(
        cli_command,
        ["gmail", "users", "messages", "list", "--params", list_params, "--format", "json"],
        diagnostics,
    )
    message_ids = [
        str(item.get("id", "")) for item in listing.get("messages", []) or [] if item.get("id")
    ]
    return [_fetch_one_message(cli_command, mid, diagnostics) for mid in message_ids[:max_results]]


def _fetch_one_message(cli_command: str, message_id: str, diagnostics: Any) -> dict:
    """取單封信的標頭並轉成統一結構。"""
    params = json.dumps({"userId": "me", "id": message_id, "format": "metadata"})
    payload = _run_gws(
        cli_command,
        ["gmail", "users", "messages", "get", "--params", params, "--format", "json"],
        diagnostics,
    )
    headers = {
        str(header.get("name", "")).lower(): str(header.get("value", ""))
        for header in payload.get("payload", {}).get("headers", []) or []
    }
    sender = headers.get("from", "")
    return {
        "id": message_id,
        "from": sender,
        "domain": sender.split("@")[-1].rstrip(">").lower() if "@" in sender else "",
        "received": headers.get("date", ""),
        "subject": headers.get("subject", "（無主旨）"),
        # live 模式沒有預先標好的 VIP 旗標，改由 main.py 用「今日會議與會者」推導。
        "is_vip": False,
        "needs_reply": not sender.lower().startswith(NO_REPLY_PREFIXES),
        "summary": str(payload.get("snippet", ""))[:80],
    }


def _run_gws(cli_command: str, argv: list[str], diagnostics: Any) -> dict:
    """執行 gws 子指令並回傳解析後的 JSON；任何失敗都要看得見，不可靜默吞掉。

    Windows 上 shutil.which 解析出的是含副檔名的完整路徑（如 xxx\\gws.CMD）；
    subprocess.run 在 shell=False 下不會自動幫裸指令名補上 PATHEXT，傳裸字串
    "gws" 會直接 FileNotFoundError。解析不到時原樣保留 cli_command，讓下面既有的
    FileNotFoundError 分支自然觸發（行為不變，只是換一個真正會踩到的路徑）。
    """
    resolved_command = shutil.which(cli_command) or cli_command
    try:
        completed = subprocess.run(
            [resolved_command, *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=CLI_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        diagnostics.red(
            symptom="gws_cli_missing",
            cause=f"找不到 {cli_command} 指令，無法讀取 Gmail",
            fix="安裝 Google Workspace CLI 並執行一次登入，或改用 --mock 離線模式",
        )
        raise SourceError(f"找不到 {cli_command} 指令") from exc
    except subprocess.TimeoutExpired as exc:
        diagnostics.red(
            symptom="gws_cli_timeout",
            cause=f"{cli_command} 超過 {CLI_TIMEOUT_SECONDS} 秒未回應",
            fix="檢查網路狀態；晨間排程請開啟 retry_on_timeout 並提早 20 分鐘執行",
        )
        raise SourceError(f"{cli_command} 逾時") from exc

    if completed.returncode != 0:
        diagnostics.red(
            symptom="oauth_error",
            cause=f"{cli_command} 回傳非零退出碼 {completed.returncode}：{completed.stderr.strip()[:200]}",
            fix="重新執行 gws 登入流程（Gmail token 有 7 天限制），再重跑一次",
        )
        raise SourceError(f"{cli_command} 執行失敗（exit {completed.returncode}）")

    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SourceError(f"{cli_command} 輸出不是合法 JSON：{completed.stdout[:200]}") from exc
