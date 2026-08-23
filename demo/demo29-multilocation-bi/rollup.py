"""多據點匯總引擎：異質 API 標準化 -> 4 週滾動基準線 -> Composite Score -> 分級與異常。

對應 apxG_p17 的三段規格：

* **Input**：`LOCATION_REGISTRY` 匯入異質 API（Shopify / Square / POS），
  三種 schema 各有一個標準化器，全部收斂成同一組 `MetricSet`。
* **Processing**：Composite Score 分級為 Top Tier / Core / Focus Required；
  4 週滾動基準線用於異常偵測（偏離 15% 觸發）。
* **Output**：跨據點排名與同儕比較**只在總部視野**產出——本檔不自行決定，
  由呼叫端傳入 `include_cross_site`，權限判定的唯一來源是 `access_control`。

兩個刻意的設計選擇：

1. **指標一律「對自己的基準線」正規化，不對同儕正規化。**
   若用同儕 min-max 正規化，店長要算出自己的分數就必須先拿到全網數字——
   權限隔離會在演算法層被架空。改成 index = 本期 / 自身 4 週基準線 x 100 後，
   店長視野與總部視野算出的同一家分數**完全一致**，且零跨店資料流動。

2. **Composite 以「已四捨五入到 1 位小數的 index」加權平均。**
   這樣報表上列出的每個 index 和最後的總分對得起來；用未捨入值會出現
   「照著表格算卻得不到那個總分」的客訴，對一份要拿去開高階會議的報表是致命的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

TWO_PLACES = Decimal("0.01")
ONE_PLACE = Decimal("0.1")
FOUR_PLACES = Decimal("0.0001")
HUNDRED = Decimal("100")

#: 直接由來源系統取得的原始指標（同時也是基準線視窗的鍵）。
RAW_METRICS = ("revenue", "transactions", "footfall", "labour_hours")

#: 指標中文標籤，給報表與異常訊息用。
METRIC_LABELS: dict[str, str] = {
    "revenue": "營收",
    "transactions": "交易筆數",
    "footfall": "來客數",
    "labour_hours": "工時",
    "average_ticket": "客單價",
    "conversion_rate": "來客轉換率",
    "revenue_per_labour_hour": "人時營收",
}

#: 分級名稱（apxG_p17 原文用語，保留英文當機器可讀鍵）。
TIER_TOP = "Top Tier"
TIER_CORE = "Core"
TIER_FOCUS = "Focus Required"
TIER_UNRATED = "Unrated"

TIER_LABELS: dict[str, str] = {
    TIER_TOP: "Top Tier（頂尖）",
    TIER_CORE: "Core（穩健）",
    TIER_FOCUS: "Focus Required（需重點關注）",
    TIER_UNRATED: "Unrated（基準線不足，無法評分）",
}


class RollupError(RuntimeError):
    """單一據點取數或標準化失敗。訊息必須指出是哪個據點、哪個欄位、哪個檔案。"""


class DiagnosticsLike(Protocol):
    """只用到 `Diagnostics.amber()`，用 Protocol 讓測試能塞假物件。"""

    def amber(self, symptom: str, fix: str) -> None: ...


# --------------------------------------------------------------------------
# 數值工具（金額一律 Decimal，百分比一律防零除）
# --------------------------------------------------------------------------


def quantize_money(value: Decimal) -> Decimal:
    """金額收斂到小數 2 位，用 ROUND_HALF_UP（財務慣例，非銀行家捨入）。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def quantize_index(value: Decimal) -> Decimal:
    """指數／百分比收斂到小數 1 位。"""
    return value.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)


