"""社群媒體一週內容產生器（模組 #4）。

把企業主提交的 10 分鐘簡報（brief）與品牌語氣檔（brand_voice），
展開成「每個平台一週 5-7 則」的貼文草稿，並在產出當下就做格式驗證：

1. 平台字元上限（X 的 280 是硬上限，超過會被平台拒收）
2. 簡報指定的禁用詞（法規或品牌禁語）
3. hashtag 數量

驗證放在產出階段而非排程階段，是因為人工審閱的 20 分鐘應該用來看「內容對不對」，
而不是幫機器數字數。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# LLMClient 在 mock 模式回傳的佔位字串前綴（見 CONTRACT.md §3）
MOCK_MARKER = "[MOCK]"
TRUNCATION_SUFFIX = "…"
MIN_TONE_EXAMPLES = 3
SUMMARY_WIDTH = 40

# 用 \w 才能同時匹配中英文 hashtag（Python 3 的 \w 預設就是 unicode）
HASHTAG_PATTERN = re.compile(r"#\w+")

REQUIRED_PROFILE_KEYS = (
    "id",
    "display_name",
    "tone",
    "char_limit",
    "posts_per_week",
    "prompt_file",
    "schedule_slots",
)


class GeneratorError(RuntimeError):
    """內容產生流程中可預期的失敗（設定缺欄位、檔案格式錯誤、產出空白等）。"""


@dataclass(frozen=True)
class PlatformProfile:
    """單一平台的語氣與格式規格（對應 config.yaml 的 platforms 區段）。"""

    platform_id: str
    display_name: str
    tone: str
    char_limit: int
    posts_per_week: int
    prompt_file: str
    schedule_slots: tuple[str, ...]
    emoji_allowed: bool = False
    hashtag_max: int = 3

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "PlatformProfile":
        """從設定片段建立 profile。缺欄位直接拋錯，不用預設值靜默掩蓋。"""
        missing = [key for key in REQUIRED_PROFILE_KEYS if key not in raw]
        if missing:
            raise GeneratorError(
                f"平台 {raw.get('id', '<未命名>')} 的設定缺少欄位：{missing}"
            )
        slots = tuple(str(slot) for slot in raw["schedule_slots"])
        if not slots:
            raise GeneratorError(f"平台 {raw['id']} 沒有任何 schedule_slots，無法排程")
        return cls(
            platform_id=str(raw["id"]),
            display_name=str(raw["display_name"]),
            tone=str(raw["tone"]),
            char_limit=int(raw["char_limit"]),
            posts_per_week=int(raw["posts_per_week"]),
            prompt_file=str(raw["prompt_file"]),
            schedule_slots=slots,
            emoji_allowed=bool(raw.get("emoji_allowed", False)),
            hashtag_max=int(raw.get("hashtag_max", 3)),
        )

    def slot_for(self, index: int) -> str:
        """第 index 則貼文的建議發布時段；時段數少於貼文數時循環使用。"""
        return self.schedule_slots[index % len(self.schedule_slots)]


# --------------------------------------------------------------------------
# 檔案載入
# --------------------------------------------------------------------------


def load_json_file(path: str | Path) -> dict[str, Any]:
    """讀取 UTF-8 JSON。檔案不存在或格式錯誤都要明確報出絕對路徑。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"找不到檔案：{target.resolve()}")
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GeneratorError(f"JSON 解析失敗（{target.resolve()}）：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise GeneratorError(f"檔案不是 UTF-8 編碼（{target.resolve()}）：{exc}") from exc


def load_prompt(path: str | Path) -> str:
    """讀取提示詞 .md。提示詞是資產，一律獨立成檔，不內嵌在 .py 字串裡。"""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"找不到提示詞檔：{target.resolve()}")
    text = target.read_text(encoding="utf-8").strip()
    if not text:
        raise GeneratorError(f"提示詞檔是空的：{target.resolve()}")
    return text


# --------------------------------------------------------------------------
# 格式驗證
# --------------------------------------------------------------------------


def count_hashtags(text: str) -> int:
    """計算貼文中的 hashtag 數量。"""
    return len(HASHTAG_PATTERN.findall(text))


def find_banned_words(text: str, banned_words: list[str]) -> list[str]:
    """回傳貼文中命中的禁用詞（不分大小寫）。"""
    lowered = text.lower()
    return [word for word in banned_words if word and word.lower() in lowered]


