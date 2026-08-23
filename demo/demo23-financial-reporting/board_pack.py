"""董事會報告四件套的組裝與排版（apxG_p08 / apxG_p09）。

四件套（逐字對應簡報的 `BOARD_PACK_TEMPLATE`）：

1. `{{REPORTING_PERIOD}} EXECUTIVE SUMMARY`：3 條關鍵財務標題 + 變異數分析
2. `P&L VARIANCE TABLE`：Actual｜Budget｜Variance $｜Variance %｜Prior Year
3. `CASHFLOW SUMMARY`：營業 / 投資 / 融資現金流、期初期末餘額，
   含 `LIQUIDITY ALERT: CLOSING CASH < 60 DAYS`
4. `12-MONTH ROLLING FORECAST`：由 `forecaster.py` 產生後排入

本檔的三個不可協商行為：

- **幣別不混加**：聚合前逐一比對資料源幣別，不同幣別的資料源會被剔除並列為失敗，
  絕不換算、絕不相加（匯率是財務政策決定，不是報表程式可以自行假設的東西）。
- **變異數 > 5%（`material_pct`）標記為重大**，強制要求 AI 敘述解釋「時間差
  (Timing Difference)」何時逆轉——這是 apxG_p09 的硬性規則。
- **資料不完整的警告比一般報表醒目得多**：demo09 的每日銷售報表可以標一行
  「⚠️ 部分資料」照常發出；財務報表不行。殘缺的財務數字會被寫進董事會議事錄、
  被拿去做投資決策、被引用到對外揭露。因此本模組的橫幅是整段封鎖式警告，
  且會**自動作廢既有的財務總監核准**（見 `main.evaluate_approval`）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from sources import (
    CashflowFacts,
    PipelineFacts,
    PnLLine,
    ReadOnlyViolation,
    SourceError,
    SourceFacts,
    assert_read_only_scope,
    quantize_money,
    quantize_pct,
)

HUNDRED = Decimal("100")
DAYS_PER_MONTH = Decimal("30")

#: 損益表合計行的代碼（也是 `BoardPack.totals` 的 key）。
TOTAL_REVENUE = "TOTAL_REVENUE"
TOTAL_COGS = "TOTAL_COGS"
GROSS_PROFIT = "GROSS_PROFIT"
TOTAL_OPEX = "TOTAL_OPEX"
NET_PROFIT = "NET_PROFIT"


class DiagnosticsLike(Protocol):
    """只用到 `Diagnostics` 的 amber()，用 Protocol 讓測試能塞假物件。"""

    def amber(self, symptom: str, fix: str) -> None: ...


@dataclass(frozen=True)
class SourceFailure:
    """單一資料源失敗的紀錄，會出現在封鎖式警告與稽核事件中。"""

    source_id: str
    display_name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        """轉成 JSON-safe 結構。"""
        return {
            "source_id": self.source_id,
            "display_name": self.display_name,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class VarianceRow:
    """P&L 變異數表的一行（科目行或合計行共用）。"""

    code: str
    label: str
    category: str
    actual: Decimal
    budget: Decimal | None
    prior_year: Decimal | None
    variance_amount: Decimal | None
    variance_pct: Decimal | None
    is_material: bool

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {
            "code": self.code,
            "label": self.label,
            "category": self.category,
            "actual": str(self.actual),
            "budget": _opt_str(self.budget),
            "prior_year": _opt_str(self.prior_year),
            "variance_amount": _opt_str(self.variance_amount),
            "variance_pct": _opt_str(self.variance_pct),
            "is_material": self.is_material,
        }


@dataclass(frozen=True)
class CashflowSummary:
    """現金流量摘要與流動性警報。"""

    opening_balance: Decimal
    operating: Decimal
    investing: Decimal
    financing: Decimal
    closing_balance: Decimal
    monthly_operating_outflow: Decimal
    days_of_cash: Decimal | None
    min_days_cash: int
    is_liquidity_alert: bool

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構。"""
        return {
            "opening_balance": str(self.opening_balance),
            "operating": str(self.operating),
            "investing": str(self.investing),
            "financing": str(self.financing),
            "closing_balance": str(self.closing_balance),
            "monthly_operating_outflow": str(self.monthly_operating_outflow),
            "days_of_cash": _opt_str(self.days_of_cash),
            "min_days_cash": self.min_days_cash,
            "is_liquidity_alert": self.is_liquidity_alert,
        }


