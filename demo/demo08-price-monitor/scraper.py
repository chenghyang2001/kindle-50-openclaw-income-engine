"""競品頁面抓取與價格解析。

刻意只用標準庫 `html.parser`，不引入 BeautifulSoup / lxml：
交付給客戶的單檔部署包不該為了抓一個價格而背上外部解析器依賴。

本模組的核心紀律是「**解析失敗必須被看見**」：
任何抓不到 / 解不出價格的目標都回傳帶 `failure_reason` 的 FetchResult，
由呼叫端轉成 AMBER 警示，絕不靜默跳過讓上層誤以為「沒事」。
"""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable

# 抓取間隔下限：書中規格為 >=1 req/s，低於此值容易被競品站台判定為爬蟲並封 IP
MIN_REQUEST_INTERVAL_SECONDS = 1.0
MONEY_QUANT = Decimal("0.01")

# HTML 空元素沒有結束標籤，計算巢狀深度時必須排除，否則深度永遠歸不了零
VOID_ELEMENTS = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }
)

_SELECTOR_RE = re.compile(
    r"^(?P<tag>[A-Za-z][A-Za-z0-9]*)?"
    r"(?P<rest>(?:[#.][A-Za-z0-9_\-]+|\[[^\]]+\])*)$"
)
_TOKEN_RE = re.compile(r"[#.][A-Za-z0-9_\-]+|\[[^\]]+\]")
_ATTR_RE = re.compile(
    r"^\[\s*(?P<name>[A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"
    r"(?P<value>\"[^\"]*\"|'[^']*'|[^\]]*?)\s*\]$"
)
# 先移除所有空白（含不斷行空格），再抓數字；小數點固定為 "."
# （歐陸「1.299,00」逗號小數格式不在支援範圍，見 README 已知限制）
_WHITESPACE_RE = re.compile(r"\s+")
_PRICE_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)")


class ScraperError(RuntimeError):
    """抓取或選擇器語法層級的錯誤（不含「解析不到價格」這種預期內的失敗）"""


@dataclass(frozen=True)
class Selector:
    """支援的 CSS 選擇器子集：tag / .class / #id / [attr=value] 及其組合"""

    tag: str | None
    element_id: str | None
    classes: tuple[str, ...]
    attributes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class FetchResult:
    """單一監控目標的抓取結果。price 為 None 時 failure_reason 必定有值。"""

    name: str
    url: str
    raw_text: str | None
    price: Decimal | None
    failure_reason: str | None

    @property
    def is_parsed(self) -> bool:
        """是否成功解析出價格"""
        return self.price is not None


def parse_selector(raw: str) -> Selector:
    """把選擇器字串解析成 Selector；語法不支援時拋 ScraperError（不猜測使用者意圖）。"""
    text = (raw or "").strip()
    matched = _SELECTOR_RE.match(text)
    if not matched:
        raise ScraperError(
            f"不支援的選擇器語法：{raw!r}（僅支援 tag / .class / #id / [attr=value]）"
        )

    element_id: str | None = None
    classes: list[str] = []
    attributes: list[tuple[str, str]] = []
    for token in _TOKEN_RE.findall(matched.group("rest") or ""):
        if token.startswith("."):
            classes.append(token[1:])
        elif token.startswith("#"):
            element_id = token[1:]
        else:
            attributes.append(_parse_attribute_token(token, raw))

    return Selector(
        tag=matched.group("tag"),
        element_id=element_id,
        classes=tuple(classes),
        attributes=tuple(attributes),
    )


def _parse_attribute_token(token: str, raw: str) -> tuple[str, str]:
    """解析 [attr=value] token，值可加單引號或雙引號"""
    attr = _ATTR_RE.match(token)
    if not attr:
        raise ScraperError(f"不支援的屬性選擇器：{token!r}（出現在 {raw!r}）")
    value = attr.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return attr.group("name"), value


def _element_matches(
    selector: Selector, tag: str, attrs: list[tuple[str, str | None]]
) -> bool:
    """判斷單一元素是否命中選擇器（class 為精確比對，price 不會誤命中 price--was）"""
    if selector.tag is not None and selector.tag.lower() != tag.lower():
        return False
    attr_map = {name.lower(): (value or "") for name, value in attrs}
    if selector.element_id is not None and attr_map.get("id") != selector.element_id:
        return False
    if selector.classes:
        present = set(attr_map.get("class", "").split())
        if not set(selector.classes).issubset(present):
            return False
    return all(attr_map.get(name.lower()) == value for name, value in selector.attributes)


class _FirstMatchTextParser(HTMLParser):
    """擷取第一個命中選擇器的元素的內層文字"""

    def __init__(self, selector: Selector) -> None:
        super().__init__(convert_charrefs=True)
        self._selector = selector
        self._depth = 0
        self._is_capturing = False
        self._chunks: list[str] = []
        self.text: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.text is not None or tag.lower() in VOID_ELEMENTS:
            return
        if self._is_capturing:
            self._depth += 1
        elif _element_matches(self._selector, tag, attrs):
            self._is_capturing = True
            self._depth = 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """自閉合標籤包不住文字，覆寫成 no-op 以免預設實作擾動巢狀深度"""
        return

    def handle_endtag(self, tag: str) -> None:
        if not self._is_capturing or self.text is not None or tag.lower() in VOID_ELEMENTS:
            return
        self._depth -= 1
        if self._depth <= 0:
            self.text = "".join(self._chunks).strip()
            self._is_capturing = False

    def handle_data(self, data: str) -> None:
        if self._is_capturing and self.text is None:
            self._chunks.append(data)


