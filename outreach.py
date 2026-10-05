"""Cold emails to small businesses, sent from the owner's own Gmail after one tap in Telegram.

Railway blocks outbound SMTP on the Hobby plan, so mail goes through a tiny Google Apps
Script web app the owner deploys once in their own Google account (see MAILER_SCRIPT).
It sends with GmailApp, so every email sits in the owner's Sent folder and replies land in
their inbox, and it lets the bot look for replies to tell the owner about them.

Every morning the bot offers the day's batch (at most OUTREACH_DAILY_LIMIT) and sends
nothing until the owner taps ✅, or with OUTREACH_AUTO=true sends it straight away and
reports. Each business is emailed once, never followed up.

    OUTREACH_LEADS          JSON list: [{"email", "name", "region": "sg"|"hk"|"uk"…, "lang": "en"|"zh", "first"}]
                            Leads with a "batch" go out together as soon as the mailer can carry images,
                            outside the daily limit, each with its own "mockup" (web_static/mockups/<slug>.png).
    OUTREACH_DAILY_LIMIT    default 10
    OUTREACH_MIX            e.g. hk:6,sg:4 — each region's share of the day; any room left goes to the rest
    OUTREACH_TIME           default 10:00, in OUTREACH_TIMEZONE (default Asia/Singapore)
    MAIL_WEBHOOK_URL        optional; normally the owner just sends the /exec link to the bot
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import ClientSession, ClientTimeout
from yarl import URL

DAILY_LIMIT = int(os.environ.get("OUTREACH_DAILY_LIMIT", "10"))


def parse_mix(raw: str) -> dict[str, int]:
    """OUTREACH_MIX like "hk:6,sg:4": how many a day from each region, in that order."""
    mix = {}
    for part in raw.split(","):
        region, _, n = part.partition(":")
        if region.strip() and n.strip().isdigit():
            mix[region.strip().lower()] = int(n)
    return mix


MIX = parse_mix(os.environ.get("OUTREACH_MIX", ""))
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
# Outside HK/SG/MY the email is English only, sent in the recipient's working hours.
REGION_TZ = {"uk": "Europe/London", "ie": "Europe/Dublin", "au": "Australia/Sydney", "nz": "Pacific/Auckland",
             "ca": "America/Toronto", "us": "America/New_York"}
WORK_HOURS = (dt.time(8, 30), dt.time(18, 0))
BATCH_GAP_SECONDS = float(os.environ.get("OUTREACH_BATCH_GAP_SECONDS", "90"))
MOCKUP_DIR = Path(__file__).with_name("web_static") / "mockups"
# the plain-text letter links its mockup here (web.py serves both the committed ones and the drawn ones)
MOCKUP_URL = os.environ.get("OUTREACH_MOCKUP_URL", "https://worker-production-42fb.up.railway.app/mockups/{slug}.png")
# English-speaking countries searched and emailed every working day: how many a day each
INTL_MIX = parse_mix(os.environ.get("OUTREACH_INTL_MIX", "uk:5,au:4,ie:2,nz:2"))
INTL_HOURS = (dt.time(9, 30), dt.time(16, 0))
# Out-of-office and other automatic answers: told to the owner, never answered.
AUTO_ANSWER = re.compile(r"out of (the )?office|automatic reply|auto-?reply|autoreply|away from (the )?office|"
                         r"on (annual )?leave|currently away|delivery status notification|自动回复|自動回覆|休假中", re.I)
# The drafted answer to a business goes out by itself this many minutes later (0: only with ✅),
# for at most this many answers to one business; after that the owner takes over.
REPLY_AUTO_MINUTES = int(os.environ.get("OUTREACH_REPLY_AUTO_MINUTES", "10"))
REPLY_AUTO_MAX = int(os.environ.get("OUTREACH_REPLY_AUTO_MAX", "4"))
EMAIL_ADDRESS = re.compile(r"^[\w.+'-]+@[\w-]+(\.[\w-]+)+$")
OPT_OUT = re.compile(r"no thanks|not interested|unsubscribe|remove me|stop email|不用了|不需要|唔使|唔需要", re.I)

EN_SUBJECT = "Quick idea for {name}'s customer enquiries"
EN_BODY = """Hi {name} team,

{first}

I'm Vincent, a Singaporean student who builds AI customer assistants for small businesses. It sits on your website (or Telegram), answers questions like prices, availability and how to order 24/7 using your own price list and FAQs, and alerts you straight away when a customer is ready to buy, so you can close the sale yourself.

Here's a 38-second video of it at work: {video}
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

38 秒短片，看它怎麼接客：{video}
可以先到我的網站試試看：{link}

如果你願意把價目表或常見問題發給我，我可以免費幫你做一個示範版，你先試用，滿意再決定。{price}

Vincent
Telegram：{telegram}
{email}

