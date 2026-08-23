"""自主權階梯（Autonomy Ladder）— 第 04 章核心安全設計。

書中鐵律：AI 代理人對外送出內容的權限必須「由小放大」，而不是一開始就全自動。
本模組把那條鐵律變成程式碼，讓每個 demo 只要建立一個 `AutonomyGate`，
就自動獲得「預設草稿 + 白名單才放行 + 未滿兩週提出警告」三層保護。

    READ_ONLY        只分類與分析，絕不觸碰來源、絕不外送
        v
    DRAFT（預設）    建立草稿，必須人工審查後送出
        v
    SUPERVISED_AUTO  僅自動送給白名單，其餘一律降級為 DRAFT
"""

from __future__ import annotations

from enum import Enum

# 書中鐵律：全自動之前必須先跑滿兩週草稿模式並取得客戶簽核。
MIN_DAYS_BEFORE_AUTO = 14


class AutonomyLevel(Enum):
    """自主權階梯三段式（第 04 章核心安全設計）"""

    READ_ONLY = "read_only"              # 只分類與分析，絕不觸碰來源、絕不外送
    DRAFT = "draft"                      # 建立草稿，必須人工審查後送出（預設）
    SUPERVISED_AUTO = "supervised_auto"  # 僅自動送給白名單，其餘降級為 DRAFT


class AutonomyError(ValueError):
    """自主權設定違規"""


