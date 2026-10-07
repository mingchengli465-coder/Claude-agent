"""The website chat window: a small web server next to the Telegram bot.

    GET  /            the owner's own site: services, prices, and the chat window
    GET  /demo        a demo page to send merchants (the chat window is on it)
    GET  /demo-<kind> an industry demo: a pretend B&B, florist… whose own assistant answers (demos.py)
    GET  /trial       a free trial: a shop fills in its information and gets its own assistant (trials.py)
    POST /api/trial   make one -> {slug} (from what they typed, or read from their website's url);
                      GET /api/trial/<slug> what its page shows
    GET  /t/<slug>    a shop's own assistant: a free trial, or the personal demo a cold email links to
    GET  /video       the 38-second intro video (media/), linked from the cold emails
    GET  /mockups/<x>.png  a business's mockup from a cold email (web_static/mockups, or drawn ones on the volume)
    GET  /widget.js   the chat window itself; one <script> tag embeds it in any site
    POST /api/chat    a visitor's message -> the AI's reply (customer_service.handle)
    GET  /api/messages  new messages for a visitor, so the owner's replies show up
    POST /api/hit     one page view on the owner's own pages (visits.py)
    GET  /s/<slug>    a website the owner's agent built (agent.py)
    GET  /privacy     privacy policy (Meta asks for one before Messenger goes live)
    GET/POST /fb/webhook  Facebook Messenger, when it is configured (messenger.py)

Visitors are customers on the "web" channel, keyed by a random id the widget
keeps in the browser. The owner is told and replies in Telegram exactly as for
Telegram customers; a reply lands in the database and the widget polls it out.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

import customer_service as cs
import demos as demos_mod
import sitetext
import trials as trials_mod
import visits as visits_mod

logger = logging.getLogger(__name__)

CHANNEL = "web"
# Railway (and most hosts) hand the port over in PORT.
WEB_PORT = int(os.environ.get("WEB_PORT") or os.environ.get("PORT") or "8080")
WEB_CHAT = os.environ.get("WEB_CHAT", "true").lower() != "false"
# A client's personal-assistant bot serves only the websites its agent builds:
# no owner's homepage, demo page or customer-service chat on that address.
WEB_SITES_ONLY = os.environ.get("WEB_SITES_ONLY", "false").lower() == "true"
MAX_TEXT = 1000
ENGLISH_TITLE = os.environ.get("WEB_ENGLISH_TITLE", "vinc")
_VISITOR = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_FAVICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='16' fill='#000'/>"
            "<text x='32' y='46' font-family='Georgia,serif' font-style='italic' font-size='40' fill='white'"
            " text-anchor='middle'>v</text></svg>")
_FONT = re.compile(r"^[a-z0-9-]+\.woff2$")
_GUIDE = re.compile(r"^[a-z0-9-]+\.html$")
_MOCKUP = re.compile(r"^[a-z0-9-]+\.png$")
# The whale video the owner sends the bot (bot.py), kept on the volume, behind the homepage;
# until it's sent, the page draws its own deep sea.
SITE_MEDIA = {"hero.mp4": "video/mp4"}


def site_media_dir() -> Path:
    return Path(cs.CS_DB_PATH).with_name("site_media")


_ZH_SPAN = re.compile(r'<span class="zh">.*?</span>', re.S)
SHOWCASE_TEXT = {
    True: {"PITCH": "I made a free 24/7 AI assistant for my business in seconds, try it for yours: ",
           "COPIED": "Copied: paste it just before </body> on your site"},
    False: {"PITCH": "我剛免費做了一個 24 小時回覆客人的 AI 客服，貼上自己店的網址幾秒就好 / "
                     "I made a free 24/7 AI assistant for my business in seconds, try it for yours: ",
            "COPIED": "已複製：貼到網站的 </body> 前面就好 · Copied: paste it before </body>"},
}


# the big words on the demo pages stay English; the 繁/EN pages add a small Chinese line underneath
SHOWCASE_BIG = {
    True: {"BIG_H1": "OPEN 24/7", "BIG_VOL": "its assistant never sleeps", "BIG_LIVE": "LIVE DEMO:",
           "BIG_ANSWERS": "ANSWERS 24/7", "BIG_ASK": "ASK IT NOW", "BIG_MINE": "MAKE IT YOURS:",
           "BIG_WHAT": "Paste your website,<br>ready in seconds", "BIG_VERT": "Quiet hours hold new growth",
           "BIG_ART": "Wake The Quiet Hours", "BIG_ARTSUB": "where closed doors still bloom"},
    False: {"BIG_H1": "OPEN 24/7", "BIG_VOL": "its assistant never sleeps<span class='sub'>打烊了，它還在接客</span>",
            "BIG_LIVE": "LIVE DEMO:", "BIG_ANSWERS": "ANSWERS 24/7<span class='sub'>24 小時回覆客人</span>",
            "BIG_ASK": "ASK IT NOW", "BIG_MINE": "MAKE IT YOURS:",
            "BIG_WHAT": "Paste your website,<br>ready in seconds<span class='sub'>貼上你的網址，幾秒鐘做好</span>",
            "BIG_VERT": "Quiet hours hold new growth", "BIG_ART": "Wake The Quiet Hours",
            "BIG_ARTSUB": "where closed doors still bloom<span class='sub'>打烊後也有生意</span>"},
}


def showcase_fill(body: str, kind: str = "", english: bool = False) -> str:
    """The two-page demo of one business: an industry demo (its data inline) or, without a kind,
    a business's own demo (the page fetches it from /api/trial/<slug>). English: no 繁體 at all."""
    esc = lambda t: html.escape(t, quote=True)  # noqa: E731
    d = demos_mod.DEMOS.get(kind)
    shop = ({"kind": kind, "name": d["name"], "where": d["where"], "questions": d["questions"], "slug": "",
             "sample": False, "site": ""} if d else None)
    name = d["name"] if d else ""
    questions = d["questions"] if d else []
    fill = {"SHOP_JSON": json.dumps(shop, ensure_ascii=False).replace("</", "<\\/"),
            "SHOP_TITLE": esc(name or "Your assistant"), "SHOP_NAME": esc(name), "SHOP_WHERE": esc(d["where"] if d else ""),
            "SHOP_LINES": "".join(f"<p>{esc(t)}</p>" for t in questions * 2),
            "SHOP_QUESTIONS": "".join(f'<div class="line">{esc(t)}</div>' for t in questions),
            "DEEP_LANG": "en" if english else "zh", **SHOWCASE_TEXT[english], **SHOWCASE_BIG[english]}
    for key, value in fill.items():
        body = body.replace("{{" + key + "}}", value)
    return _ZH_SPAN.sub("", body).replace('<html lang="zh-TW">', '<html lang="en">', 1) if english else body
