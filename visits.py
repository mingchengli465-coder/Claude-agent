"""Who looked at the website: page views, where people came from, who chatted.

The owner's pages load widget.js with a data-track attribute; the widget then
posts one hit per page view to /api/hit (web.py). Sites that merely embed the
chat window don't carry the attribute, so a merchant's traffic is never counted.

Visitors are the widget's random browser id, so the same person on the same
browser counts once a day. The owner marks their own devices with a secret
link (?me=<token>), and those devices, bots and link-preview scanners are left
out of every number.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
import sqlite3
import time
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

KEEP_DAYS = 120
_BOT_UA = re.compile(
    r"bot|spider|crawl|slurp|headless|lighthouse|preview|wetest|facebookexternalhit|embedly|"
    r"python|curl|wget|go-http|java/|okhttp|axios|node-fetch|scrapy|httpclient",
    re.IGNORECASE)
_SOURCE_PARAM = re.compile(r"^[a-z0-9_-]{1,20}$")

# ?from= values the owner can put on a link, and what the report calls them.
FROM_NAMES = {"x": "X", "twitter": "X", "tg": "Telegram", "telegram": "Telegram", "wx": "微信",
              "wechat": "微信", "weixin": "微信", "xhs": "小红书", "xiaohongshu": "小红书",
              "fb": "Facebook", "facebook": "Facebook"}


def is_bot(ua: str) -> bool:
    return not ua or bool(_BOT_UA.search(ua))


def classify(referrer: str, ua: str, from_param: str = "") -> str:
    """Where a visitor came from, in the owner's words."""
    tag = from_param.lower()
    if tag in FROM_NAMES:
        return FROM_NAMES[tag]
    ref, low = referrer.lower(), ua.lower()
    if "micromessenger" in low or "weixin" in ref or "wx.qq.com" in ref:
        return "微信"
    if "xhsdiscover" in low or "xiaohongshu" in ref or "xhslink" in ref:
        return "小红书"
    if "twitter" in low or re.search(r"//(www\.|mobile\.)?(x\.com|twitter\.com|t\.co)(/|$)", ref):
        return "X"
    if "telegram" in low or re.search(r"//(t\.me|web\.telegram\.org)(/|$)", ref):
        return "Telegram"
    if "fban" in low or "fbav" in low or "facebook." in ref:
        return "Facebook"
    if re.search(r"//([a-z.]*\.)?(google|bing|baidu|sogou|so|yandex|duckduckgo)\.", ref):
        return "搜索引擎"
    if "qq" in low and "qq.com" in ref:
        return "QQ"
    if ref and "github.io" not in ref and "railway.app" not in ref:
        host = re.sub(r"^https?://(www\.)?", "", ref).split("/")[0]
        return host[:40] or "直接打开"
    return "直接打开"


def page_name(path: str) -> str:
    return "AI 客服演示页" if "demo" in path.lower() else "首页"


