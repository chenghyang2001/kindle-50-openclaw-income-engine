"""demo27 — 三級 Escalation Matrix（附錄G apxG_p15 逐字實作）。

| 級別 | 通道 | 通知對象 |
| --- | --- | --- |
| Critical | Slack **+** Email（雙通道） | 法務長 / 法遵官 |
| High     | Slack + Email              | 責任經理 |
| Standard | 僅 Email                   | 合規信箱 |

**安全注意（SPEC 明訂）**：Critical 級別必須雙通道同時通知，單一通道失效即漏報。
本模組因此把「雙通道可用性」做成硬檢查，失敗會標成 `incomplete_dual_channel`
並累計 amber，而不是靜默送出一半。

**全域安全閥（apxG_p03）**：所有對外 API 呼叫前必經 `--dry-run` 內部通訊測試。
`dry_run_probe()` 就是那道閘門：mock 模式回報模擬結果，live 模式檢查每個通道
所需的環境變數是否齊全，缺就把該通道標為不可用。

⚠️ 法律免責：升級等級只是**初步篩選**的排序依據，不構成法律意見。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from analyser import Finding

# 等級由弱到強；比較時一律用這張表，不要用字串排序
LEVEL_RANK: dict[str, int] = {"standard": 1, "high": 2, "critical": 3}
LEVELS: tuple[str, ...] = ("critical", "high", "standard")

# rules 允許出現的觸發鍵（其餘鍵視為設定錯誤，避免打錯字靜默失效）
RULE_KEYS: tuple[str, ...] = (
    "overdue",
    "stage_120",
    "stage_60",
    "stage_14",
    "needs_human_review",
    "regulatory_default",
)


class EscalationError(RuntimeError):
    """升級矩陣設定違規（缺級別 / 通道空白 / Critical 未設雙通道）"""


@dataclass(frozen=True)
class EscalationRoute:
    """單一級別的路由設定。"""

    level: str
    channels: tuple[str, ...]
    recipients: tuple[str, ...]
    requires_dual_channel: bool


@dataclass(frozen=True)
class ChannelProbe:
    """單一通道的 --dry-run 內部通訊測試結果。"""

    channel: str
    is_available: bool
    detail: str


@dataclass(frozen=True)
class Notice:
    """一則待發送（或被抑制）的升級通知。"""

    finding: Finding
    level: str
    route: EscalationRoute
    channels_ok: tuple[str, ...]
    channels_failed: tuple[str, ...]
    delivery_status: str
    is_suppressed: bool

    @property
    def is_deliverable(self) -> bool:
        """雙通道要求未滿足時不算可送達（單通道即漏報）。"""
        return self.delivery_status == "ready"


def load_matrix(escalation_config: dict[str, Any]) -> dict[str, EscalationRoute]:
    """把 config.escalation.levels 轉成三個 EscalationRoute，缺一即報錯。"""
    raw_levels = escalation_config.get("levels") or {}
    matrix: dict[str, EscalationRoute] = {}
    for level in LEVELS:
        entry = raw_levels.get(level)
        if not isinstance(entry, dict):
            raise EscalationError(f"escalation.levels 缺少 {level!r} 級別設定")
        channels = tuple(str(item).strip().lower() for item in (entry.get("channels") or []) if str(item).strip())
        recipients = tuple(str(item) for item in (entry.get("recipients") or []) if str(item).strip())
        if not channels or not recipients:
            raise EscalationError(f"escalation.levels.{level} 的 channels 與 recipients 都不得為空")
        matrix[level] = EscalationRoute(
            level=level,
            channels=channels,
            recipients=recipients,
            requires_dual_channel=bool(entry.get("requires_dual_channel", False)),
        )
    _guard_critical_dual_channel(matrix["critical"])
    return matrix


def _guard_critical_dual_channel(critical: EscalationRoute) -> None:
    """SPEC 硬要求：Critical 必須雙通道。設定檔想關掉就直接擋下，不容許協商。"""
    if not critical.requires_dual_channel or len(critical.channels) < 2:
        raise EscalationError(
            "Critical 級別必須設定 requires_dual_channel: true 且至少兩個通道"
            "（SPEC apxG_p15：單一通道失效即漏報）"
        )


def load_rules(escalation_config: dict[str, Any]) -> dict[str, str]:
    """讀取觸發門檻對應表，並驗證每個值都是合法級別。"""
    raw_rules = escalation_config.get("rules") or {}
    rules: dict[str, str] = {}
    for key, value in raw_rules.items():
        name = str(key)
        level = str(value).strip().lower()
        if name not in RULE_KEYS:
            raise EscalationError(f"escalation.rules 出現未知觸發鍵 {name!r}，可用：{', '.join(RULE_KEYS)}")
        if level not in LEVEL_RANK:
            raise EscalationError(f"escalation.rules.{name} 的值 {value!r} 不是合法級別")
        rules[name] = level
    missing = [key for key in ("needs_human_review", "regulatory_default") if key not in rules]
    if missing:
        raise EscalationError(f"escalation.rules 缺少必填鍵：{', '.join(missing)}")
    return rules


def classify(finding: Finding, rules: dict[str, str]) -> str:
    """判定單筆發現的升級級別；回空字串代表不需要升級。

    取所有候選來源的**最高**級別（法遵寧可往上報，不往下壓）：
    到期階段、needs_human_review、法規公告自述等級。
    """
    candidates: list[str] = []
    if finding.kind == "regulatory":
        candidates.append(finding.declared_level or rules["regulatory_default"])
    else:
        stage_level = rules.get(finding.stage)
        if stage_level:
            candidates.append(stage_level)
    if finding.needs_human_review:
        candidates.append(rules["needs_human_review"])
    if not candidates:
        return ""
    return max(candidates, key=lambda level: LEVEL_RANK[level])


def dry_run_probe(
    matrix: dict[str, EscalationRoute], channel_env: dict[str, Any], is_mock: bool
) -> dict[str, ChannelProbe]:
    """全域安全閥：對外送出前先做一次內部通訊測試，不實際打任何 API。

    mock 模式一律回報「模擬通過」（零憑證零網路是驗收條件）；
    live 模式檢查該通道宣告的環境變數是否齊全，缺就標不可用。
    """
    probes: dict[str, ChannelProbe] = {}
    for channel in sorted({channel for route in matrix.values() for channel in route.channels}):
        required = [str(name) for name in (channel_env.get(channel) or [])]
        if is_mock:
            probes[channel] = ChannelProbe(channel, True, "mock 模擬通過（未實際連線）")
            continue
        missing = [name for name in required if not os.environ.get(name)]
        detail = f"缺少環境變數：{', '.join(missing)}" if missing else f"憑證齊全（{', '.join(required) or '無需憑證'}）"
        probes[channel] = ChannelProbe(channel, not missing, detail)
    return probes


def _split_channels(
    route: EscalationRoute, probes: dict[str, ChannelProbe]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把路由通道拆成「可用」與「不可用」兩組。"""
    ok = tuple(name for name in route.channels if probes.get(name) and probes[name].is_available)
    failed = tuple(name for name in route.channels if name not in ok)
    return ok, failed