def truncate_to_limit(text: str, limit: int) -> str:
    """把貼文截到平台上限，盡量在標點或空白處斷開，避免切出半句話。

    斷點若落在保留長度的 60% 之前就寧可硬切——與其為了斷句漂亮而丟掉四成內容，
    不如保住資訊量，反正人工審閱時本來就會再修一次。
    """
    if len(text) <= limit:
        return text
    keep = limit - len(TRUNCATION_SUFFIX)
    if keep <= 0:
        return text[:limit]
    window = text[:keep]
    cut = max(window.rfind(mark) for mark in (" ", "，", "。", "、", "！", "？", "\n"))
    if cut >= keep * 0.6:
        window = window[:cut]
    return window.rstrip() + TRUNCATION_SUFFIX


def validate_post(
    text: str,
    profile: PlatformProfile,
    banned_words: list[str] | None = None,
) -> tuple[str, list[str]]:
    """驗證並修正單則貼文，回傳 (可用貼文, 警告清單)。

    字元超限一律截斷（硬性，否則平台會拒收）；禁用詞與 hashtag 超量只發警告，
    因為那需要人來判斷怎麼改寫，機器亂改反而傷品牌。
    """
    cleaned = text.strip()
    if not cleaned:
        raise GeneratorError(f"{profile.display_name} 產出空白貼文，無法排程")
    warnings: list[str] = []
    if len(cleaned) > profile.char_limit:
        warnings.append(
            f"{profile.display_name} 貼文 {len(cleaned)} 字元超過上限 "
            f"{profile.char_limit}，已自動截斷，請人工確認結尾是否完整"
        )
        cleaned = truncate_to_limit(cleaned, profile.char_limit)
    hits = find_banned_words(cleaned, banned_words or [])
    if hits:
        warnings.append(
            f"{profile.display_name} 貼文含禁用詞 {hits}，排程前必須人工改寫"
        )
    tags = count_hashtags(cleaned)
    if tags > profile.hashtag_max:
        warnings.append(
            f"{profile.display_name} 貼文有 {tags} 個 hashtag，超過建議上限 {profile.hashtag_max}"
        )
    return cleaned, warnings


# --------------------------------------------------------------------------
# 離線組稿（--mock 模式）
# --------------------------------------------------------------------------


def _theme_for(brief: dict[str, Any], index: int) -> dict[str, Any]:
    """取第 index 個主題；主題數少於貼文數時循環使用。"""
    themes = brief.get("themes") or []
    if not themes:
        raise GeneratorError("brief 沒有任何 themes，無法產生一週內容")
    return themes[index % len(themes)]


def _promo_line(brief: dict[str, Any]) -> str:
    """把促銷資訊組成一行；沒有促銷就回空字串（不要硬塞行銷語）。"""
    promo = brief.get("promotion") or {}
    if not promo.get("name"):
        return ""
    return f"{promo['name']}：{promo.get('detail', '')}（至 {promo.get('ends_on', '本週')}）"


def _hashtags(profile: PlatformProfile, brand_voice: dict[str, Any]) -> str:
    """取該平台的招牌 hashtag，數量不超過 profile 的上限。"""
    table = brand_voice.get("signature_hashtags") or {}
    tags = table.get(profile.platform_id) or []
    return " ".join(f"#{str(tag).lstrip('#')}" for tag in tags[: profile.hashtag_max])


def _compose_linkedin(
    profile: PlatformProfile,
    theme: dict[str, Any],
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
) -> str:
    """LinkedIn：專業長文，先觀點後證據，不用 emoji。"""
    blocks = [
        theme.get("title", ""),
        theme.get("angle", ""),
        f"我們在 {brief.get('business_name', '這門生意')} 的實際觀察是："
        f"{theme.get('proof_point', '')}",
        _promo_line(brief),
        brief.get("call_to_action", ""),
        _hashtags(profile, brand_voice),
    ]
    return "\n\n".join(block for block in blocks if block)


def _compose_instagram(
    profile: PlatformProfile,
    theme: dict[str, Any],
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
) -> str:
    """Instagram：視覺導向，短句換行，第一行就要讓人停下滑動。"""
    promo = _promo_line(brief)
    blocks = [
        f"☕ {theme.get('title', '')}",
        theme.get("angle", ""),
        f"✨ {theme.get('proof_point', '')}",
        f"📌 {promo}" if promo else "",
        f"👉 {brief.get('call_to_action', '')}",
        _hashtags(profile, brand_voice),
    ]
    return "\n\n".join(block for block in blocks if block)


