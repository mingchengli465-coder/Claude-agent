"""Facebook Messenger: customers who message the owner's Facebook Page get the
same AI customer service as Telegram and the website.

Meta calls our webhook with each message; we answer 200 at once and reply
through the Graph API from a background task (Meta retries anything slower
than ~20 s). Owner replies from Telegram go back out the same way.

Environment (all three are needed, or Messenger stays off):
    FB_PAGE_TOKEN     the Page access token from the Meta app's Messenger settings
    FB_APP_SECRET     the app secret, to check that deliveries really come from Meta
    FB_VERIFY_TOKEN   any string; typed into the Meta dashboard when adding the webhook
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections import deque

from aiohttp import ClientSession, ClientTimeout, web

import customer_service as cs

logger = logging.getLogger(__name__)

CHANNEL = "facebook"
GRAPH_URL = os.environ.get("FB_GRAPH_URL", "https://graph.facebook.com/v21.0").rstrip("/")
MAX_TEXT = 2000  # Messenger's limit per message


class Messenger:
    def __init__(self, service: cs.CustomerService, page_token: str, app_secret: str, verify_token: str,
                 session: ClientSession | None = None):
        self.service = service
        self.page_token = page_token
        self.app_secret = app_secret.encode()
        self.verify_token = verify_token
        self.session = session
        self.seen: deque[str] = deque(maxlen=500)  # Meta sometimes delivers a message twice
        self.tasks: set[asyncio.Task] = set()
        service.register_channel(CHANNEL, self.send)

    @classmethod
    def from_env(cls, service: cs.CustomerService) -> "Messenger | None":
        token, secret, verify = (os.environ.get(k, "").strip() for k in ("FB_PAGE_TOKEN", "FB_APP_SECRET", "FB_VERIFY_TOKEN"))
        if not (token and secret and verify):
            return None
        return cls(service, token, secret, verify)

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/fb/webhook", self.verify)
        app.router.add_post("/fb/webhook", self.receive)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.session is not None and not self.session.closed:
            await self.session.close()

    # ---- webhook -----------------------------------------------------------------

    async def verify(self, request: web.Request) -> web.Response:
        """Meta's one-time check when the webhook is added in the dashboard."""
        q = request.query
        if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), self.verify_token):
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403, text="forbidden")

    def _signed(self, body: bytes, header: str) -> bool:
        if not header.startswith("sha256="):
            return False
        expected = hmac.new(self.app_secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(header[7:], expected)

    async def receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        if not self._signed(body, request.headers.get("X-Hub-Signature-256", "")):
            return web.Response(status=403, text="bad signature")
        try:
            data = json.loads(body)
        except ValueError:
            return web.Response(status=400, text="bad json")
        for entry in data.get("entry") or []:
            for event in entry.get("messaging") or []:
                self._spawn(self._event(event))
        return web.Response(text="EVENT_RECEIVED")

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _event(self, event: dict) -> None:
        sender = str((event.get("sender") or {}).get("id") or "")
        message = event.get("message") or {}
        if not sender or not message or message.get("is_echo"):
            return  # reads, deliveries, our own sent messages
        mid = str(message.get("mid") or "")
        if mid:
            if mid in self.seen:
                return
            self.seen.append(mid)
        inbound = cs.Inbound(channel=CHANNEL, chat_id=sender, text=str(message.get("text") or "").strip(),
                             display_name=f"Facebook 客人 {sender[-4:]}")
        try:
            if inbound.text:
                reply = await self.service.handle(inbound)
            elif message.get("attachments"):
                kind = str((message["attachments"][0] or {}).get("type") or "file")
                reply, _ = await self.service.handle_attachment(inbound, kind)
            else:
                return
            if reply:
                await self.send(sender, reply)
        except Exception:  # noqa: BLE001 - one bad message must not stop the others
            logger.exception("Facebook 消息处理失败（%s）", sender)

    # ---- sending -----------------------------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=20))
        payload = {"recipient": {"id": chat_id}, "messaging_type": "RESPONSE",
                   "message": {"text": text[:MAX_TEXT]}}
        async with self.session.post(f"{GRAPH_URL}/me/messages", params={"access_token": self.page_token},
                                     json=payload) as r:
            if r.status >= 400:
                detail = (await r.text())[:300]
                # Past 24 hours since the customer's last message Messenger refuses
                # ordinary replies; the owner sees this error in Telegram.
                raise RuntimeError(f"Facebook 发送失败（{r.status}）：{detail}")
