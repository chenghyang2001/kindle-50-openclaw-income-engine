"""模組 #11：SEO 內容引擎 — Phase 2 文章草擬（Article Drafting）。

把 Phase 1 選出的關鍵字，展開成 H2 大綱、1500 字草稿、FAQ 與內部連結建議，
最後包成可以推進 CMS 的 payload。

**離線組稿為什麼會留下【待填：…】標記**

書中把這個模組定位成「取代每月 $3k-$5k 的外包內容團隊」，而外包稿最大的風險
不是文筆，是**編出來的數字**。一篇含錯誤數據的文章被 Google 索引後，會跟著
客戶網域好幾年。所以本模組的設計是：能從品牌檔推導的敘述照寫，需要具體數字、
客戶名稱、專案時程的位置一律留 `【待填：…】`，讓它變成編輯的工作清單，
而不是讓模型猜一個看起來合理的數字填進去。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# LLMClient 在 mock 模式回傳的佔位字串前綴（見 CONTRACT.md §3）
MOCK_MARKER = "[MOCK]"

# 待編輯補值的標記；計數後會出現在週一簡報，當成審稿清單
PLACEHOLDER_PATTERN = re.compile(r"【待填：[^】]*】")

# 中文沒有詞界，字數用「CJK 字元數 + 拉丁詞數」估算
CJK_PATTERN = re.compile(r"[㐀-䶿一-鿿]")
LATIN_WORD_PATTERN = re.compile(r"[A-Za-z][A-Za-z'\-]*|\d+(?:\.\d+)?")

META_DESCRIPTION_LIMIT = 140
DEFAULT_SECTION_KIND = "definition"


class ContentGeneratorError(RuntimeError):
    """草擬流程中可預期的失敗（提示詞缺失、產出空白、JSON 不合格式等）。"""


@dataclass(frozen=True)
class DraftSettings:
    """草稿規格（對應 config.yaml 的 content_settings）。"""

    target_words: int
    min_words: int
    max_words: int
    faq_questions: int
    h2_sections: int
    min_links: int
    max_links: int

    @classmethod
    def from_config(cls, content_settings: dict[str, Any]) -> "DraftSettings":
        """從設定片段建立草稿規格；區間顛倒直接拋錯，不默默修正。"""
        links = content_settings.get("internal_links") or {}
        settings = cls(
            target_words=int(content_settings.get("target_words", 1500)),
            min_words=int(content_settings.get("min_words", 1200)),
            max_words=int(content_settings.get("max_words", 2200)),
            faq_questions=int(content_settings.get("faq_questions", 4)),
            h2_sections=int(content_settings.get("h2_sections", 5)),
            min_links=int(links.get("min_per_article", 2)),
            max_links=int(links.get("max_per_article", 3)),
        )
        if settings.min_words > settings.max_words:
            raise ContentGeneratorError(
                f"字數區間顛倒：min_words={settings.min_words} > max_words={settings.max_words}"
            )
        return settings


# --------------------------------------------------------------------------
# 度量與檔案
# --------------------------------------------------------------------------


def count_words(text: str) -> int:
    """估算中文字數：CJK 字元各算一個字，拉丁單字與數字各算一個字。"""
    if not text:
        return 0
    return len(CJK_PATTERN.findall(text)) + len(LATIN_WORD_PATTERN.findall(text))


def count_placeholders(text: str) -> int:
    """計算【待填：…】標記數量（編輯的待辦數）。"""
    return len(PLACEHOLDER_PATTERN.findall(text or ""))


def load_prompt(path: str | Path) -> str:
    """讀取提示詞 .md。提示詞是資產，一律獨立成檔，不內嵌在 .py 字串裡。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"找不到提示詞檔：{target.resolve()}")
    text = target.read_text(encoding="utf-8").strip()
    if not text:
        raise ContentGeneratorError(f"提示詞檔是空的：{target.resolve()}")
    return text


