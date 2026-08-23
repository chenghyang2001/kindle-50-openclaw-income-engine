"""會計科目對應與發布（第 03 章 The Hands）。

負責兩件事：
1. 依 config.yaml 的關鍵字規則把發票對應到會計科目。
2. 把已通過驗證的發票推進 Xero / QuickBooks；mock 模式改寫入 mock/posted.json。

安全鐵律：``needs_review`` 的發票永遠不會被發布 —— 這是本模組唯一不可協商的行為。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from extractor import ExtractedInvoice, decimal_to_str


class AccountingError(RuntimeError):
    """會計系統設定或發布失敗。"""


@dataclass
class PostingResult:
    """單張發票的發布結果（狀態機：posted / draft / dry_run / needs_review / failed）。"""

    filename: str
    vendor: str | None
    account_code: str
    account_name: str
    amount: str | None
    currency: str
    standard_name: str | None
    system: str
    status: str = "posted"
    reason: str = ""
    matched_keyword: str = ""

    def to_dict(self) -> dict[str, Any]:
        """轉成可 JSON 序列化的 dict。"""
        return {
            "filename": self.filename,
            "vendor": self.vendor,
            "account_code": self.account_code,
            "account_name": self.account_name,
            "amount": self.amount,
            "currency": self.currency,
            "standard_name": self.standard_name,
            "system": self.system,
            "status": self.status,
            "reason": self.reason,
            "matched_keyword": self.matched_keyword,
        }


@dataclass
class _LiveEndpoint:
    """live 模式所需的憑證環境變數與端點樣板。"""

    token_env: str
    tenant_env: str
    url_template: str


class AccountingPoster:
    """把分類後的發票送進會計系統（或在 mock 模式寫成本地帳冊）。"""

    SUPPORTED_SYSTEMS = ("xero", "quickbooks")
    _ENDPOINTS: dict[str, _LiveEndpoint] = {
        "xero": _LiveEndpoint(
            token_env="XERO_ACCESS_TOKEN",
            tenant_env="XERO_TENANT_ID",
            url_template="https://api.xero.com/api.xro/2.0/Invoices",
        ),
        "quickbooks": _LiveEndpoint(
            token_env="QUICKBOOKS_ACCESS_TOKEN",
            tenant_env="QUICKBOOKS_REALM_ID",
            url_template="https://quickbooks.api.intuit.com/v3/company/{tenant}/purchase",
        ),
    }

    def __init__(
        self,
        config: dict[str, Any],
        mock: bool = True,
        dry_run: bool = False,
        diagnostics: Any | None = None,
        output_path: str | Path | None = None,
        timeout: int = 30,
    ) -> None:
        """config 為 config.yaml 的 accounting 區塊。system 不合法直接拋錯，不做靜默預設。"""
        system = str(config.get("system", "xero")).lower()
        if system not in self.SUPPORTED_SYSTEMS:
            raise AccountingError(
                f"不支援的會計系統：{system}（可用：{'/'.join(self.SUPPORTED_SYSTEMS)}）"
            )
        self.system = system
        self.mock = mock
        self.dry_run = dry_run
        self.timeout = timeout
        self._diagnostics = diagnostics
        self._rules: list[dict[str, Any]] = list(config.get("rules") or [])
        self._default_account: dict[str, Any] = dict(
            config.get("default_account") or {"code": "6999", "name": "待分類支出"}
        )
        self._results: list[PostingResult] = []
        self.output_path = Path(output_path) if output_path else Path("mock/posted.json")

    @property
    def results(self) -> list[PostingResult]:
        """目前累積的發布結果。"""
        return list(self._results)

    def classify(self, invoice: ExtractedInvoice) -> tuple[str, str, str]:
        """依關鍵字規則對應科目，回傳 (科目代碼, 科目名稱, 命中的關鍵字)。"""
        haystack = f"{invoice.vendor or ''} {invoice.description or ''}".lower()
        for rule in self._rules:
            for keyword in rule.get("keywords") or []:
                if str(keyword).lower() in haystack:
                    return str(rule.get("code", "")), str(rule.get("name", "")), str(keyword)
        return (
            str(self._default_account.get("code", "6999")),
            str(self._default_account.get("name", "待分類支出")),
            "",
        )

    def post(self, invoice: ExtractedInvoice, can_post: bool = True) -> PostingResult:
        """發布單張發票。四道閘門依序把關：信心 -> dry-run -> 自主權 -> mock/live。"""
        code, name, keyword = self.classify(invoice)
        result = PostingResult(
            filename=invoice.filename,
            vendor=invoice.vendor,
            account_code=code,
            account_name=name,
            amount=decimal_to_str(invoice.total_amount),
            currency=invoice.currency,
            standard_name=invoice.standard_name,
            system=self.system,
            matched_keyword=keyword,
        )
        result.status, result.reason = self._decide(invoice, code, can_post)
        self._results.append(result)
        return result

    def _decide(
        self, invoice: ExtractedInvoice, account_code: str, can_post: bool
    ) -> tuple[str, str]:
        """決定這張發票的最終狀態。順序不可調換：信心不足永遠優先於一切。"""
        if invoice.needs_review:
            return "needs_review", "；".join(invoice.issues) or "提取信心不足"
        if self.dry_run:
            return "dry_run", "--dry-run：流程跑完但未發布"
        if not can_post:
            return "draft", "自主權未達 supervised_auto，僅建立草稿待人工送出"
        if self.mock:
            return "posted", f"mock：已寫入 {self.output_path.name}"
        return self._post_live(invoice, account_code)

    def post_batch(
        self, invoices: list[ExtractedInvoice], can_post: bool = True
    ) -> list[PostingResult]:
        """批次發布，回傳與輸入等長的結果清單。"""
        return [self.post(invoice, can_post=can_post) for invoice in invoices]

    def _build_payload(self, invoice: ExtractedInvoice, account_code: str) -> dict[str, Any]:
        """組出兩家會計系統共用的最小費用單 payload（欄位命名依 Xero 慣例）。"""
        # 走到這裡代表已通過 needs_review 驗證，金額必定存在；or 只是防禦性保險。
        net_amount = (invoice.total_amount or Decimal("0")) - (invoice.tax_amount or Decimal("0"))
        return {
            "Type": "ACCPAY",
            "Contact": {"Name": invoice.vendor},
            "Date": invoice.invoice_date,
            "CurrencyCode": invoice.currency,
            "Reference": invoice.standard_name,
            "LineItems": [
                {
                    "Description": invoice.description or invoice.filename,
                    "AccountCode": account_code,
                    "Quantity": 1,
                    "UnitAmount": decimal_to_str(net_amount),
                    "TaxAmount": decimal_to_str(invoice.tax_amount),
                }
            ],
        }

    def _post_live(self, invoice: ExtractedInvoice, account_code: str) -> tuple[str, str]:
        """真的呼叫會計系統 API。憑證缺失走 Diagnostics 紅色警報，不用預設值掩蓋。"""
        endpoint = self._ENDPOINTS[self.system]
        token = os.environ.get(endpoint.token_env)
        tenant = os.environ.get(endpoint.tenant_env)
        if not token or not tenant:
            self._raise_missing_credentials(endpoint)
            return "failed", f"缺少環境變數 {endpoint.token_env} / {endpoint.tenant_env}"
        request = urllib.request.Request(
            endpoint.url_template.format(tenant=tenant),
            data=json.dumps(self._build_payload(invoice, account_code)).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Xero-Tenant-Id": tenant,
            },
            method="POST",
        )
        return self._send(request, invoice)

    def _send(self, request: urllib.request.Request, invoice: ExtractedInvoice) -> tuple[str, str]:
        """送出請求並把網路層例外轉成明確狀態，絕不靜默吞掉。"""
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return "posted", f"{self.system} 回應 HTTP {response.status}"
        except urllib.error.HTTPError as exc:
            return "failed", f"{invoice.filename} 發布失敗：HTTP {exc.code} {exc.reason}"
        except urllib.error.URLError as exc:
            return "failed", f"{invoice.filename} 連線失敗：{exc.reason}"
        except TimeoutError:
            return "failed", f"{invoice.filename} 逾時（>{self.timeout}s）"

    def _raise_missing_credentials(self, endpoint: _LiveEndpoint) -> None:
        """憑證缺失：有 diagnostics 就走紅色警報，否則直接拋 AccountingError。"""
        symptom = f"{self.system} 憑證缺失，無法發布發票"
        cause = f"環境變數 {endpoint.token_env} 或 {endpoint.tenant_env} 未設定"
        fix = f"執行 setx {endpoint.token_env} <token> 後重開終端機，或改用 --mock 驗證流程"
        if self._diagnostics is not None:
            self._diagnostics.red(symptom, cause, fix)
            return
        raise AccountingError(f"{symptom}：{cause}。修法：{fix}")

    def flush(self) -> Path | None:
        """mock 模式把整批結果寫成本地帳冊；live 模式或無結果時回 None。"""
        if not self.mock or not self._results:
            return None
        payload = {
            "system": self.system,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry_count": len(self._results),
            "entries": [result.to_dict() for result in self._results],
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return self.output_path
