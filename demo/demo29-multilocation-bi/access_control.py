"""兩級權限視野的**資料層**閘門（apxG_p17：Full Pack / Own-site only）。

本模組是 demo29 的安全核心。三條不可妥協的規則：

1. **權限判定發生在取數之前，不是排版之前。**
   `resolve_scope()` 先算出可見據點集合，`AccessScope.filter_registry()` 再把
   名冊裁掉；店長視野下，其他據點的 mock/API 從頭到尾**不會被讀取**，
   因此他拿到的資料集裡根本不存在別家的數字，而不是「有但沒印出來」。
   這是資料隔離要求，不是 UI 便利性——只在 render 層過濾，等於把他店營運數據
   放進同一份記憶體物件、同一份 LLM prompt、同一份稽核附件裡，一次 bug 就外洩。

2. **權限設定缺失或角色未知，一律退回最小權限，而最小權限的底線是「什麼都不給」。**
   角色無法辨識 -> 直接 deny-all，**不論 `--site` 給了什麼**（角色都認不出來，就
   無從得知這個呼叫者有權看哪一店，照著他自己給的 `--site` 放行等於零驗證）；
   店長沒指定據點或指定了不存在的據點 -> 同樣 deny-all。
   任何情況都不會退回 Full Pack。安全預設值必須是「什麼都看不到」。

3. **輸出前再驗一次（縱深防禦）。**
   `assert_no_leak()` 把即將送出的 payload / 報表文字序列化後，掃描是否出現
   任何不可見據點的識別碼或店名。第 1 條已經保證不會發生，第 3 條負責在
   未來有人改壞第 1 條時當場炸掉，而不是安靜地把別店數字寄給店長。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Protocol

#: 兩種輸出包型態（apxG_p17 原文用語）。
PACK_FULL = "full_pack"
PACK_OWN_SITE = "own_site_only"


class Role(Enum):
    """兩級權限角色。刻意只有兩級——書中規格就是兩級，多開一級就多一條外洩路徑。"""

    HQ = "hq"
    SITE_MANAGER = "site_manager"


class LeakDetected(RuntimeError):
    """輸出中出現了不可見據點的識別碼或店名。這是嚴重錯誤，不可降級為警告。"""


class DiagnosticsLike(Protocol):
    """只用到 `Diagnostics.amber()`，用 Protocol 讓測試能塞假物件。"""

    def amber(self, symptom: str, fix: str) -> None: ...


@dataclass(frozen=True)
class AccessScope:
    """一次執行的可見範圍。建立後不可變——中途被改寫等同權限被提權。"""

    role: Role
    actor: str
    requested_site_id: str | None
    visible_site_ids: frozenset[str]
    pack: str
    denial_reason: str | None = None
    #: 角色字串無法辨識。這種呼叫可能是設定錯誤，也可能是探測行為，必須留痕。
    role_was_unknown: bool = False

    @property
    def is_denied(self) -> bool:
        """可見集合為空即為拒絕存取（最小權限的終點）。"""
        return not self.visible_site_ids

    @property
    def can_see_ranking(self) -> bool:
        """跨據點排名只在總部視野提供——排名本身就會洩漏他店相對位置。"""
        return self.role is Role.HQ

    @property
    def can_see_peer_comparison(self) -> bool:
        """同儕比較同上：中位數是由他店數字算出來的，店長不得取得。"""
        return self.role is Role.HQ

    def is_visible(self, site_id: str) -> bool:
        """單一據點是否在可見集合內。"""
        return site_id in self.visible_site_ids

    def filter_registry(self, entries: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """裁切據點名冊。**取數迴圈只能吃這個函式的輸出**，不可直接吃原始名冊。"""
        return [dict(entry) for entry in entries if self.is_visible(str(entry.get("site_id", "")))]

    def filter_mapping(self, mapping: Mapping[str, Any]) -> dict[str, Any]:
        """裁切以 site_id 為鍵的字典（基準線狀態檔就是這種形狀）。"""
        return {key: value for key, value in mapping.items() if self.is_visible(str(key))}

    def to_dict(self) -> dict[str, Any]:
        """稽核日誌與回傳結果用的可序列化形式。"""
        return {
            "role": self.role.value,
            "actor": self.actor,
            "requested_site_id": self.requested_site_id,
            "pack": self.pack,
            "visible_site_ids": sorted(self.visible_site_ids),
            "is_denied": self.is_denied,
            "denial_reason": self.denial_reason,
            "role_was_unknown": self.role_was_unknown,
        }


def _normalise(value: Any) -> str:
    """統一把 CLI / YAML 傳來的值收斂成去空白的小寫字串。"""
    return "" if value is None else str(value).strip().lower()


def parse_role(raw: Any) -> tuple[Role, bool]:
    """把字串轉成 `(角色, 是否可辨識)`。

    無法辨識時第一個值降為 `SITE_MANAGER`，但第二個值為 False——呼叫端**必須**據此
    強制 deny-all，不可只靠「降級成店長」了事。理由見 `resolve_scope` 的說明。

    本函式刻意不吃 `diagnostics`：它是純函式，警示由 `resolve_scope` 統一發出，
    避免同一次解析在兩個地方各印一則語意重疊的琥珀燈。
    """
    try:
        return Role(_normalise(raw)), True
    except ValueError:
        return Role.SITE_MANAGER, False


def resolve_scope(
    role_raw: Any,
    site_raw: Any,
    actor_raw: Any,
    registry_site_ids: Iterable[str],
    diagnostics: DiagnosticsLike | None = None,
) -> AccessScope:
    """算出本次執行的可見據點集合。這是整個模組唯一有權放行資料的地方。

    **角色無法辨識時一律 deny-all，不論 `--site` 給了什麼。**

    `--site` 是呼叫者自己給的參數、沒有經過任何驗證；角色認不出來，就等於不知道
    這個呼叫者有權存取哪一家店。此時若還照著 `--site` 放行，等於「你說要看 X 就
    給你 X」——任何人只要亂打一個角色名加上一個有效店碼，就能拿到那家店的完整
    營運資料，而且他根本不需要知道任何合法角色名稱。

    對「不存在的據點」既然已經 deny-all，對「不認得的角色」就必須一樣嚴格：
    **不知道你是誰，就什麼都不給。**
    """
    all_ids = frozenset(str(item) for item in registry_site_ids)
    role, is_recognised_role = parse_role(role_raw)
    actor = str(actor_raw).strip() if actor_raw else f"unknown:{role.value}"

    if not is_recognised_role:
        # 刻意不在訊息中列出合法角色清單：那等於直接告訴探測者正確答案是什麼。
        return _denied(
            actor=actor,
            site_id=str(site_raw).strip() if site_raw else None,
            diagnostics=diagnostics,
            reason=f"角色 {role_raw!r} 無法辨識，已拒絕所有存取",
            role_was_unknown=True,
        )

    if role is Role.HQ:
        return AccessScope(
            role=role,
            actor=actor,
            requested_site_id=None,
            visible_site_ids=all_ids,
            pack=PACK_FULL,
        )
    return _resolve_site_manager(actor, site_raw, all_ids, diagnostics)


def _resolve_site_manager(
    actor: str,
    site_raw: Any,
    all_ids: frozenset[str],
    diagnostics: DiagnosticsLike | None,
) -> AccessScope:
    """店長視野：只認一家店，認不出來就 deny-all，絕不放寬。"""
    site_id = str(site_raw).strip() if site_raw else ""

    if not site_id:
        return _denied(actor, None, diagnostics, "店長角色未指定 --site，無法判定該看哪一家")
    if site_id not in all_ids:
        # 刻意不在訊息中列出合法據點清單：那等於對未授權者洩漏全網名冊。
        return _denied(actor, site_id, diagnostics, f"據點 {site_id!r} 不在名冊中或未授權")

    return AccessScope(
        role=Role.SITE_MANAGER,
        actor=actor,
        requested_site_id=site_id,
        visible_site_ids=frozenset({site_id}),
        pack=PACK_OWN_SITE,
    )


def _denied(
    actor: str,
    site_id: str | None,
    diagnostics: DiagnosticsLike | None,
    reason: str,
    role_was_unknown: bool = False,
) -> AccessScope:
    """建立 deny-all 範圍：可見集合為空，後續取數迴圈自然一筆都不會跑。

    `role_was_unknown` 會一路帶進 `to_dict()` 與稽核軌跡：角色認不出來的呼叫
    可能是設定錯誤，也可能是有人在試探角色名稱，兩者都必須留痕。
    """
    if diagnostics is not None:
        diagnostics.amber(
            f"權限解析失敗（{reason}），本次可見據點為空集合",
            "確認 --role / --site 是否正確；系統設計上寧可產出空報表，也不放寬為 Full Pack",
        )
    return AccessScope(
        role=Role.SITE_MANAGER,
        actor=actor,
        requested_site_id=site_id,
        visible_site_ids=frozenset(),
        pack=PACK_OWN_SITE,
        denial_reason=reason,
        role_was_unknown=role_was_unknown,
    )


def hidden_identifiers(
    scope: AccessScope,
    registry: Iterable[Mapping[str, Any]],
) -> list[str]:
    """列出本次**不可見**據點的識別碼與店名，供洩漏掃描比對用。"""
    tokens: list[str] = []
    for entry in registry:
        site_id = str(entry.get("site_id", "")).strip()
        if not site_id or scope.is_visible(site_id):
            continue
        tokens.append(site_id)
        display_name = str(entry.get("display_name", "")).strip()
        if display_name:
            tokens.append(display_name)
    return tokens


def assert_no_leak(payload: Any, forbidden_tokens: Iterable[str]) -> None:
    """縱深防禦：payload 序列化後若含任何不可見據點的識別碼/店名就立刻拋錯。

    刻意用 `LeakDetected` 而不是回傳 False——外洩不是可以「記個警告繼續送」的事，
    呼叫端必須被迫處理。`default=str` 讓 Decimal 等型別也能被掃到。
    """
    haystack = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str)
    hits = sorted({token for token in forbidden_tokens if token and token in haystack})
    if hits:
        raise LeakDetected(f"輸出中偵測到不可見據點的識別資訊：{'、'.join(hits)}")