P.S. 如果暫時不需要，回覆「不用了」就可以，我不會再打擾你。"""
ZH_PRICE = {"sg": "正式搭建一次性 US$70 起，之後每月 US$9.9。",
            "hk": "正式搭建一次性 US$70（約 HK$550）起，之後每月 US$9.9（約 HK$78）。"}
# Web design studios get a partnership offer instead: they refer clients, I build, they earn 30%.
DEMO_LINK = os.environ.get("OUTREACH_DEMO_LINK", "https://mingchengli465-coder.github.io/Claude-agent/demo.html?from=email")
# The industry demo matching the business (demos.py): a pretend shop of the same kind to chat with
DEMO_PAGE = os.environ.get("OUTREACH_DEMO_PAGE", "https://mingchengli465-coder.github.io/Claude-agent/demo-{kind}.html?from=email")
# A working demo in the business's own name (trials.py), sample prices until they give theirs.
# bot.py makes it and puts its link on the lead as "personal"; the letters then point there.
TRIAL_PAGE = os.environ.get("OUTREACH_TRIAL_PAGE", "https://mingchengli465-coder.github.io/Claude-agent/t.html?s={slug}&from=email")
PERSONAL_EN = "I also set up a working demo in {name}'s name, with sample prices for now, that you can chat with: {url}"
PERSONAL_ZH = "我用「{name}」的名字先做了一個能直接聊天的示範版（價格先用示範資料）：{url}"
PERSONAL_EN_ASIA = "I've already set up a working demo in {name}'s name (with sample prices for now) that you can chat with: {url}"
# …and when the demo has read the business's own website (sitetext.py)
PERSONAL_READ_EN = "I also set up a working demo for {name} that has already read your website, so you can ask it what your customers ask: {url}"
PERSONAL_READ_ZH = "我已經讓 AI 先讀了「{name}」的網站，做好一個能直接聊天的示範版，你可以像客人一樣問問它：{url}"
PERSONAL_READ_EN_ASIA = "I've already set up a working demo for {name} that has read your website, so you can ask it what your customers ask: {url}"


# English-speaking countries get the English-only pages; Singapore, Hong Kong and Malaysia the 繁體 + English ones.
DEMO_PAGE_EN = os.environ.get("OUTREACH_DEMO_PAGE_EN", "https://mingchengli465-coder.github.io/Claude-agent/demo-{kind}-en.html?from=email")
TRIAL_PAGE_EN = os.environ.get("OUTREACH_TRIAL_PAGE_EN", "https://mingchengli465-coder.github.io/Claude-agent/t-en.html?s={slug}&from=email")
VIDEO_LINK_EN = os.environ.get("OUTREACH_VIDEO_LINK_EN", "https://mingchengli465-coder.github.io/Claude-agent/video-en.html?from=email")


def english_pages(lead: dict) -> bool:
    return is_intl(lead)


def demo_link(lead: dict) -> str:
    from mockup import kind_of
    kind = kind_of(lead)
    if not kind or lead.get("kind") == "agency":
        return ""
    return (DEMO_PAGE_EN if english_pages(lead) else DEMO_PAGE).format(kind=kind)


def trial_link(slug: str, lead: dict | None = None, english: bool = False) -> str:
    """The page of a business's own demo (trials.py): English-only for the English-speaking countries."""
    return (TRIAL_PAGE_EN if english or (lead and english_pages(lead)) else TRIAL_PAGE).format(slug=slug)


def site_link(lead: dict) -> str:
    return SITE_LINK + ("&lang=en" if english_pages(lead) else "")


def general_demo_link(lead: dict) -> str:
    return DEMO_LINK + ("&lang=en" if english_pages(lead) else "")


def video_link(lead: dict) -> str:
    return VIDEO_LINK_EN if english_pages(lead) else VIDEO_LINK


# A 38-second video page, linked rather than attached: attachments from a new sender land in spam.
VIDEO_LINK = os.environ.get("OUTREACH_VIDEO_LINK", "https://mingchengli465-coder.github.io/Claude-agent/video.html?from=email")
AGENCY_ZH_SUBJECT = "合作提案：讓 {name} 的網站客戶多一個 AI 客服"
AGENCY_ZH_BODY = """{name}你好，

{first}

我是 Vincent，在新加坡幫小店做 AI 客服：放在網站右下角或 Telegram 上，按店家自己的價目表 24 小時回覆客人，客人準備下單時第一時間通知老闆。

想問你們有沒有興趣合作：你們幫客戶做網站時，順便推薦 AI 客服，架設和維護都由我負責。每成交一家，你們拿架設費的 30%。客戶還是你們的，我不會另外接觸；也可以用你們的名義交付。

38 秒短片介紹：{video}
做好的示範可以看這裡：{demo}

有興趣的話直接回覆這封郵件，我可以先免費幫你們的一個客戶做試用版。

Vincent
Telegram：{telegram}
{email}

P.S. 如果暫時不需要，回覆「不用了」就可以，我不會再打擾你們。"""
AGENCY_ZH_FIRST = "你們幫小店做網站，網站上線後，店家最常遇到的問題之一，就是晚上和週末的客人訊息沒人回。"
AGENCY_EN_SUBJECT = "Partnership idea: an AI assistant for your clients' websites"
AGENCY_EN_BODY = """Hi {name} team,

{first}

I'm Vincent, based in Singapore. I build AI customer assistants for small shops: a chat window on their website (or Telegram) that answers customers 24/7 from the shop's own price list and alerts the owner the moment someone is ready to buy.

Would you be open to a simple partnership? When you build a site, you offer the AI assistant as an add-on, and I handle the setup and maintenance. For every client who signs up, you keep 30% of the setup fee. The client stays yours, I won't contact them separately, and I can deliver under your name if you prefer.

A 38-second video: {video}
Here's a finished demo: {demo}

If it sounds interesting, just reply to this email and I'll set up a free trial for one of your clients first.

Best,
Vincent
Telegram: {telegram}
{email}

P.S. If this isn't for you, just reply "no thanks" and I won't email again."""
AGENCY_EN_FIRST = ("You build websites for small businesses, and one of the most common problems those shops have after launch "
                   "is customer messages at night and on weekends that nobody answers.")

# Every email carries both languages: 繁體中文 first, English underneath.
BILINGUAL_RULE = "\n\n———— English below ————\n\n"
ZH_FIRST = "很多客人會在晚上發訊息問價錢、款式和預約時間，第二天才回覆的話，有些客人可能已經找了別家。"