def to_decimal(value: Any, site_id: str, field_name: str) -> Decimal:
    """JSON 值轉 Decimal。先轉 str 再進 Decimal，避免 float 二進位誤差。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollupError(f"{site_id} 的 {field_name} 不是合法數值：{value!r}") from exc


def safe_ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    """比值計算。分母為 0 或缺值時回 None 而不是 0——
    「沒有來客」和「轉換率 0%」是兩件不同的事，混為一談會讓報表說謊。"""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _index(current: Decimal | None, baseline: Decimal | None) -> Decimal | None:
    """本期相對自身基準線的指數（基準線 = 100）。無基準線時回 None。"""
    ratio = safe_ratio(current, baseline)
    return None if ratio is None else quantize_index(ratio * HUNDRED)


def _opt_money(value: Decimal | None) -> str | None:
    """金額欄位的可選序列化。"""
    return None if value is None else str(quantize_money(value))


def _opt_rate(value: Decimal | None) -> str | None:
    """比率欄位的可選序列化（保留 4 位小數，轉換率小數位有意義）。"""
    return None if value is None else str(value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP))


def _opt_str(value: Decimal | None) -> str | None:
    """通用的可選序列化。"""
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# 標準化後的指標容器
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MetricSet:
    """標準化後的一組指標。三種異質 API 全部收斂成這個形狀。"""

    revenue: Decimal
    transactions: Decimal
    footfall: Decimal
    labour_hours: Decimal

    @property
    def average_ticket(self) -> Decimal | None:
        """客單價 = 營收 / 交易筆數。"""
        return safe_ratio(self.revenue, self.transactions)

    @property
    def conversion_rate(self) -> Decimal | None:
        """來客轉換率 = 交易筆數 / 來客數。"""
        return safe_ratio(self.transactions, self.footfall)

    @property
    def revenue_per_labour_hour(self) -> Decimal | None:
        """人時營收 = 營收 / 工時。"""
        return safe_ratio(self.revenue, self.labour_hours)

    def get(self, name: str) -> Decimal | None:
        """依名稱取指標（原始或衍生皆可）。名稱不存在直接拋錯，不回 None 掩蓋打錯字。"""
        if name not in METRIC_LABELS:
            raise RollupError(f"未知的指標名稱：{name!r}")
        return getattr(self, name)

    def to_dict(self) -> dict[str, str | None]:
        """轉可序列化結構；數值全部走字串，保住 Decimal 精度。"""
        return {
            "revenue": str(quantize_money(self.revenue)),
            "transactions": str(self.transactions),
            "footfall": str(self.footfall),
            "labour_hours": str(self.labour_hours),
            "average_ticket": _opt_money(self.average_ticket),
            "conversion_rate": _opt_rate(self.conversion_rate),
            "revenue_per_labour_hour": _opt_money(self.revenue_per_labour_hour),
        }


@dataclass(frozen=True)
class SiteSnapshot:
    """單一據點本期的標準化快照。"""

    site_id: str
    display_name: str
    region: str
    adapter: str
    metrics: MetricSet


@dataclass(frozen=True)
class SiteFailure:
    """單一據點取數失敗；會出現在報表橫幅與回傳結果中，不會從報表上消失。"""

    site_id: str
    display_name: str
    reason: str


@dataclass(frozen=True)
class SiteScore:
    """單一據點的評分結果（純資料，不含排版）。"""

    site_id: str
    display_name: str
    region: str
    metrics: MetricSet
    baseline: MetricSet | None
    indices: dict[str, Decimal | None]
    composite: Decimal | None
    tier: str
    anomalies: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """轉 JSON-safe 結構。"""
        return {
            "site_id": self.site_id,
            "display_name": self.display_name,
            "region": self.region,
            "metrics": self.metrics.to_dict(),
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "indices": {key: _opt_str(value) for key, value in self.indices.items()},
            "composite_score": _opt_str(self.composite),
            "tier": self.tier,
            "anomalies": list(self.anomalies),
        }


# --------------------------------------------------------------------------
# 異質 API 標準化器（Input：LOCATION_REGISTRY）
# --------------------------------------------------------------------------


def _load_payload(mock_path: Path, site_id: str) -> dict[str, Any]:
    """讀取並解析單一據點的資料檔；四種失敗一律收斂成 RollupError。

    第四種是「來源系統回了 error 物件」——這正是 `--live` 時 API 回 5xx／逾時
    要對應的行為，因此 mock 也保留一份，讓部分失敗路徑真的被走過。
    """
    try:
        raw = mock_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RollupError(f"{site_id} 無法讀取資料檔 {mock_path}：{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RollupError(f"{site_id} 的資料檔 JSON 解析失敗 {mock_path}：{exc}") from exc

    if not isinstance(payload, dict):
        raise RollupError(f"{site_id} 的資料檔頂層必須是物件：{mock_path}")

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code", "unknown")
        raise RollupError(f"{site_id} 來源系統回報錯誤 [{code}]：{error.get('message', '(無訊息)')}")
    return payload


def _rows(payload: Mapping[str, Any], key: str, site_id: str) -> list[dict[str, Any]]:
    """取出必要的資料列陣列；缺漏、型別不符或元素非物件一律 RollupError。"""
    value = payload.get(key)
    if not isinstance(value, list) or not value:
        raise RollupError(f"{site_id} 缺少非空陣列欄位 {key}")
    for item in value:
        if not isinstance(item, dict):
            raise RollupError(f"{site_id} 的 {key} 元素必須是物件：{item!r}")
    return value


def _sum_field(rows: Iterable[Mapping[str, Any]], key: str, site_id: str) -> Decimal:
    """加總資料列中的某個數值欄位。"""
    return sum((to_decimal(row.get(key), site_id, key) for row in rows), Decimal("0"))


def _scale(value: Decimal, minor_unit_scale: Any, site_id: str) -> Decimal:
    """把最小貨幣單位整數換算回主單位。比例由 registry 指定，不寫死在程式碼裡。"""
    scale = int(to_decimal(minor_unit_scale or 0, site_id, "minor_unit_scale"))
    if scale < 0:
        raise RollupError(f"{site_id} 的 minor_unit_scale 不可為負數：{scale}")
    return value / (Decimal(10) ** scale)


def adapt_shopify(payload: Mapping[str, Any], entry: Mapping[str, Any], site_id: str) -> MetricSet:
    """Shopify Analytics 每日彙總 -> MetricSet（營收採淨額：gross - refunds）。"""
    rows = _rows(payload, "daily_orders", site_id)
    net = _sum_field(rows, "gross_sales", site_id) - _sum_field(rows, "refunds", site_id)
    return MetricSet(
        revenue=_scale(net, entry.get("minor_unit_scale"), site_id),
        transactions=_sum_field(rows, "order_count", site_id),
        footfall=_sum_field(rows, "sessions", site_id),
        labour_hours=to_decimal(payload.get("staff_hours"), site_id, "staff_hours"),
    )


def _money_field(row: Mapping[str, Any], key: str, site_id: str) -> Decimal:
    """Square 的 `*_money` 物件取值；欄位不存在視為 0（退款欄可以合法缺席）。"""
    money = row.get(key)
    if money is None:
        return Decimal("0")
    if not isinstance(money, dict):
        raise RollupError(f"{site_id} 的 {key} 必須是物件：{money!r}")
    return to_decimal(money.get("amount", 0), site_id, f"{key}.amount")


def adapt_square(payload: Mapping[str, Any], entry: Mapping[str, Any], site_id: str) -> MetricSet:
    """Square Payments 彙總 -> MetricSet（金額為最小貨幣單位整數，依 scale 換算）。"""
    rows = _rows(payload, "payments_summary", site_id)
    gross = sum((_money_field(row, "amount_money", site_id) for row in rows), Decimal("0"))
    refunds = sum((_money_field(row, "refunded_money", site_id) for row in rows), Decimal("0"))
    labor = payload.get("labor")
    if not isinstance(labor, dict):
        raise RollupError(f"{site_id} 缺少 labor 物件")
    return MetricSet(
        revenue=_scale(gross - refunds, entry.get("minor_unit_scale"), site_id),
        transactions=_sum_field(rows, "payment_count", site_id),
        footfall=_sum_field(rows, "customer_count", site_id),
        labour_hours=to_decimal(labor.get("hours"), site_id, "labor.hours"),
    )


def adapt_pos(payload: Mapping[str, Any], entry: Mapping[str, Any], site_id: str) -> MetricSet:
    """傳統 POS 日結表 -> MetricSet（net_sales 已是淨額）。"""
    rows = _rows(payload, "business_days", site_id)
    return MetricSet(
        revenue=_scale(_sum_field(rows, "net_sales", site_id), entry.get("minor_unit_scale"), site_id),
        transactions=_sum_field(rows, "tickets", site_id),
        footfall=_sum_field(rows, "door_count", site_id),
        labour_hours=_sum_field(rows, "hours_worked", site_id),
    )


#: 標準化器註冊表。測試可用 monkeypatch 換掉其中一個模擬單店故障。
ADAPTERS: dict[str, Callable[[Mapping[str, Any], Mapping[str, Any], str], MetricSet]] = {
    "shopify": adapt_shopify,
    "square": adapt_square,
    "pos": adapt_pos,
}


def collect_sites(
    entries: Iterable[Mapping[str, Any]],
    base_dir: Path,
    diagnostics: DiagnosticsLike | None = None,
) -> tuple[list[SiteSnapshot], list[SiteFailure]]:
    """逐一取數並標準化。**任何單店失敗都不中斷迴圈**（比照 demo09 的部分失敗設計）。

    呼叫端必須傳入**已經過 `AccessScope.filter_registry()` 裁切**的名冊；
    本函式不做權限判斷，它只負責忠實地把拿到的名冊跑完。
    """
    snapshots: list[SiteSnapshot] = []
    failures: list[SiteFailure] = []

    for entry in entries:
        site_id = str(entry.get("site_id", "")).strip()
        display_name = str(entry.get("display_name") or site_id or "未命名據點")
        try:
            snapshots.append(_collect_one(entry, site_id, display_name, base_dir))
        except RollupError as exc:
            failures.append(SiteFailure(site_id, display_name, str(exc)))
            _amber(diagnostics, display_name, str(exc))

    return snapshots, failures


def _collect_one(
    entry: Mapping[str, Any], site_id: str, display_name: str, base_dir: Path
) -> SiteSnapshot:
    """取單一據點。抽出來讓 `collect_sites` 維持在 30 行以內。"""
    if not site_id:
        raise RollupError("名冊項目缺少 site_id")
    adapter_name = str(entry.get("adapter", "")).strip().lower()
    adapter = ADAPTERS.get(adapter_name)
    if adapter is None:
        known = ", ".join(sorted(ADAPTERS))
        raise RollupError(f"{site_id} 的 adapter {adapter_name!r} 未註冊（可用：{known}）")

    payload = _load_payload(base_dir / str(entry.get("mock_file", "")), site_id)
    return SiteSnapshot(
        site_id=site_id,
        display_name=display_name,
        region=str(entry.get("region", "")),
        adapter=adapter_name,
        metrics=adapter(payload, entry, site_id),
    )


def _amber(diagnostics: DiagnosticsLike | None, display_name: str, reason: str) -> None:
    """把單店故障送進診斷矩陣的琥珀燈：報表照發，但維運端必須知道要修。"""
    if diagnostics is None:
        return
    diagnostics.amber(
        f"{display_name} 無回應，本期匯總以部分資料產出",
        f"檢查該據點的 API 憑證與連線後重跑；原因：{reason}",
    )


# --------------------------------------------------------------------------
# 4 週滾動基準線（狀態檔）
# --------------------------------------------------------------------------


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    """讀取一份頂層為物件的 JSON 設定檔（名冊、基準線）。

    三種失敗分別對應三種不同的修法，因此訊息要能區分：檔案不在（路徑錯／未部署）、
    JSON 壞掉（手改壞了）、頂層不是物件（格式版本不對）。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"找不到{label}檔：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}檔 JSON 解析失敗 {path}：{exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"{label}檔頂層必須是物件：{path}")
    return payload


