"""四階報告鏈：Shift End → Daily 06:00 → Weekly → Monthly Board PDF Pack。

這是整個模組的技術重點（apxG_p16 逐字）：

| 階 | 受眾 | 聚合層級 | 詳細度 | 格式 |
| --- | --- | --- | --- | --- |
| Shift End | 現場領班 Supervisor | 單班 × 單線 | 逐點量測 + 逐條 Nelson 判定 | HTML / Slack |
| Daily 06:00 | 營運總監 Ops Director | 單日 × 全線 | 跨班對比 + 預測性警告 | PDF |
| Weekly | 品質經理 Quality Manager | 單週 × 全廠 | 週對週比較 + 重複性問題 | PDF |
| Monthly | 董事會 Board | 單月 × 全廠 | 高層摘要 + 決策建議 | PDF Pack |

**同一份底層資料，四種呈現。** 差別不在數字，而在「誰要拿它做什麼決定」：
領班要知道現在該不該停線調機；廠長要知道今天要把人力壓在哪條線；
品質經理要知道這個問題是偶發還是體質；董事會要知道要不要批預算換模具。

---

## 本檔最重要的一條規則：警報只做聯集，永遠不做平均

`_escalate()` 是唯一的往上傳遞路徑，它做的事只有「聯集 + 去重 + 排序」。
沒有任何一條程式路徑能讓警報在往上走的時候消失。

`enforce_no_suppression` 開啟時，每一階之間都會跑 `assert_no_alert_dropped()`，
下階的任何一則警報沒出現在上階就直接拋 `AlertSuppressionError` 讓整條鏈失敗。
**寧可整條報告鏈當掉，也不要送出一份「看起來很正常」的月報。**

這比照 demo05「星等硬性決定路由」的精神：安全關鍵訊號不交給聚合邏輯決定。
品管場景的風險更直接——被平滑掉的那條 3σ 超限，下游就是客戶手上的瑕疵品。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Sequence

from aggregator import (
    AlertSeverity,
    LineAnalysis,
    LineSpec,
    PlantAnalysis,
    QualityAlert,
    ShiftAnalysis,
    aggregate_shifts,
    count_by_severity,
    dedupe_alerts,
    quantize_pct,
    to_decimal,
)


class AlertSuppressionError(RuntimeError):
    """上階報告漏掉了下階已偵測到的警報。這是本模組唯一不可容忍的錯誤。"""


class ReportTier(Enum):
    """四階報告鏈的層級。順序即上報順序。"""

    SHIFT_END = "shift_end"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


@dataclass(frozen=True)
class TierProfile:
    """每一階的受眾、聚合層級、詳細度與輸出格式。"""

    tier: ReportTier
    title: str
    audience: str
    cadence: str
    output_format: str
    aggregation: str
    detail_level: str
    decision: str


#: 四階的固定設定（apxG_p16）。受眾與格式不可由 config 覆寫——
#: 「班長收到的是董事會版本」這種錯配，比報告晚送更傷信任。
TIER_PROFILES: dict[ReportTier, TierProfile] = {
    ReportTier.SHIFT_END: TierProfile(
        tier=ReportTier.SHIFT_END,
        title="班末品質報告",
        audience="現場領班（Supervisor）",
        cadence="每一班結束即時觸發",
        output_format="HTML / Slack",
        aggregation="單班 × 單線",
        detail_level="逐點量測值、逐條 Nelson Rule 判定、立即處置指示",
        decision="現在要不要停線、調機、隔離本班產出",
    ),
    ReportTier.DAILY: TierProfile(
        tier=ReportTier.DAILY,
        title="每日品質晨報",
        audience="營運總監 / 廠長（Ops Director）",
        cadence="每日 06:00",
        output_format="PDF（本模組輸出 PDF 版面結構，不產生 PDF 檔）",
        aggregation="單日 × 全產線",
        detail_level="跨班對比、當日趨勢、預測性警告、未結案警報",
        decision="今天人力與稽核資源要壓在哪一條線",
    ),
    ReportTier.WEEKLY: TierProfile(
        tier=ReportTier.WEEKLY,
        title="週品質回顧",
        audience="品質經理（Quality Manager）",
        cadence="每週一 07:00",
        output_format="PDF（本模組輸出 PDF 版面結構，不產生 PDF 檔）",
        aggregation="單週 × 全廠",
        detail_level="週對週比較、產線排名、重複性問題辨識",
        decision="這是偶發事件還是製程體質問題，要不要開矯正措施單",
    ),
    ReportTier.MONTHLY: TierProfile(
        tier=ReportTier.MONTHLY,
        title="月度董事會品質報告包",
        audience="董事會（Board）",
        cadence="每月 1 日 09:00",
        output_format="PDF Pack（本模組輸出分冊結構，不產生 PDF 檔）",
        aggregation="單月 × 全廠 × 全期",
        detail_level="高層摘要、月對月趨勢、風險登錄、決策建議",
        decision="要不要批預算換模具／擴編品保／調整客戶承諾",
    ),
}


@dataclass(frozen=True)
class ChainContext:
    """建鏈所需的外部脈絡（時間、廠別、歷史基準、政策開關）。"""

    plant_name: str
    as_of: datetime
    timezone_name: str
    daily_deliver_at: str
    weekly_deliver_at: str
    monthly_deliver_at: str
    history: dict[str, Any] = field(default_factory=dict)
    carry_forward: tuple[QualityAlert, ...] = ()
    enforce_no_suppression: bool = True
    banner_when_average_looks_fine: bool = True

    @property
    def stamp(self) -> str:
        """報告產生時間戳（含時區），四階共用。"""
        return self.as_of.isoformat(timespec="seconds")


@dataclass(frozen=True)
class TierReport:
    """單一份報告。`alerts` 是這一階看得見的全部警報（已聯集下階）。"""

    tier: ReportTier
    profile: TierProfile
    period_key: str
    period_label: str
    subject: str
    aggregate: dict[str, Any]
    alerts: tuple[QualityAlert, ...]
    body_markdown: str
    source_keys: tuple[str, ...]
    pdf_pack: tuple[dict[str, str], ...] = ()

    @property
    def alert_ids(self) -> set[str]:
        return {alert.alert_id for alert in self.alerts}

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier.value,
            "title": self.profile.title,
            "audience": self.profile.audience,
            "cadence": self.profile.cadence,
            "output_format": self.profile.output_format,
            "aggregation": self.profile.aggregation,
            "detail_level": self.profile.detail_level,
            "period_key": self.period_key,
            "period_label": self.period_label,
            "subject": self.subject,
            "aggregate": self.aggregate,
            "alert_counts": count_by_severity(self.alerts),
            "alerts": [alert.to_dict() for alert in self.alerts],
            "source_keys": list(self.source_keys),
            "body_markdown": self.body_markdown,
            "pdf_pack": [dict(section) for section in self.pdf_pack],
        }


@dataclass(frozen=True)
class ChainResult:
    """四階報告鏈的完整產出。"""

    reports: dict[ReportTier, tuple[TierReport, ...]]

    def tier(self, tier: ReportTier) -> tuple[TierReport, ...]:
        return self.reports.get(tier, ())

    @property
    def all_reports(self) -> tuple[TierReport, ...]:
        collected: list[TierReport] = []
        for tier in ReportTier:
            collected.extend(self.reports.get(tier, ()))
        return tuple(collected)

    def alert_ids(self, tier: ReportTier) -> set[str]:
        """該階所有報告的警報 id 聯集。"""
        ids: set[str] = set()
        for report in self.tier(tier):
            ids |= report.alert_ids
        return ids

    def to_dict(self) -> dict[str, Any]:
        return {
            tier.value: [report.to_dict() for report in self.tier(tier)] for tier in ReportTier
        }


# --------------------------------------------------------------------------
# 期間鍵
# --------------------------------------------------------------------------


def week_key(shift_date: str) -> tuple[str, str]:
    """把日期換成 ISO 週鍵與人看的週標籤。"""
    day = date.fromisoformat(shift_date)
    iso = day.isocalendar()
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    key = f"{iso[0]}-W{iso[1]:02d}"
    return key, f"{key}（{monday:%m/%d}–{sunday:%m/%d}）"


def month_key(shift_date: str) -> tuple[str, str]:
    """把日期換成月鍵與人看的月標籤。"""
    day = date.fromisoformat(shift_date)
    return f"{day:%Y-%m}", f"{day.year} 年 {day.month} 月"


# --------------------------------------------------------------------------
# 排版工具
# --------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """產出 Markdown 表格。沒有資料列時回一行明示「無資料」而不是空表。"""
    if not rows:
        return ["（本區段無資料）"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(str(cell) for cell in row) + " |" for row in rows)
    return lines


def _dash(value: Any) -> str:
    """None 一律顯示為「—」，絕不顯示 0——那會把「沒量到」講成「表現完美」。"""
    return "—" if value is None else str(value)


def _alert_rows(alerts: Sequence[QualityAlert]) -> list[list[str]]:
    """警報清單表格列。"""
    return [
        [
            alert.severity.label,
            alert.line_name,
            alert.shift_id or "（跨班）",
            alert.headline,
            "沿用" if alert.is_carry_forward else "本期",
        ]
        for alert in alerts
    ]


def _alert_section(alerts: Sequence[QualityAlert], heading: str) -> list[str]:
    """警報區段。永遠印出，即使一則都沒有也要明說「無」。"""
    lines = [f"### {heading}"]
    if not alerts:
        lines.append("本期無未結案品質警報。")
        return lines
    lines.extend(_table(["嚴重度", "產線", "班別", "說明", "來源"], _alert_rows(alerts)))
    lines.append("")
    lines.append("> 以上每一則都必須逐案結案。上層報告的平均值不構成結案依據。")
    return lines


def _banner(alerts: Sequence[QualityAlert], aggregate: dict[str, Any], enabled: bool) -> list[str]:
    """平均值看起來正常、但仍有警報未結案時的強制警示橫幅。

    這是本模組的核心保護：高階讀者最容易犯的錯，就是看到
    「全廠不良率 0.62%，遠低於上限」就翻頁。橫幅把警報推回他眼前。
    """
    if not enabled or not alerts:
        return []
    counts = count_by_severity(alerts)
    rate = aggregate.get("defect_rate_pct")
    return [
        f"> ⚠️ **{counts['critical']} 件重大 / {counts['major']} 件主要 / {counts['minor']} 件次要**"
        f"品質警報未結案。",
        f"> 本期彙總不良率 {_dash(rate)}%，即使落在容忍區間內，**也不代表上列警報已解除**。",
        "> 品質異常不因平均而消失：任何一階偵測到的超標，四階都看得見。",
        "",
    ]


def _header(profile: TierProfile, period_label: str, ctx: ChainContext) -> list[str]:
    """四階共用的報告抬頭。受眾與用途寫在最上方，避免拿錯版本。"""
    return [
        f"# {profile.title}｜{period_label}",
        "",
        f"- **廠別**：{ctx.plant_name}",
        f"- **收件對象**：{profile.audience}",
        f"- **排程**：{profile.cadence}（{ctx.timezone_name}）",
        f"- **輸出格式**：{profile.output_format}",
        f"- **聚合層級**：{profile.aggregation}",
        f"- **這份報告要回答的決策**：{profile.decision}",
        f"- **產生時間**：{ctx.stamp}",
        "",
    ]


# --------------------------------------------------------------------------
# 第 1 階：Shift End → Supervisor
# --------------------------------------------------------------------------


def _reading_rows(shift: ShiftAnalysis) -> list[list[str]]:
    """逐點量測表——只有班末報告會逐點列出，上層不會。"""
    rows = []
    for index, value in enumerate(shift.record.readings, start=1):
        offset = shift.chart.sigma_offset(value)
        flag = "❌ 超規" if value in shift.out_of_spec else ("⚠️ 超出 3σ" if abs(offset) > 3 else "")
        rows.append([str(index), f"{value}{shift.spec.unit}", f"{offset}σ", flag])
    return rows


def _shift_actions(shift: ShiftAnalysis) -> list[str]:
    """給領班的立即處置。沒有警報時也要明寫「照常交班」，不留空白讓人自己猜。"""
    if not shift.alerts:
        return ["- 本班無異常，照常交班。控制圖與不良率均在管制範圍內。"]
    actions = []
    for alert in shift.alerts:
        if alert.category == "spec":
            actions.append(f"- **立即隔離**本班產出並通知品管工程師：{alert.headline}")
        elif alert.severity is AlertSeverity.CRITICAL:
            actions.append(f"- **停線確認**後再續產：{alert.headline}")
        else:
            actions.append(f"- 交班時面對面告知下一班領班：{alert.headline}")
    return actions


def build_shift_report(shift: ShiftAnalysis, ctx: ChainContext) -> TierReport:
    """建立單一班別的班末報告（第 1 階）。"""
    profile = TIER_PROFILES[ReportTier.SHIFT_END]
    record, spec = shift.record, shift.spec
    label = f"{record.shift_date} {record.shift_code} 班｜{spec.line_name}"
    aggregate = aggregate_shifts([shift])

    body = _header(profile, label, ctx)
    body.append(f"領班：{record.supervisor}｜量測項目：{spec.metric_name}（{spec.unit}）")
    body.append("")
    body.extend(_shift_metrics_section(shift, spec))
    body.extend(_shift_chart_section(shift, spec))
    body.extend(_alert_section(shift.alerts, "本班品質警報"))
    body.append("")
    body.append("### 立即處置")
    body.extend(_shift_actions(shift))

    return TierReport(
        tier=ReportTier.SHIFT_END,
        profile=profile,
        period_key=record.shift_id,
        period_label=label,
        subject=_subject(profile, label, shift.alerts),
        aggregate=aggregate,
        alerts=tuple(dedupe_alerts(shift.alerts)),
        body_markdown="\n".join(body),
        source_keys=(record.shift_id,),
    )


def _shift_metrics_section(shift: ShiftAnalysis, spec: LineSpec) -> list[str]:
    """班末報告的本班數據區段。"""
    lines = ["### 本班數據", ""]
    lines.extend(
        _table(
            ["投入", "不良", "不良率", "不良率上限", "良率", "良率目標"],
            [[
                str(shift.record.units_produced),
                str(shift.record.units_defective),
                f"{_dash(shift.defect_rate_pct)}%",
                f"{spec.defect_rate_limit_pct}%",
                f"{_dash(shift.yield_pct)}%",
                f"{spec.target_yield_pct}%",
            ]],
        )
    )
    lines.append("")
    return lines


def _shift_chart_section(shift: ShiftAnalysis, spec: LineSpec) -> list[str]:
    """班末報告的控制圖與 Nelson 判定區段。"""
    chart = shift.chart
    lines = [
        "### 控制圖（UCL / Mean / LCL）",
        "",
        f"UCL {chart.ucl}{spec.unit}｜Mean {chart.mean}{spec.unit}｜LCL {chart.lcl}{spec.unit}"
        f"（規格界限 {spec.lsl}–{spec.usl}{spec.unit}）",
        "",
    ]
    lines.extend(_table(["#", "量測值", "距中心線", "判定"], _reading_rows(shift)))
    lines.append("")
    lines.append("### Nelson Rules 判定")
    lines.append("")
    lines.extend(
        _table(
            ["規則", "名稱", "觸發位置"],
            [[f"R{v.rule}", v.name, v.detail] for v in shift.violations],
        )
    )
    lines.append("")
    return lines


def _subject(profile: TierProfile, label: str, alerts: Sequence[QualityAlert]) -> str:
    """通知主旨：最嚴重的等級放最前面，手機通知列被截斷也讀得到。"""
    if not alerts:
        return f"{profile.title}｜{label}｜無異常"
    counts = count_by_severity(alerts)
    if counts["critical"]:
        return f"🔴 {profile.title}｜{label}｜{counts['critical']} 件重大警報"
    if counts["major"]:
        return f"🟠 {profile.title}｜{label}｜{counts['major']} 件主要警報"
    return f"🟡 {profile.title}｜{label}｜{counts['minor']} 件次要警報"


# --------------------------------------------------------------------------
# 往上聚合的共用邏輯
# --------------------------------------------------------------------------


def _escalate(
    lower_reports: Sequence[TierReport],
    extra: Iterable[QualityAlert] = (),
) -> tuple[QualityAlert, ...]:
    """唯一的往上傳遞路徑：聯集 + 去重 + 排序。

    這裡刻意沒有任何 filter / threshold / severity 參數——
    只要有辦法在這個函式簽名上加一個「只往上傳 critical」的旗標，
    早晚就會有人為了讓月報好看而打開它。
    """
    collected: list[QualityAlert] = []
    for report in lower_reports:
        collected.extend(report.alerts)
    collected.extend(extra)
    return tuple(dedupe_alerts(collected))


def assert_no_alert_dropped(
    lower: Sequence[TierReport],
    upper: Sequence[TierReport],
    lower_name: str,
    upper_name: str,
) -> None:
    """確認下階的每一則警報都出現在上階。缺一則就讓整條鏈失敗。"""
    lower_ids: set[str] = set()
    for report in lower:
        lower_ids |= report.alert_ids
    upper_ids: set[str] = set()
    for report in upper:
        upper_ids |= report.alert_ids

    missing = sorted(lower_ids - upper_ids)
    if missing:
        raise AlertSuppressionError(
            f"{upper_name} 漏掉了 {lower_name} 已偵測到的 {len(missing)} 則警報："
            f"{'、'.join(missing[:5])}"
            f"{' …' if len(missing) > 5 else ''}。"
            "品質警報只能聯集、不得過濾，請檢查聚合邏輯。"
        )


# --------------------------------------------------------------------------
# 第 2 階：Daily 06:00 → Ops Director
# --------------------------------------------------------------------------


def _shift_matrix_rows(shifts: Sequence[ShiftAnalysis]) -> list[list[str]]:
    """跨班對比矩陣：每一列一個班別。廠長看的是「哪一班出問題」。"""
    return [
        [
            s.record.shift_date,
            s.record.shift_code,
            s.spec.line_name,
            s.record.supervisor,
            str(s.record.units_produced),
            f"{_dash(s.defect_rate_pct)}%",
            f"{s.spec.defect_rate_limit_pct}%",
            "⚠️" if s.alerts else "",
        ]
        for s in shifts
    ]


def _forecast_rows(lines: Sequence[LineAnalysis]) -> list[list[str]]:
    """預測性趨勢表（ch07_p11：瑕疵產生前 3-5 個班次警告）。"""
    rows = []
    for line in lines:
        forecast = line.forecast
        rows.append([
            forecast.line_name,
            f"{_dash(forecast.latest_pct)}%",
            f"{_dash(forecast.slope_pct_per_shift)} pp/班",
            f"{forecast.limit_pct}%",
            _dash(forecast.shifts_to_breach),
            forecast.severity.label if forecast.severity else "—",
        ])
    return rows


def _outage_rows(plant: PlantAnalysis) -> list[list[str]]:
    """資料缺漏表。缺漏必須有自己的區段，不能只躺在警報清單裡。"""
    return [
        [o.line_name, o.source_id, o.last_seen_at or "—", o.reason] for o in plant.outages
    ]


def build_daily_report(
    period_key: str,
    shifts: Sequence[ShiftAnalysis],
    shift_reports: Sequence[TierReport],
    plant: PlantAnalysis,
    period_alerts: Sequence[QualityAlert],
    ctx: ChainContext,
) -> TierReport:
    """建立單日的 06:00 晨報（第 2 階）。"""
    profile = TIER_PROFILES[ReportTier.DAILY]
    label = f"{period_key}（{ctx.daily_deliver_at} 送達）"
    alerts = _escalate(shift_reports, period_alerts)
    aggregate = aggregate_shifts(shifts)

    body = _header(profile, label, ctx)
    body.extend(_banner(alerts, aggregate, ctx.banner_when_average_looks_fine))
    body.extend(_daily_summary_section(aggregate, plant))
    body.append("### 跨班對比（單日 × 全產線）")
    body.append("")
    body.extend(_table(
        ["日期", "班別", "產線", "領班", "投入", "不良率", "上限", "警報"],
        _shift_matrix_rows(shifts),
    ))
    body.append("")
    body.extend(_daily_forecast_section(plant))
    body.extend(_alert_section(alerts, "當日未結案品質警報（含跨班與資料缺漏）"))

    return TierReport(
        tier=ReportTier.DAILY,
        profile=profile,
        period_key=period_key,
        period_label=label,
        subject=_subject(profile, label, alerts),
        aggregate=aggregate,
        alerts=alerts,
        body_markdown="\n".join(body),
        source_keys=tuple(report.period_key for report in shift_reports),
    )


def _daily_summary_section(aggregate: dict[str, Any], plant: PlantAnalysis) -> list[str]:
    """當日全廠彙總。含「未涵蓋產線」一欄，避免缺漏被平均掩蓋。"""
    covered = aggregate.get("lines_covered") or []
    missing = [o.line_id for o in plant.outages]
    lines = ["### 當日彙總", ""]
    lines.extend(_table(
        ["班數", "投入", "不良", "不良率", "良率", "最差單班不良率", "涵蓋產線", "未涵蓋產線"],
        [[
            str(aggregate["shift_count"]),
            str(aggregate["units_produced"]),
            str(aggregate["units_defective"]),
            f"{_dash(aggregate['defect_rate_pct'])}%",
            f"{_dash(aggregate['yield_pct'])}%",
            f"{_dash(aggregate['worst_shift_defect_rate_pct'])}%",
            "、".join(covered) or "—",
            "、".join(missing) or "無",
        ]],
    ))
    lines.append("")
    return lines


def _daily_forecast_section(plant: PlantAnalysis) -> list[str]:
    """預測性警告 + 資料缺漏兩個區段。"""
    lines = ["### 預測性趨勢警告（瑕疵發生前 3–5 個班次）", ""]
    lines.extend(_table(
        ["產線", "最近不良率", "趨勢斜率", "上限", "預估幾班後撞線", "嚴重度"],
        _forecast_rows(plant.lines),
    ))
    lines.append("")
    lines.append("### 資料缺漏")
    lines.append("")
    lines.extend(_table(["產線", "來源", "最後回應", "原因"], _outage_rows(plant)))
    lines.append("")
    if plant.outages:
        lines.append("> 缺漏產線的產出**未納入**上方任何統計。全廠平均值不得視為已涵蓋此線。")
        lines.append("")
    return lines


# --------------------------------------------------------------------------
# 第 3 階：Weekly → Quality Manager
# --------------------------------------------------------------------------


def _shifts_in(
    plant: PlantAnalysis, dates: set[str], line_id: str | None = None
) -> list[ShiftAnalysis]:
    """挑出落在指定日期集合內的班別。

    週報與月報必須只聚合**自己期間內**的班別。若圖方便直接用 plant 的全部班別，
    跨月執行時 8 月的月報會把 9 月的數字算進去——這種錯誤在單月的測試資料上
    完全看不出來，要等到跨月那天才會爆，而那天沒有人會想到是這裡。
    """
    return [
        shift
        for line in plant.lines
        for shift in line.shifts
        if shift.record.shift_date in dates
        and (line_id is None or line.spec.line_id == line_id)
    ]


def _week_over_week_rows(
    plant: PlantAnalysis,
    alerts: Sequence[QualityAlert],
    history: dict[str, Any],
    dates: set[str],
) -> list[list[str]]:
    """週對週比較。上週沒有基準值就寫「—」，禁止用本週值回填。"""
    previous = ((history.get("previous_week") or {}).get("lines")) or {}
    rows = []
    for line in plant.lines:
        current = aggregate_shifts(_shifts_in(plant, dates, line.spec.line_id))
        rate = current["defect_rate_pct"]
        prior = (previous.get(line.spec.line_id) or {}).get("defect_rate_pct")
        rows.append([
            line.spec.line_name,
            f"{_dash(rate)}%",
            f"{_dash(prior)}%",
            _delta(rate, prior),
            str(sum(1 for a in alerts if a.line_id == line.spec.line_id)),
        ])
    for outage in plant.outages:
        prior = (previous.get(outage.line_id) or {}).get("defect_rate_pct")
        rows.append([outage.line_name, "無資料", f"{_dash(prior)}%", "—", "1"])
    return rows


def _delta(current: str | None, prior: str | None) -> str:
    """算週對週差額。任一邊缺值一律回「—」，不推估。"""
    if current is None or prior is None:
        return "—"
    diff = quantize_pct(to_decimal(current, "週對週", "current") - to_decimal(prior, "週對週", "prior"))
    return f"{'+' if diff > 0 else ''}{diff} pp"


def _recurring_rows(alerts: Sequence[QualityAlert]) -> list[list[str]]:
    """重複性問題：同一產線同一類警報出現 2 次以上，代表是體質不是意外。"""
    counter: dict[tuple[str, str, str], int] = {}
    for alert in alerts:
        counter[(alert.line_id, alert.line_name, alert.category)] = (
            counter.get((alert.line_id, alert.line_name, alert.category), 0) + 1
        )
    return [
        [name, category, str(count), "建議開立矯正措施單（CAR）"]
        for (_, name, category), count in sorted(counter.items(), key=lambda kv: -kv[1])
        if count >= 2
    ]


def build_weekly_report(
    period_key: str,
    period_label: str,
    daily_reports: Sequence[TierReport],
    plant: PlantAnalysis,
    ctx: ChainContext,
) -> TierReport:
    """建立單週回顧（第 3 階）。"""
    profile = TIER_PROFILES[ReportTier.WEEKLY]
    alerts = _escalate(daily_reports)
    dates = {report.period_key for report in daily_reports}
    aggregate = aggregate_shifts(_shifts_in(plant, dates))
    label = f"{period_label}（{ctx.weekly_deliver_at} 送達）"

    body = _header(profile, label, ctx)
    body.extend(_banner(alerts, aggregate, ctx.banner_when_average_looks_fine))
    body.append("### 週對週比較（各產線不良率）")
    body.append("")
    body.extend(_table(
        ["產線", "本週", "上週", "變化", "本週警報數"],
        _week_over_week_rows(plant, alerts, ctx.history, dates),
    ))
    body.append("")
    body.extend(_weekly_recurring_section(alerts))
    body.extend(_alert_section(alerts, "本週未結案品質警報"))

    return TierReport(
        tier=ReportTier.WEEKLY,
        profile=profile,
        period_key=period_key,
        period_label=label,
        subject=_subject(profile, period_label, alerts),
        aggregate=aggregate,
        alerts=alerts,
        body_markdown="\n".join(body),
        source_keys=tuple(report.period_key for report in daily_reports),
    )


def _weekly_recurring_section(alerts: Sequence[QualityAlert]) -> list[str]:
    """重複性問題區段。"""
    rows = _recurring_rows(alerts)
    lines = ["### 重複性問題（同一產線同類警報 ≥ 2 次）", ""]
    lines.extend(_table(["產線", "警報類別", "次數", "建議"], rows))
    lines.append("")
    if not rows:
        lines.append("> 本週未出現重複性問題。單次事件仍須逐案結案，見下方警報清單。")
        lines.append("")
    return lines


# --------------------------------------------------------------------------
# 第 4 階：Monthly → Board（PDF Pack 結構）
# --------------------------------------------------------------------------


def _board_summary(
    aggregate: dict[str, Any], alerts: Sequence[QualityAlert], plant: PlantAnalysis
) -> list[str]:
    """董事會高層摘要：五行以內，每行都是一個可以直接下決定的事實。"""
    counts = count_by_severity(alerts)
    lines = [
        f"- 全廠產出 {aggregate['units_produced']} 件，不良 {aggregate['units_defective']} 件，"
        f"不良率 {_dash(aggregate['defect_rate_pct'])}%。",
        f"- 未結案品質警報 {len(alerts)} 件"
        f"（重大 {counts['critical']}／主要 {counts['major']}／次要 {counts['minor']}）。",
    ]
    critical = [a for a in alerts if a.severity is AlertSeverity.CRITICAL]
    if critical:
        lines.append(f"- 需董事會知悉的重大項目：{critical[0].headline}（共 {len(critical)} 件）。")
    if plant.outages:
        names = "、".join(o.line_name for o in plant.outages)
        lines.append(f"- {names} 本期無資料，其產出未納入上述統計，數字不代表全廠實況。")
    warned = [line for line in plant.lines if line.forecast.severity is not None]
    if warned:
        first = warned[0].forecast
        lines.append(
            f"- 預測性警告：{first.line_name} 預計 {first.shifts_to_breach} 個班次後不良率撞上限，"
            "尚在瑕疵發生之前，處置成本最低。"
        )
    return lines


def _month_over_month_rows(aggregate: dict[str, Any], history: dict[str, Any]) -> list[list[str]]:
    """月對月比較。上月基準來自 history.json，缺項留「—」。"""
    prior = ((history.get("previous_month") or {}).get("plant")) or {}
    return [[
        f"{_dash(aggregate['defect_rate_pct'])}%",
        f"{_dash(prior.get('defect_rate_pct'))}%",
        _delta(aggregate["defect_rate_pct"], prior.get("defect_rate_pct")),
        _dash(prior.get("critical_alerts")),
        _dash(prior.get("customer_complaints")),
    ]]


def _board_decisions(alerts: Sequence[QualityAlert], plant: PlantAnalysis) -> list[str]:
    """決策建議：每一條都綁定一個具體警報，不寫泛泛的「持續精進」。"""
    decisions = []
    for alert in alerts:
        if alert.severity is AlertSeverity.CRITICAL and alert.category == "data_outage":
            decisions.append(f"- 核撥 MES 邊緣節點維護預算：{alert.line_name} 資料鏈中斷中。")
        elif alert.severity is AlertSeverity.CRITICAL:
            decisions.append(f"- 指派品保專案負責人並設定結案日期：{alert.headline}")
    for line in plant.lines:
        if line.forecast.severity is not None:
            decisions.append(
                f"- 於 {line.forecast.shifts_to_breach} 個班次內完成 {line.spec.line_name} 製程調機"
                "或模具檢修，趕在瑕疵實際產生之前。"
            )
    return decisions or ["- 本期無需董事會層級決策。既有矯正措施由品質經理層級追蹤即可。"]


def build_monthly_report(
    period_key: str,
    period_label: str,
    weekly_reports: Sequence[TierReport],
    plant: PlantAnalysis,
    ctx: ChainContext,
) -> TierReport:
    """建立月度董事會報告包（第 4 階）。"""
    profile = TIER_PROFILES[ReportTier.MONTHLY]
    alerts = _escalate(weekly_reports)
    dates = {day for report in weekly_reports for day in report.source_keys}
    aggregate = aggregate_shifts(_shifts_in(plant, dates))
    label = f"{period_label}（{ctx.monthly_deliver_at} 送達）"

    pack = _build_pdf_pack(aggregate, alerts, plant, ctx)
    body = _header(profile, label, ctx)
    body.extend(_banner(alerts, aggregate, ctx.banner_when_average_looks_fine))
    body.append("### 報告包分冊（PDF Pack 結構）")
    body.append("")
    body.extend(_table(
        ["冊次", "名稱", "頁面用途"],
        [[str(i), s["name"], s["purpose"]] for i, s in enumerate(pack, start=1)],
    ))
    body.append("")
    for section in pack:
        body.append(f"### {section['name']}")
        body.append("")
        body.append(section["body_markdown"])
        body.append("")

    return TierReport(
        tier=ReportTier.MONTHLY,
        profile=profile,
        period_key=period_key,
        period_label=label,
        subject=_subject(profile, period_label, alerts),
        aggregate=aggregate,
        alerts=alerts,
        body_markdown="\n".join(body),
        source_keys=tuple(report.period_key for report in weekly_reports),
        pdf_pack=tuple(pack),
    )


def _build_pdf_pack(
    aggregate: dict[str, Any],
    alerts: Sequence[QualityAlert],
    plant: PlantAnalysis,
    ctx: ChainContext,
) -> list[dict[str, str]]:
    """組出 PDF Pack 的四個分冊。

    **本模組不產生 PDF 檔**：依交付限制不引入任何 PDF 函式庫，
    這裡只輸出「分冊 + 版面內容」的結構，交由下游排版服務渲染。
    """
    mom = _table(
        ["本月不良率", "上月不良率", "變化", "上月重大警報", "上月客訴件數"],
        _month_over_month_rows(aggregate, ctx.history),
    )
    risk_rows = _alert_rows([a for a in alerts if a.severity is AlertSeverity.CRITICAL])
    return [
        {
            "name": "第 1 冊｜高層摘要",
            "purpose": "董事會開場 3 分鐘閱讀",
            "body_markdown": "\n".join(_board_summary(aggregate, alerts, plant)),
        },
        {
            "name": "第 2 冊｜月對月趨勢",
            "purpose": "確認品質走勢是改善還是惡化",
            "body_markdown": "\n".join(mom),
        },
        {
            "name": "第 3 冊｜風險登錄（重大警報）",
            "purpose": "逐案追蹤，含資料缺漏",
            "body_markdown": "\n".join(
                _table(["嚴重度", "產線", "班別", "說明", "來源"], risk_rows)
            ),
        },
        {
            "name": "第 4 冊｜決策建議",
            "purpose": "需要董事會核可的事項",
            "body_markdown": "\n".join(_board_decisions(alerts, plant)),
        },
    ]


# --------------------------------------------------------------------------
# 建鏈主流程
# --------------------------------------------------------------------------


def _period_level_alerts(plant: PlantAnalysis, ctx: ChainContext) -> list[QualityAlert]:
    """不屬於任何單一班別的警報：資料缺漏、跨班趨勢、預測性警告、上期沿用。

    這些從第 2 階（Daily）進入報告鏈——班長手上沒有跨班視野，
    但廠長以上每一階都必須看見。
    """
    collected: list[QualityAlert] = list(plant.outage_alerts)
    for line in plant.lines:
        collected.extend(line.trend_alerts)
        collected.extend(line.forecast.alerts)
    collected.extend(ctx.carry_forward)
    return dedupe_alerts(collected)


def build_chain(plant: PlantAnalysis, ctx: ChainContext) -> ChainResult:
    """建立完整的四階報告鏈，並在每一階之間強制檢查沒有警報被吃掉。"""
    shift_reports = [build_shift_report(shift, ctx) for shift in plant.shifts]
    period_alerts = _period_level_alerts(plant, ctx)
    daily_reports = _build_daily_tier(plant, shift_reports, period_alerts, ctx)
    weekly_reports = _build_weekly_tier(plant, daily_reports, ctx)
    monthly_reports = _build_monthly_tier(plant, weekly_reports, ctx)

    if ctx.enforce_no_suppression:
        assert_no_alert_dropped(shift_reports, daily_reports, "班末報告", "每日晨報")
        assert_no_alert_dropped(daily_reports, weekly_reports, "每日晨報", "週回顧")
        assert_no_alert_dropped(weekly_reports, monthly_reports, "週回顧", "月度董事會報告包")

    return ChainResult(
        reports={
            ReportTier.SHIFT_END: tuple(shift_reports),
            ReportTier.DAILY: tuple(daily_reports),
            ReportTier.WEEKLY: tuple(weekly_reports),
            ReportTier.MONTHLY: tuple(monthly_reports),
        }
    )


def _build_daily_tier(
    plant: PlantAnalysis,
    shift_reports: Sequence[TierReport],
    period_alerts: Sequence[QualityAlert],
    ctx: ChainContext,
) -> list[TierReport]:
    """依班別日期分組建立每日晨報。

    期間級警報（資料缺漏／跨班趨勢／預測／沿用）**掛在每一天**：
    MES 中斷不是只有某一天的廠長需要知道，是每天早上都要再看到一次。
    """
    by_date: dict[str, list[ShiftAnalysis]] = {}
    for shift in plant.shifts:
        by_date.setdefault(shift.record.shift_date, []).append(shift)

    reports = []
    for day in sorted(by_date):
        reports.append(
            build_daily_report(
                day,
                by_date[day],
                _match_reports(shift_reports, by_date[day]),
                plant,
                period_alerts,
                ctx,
            )
        )
    if not reports:
        reports.append(build_daily_report(
            ctx.as_of.strftime("%Y-%m-%d"), [], [], plant, period_alerts, ctx
        ))
    return reports


def _shift_ids(shifts: Sequence[ShiftAnalysis]) -> tuple[str, ...]:
    return tuple(shift.record.shift_id for shift in shifts)


def _match_reports(
    shift_reports: Sequence[TierReport], shifts: Sequence[ShiftAnalysis]
) -> list[TierReport]:
    """挑出對應這些班別的班末報告。"""
    wanted = set(_shift_ids(shifts))
    return [report for report in shift_reports if report.period_key in wanted]


def _build_weekly_tier(
    plant: PlantAnalysis, daily_reports: Sequence[TierReport], ctx: ChainContext
) -> list[TierReport]:
    """依 ISO 週分組建立週回顧。"""
    grouped: dict[str, tuple[str, list[TierReport]]] = {}
    for report in daily_reports:
        key, label = week_key(report.period_key)
        grouped.setdefault(key, (label, []))[1].append(report)
    return [
        build_weekly_report(key, label, reports, plant, ctx)
        for key, (label, reports) in sorted(grouped.items())
    ]


def _build_monthly_tier(
    plant: PlantAnalysis, weekly_reports: Sequence[TierReport], ctx: ChainContext
) -> list[TierReport]:
    """依月份分組建立董事會報告包。"""
    grouped: dict[str, tuple[str, list[TierReport]]] = {}
    for report in weekly_reports:
        source = report.source_keys[0] if report.source_keys else ctx.as_of.strftime("%Y-%m-%d")
        key, label = month_key(source)
        grouped.setdefault(key, (label, []))[1].append(report)
    return [
        build_monthly_report(key, label, reports, plant, ctx)
        for key, (label, reports) in sorted(grouped.items())
    ]