def _compose_x(
    profile: PlatformProfile,
    theme: dict[str, Any],
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
) -> str:
    """X：一則只講一個重點，全文壓在 280 字元內。"""
    core = f"{theme.get('title', '')}：{theme.get('proof_point', '')}"
    parts = (brief.get("call_to_action", ""), _hashtags(profile, brand_voice))
    tail = " ".join(part for part in parts if part)
    return f"{core}\n\n{tail}".strip()


def _compose_facebook(
    profile: PlatformProfile,
    theme: dict[str, Any],
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
) -> str:
    """Facebook：像跟熟客聊天，結尾用問句邀請留言。"""
    blocks = [
        f"嗨，{theme.get('title', '')} 🙂",
        theme.get("angle", ""),
        theme.get("proof_point", ""),
        _promo_line(brief),
        theme.get("community_question", "你這禮拜想先試哪一款？留言告訴我們。"),
        brief.get("call_to_action", ""),
        _hashtags(profile, brand_voice),
    ]
    return "\n\n".join(block for block in blocks if block)


def _compose_generic(
    profile: PlatformProfile,
    theme: dict[str, Any],
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
) -> str:
    """未知平台的保底組稿，確保新增平台時不會直接爆掉。"""
    blocks = [
        theme.get("title", ""),
        theme.get("angle", ""),
        brief.get("call_to_action", ""),
        _hashtags(profile, brand_voice),
    ]
    return "\n\n".join(block for block in blocks if block)


OFFLINE_COMPOSERS: dict[str, Callable[..., str]] = {
    "linkedin": _compose_linkedin,
    "instagram": _compose_instagram,
    "x": _compose_x,
    "facebook": _compose_facebook,
}


def compose_offline_post(
    profile: PlatformProfile,
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
    index: int,
) -> str:
    """mock 模式的離線組稿。

    LLMClient 在 mock 下只回傳固定佔位字串，直接拿去排程等於在看一堆 [MOCK]。
    這裡改用 brief 的真實素材依平台語氣組出可讀貼文，讓 `--mock` 展示的是
    「排程流程長什麼樣」而不是佔位符。
    """
    theme = _theme_for(brief, index)
    composer = OFFLINE_COMPOSERS.get(profile.platform_id, _compose_generic)
    return composer(profile, theme, brief, brand_voice)


# --------------------------------------------------------------------------
# 產生流程
# --------------------------------------------------------------------------


def build_user_prompt(
    profile: PlatformProfile,
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
    index: int,
) -> str:
    """組出送進 LLM 的 user prompt（系統提示詞另外從 prompts/*.md 載入）。"""
    theme = _theme_for(brief, index)
    emoji_rule = "可使用" if profile.emoji_allowed else "禁止使用"
    lines = [
        f"平台：{profile.display_name}（字元上限 {profile.char_limit}）",
        f"語氣要求：{profile.tone}",
        f"emoji：{emoji_rule}；hashtag 最多 {profile.hashtag_max} 個",
        f"品牌：{brand_voice.get('brand', '')}｜{brand_voice.get('positioning', '')}",
        f"品牌語氣支柱：{'、'.join(brand_voice.get('voice_pillars') or [])}",
        f"語氣範例：{' / '.join(brand_voice.get('tone_examples') or []) or '（缺）'}",
        f"本則主題：{theme.get('title', '')}",
        f"切入角度：{theme.get('angle', '')}",
        f"可引用事實：{theme.get('proof_point', '')}",
        f"促銷：{_promo_line(brief) or '（本週無促銷，不要編造）'}",
        f"行動呼籲：{brief.get('call_to_action', '')}",
        f"禁用詞（絕對不可出現）：{brief.get('banned_words') or '（無）'}",
        "只輸出貼文本文，不要加標題、不要解釋、不要用引號包起來。",
    ]
    return "\n".join(lines)


def _tighten_prompt(user_prompt: str, profile: PlatformProfile) -> str:
    """重試時追加壓縮指示，而不是重送同一份 prompt 期待不同結果。"""
    return (
        f"{user_prompt}\n\n【重試指示】上一版超過 {profile.char_limit} 字元。"
        f"請刪到 {profile.char_limit} 字元以內，優先砍鋪陳，保留事實與行動呼籲。"
    )


def _request_text(client: Any, system_prompt: str, user_prompt: str) -> str:
    """呼叫 LLM 取得單則貼文文字。"""
    raw = client.complete(system=system_prompt, user=user_prompt, max_tokens=800)
    return (raw or "").strip()