# Per IP address: messages a minute, and new visitor ids an hour (each new
# visitor pings the owner, so this is what keeps a script from spamming them).
IP_MESSAGES_PER_MINUTE = int(os.environ.get("WEB_IP_MESSAGES_PER_MINUTE", "20"))
IP_NEW_VISITORS_PER_HOUR = int(os.environ.get("WEB_IP_NEW_VISITORS_PER_HOUR", "5"))
IP_HITS_PER_MINUTE = int(os.environ.get("WEB_IP_HITS_PER_MINUTE", "30"))
# Free trials: a few per address an hour, and a ceiling a day for everyone together.
IP_TRIALS_PER_HOUR = int(os.environ.get("WEB_IP_TRIALS_PER_HOUR", "3"))
TRIALS_PER_DAY = int(os.environ.get("WEB_TRIALS_PER_DAY", "60"))
# a personal demo being opened is told to the owner at most this often
TRIAL_VIEW_NOTICE_SECONDS = 6 * 3600

_STATIC = Path(__file__).with_name("web_static")
# The intro video and its poster: the only files under media/ the site serves.
_MEDIA = {"vinc-intro.mp4": "video/mp4", "vinc-intro-poster.jpg": "image/jpeg"}
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
}


class Window:
    """At most `limit` events per `seconds` per key."""

    def __init__(self, limit: int, seconds: float):
        self.limit, self.seconds = limit, seconds
        self.events: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, now: float) -> bool:
        events = self.events[key]
        while events and now - events[0] >= self.seconds:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


