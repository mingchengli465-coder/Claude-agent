"""The owner's agent: in their own Telegram chat the bot can build and edit
websites and draft posts, not just talk.

The chat model gets four tools. Websites are generated as one self-contained
HTML page, stored in SQLite and served by web.py at /s/<slug>. Posts are only
ever drafted here: the bot shows the draft with 发布 / 取消 buttons, and
nothing goes out until the owner presses 发布 (bot.py handles that).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SITE_TIMEOUT = float(os.environ.get("AGENT_SITE_TIMEOUT", "240"))
CHAT_TIMEOUT = float(os.environ.get("AGENT_CHAT_TIMEOUT", "90"))
MAX_ROUNDS = 5
PLATFORM_NAMES = {"x": "X", "bluesky": "Bluesky", "channel": "Telegram 频道"}
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


class SiteStore:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS sites (
            slug TEXT PRIMARY KEY, chat_id TEXT NOT NULL, title TEXT DEFAULT '',
            html TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)""")
        self.db.commit()

    def get(self, slug: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sites WHERE slug = ?", (slug,)).fetchone()

    def list(self, chat_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT slug, title, updated FROM sites WHERE chat_id = ? ORDER BY updated DESC",
                               (str(chat_id),)).fetchall()

    def free_slug(self, wanted: str) -> str:
        base = re.sub(r"[^a-z0-9-]+", "-", (wanted or "").lower()).strip("-")[:30]
        if not _SLUG.match(base or ""):
            base = "site-" + secrets.token_hex(3)
        slug, n = base, 2
        while self.get(slug) is not None:
            slug, n = f"{base}-{n}", n + 1
        return slug

    def save(self, slug: str, chat_id: str, title: str, html: str) -> None:
        now = time.time()
        self.db.execute("""INSERT INTO sites (slug, chat_id, title, html, created, updated) VALUES (?, ?, ?, ?, ?, ?)
                           ON CONFLICT (slug) DO UPDATE SET title = excluded.title, html = excluded.html,
                           updated = excluded.updated""", (slug, str(chat_id), title, html, now, now))
        self.db.commit()


SITE_SYSTEM = """你是一位顶尖的网页设计师和前端工程师。根据要求写一个完整的单页网站。

要求：
- 只输出一个完整的 HTML 文件（从 <!doctype html> 开始，到 </html> 结束），不要任何解释，不要用 ``` 包起来。
- 所有 CSS、JS 都写在这个文件里。不引用任何外部文件、字体或图片（中国大陆打不开 Google Fonts 等）。
  字体用系统字体：-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif。
- 图片用 emoji、内联 SVG、CSS 渐变或几何图形代替，不要用图片链接。
- 手机和电脑都要好看：响应式布局，手机宽度下不能出现横向滚动。
- 设计要精致、现代、有质感：合理的留白、层次、配色，可以有细腻的动效。不要做成模板感很重的样子。
- 用户用什么语言提要求，网站就用什么语言，除非用户另外指定。
- 用户没给的信息（电话、地址、价格等），用明显的占位写法，比如「电话：待填写」，不要编造看起来真实的联系方式。
- <title> 要写网站的名字。"""

EDIT_SYSTEM = """你是一位顶尖的前端工程师。下面是一个网站现在的完整 HTML，按用户的修改要求改好它。
- 只输出改好后的完整 HTML 文件，不要任何解释，不要用 ``` 包起来。
- 只改用户要求的部分，其他内容和风格保持不变。
- 仍然不引用任何外部文件、字体或图片。"""

AGENT_SYSTEM = """你是主人的私人助理，住在 Telegram 里。你可以正常聊天、回答问题、写东西，也可以用工具帮主人做事：
- make_website：建一个新网站（会自动上线，给主人网址）
- edit_website：修改已经建好的网站
- list_websites：看主人建过哪些网站
- draft_post：写一条要发到社交平台的帖子草稿

规则：
- 主人要做网站时，直接调用 make_website，把主人的要求完整、具体地写进 request（行业、要哪些板块、风格、颜色、联系方式等）。
  要求太模糊时（比如只说"做个网站"），先问一句是做什么的网站。
- 主人要改网站时用 edit_website；不确定改哪个就先 list_websites。
- 主人要发帖时，一定用 draft_post 写好草稿，不要说已经发了。草稿会给主人看，主人点「发布」才会真的发出去。
  现在能发的平台：{platforms}。X 的正文最多 280 字符（中文一个字算 2），正文里不要放链接。
- 工具做完后，用一两句话告诉主人结果，网站要把网址给他。
- 回复简洁，用主人的语言。"""

TOOLS = [
    {"type": "function", "function": {
        "name": "make_website",
        "description": "建一个新的单页网站并上线，返回网址。",
        "parameters": {"type": "object", "properties": {
            "request": {"type": "string", "description": "网站的完整要求：做什么的、要哪些板块、风格、颜色、文字内容、联系方式等"},
            "slug": {"type": "string", "description": "网址里用的英文短名，小写字母、数字、横线，比如 cake-shop"},
        }, "required": ["request"]}}},
    {"type": "function", "function": {
        "name": "edit_website",
        "description": "按要求修改一个已经建好的网站，网址不变。",
        "parameters": {"type": "object", "properties": {
            "slug": {"type": "string", "description": "要改的网站的短名"},
            "change": {"type": "string", "description": "具体要怎么改"},
        }, "required": ["slug", "change"]}}},
    {"type": "function", "function": {
        "name": "list_websites",
        "description": "列出主人建过的网站和网址。",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "draft_post",
        "description": "写一条社交平台帖子的草稿，发给主人确认。主人点发布才会发出。",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "帖子正文"},
            "platforms": {"type": "array", "items": {"type": "string", "enum": ["x", "bluesky", "channel"]},
                          "description": "要发到哪些平台"},
        }, "required": ["text", "platforms"]}}},
]