# The owner pastes this into script.google.com once. {secret} is filled in per bot.
# version 2 sends HTML with inline images (the mockups); version 1 sent plain text only
MAILER_VERSION = 2
MAILER_SCRIPT = """const SECRET = '{secret}';

function doPost(e) {
  const req = JSON.parse(e.postData.contents);
  if (req.secret !== SECRET) return out({ok: false, error: 'bad secret'});
  if (req.action === 'ping') return out({ok: true, version: 2, email: Session.getEffectiveUser().getEmail()});
  if (req.action === 'send') {
    const options = {name: req.name || 'Vincent'};
    if (req.html) {
      options.htmlBody = req.html;
      options.inlineImages = {};
      Object.keys(req.images || {}).forEach(function (k) {
        options.inlineImages[k] = Utilities.newBlob(Utilities.base64Decode(req.images[k]), 'image/png', k + '.png');
      });
    }
    GmailApp.sendEmail(req.to, req.subject, req.body, options);
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


INTL_SUBJECT = "A quick mockup for {name}"
INTL_BODY = """{greeting}

{first}

{problem} So I put together a quick mockup of {name}'s website with a 24/7 assistant on it:

{mockup}

It answers from your own information ({facts}), takes down every enquiry, and sends it straight to you to confirm. {demo_line}

I'm Vincent, a student developer in Singapore, and I set these up for small businesses. I'd be happy to build you a free trial version first, so you can see how it handles real questions. If you decide to keep it, it's a one-off {price}, cancel anytime.

Here's a 38-second video of how it works: {video}

Best,
Vincent
{link}

P.S. If this isn't for you, just reply "no thanks" and I won't email again."""
INTL_PRICE = {"uk": "US$70 (about £55), then US$9.9 (about £8) a month", "ie": "US$70 (about €65), then US$9.9 (about €9) a month",
              "au": "US$70 (about A$110), then US$9.9 (about A$15) a month",
              "nz": "US$70 (about NZ$120), then US$9.9 (about NZ$17) a month"}
INTL_PROBLEM = {
    "bnb": "Guests often message in the evening asking about rooms, dates and arrival times, and by morning some have booked elsewhere.",
    "florist": "A lot of flower orders and wedding enquiries arrive after the shop has closed, and by morning some customers have ordered elsewhere.",
    "tutor": "Parents often look for tuition in the evening, once the children are in bed, and by morning some have enquired elsewhere.",
    "bakery": "Cake orders often start late in the evening with questions about sizes, flavours and dates, and by morning some customers have ordered elsewhere.",
    "beauty": "Clients often message late in the evening to ask about treatments and book a time, and by morning some have booked elsewhere.",
    "groomer": "Owners often message in the evening about prices and free slots, and by morning some have booked elsewhere.",
}
INTL_KINDWORD = {"bnb": "B&B", "florist": "florist", "tutor": "tuition centre", "bakery": "cake shop",
                 "beauty": "beauty studio", "groomer": "dog groomer"}
INTL_FACTS = {"bnb": "rooms, prices, house rules", "florist": "flowers, prices, delivery areas",
              "tutor": "courses, fees, timetables", "bakery": "cakes, flavours, prices, lead times",
              "beauty": "treatments, prices, opening hours", "groomer": "services, prices, opening hours"}
MOCKUP_MARK = "{mockup}"


def is_intl(lead: dict) -> bool:
    return (lead.get("region") or "") in REGION_TZ


def mockup_slug(lead: dict) -> str:
    """The lead's mockup: one drawn for it before (web_static/mockups), else one drawn on demand."""
    from mockup import kind_of
    if lead.get("mockup"):
        return lead["mockup"]
    if lead.get("kind") == "agency":
        # a designer's mockup shows the client's site we found them through
        if not lead.get("client") or not kind_of(lead):
            return ""
        return "a-" + hashlib.sha1(lead["email"].lower().encode()).hexdigest()[:12]
    if not kind_of(lead):
        return ""
    return "m-" + hashlib.sha1(lead["email"].lower().encode()).hexdigest()[:12]


# Web designers found through a client's site ("Website by …", leadfinder.py): a partnership offer
# about that client, with a mockup of the client's site and a demo built from it.
INTL_AGENCY_SUBJECT = "{client}'s website + a 24/7 assistant (a partnership idea)"
INTL_AGENCY_BODY = """{greeting}

{first}

I'm Vincent, a student developer in Singapore. I set up AI assistants for small businesses like {client}: a chat window on their website that answers customers 24/7 from the business's own information and passes every booking or order straight to the owner. Here's a quick mockup of how it could look on {client}'s site:

{mockup}

{demo_line}

Would a simple partnership interest you? You offer it to your clients as an add-on, I do the setup and the upkeep, and you keep 30% of every setup fee (it's US$70 one-off, then US$9.9 a month for the client). The clients stay yours, I won't contact them separately, and I can deliver it under your name.

Here's a 38-second video of how it works: {video}

If you like, I'll set up a free trial for {client} or any other client first, so you can show it to them.

Best,
Vincent
{link}

P.S. If this isn't for you, just reply "no thanks" and I won't email again."""
AGENCY_DEMO_EN = "I also made a working demo for {client}, built from its website, that you could show them: {url}"
AGENCY_DEMO_SAMPLE_EN = "I also made a working demo in {client}'s name (with sample prices for now) that you could show them: {url}"
AGENCY_DEMO_ZH = "我也用{client}的網站做了一個能直接聊天的示範版，你們可以拿給客戶看：{url}"
AGENCY_DEMO_SAMPLE_ZH = "我也用{client}的名字做了一個能直接聊天的示範版（價格先用示範資料），你們可以拿給客戶看：{url}"


