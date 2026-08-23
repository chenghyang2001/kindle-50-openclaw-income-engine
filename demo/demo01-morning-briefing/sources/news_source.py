"""新聞來源。

- ``is_mock=True``：讀 ``mock/news.json``，零憑證。
- ``is_mock=False``：用 ``urllib.request`` 抓 RSS，再用標準庫 ElementTree 解析。

與行事曆不同，新聞是**權重最低**的輸入：單一 feed 掛掉走 AMBER 讓流程繼續，
只有全部 feed 都失敗才把整個新聞區塊標成「無」。書中原則是「部分資料」優於「整份失敗」。
"""

import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from . import SourceError, read_mock_payload

# 部分新聞站會擋掉沒有 User-Agent 的請求，補一個可辨識的字串。
REQUEST_HEADERS = {"User-Agent": "openclaw-morning-briefing/1.0"}
MAX_TITLE_LENGTH = 60


def fetch_headlines(
    source_config: dict,
    base_dir: Path,
    is_mock: bool,
    diagnostics: Any,
) -> list[dict]:
    """取得新聞標題清單，數量上限由 ``max_items`` 決定。"""
    if not source_config.get("enabled", True):
        return []

    max_items = int(source_config.get("max_items", 8) or 8)
    if is_mock:
        raw_items = read_mock_payload(
            base_dir, source_config.get("mock_file", "mock/news.json"), "items"
        )
        return [normalize_item(item) for item in raw_items][:max_items]

    return _fetch_live_headlines(source_config, diagnostics)[:max_items]


def normalize_item(raw: dict) -> dict:
    """把離線 JSON 的新聞轉成主流程使用的統一結構。"""
    return {
        "id": str(raw.get("id", "")),
        "source": str(raw.get("source", "")),
        "title": str(raw.get("title", ""))[:MAX_TITLE_LENGTH],
        "url": str(raw.get("url", "")),
        "published": str(raw.get("published", "")),
        "topic": str(raw.get("topic", "一般")),
        "impact": str(raw.get("impact", "low")),
    }


def _fetch_live_headlines(source_config: dict, diagnostics: Any) -> list[dict]:
    """逐一抓取設定的 RSS feed；單一 feed 失敗走 AMBER，不中斷整份簡報。"""
    feeds = [str(url) for url in source_config.get("feeds", []) or []]
    timeout = int(source_config.get("request_timeout", 10) or 10)
    rate_limit_seconds = float(source_config.get("rate_limit_seconds", 1) or 0)

    collected: list[dict] = []
    for index, feed_url in enumerate(feeds):
        if index > 0 and rate_limit_seconds > 0:
            # 對來源端有禮貌：連續抓取之間至少間隔 1 秒，避免被封鎖。
            time.sleep(rate_limit_seconds)
        try:
            collected.extend(_parse_feed(feed_url, timeout))
        except SourceError as exc:
            diagnostics.amber(
                symptom="news_feed_unreachable",
                fix=f"{exc}；確認 feed 網址仍有效，必要時在 config.yaml 換掉該來源",
            )

    if feeds and not collected:
        diagnostics.amber(
            symptom="news_feed_unreachable",
            fix="所有新聞來源都失敗，本次簡報的 NEWS_ITEMS 會標示為「無」",
        )
    return collected


def _parse_feed(feed_url: str, timeout: int) -> list[dict]:
    """抓取並解析單一 RSS feed。"""
    request = urllib.request.Request(feed_url, headers=REQUEST_HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_xml = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise SourceError(f"新聞來源無法連線：{feed_url}（{exc}）") from exc

    try:
        root = ElementTree.fromstring(raw_xml)
    except ElementTree.ParseError as exc:
        raise SourceError(f"新聞來源不是合法 RSS：{feed_url}（{exc}）") from exc

    channel_title = root.findtext("./channel/title", default=feed_url)
    return [_element_to_item(node, channel_title) for node in root.findall(".//item")]


def _element_to_item(node: ElementTree.Element, channel_title: str) -> dict:
    """把單一 RSS <item> 轉成統一結構。"""
    return {
        "id": node.findtext("guid", default=""),
        "source": channel_title,
        "title": (node.findtext("title", default="") or "")[:MAX_TITLE_LENGTH],
        "url": node.findtext("link", default=""),
        "published": node.findtext("pubDate", default=""),
        "topic": node.findtext("category", default="一般"),
        # RSS 沒有影響力欄位，一律先給 medium，交由提示詞判斷是否值得寫進簡報。
        "impact": "medium",
    }