def _delivery_status(route: EscalationRoute, ok: tuple[str, ...], failed: tuple[str, ...]) -> str:
    """決定送達狀態。Critical 少一個通道就算漏報，不能當成部分成功。"""
    if not ok:
        return "blocked_no_channel"
    if route.requires_dual_channel and failed:
        return "incomplete_dual_channel"
    return "ready"


def build_notices(
    findings: list[Finding],
    rules: dict[str, str],
    matrix: dict[str, EscalationRoute],
    probes: dict[str, ChannelProbe],
    already_notified: dict[str, str],
) -> list[Notice]:
    """把需要升級的發現轉成通知；已在同一階段通報過的標記為抑制。"""
    notices: list[Notice] = []
    for finding in findings:
        level = classify(finding, rules)
        if not level:
            continue
        route = matrix[level]
        ok, failed = _split_channels(route, probes)
        notices.append(
            Notice(
                finding=finding,
                level=level,
                route=route,
                channels_ok=ok,
                channels_failed=failed,
                delivery_status=_delivery_status(route, ok, failed),
                is_suppressed=already_notified.get(finding.key) == finding.stage,
            )
        )
    notices.sort(key=lambda item: (-LEVEL_RANK[item.level], item.finding.days if item.finding.days is not None else 9999))
    return notices


def dual_channel_warnings(notices: list[Notice]) -> list[str]:
    """列出所有雙通道未滿足的 Critical 通知（呼叫端據此發 amber）。"""
    return [
        f"Critical 通知 {notice.finding.record_id} 只剩通道 {', '.join(notice.channels_ok) or '（無）'}"
        f"，失效通道：{', '.join(notice.channels_failed)}｜單一通道即漏報"
        for notice in notices
        if notice.delivery_status != "ready"
    ]


def render_notice(notice: Notice) -> str:
    """把一則通知轉成人看的文字（含逐字條款佐證與來源依據）。"""
    finding = notice.finding
    days_text = "（天數不明）" if finding.days is None else f"{finding.days} 天"
    header = f"[{notice.level.upper()}] {finding.kind}｜{finding.record_id}｜{finding.title}"
    lines = [
        header,
        f"  階段：{finding.stage}｜距到期/應審查：{days_text}｜負責人：{finding.owner}",
        f"  通知對象：{', '.join(notice.route.recipients)}｜通道：{', '.join(notice.route.channels)}"
        f"｜狀態：{notice.delivery_status}{'（已抑制重複）' if notice.is_suppressed else ''}",
        f"  來源依據：{finding.source_ref}",
        f"  條款原文（逐字）：{finding.evidence}",
    ]
    if finding.needs_human_review:
        lines.append(f"  ⚠ 需人工複核：{'；'.join(finding.review_reasons)}")
    return "\n".join(lines)
