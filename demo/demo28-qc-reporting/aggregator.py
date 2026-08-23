"""SPC 控制圖、Nelson Rules、預測性趨勢警告，以及品質警報的資料模型。

本檔負責「把 MES 的原始量測值變成警報」，不負責排版與發送。

三個刻意的設計決定：

1. **控制界限來自基準線，不從當期資料反算。**
   `lines.json` 的 `baseline_mean` / `baseline_sigma` 是製程穩定期建立的基準。
   若改用當期資料算 UCL/LCL，製程整體漂移時界限會跟著漂，於是永遠「都在管制內」——
   這是 SPC 最典型的自欺，也正是 ch07_p11 要消滅的「隔天早上才發現」。

2. **不良率分母為 0 時回 `None`，不回 0。**
   「這班沒有產出」與「這班不良率 0%」在品管上是完全不同的兩件事。
   前者要浮上去問為什麼停線，後者是好消息。混為一談會讓停線變成「表現優異」。

3. **警報是不可再聚合的原子單位。**
   `QualityAlert` 一旦產生就只能被聯集（union），不能被平均、不能被門檻過濾掉。
   平均值決定的是「敘述怎麼寫」，永遠不決定「警報要不要出現」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

TWO_PLACES = Decimal("0.01")
FOUR_PLACES = Decimal("0.0001")
HUNDRED = Decimal("100")


class SourceError(RuntimeError):
    """單一 MES 資料源取數失敗。不會中斷整條報告鏈。"""


class AlertSeverity(Enum):
    """品質警報嚴重度。排序用 `rank`，數字越小越嚴重。"""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"

    @property
    def rank(self) -> int:
        return {"critical": 0, "major": 1, "minor": 2}[self.value]

    @property
    def label(self) -> str:
        return {"critical": "🔴 重大", "major": "🟠 主要", "minor": "🟡 次要"}[self.value]


#: Nelson Rules 八條的中文名稱與判定摘要（apxG_p16：套用 Nelson Rules 偵測異常趨勢）。
NELSON_RULE_NAMES: dict[int, str] = {
    1: "單點超出 3σ 管制界限",
    2: "連續 9 點落在中心線同一側",
    3: "連續 6 點持續上升或持續下降",
    4: "連續 14 點上下交替震盪",
    5: "連續 3 點中有 2 點超出同側 2σ",
    6: "連續 5 點中有 4 點超出同側 1σ",
    7: "連續 15 點全部落在 ±1σ 之內（變異被過度壓縮，疑似量測失真）",
    8: "連續 8 點全部落在 ±1σ 之外（雙峰分布，疑似混料）",
}


def to_decimal(value: Any, context: str, field_name: str) -> Decimal:
    """把 JSON/YAML 的值轉成 Decimal。轉不動就明確報錯，不用 0 掩蓋。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{context} 的 {field_name} 不是合法數值：{value!r}") from exc