@dataclass(frozen=True)
class BoardPack:
    """一份完整的董事會財務包（純資料，不含排版與核准狀態）。"""

    period: str
    currency: str
    lines: tuple[VarianceRow, ...]
    totals: dict[str, VarianceRow]
    cashflow: CashflowSummary | None
    pipeline: PipelineFacts | None
    failures: tuple[SourceFailure, ...]
    material_pct: Decimal
    source_highlights: dict[str, dict[str, str]]

    @property
    def is_partial(self) -> bool:
        """只要有任一資料源失敗，整份財務包即為不完整。"""
        return bool(self.failures)

    @property
    def material_lines(self) -> tuple[VarianceRow, ...]:
        """所有重大變異數科目行（不含合計行），供 AI 敘述逐條解釋。"""
        return tuple(row for row in self.lines if row.is_material)

    def alerts(self) -> list[str]:
        """所有需要在報告最上方點名的警示。"""
        items: list[str] = []
        if self.is_partial:
            names = "、".join(f.display_name for f in self.failures)
            items.append(f"財務資料不完整：{names} 取數失敗")
        if self.cashflow is not None and self.cashflow.is_liquidity_alert:
            items.append(
                f"LIQUIDITY ALERT：期末現金僅可支應 {self.cashflow.days_of_cash} 天"
                f"（門檻 {self.cashflow.min_days_cash} 天）"
            )
        if self.material_lines:
            items.append(
                f"重大變異數 {len(self.material_lines)} 項（門檻 ±{self.material_pct}%），"
                "需說明時間差逆轉時間點"
            )
        return items

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（也是核准指紋的計算基礎）。"""
        return {
            "period": self.period,
            "currency": self.currency,
            "is_partial": self.is_partial,
            "material_pct": str(self.material_pct),
            "lines": [row.to_dict() for row in self.lines],
            "totals": {key: row.to_dict() for key, row in self.totals.items()},
            "cashflow": self.cashflow.to_dict() if self.cashflow else None,
            "pipeline": _pipeline_dict(self.pipeline),
            "failed_sources": [item.to_dict() for item in self.failures],
            "source_highlights": self.source_highlights,
        }

    def to_json(self) -> str:
        """給 LLM 與指紋計算用的穩定序列化（sort_keys 確保同內容同指紋）。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)


# --------------------------------------------------------------------------
# 取數
# --------------------------------------------------------------------------


def resolve_source_path(entry: dict, module_dir: Path, is_mock: bool) -> Path:
    """決定資料源要讀哪個檔：mock 讀 `mock_file`，live 優先讀 `live_file`。"""
    key = "mock_file" if is_mock else "live_file"
    raw = str(entry.get(key) or entry.get("mock_file") or "").strip()
    if not raw:
        raise SourceError(f"資料源 {entry.get('id')!r} 未設定 {key}")
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (module_dir / path)


def collect(
    entries: Iterable[dict],
    module_dir: Path,
    fetchers: dict[str, Callable[[Path], SourceFacts]],
    diagnostics: DiagnosticsLike,
    is_mock: bool = True,
) -> tuple[list[SourceFacts], list[SourceFailure]]:
    """依 config 逐一取數。

    唯讀檢查在**取數之前**執行，而且失敗時不降級：`ReadOnlyViolation` 會往上拋，
    由 `main` 升級為紅色警報並中止。取數本身失敗才走「標示為失敗資料源」的降級路徑。
    """
    facts: list[SourceFacts] = []
    failures: list[SourceFailure] = []

    for entry in entries:
        source_id = str(entry.get("id", "")).strip()
        display_name = str(entry.get("display_name") or source_id or "未命名資料源")
        assert_read_only_scope(source_id or "未命名資料源", entry.get("scope"))

        fetcher = fetchers.get(source_id)
        if fetcher is None:
            failures.append(SourceFailure(source_id, display_name, "設定檔指定了未註冊的資料源"))
            diagnostics.amber(
                f"未註冊的資料源 {source_id!r}", "確認 config.yaml 的 sources[].id 與 sources/ 套件一致"
            )
            continue

        try:
            facts.append(fetcher(resolve_source_path(entry, module_dir, is_mock)))
        except SourceError as exc:
            failures.append(SourceFailure(source_id, display_name, str(exc)))
            diagnostics.amber(
                f"{display_name} 取數失敗：{exc}",
                "修復資料源後重跑；在此之前本期報表一律維持草稿且不得對董事會發布",
            )
    return facts, failures


