"""多源聚合：把 CRM / Shopify / Stripe 的快照合成一份可發送的每日銷售報表。

負責四件事：

1. **聚合**：三個互不重疊的營收管道相加，全程 `Decimal`。
2. **目標差距**：當日達成率、與每日目標的差額、月累計進度。
3. **異常標記**：達成率 < 80% 或 > 150%；當日營收較 7 日均值偏離 > 30%。
4. **部分失敗**：任一資料源拋 `SourceError` 時記錄失敗、寫入 `Diagnostics.amber`，
   報表加上「⚠️ 部分資料：Stripe 無回應」橫幅後**照常產出**。

為什麼異常規則只套用在「當日」達成率而不套月進度：月中任何一天的月累計
達成率天然就低於 80%（例如 24 號才跑到 66%），套下去每天都會噴假警報，
一週內團隊就會把警示當背景雜訊。月進度只做資訊呈現，不觸發異常。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from sources import (
    SourceError,
    SourceSnapshot,
    quantize_money,
    to_decimal,
)

ONE_PLACE = Decimal("0.1")
HUNDRED = Decimal("100")


class DiagnosticsLike(Protocol):
    """只用到 `Diagnostics` 的 amber()，用 Protocol 讓測試能塞假物件。"""

    def amber(self, symptom: str, fix: str) -> None: ...


@dataclass(frozen=True)
class SourceFailure:
    """單一資料源失敗的紀錄，會出現在報表橫幅與回傳 dict 中。"""

    source_id: str
    display_name: str
    reason: str


@dataclass(frozen=True)
class Targets:
    """業績目標與比較基準。"""

    daily_revenue: Decimal
    monthly_revenue: Decimal
    month_to_date_revenue: Decimal
    trailing_7_day_revenue: tuple[Decimal, ...]


@dataclass(frozen=True)
class Thresholds:
    """異常判定閾值，全部以百分比表示。"""

    attainment_low_pct: Decimal
    attainment_high_pct: Decimal
    daily_deviation_pct: Decimal


@dataclass(frozen=True)
class SalesReport:
    """一份完整的每日銷售報表（純資料，不含排版）。"""

    currency: str
    snapshots: tuple[SourceSnapshot, ...]
    failures: tuple[SourceFailure, ...]
    total_revenue: Decimal
    order_count: int
    targets: Targets
    daily_attainment_pct: Decimal | None
    gap_to_daily_target: Decimal
    month_to_date_total: Decimal
    monthly_attainment_pct: Decimal | None
    trailing_avg: Decimal | None
    deviation_pct: Decimal | None
    anomalies: tuple[str, ...]

    @property
    def is_partial(self) -> bool:
        """只要有任一資料源失敗就是部分資料。"""
        return bool(self.failures)

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {
            "currency": self.currency,
            "is_partial": self.is_partial,
            "totals": {
                "revenue": str(self.total_revenue),
                "orders": self.order_count,
            },
            "targets": {
                "daily": str(self.targets.daily_revenue),
                "monthly": str(self.targets.monthly_revenue),
                "month_to_date_before_today": str(self.targets.month_to_date_revenue),
            },
            "attainment": {
                "daily_pct": _opt_str(self.daily_attainment_pct),
                "monthly_pct": _opt_str(self.monthly_attainment_pct),
                "gap_to_daily_target": str(self.gap_to_daily_target),
                "month_to_date_total": str(self.month_to_date_total),
            },
            "trailing_7_day": {
                "average": _opt_str(self.trailing_avg),
                "deviation_pct": _opt_str(self.deviation_pct),
            },
            "sources": [
                {
                    "source_id": snap.source_id,
                    "display_name": snap.display_name,
                    "revenue": str(snap.revenue),
                    "orders": snap.order_count,
                    "highlights": dict(snap.highlights),
                }
                for snap in self.snapshots
            ],
            "failed_sources": [
                {
                    "source_id": fail.source_id,
                    "display_name": fail.display_name,
                    "reason": fail.reason,
                }
                for fail in self.failures
            ],
            "anomalies": list(self.anomalies),
        }

    def to_json(self) -> str:
        """給 LLM 當 user message 用的緊湊 JSON。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _percent(part: Decimal, whole: Decimal) -> Decimal | None:
    """算百分比。分母為 0 時回 None 而不是 0——「目標 0 元」和「達成率 0%」
    是完全不同的兩件事，混為一談會讓報表說謊。"""
    if whole == 0:
        return None
    return (part / whole * HUNDRED).quantize(ONE_PLACE, rounding=ROUND_HALF_UP)


