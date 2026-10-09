"""The day's ten most important world events, with a YouTube video and the X conversation for each.

Once a day (NEWS_TIME in NEWS_TIMEZONE) the bot:
    1. reads the last 36 hours of headlines from international newsrooms' RSS feeds
       (BBC, Al Jazeera, Guardian, NYT, NPR, DW, France 24, Sky, CNBC, Google News, BBC 中文)
    2. reads the latest uploads of news channels on YouTube (their public channel feeds)
    3. asks the AI to pick the ten events that matter most to the world, write each up
       in Chinese, and match each to one of those YouTube videos when one is about it
    4. sends the list to NEWS_CHAT_ID: a source article, a YouTube video (or a YouTube
       search of today's uploads when no channel covered it), and an X search of the event

No keys beyond the AI's: feeds need no account. X has no free search API, so the X link
opens X's own search for the event (top posts), not one hand-picked post.

Environment:
    NEWS_CHAT_ID     where to send it (comma-separated Telegram chat IDs); empty = off
    NEWS_TIME        default 08:00
    NEWS_TIMEZONE    default Asia/Shanghai
    NEWS_STATE_PATH  remembers the last day sent (default: next to CS_DB_PATH)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import logging
import re
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Guardian", "https://www.theguardian.com/world/rss"),
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"),
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("NPR", "https://feeds.npr.org/1004/rss.xml"),
    ("DW", "https://rss.dw.com/rdf/rss-en-all"),
    ("France 24", "https://www.france24.com/en/rss"),
    ("Sky News", "https://feeds.skynews.com/feeds/rss/world.xml"),
    ("CNBC", "https://www.cnbc.com/id/100727362/device/rss/rss.html"),
    ("Google News", "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"),
    ("Google News", "https://news.google.com/rss?hl=en-SG&gl=SG&ceid=SG:en"),
    ("BBC 中文", "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml"),
]
# News channels on YouTube: https://www.youtube.com/feeds/videos.xml?channel_id=<id> lists the latest uploads
VIDEO_CHANNELS = [
    ("BBC News", "UC16niRr50-MSBwiO3YDb3RA"),
    ("Reuters", "UChqUTb7kYRX8-EiaN3XFrSQ"),
    ("AP", "UC52X5wxOL_s5yw0dQk7NtgA"),
    ("Al Jazeera English", "UCNye-wNBqNL5ZzHSJj3l8Bg"),
    ("DW News", "UCknLrEdhRCp1aegoMqRaCZg"),
    ("Sky News", "UCoMdktPbSTixAyNGwb-UYkQ"),
    ("CNA", "UC83jt4dlz1Gjl58fzQrrKZg"),
    ("FRANCE 24 English", "UCQfwfsi5VrQ8yKZ-UWmAEFg"),
    ("Guardian News", "UCHpw8xwDNhU9gdohEcJu4aA"),
    ("CNN", "UCupvZG-5ko_eiXAupbDfxWw"),
    ("NBC News", "UCeY0bbntWzzVIaj2z3QigXg"),
    ("WION", "UC_gUM8rL-Lrg6O3adPW9K1g"),
]
YT_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={}"
USER_AGENT = "Mozilla/5.0 (compatible; vinc-news/1.0; +https://github.com/mingchengli465-coder/Claude-agent)"
WINDOW_HOURS = 36       # headlines older than this are yesterday's news
PER_FEED = 25           # headlines kept from one feed
MAX_HEADLINES = 220     # what the AI reads
MAX_VIDEOS = 160
EVENTS = 10
AI_TIMEOUT = 240        # seconds: a long read for the model
CHUNK = 3800            # Telegram allows 4096 characters a message
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_JSON = re.compile(r"\{.*\}", re.S)

PROMPT = """你是国际新闻编辑。下面是过去 {hours} 小时各大国际媒体的新闻标题（H 开头）和新闻频道在 YouTube 上的最新视频（V 开头）。

请选出对全人类最重要的 {n} 件事，按重要性从高到低排：
- 优先：战争与和平、外交与重大政策、重大灾难与伤亡、全球经济与市场、科技与科学突破、公共卫生、气候
- 多家媒体都在报道的事件更重要；同一件事只算一次
- 不要娱乐八卦，体育只有特别重大时才选
- 每件事写一个简体中文标题（20 字内）和一两句中文摘要（60 字内），只写新闻里有的事实

每件事要给：
- "source"：最能代表这件事的一条 H 编号（数字），尽量选原媒体，少选 Google News
- "video"：讲这件事的一条 V 编号（数字）；没有真正对应的视频就填 null，宁缺毋滥
- "x_query"：在 X 上搜这件事用的英文关键词（2 到 5 个词）

只输出 JSON，格式：
{{"events": [{{"title": "...", "summary": "...", "source": 3, "video": 12, "x_query": "..."}}]}}

新闻标题：
{headlines}