def shop_name(path: Path = cs.CS_PRODUCTS_PATH) -> str:
    """The shop's name from products.yaml, for the page and the chat window title."""
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = str((data.get("店铺") or {}).get("名称") or "").strip()
        return "" if name == cs.PLACEHOLDER else name
    except Exception:  # noqa: BLE001 - a title is not worth failing over
        return ""


_TELEGRAM_URL = re.compile(r"^https://t\.me/[A-Za-z0-9_]{4,64}$")
_WECHAT_ID = re.compile(r"^[A-Za-z][-_A-Za-z0-9]{4,31}$")


def owner_contacts(path: Path = cs.CS_PRODUCTS_PATH) -> tuple[str, list[str]]:
    """(Telegram link, WeChat IDs) from 联系本人 in products.yaml; anything
    malformed is left out rather than put on the page."""
    try:
        import yaml

        data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("联系本人") or {}
    except Exception:  # noqa: BLE001 - the page still works without contacts
        return "", []
    telegram = str(data.get("Telegram") or "").strip()
    wechat = data.get("微信号") or []
    if isinstance(wechat, str):
        wechat = [wechat]
    return (telegram if _TELEGRAM_URL.match(telegram) else "",
            [w for w in (str(x).strip() for x in wechat) if _WECHAT_ID.match(w)])


_OTHER_LINKS = {
    # key in products.yaml: (label, pattern, how to turn the value into a link)
    "X": ("X", re.compile(r"^https://(x|twitter)\.com/[A-Za-z0-9_]{1,15}/?$"), lambda v: v),
    "Facebook": ("Facebook", re.compile(r"^https://(www\.|m\.)?facebook\.com/[A-Za-z0-9_.?=&/-]{1,120}$"), lambda v: v),
    "邮箱": ("Email", re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,120}\.[A-Za-z]{2,24}$"), lambda v: "mailto:" + v),
}


def owner_links(path: Path = cs.CS_PRODUCTS_PATH) -> list[tuple[str, str, str]]:
    """[(label, href, text)] for the owner's X / Facebook / email in 联系本人,
    in that order; empty or malformed entries are left out."""
    try:
        import yaml

        data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("联系本人") or {}
    except Exception:  # noqa: BLE001 - the page still works without them
        return []
    links = []
    for key, (label, pattern, href) in _OTHER_LINKS.items():
        value = str(data.get(key) or "").strip()
        if pattern.match(value):
            handle = value.rstrip("/").rsplit("/", 1)[-1]
            text = value if key == "邮箱" else "→" if "?" in handle else "@" + handle
            links.append((label, href(value), text))
    return links



def demo_fill(body: str, kind: str, english: bool = False) -> str:
    """The industry demo page for one kind of business (English-only: links to the other English ones)."""
    d = demos_mod.DEMOS[kind]
    esc = lambda t: html.escape(t, quote=True)  # noqa: E731
    questions = "".join(f'<button class="chip" type="button" onclick="window.aiChatAsk && aiChatAsk(this.textContent)">{esc(q)}</button>'
                        for q in d["questions"])
    others = "".join(f'<a href="demo-{k}-en">{esc(o["label"])}</a>' if english else
                     f'<a href="demo-{k}">{esc(o["label_zh"])} · {esc(o["label"])}</a>'
                     for k, o in demos_mod.DEMOS.items() if k != kind)
    return (body.replace("{{DEMO_KIND}}", kind).replace("{{DEMO_NAME}}", esc(d["name"]))
                .replace("{{DEMO_LABEL_ZH}}", esc(d["label_zh"])).replace("{{DEMO_LABEL_LOWER}}", esc(d["label"].lower()))
                .replace("{{DEMO_LABEL}}", esc(d["label"])).replace("{{DEMO_WHERE}}", esc(d["where"]))
                .replace("{{DEMO_QUESTIONS}}", questions).replace("{{DEMO_OTHERS}}", others))

