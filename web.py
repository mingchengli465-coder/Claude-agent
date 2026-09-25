"""The website chat window: a small web server next to the Telegram bot.

    GET  /            the owner's own site: services, prices, and the chat window
    GET  /demo        a demo page to send merchants (the chat window is on it)
    GET  /widget.js   the chat window itself; one <script> tag embeds it in any site
    POST /api/chat    a visitor's message -> the AI's reply (customer_service.handle)
    GET  /api/messages  new messages for a visitor, so the owner's replies show up

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

from aiohttp import web

import customer_service as cs

logger = logging.getLogger(__name__)

CHANNEL = "web"
# Railway (and most hosts) hand the port over in PORT.
WEB_PORT = int(os.environ.get("WEB_PORT") or os.environ.get("PORT") or "8080")
WEB_CHAT = os.environ.get("WEB_CHAT", "true").lower() != "false"
MAX_TEXT = 1000
_VISITOR = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_FAVICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='16' fill='#000'/>"
            "<text x='32' y='46' font-family='Georgia,serif' font-style='italic' font-size='40' fill='white'"
            " text-anchor='middle'>v</text></svg>")
_FONT = re.compile(r"^[a-z0-9-]+\.woff2$")
# Per IP address: messages a minute, and new visitor ids an hour (each new
# visitor pings the owner, so this is what keeps a script from spamming them).
IP_MESSAGES_PER_MINUTE = int(os.environ.get("WEB_IP_MESSAGES_PER_MINUTE", "20"))
IP_NEW_VISITORS_PER_HOUR = int(os.environ.get("WEB_IP_NEW_VISITORS_PER_HOUR", "5"))

_STATIC = Path(__file__).with_name("web_static")
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


class WebChat:
    def __init__(self, service: cs.CustomerService, title: str = "", contact_link: str = "",
                 clock=time.time):
        self.service = service
        self.title = title or "AI 客服"
        self.contact_link = contact_link
        self.clock = clock
        self.ip_messages = Window(IP_MESSAGES_PER_MINUTE, 60)
        self.ip_visitors = Window(IP_NEW_VISITORS_PER_HOUR, 3600)
        self.runner: web.AppRunner | None = None
        # Owner replies are stored by deliver_owner_reply; the widget polls them out.
        service.register_channel(CHANNEL, self._send)

    async def _send(self, chat_id: str, text: str) -> None:
        return None

    # ---- the app -----------------------------------------------------------------

    def app(self) -> web.Application:
        app = web.Application(client_max_size=64 * 1024)
        app.router.add_get("/", self.site)
        app.router.add_get("/demo", self.page)
        app.router.add_get("/widget.js", self.widget)
        app.router.add_get("/fonts/{name}", self.font)
        app.router.add_get("/healthz", self.health)
        app.router.add_get("/favicon.ico", self.favicon)
        app.router.add_post("/api/chat", self.chat)
        app.router.add_get("/api/messages", self.messages)
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

    async def site(self, request: web.Request) -> web.Response:
        return self._render("site.html")

    async def page(self, request: web.Request) -> web.Response:
        return self._render("demo.html")

    def _render(self, name: str) -> web.Response:
        body = (_STATIC / name).read_text(encoding="utf-8")
        contact = html.escape(self.contact_link, quote=True)
        body = (body.replace("{{TITLE}}", html.escape(self.title))
                    .replace("{{TITLE_ATTR}}", html.escape(self.title, quote=True))
                    .replace("{{CONTACT}}", contact)
                    .replace("{{CONTACT_DISPLAY}}", "" if contact else "none"))
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
        text = text[:MAX_TEXT]
        now, ip = self.clock(), self._ip(request)
        if not self.ip_messages.allow(ip, now):
            return self._json({"error": "slow down"}, 429)
        store = self.service.store
        if store.customer(CHANNEL, visitor) is None and not self.ip_visitors.allow(ip, now):
            return self._json({"error": "slow down"}, 429)

        lang = str(data.get("lang") or "").lower()
        latest = store.messages_after(CHANNEL, visitor, 0, 1)
        before = latest[-1]["id"] if latest else 0
        try:
            reply = await self.service.handle(cs.Inbound(
                channel=CHANNEL, chat_id=visitor, text=text,
                display_name=f"网页访客 {visitor[:4]}",
                lang="zh" if lang.startswith("zh") else "en" if lang else "",
            ))
        except Exception:  # noqa: BLE001 - the visitor gets an error, the bot keeps running
            logger.exception("网站聊天出错（%s）", visitor)
            return self._json({"error": "server error"}, 500)
        # The id the reply was stored under, so the widget doesn't show it twice
        # when it polls. (A rate-limit warning isn't stored and has none.)
        reply_id = None
        if reply is not None:
            for row in store.messages_after(CHANNEL, visitor, before):
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
        rows = self.service.store.messages_after(CHANNEL, visitor, after)
        return self._json({"messages": [
            {"id": r["id"], "role": r["role"], "text": r["text"], "ts": r["ts"]} for r in rows
        ]})