YouTube 视频：
{videos}
"""


@dataclass
class Item:
    source: str
    title: str
    link: str
    summary: str = ""
    published: dt.datetime | None = None


@dataclass
class Event:
    title: str
    summary: str
    source: Item | None
    video: Item | None
    x_query: str


# --------------------------------------------------------------------------- #
# Reading feeds (RSS 2.0, RSS 1.0 / RDF, Atom)
# --------------------------------------------------------------------------- #

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, *names: str) -> ET.Element | None:
    for child in node:
        if _local(child.tag) in names:
            return child
    return None


def _text(node: ET.Element, *names: str) -> str:
    child = _child(node, *names)
    return (child.text or "").strip() if child is not None else ""


def _clean(text: str, limit: int = 220) -> str:
    text = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _date(value: str) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def parse_feed(data: bytes, source: str) -> list[Item]:
    """Headlines (or videos) of one feed, newest first as the feed lists them."""
    root = ET.fromstring(data)
    items: list[Item] = []
    for node in root.iter():
        kind = _local(node.tag)
        if kind not in ("item", "entry"):
            continue
        title = _clean(_text(node, "title"), 300)
        link = _text(node, "link")
        if not link:  # Atom: <link href="..."/>
            for child in node:
                if _local(child.tag) == "link" and child.get("rel", "alternate") == "alternate":
                    link = child.get("href", "")
                    break
        video_id = _text(node, "videoId")
        if video_id:
            link = f"https://www.youtube.com/watch?v={video_id}"
        if not (title and link):
            continue
        summary = _text(node, "description", "summary", "content")
        group = _child(node, "group")  # YouTube puts the description in media:group
        if not summary and group is not None:
            summary = _text(group, "description")
        published = _date(_text(node, "pubDate", "published", "date", "updated"))
        items.append(Item(source=source, title=title, link=link.strip(), summary=_clean(summary), published=published))
    return items


async def _fetch(session: ClientSession, source: str, url: str) -> list[Item]:
    async with session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"}) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return parse_feed(await r.read(), source)


async def fetch_all(feeds: list[tuple[str, str]], session: ClientSession | None = None) -> tuple[list[list[Item]], list[str]]:
    """Every feed's items (one list per feed, in order) and the feeds that failed."""
    own = session is None
    session = session or ClientSession(timeout=ClientTimeout(total=25))
    try:
        results = await asyncio.gather(*(_fetch(session, name, url) for name, url in feeds), return_exceptions=True)
    finally:
        if own:
            await session.close()
    lists, failed = [], []
    for (name, url), result in zip(feeds, results):
        if isinstance(result, BaseException):
            failed.append(f"{name}（{str(result) or type(result).__name__}）")
            lists.append([])
        else:
            lists.append(result)
    return lists, failed


def recent(lists: list[list[Item]], now: dt.datetime, hours: int = WINDOW_HOURS, per_feed: int = PER_FEED,
           limit: int = MAX_HEADLINES) -> list[Item]:
    """Fresh items, a few from every feed in turn (so no single feed crowds out the rest), no repeats."""
    cutoff = now - dt.timedelta(hours=hours)
    kept = [[i for i in items if i.published is None or i.published >= cutoff][:per_feed] for items in lists]
    out, seen = [], set()
    for row in range(per_feed):
        for items in kept:
            if row < len(items):
                key = re.sub(r"\W+", "", items[row].title.lower())[:80]
                if key not in seen:
                    seen.add(key)
                    out.append(items[row])
    return out[:limit]


# --------------------------------------------------------------------------- #
# Choosing the ten
# --------------------------------------------------------------------------- #

def build_prompt(headlines: list[Item], videos: list[Item], hours: int = WINDOW_HOURS, n: int = EVENTS) -> str:
    h = "\n".join(f"H{i} [{it.source}] {it.title}" + (f" — {it.summary}" if it.summary else "")
                  for i, it in enumerate(headlines, 1))
    v = "\n".join(f"V{i} [{it.source}] {it.title}" for i, it in enumerate(videos, 1)) or "（没有）"
    return PROMPT.format(hours=hours, n=n, headlines=h, videos=v)


def _pick(items: list[Item], ref) -> Item | None:
    if ref is None:
        return None
    try:
        index = int(str(ref).strip().lstrip("HVhv"))
    except ValueError:
        return None
    return items[index - 1] if 1 <= index <= len(items) else None


def parse_events(text: str, headlines: list[Item], videos: list[Item], n: int = EVENTS) -> list[Event]:
    found = _JSON.search(text or "")
    if not found:
        raise ValueError("AI 没有给出 JSON")
    data = json.loads(found.group(0))
    events = []
    for row in data.get("events") or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        events.append(Event(title=title, summary=str(row.get("summary") or "").strip(),
                            source=_pick(headlines, row.get("source")), video=_pick(videos, row.get("video")),
                            x_query=str(row.get("x_query") or "").strip() or title))
    if not events:
        raise ValueError("AI 没有选出任何事件")
    return events[:n]


