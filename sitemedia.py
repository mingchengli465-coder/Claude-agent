"""The owner's own pictures and video for the site, sent to the bot in Telegram.

    hero.mp4        the whale video behind the homepage (and every demo page's first screen)
    art-top.jpg     the dead wood on the second page
    art-bottom.jpg  the same wood grown over with moss and flowers, revealed under the cursor

A video is always the hero. A picture goes where its caption says ("枯木" / "苔藓", "上层" / "底层"…);
without a caption, a green picture is the bloom and any other is the dead wood. Until they're sent,
the pages draw their own (web.py, web_static/art).
"""

from __future__ import annotations

import io

from PIL import Image

HERO, TOP, BOTTOM = "hero.mp4", "art-top.jpg", "art-bottom.jpg"
LABELS = {HERO: "首页背景视频（鲸鱼）", TOP: "第二页上层（枯木）", BOTTOM: "第二页底层（枯木生春，鼠标移过去才露出来）"}
# "枯木生春" names the bloom but contains "枯木": the bloom's words are checked first
BOTTOM_WORDS = ("生春", "苔", "花", "底层", "底層", "下层", "下層", "bottom", "bloom", "moss", "spring", "flower")
TOP_WORDS = ("枯木", "上层", "上層", "top", "dead", "wood")
MAX_SIDE = 2560
TELEGRAM_LIMIT = 20 * 1024 * 1024  # what a bot may download


def greenness(data: bytes) -> float:
    """The share of a picture's lit pixels that are green (moss: a good part; embers and bark: none)."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((200, 200))
    lit = [(r, g, b) for r, g, b in img.getdata() if r + g + b > 60]
    if not lit:
        return 0.0
    return sum(1 for r, g, b in lit if g > r * 1.1 and g > b * 1.1) / len(lit)


def slot_for(video: bool, caption: str = "", data: bytes | None = None) -> str:
    if video:
        return HERO
    text = (caption or "").lower()
    if any(w in text for w in BOTTOM_WORDS):
        return BOTTOM
    if any(w in text for w in TOP_WORDS):
        return TOP
    return BOTTOM if data is not None and greenness(data) > 0.15 else TOP


def as_jpeg(data: bytes) -> bytes:
    """Any picture as a web-sized JPEG."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=86, optimize=True, progressive=True)
    return out.getvalue()
