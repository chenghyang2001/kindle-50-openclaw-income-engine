"""模組 #30 — 多租戶隔離層（namespace 解析、跨租戶拒絕、路徑跳脫防禦）。

規格來源（apxG_p19）：每個子客戶以 `[RESELLER_SLUG]/[SUB_CLIENT_SLUG_X]`
namespace 完全隔離，原文要求「確保資料絕對隔離」。

設計立場：**寧可拒絕，不要猜測。**
namespace 解析失敗、缺失、或含路徑跳脫字元時一律拒絕執行，不做任何「順手修正」
（例如自動轉小寫、自動剝掉 `..`）。修正等於幫攻擊者把畸形輸入補成合法輸入，
而白牌場景一旦跨租戶外洩，毀掉的是經銷商與基礎設施提供者兩層信譽。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# slug 允許：小寫英數與連字號，頭尾必須是英數，長度 2-40。
# 不允許大寫是刻意的——大寫會誘使實作者「順手 .lower()」，那就是一種修正。
SLUG_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])$")

# 明確列出跳脫字元，是為了讓拒絕訊息能指出「哪一個」片段有問題，
# 而不是只丟一句「格式錯誤」讓維運人員瞎猜。
FORBIDDEN_FRAGMENTS = ("..", "\\", "//", "~", ":", "%", "\x00")

SEPARATOR = "/"


class TenancyError(RuntimeError):
    """租戶設定或資料存取違規的基底例外。"""


class NamespaceError(TenancyError):
    """namespace 無法解析、缺失、或含路徑跳脫字元。"""


class IsolationViolation(TenancyError):
    """跨租戶存取嘗試——本模組最高層級的安全事件。"""

    def __init__(self, message: str, actor: str, target: str) -> None:
        super().__init__(message)
        self.actor = actor
        self.target = target


@dataclass(frozen=True)
class Namespace:
    """不可變的租戶座標。凍結是刻意的：解析完成後不允許任何人再改寫。"""

    reseller_slug: str
    sub_client_slug: str | None = None

    @property
    def path(self) -> str:
        """回傳 `reseller` 或 `reseller/sub_client` 形式的正規字串。"""
        if self.sub_client_slug is None:
            return self.reseller_slug
        return f"{self.reseller_slug}{SEPARATOR}{self.sub_client_slug}"

    @property
    def is_reseller_scope(self) -> bool:
        """True 代表這是經銷商層級（沒有指定子客戶）。"""
        return self.sub_client_slug is None

    def __str__(self) -> str:
        return self.path


def _reject(reason: str, raw: Any) -> NamespaceError:
    """統一組出拒絕訊息，附上原始輸入以利稽核追查。"""
    return NamespaceError(f"namespace 遭拒（{reason}）：{raw!r}")


def _validate_slug(slug: str, field: str, raw: Any) -> str:
    """驗證單一 slug；不符即拋錯，絕不修正。"""
    for fragment in FORBIDDEN_FRAGMENTS:
        if fragment in slug:
            raise _reject(f"{field} 含禁用片段 {fragment!r}", raw)
    if not SLUG_PATTERN.match(slug):
        raise _reject(f"{field} 不符 slug 規則（小寫英數與連字號，長度 2-40）", raw)
    return slug


def parse_namespace(raw: Any, require_sub_client: bool = True) -> Namespace:
    """把 `reseller/sub_client` 字串解析成 Namespace。

    require_sub_client=False 時允許只給經銷商層級（供 `--tenant acme-ops` 用）。
    空白、缺失、多段、跳脫字元一律拒絕。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise _reject("空值或非字串", raw)
    if raw != raw.strip():
        raise _reject("含前後空白（不自動修剪）", raw)
    for fragment in FORBIDDEN_FRAGMENTS:
        if fragment in raw:
            raise _reject(f"含禁用片段 {fragment!r}", raw)
    parts = raw.split(SEPARATOR)
    if len(parts) == 1:
        if require_sub_client:
            raise _reject("缺少子客戶層級", raw)
        return Namespace(_validate_slug(parts[0], "reseller_slug", raw))
    if len(parts) != 2:
        raise _reject(f"層級數必須為 2，實得 {len(parts)}", raw)
    reseller = _validate_slug(parts[0], "reseller_slug", raw)
    sub_client = _validate_slug(parts[1], "sub_client_slug", raw)
    return Namespace(reseller, sub_client)


