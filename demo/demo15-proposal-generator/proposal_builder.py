"""demo15 提案文件組裝 —— 敘事由 LLM 寫，數字由程式填。

對應 SPEC #15 的 `Drafting Engine`（高階主管摘要／方法論／案例匹配）與 `Output`
（PDF + Drive + Email，依核准模式送出電子簽署）。

兩條鐵律寫在程式裡，不是寫在註解裡：

1. **金額遮蔽（``redact_monetary_tokens``）**
   LLM 只負責敘事。萬一它「順手」寫出一個看似合理的金額（實測很常見：
   模型會從會議筆記的預算暗示外推出一個報價），該數字會被遮蔽成佔位字串並發 AMBER。
   價格一律由 ``pricing.QuoteEngine`` 計算後由本模組填入表格。
2. **電子簽署永不自動送出（``build_signature_request``）**
   ``is_sent`` 恆為 False。提案是草稿，簽名是締約——客戶一旦簽下去就是合約，
   AI 不得代替公司締約。即使 autonomy 開到 ``supervised_auto``、即使設定檔把
   ``signature.require_human_approval`` 改成 false，本模組仍只產生「待人工核准」的請求單。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pricing import QuoteOption, format_money, format_rate, money_str, pick_recommended

STATUS_DRAFT = "draft_pending_approval"
STATUS_NEEDS_PRICING_REVIEW = "needs_pricing_review"
STATUS_LABELS: dict[str, str] = {
    STATUS_DRAFT: "草稿・待核准",
    STATUS_NEEDS_PRICING_REVIEW: "草稿・待核准（超出自動報價範圍，需主管核價）",
}

SIGNATURE_STATUS_PENDING = "pending_human_approval"
SIGNATURE_BLOCKED_REASON = (
    "電子簽署請求一律需人工核准後手動送出：客戶一旦簽署即成立合約，AI 不得代為締約。"
)

DEFAULT_PLACEHOLDER = "［金額由系統計算後填入］"

# 金額樣式偵測。三種寫法都抓：
#   1. 幣別符號／代碼在前：$1,200 / US$980.50 / USD 1200 / £980 / NT$3,000
#   2. 數字在後接幣別單位：1200 美元 / 3,000 元 / 250 dollars
#   3. 帶千分位的裸數字：12,500.00（純小整數如「3 小時」刻意**不**攔截，
#      否則敘事裡的工時、頁數、天數會被誤遮成佔位字串）
_MONEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:US\$|NT\$|\$|£|€|¥|USD|TWD|EUR|GBP|JPY)\s?\d[\d,]*(?:\.\d+)?", re.IGNORECASE),
    re.compile(r"\d[\d,]*(?:\.\d+)?\s?(?:美元|元整|元|dollars?)", re.IGNORECASE),
    re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?"),
)
_SECTION_PATTERN = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


class ProposalError(RuntimeError):
    """提案組裝失敗（敘事來源缺失、必要欄位缺漏）。"""


@dataclass
class ProposalDocument:
    """一份提案草稿的完整結果。``markdown`` 即交付給人審閱的文件本體。"""

    proposal_id: str
    deal_id: str
    client_name: str
    contact_name: str
    contact_email: str
    status: str
    quote_date: str
    valid_until: str
    currency: str
    options: list[QuoteOption]
    recommended_tier: str
    sections: dict[str, str]
    redactions: list[str]
    issues: list[str]
    signature_request: dict[str, Any]
    delivery_mode: str
    markdown: str = ""
    generated_at: str = ""
    is_regenerated: bool = False
    missing_sections: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        """給人看的狀態文字（永遠帶「草稿・待核准」字樣）。"""
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def requires_human_pricing(self) -> bool:
        """是否超出自動報價範圍。"""
        return self.status == STATUS_NEEDS_PRICING_REVIEW

    def to_dict(self) -> dict[str, Any]:
        """轉成可 JSON 序列化的 dict（金額一律字串）。"""
        recommended = self.recommended_option
        return {
            "proposal_id": self.proposal_id,
            "deal_id": self.deal_id,
            "client_name": self.client_name,
            "contact_name": self.contact_name,
            "contact_email": self.contact_email,
            "status": self.status,
            "status_label": self.status_label,
            "requires_human_pricing": self.requires_human_pricing,
            "quote_date": self.quote_date,
            "valid_until": self.valid_until,
            "currency": self.currency,
            "recommended_tier": self.recommended_tier,
            "recommended_setup_total": money_str(recommended.setup_total),
            "recommended_monthly_total": money_str(recommended.monthly_total),
            "recommended_first_year_total": money_str(recommended.first_year_total),
            "options": [option.to_dict() for option in self.options],
            "sections": dict(self.sections),
            "missing_sections": list(self.missing_sections),
            "redactions": list(self.redactions),
            "issues": list(self.issues),
            "signature_request": dict(self.signature_request),
            "delivery_mode": self.delivery_mode,
            "generated_at": self.generated_at,
            "is_regenerated": self.is_regenerated,
            "markdown": self.markdown,
        }

    @property
    def recommended_option(self) -> QuoteOption:
        """被標示為推薦的方案物件。"""
        for option in self.options:
            if option.tier_key == self.recommended_tier:
                return option
        return pick_recommended(self.options)


# --------------------------------------------------------------------------- #
# 敘事處理（LLM 產出 -> 可信任的段落）
# --------------------------------------------------------------------------- #
def redact_monetary_tokens(
    text: str, placeholder: str = DEFAULT_PLACEHOLDER
) -> tuple[str, list[str]]:
    """遮蔽敘事中的任何金額樣式。回傳（處理後文字, 被遮蔽的原始字串清單）。

    這是本模組最重要的一道閘門：LLM 可以寫「我們理解你的瓶頸」，
    但**不可以**寫「投資約 $4,500」——那個數字沒有經過 RATE_CARD，簽下去就是虧損。
    """
    found: list[str] = []
    redacted = text
    for pattern in _MONEY_PATTERNS:
        def _replace(match: re.Match[str]) -> str:
            found.append(match.group(0))
            return placeholder

        redacted = pattern.sub(_replace, redacted)
    return redacted, found


def split_sections(text: str) -> dict[str, str]:
    """把 ``## 標題`` 形式的敘事切成「標題 -> 內文」。沒有標題時整段歸到「敘事」。"""
    matches = list(_SECTION_PATTERN.finditer(text))
    if not matches:
        return {"敘事": text.strip()}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group("title")] = text[start:end].strip()
    return sections


