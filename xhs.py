"""Xiaohongshu (小红书) note generation: topic pick, copy, and cover image.

Each note is one OpenRouter call that returns JSON, plus a cover rendered
locally with Pillow. Nothing here touches Telegram — bot.py wires it up.
"""

import asyncio
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Falls back to the chat model so a single MODEL setting configures both.
XHS_MODEL = os.environ.get("XHS_MODEL") or os.environ.get("MODEL", "deepseek/deepseek-chat-v3.1:free")
XHS_REQUEST_TIMEOUT = float(os.environ.get("XHS_REQUEST_TIMEOUT", "120"))
XHS_STATE_FILE = Path(os.environ.get("XHS_STATE_FILE", "xhs_state.json"))
# Topics from the last N days are shown to the model as things to avoid.
XHS_AVOID_DAYS = int(os.environ.get("XHS_AVOID_DAYS", "7"))

FONT_PATH = Path(os.environ.get("XHS_FONT_PATH", Path(__file__).parent / "fonts" / "NotoSansSC-VF.ttf"))

# Rotated one per day so no single area dominates the account.
DOMAINS = [
    "搞钱与职场",
    "消费观",
    "感情与生活选择",
    "AI 与未来",
    "年轻人现状",
    "反常识观点",
]

# --- cover design ----------------------------------------------------------
COVER_W, COVER_H = 1080, 1440
YELLOW = (255, 225, 77)      # #FFE14D
BLACK = (17, 17, 17)
RED = (232, 50, 46)          # #E8322E
WHITE = (255, 255, 255)
MARGIN = 76
BADGE_TEXT = "真实经历"

client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
    timeout=XHS_REQUEST_TIMEOUT,
) if OPENROUTER_API_KEY else None


@dataclass
class Note:
    """One generated note: the copy plus the text that goes on the cover."""
    domain: str
    topic: str
    title: str
    body: str
    tags: list[str]
    cover_main: list[str] = field(default_factory=list)
    cover_question: str = ""
    cover_small: list[str] = field(default_factory=list)

    def tags_text(self) -> str:
        return "  ".join(f"#{t}" for t in self.tags)


class GenerationError(RuntimeError):
    """Raised when the model could not produce a usable note."""