def article_plain_text(article: dict[str, Any]) -> str:
    """把文章各區塊串成純文字，供字數統計與禁用詞檢查。"""
    parts: list[str] = [
        str(article.get("title", "")),
        str(article.get("introduction", "")),
    ]
    for section in article.get("sections") or []:
        parts.append(str(section.get("heading", "")))
        parts.append(str(section.get("body", "")))
    for item in article.get("faq") or []:
        parts.append(str(item.get("question", "")))
        parts.append(str(item.get("answer", "")))
    return "\n".join(part for part in parts if part)


# --------------------------------------------------------------------------
# Phase 1 的產出：主題簡報（大綱 + 角度）
# --------------------------------------------------------------------------


def build_outline(
    keyword: str, seed_topic: str, patterns: list[dict[str, Any]], h2_sections: int
) -> list[dict[str, str]]:
    """依樣板組出 H2 大綱。樣板數少於需求則循環使用。"""
    if not patterns:
        raise ContentGeneratorError("content_settings.outline_patterns 是空的，無法組大綱")
    outline: list[dict[str, str]] = []
    for index in range(max(1, h2_sections)):
        pattern = patterns[index % len(patterns)]
        heading = str(pattern.get("heading", "")).format(keyword=keyword, seed=seed_topic)
        outline.append(
            {"kind": str(pattern.get("kind", DEFAULT_SECTION_KIND)), "heading": heading}
        )
    return outline


def build_faq_questions(
    keyword: str, seed_topic: str, patterns: list[str], count: int
) -> list[str]:
    """依樣板組出 FAQ 題目。樣板數少於需求則循環使用。"""
    if not patterns:
        return []
    return [
        str(patterns[index % len(patterns)]).format(keyword=keyword, seed=seed_topic)
        for index in range(max(0, count))
    ]


def build_topic_brief(
    candidate: Any, brand: dict[str, Any], content_settings: dict[str, Any]
) -> dict[str, Any]:
    """把一個關鍵字候選展開成 Phase 1 的主題簡報（角度 + 大綱 + FAQ 題目）。"""
    keyword = candidate.query
    seed_topic = candidate.seed_topic or keyword
    settings = DraftSettings.from_config(content_settings)
    return {
        "keyword": keyword,
        "seed_topic": seed_topic,
        "position": candidate.position,
        "impressions": candidate.impressions,
        "difficulty": candidate.difficulty,
        "source": candidate.source,
        "search_intent": _search_intent(candidate, keyword),
        "angle": _angle(keyword, brand),
        "primary_reader": str(brand.get("audience", "營運負責人")),
        "outline": build_outline(
            keyword,
            seed_topic,
            content_settings.get("outline_patterns") or [],
            settings.h2_sections,
        ),
        "faq_questions": build_faq_questions(
            keyword,
            seed_topic,
            content_settings.get("faq_patterns") or [],
            settings.faq_questions,
        ),
    }


def _search_intent(candidate: Any, keyword: str) -> str:
    """依排名與點擊狀況推斷搜尋意圖所處的階段（可被 live 模式的模型覆寫）。"""
    if candidate.position is None:
        return f"尚無排名資料，推測搜尋「{keyword}」的人處於認知階段，想先弄懂範圍"
    if candidate.clicks > 0 and candidate.impressions > 0:
        ratio = candidate.clicks / candidate.impressions
        if ratio >= 0.03:
            return f"已經有人點進來，搜尋「{keyword}」的人處於評估階段，要的是判斷依據"
    return f"有曝光但點擊少，搜尋「{keyword}」的人還在比較答案，標題與前三句決定去留"


def _angle(keyword: str, brand: dict[str, Any]) -> str:
    """從品牌檔的專業筆記推導角度，不憑空發明立場。"""
    notes = [str(note) for note in (brand.get("expertise_notes") or []) if str(note)]
    if not notes:
        return f"把{keyword}拆成可以自己判斷的幾個界線，而不是給一份工具比較表"
    return f"{notes[0]}；因此談{keyword}要先講順序，再談工具"


# --------------------------------------------------------------------------
# 內部連結建議
# --------------------------------------------------------------------------