def enforce_single_currency(
    facts: Iterable[SourceFacts],
    reporting_currency: str,
    diagnostics: DiagnosticsLike,
) -> tuple[list[SourceFacts], list[SourceFailure]]:
    """剔除幣別不符的資料源。不換算、不相加——匯率假設不是報表程式的權責。"""
    kept: list[SourceFacts] = []
    rejected: list[SourceFailure] = []
    target = reporting_currency.strip().upper()

    for fact in facts:
        currency = (fact.currency or "").strip().upper()
        if currency and currency != target:
            reason = f"幣別不符（{currency} ≠ 報表幣別 {target}），拒絕混加"
            rejected.append(SourceFailure(fact.source_id, fact.display_name, reason))
            diagnostics.amber(
                f"{fact.display_name} {reason}",
                "改用同幣別帳套，或由財務政策決定匯率後另建多幣別版本",
            )
            continue
        kept.append(fact)
    return kept, rejected


# --------------------------------------------------------------------------
# 變異數
# --------------------------------------------------------------------------


def build_variance_row(
    code: str,
    label: str,
    category: str,
    actual: Decimal,
    budget: Decimal | None,
    prior_year: Decimal | None,
    material_pct: Decimal,
) -> VarianceRow:
    """計算單行變異數。沒有預算時不假裝預算是 0（那會產生天文數字的百分比）。"""
    amount = None if budget is None else quantize_money(actual - budget)
    if budget is None or budget == 0:
        pct = None
        # 無預算但有實際發生數：仍屬需要說明的異常（預算漏編也是一種發現）。
        is_material = budget is not None and actual != 0
    else:
        pct = quantize_pct(amount / abs(budget) * HUNDRED)
        is_material = abs(pct) > material_pct

    return VarianceRow(
        code=code,
        label=label,
        category=category,
        actual=quantize_money(actual),
        budget=budget,
        prior_year=prior_year,
        variance_amount=amount,
        variance_pct=pct,
        is_material=is_material,
    )


def _sum_rows(rows: Iterable[VarianceRow], attribute: str) -> Decimal | None:
    """合計某欄位；只要有一行缺值就回 None（不可用部分預算充當全部預算）。"""
    values = [getattr(row, attribute) for row in rows]
    if any(value is None for value in values):
        return None
    total = sum(values, Decimal("0"))
    return quantize_money(total)


def _category_total(
    rows: tuple[VarianceRow, ...],
    category: str,
    code: str,
    label: str,
    material_pct: Decimal,
) -> VarianceRow:
    """把某分類的科目行加總成一行合計。"""
    members = tuple(row for row in rows if row.category == category)
    return build_variance_row(
        code=code,
        label=label,
        category=category,
        actual=_sum_rows(members, "actual") or Decimal("0.00"),
        budget=_sum_rows(members, "budget"),
        prior_year=_sum_rows(members, "prior_year"),
        material_pct=material_pct,
    )


def _derived_total(
    code: str,
    label: str,
    positive: VarianceRow,
    negative: VarianceRow,
    material_pct: Decimal,
) -> VarianceRow:
    """由兩行合計相減得出的行（毛利 = 營收 - 銷貨成本；淨利 = 毛利 - 營業費用）。"""
    return build_variance_row(
        code=code,
        label=label,
        category="derived",
        actual=positive.actual - negative.actual,
        budget=_subtract(positive.budget, negative.budget),
        prior_year=_subtract(positive.prior_year, negative.prior_year),
        material_pct=material_pct,
    )


def _subtract(left: Decimal | None, right: Decimal | None) -> Decimal | None:
    """兩個可選金額相減；任一缺值即整體缺值。"""
    if left is None or right is None:
        return None
    return quantize_money(left - right)


def build_totals(
    rows: tuple[VarianceRow, ...], material_pct: Decimal
) -> dict[str, VarianceRow]:
    """組出五行合計：營收 / 銷貨成本 / 毛利 / 營業費用 / 淨利。"""
    revenue = _category_total(rows, "revenue", TOTAL_REVENUE, "營收合計", material_pct)
    cogs = _category_total(rows, "cogs", TOTAL_COGS, "銷貨成本合計", material_pct)
    opex = _category_total(rows, "opex", TOTAL_OPEX, "營業費用合計", material_pct)
    gross = _derived_total(GROSS_PROFIT, "毛利", revenue, cogs, material_pct)
    net = _derived_total(NET_PROFIT, "淨利", gross, opex, material_pct)
    return {
        TOTAL_REVENUE: revenue,
        TOTAL_COGS: cogs,
        GROSS_PROFIT: gross,
        TOTAL_OPEX: opex,
        NET_PROFIT: net,
    }


# --------------------------------------------------------------------------
# 現金流
# --------------------------------------------------------------------------