def generate_narrative(
    deal: dict[str, Any],
    llm: Any,
    prompt_path: Path,
    fixture: Path | None,
    max_tokens: int = 1800,
) -> str:
    """呼叫 LLM 產生提案敘事。mock 模式讀 fixture，live 模式打 Claude API。"""
    if not prompt_path.is_file():
        raise ProposalError(f"找不到提示詞檔：{prompt_path}")
    system = prompt_path.read_text(encoding="utf-8")
    return llm.complete(
        system=system,
        user=build_user_prompt(deal),
        max_tokens=max_tokens,
        fixture=fixture,
    )


def build_user_prompt(deal: dict[str, Any]) -> str:
    """把 CRM Deal ＋ 會議筆記 ＋ 公司背景組成使用者訊息（SPEC #15 的 Input 段）。"""
    services = "、".join(
        f"{entry.get('code')} × {entry.get('quantity', 1)}"
        for entry in deal.get("requested_services") or []
    )
    return "\n".join(
        [
            f"客戶公司：{deal.get('company', '')}",
            f"產業：{deal.get('industry', '')}",
            f"聯絡窗口：{deal.get('contact_name', '')}（{deal.get('contact_title', '')}）",
            f"CRM 交易編號：{deal.get('deal_id', '')}",
            f"公司背景：{deal.get('company_profile', '')}",
            f"近期公司新聞：{deal.get('recent_news', '')}",
            "探索會議筆記：",
            str(deal.get("meeting_notes", "")).strip(),
            f"業務已選定的服務項目代碼（金額不由你決定）：{services}",
        ]
    )


