"""demo24 — 匿名化前置處理（BIAS_MITIGATION 的第一道閘門，模組 #24）。

鐵律：匿名化必須在「任何評分動作之前」完成，而且**不可由設定關閉**。
`enforce_bias_switches()` 會把 config 的四個開關與法定值逐一比對，不符即拋錯，
由呼叫端升為紅色警報停機——可以被設定關掉的防線等於沒有防線。

本模組只做四件事：

1. **白名單保留**：只留 `anonymisation.keep_fields` 明列的欄位。
   用白名單而非黑名單，是因為 ATS 的 payload 欄位會被客戶隨時新增；
   黑名單永遠追不上，而漏掉一個新欄位就是一次歧視訴訟的證據。
2. **自由文字洗白**：把被移除欄位的值從自我介紹與經歷內文中洗掉。
   「Hi, I'm Amara」這一句就足以讓整套匿名化破功，這是實務上最常見的破口。
3. **產生匿名識別碼**：sha256(application_id + salt) 前 8 碼，避免由 ID 順序
   反推投遞先後（早投遞者常被無意識地當成「比較積極」）。
4. **鎖進 IdentityVault**：原始履歷只留在保險庫，取出需招募經理具名核准。

`verify_anonymisation()` 是最後一道保險：評分前再掃一次，只要還找得到任何一個
原始受保護值就回報洩漏。寧可不出短名單，也不要出一份帶著姓名的短名單。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable

SALT_ENV = "HR_ANON_SALT"

# 四鐵律的法定值（SPEC #24 / ch07_p07 / apxG_p10）。config 只能「符合」，不能「調整」。
MANDATED_BIAS_SWITCHES: dict[str, Any] = {
    "anonymisation_pass": True,
    "structured_criteria_only": True,
    "shortlist_presentation": "identifiers_only",
    "reveal_requires_manager_approval": True,
}

# 切詞用：拉丁字母（含附加符號）與數字以外一律視為分隔符。
_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-zÀ-ɏ]+")


class BiasMitigationError(RuntimeError):
    """反偏見四鐵律被設定檔改動（管線必須停機，不得降級續跑）。"""


class AnonymisationError(RuntimeError):
    """匿名化本身失敗（缺 application_id、keep/remove 設定互相矛盾等）。"""


class RevealNotAuthorisedError(PermissionError):
    """未經招募經理具名核准就試圖取出真實身分。"""


def enforce_bias_switches(config: dict[str, Any]) -> None:
    """比對 `bias_mitigation` 四開關與法定值，任一不符即拋 BiasMitigationError。"""
    switches = config.get("bias_mitigation") or {}
    breaches = [
        f"{key}={switches.get(key)!r}（法定值 {expected!r}）"
        for key, expected in MANDATED_BIAS_SWITCHES.items()
        if switches.get(key) != expected
    ]
    if breaches:
        raise BiasMitigationError(
            "反偏見鐵律被改動，管線拒絕啟動：" + "；".join(breaches)
        )


@dataclass(frozen=True)
class AnonymisedApplication:
    """一份完成匿名化的申請。`fields` 內不含 application_id，也不含任何受保護欄位。"""

    identifier: str
    fields: dict[str, Any]
    removed_fields: tuple[str, ...]
    dropped_fields: tuple[str, ...]
    redaction_count: int

    def to_dict(self) -> dict[str, Any]:
        """序列化成可寫進報表 / JSON 的結構。"""
        return {
            "identifier": self.identifier,
            "fields": dict(self.fields),
            "removed_fields": list(self.removed_fields),
            "dropped_fields": list(self.dropped_fields),
            "redaction_count": self.redaction_count,
        }


class IdentityVault:
    """匿名識別碼 ↔ 原始履歷的唯一對照表（程序內記憶體，不落地）。

    刻意不提供「列出所有姓名」之類的批次介面：任何一次身分揭露都必須是
    針對單一識別碼、由具名的招募經理發動，且由呼叫端寫進稽核日誌。
    """

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}

    def store(self, identifier: str, application: dict[str, Any]) -> None:
        """存入原始履歷。同一識別碼重複存入視為資料源異常。"""
        if identifier in self._records:
            raise AnonymisationError(f"識別碼重複：{identifier}（application_id 可能有重複投遞）")
        self._records[identifier] = dict(application)

    def has(self, identifier: str) -> bool:
        """保險庫中是否有這個識別碼。"""
        return identifier in self._records

    @property
    def size(self) -> int:
        """保險庫內的履歷數量。"""
        return len(self._records)

    def ats_reference(self, identifier: str) -> str:
        """取出 ATS 參照（application_id）。這不是個資，只是回寫 ATS 用的鍵。"""
        record = self._records.get(identifier)
        if record is None:
            raise AnonymisationError(f"保險庫中沒有識別碼 {identifier}")
        return str(record.get("application_id", ""))

    def reveal(self, identifier: str, approved_by: str, reason: str = "") -> dict[str, Any]:
        """取出真實身分。`approved_by` 必須是具名的招募經理，空字串一律拒絕。"""
        if not isinstance(approved_by, str) or not approved_by.strip():
            raise RevealNotAuthorisedError(
                f"揭露 {identifier} 的身分需要招募經理具名核准（--approved-by），本次未提供"
            )
        record = self._records.get(identifier)
        if record is None:
            raise AnonymisationError(f"保險庫中沒有識別碼 {identifier}")
        revealed = dict(record)
        revealed["_reveal_approved_by"] = approved_by.strip()
        revealed["_reveal_reason"] = reason.strip()
        return revealed


def resolve_salt(anon_cfg: dict[str, Any]) -> str:
    """取得識別碼雜湊用的 salt：環境變數優先，其次 config 的 mock 用預設值。

    salt 不是機密金鑰（只防止識別碼被暴力對照回 application_id），
    因此允許有 fallback；但正式部署仍應設環境變數，讓不同客戶的識別碼互不重疊。
    """
    from_env = os.environ.get(SALT_ENV, "").strip()
    if from_env:
        return from_env
    configured = str(anon_cfg.get("identifier_salt", "")).strip()
    # config_loader 對未設定的 ${VAR} 會保留原樣，這種字串不能當 salt 用。
    if configured and not configured.startswith("${"):
        return configured
    fallback = str(anon_cfg.get("identifier_fallback_salt", "")).strip()
    if not fallback:
        raise AnonymisationError(f"缺少識別碼 salt：請設定環境變數 {SALT_ENV}")
    return fallback


def make_identifier(application_id: str, salt: str, prefix: str = "CAND") -> str:
    """匿名識別碼 = <prefix>-<sha256(application_id + salt) 前 8 碼>。"""
    digest = hashlib.sha256(f"{application_id}::{salt}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:8]}"


def collect_protected_values(
    application: dict[str, Any], anon_cfg: dict[str, Any]
) -> tuple[str, ...]:
    """蒐集所有需要從自由文字中洗掉的原始值（長字串優先，避免先洗掉短的片段）。"""
    min_len = int(anon_cfg.get("min_scrub_length", 3))
    tokenised = set(anon_cfg.get("tokenised_fields") or ())
    values: set[str] = set()
    for name in anon_cfg.get("remove_fields") or ():
        raw = application.get(name)
        # 布林值（如 right_to_work 型別的欄位）洗進內文只會製造誤傷，跳過。
        if raw is None or isinstance(raw, bool):
            continue
        text = str(raw).strip()
        if len(text) >= min_len:
            values.add(text)
        if name in tokenised:
            values.update(part for part in _TOKEN_SPLIT.split(text) if len(part) >= min_len)
    return tuple(sorted(values, key=len, reverse=True))


def secret_pattern(secret: str) -> re.Pattern[str]:
    """把一個受保護值編成「詞邊界」比對式（不分大小寫）。

    為什麼一定要詞邊界：住址「41 Harbour View」會拆出 token `View`，
    純子字串比對會把履歷裡的 `code review` 洗成 `code re[REDACTED]`——
    匿名化反而毀掉了能力證據，分數就跟著失真。
    值的頭尾若不是英數字（如電話 `+44 ...`），該側就不加邊界，否則永遠比不中。
    """
    prefix = r"\b" if secret[:1].isalnum() else ""
    suffix = r"\b" if secret[-1:].isalnum() else ""
    return re.compile(f"{prefix}{re.escape(secret)}{suffix}", re.IGNORECASE)


def scrub_text(text: str, secrets: Iterable[str], token: str) -> tuple[str, int]:
    """把 secrets 逐一從 text 中換成 token（詞邊界比對，不分大小寫）。"""
    result = text
    count = 0
    for secret in secrets:
        result, hits = secret_pattern(secret).subn(token, result)
        count += hits
    return result, count


def _scrub_value(value: Any, secrets: Iterable[str], token: str) -> tuple[Any, int]:
    """對字串或字串清單做洗白；其他型別原樣回傳（自由文字欄位只會是這兩種）。"""
    secrets = tuple(secrets)
    if isinstance(value, str):
        return scrub_text(value, secrets, token)
    if isinstance(value, list):
        cleaned: list[Any] = []
        total = 0
        for item in value:
            item_value, hits = _scrub_value(item, secrets, token)
            cleaned.append(item_value)
            total += hits
        return cleaned, total
    return value, 0


def _validate_field_sets(keep: list[str], remove: list[str]) -> None:
    """keep 與 remove 不得重疊——重疊代表設定者對某欄位的意圖自相矛盾。"""
    overlap = sorted(set(keep) & set(remove))
    if overlap:
        raise AnonymisationError(f"anonymisation.keep_fields 與 remove_fields 重疊：{overlap}")
    if not keep:
        raise AnonymisationError("anonymisation.keep_fields 不可為空（白名單為空等於沒有輸入）")


def anonymise(
    application: dict[str, Any],
    config: dict[str, Any],
    vault: IdentityVault | None = None,
) -> AnonymisedApplication:
    """把單一份原始申請轉成匿名申請，並（可選）把原件鎖進保險庫。"""
    anon_cfg = config.get("anonymisation") or {}
    application_id = str(application.get("application_id") or "").strip()
    if not application_id:
        raise AnonymisationError("申請資料缺少 application_id，無法產生匿名識別碼")

    keep = [str(name) for name in anon_cfg.get("keep_fields") or []]
    remove = [str(name) for name in anon_cfg.get("remove_fields") or []]
    _validate_field_sets(keep, remove)

    secrets = collect_protected_values(application, anon_cfg)
    token = str(anon_cfg.get("redaction_token", "[REDACTED]"))
    free_text = set(anon_cfg.get("free_text_fields") or ())
    fields, redactions = _build_fields(application, keep, free_text, secrets, token)

    identifier = make_identifier(
        application_id, resolve_salt(anon_cfg), str(anon_cfg.get("identifier_prefix", "CAND"))
    )
    if vault is not None:
        vault.store(identifier, application)
    # 白名單以外的欄位一律丟棄，並如實記錄丟了什麼（供稽核與設定檢討）。
    dropped = tuple(sorted(set(application) - set(keep) - set(remove)))
    return AnonymisedApplication(
        identifier=identifier,
        fields=fields,
        removed_fields=tuple(name for name in remove if name in application),
        dropped_fields=dropped,
        redaction_count=redactions,
    )


def _build_fields(
    application: dict[str, Any],
    keep: list[str],
    free_text: set[str],
    secrets: tuple[str, ...],
    token: str,
) -> tuple[dict[str, Any], int]:
    """依白名單挑欄位，自由文字欄位順手洗白。"""
    fields: dict[str, Any] = {}
    redactions = 0
    for name in keep:
        if name not in application:
            continue
        value = application[name]
        if name in free_text:
            value, hits = _scrub_value(value, secrets, token)
            redactions += hits
        fields[name] = value
    return fields, redactions


def anonymise_all(
    applications: list[dict[str, Any]],
    config: dict[str, Any],
    vault: IdentityVault | None = None,
) -> list[AnonymisedApplication]:
    """批次匿名化。任何一份失敗即整批中止——半匿名的批次比完全沒跑更危險。"""
    return [anonymise(application, config, vault) for application in applications]


def verify_anonymisation(
    anonymised: AnonymisedApplication, secrets: Iterable[str]
) -> list[str]:
    """評分前的最後檢查：回傳仍能在匿名資料中找到的原始受保護值。

    回傳空清單才代表通過。呼叫端必須把非空結果視為紅色警報，不可只記警告。
    """
    haystack = repr(anonymised.fields)
    # 與 scrub_text 用同一套詞邊界規則，否則「洗白正確」卻被誤判成洩漏。
    return [secret for secret in secrets if secret and secret_pattern(secret).search(haystack)]


def verify_batch(
    pairs: Iterable[tuple[AnonymisedApplication, dict[str, Any]]], config: dict[str, Any]
) -> dict[str, list[str]]:
    """對「匿名件 / 原始件」配對批次驗證，回傳 {識別碼: 洩漏值清單}（只收非空的）。"""
    anon_cfg = config.get("anonymisation") or {}
    leaks: dict[str, list[str]] = {}
    for anonymised, original in pairs:
        found = verify_anonymisation(anonymised, collect_protected_values(original, anon_cfg))
        if found:
            leaks[anonymised.identifier] = found
    return leaks