def _agency_demo(lead: dict, zh: bool = False) -> str:
    if zh:
        line = AGENCY_DEMO_ZH if lead.get("personal_read") else AGENCY_DEMO_SAMPLE_ZH
    else:
        line = AGENCY_DEMO_EN if lead.get("personal_read") else AGENCY_DEMO_SAMPLE_EN
    return line.format(client=lead["client"], url=lead["personal"])


def _intl_agency_parts(lead: dict) -> tuple[str, str]:
    host = (lead.get("host") or "").strip()
    personal = lead.get("personal")
    fill = {"client": lead["client"], "first": lead.get("first") or f"I came across {lead['client']}'s website and saw that you built it.",
            "greeting": f"Hi {host}," if host else f"Hi {lead['name']} team,", "mockup": MOCKUP_MARK,
            "video": video_link(lead).replace("from=email", "from=agency"), "link": site_link(lead).replace("from=email", "from=agency"),
            "demo_line": _agency_demo(lead) if personal else
            "You can chat with a working demo here: " + (demo_link({**lead, "kind": "shop"}) or general_demo_link(lead))}
    return INTL_AGENCY_SUBJECT.format(**fill), INTL_AGENCY_BODY.format(**fill)


def _intl_parts(lead: dict) -> tuple[str, str]:
    from mockup import kind_of
    if lead.get("kind") == "agency" and lead.get("client"):
        return _intl_agency_parts(lead)
    cat = kind_of(lead) or "bnb"
    host = (lead.get("host") or "").strip()
    fill = {"name": lead["name"], "first": lead.get("first") or f"I came across {lead['name']} online.",
            "greeting": f"Hi {host}," if host else "Hello,", "problem": INTL_PROBLEM.get(cat, INTL_PROBLEM["bnb"]),
            "facts": INTL_FACTS.get(cat, INTL_FACTS["bnb"]), "video": video_link(lead).replace("from=email", "from=uk"),
            "link": site_link(lead).replace("from=email", "from=uk"), "mockup": MOCKUP_MARK,
            "price": INTL_PRICE.get(lead.get("region") or "", "US$70, then US$9.9 a month"),
            "kindword": INTL_KINDWORD.get(cat, "small business")}
    personal = lead.get("personal")
    fill["demo_line"] = ((PERSONAL_READ_EN if lead.get("personal_read") else PERSONAL_EN).format(name=lead["name"], url=personal)
                         if personal else
                         f"You can chat with a demo one for a {fill['kindword']} here: "
                         + demo_link(lead).replace("from=email", "from=uk"))
    return INTL_SUBJECT.format(**fill), INTL_BODY.format(**fill)


def mockup_png(lead: dict, cache: Path | None = None) -> bytes | None:
    """The PNG: committed (web_static/mockups), cached in `cache`, or drawn now and cached there."""
    slug = mockup_slug(lead)
    if not re.fullmatch(r"[a-z0-9-]+", slug or ""):
        return None
    for folder in (MOCKUP_DIR, cache):
        if folder is not None and (folder / f"{slug}.png").is_file():
            return (folder / f"{slug}.png").read_bytes()
    if lead.get("mockup") or cache is None:
        return None  # a named mockup that isn't there is not redrawn
    import mockup
    png = mockup.render({**lead, "name": lead["client"], "kind": "shop", "host": ""} if lead.get("kind") == "agency" else lead)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"{slug}.png").write_bytes(png)
    return png


MOCKUP_INTRO = "我為 {name} 做了一張示意圖：AI 客服放在你們網站上的樣子 / A quick mockup of {name}'s website with the AI assistant on it:"
MOCKUP_INTRO_AGENCY = ("我用你們做的 {name} 網站畫了一張示意圖：加上 AI 客服的樣子 / "
                       "A quick mockup of {name}'s website (which you built) with the AI assistant on it:")


def compose_html(lead: dict) -> str:
    """The letter as HTML, with the mockup shown inline (cid:mockup): in place in the English letter,
    at the top of the 繁體 + English one."""
    if is_intl(lead):
        _, body = _intl_parts(lead)
    else:
        _, body = compose(lead)
        intro = MOCKUP_INTRO_AGENCY.format(name=lead["client"]) if lead.get("kind") == "agency" and lead.get("client") \
            else MOCKUP_INTRO.format(name=lead["name"])
        body = intro + "\n\n" + MOCKUP_MARK + "\n\n" + body
    return to_html(body, lead)


# --- the one follow-up, five days later, to businesses that haven't answered ----------------------
FOLLOWUP_DAYS = int(os.environ.get("OUTREACH_FOLLOWUP_DAYS", "5"))
# Apps Script lets a free Gmail account email 100 people a day; stay under it, follow-ups included
SEND_CAP_24H = int(os.environ.get("OUTREACH_SEND_CAP", "95"))
FOLLOWUP_EN = """{greeting}

Just bringing this back to the top of your inbox in case it got buried. Here's the mockup I made for {name} again:

{mockup}

If a 24/7 assistant that answers from your own information and passes every enquiry to you would help, I'm happy to set up a free trial on your site this week, with no obligation.{personal}

Best,
Vincent
{link}

P.S. If it's not for you, just reply "no thanks" and I won't email again."""
FOLLOWUP_ZH = """{name}你好，

上次的郵件可能被其他訊息蓋過了，再附上一次我為你們做的示意圖：

{mockup}

如果一個按你們自己的資料 24 小時回覆、並把每個查詢轉給你們的 AI 客服有幫助，我很樂意這星期先免費幫你們做一個試用版，不用任何承諾。{personal}

Vincent
Telegram：{telegram}

P.S. 如果暫時不需要，回覆「不用了」就可以，我不會再打擾你們。"""