# --------------------------------------------------------------------------- #
# 電子簽署（永遠只到「待核准」為止）
# --------------------------------------------------------------------------- #
def build_signature_request(
    proposal_id: str,
    deal: dict[str, Any],
    signature_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """產生電子簽署請求單。``is_sent`` 恆為 False —— 本模組不具備送出的能力。

    刻意不提供任何 ``send=True`` 參數：能被參數打開的鐵律不是鐵律。
    要送出，人必須自己登入 DocuSign 後台按下送出鍵。
    """
    config = signature_config or {}
    return {
        "provider": str(config.get("provider", "docusign")),
        "document_id": proposal_id,
        "recipient_name": str(deal.get("contact_name", "")),
        "recipient_email": str(deal.get("contact_email", "")),
        "status": SIGNATURE_STATUS_PENDING,
        "is_sent": False,
        "requires_human_approval": True,
        "approver_role": str(config.get("approver_role", "業務主管（Deal Owner）")),
        "blocked_reason": SIGNATURE_BLOCKED_REASON,
        "approval_note": str(config.get("approval_note", "")),
    }


# --------------------------------------------------------------------------- #
# 文件渲染
# --------------------------------------------------------------------------- #
def _render_option_table(option: QuoteOption) -> list[str]:
    """單一方案的明細表 ＋ 小計區塊。所有數字都來自 QuoteOption，不經過 LLM。"""
    mark = "（★ 推薦）" if option.is_recommended else ""
    lines = [
        f"### {option.tier_name}{mark}",
        "",
        option.summary,
        "",
        "| 服務項目 | 計費 | 數量 | 單價 | 小計 |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for item in option.lines:
        billing = "一次性" if item.billing == "one_off" else "每月"
        lines.append(
            f"| {item.name} | {billing} | {item.quantity} {item.unit} "
            f"| {format_money(item.unit_price, option.currency)} "
            f"| {format_money(item.amount, option.currency)} |"
        )
    lines.extend(["", *_render_option_totals(option), ""])
    if option.issues:
        lines.append("> ⚠️ **本方案超出自動報價範圍，需業務主管核價：**")
        lines.extend(f"> - {issue}" for issue in option.issues)
        lines.append("")
    return lines


def _render_option_totals(option: QuoteOption) -> list[str]:
    """方案的金額結算列（折扣、稅、建置費、月費、首年總額）。"""
    currency = option.currency
    rows = [f"- 一次性小計：{format_money(option.one_off_subtotal, currency)}"]
    if option.discount_amount:
        rows.append(
            f"- 折扣（{format_rate(option.discount_rate)}，僅適用一次性費用）："
            f"-{format_money(option.discount_amount, currency)}"
        )
    rows.extend(
        [
            f"- 稅（{format_rate(option.tax_rate)}）：{format_money(option.one_off_tax, currency)}",
            f"- **建置費合計（含稅）：{format_money(option.setup_total, currency)}**",
            f"- **訂閱月費（含稅）：{format_money(option.monthly_total, currency)} / 月**",
            f"- 首年投資總額（建置費 + 月費 × 12）："
            f"{format_money(option.first_year_total, currency)}",
        ]
    )
    return rows


def render_markdown(document: ProposalDocument) -> str:
    """組出完整提案 Markdown。開頭與結尾各壓一次「草稿・待核准」浮水印。"""
    head = [
        f"# 服務提案 — {document.client_name}",
        "",
        f"> **狀態：{document.status_label}**　本文件為系統自動生成的草稿，"
        "**尚未經人工核准，亦未送出任何電子簽署請求**。",
        "",
        f"- 提案編號：`{document.proposal_id}`　CRM 交易：`{document.deal_id}`",
        f"- 客戶窗口：{document.contact_name}　報價日：{document.quote_date}"
        f"　**報價有效至：{document.valid_until}**",
        f"- 幣別：{document.currency}",
        "",
        "---",
        "",
    ]
    body: list[str] = []
    for title, content in document.sections.items():
        body.extend([f"## {title}", "", content, ""])
    body.extend(["## 投資選項", ""])
    for option in document.options:
        body.extend(_render_option_table(option))
    return "\n".join([*head, *body, *_render_footer(document)])


def _render_footer(document: ProposalDocument) -> list[str]:
    """文件結尾：遮蔽紀錄、核准與簽署流程說明。"""
    signature = document.signature_request
    lines = ["---", "", "## 核准與簽署流程", ""]
    if document.redactions:
        # 刻意只報數量、不把被遮蔽的原始金額印回文件：那些數字沒有經過 RATE_CARD，
        # 一旦出現在客戶手上的文件裡，就有被誤讀成「真的報過這個價」的風險。
        # 原始字串留在執行結果與 AMBER 診斷中供內部覆核。
        lines.extend(
            [
                f"> ⚠️ 系統偵測到敘事中有 {len(document.redactions)} 處未經定價卡驗算的金額，"
                "已全數遮蔽。本提案的價格一律以上方投資選項的報價表為準。",
                "",
            ]
        )
    lines.extend(
        [
            f"1. **人工覆核**：由{signature.get('approver_role', '業務主管')}確認服務範圍與金額。",
            "2. **人工核准**：核准後才可對外寄送本提案。",
            f"3. **人工送簽**：電子簽署（{signature.get('provider', 'docusign')}）"
            f"目前狀態為 `{signature.get('status')}`，"
            f"`is_sent = {str(signature.get('is_sent')).lower()}`。",
            "",
            f"> {SIGNATURE_BLOCKED_REASON}",
            "",
            f"_本文件由 demo15 提案與報價生成器產出，狀態：{document.status_label}。_",
        ]
    )
    return lines


# --------------------------------------------------------------------------- #
# 主組裝流程
# --------------------------------------------------------------------------- #
def build_proposal(
    deal: dict[str, Any],
    options: list[QuoteOption],
    narrative: str,
    drafting_config: dict[str, Any] | None,
    signature_config: dict[str, Any] | None,
    quote_date: date,
    valid_until: date,
    delivery_mode: str,
) -> ProposalDocument:
    """把報價、敘事、簽署請求組成一份提案草稿。"""
    drafting = drafting_config or {}
    placeholder = str(drafting.get("redaction_placeholder", DEFAULT_PLACEHOLDER))
    clean_text, redactions = redact_monetary_tokens(narrative, placeholder)
    sections = split_sections(clean_text)
    required = [str(name) for name in drafting.get("required_sections") or []]
    missing = [name for name in required if name not in sections]

    proposal_id = f"PROP-{deal.get('deal_id', 'UNKNOWN')}-{quote_date:%Y%m%d}"
    issues = [issue for option in options for issue in option.issues]
    document = ProposalDocument(
        proposal_id=proposal_id,
        deal_id=str(deal.get("deal_id", "")),
        client_name=str(deal.get("company", "")),
        contact_name=str(deal.get("contact_name", "")),
        contact_email=str(deal.get("contact_email", "")),
        status=STATUS_NEEDS_PRICING_REVIEW if issues else STATUS_DRAFT,
        quote_date=quote_date.isoformat(),
        valid_until=valid_until.isoformat(),
        currency=options[0].currency if options else "USD",
        options=options,
        recommended_tier=pick_recommended(options).tier_key,
        sections=sections,
        redactions=redactions,
        issues=issues,
        signature_request=build_signature_request(proposal_id, deal, signature_config),
        delivery_mode=delivery_mode,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        missing_sections=missing,
    )
    document.markdown = render_markdown(document)
    return document