class Visits:
    def __init__(self, path: Path | str, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS visits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                visitor TEXT NOT NULL,
                page TEXT NOT NULL,
                source TEXT NOT NULL,
                lang TEXT DEFAULT '',
                host TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS visits_by_ts ON visits (ts);
            CREATE TABLE IF NOT EXISTS owner_devices (visitor TEXT PRIMARY KEY, marked_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS visit_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        self.db.commit()

    # ---- the owner's secret link -------------------------------------------------

    def _setting(self, key: str) -> str:
        row = self.db.execute("SELECT value FROM visit_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else ""

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO visit_settings (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def owner_token(self) -> str:
        token = self._setting("owner_token")
        if not token:
            token = secrets.token_urlsafe(12)
            self._set("owner_token", token)
        return token

    def mark_owner(self, visitor: str, token: str) -> bool:
        if not token or not secrets.compare_digest(token, self.owner_token()):
            return False
        self.db.execute("INSERT OR REPLACE INTO owner_devices (visitor, marked_at) VALUES (?, ?)",
                        (visitor, self.clock()))
        self.db.commit()
        return True

    def owner_devices(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM owner_devices").fetchone()[0]

    def take_flag(self, key: str) -> bool:
        """True the first time only (e.g. to send the owner their links once)."""
        if self._setting(key):
            return False
        self._set(key, "1")
        return True

    # ---- recording ---------------------------------------------------------------

    def record(self, visitor: str, path: str, referrer: str, ua: str, from_param: str = "",
               lang: str = "", host: str = "") -> bool:
        if is_bot(ua):
            return False
        from_param = from_param.lower() if _SOURCE_PARAM.match(from_param.lower()) else ""
        now = self.clock()
        self.db.execute(
            "INSERT INTO visits (ts, visitor, page, source, lang, host) VALUES (?, ?, ?, ?, ?, ?)",
            (now, visitor, page_name(path), classify(referrer, ua, from_param), lang[:16], host[:80]))
        self.db.execute("DELETE FROM visits WHERE ts < ?", (now - KEEP_DAYS * 86400,))
        self.db.commit()
        return True

    # ---- reporting ---------------------------------------------------------------

    def stats(self, start: float, end: float) -> dict:
        owners = "SELECT visitor FROM owner_devices"
        rows = self.db.execute(
            f"SELECT visitor, page, source, lang FROM visits WHERE ts >= ? AND ts < ?"
            f" AND visitor NOT IN ({owners}) ORDER BY id", (start, end)).fetchall()
        first_source: dict[str, str] = {}
        langs: dict[str, str] = {}
        for r in rows:
            first_source.setdefault(r["visitor"], r["source"])
            langs.setdefault(r["visitor"], r["lang"])
        chats, chat_messages = 0, 0
        try:
            got = self.db.execute(
                f"SELECT chat_id, COUNT(*) AS n FROM messages WHERE channel = 'web' AND role = 'customer'"
                f" AND ts >= ? AND ts < ? AND chat_id NOT IN ({owners}) GROUP BY chat_id", (start, end)).fetchall()
            chats, chat_messages = len(got), sum(r["n"] for r in got)
        except sqlite3.OperationalError:  # no customer-service tables in this database
            pass
        return {
            "people": len(first_source),
            "views": len(rows),
            "sources": Counter(first_source.values()),
            "pages": Counter(r["page"] for r in rows),
            "foreign": sum(1 for lang in langs.values() if lang and not lang.lower().startswith("zh")),
            "chats": chats,
            "chat_messages": chat_messages,
        }

    def day_bounds(self, day: dt.date, tz: ZoneInfo) -> tuple[float, float]:
        start = dt.datetime.combine(day, dt.time(), tzinfo=tz)
        return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()

    def report(self, tz: ZoneInfo, day: dt.date | None = None, today_too: bool = False) -> str:
        """The owner's summary for one day (yesterday by default) and the last 7 days."""
        today = dt.datetime.fromtimestamp(self.clock(), tz).date()
        day = day or today - dt.timedelta(days=1)
        s = self.stats(*self.day_bounds(day, tz))
        lines = [f"📊 {day.month}月{day.day}日 网站访客", _describe(s)]
        if today_too:
            t = self.stats(self.day_bounds(today, tz)[0], self.clock())
            lines += ["", f"今天到现在：{t['people']} 人，打开 {t['views']} 次" + (
                f"，{t['chats']} 人跟 AI 客服聊了" if t["chats"] else "")]
        week_start = self.day_bounds(day - dt.timedelta(days=6), tz)[0]
        w = self.stats(week_start, self.day_bounds(day, tz)[1])
        lines += ["", f"最近 7 天：{w['people']} 人，打开 {w['views']} 次" + (
            f"，{w['chats']} 人跟 AI 客服聊了" if w["chats"] else "")]
        if w["sources"]:
            lines.append("7 天来源：" + _counts(w["sources"]))
        lines += ["", "（你自己标记过的设备、搜索引擎机器人、微信/Telegram 自动检查链接都已去掉）"]
        return "\n".join(lines)


def _counts(counter: Counter) -> str:
    return " · ".join(f"{k} {v}" for k, v in counter.most_common())


def _describe(s: dict) -> str:
    if not s["people"]:
        return "没有人来看网站。"
    out = [f"来了 {s['people']} 个人，一共打开页面 {s['views']} 次"]
    out.append("从哪来：" + _counts(s["sources"]))
    out.append("看了哪页：" + _counts(s["pages"]))
    if s["foreign"]:
        out.append(f"其中 {s['foreign']} 个人的手机/电脑不是中文（可能是海外客人）")
    out.append(f"跟 AI 客服聊天：{s['chats']} 人，发了 {s['chat_messages']} 条消息" if s["chats"]
               else "没人跟 AI 客服聊天")
    return "\n".join(out)
