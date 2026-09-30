"""Cold emails to small businesses, sent from the owner's own Gmail after one tap in Telegram.

Railway blocks outbound SMTP on the Hobby plan, so mail goes through a tiny Google Apps
Script web app the owner deploys once in their own Google account (see MAILER_SCRIPT).
It sends with GmailApp, so every email sits in the owner's Sent folder and replies land in
their inbox, and it lets the bot look for replies to tell the owner about them.

Every morning the bot offers the day's batch (at most OUTREACH_DAILY_LIMIT) and sends
nothing until the owner taps ✅, or with OUTREACH_AUTO=true sends it straight away and
reports. Each business is emailed once, never followed up.

    OUTREACH_LEADS          JSON list: [{"email", "name", "region": "sg"|"hk", "lang": "en"|"zh", "first"}]
    OUTREACH_DAILY_LIMIT    default 10
    OUTREACH_TIME           default 10:00, in OUTREACH_TIMEZONE (default Asia/Singapore)
    MAIL_WEBHOOK_URL        optional; normally the owner just sends the /exec link to the bot
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout

DAILY_LIMIT = int(os.environ.get("OUTREACH_DAILY_LIMIT", "10"))
SEND_TIME = os.environ.get("OUTREACH_TIME", "10:00")
TIMEZONE = os.environ.get("OUTREACH_TIMEZONE", "Asia/Singapore")
GAP_SECONDS = float(os.environ.get("OUTREACH_GAP_SECONDS", "45"))
# true: the day's batch goes out on its own, and the owner is told afterwards
AUTO = os.environ.get("OUTREACH_AUTO", "false").lower() == "true"
SENDER_NAME = os.environ.get("OUTREACH_SENDER_NAME", "Vincent")
SITE_LINK = os.environ.get("OUTREACH_LINK", "https://mingchengli465-coder.github.io/Claude-agent/?from=email")
TELEGRAM_LINK = os.environ.get("OUTREACH_TELEGRAM", "https://t.me/Vinceeeeentttt")
REPLY_EMAIL = os.environ.get("OUTREACH_EMAIL", "mingchengli465@gmail.com")

SCRIPT_URL = re.compile(r"https://script\.google\.com/macros/s/[\w-]+/exec")
OPT_OUT = re.compile(r"no thanks|not interested|unsubscribe|remove me|stop email|不用了|不需要|唔使|唔需要", re.I)

EN_SUBJECT = "Quick idea for {name}'s customer enquiries"
EN_BODY = """Hi {name} team,

{first}

I'm Vincent, a Singaporean student who builds AI customer assistants for small businesses. It sits on your website (or Telegram), answers questions like prices, availability and how to order 24/7 using your own price list and FAQs, and alerts you straight away when a customer is ready to buy, so you can close the sale yourself.

You can try the one on my own site: {link}

If you send me your price list or FAQ, I'm happy to set up a free demo on it so you can see how it answers your customers before deciding anything. {price}

Best,
Vincent
Telegram: {telegram}
{email}

P.S. If this isn't useful for you, just reply "no thanks" and I won't email again."""
EN_PRICE = {"sg": "After that it's a one-off setup from US$70, then US$9.9/month.",
            "hk": "After that it's a one-off setup from US$70 (about HK$550), then US$9.9 (about HK$78) a month."}
EN_FIRST = ("Many customers probably message you after hours to ask about prices, availability and bookings, "
            "and by morning some of them have gone elsewhere.")

ZH_SUBJECT = "{name}的客人查詢，可以交給 AI 24 小時回覆"
ZH_BODY = """{name}你好，

{first}

我是 Vincent，一名新加坡大學生，專門幫小店做 AI 客服。它可以放在你的網站或 Telegram 上，按你自己的價目表和常見問題 24 小時自動回覆，繁體、英文都可以；客人準備下單時會第一時間通知你，由你親自成交。

可以先到我的網站試試看：{link}

如果你願意把價目表或常見問題發給我，我可以免費幫你做一個示範版，你先試用，滿意再決定。正式搭建一次性 US$70（約 HK$550）起，之後每月 US$9.9（約 HK$78）。