def parse_level(value: str | AutonomyLevel) -> AutonomyLevel:
    """把 config.yaml 的字串（如 ``"draft"``）轉成 `AutonomyLevel`。

    契約凍結了 `AutonomyGate.__init__` 的簽名，因此字串轉換獨立成模組層函式，
    讓各 demo 讀完 YAML 後先呼叫本函式，再把結果餵給 `AutonomyGate`。

    參數不合法時拋 `AutonomyError`（而非 `ValueError`），呼叫端只需接一種例外。
    """
    if isinstance(value, AutonomyLevel):
        return value
    if not isinstance(value, str):
        raise AutonomyError(f"autonomy 設定必須是字串或 AutonomyLevel，收到 {type(value).__name__}")
    try:
        return AutonomyLevel(value.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(level.value for level in AutonomyLevel)
        raise AutonomyError(f"未知的自主權層級 {value!r}，可用值：{allowed}") from exc


def validate_whitelist(approved_senders: list[str]) -> list[str]:
    """檢查白名單設定品質，回傳警告清單（不拋錯）。

    這些都是「設定寫錯但語法合法」的情況，程式無法自動猜出使用者的意圖，
    只能在啟動時就講清楚——而不是等到某封信誤發出去才發現。

    偵測項目：
    - 只有 "@"：會命中所有 email，等同白名單全開（最嚴重）
    - 空字串或只有空白：無效項目
    - 看起來像網域卻沒有 "@" 開頭（如 "acme.com"）：會被當成完整 email 精確比對
    - 非字串型別：無法比對
    """
    warnings: list[str] = []
    for index, raw in enumerate(approved_senders):
        if not isinstance(raw, str):
            warnings.append(f"白名單第 {index + 1} 項不是字串（{type(raw).__name__}），無法比對，請移除或改寫")
            continue
        item = raw.strip()
        if item == "@":
            warnings.append(
                f"[嚴重] 白名單第 {index + 1} 項只有 \"@\"，會命中所有 email，"
                "等同白名單全開、SUPERVISED_AUTO 形同無限制外送，請立刻改成具體網域（如 @acme.com）"
            )
        elif not item:
            warnings.append(f"白名單第 {index + 1} 項是空字串或只有空白，不會命中任何人，請移除")
        elif "@" not in item and "." in item:
            warnings.append(
                f"白名單第 {index + 1} 項 {raw!r} 看起來像網域，但沒有以 \"@\" 開頭，"
                f"這會被當成完整 email 做精確比對；若要做網域比對請改寫成 \"@{item}\""
            )
    # 最嚴重的問題排最前面，避免被一長串次要警告淹沒
    return sorted(warnings, key=lambda text: not text.startswith("[嚴重]"))


class AutonomyGate:
    """自主權閘門：決定「這封內容能不能自動送給這個人」。"""

    def __init__(
        self,
        level: AutonomyLevel = AutonomyLevel.DRAFT,
        approved_senders: list[str] | None = None,
        days_in_draft: int = 0,
    ) -> None:
        """
        level:            要求的自主權層級，預設 DRAFT
        approved_senders: SUPERVISED_AUTO 的白名單（email 或 domain，如 "@acme.com"）
        days_in_draft:    已在草稿模式運行的天數

        規則：
        1. level=SUPERVISED_AUTO 但 approved_senders 為空 -> 拋 AutonomyError
        2. level=SUPERVISED_AUTO 但 days_in_draft < 14 -> 不拋錯，但 warnings 追加警告
        3. 白名單品質問題（裸網域、空項目、單獨的 "@"）-> 不拋錯，warnings 追加警告

        規則 3 不分層級都會檢查：設定錯誤要在啟動時就看得到，
        而不是等到某天把 level 調成 SUPERVISED_AUTO 才突然誤發。
        """
        if not isinstance(level, AutonomyLevel):
            raise AutonomyError(f"level 必須是 AutonomyLevel，收到 {type(level).__name__}；可先用 parse_level() 轉換")
        if not isinstance(days_in_draft, int) or isinstance(days_in_draft, bool):
            raise AutonomyError(f"days_in_draft 必須是整數，收到 {type(days_in_draft).__name__}")
        if days_in_draft < 0:
            raise AutonomyError(f"days_in_draft 不可為負數，收到 {days_in_draft}")

        self._level = level
        self._approved_senders = self._normalize_senders(approved_senders)
        self._days_in_draft = days_in_draft
        self._warnings: list[str] = validate_whitelist(approved_senders or [])

        if level is AutonomyLevel.SUPERVISED_AUTO:
            self._guard_supervised_auto()

    @staticmethod
    def _normalize_senders(approved_senders: list[str] | None) -> list[str]:
        """白名單一律先去空白並轉小寫，比對時才不必每次重算。"""
        if approved_senders is None:
            return []
        if isinstance(approved_senders, str):
            raise AutonomyError("approved_senders 必須是 list[str]，不可直接傳字串")
        normalized: list[str] = []
        for raw in approved_senders:
            if not isinstance(raw, str):
                raise AutonomyError(f"approved_senders 元素必須是字串，收到 {type(raw).__name__}")
            item = raw.strip().lower()
            if item and item not in normalized:
                normalized.append(item)
        return normalized

    def _guard_supervised_auto(self) -> None:
        """SUPERVISED_AUTO 的兩道防線：空白名單直接擋，未滿兩週只警告。"""
        if not self._approved_senders:
            raise AutonomyError(
                "SUPERVISED_AUTO 必須提供非空的 approved_senders；"
                "沒有白名單的全自動等同無限制外送，違反第 04 章安全設計"
            )
        if self._days_in_draft < MIN_DAYS_BEFORE_AUTO:
            self._warnings.append(
                f"草稿模式僅運行 {self._days_in_draft} 天（未滿 {MIN_DAYS_BEFORE_AUTO} 天）就啟用 "
                "SUPERVISED_AUTO；書中鐵律要求兩週觀察期 + 客戶簽核後才可全自動"
            )

    @property
    def level(self) -> AutonomyLevel:
        """建構時要求的層級（未經收件人降級）。"""
        return self._level

    @property
    def approved_senders(self) -> list[str]:
        """正規化後的白名單副本（外部無法直接改動內部狀態）。"""
        return list(self._approved_senders)

    @property
    def days_in_draft(self) -> int:
        """已在草稿模式運行的天數。"""
        return self._days_in_draft

    @property
    def warnings(self) -> list[str]:
        """累積的警告訊息（供 diagnostics AMBER 使用）"""
        return list(self._warnings)

    def _is_approved(self, recipient: str) -> bool:
        """白名單比對（2026-08-24 修訂，安全性強化）。

        1. 白名單項目以 "@" 開頭（如 "@acme.com"）-> 網域比對，recipient 結尾相符才命中
        2. 白名單項目不以 "@" 開頭 -> 一律視為完整 email，只做精確相等比對
        3. 兩者皆不分大小寫

        為何裸網域不再做結尾比對：白名單若寫 "acme.com"，`boss@evil-acme.com`
        會因為結尾相符而誤判命中。攻擊者只要註冊一個含目標網域的網域名，
        就能讓系統對他自動送出內容。這種洞不能只靠文件提醒使用者，必須在程式層擋掉——
        `@` 開頭強制了「分隔符必須存在」，`bob@evil-acme.com` 不會以 `@acme.com` 結尾。
        """
        target = recipient.strip().lower()
        if not target:
            return False
        for item in self._approved_senders:
            if item.startswith("@"):
                if target.endswith(item):  # 網域比對
                    return True
            elif target == item:           # 完整 email，只接受精確相等
                return True
        return False

    def effective_level(self, recipient: str) -> AutonomyLevel:
        """回傳針對此收件人的實際層級。

        SUPERVISED_AUTO 且 recipient 命中白名單 -> SUPERVISED_AUTO
        SUPERVISED_AUTO 但未命中                -> DRAFT（降級）
        其餘                                     -> 原 level

        白名單比對規則見 `_is_approved`：`@` 開頭做網域比對，
        其餘一律當成完整 email 做精確相等比對，皆不分大小寫。
        """
        if self._level is not AutonomyLevel.SUPERVISED_AUTO:
            return self._level
        if not isinstance(recipient, str):
            raise AutonomyError(f"recipient 必須是字串，收到 {type(recipient).__name__}")
        return AutonomyLevel.SUPERVISED_AUTO if self._is_approved(recipient) else AutonomyLevel.DRAFT

    def can_send(self, recipient: str) -> bool:
        """effective_level(recipient) is SUPERVISED_AUTO 時才回 True"""
        return self.effective_level(recipient) is AutonomyLevel.SUPERVISED_AUTO