async def choose(attempts: list[tuple], prompt: str, parse) -> list[Event]:
    """The AI's events, trying each (client, model) in turn: first in strict JSON mode, then plain
    (not every model takes response_format); an answer that isn't usable moves on to the next model."""
    last: Exception | None = None
    for api, model in attempts:
        for strict in (True, False):
            kwargs = dict(model=model, temperature=0.3, max_tokens=8000,
                          messages=[{"role": "user", "content": prompt}])
            if strict:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                response = await asyncio.wait_for(api.chat.completions.create(**kwargs), AI_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - plain mode next, then the next model
                logger.warning("新闻：%s%s 失败：%s", model, "（JSON 模式）" if strict else "", exc)
                last = exc
                continue
            text = (response.choices[0].message.content or "").strip() if response.choices else ""
            try:
                return parse(text)
            except ValueError as exc:
                logger.warning("新闻：%s 的回答用不了（%s）：%s", model, exc, text[:300].replace("\n", " "))
                last = exc
                break  # same model, same prompt: likely the same answer; try the next model
    raise RuntimeError(f"AI 都失败了：{last}")


# --------------------------------------------------------------------------- #
# The message
# --------------------------------------------------------------------------- #

def x_search(query: str) -> str:
    return "https://x.com/search?" + urllib.parse.urlencode({"q": query, "src": "typed_query", "f": "top"})


def youtube_search(query: str) -> str:
    # sp=EgIIAg%3D%3D: YouTube's "uploaded today" filter
    return "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query, "sp": "EgIIAg=="})


def _a(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'


def render(events: list[Event], day: dt.date, failed: int = 0) -> list[str]:
    """Telegram HTML messages, split between events so no link is ever cut in half."""
    weekday = "一二三四五六日"[day.weekday()]
    head = f"🌍 <b>今日世界十大事件</b>　{day.month}月{day.day}日 周{weekday}\n"
    blocks = []
    for n, e in enumerate(events, 1):
        links = []
        if e.source is not None:
            links.append("📰 " + _a(e.source.link, e.source.source))
        if e.video is not None:
            links.append("▶️ " + _a(e.video.link, f"YouTube · {e.video.source}"))
        else:
            links.append("▶️ " + _a(youtube_search(e.x_query), "YouTube 今日视频"))
        links.append("𝕏 " + _a(x_search(e.x_query), "X 上的讨论"))
        body = f"\n{n}. <b>{html.escape(e.title)}</b>\n"
        if e.summary:
            body += html.escape(e.summary) + "\n"
        blocks.append(body + "　".join(links) + "\n")
    tail = "\n<i>新闻来自 BBC、半岛电视台、卫报、纽约时报、NPR、DW、France 24 等，AI 整理成中文。</i>"
    if failed:
        tail += f"\n<i>（{failed} 个来源今天没打开）</i>"
    messages, current = [], head
    for block in blocks:
        if len(current) + len(block) > CHUNK:
            messages.append(current.rstrip())
            current = ""
        current += block
    if len(current) + len(tail) > CHUNK:
        messages.append(current.rstrip())
        current = ""
    messages.append((current + tail).strip())
    return messages


async def build_digest(attempts: list[tuple], now: dt.datetime, session: ClientSession | None = None,
                       feeds: list[tuple[str, str]] | None = None,
                       channels: list[tuple[str, str]] | None = None, day: dt.date | None = None) -> list[str]:
    """The day's messages, ready to send. Raises when there is nothing to send."""
    feeds = FEEDS if feeds is None else feeds
    channels = VIDEO_CHANNELS if channels is None else channels
    video_feeds = [(name, YT_FEED.format(cid)) for name, cid in channels]
    (news_lists, news_failed), (video_lists, video_failed) = await asyncio.gather(
        fetch_all(feeds, session), fetch_all(video_feeds, session))
    if news_failed or video_failed:
        logger.warning("新闻：这些来源没打开：%s", "、".join(news_failed + video_failed))
    headlines = recent(news_lists, now)
    videos = recent(video_lists, now, hours=WINDOW_HOURS, per_feed=15, limit=MAX_VIDEOS)
    if len(headlines) < EVENTS:
        raise RuntimeError(f"只拿到 {len(headlines)} 条新闻，来源可能都打不开：{'、'.join(news_failed) or '（无）'}")
    logger.info("新闻：%s 条标题、%s 条视频，交给 AI 挑选", len(headlines), len(videos))
    events = await choose(attempts, build_prompt(headlines, videos), lambda text: parse_events(text, headlines, videos))
    return render(events, day or now.date(), failed=len(news_failed))


# --------------------------------------------------------------------------- #
# Once a day
# --------------------------------------------------------------------------- #

class State:
    """The last day a digest went out, so a restart doesn't send it twice (or skip it)."""

    def __init__(self, path: Path):
        self.path = path

    def last(self) -> str:
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("last_sent", "")
        except (OSError, ValueError):
            return ""

    def mark(self, day: dt.date) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"last_sent": day.isoformat()}), encoding="utf-8")
        except OSError:
            logger.exception("新闻：记不住今天已经发过（%s）", self.path)


def due(now_local: dt.datetime, at: dt.time, last_sent: str) -> bool:
    """Past today's sending time, and today's not sent yet."""
    return now_local.time().replace(tzinfo=None) >= at.replace(tzinfo=None) and last_sent != now_local.date().isoformat()