Vincent
Telegram：{telegram}
{email}

P.S. 如果暫時不需要，回覆「不用了」就可以，我不會再打擾你。"""
ZH_FIRST = "很多客人會在晚上發訊息問價錢、款式和預約時間，第二天才回覆的話，有些客人可能已經找了別家。"

# The owner pastes this into script.google.com once. {secret} is filled in per bot.
MAILER_SCRIPT = """const SECRET = '{secret}';

function doPost(e) {
  const req = JSON.parse(e.postData.contents);
  if (req.secret !== SECRET) return out({ok: false, error: 'bad secret'});
  if (req.action === 'ping') return out({ok: true, email: Session.getEffectiveUser().getEmail()});
  if (req.action === 'send') {
    GmailApp.sendEmail(req.to, req.subject, req.body, {name: req.name || 'Vincent'});
    return out({ok: true});
  }
  if (req.action === 'replies') {
    const emails = req.emails.map(function (x) { return x.toLowerCase(); });
    const query = '(' + emails.map(function (x) { return 'from:' + x; }).join(' OR ')
      + ' OR from:mailer-daemon) newer_than:' + (req.days || 3) + 'd';
    const found = [];
    GmailApp.search(query, 0, 30).forEach(function (t) {
      t.getMessages().forEach(function (m) {
        const from = m.getFrom().toLowerCase();
        const text = m.getPlainBody();
        const who = emails.filter(function (x) {
          return from.indexOf(x) >= 0 || (from.indexOf('mailer-daemon') >= 0 && text.toLowerCase().indexOf(x) >= 0);
        })[0];
        if (who) found.push({id: m.getId(), lead: who, bounce: from.indexOf('mailer-daemon') >= 0,
                             subject: m.getSubject(), text: text.slice(0, 1500)});
      });
    });
    return out({ok: true, replies: found});
  }
  return out({ok: false, error: 'unknown action'});
}