# --------------------------------------------------------------------------- #
# State: which domain is next, and what we've already written
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    if not XHS_STATE_FILE.exists():
        return {"domain_index": 0, "history": []}
    try:
        state = json.loads(XHS_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read %s; starting fresh", XHS_STATE_FILE, exc_info=True)
        return {"domain_index": 0, "history": []}
    state.setdefault("domain_index", 0)
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    state["history"] = (state.get("history") or [])[-60:]
    tmp = XHS_STATE_FILE.with_suffix(XHS_STATE_FILE.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, XHS_STATE_FILE)
    except OSError:
        logger.error("Could not persist %s", XHS_STATE_FILE, exc_info=True)


def recent_topics(state: dict, days: int = XHS_AVOID_DAYS) -> list[str]:
    """Topics written within the last `days` days, newest first."""
    cutoff = date.today() - timedelta(days=days)
    out = []
    for entry in reversed(state.get("history") or []):
        try:
            when = date.fromisoformat(entry.get("date", ""))
        except ValueError:
            continue
        if when >= cutoff:
            out.append(f"{entry.get('topic', '')}（{entry.get('title', '')}）")
    return out


def take_domain(state: dict) -> str:
    index = int(state.get("domain_index", 0)) % len(DOMAINS)
    state["domain_index"] = (index + 1) % len(DOMAINS)
    return DOMAINS[index]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """你是资深的小红书内容写手，擅长写出评论区会吵起来的观点型笔记。
你只输出简体中文，只输出 JSON，不输出任何解释、前言或 Markdown 代码块标记。"""

# These four are non-negotiable and repeated in every request.
HARD_RULES = """【必须遵守的硬性规则，违反即视为失败】
1. 不攻击、不贬低任何群体：性别、地域、民族、职业、年龄、学历、体型、婚育状况等都不行。
   可以对"某种做法"或"某种观念"表达强烈态度，但不能指向某一类人。
2. 不编造新闻事件、统计数据、研究结论或名人言论。正文里的细节只能是个人化的生活经历，
   不能写成"某媒体报道""数据显示""某某说过"这类伪事实。
3. 不给医疗建议（含用药、诊断、心理诊断），不给投资理财建议（含推荐标的、预测涨跌、收益承诺）。
4. 不涉及政治、时政、政策评价、国际关系。"""


def _build_user_prompt(domain: str, avoid: list[str]) -> str:
    avoid_block = "\n".join(f"- {t}" for t in avoid) if avoid else "（暂无，随便挑）"
    return f"""请就下面这个领域，写一篇小红书笔记。

【领域】{domain}

【最近 {XHS_AVOID_DAYS} 天已经写过的选题，不要重复，也不要换个说法写同一件事】
{avoid_block}

【选题要求】
挑一个观点对立、评论区会吵起来的话题。要具体到一个场景或一个决定，不要空泛的大道理。

【文案要求】
- 标题：20 字以内，立场鲜明，可以带 emoji
- 正文：300-600 字，第一人称，要有具体细节（时间、金额、场景、对话），
  口语化，多分段（每段 1-3 句，段与段之间空一行），
  结尾抛出一个二选一的问题或反问，引导读者在评论区站队
- 话题标签：6-8 个，不要带 # 号
- 封面文案：主标 2 行（每行 6-10 字，短促有力）、
  争议问句 1 句（15 字以内）、小字 3 行（每行 8-14 字）

{HARD_RULES}

【输出格式】严格输出下面结构的 JSON，不要加代码块标记：
{{
  "topic": "这篇的选题，一句话概括",
  "title": "标题",
  "body": "正文，段落之间用 \\n\\n 分隔",
  "tags": ["标签1", "标签2", "标签3", "标签4", "标签5", "标签6"],
  "cover": {{
    "main": ["主标第一行", "主标第二行"],
    "question": "争议问句",
    "small": ["小字第一行", "小字第二行", "小字第三行"]
  }}
}}"""


def _extract_json(raw: str) -> dict:
    """Pull the JSON object out of a reply that may be fenced or padded."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise GenerationError("模型没有返回 JSON")
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise GenerationError(f"返回的 JSON 无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise GenerationError("返回的 JSON 不是对象")
    return data


def _clean_tag(tag: str) -> str:
    return re.sub(r"^#+", "", str(tag)).strip()


def _normalize(data: dict, domain: str) -> Note:
    """Coerce the model's JSON into the shape the sender and cover expect.

    Soft violations (a 22-character title, 9 tags) are trimmed rather than
    retried — a retry costs a call and usually returns the same shape.
    """
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title or not body:
        raise GenerationError("标题或正文为空")

    if len(title) > 20:
        title = title[:20]

    tags = [t for t in (_clean_tag(t) for t in (data.get("tags") or [])) if t][:8]
    while len(tags) < 6:
        tags.append(domain.replace(" ", ""))

    cover = data.get("cover") or {}
    main = [str(x).strip() for x in (cover.get("main") or []) if str(x).strip()][:2]
    small = [str(x).strip() for x in (cover.get("small") or []) if str(x).strip()][:3]
    question = str(cover.get("question") or "").strip()

    # The cover must always have something to draw, even on a lazy reply.
    if not main:
        main = [title[:10], title[10:20]] if len(title) > 10 else [title]
    if not question:
        question = "你会怎么选？"
    while len(small) < 3:
        small.append("")

    return Note(
        domain=domain,
        topic=str(data.get("topic") or title).strip(),
        title=title,
        body=body,
        tags=tags,
        cover_main=main,
        cover_question=question,
        cover_small=small,
    )


async def _one_call(domain: str, avoid: list[str]) -> Note:
    if client is None:
        raise GenerationError("OPENROUTER_API_KEY 没有设置")
    response = await client.chat.completions.create(
        model=XHS_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(domain, avoid)},
        ],
        temperature=1.0,
    )
    if not response.choices:
        raise GenerationError("模型返回了空的 choices")
    return _normalize(_extract_json(response.choices[0].message.content or ""), domain)


async def generate_note(domain: str | None = None, state: dict | None = None) -> Note:
    """Generate one note, retrying once before giving up.

    A malformed reply counts as a failure just like a transport error, since
    both leave us without a usable note.
    """
    own_state = state is None
    state = load_state() if own_state else state
    chosen = domain or take_domain(state)
    avoid = recent_topics(state)

    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            note = await _one_call(chosen, avoid)
        except Exception as exc:  # noqa: BLE001 - retry on anything, then report
            last_error = exc
            logger.warning("小红书生成第 %d 次失败：%s", attempt, exc, exc_info=True)
            continue

        state.setdefault("history", []).append(
            {
                "date": date.today().isoformat(),
                "domain": chosen,
                "topic": note.topic,
                "title": note.title,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        save_state(state)
        return note

    if own_state:
        save_state(state)  # Keep the domain rotation moving past a dead topic.
    raise GenerationError(f"连续两次生成失败：{last_error}") from last_error


# --------------------------------------------------------------------------- #
# Cover rendering
# --------------------------------------------------------------------------- #

_font_cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}


def load_font(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    """Load the bundled variable font at a named weight, cached by (size, weight)."""
    key = (size, weight)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    if not FONT_PATH.exists():
        raise GenerationError(
            f"找不到字体文件 {FONT_PATH}。仓库的 fonts/ 目录里应该有 NotoSansSC-VF.ttf。"
        )
    font = ImageFont.truetype(str(FONT_PATH), size)
    try:
        font.set_variation_by_name(weight)
    except Exception:  # noqa: BLE001 - a static build just stays at its own weight
        logger.debug("字体不支持可变字重 %s，使用默认字重", weight, exc_info=True)
    _font_cache[key] = font
    return font


# Punctuation that must not start a line.
_NO_LEAD = "。，、；：？！）」』】》%…·.,;:?!)]}"


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Wrap CJK text character by character, keeping punctuation off line starts."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines: list[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + ch
        if current and draw.textlength(trial, font=font) > max_width:
            if ch in _NO_LEAD and len(current) > 1:
                # Pull one character down so the punctuation isn't orphaned.
                lines.append(current[:-1])
                current = current[-1] + ch
            else:
                lines.append(current)
                current = ch
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def fit_lines(
    lines: list[str],
    box_w: int,
    box_h: int,
    weight: str,
    max_size: int,
    min_size: int = 22,
    spacing: float = 1.22,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest size at which `lines` fit the box, re-wrapping if they still don't."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text = [ln for ln in lines if ln]
    for size in range(max_size, min_size - 1, -2):
        font = load_font(size, weight)
        if all(draw.textlength(ln, font=font) <= box_w for ln in text):
            if len(text) * size * spacing <= box_h:
                return font, text
    # Still too big at the minimum: re-flow everything and clip to the box.
    font = load_font(min_size, weight)
    flowed: list[str] = []
    for ln in text:
        flowed.extend(wrap_text(ln, font, box_w))
    max_lines = max(1, int(box_h // (min_size * spacing)))
    return font, flowed[:max_lines]


def _draw_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    fill,
    spacing: float = 1.22,
    center_w: int | None = None,
) -> int:
    """Draw lines top-down; returns the y just below the block."""
    step = int(font.size * spacing)
    for i, line in enumerate(lines):
        lx = x
        if center_w is not None:
            lx = x + (center_w - draw.textlength(line, font=font)) / 2
        draw.text((lx, y + i * step), line, font=font, fill=fill)
    return y + len(lines) * step


def _draw_badge(draw: ImageDraw.ImageDraw, x: int, y: int) -> int:
    """Black tag in the top-left corner. Returns the y below it."""
    font = load_font(40, "Bold")
    tw = draw.textlength(BADGE_TEXT, font=font)
    pad_x, pad_y = 26, 14
    w, h = int(tw + pad_x * 2), int(font.size + pad_y * 2)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=BLACK)
    draw.text((x + pad_x, y + pad_y - 4), BADGE_TEXT, font=font, fill=WHITE)
    return y + h


def _question_layout(question: str, box_w: int) -> tuple[ImageFont.FreeTypeFont, list[str], int, int]:
    """Measure the red question box before drawing it: (font, lines, box_h, pad_x)."""
    pad_x, pad_y = 34, 28
    inner_w = box_w - pad_x * 2
    font, lines = fit_lines([question], inner_w, 320, "Bold", 64, min_size=30)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    if len(lines) == 1 and draw.textlength(lines[0], font=font) > inner_w:
        lines = wrap_text(lines[0], font, inner_w)
    box_h = len(lines) * int(font.size * 1.24) + pad_y * 2
    return font, lines, box_h, pad_x


def _draw_question(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    lines: list[str],
    x: int,
    y: int,
    box_w: int,
    box_h: int,
    pad_x: int,
) -> int:
    """Red rounded box holding the argumentative question. Returns y below it."""
    draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=26, fill=RED)
    _draw_block(draw, lines, font, x + pad_x, y + 28, WHITE, 1.24, center_w=box_w - pad_x * 2)
    return y + box_h


def render_cover(note: Note, variant: int = 0) -> bytes:
    """Render the 1080x1440 cover as PNG bytes. `variant` reorders the blocks."""
    img = Image.new("RGB", (COVER_W, COVER_H), YELLOW)
    draw = ImageDraw.Draw(img)
    x = MARGIN
    box_w = COVER_W - MARGIN * 2

    badge_bottom = _draw_badge(draw, x, MARGIN)

    # Lay every block out first, so the leftover height can be shared as gaps
    # instead of pooling at the bottom of the frame.
    main_spacing, small_spacing = 1.16, 1.38
    main_font, main_lines = fit_lines(note.cover_main, box_w, 620, "Black", 168, min_size=48)
    small_font, small_lines = fit_lines(
        [ln for ln in note.cover_small if ln], box_w, 300, "Medium", 50, min_size=24
    )
    q_font, q_lines, q_h, q_pad = _question_layout(note.cover_question, box_w)

    main_h = len(main_lines) * int(main_font.size * main_spacing)
    small_h = len(small_lines) * int(small_font.size * small_spacing)

    blocks = {"main": main_h, "question": q_h, "small": small_h}
    order = ["main", "question", "small"]
    if variant % 3 == 1:
        order = ["main", "small", "question"]
    elif variant % 3 == 2:
        order = ["question", "main", "small"]

    top = badge_bottom + 60
    available = (COVER_H - MARGIN) - top
    slack = available - sum(blocks.values())
    # Two gaps between three blocks; keep them generous but bounded, and let
    # whatever is left push the group down so it sits off the badge.
    gap = max(40, min(int(slack / 2), 150)) if slack > 0 else 34
    y = top + max(0, min(int((slack - gap * 2) * 0.45), 220))

    for i, kind in enumerate(order):
        if kind == "main":
            y = _draw_block(draw, main_lines, main_font, x, y, BLACK, main_spacing)
        elif kind == "question":
            y = _draw_question(draw, q_font, q_lines, x, y, box_w, q_h, q_pad)
        else:
            y = _draw_block(draw, small_lines, small_font, x, y, BLACK, small_spacing)
        if i < len(order) - 1:
            y += gap

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def render_cover_async(note: Note, variant: int = 0) -> bytes:
    """Pillow is blocking; keep it off the event loop."""
    return await asyncio.to_thread(render_cover, note, variant)
