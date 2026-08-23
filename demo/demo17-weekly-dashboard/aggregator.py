"""每週績效儀表板的聚合核心：六源取數 → METRICS_MAP 對照 → WoW/移動平均 → RAG 燈號。

負責五件事：

1. **多源取數**：GA4 / Google Ads / Meta / CRM / Xero / QuickBooks 各自獨立，
   任一失敗只影響它負責的 KPI，不影響其他區塊（見 `collect`）。
2. **METRICS_MAP**：把 config 的區塊定義轉成 `BlockSpec`，強制每區塊 4-5 個 KPI。
3. **週對週比較（WoW）與四週移動平均**：歷史值來自狀態檔，沒有歷史就回 None。
4. **異常標記與 RAG 燈號**：門檻全部由 `Thresholds` 帶入，程式碼不寫死數字。
5. **Focus Actions**：依「對客戶最不利」排序挑出前 N 個 KPI，交給提示詞寫成建議。

三個貫穿全檔的設計決定：

- **數值一律 `Decimal`**。百分比變化在四週移動平均上會被反覆放大，float 誤差
  會讓 15.0% 的門檻在邊界上隨機翻面。
- **分母為 0 或缺值一律回 `None`，不回 0%**。「上週是 0」和「這週沒變化」是
  完全不同的兩件事；把首週當成 0 去算會產出 +∞ 或假的 +100%，那正是這份
  報表最容易讓管理層做錯決策的地方。
- **失敗的 KPI 仍然留在報表上**，標成「⚠️ 無資料」而不是從畫面上消失。
  消失的指標沒有人會發現它消失了。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Protocol

TWO_PLACES = Decimal("0.01")
ONE_PLACE = Decimal("0.1")
HUNDRED = Decimal("100")


class SourceError(RuntimeError):
    """單一資料源取數失敗。訊息必須指出是哪個資料源、哪個檔案、什麼原因。"""


class RagStatus(Enum):
    """儀表板燈號。UNKNOWN（灰）代表「沒有比較基準」，不是「正常」——
    首週與故障源都會落在這一格，兩者都不該被看成綠燈。"""

    GREEN = "green"
    AMBER = "amber"
    RED = "red"
    UNKNOWN = "unknown"


#: 燈號嚴重度排序，用來把多個 KPI 收斂成區塊燈號、多個區塊收斂成整體燈號。
_RAG_RANK: dict[RagStatus, int] = {
    RagStatus.UNKNOWN: 0,
    RagStatus.GREEN: 1,
    RagStatus.AMBER: 2,
    RagStatus.RED: 3,
}


class DiagnosticsLike(Protocol):
    """只用到 `Diagnostics` 的 amber()，用 Protocol 讓測試能塞假物件。"""

    def amber(self, symptom: str, fix: str) -> None: ...


# --------------------------------------------------------------------------
# 基礎型別
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KpiSpec:
    """單一 KPI 的定義（來自 config 的 metrics_map）。"""

    kpi_id: str
    display_name: str
    source_id: str
    metric_key: str
    unit: str
    is_higher_better: bool


@dataclass(frozen=True)
class BlockSpec:
    """儀表板的一個區塊，內含 4-5 個 KPI。"""

    block_id: str
    display_name: str
    kpis: tuple[KpiSpec, ...]


@dataclass(frozen=True)
class Thresholds:
    """異常與 RAG 判定門檻，全部以百分比表示。"""

    anomaly_pct: Decimal
    rag_amber_pct: Decimal
    rag_red_pct: Decimal
    max_kpis_per_block: int


@dataclass(frozen=True)
class SourceSnapshot:
    """單一資料源本週的原始取數結果（metrics 值維持字串，之後才進 Decimal）。"""

    source_id: str
    display_name: str
    week_id: str
    metrics: dict[str, str]


@dataclass(frozen=True)
class SourceFailure:
    """單一資料源失敗紀錄，會出現在報表橫幅與回傳 dict 中。"""

    source_id: str
    display_name: str
    reason: str


@dataclass(frozen=True)
class WeekRecord:
    """歷史狀態檔中的一週快照。"""

    week_id: str
    values: dict[str, Decimal]


@dataclass(frozen=True)
class MetricResult:
    """單一 KPI 的完整計算結果。"""

    spec: KpiSpec
    value: Decimal | None
    previous: Decimal | None
    wow_pct: Decimal | None
    moving_avg: Decimal | None
    vs_avg_pct: Decimal | None
    rag: RagStatus
    is_anomaly: bool
    unavailable_reason: str | None

    @property
    def is_available(self) -> bool:
        """本週值是否取得成功。"""
        return self.value is not None

    @property
    def favourable_pct(self) -> Decimal | None:
        """把變化轉成「對客戶有利為正」。廣告花費 +20% 在這裡是 -20。"""
        if self.wow_pct is None:
            return None
        return self.wow_pct if self.spec.is_higher_better else -self.wow_pct

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {
            "kpi_id": self.spec.kpi_id,
            "display_name": self.spec.display_name,
            "source_id": self.spec.source_id,
            "unit": self.spec.unit,
            "direction": "up" if self.spec.is_higher_better else "down",
            "value": _opt_str(self.value),
            "previous": _opt_str(self.previous),
            "wow_pct": _opt_str(self.wow_pct),
            "moving_avg": _opt_str(self.moving_avg),
            "vs_moving_avg_pct": _opt_str(self.vs_avg_pct),
            "rag": self.rag.value,
            "is_anomaly": self.is_anomaly,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class BlockResult:
    """一個區塊的計算結果。"""

    spec: BlockSpec
    metrics: tuple[MetricResult, ...]
    rag: RagStatus

    @property
    def is_partial(self) -> bool:
        """區塊中只要有任一 KPI 取不到值就算部分資料。"""
        return any(not item.is_available for item in self.metrics)

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構。"""
        return {
            "block_id": self.spec.block_id,
            "display_name": self.spec.display_name,
            "rag": self.rag.value,
            "is_partial": self.is_partial,
            "metrics": [item.to_dict() for item in self.metrics],
        }