def load_baseline_windows(path: Path) -> dict[str, dict[str, list[str]]]:
    """讀基準線狀態檔／種子檔，回傳 {site_id: {metric: [週值, ...]}}。"""
    payload = load_json_object(path, "基準線")
    sites = payload.get("sites")
    if not isinstance(sites, dict):
        raise ValueError(f"基準線檔缺少 sites 物件：{path}")
    return {str(key): dict(value) for key, value in sites.items() if isinstance(value, dict)}


def baseline_from_window(
    window: Mapping[str, Any],
    site_id: str,
    min_weeks: int,
    diagnostics: DiagnosticsLike | None = None,
) -> MetricSet | None:
    """把滾動視窗折成基準線 `MetricSet`。樣本不足時仍計算，但記琥珀燈。

    任一指標完全沒有樣本就回 None——那時所有 index 都會是 None，據點被標成
    Unrated，而不是被硬套一個從 0 除出來的假分數。
    """
    values: dict[str, Decimal] = {}
    for metric in RAW_METRICS:
        series = window.get(metric)
        if not isinstance(series, list) or not series:
            return None
        if len(series) < min_weeks and diagnostics is not None:
            diagnostics.amber(
                f"{site_id} 的{METRIC_LABELS[metric]}基準線只有 {len(series)} 週樣本（需 {min_weeks} 週）",
                "累積滿 4 週再解讀異常標記；樣本不足時的偏離值容易是雜訊",
            )
        total = sum((to_decimal(item, site_id, f"baseline.{metric}") for item in series), Decimal("0"))
        values[metric] = quantize_money(total / Decimal(len(series)))
    return MetricSet(**values)