def clean_html(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    start = text.lower().find("<!doctype")
    if start < 0:
        start = text.lower().find("<html")
    end = text.lower().rfind("</html>")
    if start < 0 or end < 0:
        raise ValueError("模型没有给出完整的网页")
    return text[start:end + len("</html>")]


def page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:80] if m else ""


class Agent:
    def __init__(self, client, model: str, store: SiteStore, base_url: str, platforms: list[str],
                 on_draft=None, on_progress=None):
        self.client = client
        self.model = model
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.platforms = platforms  # which of x / bluesky / channel are set up
        self.on_draft = on_draft        # async (chat_id, text, platforms) -> None
        self.on_progress = on_progress  # async (chat_id, text) -> None

    def url(self, slug: str) -> str:
        return f"{self.base_url}/s/{slug}"

    async def _complete(self, messages: list[dict], timeout: float, **kw):
        return await asyncio.wait_for(self.client.chat.completions.create(
            model=self.model, messages=messages, **kw), timeout)

    async def _html(self, system: str, prompt: str) -> str:
        response = await self._complete([{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                                        SITE_TIMEOUT, max_tokens=8000, temperature=0.7)
        return clean_html(response.choices[0].message.content or "")

    async def _progress(self, chat_id, text: str) -> None:
        if self.on_progress is not None:
            try:
                await self.on_progress(chat_id, text)
            except Exception:  # noqa: BLE001 - a status line is not worth failing over
                logger.debug("进度消息没发出去", exc_info=True)

    # ---- tools ---------------------------------------------------------------------

    async def make_website(self, chat_id, request: str, slug: str = "") -> dict:
        await self._progress(chat_id, "🛠 正在做网站，大概 1～2 分钟…")
        html = await self._html(SITE_SYSTEM, request)
        slug = self.store.free_slug(slug or page_title(html))
        self.store.save(slug, chat_id, page_title(html), html)
        return {"ok": True, "slug": slug, "url": self.url(slug), "title": page_title(html)}

    async def edit_website(self, chat_id, slug: str, change: str) -> dict:
        site = self.store.get(slug)
        if site is None or site["chat_id"] != str(chat_id):
            return {"ok": False, "error": f"没有叫 {slug} 的网站", "sites": [r["slug"] for r in self.store.list(chat_id)]}
        await self._progress(chat_id, "🛠 正在修改网站…")
        html = await self._html(EDIT_SYSTEM, f"【修改要求】\n{change}\n\n【现在的 HTML】\n{site['html']}")
        self.store.save(slug, chat_id, page_title(html) or site["title"], html)
        return {"ok": True, "slug": slug, "url": self.url(slug)}

    async def list_websites(self, chat_id) -> dict:
        return {"sites": [{"slug": r["slug"], "title": r["title"], "url": self.url(r["slug"])}
                          for r in self.store.list(chat_id)]}

    async def draft_post(self, chat_id, text: str, platforms: list[str]) -> dict:
        chosen = [p for p in platforms if p in self.platforms]
        if not chosen:
            return {"ok": False, "error": "这些平台还没接上", "available": self.platforms}
        if self.on_draft is not None:
            await self.on_draft(chat_id, text.strip(), chosen)
        return {"ok": True, "note": "草稿已发给主人，等他点「发布」才会发出去。不要说已经发了。"}

    # ---- the loop ------------------------------------------------------------------

    async def run(self, chat_id, history: list[dict], user_text: str) -> str:
        platforms = "、".join(PLATFORM_NAMES[p] for p in self.platforms) or "（还没有接上任何平台）"
        messages = [{"role": "system", "content": AGENT_SYSTEM.format(platforms=platforms)}, *history,
                    {"role": "user", "content": user_text}]
        tools = {"make_website": self.make_website, "edit_website": self.edit_website,
                 "list_websites": self.list_websites, "draft_post": self.draft_post}
        for _ in range(MAX_ROUNDS):
            response = await self._complete(messages, CHAT_TIMEOUT, tools=TOOLS, temperature=0.5)
            msg = response.choices[0].message
            calls = list(getattr(msg, "tool_calls", None) or [])
            if not calls:
                return (msg.content or "").strip() or "好的～"
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                    result = await tools[c.function.name](chat_id, **args)
                except Exception as exc:  # noqa: BLE001 - the model hears what went wrong and says so
                    logger.exception("工具 %s 出错", c.function.name)
                    result = {"ok": False, "error": str(exc)[:300]}
                messages.append({"role": "tool", "tool_call_id": c.id, "content": json.dumps(result, ensure_ascii=False)})
        return "这件事步骤有点多，我先做到这里了，你看看结果，还需要什么再跟我说～"