def _page_score(page: dict[str, Any], keyword: str, seed_topic: str) -> int:
    """既有頁面與這個關鍵字的相關度。分數為 0 就不該連。"""
    score = 0
    topics = [str(item).lower() for item in (page.get("topics") or [])]
    if seed_topic and seed_topic.lower() in topics:
        score += 3
    lowered_keyword = keyword.lower()
    for item in page.get("keywords") or []:
        text = str(item).lower()
        if text and text in lowered_keyword:
            score += 2
    for token in keyword.split():
        if token and token.lower() in str(page.get("title", "")).lower():
            score += 1
    return score


def suggest_internal_links(
    keyword: str,
    seed_topic: str,
    site_pages: list[dict[str, Any]],
    outline: list[dict[str, str]],
    max_links: int,
) -> list[dict[str, str]]:
    """挑出真的相關的既有頁面。寧可少放，也不為了湊數量硬連。"""
    scored: list[tuple[int, dict[str, Any]]] = []
    for page in site_pages:
        score = _page_score(page, keyword, seed_topic)
        if score > 0:
            scored.append((score, page))
    # 兩段穩定排序：先讓新文章排前面（同分時連新的比連舊的有價值），再依相關度排
    scored.sort(key=lambda pair: str(pair[1].get("published_on", "")), reverse=True)
    scored.sort(key=lambda pair: -pair[0])
    links: list[dict[str, str]] = []
    for index, (score, page) in enumerate(scored[: max(0, max_links)]):
        placement = outline[index % len(outline)]["heading"] if outline else "內文"
        links.append(
            {
                "url": str(page.get("url", "")),
                "anchor": str(page.get("title", "")),
                "placement": placement,
                "reason": f"主題相關度 {score}：與「{seed_topic}」同一題材，可承接讀者的下一個問題",
            }
        )
    return links


# --------------------------------------------------------------------------
# 離線組稿（--mock 模式）
# --------------------------------------------------------------------------


def _paragraph(sentences: list[str]) -> str:
    """把句子串成一段（每句自帶句號）。"""
    return "".join(f"{item.strip()}。" for item in sentences if item and item.strip())


def _blocks(*paragraphs: str) -> str:
    """段落之間空一行。"""
    return "\n\n".join(item for item in paragraphs if item)


def _note(brand: dict[str, Any], index: int, fallback: str) -> str:
    """取品牌檔第 index 條專業筆記；沒有就用保底句，不編造。"""
    notes = [str(item) for item in (brand.get("expertise_notes") or []) if str(item)]
    if not notes:
        return fallback
    return notes[index % len(notes)]


def _section_definition(keyword: str, seed: str, brand: dict[str, Any]) -> str:
    """定義段：先把範圍收斂，避免同一個詞在不同公司指不同的事。"""
    audience = str(brand.get("audience", "營運負責人"))
    first = [
        f"談{keyword}之前要先把範圍講清楚，因為同一個詞在不同公司指的往往不是同一件事",
        f"對{audience}來說，它通常包含三個部分：現場實際發生的動作、系統裡記錄的資料，"
        "以及兩者對不上時的處理方式",
        "多數團隊的痛點不在前兩項，而在第三項，現場做完了系統沒更新，"
        "隔天就沒有人知道該相信哪一邊",
        f"{brand.get('brand', '我們')}在導入專案裡看到的順序幾乎都一樣，先把定義收斂，再談工具",
    ]
    second = [
        f"如果你正在搜尋「{keyword}」，多半是手上已經出現對不上的數字，而不是想讀教科書定義",
        "所以這一段只給你判斷用的界線，哪些事屬於這個題目、哪些事其實是別的問題",
        f"屬於這個題目的是：{_note(brand, 0, '流程、資料與異常處理三者的一致性')}",
        "不屬於這個題目卻常被混在一起討論的，是人力排班、採購決策，以及跟供應商的交期談判",
        f"【待填：你們公司目前對{keyword}的定義寫在哪一份文件裡】",
    ]
    return _blocks(_paragraph(first), _paragraph(second))