def safe_child_path(root: Path, namespace: Namespace) -> Path:
    """把 namespace 組成資料檔路徑，並保證結果仍落在 root 之內。

    先組合再 `resolve()` 後驗證，而不是只檢查字串——symlink 與作業系統層級的
    路徑正規化都可能讓「看起來安全」的字串指到 root 之外。
    """
    if namespace.is_reseller_scope:
        raise NamespaceError(f"資料檔存取必須指定子客戶層級：{namespace.path}")
    root_resolved = Path(root).expanduser().resolve()
    leaf = f"{namespace.sub_client_slug}.json"
    candidate = (root_resolved / namespace.reseller_slug / leaf).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise IsolationViolation(
            f"路徑跳脫：{candidate} 不在允許根目錄 {root_resolved} 內",
            actor=namespace.path,
            target=str(candidate),
        )
    return candidate


def build_registry(payload: Any) -> dict[str, dict]:
    """把 tenants.json 轉成 {namespace_path: 租戶設定} 的索引。

    每一筆都在此刻就解析 namespace——寧可在啟動階段整批失敗，
    也不要等跑到一半才發現某個租戶的 slug 是畸形的。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("resellers"), list):
        raise TenancyError("tenants.json 結構錯誤：頂層需為含 resellers 陣列的物件")
    registry: dict[str, dict] = {}
    for reseller in payload["resellers"]:
        raw_slug = reseller.get("reseller_slug")
        reseller_slug = _validate_slug(str(raw_slug or ""), "reseller_slug", raw_slug)
        for sub in reseller.get("sub_clients") or []:
            raw_ns = f"{reseller_slug}{SEPARATOR}{sub.get('sub_client_slug')}"
            namespace = parse_namespace(raw_ns)
            if namespace.path in registry:
                raise TenancyError(f"重複的租戶 namespace：{namespace.path}")
            registry[namespace.path] = {
                "namespace": namespace,
                "reseller": reseller,
                "sub_client": sub,
            }
    if not registry:
        raise TenancyError("tenants.json 未包含任何子客戶")
    return registry


class TenantStore:
    """租戶資料的唯一入口。所有讀取都必須帶 actor namespace。

    刻意不提供「不帶 actor」的讀取方法：只要留一條後門，
    日後就一定會有人為了方便而走那條後門。
    """

    def __init__(self, data_root: Path, audit: Any | None = None) -> None:
        self._data_root = Path(data_root).expanduser().resolve()
        self._audit = audit
        self._denied: list[dict] = []

    @property
    def data_root(self) -> Path:
        """允許存取的資料根目錄（絕對路徑）。"""
        return self._data_root

    @property
    def denied(self) -> list[dict]:
        """本次執行被拒絕的存取嘗試（供結果回報與測試斷言）。"""
        return list(self._denied)

    def _log(self, event: str, severity: str, actor: str, target: str, reason: str) -> None:
        """把存取判定寫進稽核日誌；沒掛 audit 時安靜略過（供單元測試直呼）。"""
        if self._audit is None:
            return
        self._audit.record(
            event=event,
            severity=severity,
            actor=actor,
            namespace=target,
            detail={"reason": reason},
        )

    def _deny(self, actor: Namespace, target: Namespace, reason: str) -> IsolationViolation:
        """記錄一次拒絕並回傳待拋出的例外（拒絕一定留痕）。"""
        entry = {"actor": actor.path, "target": target.path, "reason": reason}
        self._denied.append(entry)
        self._log("cross_tenant_denied", "red", actor.path, target.path, reason)
        return IsolationViolation(
            f"跨租戶存取遭拒：{actor.path} → {target.path}（{reason}）",
            actor=actor.path,
            target=target.path,
        )

    def read(self, actor: Namespace, target: Namespace) -> dict:
        """以 actor 身分讀取 target 的資料；不同租戶一律拒絕。"""
        if actor.reseller_slug != target.reseller_slug:
            raise self._deny(actor, target, "經銷商層級不符")
        if actor.is_reseller_scope or target.is_reseller_scope:
            raise self._deny(actor, target, "資料讀取必須指定子客戶層級")
        if actor.sub_client_slug != target.sub_client_slug:
            raise self._deny(actor, target, "子客戶層級不符")
        return self._load(actor, target)

    def _load(self, actor: Namespace, target: Namespace) -> dict:
        """實際讀檔。路徑跳脫與 namespace 不符都視為安全事件而非小錯。"""
        try:
            path = safe_child_path(self._data_root, target)
        except IsolationViolation:
            raise self._deny(actor, target, "路徑跳脫")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TenancyError(f"租戶資料無法讀取：{path}｜{exc}") from exc
        if str(payload.get("namespace") or "") != target.path:
            raise self._deny(actor, target, "資料檔內宣告的 namespace 與請求不符")
        self._log("tenant_read", "green", actor.path, target.path, "同租戶讀取")
        return payload