def build_cashflow_summary(facts: CashflowFacts | None, min_days_cash: int) -> CashflowSummary | None:
    """算出現金可支應天數與流動性警報（apxG_p08：CLOSING CASH < 60 DAYS）。"""
    if facts is None:
        return None

    daily_outflow = facts.monthly_operating_outflow / DAYS_PER_MONTH
    days = quantize_pct(facts.closing_balance / daily_outflow) if daily_outflow > 0 else None
    return CashflowSummary(
        opening_balance=facts.opening_balance,
        operating=facts.operating,
        investing=facts.investing,
        financing=facts.financing,
        closing_balance=facts.closing_balance,
        monthly_operating_outflow=facts.monthly_operating_outflow,
        days_of_cash=days,
        min_days_cash=min_days_cash,
        is_liquidity_alert=days is not None and days < Decimal(min_days_cash),
    )


# --------------------------------------------------------------------------
# 組裝
# --------------------------------------------------------------------------


def _merge_budget(facts: Iterable[SourceFacts]) -> dict[str, Decimal]:
    """合併所有資料源的預算對照表。"""
    budget: dict[str, Decimal] = {}
    for fact in facts:
        budget.update(fact.budget_by_code)
    return budget


def _merge_lines(facts: Iterable[SourceFacts]) -> list[PnLLine]:
    """合併所有資料源的損益科目行，依代碼排序讓報表每期版面一致。"""
    lines: list[PnLLine] = []
    for fact in facts:
        lines.extend(fact.pnl_lines)
    return sorted(lines, key=lambda line: line.code)


def _first(facts: Iterable[SourceFacts], attribute: str) -> Any:
    """取第一個提供該區塊的資料源內容（現金流 / 管道各只會有一個來源）。"""
    for fact in facts:
        value = getattr(fact, attribute)
        if value is not None:
            return value
    return None


def build_board_pack(
    facts: Iterable[SourceFacts],
    failures: Iterable[SourceFailure],
    period: str,
    currency: str,
    material_pct: Decimal,
    min_days_cash: int,
) -> BoardPack:
    """把各資料源的事實組成董事會財務包。"""
    kept = list(facts)
    budget = _merge_budget(kept)
    rows = tuple(
        build_variance_row(
            code=line.code,
            label=line.label,
            category=line.category,
            actual=line.actual,
            budget=budget.get(line.code),
            prior_year=line.prior_year,
            material_pct=material_pct,
        )
        for line in _merge_lines(kept)
    )

    return BoardPack(
        period=period,
        currency=currency.strip().upper(),
        lines=rows,
        totals=build_totals(rows, material_pct),
        cashflow=build_cashflow_summary(_first(kept, "cashflow"), min_days_cash),
        pipeline=_first(kept, "pipeline"),
        failures=tuple(failures),
        material_pct=material_pct,
        source_highlights={fact.source_id: dict(fact.highlights) for fact in kept},
    )


# --------------------------------------------------------------------------
# 排版
# --------------------------------------------------------------------------


def _opt_str(value: Decimal | None) -> str | None:
    """Decimal 轉字串；None 保持 None（JSON 的 null 才能表達「沒有這個數字」）。"""
    return None if value is None else str(value)


def _pipeline_dict(pipeline: PipelineFacts | None) -> dict[str, Any] | None:
    """管道事實轉 JSON-safe 結構。"""
    if pipeline is None:
        return None
    return {
        "open_pipeline_value": str(pipeline.open_pipeline_value),
        "weighted_pipeline_value": str(pipeline.weighted_pipeline_value),
        "monthly_recurring_revenue": str(pipeline.monthly_recurring_revenue),
        "invoice_count": pipeline.invoice_count,
        "overdue_receivables": str(pipeline.overdue_receivables),
    }


def money(value: Decimal | None, currency: str) -> str:
    """報表用金額格式。None 一律顯示「—」，不顯示 0。"""
    return "—" if value is None else f"{currency} {value:,.2f}"


def pct(value: Decimal | None) -> str:
    """報表用百分比格式。"""
    return "—" if value is None else f"{value}%"


def partial_banner(failures: tuple[SourceFailure, ...]) -> str:
    """財務資料不完整的封鎖式警告。

    刻意做得比 demo09 的一行「⚠️ 部分資料」醒目數倍：不完整的財務數字比沒有數字
    更危險，讀者必須在看到任何一個金額之前就知道這份數字不能用來做決策。
    """
    if not failures:
        return ""
    bar = "■" * 62
    lines = [bar, "⛔ 財務資料不完整 — 本報表不得作為董事會決議或對外揭露依據 ⛔", bar]
    for item in failures:
        lines.append(f"  ⛔ 缺少資料源：{item.display_name}｜原因：{item.reason}")
    lines.extend(
        [
            "  ⛔ 以下所有金額、變異數與滾動預測皆建立在殘缺基礎上。",
            "  ⛔ 既有的財務總監核准已自動作廢，補齊資料後必須重新審核。",
            bar,
        ]
    )
    return "\n".join(lines)