def _section_pain(keyword: str, seed: str, brand: dict[str, Any]) -> str:
    """瓶頸段：三個結構性問題，不是努力程度的問題。"""
    first = [
        f"{seed}會卡住通常不是因為沒人努力，而是三個結構性的瓶頸同時存在",
        "第一個是主檔沒整理乾淨，品號重複、儲位命名沒有規則，任何統計都會在源頭就歪掉",
        "第二個是紀錄時間點落後，現場先做事、事後才補單，中間那段空窗期系統是盲的",
        "第三個是責任沒有落到人身上，所有人都能改資料，出錯時卻沒有人知道要問誰",
    ]
    second = [
        "這三個瓶頸的共同特徵是它們都不會讓你當天就出事",
        "它們會先讓數字慢慢失真，等到盤點或客訴的時候才一次爆出來",
        f"這也是為什麼很多團隊在評估{keyword}之前，都會先覺得自己沒那麼嚴重",
        f"{_note(brand, 1, '最常見的失敗點是主檔沒整理乾淨就上線')}",
        "【待填：你們最近一次盤差或揀錯的實際案例，用來取代這裡的通則描述】",
    ]
    return _blocks(_paragraph(first), _paragraph(second))


def _section_howto(keyword: str, seed: str, brand: dict[str, Any]) -> str:
    """做法段：四個階段，順序不能跳。"""
    first = [
        f"{keyword}的實際做法可以拆成四個階段，順序不能跳",
        "第一階段是現況盤點，把現在的流程、表單，以及每個人手上的私版試算表全部攤開",
        "第二階段是主檔整理，品號、儲位、單位換算這三件事沒整乾淨，後面每一步都會付利息",
        "第三階段是試營運，挑一個品類或一個貨區先跑，讓錯誤在可控的小範圍內發生",
        "第四階段才是正式切換，而且切換當週要保留舊流程當備援",
    ]
    second = [
        "每個階段結束都要有一個可以檢查的產出，否則專案會停在「大家都覺得差不多了」",
        "現況盤點的產出是流程圖與表單清單，主檔整理的產出是一份可以直接匯入的乾淨檔案",
        "試營運的產出是錯誤清單與對應的修正，正式切換的產出是新舊流程對照表",
        "這四份產出也是你日後回頭追問題的依據，不要只留在會議記錄裡",
        "【待填：你們預計投入的內部人力，以及各階段的負責人】",
    ]
    return _blocks(_paragraph(first), _paragraph(second))


def _section_cost(keyword: str, seed: str, brand: dict[str, Any]) -> str:
    """成本段：三塊成本，第三塊最常被漏掉。"""
    cta = str(brand.get("call_to_action", "可以先做一次現況自評"))
    first = [
        "成本要分成三塊估：軟體、導入服務，以及自己人的時間",
        "前兩塊看得見，第三塊最常被漏掉，也最常是專案延期的真正原因",
        "自己人的時間主要花在主檔整理與試營運，這兩段沒有人可以完全外包",
        "時程上決定成敗的不是系統開通得多快，而是主檔整理花了多久",
    ]
    second = [
        "報價要問清楚三件事：是否含資料轉入、上線後的支援方式，以及加開站點或使用者怎麼計價",
        "這三項沒問清楚，後續的追加費用往往會超過原本的簽約金額",
        f"【待填：{brand.get('brand', '我們')}目前的方案級距與各級距包含的服務範圍】",
        "【待填：一個真實專案從評估到上線的實際天數】",
        f"如果你想先抓一個粗略區間，{cta.rstrip('。')}",
    ]
    return _blocks(_paragraph(first), _paragraph(second))