@dataclass(frozen=True)
class WeeklyDashboard:
    """一份完整的週報（純資料，不含排版）。"""

    week_id: str
    currency: str
    blocks: tuple[BlockResult, ...]
    failures: tuple[SourceFailure, ...]
    overall_rag: RagStatus
    anomalies: tuple[str, ...]
    focus_actions: tuple[MetricResult, ...]
    comparison_week_id: str | None
    history_weeks: int
    moving_average_weeks: int

    @property
    def is_partial(self) -> bool:
        """只要有任一資料源失敗就是部分資料。"""
        return bool(self.failures)

    @property
    def has_comparison(self) -> bool:
        """是否有上週資料可比較。首週為 False。"""
        return self.comparison_week_id is not None

    def all_metrics(self) -> list[MetricResult]:
        """攤平所有區塊的 KPI，供排序與寫回狀態檔使用。"""
        return [item for block in self.blocks for item in block.metrics]

    def to_dict(self) -> dict[str, Any]:
        """轉成 JSON-safe 結構（Decimal 一律轉字串，保住精度）。"""
        return {
            "week_id": self.week_id,
            "currency": self.currency,
            "overall_rag": self.overall_rag.value,
            "is_partial": self.is_partial,
            "has_comparison": self.has_comparison,
            "comparison_week_id": self.comparison_week_id,
            "history_weeks": self.history_weeks,
            "moving_average_weeks": self.moving_average_weeks,
            "blocks": [block.to_dict() for block in self.blocks],
            "failed_sources": [
                {
                    "source_id": fail.source_id,
                    "display_name": fail.display_name,
                    "reason": fail.reason,
                }
                for fail in self.failures
            ],
            "anomalies": list(self.anomalies),
            "focus_actions": [item.to_dict() for item in self.focus_actions],
        }

    def to_json(self) -> str:
        """給 LLM 當 user message 用的 JSON。"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


def _opt_str(value: Decimal | None) -> str | None:
    """Decimal 轉字串，None 維持 None（不要變成 "None"）。"""
    return None if value is None else str(value)


# --------------------------------------------------------------------------
# 數值工具
# --------------------------------------------------------------------------


def to_decimal(value: Any, context: str, field_name: str) -> Decimal:
    """把 JSON / YAML 取出的值轉成 Decimal。先轉 str 再進 Decimal，避免 float 誤差。"""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SourceError(f"{context} 的 {field_name} 不是合法數值：{value!r}") from exc


def quantize_money(value: Decimal) -> Decimal:
    """數值收斂到 2 位。用 ROUND_HALF_UP（財務慣例），不用 Decimal 預設的銀行家捨入。"""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def quantize_pct(value: Decimal) -> Decimal:
    """百分比收斂到 1 位。"""
    return value.quantize(ONE_PLACE, rounding=ROUND_HALF_UP)


def percent_change(current: Decimal | None, previous: Decimal | None) -> Decimal | None:
    """週對週變化率。分母為 0 或任一端缺值一律回 None，**絕不回 0%**。

    首週把上週當 0 去算會得到除零錯誤或假的 +100%，管理層會據此開錯的會。
    """
    if current is None or previous is None or previous == 0:
        return None
    return quantize_pct((current - previous) / previous * HUNDRED)


# --------------------------------------------------------------------------
# 設定載入
# --------------------------------------------------------------------------


def load_thresholds(raw: dict[str, Any] | None) -> Thresholds:
    """從 config.yaml 的 thresholds 區塊建立門檻，缺項用書中預設值補。"""
    raw = raw or {}
    return Thresholds(
        anomaly_pct=to_decimal(raw.get("anomaly_pct", 15), "thresholds", "anomaly_pct"),
        rag_amber_pct=to_decimal(raw.get("rag_amber_pct", 5), "thresholds", "rag_amber_pct"),
        rag_red_pct=to_decimal(raw.get("rag_red_pct", 15), "thresholds", "rag_red_pct"),
        max_kpis_per_block=int(raw.get("max_kpis_per_block", 5)),
    )


def _build_kpi_spec(raw: dict[str, Any], block_id: str) -> KpiSpec:
    """把 config 的單筆 KPI 定義轉成 KpiSpec；缺必要欄位直接拋錯不猜。"""
    for field_name in ("id", "source", "metric"):
        if not str(raw.get(field_name, "")).strip():
            raise ValueError(f"metrics_map.{block_id} 有 KPI 缺少必要欄位 {field_name}")
    return KpiSpec(
        kpi_id=str(raw["id"]).strip(),
        display_name=str(raw.get("display_name") or raw["id"]).strip(),
        source_id=str(raw["source"]).strip(),
        metric_key=str(raw["metric"]).strip(),
        unit=str(raw.get("unit", "count")).strip(),
        is_higher_better=str(raw.get("direction", "up")).strip().lower() != "down",
    )


def load_metrics_map(
    raw: dict[str, Any] | None,
    thresholds: Thresholds,
    diagnostics: DiagnosticsLike | None = None,
) -> tuple[BlockSpec, ...]:
    """建立 METRICS_MAP。超過 max_kpis_per_block 的 KPI 會被截斷並記琥珀燈。

    為什麼是截斷而不是拋錯：書中這條限制的目的是保住可讀性，不是資料正確性。
    多塞了兩個 KPI 就整份報表不發，等於用更大的傷害去糾正一個排版問題。
    """
    blocks: list[BlockSpec] = []
    for entry in (raw or {}).get("blocks") or []:
        block_id = str(entry.get("id", "")).strip() or "unnamed"
        specs = [_build_kpi_spec(item, block_id) for item in entry.get("kpis") or []]
        if len(specs) > thresholds.max_kpis_per_block:
            _amber(
                diagnostics,
                f"區塊「{block_id}」有 {len(specs)} 個 KPI，"
                f"超過上限 {thresholds.max_kpis_per_block}，已截斷",
                "書中規定每區塊 4-5 個 KPI；請在 config.yaml 的 metrics_map 中精簡",
            )
            specs = specs[: thresholds.max_kpis_per_block]
        blocks.append(
            BlockSpec(
                block_id=block_id,
                display_name=str(entry.get("display_name") or block_id),
                kpis=tuple(specs),
            )
        )
    if not blocks:
        raise ValueError("config.yaml 的 metrics_map.blocks 是空的，沒有任何 KPI 可報告")
    return tuple(blocks)


# --------------------------------------------------------------------------
# 取數
# --------------------------------------------------------------------------


def fetch_source(mock_path: Path, source_id: str) -> SourceSnapshot:
    """讀取單一資料源的 mock JSON。

    六個資料源的 payload 形狀相同（`metrics` 為 key-value），因此共用一個 fetcher，
    不為每個平台各寫一支只差檔名的模組。`--live` 時各平台 API 的差異應收斂在
    「回傳 {metric_key: 值}」這層介面之後，本函式以下的邏輯完全不必改。

    四種失敗（讀不到檔、JSON 壞掉、payload 帶 error 欄位、缺 metrics）
    一律轉成 SourceError，交由 `collect` 走部分資料路徑。
    """
    try:
        raw = mock_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourceError(f"{source_id} 無法讀取資料檔 {mock_path}：{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError(f"{source_id} 的資料檔 JSON 解析失敗 {mock_path}：{exc}") from exc

    if not isinstance(payload, dict):
        raise SourceError(f"{source_id} 的資料檔頂層必須是物件：{mock_path}")
    if payload.get("error"):
        raise SourceError(f"{source_id} 回報錯誤：{payload['error']}")

    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        raise SourceError(f"{source_id} 缺少 metrics 物件：{mock_path}")

    return SourceSnapshot(
        source_id=source_id,
        display_name=source_id,
        week_id=str(payload.get("week_id", "")).strip(),
        metrics={str(key): str(value) for key, value in metrics.items()},
    )


def collect(
    source_configs: Iterable[dict[str, Any]],
    base_dir: Path,
    diagnostics: DiagnosticsLike | None = None,
) -> tuple[dict[str, SourceSnapshot], list[SourceFailure]]:
    """逐一取數。**任何單源失敗都不中斷迴圈**——這是整個模組的核心設計。

    失敗同時做兩件事：記進 failures（進報表橫幅與該 KPI 的「無資料」標記），
    以及寫 `Diagnostics.amber`（進 RAG 診斷矩陣的琥珀燈），讓維運端知道要修，
    但客戶的管理層照樣在週一早上收得到報表。
    """
    snapshots: dict[str, SourceSnapshot] = {}
    failures: list[SourceFailure] = []

    for entry in source_configs:
        source_id = str(entry.get("id", "")).strip()
        display_name = str(entry.get("display_name") or source_id or "未命名資料源")
        if not source_id:
            failures.append(SourceFailure("", display_name, "設定缺少 id 欄位"))
            _amber(diagnostics, f"{display_name} 設定缺少 id 欄位", "補上 sources[].id")
            continue

        try:
            snapshot = fetch_source(base_dir / str(entry.get("mock_file", "")), source_id)
        except SourceError as exc:
            failures.append(SourceFailure(source_id, display_name, str(exc)))
            _amber(
                diagnostics,
                f"{display_name} 無回應，本週報表以部分資料產出",
                f"檢查 {display_name} 憑證與 API 狀態後重跑；原因：{exc}",
            )
            continue

        snapshots[source_id] = replace(snapshot, display_name=display_name)

    return snapshots, failures


def _amber(diagnostics: DiagnosticsLike | None, symptom: str, fix: str) -> None:
    """把問題送進診斷矩陣的琥珀燈（流程繼續，不中斷）。"""
    if diagnostics is not None:
        diagnostics.amber(symptom, fix)


def resolve_week_id(
    snapshots: dict[str, SourceSnapshot],
    fallback: str,
    diagnostics: DiagnosticsLike | None = None,
) -> str:
    """決定本次報表的週次。多數決；有資料源回報不同週次時記琥珀燈。

    週次不一致通常代表某個平台的資料還沒結算完（常見於財務系統），
    這時把兩週的數字混在一起做 WoW 比較，會產生看起來合理但完全錯誤的趨勢。
    """
    week_ids = [snap.week_id for snap in snapshots.values() if snap.week_id]
    if not week_ids:
        return fallback

    ranked = sorted(set(week_ids), key=lambda item: (-week_ids.count(item), item))
    if len(ranked) > 1:
        _amber(
            diagnostics,
            f"資料源回報的週次不一致：{'、'.join(ranked)}，本次採用 {ranked[0]}",
            "確認各平台的週結算時間，或把排程延後到最慢的平台結算完成之後",
        )
    return ranked[0]


# --------------------------------------------------------------------------
# 歷史狀態
# --------------------------------------------------------------------------


def load_history(path: Path) -> list[WeekRecord]:
    """讀歷史狀態檔（由舊到新）。檔案不存在時回空 list——那就是「首週」。

    首週不是錯誤，是每個客戶都會經歷一次的正常狀態，因此不拋錯、不記琥珀燈。
    """
    if not path.exists():
        return []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"無法讀取歷史狀態檔 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"歷史狀態檔 JSON 解析失敗 {path}：{exc}") from exc

    weeks = payload.get("weeks") if isinstance(payload, dict) else None
    if not isinstance(weeks, list):
        raise ValueError(f"歷史狀態檔缺少 weeks 陣列：{path}")

    return [_build_week_record(item, path) for item in weeks]


def _build_week_record(item: Any, path: Path) -> WeekRecord:
    """把狀態檔中的一筆週資料轉成 WeekRecord。"""
    if not isinstance(item, dict):
        raise ValueError(f"歷史狀態檔的 weeks 元素必須是物件：{path}")
    values = item.get("values") or {}
    if not isinstance(values, dict):
        raise ValueError(f"歷史狀態檔的 values 必須是物件：{path}")
    return WeekRecord(
        week_id=str(item.get("week_id", "")),
        values={
            str(key): to_decimal(raw, "history", f"values.{key}")
            for key, raw in values.items()
        },
    )


def append_week(history: list[WeekRecord], dashboard: WeeklyDashboard) -> list[WeekRecord]:
    """把本週結果併入歷史；同週次重跑會覆寫而不是新增一筆。

    覆寫是刻意的：週一早上因為某個源掛掉而重跑三次，不該在歷史裡留下三筆
    2026-W33，否則四週移動平均會被同一週灌爆。
    """
    values = {
        item.spec.kpi_id: item.value
        for item in dashboard.all_metrics()
        if item.value is not None
    }
    kept = [record for record in history if record.week_id != dashboard.week_id]
    kept.append(WeekRecord(week_id=dashboard.week_id, values=values))
    return kept


def save_history(path: Path, records: list[WeekRecord], keep_weeks: int) -> None:
    """把歷史寫回狀態檔，只保留最近 keep_weeks 週。"""
    trimmed = records[-keep_weeks:] if keep_weeks > 0 else records
    payload = {
        "keep_weeks": keep_weeks,
        "weeks": [
            {
                "week_id": record.week_id,
                "values": {key: str(value) for key, value in record.values.items()},
            }
            for record in trimmed
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 計算
# --------------------------------------------------------------------------


def _read_metric(
    spec: KpiSpec,
    snapshots: dict[str, SourceSnapshot],
    failed: dict[str, str],
) -> tuple[Decimal | None, str | None]:
    """取單一 KPI 的本週值。回傳 (值, 無法取得的原因)，兩者必有一為 None。"""
    if spec.source_id in failed:
        return None, f"{failed[spec.source_id]} 無回應"

    snapshot = snapshots.get(spec.source_id)
    if snapshot is None:
        return None, f"資料源 {spec.source_id} 未在 config.yaml 的 sources 中設定"
    if spec.metric_key not in snapshot.metrics:
        return None, f"{snapshot.display_name} 未提供指標 {spec.metric_key}"

    try:
        raw = snapshot.metrics[spec.metric_key]
        return to_decimal(raw, snapshot.display_name, spec.metric_key), None
    except SourceError as exc:
        return None, str(exc)


def _moving_average(history: list[WeekRecord], kpi_id: str, window: int) -> Decimal | None:
    """最近 window 週的移動平均（不含本週）。完全沒有歷史值時回 None。

    刻意不要求「必須湊滿 window 週」：第二週就有一週基準可看，比什麼都不給好；
    樣本不足的事實由 `history_weeks` 一併回報給提示詞，由敘述層說清楚。
    """
    values = [
        record.values[kpi_id] for record in history[-window:] if kpi_id in record.values
    ]
    if not values:
        return None
    return quantize_money(sum(values, Decimal("0")) / Decimal(len(values)))


def _rag_for(favourable_pct: Decimal | None, thresholds: Thresholds) -> RagStatus:
    """依「對客戶不利的變動幅度」決定燈號。沒有比較基準一律灰燈。"""
    if favourable_pct is None:
        return RagStatus.UNKNOWN
    if favourable_pct <= -thresholds.rag_red_pct:
        return RagStatus.RED
    if favourable_pct <= -thresholds.rag_amber_pct:
        return RagStatus.AMBER
    return RagStatus.GREEN


def evaluate_metric(
    spec: KpiSpec,
    snapshots: dict[str, SourceSnapshot],
    failed: dict[str, str],
    history: list[WeekRecord],
    thresholds: Thresholds,
    moving_average_weeks: int = 4,
) -> MetricResult:
    """算出單一 KPI 的 WoW、移動平均、燈號與異常標記。"""
    value, reason = _read_metric(spec, snapshots, failed)
    previous = history[-1].values.get(spec.kpi_id) if history else None
    wow_pct = percent_change(value, previous)
    moving_avg = _moving_average(history, spec.kpi_id, moving_average_weeks)

    draft = MetricResult(
        spec=spec,
        value=value,
        previous=previous,
        wow_pct=wow_pct,
        moving_avg=moving_avg,
        vs_avg_pct=percent_change(value, moving_avg),
        rag=RagStatus.UNKNOWN,
        is_anomaly=wow_pct is not None and abs(wow_pct) > thresholds.anomaly_pct,
        unavailable_reason=reason,
    )
    # favourable_pct 是 spec + wow_pct 的衍生值，必須先組出 draft 才算得出燈號。
    return replace(draft, rag=_rag_for(draft.favourable_pct, thresholds))


def _worst_rag(items: Iterable[RagStatus]) -> RagStatus:
    """收斂多個燈號成一個：紅 > 黃 > 綠 > 灰。空集合回灰燈。"""
    return max(items, key=lambda status: _RAG_RANK[status], default=RagStatus.UNKNOWN)


def _anomaly_line(item: MetricResult, thresholds: Thresholds) -> str:
    """把一個異常 KPI 寫成人看得懂的一行。"""
    wow_pct = item.wow_pct
    if wow_pct is None:  # is_anomaly 為真時不會發生，保留守衛避免型別假設外洩
        return f"⚠️ {item.spec.display_name}：異常但缺少變化率"
    direction = "上升" if wow_pct > 0 else "下降"
    tone = "有利" if (item.favourable_pct or Decimal("0")) > 0 else "不利"
    return (
        f"⚠️ {item.spec.display_name}：週對週{direction} {abs(wow_pct)}%，"
        f"超過 {thresholds.anomaly_pct}% 異常門檻（方向對客戶{tone}）"
    )


def select_focus_actions(metrics: list[MetricResult], count: int) -> tuple[MetricResult, ...]:
    """挑出「對客戶最不利」的前 N 個 KPI，作為本週行動建議的依據。

    排序鍵是 favourable_pct 升冪（越負越前面），Python 的穩定排序讓同分時
    維持 config 中的區塊順序——同樣的輸入永遠得到同樣的三條建議，
    客戶不會因為重跑一次就看到不同的優先順序。

    首週或全部無資料時回空 tuple：**沒有比較基準就不編建議**。
    """
    if count <= 0:
        return ()
    ranked = [item for item in metrics if item.favourable_pct is not None]
    ranked.sort(key=lambda item: item.favourable_pct or Decimal("0"))
    return tuple(ranked[:count])


def build_dashboard(
    blocks: tuple[BlockSpec, ...],
    snapshots: dict[str, SourceSnapshot],
    failures: list[SourceFailure],
    history: list[WeekRecord],
    thresholds: Thresholds,
    week_id: str,
    currency: str = "USD",
    focus_action_count: int = 3,
    moving_average_weeks: int = 4,
) -> WeeklyDashboard:
    """把六源快照與歷史合成 WeeklyDashboard；部分資料時照樣算出可用的數字。"""
    failed = {fail.source_id: fail.display_name for fail in failures}
    block_results = [
        _build_block(block, snapshots, failed, history, thresholds, moving_average_weeks)
        for block in blocks
    ]
    all_metrics = [item for block in block_results for item in block.metrics]

    return WeeklyDashboard(
        week_id=week_id,
        currency=currency,
        blocks=tuple(block_results),
        failures=tuple(failures),
        overall_rag=_worst_rag(block.rag for block in block_results),
        anomalies=tuple(
            _anomaly_line(item, thresholds) for item in all_metrics if item.is_anomaly
        ),
        focus_actions=select_focus_actions(all_metrics, focus_action_count),
        comparison_week_id=history[-1].week_id if history else None,
        history_weeks=len(history),
        moving_average_weeks=moving_average_weeks,
    )


def _build_block(
    block: BlockSpec,
    snapshots: dict[str, SourceSnapshot],
    failed: dict[str, str],
    history: list[WeekRecord],
    thresholds: Thresholds,
    moving_average_weeks: int,
) -> BlockResult:
    """算完一個區塊的所有 KPI，並收斂出區塊燈號。"""
    metrics = tuple(
        evaluate_metric(spec, snapshots, failed, history, thresholds, moving_average_weeks)
        for spec in block.kpis
    )
    return BlockResult(spec=block, metrics=metrics, rag=_worst_rag(m.rag for m in metrics))


def partial_banner(failures: Iterable[SourceFailure]) -> str:
    """產出「⚠️ 部分資料：Meta Ads 無回應」橫幅；全部正常時回空字串。

    橫幅必須寫在報表**最上方**：管理層在看到任何數字之前，就要先知道
    這份數字是不完整的，否則會拿殘缺數據去做預算決策。
    """
    names = [fail.display_name for fail in failures]
    if not names:
        return ""
    return f"⚠️ 部分資料：{'、'.join(names)} 無回應"