def _generate_raw_text(
    client: Any,
    profile: PlatformProfile,
    system_prompt: str,
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
    index: int,
    max_attempts: int,
) -> tuple[str, int, list[str]]:
    """重試迴圈：超過平台上限先重新生成，用完次數仍超長才交給 validate_post 截斷。"""
    attempts = max(1, max_attempts)
    warnings: list[str] = []
    user_prompt = build_user_prompt(profile, brief, brand_voice, index)
    text = ""
    for attempt in range(1, attempts + 1):
        raw = _request_text(client, system_prompt, user_prompt)
        if not raw or raw.startswith(MOCK_MARKER):
            raw = compose_offline_post(profile, brief, brand_voice, index)
        text = raw.strip()
        if len(text) <= profile.char_limit:
            return text, attempt, warnings
        if attempt < attempts:
            warnings.append(
                f"{profile.display_name} 第 {attempt} 次產出 {len(text)} 字元超過上限，已重新生成"
            )
            user_prompt = _tighten_prompt(user_prompt, profile)
    return text, attempts, warnings


def generate_post(
    client: Any,
    profile: PlatformProfile,
    system_prompt: str,
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
    index: int,
    max_attempts: int = 2,
) -> dict[str, Any]:
    """產生單則貼文（含重試與格式驗證），回傳排程用的 dict。"""
    text, attempts, warnings = _generate_raw_text(
        client, profile, system_prompt, brief, brand_voice, index, max_attempts
    )
    banned_words = list(brief.get("banned_words") or [])
    text, post_warnings = validate_post(text, profile, banned_words)
    warnings.extend(post_warnings)
    return {
        "platform": profile.platform_id,
        "display_name": profile.display_name,
        "index": index + 1,
        "scheduled_for": profile.slot_for(index),
        "text": text,
        "char_count": len(text),
        "char_limit": profile.char_limit,
        "attempts": attempts,
        "status": "draft",
        "warnings": warnings,
    }


def generate_platform_posts(
    client: Any,
    profile: PlatformProfile,
    system_prompt: str,
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
    max_attempts: int = 2,
) -> list[dict[str, Any]]:
    """產生單一平台一週份（5-7 則）的貼文。"""
    return [
        generate_post(
            client, profile, system_prompt, brief, brand_voice, index, max_attempts
        )
        for index in range(profile.posts_per_week)
    ]


def check_brand_voice(
    brand_voice: dict[str, Any],
    min_tone_examples: int = MIN_TONE_EXAMPLES,
) -> list[str]:
    """語氣樣本不足時發警告（對應契約 KNOWN_SYMPTOMS 的 tone_mismatch）。"""
    examples = brand_voice.get("tone_examples") or []
    if len(examples) >= min_tone_examples:
        return []
    return [
        f"品牌語氣樣本只有 {len(examples)} 則，少於建議的 {min_tone_examples} 則，"
        f"產出語氣可能不符；請在 brand_voice.json 補齊真實貼文範例"
    ]


def generate_week(
    client: Any,
    profiles: list[PlatformProfile],
    brief: dict[str, Any],
    brand_voice: dict[str, Any],
    base_dir: str | Path,
    max_attempts: int = 2,
    min_tone_examples: int = MIN_TONE_EXAMPLES,
) -> dict[str, Any]:
    """產生全平台一週內容，回傳 {"posts", "warnings", "platforms"}。"""
    posts: list[dict[str, Any]] = []
    warnings: list[str] = check_brand_voice(brand_voice, min_tone_examples)
    platforms: list[dict[str, Any]] = []
    for profile in profiles:
        system_prompt = load_prompt(Path(base_dir) / profile.prompt_file)
        platform_posts = generate_platform_posts(
            client, profile, system_prompt, brief, brand_voice, max_attempts
        )
        posts.extend(platform_posts)
        warnings.extend(
            warning for post in platform_posts for warning in post["warnings"]
        )
        platforms.append(
            {
                "id": profile.platform_id,
                "display_name": profile.display_name,
                "posts": len(platform_posts),
                "char_limit": profile.char_limit,
            }
        )
    return {"posts": posts, "warnings": warnings, "platforms": platforms}


def first_line(text: str, width: int = SUMMARY_WIDTH) -> str:
    """取貼文第一行的前 width 字元，用於摘要列表。"""
    stripped = text.strip()
    head = stripped.splitlines()[0] if stripped else ""
    return head if len(head) <= width else head[:width] + TRUNCATION_SUFFIX