function out(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}
"""


def script_for(secret: str) -> str:
    return MAILER_SCRIPT.replace("{secret}", secret)


def compose(lead: dict) -> tuple[str, str]:
    """Subject and body for one business, in its language."""
    name = lead["name"]
    fill = {"name": name, "link": SITE_LINK, "telegram": TELEGRAM_LINK, "email": REPLY_EMAIL}
    if lead.get("lang") == "zh":
        return ZH_SUBJECT.format(**fill), ZH_BODY.format(first=lead.get("first") or ZH_FIRST, **fill)
    price = EN_PRICE.get(lead.get("region", "sg"), EN_PRICE["sg"])
    return EN_SUBJECT.format(**fill), EN_BODY.format(first=lead.get("first") or EN_FIRST, price=price, **fill)


def parse_leads(raw: str) -> list[dict]:
    """OUTREACH_LEADS, keeping only entries that have an email and a name."""
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict) and "@" in str(i.get("email", "")) and i.get("name")]


class MailError(RuntimeError):
    pass


class Leads:
    """The businesses, whether each was emailed, and the mailer's settings."""

    def __init__(self, db_path: Path | str, tz: str = TIMEZONE):
        self.path = str(db_path)
        self.tz = ZoneInfo(tz)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS leads (
                    email TEXT PRIMARY KEY, name TEXT NOT NULL, region TEXT, lang TEXT, first TEXT,
                    status TEXT NOT NULL DEFAULT 'new', sent_at TEXT, note TEXT);
                CREATE TABLE IF NOT EXISTS lead_replies (id TEXT PRIMARY KEY, email TEXT, seen TEXT);
                CREATE TABLE IF NOT EXISTS outreach_settings (key TEXT PRIMARY KEY, value TEXT);
            """)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    # --- settings --------------------------------------------------------------------------
    def setting(self, key: str) -> str:
        with self._db() as db:
            row = db.execute("SELECT value FROM outreach_settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else ""

    def set_setting(self, key: str, value: str) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO outreach_settings VALUES (?, ?)", (key, value))

    def secret(self) -> str:
        value = self.setting("secret")
        if not value:
            value = secrets.token_urlsafe(24)
            self.set_setting("secret", value)
        return value

    # --- leads -----------------------------------------------------------------------------
    def add(self, leads: list[dict]) -> int:
        """New businesses are added; ones already known keep their status."""
        with self._db() as db:
            before = db.total_changes
            db.executemany(
                "INSERT OR IGNORE INTO leads (email, name, region, lang, first) VALUES (?, ?, ?, ?, ?)",
                [(l["email"].strip().lower(), l["name"], l.get("region", "sg"), l.get("lang", "en"), l.get("first", ""))
                 for l in leads])
            return db.total_changes - before

    def get(self, email: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM leads WHERE email=?", (email.lower(),)).fetchone()
        return dict(row) if row else None

    def mark(self, email: str, status: str, note: str = "") -> None:
        sent_at = dt.datetime.now(dt.timezone.utc).isoformat() if status == "sent" else None
        with self._db() as db:
            db.execute("UPDATE leads SET status=?, note=?, sent_at=COALESCE(?, sent_at) WHERE email=?",
                       (status, note, sent_at, email.lower()))

    def sent_today(self, now: dt.datetime | None = None) -> int:
        now = now or dt.datetime.now(dt.timezone.utc)
        start = dt.datetime.combine(now.astimezone(self.tz).date(), dt.time(), self.tz).astimezone(dt.timezone.utc)
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM leads WHERE sent_at >= ?", (start.isoformat(),)).fetchone()[0]

    def room_today(self, now: dt.datetime | None = None) -> int:
        return max(0, DAILY_LIMIT - self.sent_today(now))

    def due(self, now: dt.datetime | None = None) -> list[dict]:
        """Today's batch: the next businesses not yet emailed, up to what's left of the daily limit."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM leads WHERE status='new' ORDER BY rowid LIMIT ?",
                              (self.room_today(now),)).fetchall()
        return [dict(r) for r in rows]

    def emailed(self) -> list[str]:
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT email FROM leads WHERE sent_at IS NOT NULL")]

    def counts(self) -> dict[str, int]:
        with self._db() as db:
            return {r[0]: r[1] for r in db.execute("SELECT status, COUNT(*) FROM leads GROUP BY status")}

    def first_sight(self, reply_id: str, email: str) -> bool:
        """True the first time a reply is seen, so the owner hears about it once."""
        with self._db() as db:
            before = db.total_changes
            db.execute("INSERT OR IGNORE INTO lead_replies VALUES (?, ?, ?)",
                       (reply_id, email, dt.datetime.now(dt.timezone.utc).isoformat()))
            return db.total_changes > before


class Mailer:
    """The owner's Apps Script web app."""

    def __init__(self, url: str, secret: str, session: ClientSession | None = None):
        self.url, self.secret, self._session = url, secret, session

    async def _call(self, action: str, **body) -> dict:
        own = self._session is None
        session = self._session or ClientSession(timeout=ClientTimeout(total=60))
        try:
            # Apps Script answers a POST with a redirect to the result; aiohttp follows it with a GET.
            async with session.post(self.url, json={"secret": self.secret, "action": action, **body}) as r:
                text = await r.text()
        except Exception as exc:  # noqa: BLE001 - network trouble becomes a readable error
            raise MailError(f"连不上 Gmail 发信脚本：{exc}") from exc
        finally:
            if own:
                await session.close()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise MailError("Gmail 发信脚本没有正常回应（部署时“谁可以访问”要选“任何人”）") from None
        if not data.get("ok"):
            raise MailError(f"Gmail 发信脚本出错：{data.get('error') or data}")
        return data

    async def ping(self) -> str:
        return (await self._call("ping")).get("email", "")

    async def send(self, to: str, subject: str, body: str) -> None:
        await self._call("send", to=to, subject=subject, body=body, name=SENDER_NAME)

    async def replies(self, emails: list[str], days: int = 3) -> list[dict]:
        if not emails:
            return []
        return (await self._call("replies", emails=emails, days=days)).get("replies", [])