def _section_mistakes(keyword: str, seed: str, brand: dict[str, Any]) -> str:
    """錯誤段：避開這些比學會任何技巧都有效。"""
    first = [
        "最後整理幾個反覆出現的錯誤，避開它們比學會任何技巧都有效",
        "第一個錯誤是把系統當成抓人的工具，用來追究誰做錯，結果現場開始隱瞞異常",
        "第二個錯誤是上線第一天就要求全品項一次切換，錯誤同時發生就沒有人分辨得出原因",
        "第三個錯誤是把主檔交給最有空的人整理，而不是最懂料的人",
        "第四個錯誤是沒有定義什麼情況要停下來，於是錯誤資料一路往下游流",
    ]
    second = [
        "這些錯誤有一個共同點，它們都是管理決策，不是系統功能可以擋下來的",
        f"所以評估{keyword}的時候，工具比較表只能回答一半的問題",
        "另一半要靠你先想清楚，出錯的時候誰有權力喊停、喊停之後誰來收拾",
        f"{_note(brand, 2, '盤差率與揀貨錯誤率是最值得長期追蹤的兩個指標')}",
        "【待填：你們目前的異常處理權責，以及最近一次喊停的實際情況】",
    ]
    return _blocks(_paragraph(first), _paragraph(second))


SECTION_COMPOSERS: dict[str, Callable[[str, str, dict[str, Any]], str]] = {
    "definition": _section_definition,
    "pain": _section_pain,
    "howto": _section_howto,
    "cost": _section_cost,
    "mistakes": _section_mistakes,
}

# FAQ 回答樣板，依 config 的 faq_patterns 順序循環套用
FAQ_ANSWERS: tuple[str, ...] = (
    "預算要分成軟體、導入服務、內部人力三塊估，其中內部人力最常被漏算。"
    "【待填：目前方案的實際級距與費用區間】。"
    "建議先確認報價是否包含資料轉入與上線後支援，這兩項是追加費用最常見的來源。",
    "時程的變數不在系統開通，而在主檔整理花多久，主檔越亂前置作業越長。"
    "【待填：一個真實專案從評估到上線的天數】。"
    "試營運至少要跑滿一個完整的盤點或結帳週期，才看得出真正的問題。",
    "倉庫大小不是判斷標準，品項數與異動頻率才是。"
    "品項只有幾十個、每天異動個位數，用表單就夠；"
    "品項上千或同一料號分散多個儲位，人工紀錄就會開始失準。",
    "不會衝突，但要先講清楚分工：ERP 管單據與帳，倉儲端管實際的物與位。"
    "兩邊在庫存數量上必須有唯一的真相來源，通常是倉儲端先確認再回寫。"
    "【待填：你們現在的 ERP 版本與預計的介接方式】",
)


def compose_offline_introduction(brief: dict[str, Any], brand: dict[str, Any]) -> str:
    """離線模式的開場：3-4 句，第一句就回答讀者的問題。"""
    keyword = brief["keyword"]
    return _paragraph(
        [
            f"如果你正在找{keyword}的答案，通常是因為手上已經出現對不上的數字，"
            "而不是因為想研究理論",
            f"這篇文章的立場很直接：{str(brief.get('angle', '')).rstrip('。')}",
            f"內容寫給{brand.get('audience', '營運負責人')}，不需要 IT 背景也讀得完",
            "文章最後附上 FAQ，先回答評估階段最常被問到的幾個問題",
        ]
    )


def compose_offline_article(
    brief: dict[str, Any],
    brand: dict[str, Any],
    links: list[dict[str, str]],
) -> dict[str, Any]:
    """離線組稿：用品牌檔與大綱樣板組出完整草稿，需要數字的地方留【待填】。"""
    keyword = brief["keyword"]
    seed = brief.get("seed_topic") or keyword
    sections = [
        {
            "heading": item["heading"],
            "body": SECTION_COMPOSERS.get(item["kind"], _section_definition)(
                keyword, seed, brand
            ),
        }
        for item in brief["outline"]
    ]
    faq = [
        {"question": question, "answer": FAQ_ANSWERS[index % len(FAQ_ANSWERS)]}
        for index, question in enumerate(brief.get("faq_questions") or [])
    ]
    return {
        "title": f"{keyword}：從定義、瓶頸到成本，一次把判斷依據講清楚",
        "meta_description": _meta_description(keyword, brand),
        "introduction": compose_offline_introduction(brief, brand),
        "sections": sections,
        "faq": faq,
        "internal_links": list(links),
        "source": "offline",
    }