def extract_text(html: str, selector: str) -> str | None:
    """回傳第一個命中元素的文字；找不到回傳 None（由呼叫端負責警報）"""
    parser = _FirstMatchTextParser(parse_selector(selector))
    parser.feed(html)
    parser.close()
    return parser.text


def parse_price(text: str | None) -> Decimal | None:
    """從 '$105.78'、'USD 1,299.00' 之類的字串抽出金額；抽不出來回傳 None。"""
    if text is None:
        return None
    normalized = _WHITESPACE_RE.sub("", text)
    matched = _PRICE_RE.search(normalized)
    if not matched:
        return None
    try:
        value = Decimal(matched.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def mock_filename_for(url: str) -> str:
    """由網址主機名推導快照檔名：competitor-a.example.com -> competitor_a.html"""
    host = urllib.parse.urlsplit(url).hostname or ""
    label = host[4:] if host.startswith("www.") else host
    first = label.split(".")[0] if label else "unknown"
    return f"{first.replace('-', '_')}.html"


def read_mock_page(mock_dir: Path, target: dict) -> str:
    """讀取本地 HTML 快照。檔案缺失屬設定錯誤，拋 FileNotFoundError（訊息含絕對路徑）。"""
    filename = target.get("mock_file") or mock_filename_for(str(target.get("url", "")))
    path = (mock_dir / str(filename)).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"找不到 mock 快照：{path}")
    return path.read_text(encoding="utf-8")


def fetch_page(url: str, timeout: int, user_agent: str) -> str:
    """真實抓取單一頁面；網路層錯誤一律轉成 ScraperError 並附上網址。"""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise ScraperError(f"HTTP {exc.code} 於 {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ScraperError(f"連線失敗於 {url}：{exc}") from exc


def _load_html(
    target: dict, *, is_mock: bool, mock_dir: Path, timeout: int, user_agent: str
) -> str:
    """依模式取得 HTML 內容：mock 讀本地快照，live 走真實網路"""
    if is_mock:
        return read_mock_page(mock_dir, target)
    return fetch_page(str(target["url"]), timeout, user_agent)


def scrape_target(
    target: dict,
    *,
    is_mock: bool,
    mock_dir: Path,
    timeout: int = 15,
    user_agent: str = "OpenClawPriceMonitor/1.0",
) -> FetchResult:
    """抓取並解析單一目標，任何失敗都保留原因，不靜默跳過。"""
    name = str(target.get("name", "(未命名目標)"))
    url = str(target.get("url", ""))
    selector = str(target.get("selector", ""))
    try:
        html = _load_html(
            target, is_mock=is_mock, mock_dir=mock_dir, timeout=timeout, user_agent=user_agent
        )
        raw_text = extract_text(html, selector)
    except (ScraperError, FileNotFoundError, OSError) as exc:
        return FetchResult(name, url, None, None, f"頁面取得失敗：{exc}")

    if raw_text is None:
        return FetchResult(
            name, url, None, None,
            f"選擇器 {selector!r} 在頁面中找不到對應元素（可能是網站改版）",
        )
    if not raw_text.strip():
        return FetchResult(name, url, raw_text, None, f"選擇器 {selector!r} 命中元素但內容為空")

    price = parse_price(raw_text)
    if price is None:
        return FetchResult(name, url, raw_text, None, f"價格欄位內容非數字：{raw_text.strip()!r}")
    return FetchResult(name, url, raw_text.strip(), price, None)


def scrape_targets(
    targets: Iterable[dict],
    *,
    is_mock: bool,
    mock_dir: Path,
    interval_seconds: float = MIN_REQUEST_INTERVAL_SECONDS,
    timeout: int = 15,
    user_agent: str = "OpenClawPriceMonitor/1.0",
    sleeper: Callable[[float], None] = time.sleep,
) -> list[FetchResult]:
    """依序抓取所有目標。

    真實模式下每兩次請求之間至少間隔 MIN_REQUEST_INTERVAL_SECONDS 秒（rate limit 防護）；
    mock 模式讀本地檔案、完全不觸網，故不套用節流，否則每跑一次測試要多等 6 秒。
    """
    interval = 0.0 if is_mock else max(float(interval_seconds), MIN_REQUEST_INTERVAL_SECONDS)
    results: list[FetchResult] = []
    for index, target in enumerate(targets):
        if index > 0 and interval > 0:
            sleeper(interval)
        results.append(
            scrape_target(
                target,
                is_mock=is_mock,
                mock_dir=mock_dir,
                timeout=timeout,
                user_agent=user_agent,
            )
        )
    return results