def quantize_pct(value: Decimal) -> Decimal:
    """百分比統一兩位小數，財務／品管慣例用 ROUND_HALF_UP 而非銀行家捨入。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def quantize_reading(value: Decimal) -> Decimal:
    """量測值統一四位小數，足以涵蓋 mm / Nm / µm 三種單位的現場解析度。"""
    return value.quantize(FOUR_PLACES, rounding=ROUND_HALF_UP)


def percent(part: Decimal, whole: Decimal) -> Decimal | None:
    """算百分比。分母為 0 一律回 None——「沒有產出」不是「0% 不良」。"""
    if whole == 0:
        return None
    return quantize_pct(part / whole * HUNDRED)


# --------------------------------------------------------------------------
# 資料模型
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LineSpec:
    """單一產線的規格與基準線（來自 lines.json）。"""

    line_id: str
    line_name: str
    process: str
    mes_system: str
    metric_name: str
    unit: str
    baseline_mean: Decimal
    baseline_sigma: Decimal
    usl: Decimal
    lsl: Decimal
    defect_rate_limit_pct: Decimal
    target_yield_pct: Decimal


@dataclass(frozen=True)
class ShiftRecord:
    """單一班別的原始 MES 紀錄。"""

    line_id: str
    shift_id: str
    shift_date: str
    shift_code: str
    supervisor: str
    units_produced: int
    units_defective: int
    readings: tuple[Decimal, ...]


@dataclass(frozen=True)
class LineOutage:
    """某產線在本期完全沒有資料。這本身就是一則 CRITICAL 警報。"""

    line_id: str
    line_name: str
    source_id: str
    reason: str
    last_seen_at: str | None


@dataclass(frozen=True)
class ControlChart:
    """控制圖三線與 1σ / 2σ 分區（Zone A/B/C）。"""

    mean: Decimal
    sigma: Decimal
    ucl: Decimal
    lcl: Decimal
    upper_2s: Decimal
    lower_2s: Decimal
    upper_1s: Decimal
    lower_1s: Decimal

    def sigma_offset(self, point: Decimal) -> Decimal:
        """回傳該點距中心線幾個 σ（帶正負號）。sigma 為 0 時回 0，不可除以零。"""
        if self.sigma == 0:
            return Decimal("0")
        return ((point - self.mean) / self.sigma).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)

    def to_dict(self) -> dict[str, str]:
        return {
            "ucl": str(self.ucl),
            "mean": str(self.mean),
            "lcl": str(self.lcl),
            "upper_2s": str(self.upper_2s),
            "lower_2s": str(self.lower_2s),
            "upper_1s": str(self.upper_1s),
            "lower_1s": str(self.lower_1s),
        }


@dataclass(frozen=True)
class NelsonViolation:
    """一條 Nelson Rule 的違反紀錄。`index` 是觸發時的最後一點序位（0-based）。"""

    rule: int
    name: str
    index: int
    detail: str


@dataclass(frozen=True)
class QualityAlert:
    """品質警報：整條報告鏈中唯一不可被聚合稀釋的單位。

    `alert_id` 必須在同一批資料上穩定重現，否則跨執行的沿用（carry forward）
    與四階之間的聯集檢查都會失效。
    """

    alert_id: str
    severity: AlertSeverity
    category: str
    line_id: str
    line_name: str
    shift_id: str | None
    origin_tier: str
    headline: str
    detail: str
    detected_at: str
    is_carry_forward: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "severity": self.severity.value,
            "category": self.category,
            "line_id": self.line_id,
            "line_name": self.line_name,
            "shift_id": self.shift_id,
            "origin_tier": self.origin_tier,
            "headline": self.headline,
            "detail": self.detail,
            "detected_at": self.detected_at,
            "is_carry_forward": self.is_carry_forward,
        }

    def as_carry_forward(self) -> "QualityAlert":
        """複製成「沿用自上次執行」的版本，供 state file 還原時使用。"""
        return QualityAlert(
            alert_id=self.alert_id,
            severity=self.severity,
            category=self.category,
            line_id=self.line_id,
            line_name=self.line_name,
            shift_id=self.shift_id,
            origin_tier=self.origin_tier,
            headline=self.headline,
            detail=self.detail,
            detected_at=self.detected_at,
            is_carry_forward=True,
        )


@dataclass(frozen=True)
class ShiftAnalysis:
    """單一班別的分析結果（班末報告的唯一資料來源）。"""

    record: ShiftRecord
    spec: LineSpec
    chart: ControlChart
    mean_reading: Decimal | None
    defect_rate_pct: Decimal | None
    yield_pct: Decimal | None
    out_of_spec: tuple[Decimal, ...]
    violations: tuple[NelsonViolation, ...]
    alerts: tuple[QualityAlert, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.record.line_id,
            "line_name": self.spec.line_name,
            "shift_id": self.record.shift_id,
            "shift_date": self.record.shift_date,
            "shift_code": self.record.shift_code,
            "supervisor": self.record.supervisor,
            "units_produced": self.record.units_produced,
            "units_defective": self.record.units_defective,
            "mean_reading": _opt_str(self.mean_reading),
            "defect_rate_pct": _opt_str(self.defect_rate_pct),
            "yield_pct": _opt_str(self.yield_pct),
            "out_of_spec_count": len(self.out_of_spec),
            "chart": self.chart.to_dict(),
            "violations": [
                {"rule": v.rule, "name": v.name, "index": v.index, "detail": v.detail}
                for v in self.violations
            ],
            "alerts": [alert.to_dict() for alert in self.alerts],
        }


@dataclass(frozen=True)
class TrendForecast:
    """單一產線的預測性趨勢外推（ch07_p11：瑕疵產生前 3-5 個班次預警）。"""

    line_id: str
    line_name: str
    window_shifts: int
    slope_pct_per_shift: Decimal | None
    latest_pct: Decimal | None
    limit_pct: Decimal
    shifts_to_breach: Decimal | None
    severity: AlertSeverity | None
    alerts: tuple[QualityAlert, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "line_name": self.line_name,
            "window_shifts": self.window_shifts,
            "slope_pct_per_shift": _opt_str(self.slope_pct_per_shift),
            "latest_pct": _opt_str(self.latest_pct),
            "limit_pct": str(self.limit_pct),
            "shifts_to_breach": _opt_str(self.shifts_to_breach),
            "severity": self.severity.value if self.severity else None,
            "alerts": [alert.to_dict() for alert in self.alerts],
        }


def _opt_str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# 載入
# --------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    """讀 JSON，把 OSError / JSONDecodeError 都轉成 SourceError 便於降級處理。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SourceError(f"讀不到資料檔 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise SourceError(f"資料檔 JSON 解析失敗 {path}：{exc}") from exc


def load_line_specs(path: Path) -> dict[str, LineSpec]:
    """讀 lines.json 的產線登錄表。"""
    payload = _read_json(path)
    specs: dict[str, LineSpec] = {}
    for raw in payload.get("lines") or []:
        line_id = str(raw.get("line_id", "")).strip()
        if not line_id:
            raise ValueError(f"{path} 中有產線缺少 line_id")
        specs[line_id] = _build_spec(line_id, raw)
    if not specs:
        raise ValueError(f"{path} 沒有任何產線登錄")
    return specs


def _build_spec(line_id: str, raw: dict[str, Any]) -> LineSpec:
    """把單筆產線登錄轉成 LineSpec。"""
    ctx = f"產線 {line_id}"
    return LineSpec(
        line_id=line_id,
        line_name=str(raw.get("line_name", line_id)),
        process=str(raw.get("process", "")),
        mes_system=str(raw.get("mes_system", "")),
        metric_name=str(raw.get("metric_name", "")),
        unit=str(raw.get("unit", "")),
        baseline_mean=to_decimal(raw.get("baseline_mean"), ctx, "baseline_mean"),
        baseline_sigma=to_decimal(raw.get("baseline_sigma"), ctx, "baseline_sigma"),
        usl=to_decimal(raw.get("usl"), ctx, "usl"),
        lsl=to_decimal(raw.get("lsl"), ctx, "lsl"),
        defect_rate_limit_pct=to_decimal(
            raw.get("defect_rate_limit_pct"), ctx, "defect_rate_limit_pct"
        ),
        target_yield_pct=to_decimal(raw.get("target_yield_pct"), ctx, "target_yield_pct"),
    )


def load_source(
    path: Path,
    source_id: str,
    specs: dict[str, LineSpec],
) -> tuple[list[ShiftRecord], list[LineOutage]]:
    """讀一個 MES 資料源，回傳 `(班別紀錄, 無資料產線)`。

    `status != "ok"` 或 shifts 為空都算 outage——**不會**被當成「本期表現良好」。
    """
    payload = _read_json(path)
    records: list[ShiftRecord] = []
    outages: list[LineOutage] = []

    for line_payload in payload.get("lines") or []:
        line_id = str(line_payload.get("line_id", "")).strip()
        spec = specs.get(line_id)
        if spec is None:
            raise SourceError(f"{path} 回傳未登錄的產線 {line_id!r}，請先更新 lines.json")
        shifts = line_payload.get("shifts") or []
        if str(line_payload.get("status", "ok")) != "ok" or not shifts:
            outages.append(_build_outage(line_payload, spec, source_id))
            continue
        records.extend(_build_record(line_id, shift) for shift in shifts)

    return records, outages


def _build_outage(line_payload: dict[str, Any], spec: LineSpec, source_id: str) -> LineOutage:
    """把「無資料」轉成明確的 LineOutage。"""
    return LineOutage(
        line_id=spec.line_id,
        line_name=spec.line_name,
        source_id=source_id,
        reason=str(line_payload.get("reason") or "MES 未回傳任何班別資料，原因不明"),
        last_seen_at=(
            str(line_payload["last_seen_at"]) if line_payload.get("last_seen_at") else None
        ),
    )


def _build_record(line_id: str, raw: dict[str, Any]) -> ShiftRecord:
    """把單筆班別 JSON 轉成 ShiftRecord。"""
    shift_id = str(raw.get("shift_id", "")).strip()
    ctx = f"班別 {shift_id or line_id}"
    readings = raw.get("readings") or []
    if not isinstance(readings, list):
        raise SourceError(f"{ctx} 的 readings 必須是陣列")
    return ShiftRecord(
        line_id=line_id,
        shift_id=shift_id,
        shift_date=str(raw.get("shift_date", "")),
        shift_code=str(raw.get("shift_code", "")),
        supervisor=str(raw.get("supervisor", "（未指定）")),
        units_produced=int(raw.get("units_produced", 0)),
        units_defective=int(raw.get("units_defective", 0)),
        readings=tuple(
            to_decimal(item, ctx, "readings[]") for item in readings
        ),
    )


# --------------------------------------------------------------------------
# 控制圖與 Nelson Rules
# --------------------------------------------------------------------------


def build_chart(spec: LineSpec, sigma_multiplier: Decimal = Decimal("3")) -> ControlChart:
    """由基準線推出 UCL / Mean / LCL 三線與 1σ / 2σ 分區。"""
    mean, sigma = spec.baseline_mean, spec.baseline_sigma
    return ControlChart(
        mean=mean,
        sigma=sigma,
        ucl=mean + sigma_multiplier * sigma,
        lcl=mean - sigma_multiplier * sigma,
        upper_2s=mean + Decimal("2") * sigma,
        lower_2s=mean - Decimal("2") * sigma,
        upper_1s=mean + sigma,
        lower_1s=mean - sigma,
    )


def _zscores(points: Sequence[Decimal], chart: ControlChart) -> list[Decimal]:
    """把量測點換算成帶正負號的 σ 距離。"""
    return [chart.sigma_offset(point) for point in points]


def _rule1(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """單點超出 3σ。"""
    return [(i, f"第 {i + 1} 點 z={value}") for i, value in enumerate(z) if abs(value) > 3]


def _same_side_run(z: Sequence[Decimal], length: int, threshold: Decimal) -> list[tuple[int, str]]:
    """找出「連續 length 點都在同側且 |z| > threshold」的最早結束位置。"""
    hits: list[tuple[int, str]] = []
    for end in range(length - 1, len(z)):
        window = z[end - length + 1 : end + 1]
        if all(v > threshold for v in window) or all(v < -threshold for v in window):
            side = "上方" if window[0] > 0 else "下方"
            hits.append((end, f"第 {end - length + 2}–{end + 1} 點連續落在中心線{side}"))
            break
    return hits


def _rule2(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """連續 9 點同側。"""
    return _same_side_run(z, 9, Decimal("0"))


def _rule3(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """連續 6 點單調上升或下降。"""
    for end in range(5, len(z)):
        window = z[end - 5 : end + 1]
        rising = all(b > a for a, b in zip(window, window[1:]))
        falling = all(b < a for a, b in zip(window, window[1:]))
        if rising or falling:
            trend = "持續上升" if rising else "持續下降"
            return [(end, f"第 {end - 4}–{end + 1} 點{trend}（{window[0]}σ → {window[-1]}σ）")]
    return []


def _rule4(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """連續 14 點上下交替。"""
    for end in range(13, len(z)):
        window = z[end - 13 : end + 1]
        diffs = [b - a for a, b in zip(window, window[1:])]
        if all(x * y < 0 for x, y in zip(diffs, diffs[1:])) and all(d != 0 for d in diffs):
            return [(end, f"第 {end - 12}–{end + 1} 點上下交替震盪")]
    return []


def _k_of_n(z: Sequence[Decimal], k: int, n: int, threshold: Decimal) -> list[tuple[int, str]]:
    """連續 n 點中有 k 點超出同側 threshold σ。"""
    for end in range(n - 1, len(z)):
        window = z[end - n + 1 : end + 1]
        for sign, side in ((1, "上"), (-1, "下")):
            beyond = [v for v in window if v * sign > threshold]
            if len(beyond) >= k:
                return [(end, f"第 {end - n + 2}–{end + 1} 點中有 {len(beyond)} 點超出{side}側 {threshold}σ")]
    return []


def _rule5(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """連續 3 點中有 2 點超出同側 2σ。"""
    return _k_of_n(z, 2, 3, Decimal("2"))


def _rule6(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """連續 5 點中有 4 點超出同側 1σ。"""
    return _k_of_n(z, 4, 5, Decimal("1"))


def _rule7(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """連續 15 點全部落在 ±1σ 內（變異被過度壓縮）。"""
    for end in range(14, len(z)):
        if all(abs(v) < 1 for v in z[end - 14 : end + 1]):
            return [(end, f"第 {end - 13}–{end + 1} 點全部落在 ±1σ 內")]
    return []


def _rule8(z: Sequence[Decimal]) -> list[tuple[int, str]]:
    """連續 8 點全部落在 ±1σ 外（雙峰分布）。"""
    for end in range(7, len(z)):
        if all(abs(v) > 1 for v in z[end - 7 : end + 1]):
            return [(end, f"第 {end - 6}–{end + 1} 點全部落在 ±1σ 外")]
    return []


#: 八條規則的判定函式表。刻意用表而不是 if-elif 串，加規則不用改判定主流程。
NELSON_RULES: dict[int, Callable[[Sequence[Decimal]], list[tuple[int, str]]]] = {
    1: _rule1,
    2: _rule2,
    3: _rule3,
    4: _rule4,
    5: _rule5,
    6: _rule6,
    7: _rule7,
    8: _rule8,
}


def nelson_violations(
    points: Sequence[Decimal],
    chart: ControlChart,
    enabled_rules: Iterable[int] = tuple(NELSON_RULES),
) -> list[NelsonViolation]:
    """對一串點套用 Nelson Rules，回傳所有違反紀錄（每條規則最多報一次）。"""
    if not points:
        return []
    z = _zscores(points, chart)
    found: list[NelsonViolation] = []
    for rule in sorted(set(enabled_rules)):
        checker = NELSON_RULES.get(rule)
        if checker is None:
            continue
        for index, detail in checker(z):
            found.append(NelsonViolation(rule, NELSON_RULE_NAMES[rule], index, detail))
    return found


# --------------------------------------------------------------------------
# 閾值設定
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """SPC 與警報升級門檻（全部來自 config.yaml，不寫死在程式碼）。"""

    sigma_multiplier: Decimal
    enabled_rules: tuple[int, ...]
    nelson_severity: dict[int, AlertSeverity]
    critical_defect_multiplier: Decimal
    yield_shortfall_pct: Decimal
    out_of_spec_severity: AlertSeverity


@dataclass(frozen=True)
class TrendConfig:
    """預測性趨勢外推設定（ch07_p11：瑕疵產生前 3-5 個班次警告）。"""

    window_shifts: int
    warn_within_shifts: Decimal
    critical_within_shifts: Decimal
    min_slope_pct_per_shift: Decimal


def _severity(value: Any, fallback: AlertSeverity) -> AlertSeverity:
    """把 config 的字串轉成 AlertSeverity；無法辨識就用 fallback。"""
    try:
        return AlertSeverity(str(value).strip().lower())
    except ValueError:
        return fallback


def load_thresholds(spc_cfg: dict[str, Any] | None, thr_cfg: dict[str, Any] | None) -> Thresholds:
    """從 config.yaml 的 spc / thresholds 兩區塊建立門檻設定。"""
    spc_cfg, thr_cfg = spc_cfg or {}, thr_cfg or {}
    raw_severity = spc_cfg.get("nelson_severity") or {}
    severity_map = {
        int(key): _severity(value, AlertSeverity.MAJOR) for key, value in raw_severity.items()
    }
    rules = spc_cfg.get("enabled_rules") or list(NELSON_RULES)
    return Thresholds(
        sigma_multiplier=to_decimal(spc_cfg.get("sigma_multiplier", 3), "spc", "sigma_multiplier"),
        enabled_rules=tuple(int(rule) for rule in rules),
        nelson_severity=severity_map,
        critical_defect_multiplier=to_decimal(
            thr_cfg.get("critical_defect_multiplier", "2.0"),
            "thresholds",
            "critical_defect_multiplier",
        ),
        yield_shortfall_pct=to_decimal(
            thr_cfg.get("yield_shortfall_pct", "0.50"), "thresholds", "yield_shortfall_pct"
        ),
        out_of_spec_severity=_severity(
            thr_cfg.get("out_of_spec_severity", "critical"), AlertSeverity.CRITICAL
        ),
    )


def load_trend_config(raw: dict[str, Any] | None) -> TrendConfig:
    """從 config.yaml 的 trend 區塊建立趨勢外推設定。"""
    raw = raw or {}
    return TrendConfig(
        window_shifts=max(int(raw.get("window_shifts", 6)), 2),
        warn_within_shifts=to_decimal(
            raw.get("warn_within_shifts", 5), "trend", "warn_within_shifts"
        ),
        critical_within_shifts=to_decimal(
            raw.get("critical_within_shifts", 3), "trend", "critical_within_shifts"
        ),
        min_slope_pct_per_shift=to_decimal(
            raw.get("min_slope_pct_per_shift", "0.01"), "trend", "min_slope_pct_per_shift"
        ),
    )


# --------------------------------------------------------------------------
# 單班分析
# --------------------------------------------------------------------------


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    """算術平均。空序列回 None，不用 0 假裝有量測。"""
    if not values:
        return None
    return quantize_reading(sum(values, Decimal("0")) / Decimal(len(values)))


def _alert(
    alert_id: str,
    severity: AlertSeverity,
    category: str,
    spec: LineSpec,
    shift_id: str | None,
    origin_tier: str,
    headline: str,
    detail: str,
    detected_at: str,
) -> QualityAlert:
    """建立 QualityAlert 的統一入口，確保欄位齊全、id 命名一致。"""
    return QualityAlert(
        alert_id=alert_id,
        severity=severity,
        category=category,
        line_id=spec.line_id,
        line_name=spec.line_name,
        shift_id=shift_id,
        origin_tier=origin_tier,
        headline=headline,
        detail=detail,
        detected_at=detected_at,
    )


def sort_alerts(alerts: Iterable[QualityAlert]) -> list[QualityAlert]:
    """統一排序：嚴重度優先，其次產線、班別、id，確保輸出可重現。"""
    return sorted(
        alerts,
        key=lambda a: (a.severity.rank, a.line_id, a.shift_id or "", a.alert_id),
    )


def dedupe_alerts(alerts: Iterable[QualityAlert]) -> list[QualityAlert]:
    """依 alert_id 去重並保持排序。聯集（union）永遠不會弄丟任何一則。"""
    seen: dict[str, QualityAlert] = {}
    for alert in alerts:
        # 同 id 時保留非沿用版本：本次實際偵測到的資訊比上次的快照新。
        if alert.alert_id not in seen or (seen[alert.alert_id].is_carry_forward and not alert.is_carry_forward):
            seen[alert.alert_id] = alert
    return sort_alerts(seen.values())


def _spec_alerts(
    record: ShiftRecord,
    spec: LineSpec,
    out_of_spec: Sequence[Decimal],
    thresholds: Thresholds,
    detected_at: str,
) -> list[QualityAlert]:
    """超出規格上下限（USL / LSL）的警報。超規是出貨風險，不是統計現象。"""
    if not out_of_spec:
        return []
    listed = "、".join(f"{value}{spec.unit}" for value in out_of_spec[:5])
    return [
        _alert(
            f"spec:{record.line_id}:{record.shift_id}:out_of_spec",
            thresholds.out_of_spec_severity,
            "spec",
            spec,
            record.shift_id,
            "shift_end",
            f"{spec.line_name} 有 {len(out_of_spec)} 點量測值超出規格界限",
            f"規格 {spec.lsl}–{spec.usl}{spec.unit}，超規值：{listed}。"
            "超規品不得放行，須立即隔離該時段產出並啟動追溯。",
            detected_at,
        )
    ]


def _nelson_alerts(
    record: ShiftRecord,
    spec: LineSpec,
    violations: Sequence[NelsonViolation],
    thresholds: Thresholds,
    detected_at: str,
) -> list[QualityAlert]:
    """把單班（班內即時）的 Nelson Rule 違反轉成警報。"""
    alerts: list[QualityAlert] = []
    for violation in violations:
        severity = thresholds.nelson_severity.get(violation.rule, AlertSeverity.MAJOR)
        alerts.append(
            _alert(
                f"nelson:{record.line_id}:{record.shift_id}:R{violation.rule}",
                severity,
                "nelson",
                spec,
                record.shift_id,
                "shift_end",
                f"{spec.line_name}｜{record.shift_code} 班觸發 Nelson Rule {violation.rule}",
                f"{violation.name}；{violation.detail}。控制圖基準："
                f"UCL {spec.baseline_mean + 3 * spec.baseline_sigma}／"
                f"Mean {spec.baseline_mean}／LCL {spec.baseline_mean - 3 * spec.baseline_sigma}。",
                detected_at,
            )
        )
    return alerts


def _defect_alert(
    record: ShiftRecord,
    spec: LineSpec,
    defect_rate: Decimal,
    thresholds: Thresholds,
    detected_at: str,
) -> QualityAlert:
    """不良率超過該線上限時的警報；達上限倍數即升為重大。"""
    limit = spec.defect_rate_limit_pct
    is_critical = defect_rate >= limit * thresholds.critical_defect_multiplier
    return _alert(
        f"defect:{record.line_id}:{record.shift_id}:rate",
        AlertSeverity.CRITICAL if is_critical else AlertSeverity.MAJOR,
        "defect_rate",
        spec,
        record.shift_id,
        "shift_end",
        f"{spec.line_name}｜{record.shift_code} 班不良率 {defect_rate}% 超過上限 {limit}%",
        f"本班投入 {record.units_produced} 件、不良 {record.units_defective} 件。",
        detected_at,
    )


def _yield_alert(
    record: ShiftRecord, spec: LineSpec, yield_pct: Decimal, detected_at: str
) -> QualityAlert:
    """良率未達目標的警報。"""
    return _alert(
        f"yield:{record.line_id}:{record.shift_id}:shortfall",
        AlertSeverity.MINOR,
        "yield",
        spec,
        record.shift_id,
        "shift_end",
        f"{spec.line_name}｜{record.shift_code} 班良率 {yield_pct}% 低於目標 {spec.target_yield_pct}%",
        f"缺口 {quantize_pct(spec.target_yield_pct - yield_pct)} 個百分點。",
        detected_at,
    )


def _no_output_alert(record: ShiftRecord, spec: LineSpec, detected_at: str) -> QualityAlert:
    """本班投入為 0：那不是「0% 不良」，是停線，必須有人回答為什麼。"""
    return _alert(
        f"no_output:{record.line_id}:{record.shift_id}",
        AlertSeverity.MAJOR,
        "no_output",
        spec,
        record.shift_id,
        "shift_end",
        f"{spec.line_name}｜{record.shift_code} 班投入數為 0，無法計算不良率",
        "分母為 0 時不良率一律以「無法計算」呈現，不得記為 0%。"
        "請確認是計畫停機或資料未上傳。",
        detected_at,
    )


def _rate_alerts(
    record: ShiftRecord,
    spec: LineSpec,
    defect_rate: Decimal | None,
    yield_pct: Decimal | None,
    thresholds: Thresholds,
    detected_at: str,
) -> list[QualityAlert]:
    """不良率超標與良率未達目標的警報。"""
    if defect_rate is None:
        return [_no_output_alert(record, spec, detected_at)]

    alerts: list[QualityAlert] = []
    if defect_rate > spec.defect_rate_limit_pct:
        alerts.append(_defect_alert(record, spec, defect_rate, thresholds, detected_at))
    if yield_pct is not None and yield_pct < spec.target_yield_pct - thresholds.yield_shortfall_pct:
        alerts.append(_yield_alert(record, spec, yield_pct, detected_at))
    return alerts


def analyse_shift(
    record: ShiftRecord,
    spec: LineSpec,
    chart: ControlChart,
    thresholds: Thresholds,
    detected_at: str,
) -> ShiftAnalysis:
    """分析單一班別：控制圖判定 + 比率 + 該班所有警報。"""
    produced = Decimal(record.units_produced)
    defective = Decimal(record.units_defective)
    defect_rate = percent(defective, produced)
    yield_pct = percent(produced - defective, produced)
    out_of_spec = tuple(v for v in record.readings if v > spec.usl or v < spec.lsl)
    violations = tuple(nelson_violations(record.readings, chart, thresholds.enabled_rules))

    alerts = (
        _spec_alerts(record, spec, out_of_spec, thresholds, detected_at)
        + _nelson_alerts(record, spec, violations, thresholds, detected_at)
        + _rate_alerts(record, spec, defect_rate, yield_pct, thresholds, detected_at)
    )
    return ShiftAnalysis(
        record=record,
        spec=spec,
        chart=chart,
        mean_reading=_mean(record.readings),
        defect_rate_pct=defect_rate,
        yield_pct=yield_pct,
        out_of_spec=out_of_spec,
        violations=violations,
        alerts=tuple(sort_alerts(alerts)),
    )


# --------------------------------------------------------------------------
# 跨班趨勢與預測性警告
# --------------------------------------------------------------------------


def linear_slope(values: Sequence[Decimal]) -> Decimal | None:
    """最小平方線性回歸的斜率（每前進一個班次的變化量）。

    少於兩點無法定義趨勢時回 None——**不回 0**。回 0 會讓下游誤以為
    「量過了，很平穩」，實際上是「根本沒有足夠資料可以判斷」。
    """
    n = len(values)
    if n < 2:
        return None
    xs = [Decimal(i) for i in range(n)]
    mean_x = sum(xs, Decimal("0")) / Decimal(n)
    mean_y = sum(values, Decimal("0")) / Decimal(n)
    sxx = sum(((x - mean_x) ** 2 for x in xs), Decimal("0"))
    if sxx == 0:
        return None
    sxy = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)), Decimal("0"))
    return (sxy / sxx).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _shifts_to_breach(latest: Decimal, limit: Decimal, slope: Decimal) -> Decimal | None:
    """依線性外推推算還有幾個班次會撞上限。已經超標或趨勢向下時回 None。"""
    if slope <= 0 or latest >= limit:
        return None
    return ((limit - latest) / slope).quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def _forecast_severity(remaining: Decimal, cfg: TrendConfig) -> AlertSeverity | None:
    """把「還剩幾班撞線」換成嚴重度。超出預警視野就不發警報（避免狼來了）。"""
    if remaining <= cfg.critical_within_shifts:
        return AlertSeverity.CRITICAL
    if remaining <= cfg.warn_within_shifts:
        return AlertSeverity.MAJOR
    return None


def _forecast_alert(
    spec: LineSpec,
    remaining: Decimal,
    slope: Decimal,
    latest: Decimal,
    severity: AlertSeverity,
    period_key: str,
    detected_at: str,
) -> QualityAlert:
    """預測性趨勢警告的警報物件。"""
    return _alert(
        f"trend:{spec.line_id}:{period_key}:forecast",
        severity,
        "trend_forecast",
        spec,
        None,
        "line_trend",
        f"{spec.line_name} 不良率預計在 {remaining} 個班次後超過上限 "
        f"{spec.defect_rate_limit_pct}%",
        f"最近不良率 {latest}%，趨勢斜率 +{slope} 個百分點/班。"
        "此為瑕疵尚未發生前的預警，處置時機在下一個班次的製程調機，不是事後挑選。",
        detected_at,
    )


def forecast_line(
    spec: LineSpec,
    shifts: Sequence[ShiftAnalysis],
    cfg: TrendConfig,
    period_key: str,
    detected_at: str,
) -> TrendForecast:
    """對單一產線的不良率序列做線性外推，產出預測性趨勢警告。"""
    rates = [s.defect_rate_pct for s in shifts if s.defect_rate_pct is not None]
    window = rates[-cfg.window_shifts :]
    slope = linear_slope(window)
    latest = window[-1] if window else None

    remaining, severity, alerts = None, None, []
    if slope is not None and latest is not None and slope >= cfg.min_slope_pct_per_shift:
        remaining = _shifts_to_breach(latest, spec.defect_rate_limit_pct, slope)
        if remaining is not None:
            severity = _forecast_severity(remaining, cfg)
        if severity is not None and remaining is not None:
            alerts.append(
                _forecast_alert(spec, remaining, slope, latest, severity, period_key, detected_at)
            )

    return TrendForecast(
        line_id=spec.line_id,
        line_name=spec.line_name,
        window_shifts=len(window),
        slope_pct_per_shift=slope,
        latest_pct=latest,
        limit_pct=spec.defect_rate_limit_pct,
        shifts_to_breach=remaining,
        severity=severity,
        alerts=tuple(alerts),
    )


def _trend_alerts(
    spec: LineSpec,
    violations: Sequence[NelsonViolation],
    thresholds: Thresholds,
    period_key: str,
    detected_at: str,
) -> list[QualityAlert]:
    """跨班（班別平均值序列）的 Nelson Rule 違反轉成警報。

    這一層抓的是「單班看起來都還好，但一路在往同一個方向走」——
    正是舊流程要等到隔天早上、甚至客訴才會發現的那種漂移。
    """
    alerts: list[QualityAlert] = []
    for violation in violations:
        severity = thresholds.nelson_severity.get(violation.rule, AlertSeverity.MAJOR)
        alerts.append(
            _alert(
                f"nelson-line:{spec.line_id}:{period_key}:R{violation.rule}",
                severity,
                "nelson_trend",
                spec,
                None,
                "line_trend",
                f"{spec.line_name} 跨班趨勢觸發 Nelson Rule {violation.rule}",
                f"{violation.name}；{violation.detail}（以各班量測平均值為點）。",
                detected_at,
            )
        )
    return alerts


@dataclass(frozen=True)
class LineAnalysis:
    """單一產線在本期的完整分析（班別 + 跨班趨勢 + 預測）。"""

    spec: LineSpec
    chart: ControlChart
    shifts: tuple[ShiftAnalysis, ...]
    trend_violations: tuple[NelsonViolation, ...]
    trend_alerts: tuple[QualityAlert, ...]
    forecast: TrendForecast

    @property
    def alerts(self) -> tuple[QualityAlert, ...]:
        """本產線的全部警報（班別 + 跨班 + 預測），聯集後排序。"""
        collected: list[QualityAlert] = []
        for shift in self.shifts:
            collected.extend(shift.alerts)
        collected.extend(self.trend_alerts)
        collected.extend(self.forecast.alerts)
        return tuple(dedupe_alerts(collected))

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_id": self.spec.line_id,
            "line_name": self.spec.line_name,
            "process": self.spec.process,
            "mes_system": self.spec.mes_system,
            "metric": f"{self.spec.metric_name}（{self.spec.unit}）",
            "chart": self.chart.to_dict(),
            "shift_count": len(self.shifts),
            "trend_violations": [
                {"rule": v.rule, "name": v.name, "detail": v.detail} for v in self.trend_violations
            ],
            "forecast": self.forecast.to_dict(),
            "alerts": [alert.to_dict() for alert in self.alerts],
        }


def shift_sort_key(analysis: ShiftAnalysis) -> tuple[str, str]:
    """班別排序：先日期再班別代碼（A 早班 → B 中班 → C 夜班）。"""
    return (analysis.record.shift_date, analysis.record.shift_code)


def analyse_line(
    spec: LineSpec,
    records: Sequence[ShiftRecord],
    thresholds: Thresholds,
    trend_cfg: TrendConfig,
    period_key: str,
    detected_at: str,
) -> LineAnalysis:
    """分析單一產線的所有班別，並疊上跨班趨勢與預測性警告。"""
    chart = build_chart(spec, thresholds.sigma_multiplier)
    shifts = sorted(
        (analyse_shift(record, spec, chart, thresholds, detected_at) for record in records),
        key=shift_sort_key,
    )
    means = [s.mean_reading for s in shifts if s.mean_reading is not None]
    violations = tuple(nelson_violations(means, chart, thresholds.enabled_rules))
    return LineAnalysis(
        spec=spec,
        chart=chart,
        shifts=tuple(shifts),
        trend_violations=violations,
        trend_alerts=tuple(_trend_alerts(spec, violations, thresholds, period_key, detected_at)),
        forecast=forecast_line(spec, shifts, trend_cfg, period_key, detected_at),
    )


# --------------------------------------------------------------------------
# 全廠彙整
# --------------------------------------------------------------------------


def outage_alert(outage: LineOutage, period_key: str, detected_at: str) -> QualityAlert:
    """把「某產線無資料」轉成 CRITICAL 警報。

    這是本模組最容易被做錯的地方：沒有資料的產線若被跳過，
    全廠平均不良率會**變好看**（少了一條線的不良品拉抬），
    於是「感測器壞掉」在報表上長得跟「品質改善」一模一樣。
    """
    return QualityAlert(
        alert_id=f"outage:{outage.line_id}:{period_key}",
        severity=AlertSeverity.CRITICAL,
        category="data_outage",
        line_id=outage.line_id,
        line_name=outage.line_name,
        shift_id=None,
        origin_tier="ingest",
        headline=f"{outage.line_name} 本期無任何 MES 資料（來源：{outage.source_id}）",
        detail=f"{outage.reason}"
        + (f"；最後一次回應時間 {outage.last_seen_at}" if outage.last_seen_at else "")
        + "。該線本期產出未納入任何統計，全廠平均值不得視為已涵蓋此線。",
        detected_at=detected_at,
    )


@dataclass(frozen=True)
class PlantAnalysis:
    """全廠在本期的分析結果，是四階報告鏈唯一的資料輸入。"""

    period_key: str
    lines: tuple[LineAnalysis, ...]
    outages: tuple[LineOutage, ...]
    outage_alerts: tuple[QualityAlert, ...]

    @property
    def shifts(self) -> tuple[ShiftAnalysis, ...]:
        collected: list[ShiftAnalysis] = []
        for line in self.lines:
            collected.extend(line.shifts)
        return tuple(sorted(collected, key=shift_sort_key))

    @property
    def alerts(self) -> tuple[QualityAlert, ...]:
        collected: list[QualityAlert] = list(self.outage_alerts)
        for line in self.lines:
            collected.extend(line.alerts)
        return tuple(dedupe_alerts(collected))

    def to_dict(self) -> dict[str, Any]:
        return {
            "period_key": self.period_key,
            "lines": [line.to_dict() for line in self.lines],
            "outages": [
                {
                    "line_id": o.line_id,
                    "line_name": o.line_name,
                    "source_id": o.source_id,
                    "reason": o.reason,
                    "last_seen_at": o.last_seen_at,
                }
                for o in self.outages
            ],
            "alerts": [alert.to_dict() for alert in self.alerts],
        }


def analyse_plant(
    specs: dict[str, LineSpec],
    records: Sequence[ShiftRecord],
    outages: Sequence[LineOutage],
    thresholds: Thresholds,
    trend_cfg: TrendConfig,
    period_key: str,
    detected_at: str,
) -> PlantAnalysis:
    """把所有 MES 班別紀錄依產線分組後逐線分析，並把無資料產線轉成警報。"""
    grouped: dict[str, list[ShiftRecord]] = {}
    for record in records:
        grouped.setdefault(record.line_id, []).append(record)

    lines = tuple(
        analyse_line(specs[line_id], grouped[line_id], thresholds, trend_cfg, period_key, detected_at)
        for line_id in sorted(grouped)
    )
    return PlantAnalysis(
        period_key=period_key,
        lines=lines,
        outages=tuple(outages),
        outage_alerts=tuple(outage_alert(o, period_key, detected_at) for o in outages),
    )


def aggregate_shifts(shifts: Sequence[ShiftAnalysis]) -> dict[str, Any]:
    """把一批班別聚合成統計摘要。

    **注意**：這個函式只產生「敘述用的數字」。它永遠不決定警報要不要出現——
    警報走 `dedupe_alerts` 的聯集路徑，跟這裡算出來的平均值完全無關。
    """
    produced = sum((Decimal(s.record.units_produced) for s in shifts), Decimal("0"))
    defective = sum((Decimal(s.record.units_defective) for s in shifts), Decimal("0"))
    rates = [s.defect_rate_pct for s in shifts if s.defect_rate_pct is not None]
    return {
        "shift_count": len(shifts),
        "units_produced": int(produced),
        "units_defective": int(defective),
        "defect_rate_pct": _opt_str(percent(defective, produced)),
        "yield_pct": _opt_str(percent(produced - defective, produced)),
        "worst_shift_defect_rate_pct": _opt_str(max(rates)) if rates else None,
        "lines_covered": sorted({s.record.line_id for s in shifts}),
    }


def count_by_severity(alerts: Iterable[QualityAlert]) -> dict[str, int]:
    """依嚴重度統計警報數量，四階報告的標頭都會用到。"""
    counts = {severity.value: 0 for severity in AlertSeverity}
    for alert in alerts:
        counts[alert.severity.value] += 1
    return counts


def alert_from_dict(raw: dict[str, Any]) -> QualityAlert:
    """從狀態檔還原 QualityAlert。欄位缺漏就明確拋錯，不用預設值掩蓋。

    刻意不對缺欄位補預設：一則還原後 severity 變成 minor 的重大警報，
    比一則讀取失敗的警報危險得多。
    """
    required = ("alert_id", "severity", "category", "line_id", "line_name", "headline")
    missing = [key for key in required if not raw.get(key)]
    if missing:
        raise ValueError(f"狀態檔的警報紀錄缺少必要欄位：{'、'.join(missing)}")
    return QualityAlert(
        alert_id=str(raw["alert_id"]),
        severity=AlertSeverity(str(raw["severity"])),
        category=str(raw["category"]),
        line_id=str(raw["line_id"]),
        line_name=str(raw["line_name"]),
        shift_id=str(raw["shift_id"]) if raw.get("shift_id") else None,
        origin_tier=str(raw.get("origin_tier", "unknown")),
        headline=str(raw["headline"]),
        detail=str(raw.get("detail", "")),
        detected_at=str(raw.get("detected_at", "")),
        is_carry_forward=bool(raw.get("is_carry_forward", False)),
    )


def load_plant_name(path: Path) -> str:
    """從產線登錄表取得廠別名稱，供報告抬頭使用。"""
    payload = _read_json(path)
    return str((payload.get("plant") or {}).get("plant_name") or "全廠")
