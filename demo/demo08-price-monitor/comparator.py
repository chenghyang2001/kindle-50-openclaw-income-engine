"""價格比對、閾值判定與基準狀態檔管理。

金額一律走 `decimal.Decimal`：價格差異是要拿去談判與定價的數字，
用 float 會出現 `0.1 + 0.2 != 0.3` 這種在報表上無法辯解的誤差。

基準價的來源優先序：
    狀態檔（上一次實際觀測到的價格） > config.yaml 的 `baseline_price`
狀態檔是每日滾動的事實，config 只是第一次執行時的種子值。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

MONEY_QUANT = Decimal("0.01")
PERCENT_QUANT = Decimal("0.01")
STATE_VERSION = 1

DIRECTION_DROP = "drop"
DIRECTION_RISE = "rise"
DIRECTION_FLAT = "flat"


class ComparatorError(ValueError):
    """價格資料不合法（非數字、負值、狀態檔損毀等）"""


@dataclass(frozen=True)
class PriceChange:
    """單一目標的比對結果"""

    name: str
    url: str
    baseline_price: Decimal
    current_price: Decimal
    delta: Decimal
    delta_percent: Decimal
    direction: str
    is_breach: bool

    def as_dict(self) -> dict[str, str | bool]:
        """轉成 JSON 可序列化的形狀；金額保留字串以免下游又退回 float"""
        return {
            "name": self.name,
            "url": self.url,
            "baseline_price": str(self.baseline_price),
            "current_price": str(self.current_price),
            "delta": str(self.delta),
            "delta_percent": str(self.delta_percent),
            "direction": self.direction,
            "is_breach": self.is_breach,
        }


def to_money(raw: str | int | float | Decimal) -> Decimal:
    """把設定檔／狀態檔中的價格轉成 Decimal。

    float 先經 `str()` 再進 Decimal，避免把二進位誤差帶進來（Decimal(0.1) 會是 0.1000...5551）。
    """
    if isinstance(raw, bool):
        # bool 是 int 的子類，不先擋掉會讓 True 變成 1.00
        raise ComparatorError(f"價格不接受布林值：{raw!r}")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, (int, float)):
        value = Decimal(str(raw))
    elif isinstance(raw, str):
        try:
            value = Decimal(raw.strip())
        except InvalidOperation as exc:
            raise ComparatorError(f"無法解析價格字串：{raw!r}") from exc
    else:
        raise ComparatorError(f"不支援的價格型別：{type(raw).__name__}")

    if not value.is_finite() or value <= 0:
        raise ComparatorError(f"價格必須為正數且有限：{raw!r}")
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def to_percent(raw: str | int | float | Decimal) -> Decimal:
    """把閾值轉成 Decimal 百分比（必須為非負數）"""
    if isinstance(raw, bool):
        raise ComparatorError(f"閾值不接受布林值：{raw!r}")
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ComparatorError(f"無法解析閾值：{raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise ComparatorError(f"閾值必須為非負數：{raw!r}")
    return value


def percent_change(baseline: Decimal, current: Decimal) -> Decimal:
    """回傳 (現價 - 基準價) / 基準價 * 100，四捨五入到小數兩位"""
    if baseline <= 0:
        raise ComparatorError(f"基準價必須為正數，收到 {baseline}")
    ratio = (current - baseline) / baseline * Decimal(100)
    return ratio.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


def _direction_of(delta: Decimal) -> str:
    """由差額判定方向"""
    if delta < 0:
        return DIRECTION_DROP
    if delta > 0:
        return DIRECTION_RISE
    return DIRECTION_FLAT


def compare(
    name: str,
    url: str,
    baseline: Decimal,
    current: Decimal,
    threshold_percent: Decimal,
) -> PriceChange:
    """比對單一目標。

    判定用 `>=`：閾值設 5% 時，剛好 5.00% 也要警報。
    監控系統寧可多叫一次，也不要在邊界上讓真實變動溜過去。
    """
    delta = (current - baseline).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    delta_percent = percent_change(baseline, current)
    return PriceChange(
        name=name,
        url=url,
        baseline_price=baseline,
        current_price=current,
        delta=delta,
        delta_percent=delta_percent,
        direction=_direction_of(delta),
        is_breach=abs(delta_percent) >= threshold_percent,
    )


def resolve_baseline(
    name: str, configured: str | int | float | Decimal | None, stored: dict
) -> Decimal:
    """決定要拿來比對的基準價：狀態檔優先，其次 config，兩者皆無則拋錯。"""
    entry = stored.get(name)
    if isinstance(entry, dict) and entry.get("price") is not None:
        return to_money(entry["price"])
    if configured is None:
        raise ComparatorError(f"目標 {name!r} 既無狀態檔紀錄也無 baseline_price，無法比對")
    return to_money(configured)


def load_baselines(path: Path) -> dict:
    """讀取狀態檔的 targets 區塊；檔案不存在視為首次執行，回傳空 dict。

    檔案存在但內容損毀時**拋 ComparatorError**（不靜默改用 config 種子值），
    因為那代表整段價格歷史已經失真，必須由人工確認後再繼續。
    """
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ComparatorError(f"狀態檔損毀無法解析：{path}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise ComparatorError(f"狀態檔格式錯誤，最外層必須是物件：{path}")
    targets = payload.get("targets", {})
    if not isinstance(targets, dict):
        raise ComparatorError(f"狀態檔 targets 欄位必須是物件：{path}")
    return targets


def save_baselines(path: Path, prices: dict[str, Decimal]) -> None:
    """把本次觀測到的價格寫回狀態檔，作為下一次執行的基準。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = load_baselines(path) if path.is_file() else {}
    merged = dict(existing)
    for name, price in prices.items():
        merged[name] = {"price": str(price), "updated_at": now}

    payload = {"version": STATE_VERSION, "updated_at": now, "targets": merged}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def summarise(changes: Iterable[PriceChange]) -> dict[str, int]:
    """統計比對結果，供報告開頭的一行摘要使用"""
    items = list(changes)
    return {
        "compared": len(items),
        "breaches": sum(1 for item in items if item.is_breach),
        "drops": sum(1 for item in items if item.direction == DIRECTION_DROP),
        "rises": sum(1 for item in items if item.direction == DIRECTION_RISE),
        "flat": sum(1 for item in items if item.direction == DIRECTION_FLAT),
    }