class WebChat:
    def __init__(self, service: cs.CustomerService, title: str = "", contact_link: str = "",
                 clock=time.time, visits: visits_mod.Visits | None = None, messenger=None, sites=None,
                 demos: dict | None = None, trials: trials_mod.Trials | None = None, trial_service=None,
                 on_trial=None, media_origin: str | None = None):
        self.service = service
        # where the owner's pictures and video are served from: this server (None: when they're on
        # the volume), or another origin for a copy of the pages hosted elsewhere (GitHub Pages)
        self.media_origin = media_origin
        # free trials and personal demos (trials.py): trial_service(row) builds each one's assistant,
        # on_trial(event, row) tells the owner ("new" for a trial made, "view" for a personal demo opened)
        self.trials = trials
        self.trial_service = trial_service
        self.on_trial = on_trial
        self._trial_services: dict[str, cs.CustomerService] = {}
        self._trial_views: dict[str, float] = {}
        # industry demos: kind -> its own CustomerService (demos.py), chats kept under "web-<kind>"
        self.demos = demos or {}
        self.sites = sites
        self.visits = visits
        self.messenger = messenger
        self.title = title or "AI 客服"
        self.contact_link = contact_link
        self.clock = clock
        self.ip_messages = Window(IP_MESSAGES_PER_MINUTE, 60)
        self.ip_visitors = Window(IP_NEW_VISITORS_PER_HOUR, 3600)
        self.ip_hits = Window(IP_HITS_PER_MINUTE, 60)
        self.ip_trials = Window(IP_TRIALS_PER_HOUR, 3600)
        self.all_trials = Window(TRIALS_PER_DAY, 86400)
        self.runner: web.AppRunner | None = None
        # Owner replies are stored by deliver_owner_reply; the widget polls them out.
        if service is not None:
            service.register_channel(CHANNEL, self._send)
            # an owner reply to a demo visitor's notice comes through the main service
            for kind in self.demos:
                service.register_channel(f"{CHANNEL}-{kind}", self._send)
            service.register_channel(f"{CHANNEL}-t-*", self._send)
        for kind, demo in self.demos.items():
            demo.register_channel(f"{CHANNEL}-{kind}", self._send)

    async def _send(self, chat_id: str, text: str) -> None:
        return None

    # ---- the app -----------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        if WEB_SITES_ONLY or self.service is None:
            app.router.add_get("/", self.sites_home)
            app.router.add_get("/healthz", self.health)
            app.router.add_get("/favicon.ico", self.favicon)
            app.router.add_get("/s/{slug}", self.built_site)
            return app
        app.router.add_get("/", self.home)
        app.router.add_get("/about", self.site)
        app.router.add_get("/site-media/{name}", self.site_media)
        app.router.add_get("/demo", self.page)
        app.router.add_get("/demo-{kind}", self.demo_page)
        app.router.add_get("/video", self.video)
        app.router.add_get("/video-en", self.video_en)
        app.router.add_get("/trial", self.trial_page)
        app.router.add_get("/trial-en", self.trial_page_en)
        app.router.add_get("/t/{slug}", self.trial_shop)
        app.router.add_post("/api/trial", self.trial_create)
        app.router.add_get("/api/trial/{slug}", self.trial_info)
        app.router.add_get("/media/{name}", self.media)
        app.router.add_get("/mockups/{name}", self.mockup)
        app.router.add_get("/widget.js", self.widget)
        app.router.add_get("/fonts/{name}", self.font)
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/favicon.ico", self.favicon)
        app.router.add_post("/api/chat", self.chat)
        app.router.add_get("/api/messages", self.messages)
        app.router.add_post("/api/hit", self.hit)
        app.router.add_get("/privacy", self.privacy)
        app.router.add_get("/guides", self.guides_home)
        app.router.add_get("/guides/", self.guide)
        app.router.add_get("/guides/{name}", self.guide)
        app.router.add_get("/s/{slug}", self.built_site)
        if self.messenger is not None:
            self.messenger.routes(app)
        app.router.add_route("OPTIONS", "/api/{tail:.*}", self.preflight)
        return app

    async def start(self, port: int = WEB_PORT) -> None:
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", port).start()
        logger.info("网站聊天窗口已启动，端口 %s", port)

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    # ---- pages -------------------------------------------------------------------

    async def home(self, request: web.Request) -> web.Response:
        return self._render("home.html")

    async def site(self, request: web.Request) -> web.Response:
        return self._render("site.html")

    async def site_media(self, request: web.Request) -> web.StreamResponse:
        """The owner's own video for the site (sent to the bot), with range requests for phones."""
        name = request.match_info["name"]
        path = site_media_dir() / name
        if name not in SITE_MEDIA or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": SITE_MEDIA[name], "Cache-Control": "public, max-age=600", **CORS})

    def media_url(self, name: str) -> str:
        if self.media_origin is not None:
            return f"{self.media_origin}/site-media/{name}"
        return f"/site-media/{name}" if (site_media_dir() / name).is_file() else ""

    async def page(self, request: web.Request) -> web.Response:
        return self._render("demo.html")

    async def demo_page(self, request: web.Request) -> web.Response:
        kind = request.match_info["kind"]
        english = kind.endswith("-en")  # /demo-bnb-en: the English-only page
        kind = kind.removesuffix("-en")
        if kind not in demos_mod.DEMOS:
            raise web.HTTPNotFound()
        return self._render("showcase.html", demo=kind, english=english)

    async def video(self, request: web.Request) -> web.Response:
        return self._render("video.html")

    async def video_en(self, request: web.Request) -> web.Response:
        return self._render("video-en.html")

    async def trial_page(self, request: web.Request) -> web.Response:
        return self._render("trial.html")

    async def trial_page_en(self, request: web.Request) -> web.Response:
        return self._render("trial-en.html")

    async def trial_shop(self, request: web.Request) -> web.Response:
        # the page fetches the shop from /api/trial/<slug> (the same page is on GitHub Pages as t.html)
        if self.trials is None or self.trials.get(request.match_info["slug"]) is None:
            raise web.HTTPNotFound(text="找不到這個示範 / Not found")
        page = self._render("showcase.html", english=request.query.get("lang") == "en", showcase=True)
        # relative links (video, trial…) from /t/<slug> point at the site's root
        page.text = page.text.replace("<head>", '<head>\n<base href="/">', 1)
        return page

    async def media(self, request: web.Request) -> web.StreamResponse:
        """The intro video (with range requests, which phones need to play it)."""
        name = request.match_info["name"]
        path = Path(__file__).with_name("media") / name
        if name not in _MEDIA or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": _MEDIA[name], "Cache-Control": "public, max-age=86400"})

    async def mockup(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if not _MOCKUP.match(name):
            raise web.HTTPNotFound()
        for folder in (_STATIC / "mockups", Path(cs.CS_DB_PATH).with_name("mockups")):
            if (folder / name).is_file():
                return web.FileResponse(folder / name, headers={"Content-Type": "image/png",
                                                                "Cache-Control": "public, max-age=86400"})
        raise web.HTTPNotFound()

    async def sites_home(self, request: web.Request) -> web.Response:
        return web.Response(text="🌐", content_type="text/plain")

    async def built_site(self, request: web.Request) -> web.Response:
        site = self.sites.get(request.match_info["slug"]) if self.sites is not None else None
        if site is None:
            raise web.HTTPNotFound(text="这个网站不存在 / Not found")
        # Model-written pages run sandboxed, in their own origin: their scripts
        # can't touch this site's storage or call its API as the owner's pages.
        return web.Response(text=site["html"], content_type="text/html", headers={
            "Content-Security-Policy": "sandbox allow-scripts allow-forms allow-popups allow-modals",
            "Cache-Control": "no-cache"})

    async def guides_home(self, request: web.Request) -> web.Response:
        raise web.HTTPMovedPermanently("/guides/")

    async def guide(self, request: web.Request) -> web.Response:
        """The how-to guides (web_static/guides): Claude Code, Codex, AI customer service."""
        name = request.match_info.get("name", "index.html")
        if not _GUIDE.match(name) or not (_STATIC / "guides" / name).is_file():
            raise web.HTTPNotFound()
        return self._render(f"guides/{name}")

    async def privacy(self, request: web.Request) -> web.Response:
        return self._render("privacy.html")

    def _render(self, name: str, demo: str = "", english: bool | None = None, showcase: bool = False) -> web.Response:
        body = (_STATIC / name).read_text(encoding="utf-8")
        # the deep-sea pages share their styles and script, written into each page (one file each)
        body = (body.replace("{{DEEP_CSS}}", (_STATIC / "partials" / "deep.css").read_text(encoding="utf-8"))
                    .replace("{{DEEP_JS}}", (_STATIC / "partials" / "deep.js").read_text(encoding="utf-8")))
        body = body.replace("{{HERO_VIDEO}}", html.escape(self.media_url("hero.mp4"), quote=True))
        if english is None:
            english = name.endswith("-en.html")
        if name == "showcase.html" or showcase:
            body = showcase_fill(body, demo, english)
        elif demo:
            body = demo_fill(body, demo, english=english)
        contact = html.escape(self.contact_link, quote=True)
        telegram, wechat = owner_contacts()
        links = owner_links()
        links_html = "".join(
            f'<a class="social glass" href="{html.escape(href, quote=True)}" target="_blank" rel="noopener">'
            f'<b>{html.escape(label)}</b><span>{html.escape(text)}</span></a>'
            for label, href, text in links)
        wechat_html = "".join(
            f'<button type="button" class="wx" data-copy="{w}"><span>{w}</span><small data-i="wxCopy">複製</small></button>'
            for w in (html.escape(w, quote=True) for w in wechat))
        # the English-only pages (for the English-speaking countries) carry no Chinese, the name included
        title = ENGLISH_TITLE if english or name in ("home.html", "showcase.html") else self.title
        body = (body.replace("{{TITLE}}", html.escape(title))
                    .replace("{{TITLE_ATTR}}", html.escape(title, quote=True))
                    .replace("{{CONTACT}}", contact)
                    .replace("{{CONTACT_DISPLAY}}", "" if contact else "none")
                    .replace("{{OWNER_TG}}", html.escape(telegram, quote=True))
                    .replace("{{OWNER_TG_HANDLE}}", html.escape("@" + telegram.rsplit("/", 1)[-1] if telegram else ""))
                    .replace("{{OWNER_TG_DISPLAY}}", "" if telegram else "none")
                    .replace("{{WECHAT_IDS}}", wechat_html)
                    .replace("{{WECHAT_TEXT}}", html.escape(" / ".join(wechat)))
                    .replace("{{WECHAT_DISPLAY}}", "" if wechat else "none")
                    .replace("{{SOCIAL_LINKS}}", links_html)
                    .replace("{{SOCIAL_DISPLAY}}", "" if links else "none")
                    .replace("{{EMAIL}}", html.escape(next((t for l, h, t in links if h.startswith("mailto:")), "")))
                    .replace("{{TRIAL_KINDS}}", "".join(
                        f'<option value="{k}">{html.escape(d["label"])}</option>' if name.endswith("-en.html") else
                        f'<option value="{k}">{html.escape(d["label_zh"])} · {html.escape(d["label"])}</option>'
                        for k, d in demos_mod.DEMOS.items())))
        return web.Response(text=body, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def widget(self, request: web.Request) -> web.Response:
        body = (_STATIC / "widget.js").read_text(encoding="utf-8")
        return web.Response(text=body, content_type="application/javascript",
                            headers={**CORS, "Cache-Control": "public, max-age=300"})

    async def font(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        path = _STATIC / "fonts" / name
        if not _FONT.match(name) or not path.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=path.read_bytes(), content_type="font/woff2",
                            headers={**CORS, "Cache-Control": "public, max-age=2592000"})

    async def favicon(self, request: web.Request) -> web.Response:
        # Pages carry their icon inline; this only stops browsers' automatic lookup from 404ing.
        return web.Response(text=_FAVICON, content_type="image/svg+xml",
                            headers={"Cache-Control": "public, max-age=2592000"})

    async def health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def preflight(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=CORS)

    # ---- api ---------------------------------------------------------------------

    @staticmethod
    def _json(data: dict, status: int = 200) -> web.Response:
        return web.Response(text=json.dumps(data, ensure_ascii=False), status=status,
                            content_type="application/json", headers=CORS)

    @staticmethod
    def _ip(request: web.Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For", "")
        return forwarded.split(",")[0].strip() or request.remote or "?"

    async def chat(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - bad JSON, too big, wrong type
            return self._json({"error": "bad request"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "bad request"}, 400)
        visitor = str(data.get("v") or "")
        text = str(data.get("text") or "").strip()
        if not _VISITOR.match(visitor) or not text:
            return self._json({"error": "bad request"}, 400)
        demo = str(data.get("demo") or "")
        service, channel = self._service_for(demo)
        if service is None:
            return self._json({"error": "bad request"}, 400)
        text = text[:MAX_TEXT]
        now, ip = self.clock(), self._ip(request)
        if not self.ip_messages.allow(ip, now):
            return self._json({"error": "slow down"}, 429)
        store = service.store
        if store.customer(channel, visitor) is None and not self.ip_visitors.allow(ip, now):
            return self._json({"error": "slow down"}, 429)

        lang = str(data.get("lang") or "").lower()
        latest = store.messages_after(channel, visitor, 0, 1)
        before = latest[-1]["id"] if latest else 0
        try:
            reply = await service.handle(cs.Inbound(
                channel=channel, chat_id=visitor, text=text,
                display_name=f"{'示范页' if demo else '网页'}访客 {visitor[:4]}",
                lang="zh" if lang.startswith("zh") else "en" if lang else "",
            ))
        except Exception:  # noqa: BLE001 - the visitor gets an error, the bot keeps running
            logger.exception("网站聊天出错（%s）", visitor)
            return self._json({"error": "server error"}, 500)
        # The id the reply was stored under, so the widget doesn't show it twice
        # when it polls. (A rate-limit warning isn't stored and has none.)
        reply_id = None
        if reply is not None:
            for row in store.messages_after(channel, visitor, before):
                if row["role"] == "assistant" and row["text"] == reply:
                    reply_id = row["id"]
        return self._json({"reply": reply, "reply_id": reply_id})

    async def messages(self, request: web.Request) -> web.Response:
        visitor = request.query.get("v", "")
        if not _VISITOR.match(visitor):
            return self._json({"error": "bad request"}, 400)
        try:
            after = max(0, int(request.query.get("after", "0")))
        except ValueError:
            after = 0
        service, channel = self._service_for(request.query.get("d", ""))
        if service is None:
            return self._json({"error": "bad request"}, 400)
        rows = service.store.messages_after(channel, visitor, after)
        return self._json({"messages": [
            {"id": r["id"], "role": r["role"], "text": r["text"], "ts": r["ts"]} for r in rows
        ]})

    def _service_for(self, demo: str):
        """The owner's own assistant, an industry demo's, or a trial's ("t-<slug>"); None for an unknown one."""
        if not demo:
            return self.service, CHANNEL
        channel = f"{CHANNEL}-{demo}"
        if not demo.startswith("t-"):
            return self.demos.get(demo), channel
        slug = demo[2:]
        if slug not in self._trial_services:
            row = self.trials.get(slug) if self.trials is not None and self.trial_service is not None else None
            if row is None:
                return None, channel
            svc = self.trial_service(row)
            if svc is None:
                return None, channel
            svc.register_channel(channel, self._send)
            self._trial_services[slug] = svc
        return self._trial_services[slug], channel

    # ---- free trials -------------------------------------------------------------

    async def _tell(self, event: str, row: dict) -> None:
        if self.on_trial is None:
            return
        try:
            await self.on_trial(event, row)
        except Exception:  # noqa: BLE001 - the shop still gets its assistant
            logger.exception("试用通知没发出去")

    async def trial_create(self, request: web.Request) -> web.Response:
        if self.trials is None or self.trial_service is None:
            return self._json({"error": "not available"}, 404)
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - bad JSON, too big, wrong type
            return self._json({"error": "bad request"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "bad request"}, 400)
        field = lambda key, n: str(data.get(key) or "").strip()[:n]  # noqa: E731
        name, info, url = field("name", 80), field("info", trials_mod.MAX_INFO), field("url", 300)
        site = sitetext.normalise(url) if url and not info else ""
        if url and not info and not site:
            return self._json({"error": "url"}, 400)
        if len(name) < 2 and not site:
            return self._json({"error": "name"}, 400)
        now, ip = self.clock(), self._ip(request)
        if not self.ip_trials.allow(ip, now) or not self.all_trials.allow("all", now):
            return self._json({"error": "slow down"}, 429)
        if site:
            # "paste your website": the assistant answers from what the site says
            async with ClientSession(timeout=ClientTimeout(total=45)) as session:
                title, info = await sitetext.read_site(session, site)
            if not info:
                return self._json({"error": "site"}, 422)
            name = name if len(name) >= 2 else title or (sitetext.urlparse(site).hostname or "").removeprefix("www.")
        row = self.trials.create(name, field("kind", 20), info, field("contact", 120), site=site)
        row = {**row, "lang": "en" if field("lang", 5) == "en" else ""}  # which page it was made on
        await self._tell("new", row)
        return self._json({"slug": row["slug"]})

    async def trial_info(self, request: web.Request) -> web.Response:
        row = self.trials.get(request.match_info["slug"]) if self.trials is not None else None
        if row is None:
            return self._json({"error": "not found"}, 404)
        if row.get("source") == "lead":
            # the business we emailed opened the demo made in its name: worth knowing now
            now, slug = self.clock(), row["slug"]
            if now - self._trial_views.get(slug, -TRIAL_VIEW_NOTICE_SECONDS) >= TRIAL_VIEW_NOTICE_SECONDS:
                self._trial_views[slug] = now
                await self._tell("view", row)
        return self._json(trials_mod.public(row))

    async def hit(self, request: web.Request) -> web.Response:
        if self.visits is None:
            return self._json({"ok": False})
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - bad JSON, too big, wrong type
            return self._json({"error": "bad request"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "bad request"}, 400)
        visitor = str(data.get("v") or "")
        if not _VISITOR.match(visitor):
            return self._json({"error": "bad request"}, 400)
        if not self.ip_hits.allow(self._ip(request), self.clock()):
            return self._json({"error": "slow down"}, 429)
        text = lambda key, n: str(data.get(key) or "")[:n]  # noqa: E731
        me = text("me", 64)
        owner = self.visits.mark_owner(visitor, me) if me else False
        self.visits.record(visitor, text("page", 200), text("ref", 300), request.headers.get("User-Agent", ""),
                           text("from", 20), text("lang", 16), text("host", 80))
        return self._json({"ok": True, "owner": owner})