def _meta_description(keyword: str, brand: dict[str, Any]) -> str:
    """組 meta description 並壓在長度上限內。"""
    audience = str(brand.get("audience", "營運負責人")).split("，")[0]
    text = (
        f"{keyword}怎麼評估？本文從定義、常見瓶頸、實際做法到成本時程逐段拆解，"
        f"並整理常見錯誤與 FAQ，寫給{audience}。"
    )
    if len(text) <= META_DESCRIPTION_LIMIT:
        return text
    return text[: META_DESCRIPTION_LIMIT - 1] + "。"


# --------------------------------------------------------------------------
# Phase 1 的 LLM 補強（--live 模式才會實際呼叫）
# --------------------------------------------------------------------------


def build_selection_user_prompt(
    briefs: list[dict[str, Any]], brand: dict[str, Any]
) -> str:
    """組出 Phase 1（主題選擇）的 user prompt。

    排序與門檻已經在 `keyword_planner` 決定，這裡只請模型補「角度」與「搜尋意圖」——
    那是需要語言判斷的部分，排名 8-20 這種可驗證的商業規則不交給模型重新發明。
    """
    rows = "\n".join(
        f"  - {item['keyword']}｜排名 {item.get('position')}｜曝光 {item.get('impressions')}"
        f"｜難度 {item.get('difficulty')}｜種子主題 {item.get('seed_topic')}"
        for item in briefs
    )
    return "\n".join(
        [
            f"品牌：{brand.get('brand', '')}｜定位：{brand.get('positioning', '')}",
            f"主要讀者：{brand.get('audience', '')}",
            f"可引用的專業筆記：{'；'.join(str(item) for item in brand.get('expertise_notes') or [])}",
            f"禁用詞：{brand.get('banned_words') or '（無）'}",
            f"本週候選關鍵字（已依規則篩選排序，請勿重排）：\n{rows}",
        ]
    )