def _followup_link(lead: dict) -> str:
    return lead["personal"].replace("from=email", "from=followup")


def compose_followup(lead: dict) -> tuple[str, str]:
    """"Re:" the first email's subject; the body carries MOCKUP_MARK where the mockup goes."""
    subject, _ = compose(lead)
    host = (lead.get("host") or "").strip()
    en = FOLLOWUP_EN.format(greeting=f"Hi {host}," if host else ("Hello," if is_intl(lead) else f"Hi {lead['name']} team,"),
                            name=lead["name"], mockup=MOCKUP_MARK,
                            link=site_link(lead).replace("from=email", "from=followup"),
                            personal=f"\n\nThe demo I made in your name is still here to try: {_followup_link(lead)}"
                            if lead.get("personal") else "")
    if is_intl(lead):
        return "Re: " + subject, en
    zh_name = lead["name"] + (" " if lead["name"][-1:].isascii() else "")
    zh = FOLLOWUP_ZH.replace("{name}", zh_name, 1).format(
        mockup=MOCKUP_MARK, telegram=TELEGRAM_LINK,
        personal=f"\n\n用你們店名做的示範版還在，隨時可以試：{_followup_link(lead)}" if lead.get("personal") else "")
    # one mockup is enough: the English half refers to it
    en = en.replace(MOCKUP_MARK, "(the mockup is above)")
    return "Re: " + subject, zh + BILINGUAL_RULE + en


# --- answering a business that replied: the AI drafts, the owner taps to send ---------------------
REPLY_SYSTEM = """You write email replies for Vincent, a student developer in Singapore who sets up 24/7 AI
customer-service assistants for small businesses: a chat window on their website (or Telegram/WhatsApp)
that answers customers from the business's own price list and FAQs, day and night, and passes every
order, booking or price negotiation straight to the owner.
Facts you may use, and nothing else:
- Setup takes 2–3 days. It costs a one-off US$70, then US$9.9 a month, cancel anytime.
- Vincent first builds a free trial version on the business's own information, so they can test it before paying.
- To build the trial he needs their price list or menu, opening hours and the questions customers ask most
  (a website link or a photo of the price list is enough).
- Web designers and agencies can partner: they offer it to their clients as an add-on, Vincent does the setup
  and upkeep, and they keep 30% of every setup fee. The clients stay theirs; he can deliver under their name.
- A demo assistant for their kind of business: {demo}
- A 38-second video of how it works: {video}
- Vincent on Telegram: {telegram}
A business replied to Vincent's email. Write his answer:
- In the language of their reply (Traditional Chinese if they wrote Chinese).
- Short: 3 to 6 sentences, friendly and plain, like a person, no marketing words.
- Answer exactly what they asked. If they are interested, offer to start the free trial now and ask for
  the one thing he needs. If they ask how it works or what it costs, answer from the facts above.
- If they decline or ask him to stop, thank them in one or two sentences and say he won't email again.
- Never invent features, clients, results or numbers that aren't above. No subject line, no placeholders.
- End with "Vincent" on its own line."""


# --- a shop that made its own free trial and left its email: how to use it, and how to go live -----
TRIAL_MAIL_SUBJECT = "Your AI assistant for {name} is ready | {name} 的 AI 客服做好了"
TRIAL_MAIL_BODY = """Hi,

Your free AI assistant for {name} is ready, and it stays online: {link}

Ask it what your customers usually ask. To put it on your website, paste this one line just before </body> (Wix, Squarespace and WordPress all work):

{embed}

During the trial, the bookings and enquiries it takes come to me and I pass them on. When you're happy with it, I'll make it official: it alerts you directly (by email, WhatsApp or Telegram), I fine-tune it to your prices and rules, and it's a one-off US$70, then US$9.9 a month, cancel anytime.

Any questions, just reply to this email.

Vincent

———— 繁體中文 ————

你好，

{name} 的免費 AI 客服已經做好了，一直都在線上：{link}

可以像客人一樣問問它。想放進你的網站，把上面那一行代碼貼到網站的 </body> 前面就可以（Wix、Squarespace、WordPress 都行）。

試用期間，它接到的預約和查詢會先通知我，我再轉給你。滿意的話我幫你正式上線：直接通知你本人（Email、WhatsApp 或 Telegram），按你的價目和規定再調好，一次性 US$70，之後每月 US$9.9，隨時可停。

有任何問題，直接回覆這封郵件就好。

Vincent"""


def compose_trial(name: str, link: str, embed: str, english: bool = False) -> tuple[str, str]:
    """English + 繁體; English alone for a trial made on the English page."""
    subject = TRIAL_MAIL_SUBJECT.format(name=name)
    body = TRIAL_MAIL_BODY.format(name=name, link=link, embed=embed)
    if english:
        subject, body = subject.split(" | ")[0], body.split("\n\n———— 繁體中文 ————")[0]
    return subject, body