def render_variance_table(pack: BoardPack) -> list[str]:
    """P&L VARIANCE TABLE：Actual｜Budget｜Variance $｜Variance %｜Prior Year。"""
    lines = [f"【2. P&L VARIANCE TABLE — {pack.period}】", "科目｜Actual｜Budget｜Variance $｜Variance %｜Prior Year"]
    for row in pack.lines:
        flag = "  ⚑重大" if row.is_material else ""
        lines.append(
            f"  {row.code} {row.label}｜{money(row.actual, pack.currency)}"
            f"｜{money(row.budget, pack.currency)}｜{money(row.variance_amount, pack.currency)}"
            f"｜{pct(row.variance_pct)}｜{money(row.prior_year, pack.currency)}{flag}"
        )
    lines.append("  " + "─" * 60)
    for key in (TOTAL_REVENUE, TOTAL_COGS, GROSS_PROFIT, TOTAL_OPEX, NET_PROFIT):
        row = pack.totals[key]
        lines.append(
            f"  {row.label}｜{money(row.actual, pack.currency)}｜{money(row.budget, pack.currency)}"
            f"｜{money(row.variance_amount, pack.currency)}｜{pct(row.variance_pct)}"
            f"｜{money(row.prior_year, pack.currency)}"
        )
    return lines


def render_cashflow(pack: BoardPack) -> list[str]:
    """CASHFLOW SUMMARY（含流動性警報）。"""
    lines = ["【3. CASHFLOW SUMMARY】"]
    flow = pack.cashflow
    if flow is None:
        lines.append("  ⛔ 無現金流資料（資料源取數失敗），本期無法評估流動性")
        return lines

    lines.extend(
        [
            f"  期初現金餘額｜{money(flow.opening_balance, pack.currency)}",
            f"  營業活動現金流｜{money(flow.operating, pack.currency)}",
            f"  投資活動現金流｜{money(flow.investing, pack.currency)}",
            f"  融資活動現金流｜{money(flow.financing, pack.currency)}",
            f"  期末現金餘額｜{money(flow.closing_balance, pack.currency)}",
            f"  月營業現金支出｜{money(flow.monthly_operating_outflow, pack.currency)}",
            f"  現金可支應天數｜{flow.days_of_cash if flow.days_of_cash is not None else '—'} 天",
        ]
    )
    if flow.is_liquidity_alert:
        lines.append(
            f"  🚨 LIQUIDITY ALERT: CLOSING CASH < {flow.min_days_cash} DAYS"
            f"（實際 {flow.days_of_cash} 天）— 需列為董事會第一順位討論案"
        )
    return lines


def render_board_pack(
    pack: BoardPack,
    status_banner: str,
    schedule_lines: list[str],
    executive_summary: str,
    variance_narrative: str,
    forecast_text: str,
) -> str:
    """把四件套組成一份可直接發送的純文字董事會財務報告。"""
    lines = [f"📑 董事會財務報告｜{pack.period}｜{pack.currency}"]

    banner = partial_banner(pack.failures)
    if banner:
        lines.append(banner)
    lines.append(status_banner)
    lines.extend(schedule_lines)
    lines.append("─" * 62)

    lines.append(f"【1. {pack.period} EXECUTIVE SUMMARY】")
    lines.append(f"  {executive_summary}")
    if pack.alerts():
        lines.append("  本期警示：")
        lines.extend(f"    • {item}" for item in pack.alerts())
    lines.append("")

    lines.extend(render_variance_table(pack))
    lines.append("")
    lines.append("  變異數說明（>±%s%% 強制解釋時間差逆轉時間點）：" % pack.material_pct)
    lines.append(f"  {variance_narrative}")
    lines.append("")

    lines.extend(render_cashflow(pack))
    lines.append("")
    lines.append(forecast_text)
    return "\n".join(lines)


__all__ = [
    "BoardPack",
    "CashflowSummary",
    "DiagnosticsLike",
    "GROSS_PROFIT",
    "NET_PROFIT",
    "ReadOnlyViolation",
    "SourceFailure",
    "TOTAL_COGS",
    "TOTAL_OPEX",
    "TOTAL_REVENUE",
    "VarianceRow",
    "build_board_pack",
    "build_variance_row",
    "collect",
    "enforce_single_currency",
    "money",
    "partial_banner",
    "pct",
    "render_board_pack",
    "render_cashflow",
    "render_variance_table",
    "resolve_source_path",
]