def parse_topics_json(raw: str) -> dict[str, dict[str, Any]]:
    """解析 Phase 1 的 JSON 輸出，回傳 {關鍵字: 覆寫欄位}。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContentGeneratorError(f"模型回傳的不是合法 JSON：{exc}") from exc
    topics = data.get("topics") if isinstance(data, dict) else None
    if not isinstance(topics, list) or not topics:
        raise ContentGeneratorError("模型回傳的 JSON 缺少 topics 陣列")
    return {
        str(item.get("keyword", "")): item
        for item in topics
        if isinstance(item, dict) and item.get("keyword")
    }


def apply_topic_overrides(
    briefs: list[dict[str, Any]], overrides: dict[str, dict[str, Any]]
) -> list[str]:
    """把模型補的角度／意圖／大綱標題併回主題簡報，回傳被覆寫的關鍵字清單。"""
    applied: list[str] = []
    for brief in briefs:
        override = overrides.get(brief["keyword"])
        if not override:
            continue
        for field in ("search_intent", "angle", "primary_reader"):
            if str(override.get(field, "")).strip():
                brief[field] = str(override[field]).strip()
        headings = override.get("outline")
        if isinstance(headings, list) and len(headings) == len(brief["outline"]):
            for section, heading in zip(brief["outline"], headings):
                if str(heading).strip():
                    section["heading"] = str(heading).strip()
        applied.append(brief["keyword"])
    return applied


# --------------------------------------------------------------------------
# Phase 2 的 LLM 草擬（--live 模式）
# --------------------------------------------------------------------------


def build_user_prompt(
    brief: dict[str, Any],
    brand: dict[str, Any],
    site_pages: list[dict[str, Any]],
    settings: DraftSettings,
) -> str:
    """組出送進 LLM 的 user prompt（系統提示詞另從 prompts/*.md 載入）。"""
    headings = "\n".join(f"  {index + 1}. {item['heading']}" for index, item in enumerate(brief["outline"]))
    questions = "\n".join(f"  - {item}" for item in brief.get("faq_questions") or [])
    pages = "\n".join(
        f"  - {page.get('url', '')}｜{page.get('title', '')}｜主題 {page.get('topics') or []}"
        for page in site_pages
    )
    return "\n".join(
        [
            f"目標關鍵字：{brief['keyword']}（種子主題：{brief.get('seed_topic', '')}）",
            f"搜尋意圖：{brief.get('search_intent', '')}",
            f"文章角度：{brief.get('angle', '')}",
            f"主要讀者：{brief.get('primary_reader', '')}",
            f"品牌：{brand.get('brand', '')}｜定位：{brand.get('positioning', '')}",
            f"品牌語氣支柱：{'、'.join(str(item) for item in brand.get('voice_pillars') or [])}",
            f"可引用的專業筆記：{'；'.join(str(item) for item in brand.get('expertise_notes') or [])}",
            f"禁用詞（絕對不可出現）：{brand.get('banned_words') or '（無）'}",
            f"目標字數：{settings.target_words}（下限 {settings.min_words}，上限 {settings.max_words}）",
            f"H2 大綱（依序展開）：\n{headings}",
            f"FAQ 題目：\n{questions}" if questions else "FAQ 題目：（無，請自行擬 3 題）",
            f"可選的內部連結頁面（只能從這裡挑 {settings.min_links}-{settings.max_links} 個）：\n{pages}",
        ]
    )


def parse_article_json(raw: str) -> dict[str, Any]:
    """解析模型回傳的 JSON；容忍被 ``` 包住的情況。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContentGeneratorError(f"模型回傳的不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not data.get("sections"):
        raise ContentGeneratorError("模型回傳的 JSON 缺少 sections 欄位")
    data["source"] = "llm"
    return data


def draft_article(
    client: Any,
    system_prompt: str,
    brief: dict[str, Any],
    brand: dict[str, Any],
    site_pages: list[dict[str, Any]],
    settings: DraftSettings,
) -> tuple[dict[str, Any], list[str]]:
    """產出單篇草稿，回傳 (文章, 警告清單)。

    mock 模式或模型回傳無法解析時，一律退回離線組稿——一篇 JSON 壞掉不該
    讓整週三篇都交不出來，但退回這件事必須留下警告，不可靜默替換。
    """
    links = suggest_internal_links(
        brief["keyword"],
        brief.get("seed_topic") or brief["keyword"],
        site_pages,
        brief["outline"],
        settings.max_links,
    )
    warnings: list[str] = []
    raw = client.complete(
        system=system_prompt,
        user=build_user_prompt(brief, brand, site_pages, settings),
        max_tokens=4000,
    )
    text = (raw or "").strip()
    if not text or text.startswith(MOCK_MARKER):
        return compose_offline_article(brief, brand, links), warnings
    try:
        article = parse_article_json(text)
    except ContentGeneratorError as exc:
        warnings.append(f"「{brief['keyword']}」的模型輸出無法解析（{exc}），已退回離線組稿")
        return compose_offline_article(brief, brand, links), warnings
    article.setdefault("internal_links", links)
    return article, warnings


# --------------------------------------------------------------------------
# 驗證與交付
# --------------------------------------------------------------------------


def find_banned_words(text: str, banned_words: list[str]) -> list[str]:
    """回傳文中命中的禁用詞（不分大小寫）。"""
    lowered = text.lower()
    return [word for word in banned_words if word and str(word).lower() in lowered]


def finalize_article(
    article: dict[str, Any],
    brief: dict[str, Any],
    brand: dict[str, Any],
    settings: DraftSettings,
) -> tuple[dict[str, Any], list[str]]:
    """補齊統計欄位並做交付前驗證，回傳 (文章, 警告清單)。"""
    plain = article_plain_text(article)
    if not plain.strip():
        raise ContentGeneratorError(f"「{brief['keyword']}」產出空白草稿，無法送審")
    article["keyword"] = brief["keyword"]
    article["seed_topic"] = brief.get("seed_topic") or brief["keyword"]
    article["word_count"] = count_words(plain)
    article["placeholder_count"] = count_placeholders(plain)
    article["section_count"] = len(article.get("sections") or [])
    article["faq_count"] = len(article.get("faq") or [])
    article["link_count"] = len(article.get("internal_links") or [])
    article["slug"] = _slugify(brief["keyword"])
    return article, _article_warnings(article, brand, settings)


def _article_warnings(
    article: dict[str, Any], brand: dict[str, Any], settings: DraftSettings
) -> list[str]:
    """交付前驗證：字數、禁用詞、內部連結數量。"""
    keyword = article.get("keyword", "")
    warnings: list[str] = []
    words = int(article.get("word_count", 0))
    if words < settings.min_words:
        warnings.append(
            f"「{keyword}」草稿只有 {words} 字，低於下限 {settings.min_words}，請補寫或改用 --live"
        )
    elif words > settings.max_words:
        warnings.append(f"「{keyword}」草稿 {words} 字超過上限 {settings.max_words}，審稿時請刪減鋪陳")
    hits = find_banned_words(
        article_plain_text(article), [str(item) for item in brand.get("banned_words") or []]
    )
    if hits:
        warnings.append(f"「{keyword}」草稿含品牌禁用詞 {hits}，發布前必須改寫")
    if int(article.get("link_count", 0)) < settings.min_links:
        warnings.append(
            f"「{keyword}」只找到 {article.get('link_count', 0)} 個相關內部連結，"
            f"少於建議的 {settings.min_links} 個，代表這個題材的既有內容還不夠"
        )
    return warnings


def _slugify(keyword: str) -> str:
    """把關鍵字轉成 CMS 用的 slug；中文保留原字，空白與符號換成連字號。"""
    cleaned = re.sub(r"[^\w㐀-䶿一-鿿]+", "-", keyword.strip().lower())
    return cleaned.strip("-") or "untitled"


def render_markdown(article: dict[str, Any]) -> str:
    """把文章渲染成 Markdown（推進 CMS 或給編輯審稿用）。"""
    lines = [f"# {article.get('title', '')}", "", str(article.get("introduction", "")), ""]
    for section in article.get("sections") or []:
        lines += [f"## {section.get('heading', '')}", "", str(section.get("body", "")), ""]
    if article.get("faq"):
        lines += ["## 常見問題（FAQ）", ""]
        for item in article["faq"]:
            lines += [f"### {item.get('question', '')}", "", str(item.get("answer", "")), ""]
    if article.get("internal_links"):
        lines += ["## 內部連結建議（審稿用，不會出現在正文）", ""]
        for link in article["internal_links"]:
            lines.append(
                f"- [{link.get('anchor', '')}]({link.get('url', '')})"
                f" — 放在「{link.get('placement', '')}」：{link.get('reason', '')}"
            )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_cms_payload(article: dict[str, Any], cms_config: dict[str, Any]) -> dict[str, Any]:
    """組出推進 CMS 的 payload。

    初始狀態吃 config 的 `default_status`（預設 draft）；最終狀態由 `main._apply_autonomy`
    依自主權閘門決定，因為「能不能直接發布」是安全決策，不該由 CMS 設定值單方面決定。
    """
    return {
        "provider": str(cms_config.get("provider", "wordpress")),
        "status": str(cms_config.get("default_status", "draft")),
        "title": article.get("title", ""),
        "slug": article.get("slug", ""),
        "excerpt": article.get("meta_description", ""),
        "categories": [str(cms_config.get("category", ""))] if cms_config.get("category") else [],
        "author": str(cms_config.get("author", "")),
        "content_markdown": render_markdown(article),
        "meta": {
            "focus_keyword": article.get("keyword", ""),
            "word_count": article.get("word_count", 0),
            "internal_links": article.get("internal_links") or [],
        },
    }