def resolve_baselines(
    windows: Mapping[str, Mapping[str, Any]],
    site_ids: Iterable[str],
    min_weeks: int,
    diagnostics: DiagnosticsLike | None = None,
) -> dict[str, MetricSet | None]:
    """把視窗字典折成 {site_id: MetricSet | None}，只處理傳入的據點。"""
    resolved: dict[str, MetricSet | None] = {}
    for site_id in site_ids:
        window = windows.get(site_id)
        resolved[site_id] = (
            None if window is None
            else baseline_from_window(window, site_id, min_weeks, diagnostics)
        )
    return resolved


def update_windows(
    windows: Mapping[str, Mapping[str, Any]],
    snapshots: Iterable[SiteSnapshot],
    window_weeks: int,
) -> dict[str, dict[str, list[str]]]:
    """把本期實績推進滾動視窗，只保留最近 `window_weeks` 週。

    未出現在 snapshots 中的據點（無回應或不可見）其視窗**原樣保留**：
    以缺值推進視窗，會讓基準線被一次故障永久污染。
    """
    updated: dict[str, dict[str, list[str]]] = {
        site: {metric: [str(item) for item in list(series)] for metric, series in window.items()}
        for site, window in windows.items()
    }
    for snapshot in snapshots:
        target = updated.setdefault(snapshot.site_id, {metric: [] for metric in RAW_METRICS})
        for metric in RAW_METRICS:
            series = list(target.get(metric, []))
            series.append(str(quantize_money(snapshot.metrics.get(metric) or Decimal("0"))))
            target[metric] = series[-window_weeks:]
    return updated