def reply_messages(lead: dict, their_subject: str, their_text: str) -> list[dict]:
    """The chat for the model that drafts Vincent's answer to a business's reply."""
    if lead.get("kind") == "trial":
        # someone who made a free trial on the site: the first email was its how-to (link kept in "site")
        _, first = compose_trial(lead["name"], lead.get("site") or "", "(one line of code)")
        lead = {**lead, "personal": lead.get("site") or ""}
    else:
        _, first = compose(lead)
    system = REPLY_SYSTEM.format(demo=lead.get("personal") or demo_link(lead) or general_demo_link(lead),
                                 video=video_link(lead), telegram=TELEGRAM_LINK)
    user = (f"Business: {lead['name']} ({lead.get('region') or ''})\n\n"
            f"Vincent's first email:\n{plain(first, lead)[:2500]}\n\n"
            f"Their reply (subject: {their_subject}):\n{their_text[:2500]}\n\nWrite Vincent's reply.")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def plain(body: str, lead: dict) -> str:
    """The plain-text version: the mockup as a link (or nothing, without one)."""
    slug = mockup_slug(lead)
    return body.replace(MOCKUP_MARK, f"(mockup: {MOCKUP_URL.format(slug=slug)})" if slug else "").replace("\n\n\n\n", "\n\n")


def to_html(body: str, lead: dict) -> str:
    import html as _html
    link = re.compile(r"(https://\S+)")
    paras = []
    for para in body.split("\n\n"):
        if para == MOCKUP_MARK:
            alt = _html.escape(f"Mockup of {lead.get('client') or lead['name']}'s website with a 24/7 assistant", quote=True)
            paras.append(f'<p><img src="cid:mockup" width="560" alt="{alt}" '
                         'style="width:100%;max-width:560px;height:auto;border:1px solid #e5e5ea;border-radius:12px"></p>')
            continue
        text = link.sub(r'<a href="\1">\1</a>', _html.escape(para)).replace("\n", "<br>")
        paras.append(f"<p>{text}</p>")
    return ('<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;color:#1d1d1f;max-width:600px">'
            + "".join(paras) + "</div>")


def daily_window(region: str, now: dt.datetime | None = None) -> bool:
    """The everyday English-market emails: weekday mornings, recipient's clock (replies come best then)."""
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(REGION_TZ[region]))
    return local.weekday() < 5 and INTL_HOURS[0] <= local.time() <= INTL_HOURS[1]


def followup_window(lead: dict, now: dt.datetime | None = None) -> bool:
    """Follow-ups go in the recipient's weekday afternoon (13:00–16:30), after the day's first emails."""
    tz = REGION_TZ.get(lead.get("region") or "", TIMEZONE)
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(tz))
    return local.weekday() < 5 and dt.time(13, 0) <= local.time() <= dt.time(16, 30)


def in_work_hours(lead: dict, now: dt.datetime | None = None) -> bool:
    """English-market emails go out on the recipient's own clock, 8:30–18:00."""
    tz = REGION_TZ.get(lead.get("region") or "")
    if not tz:
        return True
    local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(ZoneInfo(tz)).time()
    return WORK_HOURS[0] <= local <= WORK_HOURS[1]


def script_for(secret: str) -> str:
    return MAILER_SCRIPT.replace("{secret}", secret)


def compose(lead: dict) -> tuple[str, str]:
    """Subject and body for one business: 繁體中文 first, then the same email in English.
    English-speaking markets get the English letter alone, with a link to their mockup."""
    if is_intl(lead):
        subject, body = _intl_parts(lead)
        slug = mockup_slug(lead)
        return subject, body.replace(MOCKUP_MARK, f"(mockup: {MOCKUP_URL.format(slug=slug)})" if slug else "").replace("\n\n\n\n", "\n\n")
    # "try it on my site" goes to the demo shop of their own kind when there is one
    fill = {"name": lead["name"], "link": demo_link(lead) or SITE_LINK, "telegram": TELEGRAM_LINK, "email": REPLY_EMAIL,
            "demo": DEMO_LINK, "video": VIDEO_LINK}
    region = lead.get("region") or "sg"
    # "Monice Bakes 你好" but "思思蛋糕你好": a space only after a Latin name
    zh_name = lead["name"] + (" " if lead["name"][-1:].isascii() else "")
    if lead.get("kind") == "agency":
        zh = AGENCY_ZH_BODY.replace("{name}", zh_name, 1).format(first=lead.get("first_zh") or AGENCY_ZH_FIRST, **fill)
        en = AGENCY_EN_BODY.format(first=lead.get("first") or AGENCY_EN_FIRST, **fill)
        subject = AGENCY_ZH_SUBJECT.format(**fill) + " | " + AGENCY_EN_SUBJECT
        if lead.get("personal") and lead.get("client"):
            zh = zh.replace(f"做好的示範可以看這裡：{DEMO_LINK}", _agency_demo(lead, zh=True))
            en = en.replace(f"Here's a finished demo: {DEMO_LINK}", _agency_demo(lead))
        return subject, zh + BILINGUAL_RULE + en
    zh = ZH_BODY.replace("{name}", zh_name, 1).format(first=lead.get("first_zh") or ZH_FIRST, price=ZH_PRICE.get(region, ZH_PRICE["sg"]), **fill)
    # leads written in Chinese only carry a Chinese first line; English gets the general one
    first_en = lead.get("first") if lead.get("lang") != "zh" else ""
    en = EN_BODY.format(first=first_en or EN_FIRST, price=EN_PRICE.get(region, EN_PRICE["sg"]), **fill)
    subject = ZH_SUBJECT.replace("{name}", zh_name).format(**fill) + " | " + EN_SUBJECT.format(**fill)
    if lead.get("personal"):
        read = lead.get("personal_read")
        zh = zh.replace(f"可以先到我的網站試試看：{fill['link']}",
                        (PERSONAL_READ_ZH if read else PERSONAL_ZH).format(name=lead["name"], url=lead["personal"]))
        en = en.replace(f"You can try the one on my own site: {fill['link']}",
                        (PERSONAL_READ_EN_ASIA if read else PERSONAL_EN_ASIA).format(name=lead["name"], url=lead["personal"]))
    return subject, zh + BILINGUAL_RULE + en


