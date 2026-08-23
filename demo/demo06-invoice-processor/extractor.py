"""發票欄位提取器（第 03 章 The Brain 前半段）。

從發票純文字抽出四個關鍵欄位：廠商、金額、日期、稅額，並做稅額合理性驗證。

設計鐵律：
1. 金額一律用 decimal.Decimal，全程禁止 float —— 財務尾差會累積成對不上的帳。
2. 提取信心不足（缺欄位或稅額對不上）一律標 needs_review，**不得**送進會計系統。
   寧可讓帳務經理花 2 分鐘覆核，也不要把錯誤金額寫進客戶的 Xero。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, DivisionByZero, InvalidOperation
from pathlib import Path
from typing import Any

# 金額必須帶兩位小數才視為可信。模糊掃描常把 0 認成字母 O（如 "1,240.O0"），
# 這個嚴格 pattern 會直接解析失敗 —— 刻意的保守設計，失敗好過猜錯金額。
_AMOUNT = r"(\d{1,3}(?:,\d{3})*\.\d{2})"

_VENDOR_RE = re.compile(r"^\s*Vendor\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_DESC_RE = re.compile(r"^\s*Description\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_DATE_RE = re.compile(r"^\s*(?:Invoice\s+)?Date\s*:\s*(\S+)\s*$", re.MULTILINE | re.IGNORECASE)
_SUBTOTAL_RE = re.compile(rf"^\s*Subtotal\s*:\s*{_AMOUNT}", re.MULTILINE | re.IGNORECASE)
_TAX_RE = re.compile(
    rf"^\s*(?:Sales\s+Tax|VAT|GST|Tax)[^:\n]*:\s*{_AMOUNT}", re.MULTILINE | re.IGNORECASE
)
_TOTAL_RE = re.compile(rf"^\s*Total(?:\s+Due)?\s*:\s*{_AMOUNT}", re.MULTILINE | re.IGNORECASE)
_TOTAL_CURRENCY_RE = re.compile(
    rf"^\s*Total(?:\s+Due)?\s*:\s*{_AMOUNT}\s*([A-Za-z]{{3}})", re.MULTILINE | re.IGNORECASE
)

# 欄位代碼 -> 給人看的繁中標籤（診斷訊息用）
_FIELD_LABELS: dict[str, str] = {
    "vendor": "廠商",
    "invoice_date": "發票日期",
    "total_amount": "總金額",
    "tax_amount": "稅額",
    "subtotal": "小計",
}


@dataclass
class ExtractedInvoice:
    """單張發票的提取結果。金額欄位一律 Decimal 或 None。"""

    filename: str
    vendor: str | None = None
    description: str | None = None
    invoice_date: str | None = None
    subtotal: Decimal | None = None
    tax_amount: Decimal | None = None
    total_amount: Decimal | None = None
    currency: str = "GBP"
    is_foreign_currency: bool = False
    needs_review: bool = False
    issues: list[str] = field(default_factory=list)
    standard_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """轉成可 JSON 序列化的 dict；Decimal 一律轉字串保留精度。"""
        return {
            "filename": self.filename,
            "vendor": self.vendor,
            "description": self.description,
            "invoice_date": self.invoice_date,
            "subtotal": decimal_to_str(self.subtotal),
            "tax_amount": decimal_to_str(self.tax_amount),
            "total_amount": decimal_to_str(self.total_amount),
            "currency": self.currency,
            "is_foreign_currency": self.is_foreign_currency,
            "needs_review": self.needs_review,
            "issues": list(self.issues),
            "standard_name": self.standard_name,
        }


def decimal_to_str(value: Decimal | None) -> str | None:
    """Decimal 轉兩位小數字串；None 原樣回傳。"""
    return None if value is None else f"{value:.2f}"


def to_decimal(raw: str | None) -> Decimal | None:
    """把 "1,234.56" 轉成 Decimal。無法解析回傳 None（呼叫端負責標 needs_review）。"""
    if raw is None:
        return None
    try:
        return Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def parse_invoice_date(raw: str | None) -> str | None:
    """支援 YYYY-MM-DD / DD/MM/YYYY / DD-MM-YYYY，統一輸出 ISO 字串。"""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _first_group(regex: re.Pattern[str], text: str) -> str | None:
    """回傳第一個命中的 group(1)，沒命中回 None。"""
    match = regex.search(text)
    return match.group(1) if match else None


def parse_invoice_text(raw_text: str, default_currency: str) -> dict[str, Any]:
    """規則式解析（mock 模式的 The Brain）。只負責抽欄位，不做合理性驗證。"""
    currency_match = _TOTAL_CURRENCY_RE.search(raw_text)
    currency = currency_match.group(2) if currency_match else default_currency
    return {
        "vendor": _first_group(_VENDOR_RE, raw_text),
        "description": _first_group(_DESC_RE, raw_text),
        "invoice_date": parse_invoice_date(_first_group(_DATE_RE, raw_text)),
        "subtotal": to_decimal(_first_group(_SUBTOTAL_RE, raw_text)),
        "tax_amount": to_decimal(_first_group(_TAX_RE, raw_text)),
        "total_amount": to_decimal(_first_group(_TOTAL_RE, raw_text)),
        "currency": str(currency).upper(),
    }


def parse_with_llm(
    raw_text: str, llm: Any, prompt_path: Path, default_currency: str
) -> dict[str, Any]:
    """live 模式：交給 Claude 提取，要求嚴格 JSON（schema 見 prompts/extract_invoice.md）。

    這裡不吞例外 —— JSON 壞掉必須讓呼叫端知道並降級為規則式解析 + needs_review。
    """
    system_prompt = Path(prompt_path).read_text(encoding="utf-8")
    payload = json.loads(llm.complete(system=system_prompt, user=raw_text, max_tokens=800))
    if not isinstance(payload, dict):
        raise ValueError(f"提取結果不是 JSON 物件：{type(payload).__name__}")
    currency = str(payload.get("currency") or default_currency)
    return {
        "vendor": payload.get("vendor"),
        "description": payload.get("description"),
        "invoice_date": parse_invoice_date(payload.get("invoice_date")),
        "subtotal": to_decimal(payload.get("subtotal")),
        "tax_amount": to_decimal(payload.get("tax_amount")),
        "total_amount": to_decimal(payload.get("total_amount")),
        "currency": currency.upper(),
    }


def check_required_fields(invoice: ExtractedInvoice, settings: dict[str, Any]) -> list[str]:
    """必填欄位缺一不可，缺了就是信心不足。"""
    required = settings.get("required_fields") or [
        "vendor",
        "invoice_date",
        "total_amount",
        "tax_amount",
    ]
    return [
        f"缺少欄位：{_FIELD_LABELS.get(name, name)}"
        for name in required
        if getattr(invoice, name, None) in (None, "")
    ]


def check_tax_consistency(invoice: ExtractedInvoice, settings: dict[str, Any]) -> list[str]:
    """稅額合理性：小計＋稅額須等於總額，且隱含稅率須落在允許清單內。"""
    total, tax = invoice.total_amount, invoice.tax_amount
    if total is None or tax is None:
        return []  # 缺欄位已由 check_required_fields 回報，不重複記帳
    issues: list[str] = []
    tolerance = Decimal(str(settings.get("tax_tolerance", "0.02")))
    if invoice.subtotal is not None and abs(invoice.subtotal + tax - total) > tolerance:
        issues.append(f"小計 {invoice.subtotal} ＋ 稅額 {tax} 不等於總額 {total}")
    net = total - tax
    if net <= 0:
        issues.append(f"稅前金額異常（{net}），無法驗證稅率")
        return issues
    issues.extend(_check_tax_rate(tax, net, settings))
    return issues


def _check_tax_rate(tax: Decimal, net: Decimal, settings: dict[str, Any]) -> list[str]:
    """隱含稅率是否落在 config 的 allowed_tax_rates 內（含容差）。"""
    allowed = [Decimal(str(rate)) for rate in settings.get("allowed_tax_rates", [0, 0.05, 0.2])]
    rate_tolerance = Decimal(str(settings.get("tax_rate_tolerance", "0.005")))
    try:
        implied = (tax / net).quantize(Decimal("0.0001"))
    except (InvalidOperation, DivisionByZero) as exc:
        return [f"稅率計算失敗：{exc}"]
    if any(abs(implied - rate) <= rate_tolerance for rate in allowed):
        return []
    readable = "、".join(f"{rate:.2%}" for rate in allowed)
    return [f"隱含稅率 {implied:.2%} 不在允許清單（{readable}）"]


def slugify_vendor(vendor: str) -> str:
    """廠商名轉檔名安全片段：保留中英數，其餘壓成連字號。"""
    slug = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", vendor).strip("-")
    return slug or "unknown-vendor"


def build_standard_name(invoice: ExtractedInvoice, settings: dict[str, Any]) -> str:
    """標準化檔名：YYYY-MM-DD_<廠商>_<金額><幣別>.pdf"""
    template = settings.get("filename_template", "{date}_{vendor}_{amount}{currency}.pdf")
    return template.format(
        date=invoice.invoice_date,
        vendor=slugify_vendor(invoice.vendor or ""),
        amount=f"{invoice.total_amount:.2f}",
        currency=invoice.currency,
    )


def extract_invoice(
    record: dict[str, Any],
    settings: dict[str, Any],
    llm: Any | None = None,
    prompt_path: Path | None = None,
) -> ExtractedInvoice:
    """提取單張發票。llm 有給就走 Claude，失敗自動降級為規則式解析並標記問題。"""
    raw_text = str(record.get("raw_text", ""))
    default_currency = str(settings.get("default_currency", "GBP"))
    issues: list[str] = []
    if llm is not None and prompt_path is not None:
        try:
            fields = parse_with_llm(raw_text, llm, prompt_path, default_currency)
        except (json.JSONDecodeError, ValueError, OSError, RuntimeError) as exc:
            issues.append(f"Claude 提取失敗，已降級為規則式解析：{exc}")
            fields = parse_invoice_text(raw_text, default_currency)
    else:
        fields = parse_invoice_text(raw_text, default_currency)

    invoice = ExtractedInvoice(filename=str(record.get("filename", "unknown.pdf")), **fields)
    invoice.is_foreign_currency = invoice.currency != default_currency.upper()
    issues.extend(check_required_fields(invoice, settings))
    issues.extend(check_tax_consistency(invoice, settings))
    invoice.issues = issues
    invoice.needs_review = bool(issues)
    invoice.standard_name = None if invoice.needs_review else build_standard_name(invoice, settings)
    return invoice


def load_mock_invoices(path: str | Path) -> list[dict[str, Any]]:
    """讀取 mock 發票資料（不產生真的 PDF，raw_text 已是郵件閘道轉出的純文字）。"""
    invoice_path = Path(path).resolve()
    if not invoice_path.is_file():
        raise FileNotFoundError(f"找不到 mock 發票資料：{invoice_path}")
    try:
        payload = json.loads(invoice_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"mock 發票資料 JSON 格式錯誤（{invoice_path}）：{exc}") from exc
    records = payload.get("invoices") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError(f"mock 發票資料應為 list 或含 invoices 欄位的物件：{invoice_path}")
    return records