def save_baseline_windows(path: Path, windows: Mapping[str, Any], window_weeks: int) -> None:
    """寫回狀態檔。先寫暫存檔再 replace，避免寫到一半被中斷留下半截 JSON。"""
    payload = {
        "_note": "由 demo29 匯總引擎自動更新的 4 週滾動基準線視窗，請勿手改。",
        "window_weeks": window_weeks,
        "sites": dict(windows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


# --------------------------------------------------------------------------
# Composite Score / 分級 / 異常
# --------------------------------------------------------------------------


def load_weights(raw: Mapping[str, Any] | None) -> dict[str, Decimal]:
    """讀取 composite 權重。權重全為 0 或未設定會讓分數失去意義，直接拋錯。"""
    weights: dict[str, Decimal] = {}
    for name, value in (raw or {}).items():
        if name not in METRIC_LABELS:
            raise ValueError(f"composite.weights 出現未知指標 {name!r}")
        try:
            weight = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"composite.weights.{name} 不是合法數值：{value!r}") from exc
        if weight < 0:
            raise ValueError(f"composite.weights.{name} 不可為負數：{value!r}")
        weights[name] = weight
    if not weights or sum(weights.values()) == 0:
        raise ValueError("composite.weights 必須至少有一個正權重")
    return weights


def compute_indices(
    metrics: MetricSet, baseline: MetricSet | None, weights: Mapping[str, Decimal]
) -> dict[str, Decimal | None]:
    """算出每個計分指標相對自身基準線的指數（基準線 = 100）。"""
    if baseline is None:
        return {name: None for name in weights}
    return {name: _index(metrics.get(name), baseline.get(name)) for name in weights}


def compute_composite(
    indices: Mapping[str, Decimal | None], weights: Mapping[str, Decimal]
) -> Decimal | None:
    """加權平均出 Composite Score。只納入有指數的指標，全缺時回 None（防零除）。"""
    total_weight = Decimal("0")
    weighted_sum = Decimal("0")
    for name, index in indices.items():
        if index is None:
            continue
        weight = weights.get(name, Decimal("0"))
        weighted_sum += weight * index
        total_weight += weight
    if total_weight == 0:
        return None
    return (weighted_sum / total_weight).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def classify_tier(composite: Decimal | None, top_tier_min: Decimal, core_min: Decimal) -> str:
    """Composite Score -> Top Tier / Core / Focus Required（無分數則 Unrated）。"""
    if composite is None:
        return TIER_UNRATED
    if composite >= top_tier_min:
        return TIER_TOP
    if composite >= core_min:
        return TIER_CORE
    return TIER_FOCUS


def detect_anomalies(indices: Mapping[str, Decimal | None], deviation_pct: Decimal) -> list[str]:
    """偏離 4 週滾動基準線超過門檻（規格：15%）就標記，正負皆標。

    正向偏離同樣要標：突然暴衝多半是重複計算、活動檔期或單筆特大交易，
    只抓下跌會讓「看起來很好」的資料錯誤永遠沒人查。
    """
    anomalies: list[str] = []
    for name, index in indices.items():
        if index is None:
            continue
        deviation = index - HUNDRED
        if abs(deviation) > deviation_pct:
            anomalies.append(
                f"⚠️ {METRIC_LABELS[name]}偏離 4 週滾動基準線 {deviation:+.1f}%"
                f"（門檻 ±{deviation_pct}%）"
            )
    return anomalies


def score_site(
    snapshot: SiteSnapshot,
    baseline: MetricSet | None,
    weights: Mapping[str, Decimal],
    top_tier_min: Decimal,
    core_min: Decimal,
    deviation_pct: Decimal,
) -> SiteScore:
    """單一據點評分。此函式完全不知道有其他據點存在——這正是隔離得以成立的原因。"""
    indices = compute_indices(snapshot.metrics, baseline, weights)
    composite = compute_composite(indices, weights)
    return SiteScore(
        site_id=snapshot.site_id,
        display_name=snapshot.display_name,
        region=snapshot.region,
        metrics=snapshot.metrics,
        baseline=baseline,
        indices=indices,
        composite=composite,
        tier=classify_tier(composite, top_tier_min, core_min),
        anomalies=tuple(detect_anomalies(indices, deviation_pct)),
    )


# --------------------------------------------------------------------------
# 匯總結果
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RollupResult:
    """一份完整的匯總結果（純資料，不含排版）。

    `ranking` / `peer_median_composite` / `network_totals` 在店長視野一律是 None：
    它們是由他店數字算出來的，**存在本物件裡就已經是外洩**。
    """

    pack: str
    currency: str
    period_label: str
    scores: tuple[SiteScore, ...]
    failures: tuple[SiteFailure, ...]
    network_totals: dict[str, Decimal] | None
    ranking: tuple[dict[str, Any], ...] | None
    peer_median_composite: Decimal | None

    @property
    def is_partial(self) -> bool:
        """可見範圍內只要有任一據點無回應就是部分資料。"""
        return bool(self.failures)

    @property
    def anomaly_count(self) -> int:
        """可見範圍內的異常標記總數。"""
        return sum(len(score.anomalies) for score in self.scores)

    def to_dict(self) -> dict[str, Any]:
        """轉 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {
            "pack": self.pack,
            "currency": self.currency,
            "period_label": self.period_label,
            "is_partial": self.is_partial,
            "sites_reporting": len(self.scores),
            "sites_unavailable": len(self.failures),
            "sites": [score.to_dict() for score in self.scores],
            "failed_sites": [
                {"site_id": item.site_id, "display_name": item.display_name, "reason": item.reason}
                for item in self.failures
            ],
            "network_totals": (
                None if self.network_totals is None
                else {key: str(value) for key, value in self.network_totals.items()}
            ),
            "ranking": None if self.ranking is None else [dict(item) for item in self.ranking],
            "peer_median_composite": _opt_str(self.peer_median_composite),
            "anomaly_count": self.anomaly_count,
        }

    def to_json(self) -> str:
        """給 LLM 當 user message 用的 JSON（內容已受權限裁切）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _median(values: list[Decimal]) -> Decimal | None:
    """中位數。偶數個取中間兩個的平均；空清單回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    pair = ordered[mid - 1] + ordered[mid]
    return (pair / Decimal("2")).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _network_totals(scores: Iterable[SiteScore]) -> dict[str, Decimal]:
    """全網原始指標加總（僅總部視野呼叫）。"""
    totals = {metric: Decimal("0") for metric in RAW_METRICS}
    for score in scores:
        for metric in RAW_METRICS:
            totals[metric] += score.metrics.get(metric) or Decimal("0")
    totals["revenue"] = quantize_money(totals["revenue"])
    return totals


def build_ranking(scores: Iterable[SiteScore], median: Decimal | None) -> list[dict[str, Any]]:
    """跨據點排名 + 對同儕中位數的差距（僅總部視野呼叫）。"""
    rated = sorted(
        (score for score in scores if score.composite is not None),
        key=lambda item: item.composite,
        reverse=True,
    )
    ranking: list[dict[str, Any]] = []
    for position, score in enumerate(rated, start=1):
        delta = _index(score.composite, median)
        ranking.append({
            "rank": position,
            "site_id": score.site_id,
            "display_name": score.display_name,
            "region": score.region,
            "composite_score": str(score.composite),
            "tier": score.tier,
            "vs_peer_median_pct": None if delta is None else f"{delta - HUNDRED:+.1f}",
        })
    return ranking


def build_rollup(
    snapshots: Iterable[SiteSnapshot],
    failures: Iterable[SiteFailure],
    baselines: Mapping[str, MetricSet | None],
    weights: Mapping[str, Decimal],
    top_tier_min: Decimal,
    core_min: Decimal,
    deviation_pct: Decimal,
    pack: str,
    currency: str,
    period_label: str,
    include_cross_site: bool,
) -> RollupResult:
    """把快照折成完整匯總結果。

    `include_cross_site` 由呼叫端從 `AccessScope` 取得：False 時排名、同儕中位數與
    全網加總**根本不會被計算**，因此也不可能出現在任何序列化輸出裡。
    """
    scores = tuple(
        score_site(
            snapshot,
            baselines.get(snapshot.site_id),
            weights,
            top_tier_min,
            core_min,
            deviation_pct,
        )
        for snapshot in snapshots
    )
    median = (
        _median([score.composite for score in scores if score.composite is not None])
        if include_cross_site else None
    )
    return RollupResult(
        pack=pack,
        currency=currency,
        period_label=period_label,
        scores=scores,
        failures=tuple(failures),
        network_totals=_network_totals(scores) if include_cross_site else None,
        ranking=tuple(build_ranking(scores, median)) if include_cross_site else None,
        peer_median_composite=median,
    )


def partial_banner(failures: Iterable[SiteFailure]) -> str:
    """產出「⚠️ 部分資料：台南東區店 無回應」橫幅；全部正常時回空字串。

    橫幅必須放在報表**最上方**：讀者在看到任何排名之前就要知道這份排名不完整，
    否則會拿殘缺名次去做人事或資源決策。
    """
    names = [failure.display_name for failure in failures]
    if not names:
        return ""
    return f"⚠️ 部分資料：{'、'.join(names)} 無回應"