def parse_leads(raw: str) -> list[dict]:
    """OUTREACH_LEADS, keeping only entries that have an email and a name."""
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict) and "@" in str(i.get("email", "")) and i.get("name")]


# the daily HK/SG/MY batch (and its limit) leaves the English-market countries alone
ASIA = "COALESCE(region, '') NOT IN ('uk', 'ie', 'au', 'nz', 'ca', 'us', 'trial')"


# errors from Gmail or the network rather than from the address: worth another try later
TRANSIENT = ("没有正常回应", "连不上")


def is_transient(error: Exception) -> bool:
    return any(t in str(error) for t in TRANSIENT)


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
            columns = [r[1] for r in db.execute("PRAGMA table_info(leads)")]
            if "first_zh" not in columns:
                db.execute("ALTER TABLE leads ADD COLUMN first_zh TEXT")
            for column in ("kind", "batch", "mockup", "cat", "host", "city", "site", "followed_at", "client", "client_site"):
                if column not in columns:
                    db.execute(f"ALTER TABLE leads ADD COLUMN {column} TEXT")

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

    def settings_like(self, prefix: str) -> list[tuple[str, str]]:
        """Every non-empty setting whose key starts with `prefix`."""
        with self._db() as db:
            rows = db.execute("SELECT key, value FROM outreach_settings WHERE substr(key, 1, ?) = ? AND value != ''",
                              (len(prefix), prefix)).fetchall()
        return [(r["key"], r["value"]) for r in rows]

    def secret(self) -> str:
        value = self.setting("secret")
        if not value:
            value = secrets.token_urlsafe(24)
            self.set_setting("secret", value)
        return value

    # --- leads -----------------------------------------------------------------------------
    def add(self, leads: list[dict]) -> int:
        """New businesses are added; ones already known keep their status. Businesses not yet
        emailed take the latest wording (name, first lines), so a fixed list applies before sending."""
        fields = ("name", "region", "lang", "first", "first_zh", "kind", "batch", "mockup", "cat", "host", "city", "site",
                  "client", "client_site")
        default = {"region": "sg", "lang": "en", "kind": "shop"}
        rows = [(l["email"].strip().lower(),) + tuple(str(l.get(f, default.get(f, ""))) for f in fields) for l in leads]
        with self._db() as db:
            count = lambda: db.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            before = count()
            db.executemany(f"INSERT OR IGNORE INTO leads (email, {', '.join(fields)}) "
                           f"VALUES ({', '.join('?' * (len(fields) + 1))})", rows)
            db.executemany(f"UPDATE leads SET {', '.join(f + '=?' for f in fields)} WHERE email=? AND status='new'",
                           [r[1:] + r[:1] for r in rows])
            return count() - before

    def get(self, email: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM leads WHERE email=?", (email.lower(),)).fetchone()
        return dict(row) if row else None

    def mark(self, email: str, status: str, note: str = "") -> None:
        sent_at = dt.datetime.now(dt.timezone.utc).isoformat() if status == "sent" else None
        with self._db() as db:
            db.execute("UPDATE leads SET status=?, note=?, sent_at=COALESCE(?, sent_at) WHERE email=?",
                       (status, note, sent_at, email.lower()))

    def _day_start(self, now: dt.datetime | None = None) -> dt.datetime:
        now = now or dt.datetime.now(dt.timezone.utc)
        return dt.datetime.combine(now.astimezone(self.tz).date(), dt.time(), self.tz).astimezone(dt.timezone.utc)

    def sent_today(self, now: dt.datetime | None = None) -> int:
        with self._db() as db:
            return db.execute(f"SELECT COUNT(*) FROM leads WHERE sent_at >= ? AND COALESCE(batch, '') = '' AND {ASIA}",
                              (self._day_start(now).isoformat(),)).fetchone()[0]

    def room_today(self, now: dt.datetime | None = None) -> int:
        return max(0, DAILY_LIMIT - self.sent_today(now))

    def due(self, now: dt.datetime | None = None) -> list[dict]:
        """Today's batch: the next businesses not yet emailed, up to what's left of the daily limit,
        taking each region's share from OUTREACH_MIX first."""
        room = self.room_today(now)
        with self._db() as db:
            waiting = [dict(r) for r in db.execute(
                f"SELECT * FROM leads WHERE status='new' AND COALESCE(batch, '') = '' AND {ASIA} ORDER BY rowid")]
            sent = dict(db.execute("SELECT region, COUNT(*) FROM leads WHERE sent_at >= ? GROUP BY region",
                                   (self._day_start(now).isoformat(),)).fetchall())
        batch = []
        for region, share in MIX.items():
            left = max(0, share - sent.get(region, 0))
            batch += [l for l in waiting if l["region"] == region][:min(left, room - len(batch))]
        batch += [l for l in waiting if l not in batch][:room - len(batch)]
        return batch

    def intl_due(self, region: str, now: dt.datetime | None = None) -> list[dict]:
        """Today's English-market emails for one country: up to its INTL_MIX share, counted on
        that country's own calendar day."""
        now = now or dt.datetime.now(dt.timezone.utc)
        tz = ZoneInfo(REGION_TZ[region])
        start = dt.datetime.combine(now.astimezone(tz).date(), dt.time(), tz).astimezone(dt.timezone.utc)
        with self._db() as db:
            sent = db.execute("SELECT COUNT(*) FROM leads WHERE region=? AND sent_at >= ? AND COALESCE(batch, '') = ''",
                              (region, start.isoformat())).fetchone()[0]
            room = max(0, INTL_MIX.get(region, 0) - sent)
            return [dict(r) for r in db.execute(
                "SELECT * FROM leads WHERE status='new' AND region=? AND COALESCE(batch, '') = '' ORDER BY rowid LIMIT ?",
                (region, room))]

    def waiting(self, region: str) -> int:
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM leads WHERE status='new' AND region=?", (region,)).fetchone()[0]

    def known(self) -> tuple[set[str], set[str]]:
        """Every email and (lower-case) name on the list, sent or not."""
        with self._db() as db:
            rows = db.execute("SELECT email, name FROM leads").fetchall()
        return {r[0].lower() for r in rows}, {r[1].lower() for r in rows}

    def retry_transient(self) -> int:
        """Emails that failed on a Gmail hiccup (not a bad address) go back on the list."""
        with self._db() as db:
            return db.execute("UPDATE leads SET status='new', note='' WHERE status='failed' AND "
                              "(note LIKE ? OR note LIKE ?)", (f"%{TRANSIENT[0]}%", f"%{TRANSIENT[1]}%")).rowcount

    def followup_due(self, now: dt.datetime | None = None, limit: int = 100) -> list[dict]:
        """Emailed FOLLOWUP_DAYS ago or more, no answer, not followed up yet (agencies aren't chased)."""
        now = now or dt.datetime.now(dt.timezone.utc)
        before = (now - dt.timedelta(days=FOLLOWUP_DAYS)).isoformat()
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM leads WHERE status='sent' AND sent_at <= ? AND followed_at IS NULL "
                "AND COALESCE(kind, '') NOT IN ('agency', 'trial') ORDER BY sent_at LIMIT ?", (before, limit))]

    def mark_followed(self, email: str) -> None:
        with self._db() as db:
            db.execute("UPDATE leads SET followed_at=? WHERE email=?",
                       (dt.datetime.now(dt.timezone.utc).isoformat(), email.lower()))

    def sent_last_24h(self, now: dt.datetime | None = None) -> int:
        """Every email that left in the last 24 hours, first ones and follow-ups."""
        since = ((now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(hours=24)).isoformat()
        with self._db() as db:
            return db.execute("SELECT (SELECT COUNT(*) FROM leads WHERE sent_at >= ?) + "
                              "(SELECT COUNT(*) FROM leads WHERE followed_at >= ?)", (since, since)).fetchone()[0]

    def batch_waiting(self) -> list[dict]:
        """Leads in a batch (see OUTREACH_LEADS) not emailed yet."""
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM leads WHERE status='new' AND COALESCE(batch, '') != '' ORDER BY rowid")]

    def emailed(self, days: int = 21) -> list[str]:
        """Businesses emailed in the last few weeks: the ones whose replies are still worth watching."""
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT email FROM leads WHERE sent_at >= ? ORDER BY sent_at DESC", (since,))]

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
            text = await self._post(session, {"secret": self.secret, "action": action, **body})
        except Exception as exc:  # noqa: BLE001 - network trouble becomes a readable error
            raise MailError(f"连不上 Gmail 发信脚本：{exc}") from exc
        finally:
            if own:
                await session.close()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Apps Script shows an HTML page when the script throws (a passing Gmail hiccup,
            # usually) or when the deployment isn't public; keep a little of it for the log
            page = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
            seen = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page)).strip()[:300]
            raise MailError("Gmail 发信脚本没有正常回应（部署时“谁可以访问”要选“任何人”）"
                            + (f"：{seen}" if seen else "")) from None
        if not data.get("ok"):
            raise MailError(f"Gmail 发信脚本出错：{data.get('error') or data}")
        return data

    async def _post(self, session: ClientSession, payload: dict) -> str:
        """Apps Script answers a POST with a redirect to the result (script.googleusercontent.com/
        macros/echo), read with a GET. Now and then it redirects to an /exec address instead, which
        has to be POSTed again; a GET there finds no doGet."""
        url, method = self.url, "POST"
        for _ in range(5):
            kwargs = {"json": payload} if method == "POST" else {}
            async with session.request(method, url, allow_redirects=False, **kwargs) as r:
                if r.status not in (301, 302, 303, 307, 308) or "Location" not in r.headers:
                    return await r.text()
                url = str(r.url.join(URL(r.headers["Location"])))
                method = "POST" if URL(url).path.endswith("/exec") else "GET"
        raise MailError("Gmail 发信脚本转了太多次")

    async def ping(self) -> str:
        return (await self._call("ping")).get("email", "")

    async def version(self) -> int:
        """1 for the first script (plain text only), 2 once it can send images."""
        return int((await self._call("ping")).get("version", 1))

    async def send(self, to: str, subject: str, body: str, html: str = "", image: bytes | None = None) -> None:
        extra = {}
        if html:
            extra = {"html": html, "images": {"mockup": base64.b64encode(image).decode()} if image else {}}
        await self._call("send", to=to, subject=subject, body=body, name=SENDER_NAME, **extra)

    async def replies(self, emails: list[str], days: int = 3, chunk: int = 15) -> list[dict]:
        """Replies from these addresses. A few at a time: one Gmail search with every address in it
        gets too long for Gmail once the list grows."""
        found = []
        for i in range(0, len(emails), chunk):
            found += (await self._call("replies", emails=emails[i:i + chunk], days=days)).get("replies", [])
        return found