def load_targets(mock_path: Path, overrides: dict[str, Any] | None = None) -> Targets:
    """讀 targets.json，再套用 config.yaml 的覆寫值。

    為什麼是兩層：客戶調整業績目標時只該動 config.yaml（受版控、看得到誰改的），
    targets.json 代表商業系統回傳的當下快照（含月累計與 7 日歷史），不該手改。
    """
    try:
        payload = json.loads(mock_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"找不到目標檔：{mock_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"目標檔 JSON 解析失敗 {mock_path}：{exc}") from exc

    merged = dict(payload)
    for key in ("daily_revenue", "monthly_revenue"):
        value = (overrides or {}).get(key)
        if value is not None:
            merged[key] = value

    history = merged.get("trailing_7_day_revenue") or []
    if not isinstance(history, list):
        raise ValueError(f"trailing_7_day_revenue 必須是陣列：{mock_path}")

    return Targets(
        daily_revenue=to_decimal(merged.get("daily_revenue", "0"), "targets", "daily_revenue"),
        monthly_revenue=to_decimal(merged.get("monthly_revenue", "0"), "targets", "monthly_revenue"),
        month_to_date_revenue=to_decimal(
            merged.get("month_to_date_revenue", "0"), "targets", "month_to_date_revenue"
        ),
        trailing_7_day_revenue=tuple(
            to_decimal(item, "targets", "trailing_7_day_revenue[]") for item in history
        ),
    )


def load_thresholds(raw: dict[str, Any] | None) -> Thresholds:
    """從 config.yaml 的 thresholds 區塊建立閾值，缺項用書中預設值補。"""
    raw = raw or {}
    return Thresholds(
        attainment_low_pct=to_decimal(
            raw.get("attainment_low_pct", 80), "thresholds", "attainment_low_pct"
        ),
        attainment_high_pct=to_decimal(
            raw.get("attainment_high_pct", 150), "thresholds", "attainment_high_pct"
        ),
        daily_deviation_pct=to_decimal(
            raw.get("daily_deviation_pct", 30), "thresholds", "daily_deviation_pct"
        ),
    )


def collect(
    source_configs: Iterable[dict[str, Any]],
    base_dir: Path,
    fetchers: dict[str, Callable[[Path], SourceSnapshot]],
    diagnostics: DiagnosticsLike | None = None,
) -> tuple[list[SourceSnapshot], list[SourceFailure]]:
    """逐一取數。**任何單源失敗都不中斷迴圈**——這是整個模組的核心設計。

    失敗會同時做兩件事：記進 failures（進報表橫幅）、寫 Diagnostics.amber
    （進 RAG 診斷矩陣的琥珀燈），讓維運端知道要修，但團隊照樣收得到報表。
    """
    snapshots: list[SourceSnapshot] = []
    failures: list[SourceFailure] = []

    for entry in source_configs:
        source_id = str(entry.get("id", "")).strip()
        display_name = str(entry.get("display_name") or source_id or "未命名資料源")
        fetcher = fetchers.get(source_id)
        if fetcher is None:
            failures.append(SourceFailure(source_id, display_name, "設定中的資料源未註冊"))
            _amber(diagnostics, display_name, "設定中的資料源未註冊")
            continue

        mock_path = base_dir / str(entry.get("mock_file", ""))
        try:
            snapshots.append(fetcher(mock_path))
        except SourceError as exc:
            failures.append(SourceFailure(source_id, display_name, str(exc)))
            _amber(diagnostics, display_name, str(exc))

    return snapshots, failures


def _amber(diagnostics: DiagnosticsLike | None, display_name: str, reason: str) -> None:
    """把單源故障送進診斷矩陣的琥珀燈。"""
    if diagnostics is None:
        return
    diagnostics.amber(
        f"{display_name} 無回應，本日報表以部分資料產出",
        f"檢查 {display_name} 憑證與 API 狀態後重跑；原因：{reason}",
    )


def detect_anomalies(
    attainment_pct: Decimal | None,
    deviation_pct: Decimal | None,
    thresholds: Thresholds,
) -> list[str]:
    """套用書中的兩條異常規則，回傳給人看的中文標記。"""
    anomalies: list[str] = []

    if attainment_pct is None:
        anomalies.append("⚠️ 每日目標為 0，無法計算達成率——請確認目標設定")
    elif attainment_pct < thresholds.attainment_low_pct:
        anomalies.append(
            f"⚠️ 今日達成率 {attainment_pct}%，低於下限 {thresholds.attainment_low_pct}%"
        )
    elif attainment_pct > thresholds.attainment_high_pct:
        # 超標同樣是異常：多半代表重複計算、測試訂單或一筆特大單，需要人確認。
        anomalies.append(
            f"⚠️ 今日達成率 {attainment_pct}%，高於上限 {thresholds.attainment_high_pct}%，請確認是否有重複計算"
        )

    if deviation_pct is not None and abs(deviation_pct) > thresholds.daily_deviation_pct:
        direction = "高於" if deviation_pct > 0 else "低於"
        anomalies.append(
            f"⚠️ 今日營收{direction} 7 日均值 {abs(deviation_pct)}%，"
            f"超過 {thresholds.daily_deviation_pct}% 容忍區間"
        )

    return anomalies


def _average(values: tuple[Decimal, ...]) -> Decimal | None:
    """7 日均值。沒有歷史資料時回 None，不用 0 假裝有基準。"""
    if not values:
        return None
    return quantize_money(sum(values, Decimal("0")) / Decimal(len(values)))


def build_report(
    snapshots: list[SourceSnapshot],
    failures: list[SourceFailure],
    targets: Targets,
    thresholds: Thresholds,
    currency: str = "USD",
) -> SalesReport:
    """把快照與目標合成 SalesReport；部分資料時照樣算出可用的數字。"""
    total_revenue = quantize_money(sum((s.revenue for s in snapshots), Decimal("0")))
    order_count = sum(s.order_count for s in snapshots)

    attainment_pct = _percent(total_revenue, targets.daily_revenue)
    trailing_avg = _average(targets.trailing_7_day_revenue)
    deviation_pct = (
        _percent(total_revenue - trailing_avg, trailing_avg) if trailing_avg else None
    )

    month_to_date_total = quantize_money(targets.month_to_date_revenue + total_revenue)

    return SalesReport(
        currency=currency,
        snapshots=tuple(snapshots),
        failures=tuple(failures),
        total_revenue=total_revenue,
        order_count=order_count,
        targets=targets,
        daily_attainment_pct=attainment_pct,
        gap_to_daily_target=quantize_money(total_revenue - targets.daily_revenue),
        month_to_date_total=month_to_date_total,
        monthly_attainment_pct=_percent(month_to_date_total, targets.monthly_revenue),
        trailing_avg=trailing_avg,
        deviation_pct=deviation_pct,
        anomalies=tuple(detect_anomalies(attainment_pct, deviation_pct, thresholds)),
    )


def partial_banner(failures: Iterable[SourceFailure]) -> str:
    """產出「⚠️ 部分資料：Stripe 無回應」橫幅；全部正常時回空字串。

    橫幅必須寫在報表**最上方**：讀者在看到任何數字之前，就要先知道
    這份數字是不完整的，否則會拿殘缺數據去開會做決策。
    """
    names = [fail.display_name for fail in failures]
    if not names:
        return ""
    return f"⚠️ 部分資料：{'、'.join(names)} 無回應"
