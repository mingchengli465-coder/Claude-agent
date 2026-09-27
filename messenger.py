"""Facebook: the owner's Facebook Page gets the same AI customer service as
Telegram and the website, and posts go out automatically.

- Messenger: customers who message the Page get AI replies; owner replies from
  Telegram go back out the same way.
- Comments: a new comment under a Page post gets an AI reply underneath it.
- Posts: each scheduled X post is also published on the Page (see bot.py).

Meta calls our webhook with each event; we answer 200 at once and reply from a
background task (Meta retries anything slower than ~20 s).

Setup needs as little of the owner as possible. From the Meta app they copy
the App ID, the App Secret and one User token (Graph API Explorer, with the
Page permissions ticked). connect() then swaps that for a Page token that
doesn't expire, subscribes the Page to messages and comments, and registers
our webhook with Meta, so nothing has to be typed into the dashboard.

Environment:
    FB_APP_ID, FB_APP_SECRET   from the Meta app's Settings > Basic
    FB_USER_TOKEN              a User token from Graph API Explorer (or set
                               FB_PAGE_TOKEN directly instead)
    FB_VERIFY_TOKEN            any string; the webhook's handshake secret
    FB_PAGE_ID                 which Page, if the owner runs several (optional)
    FB_CALLBACK_URL            the public webhook URL (default: from RAILWAY_PUBLIC_DOMAIN)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from collections import deque
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

import customer_service as cs

logger = logging.getLogger(__name__)

CHANNEL = "facebook"
COMMENT_CHANNEL = "fb_comment"
GRAPH_URL = os.environ.get("FB_GRAPH_URL", "https://graph.facebook.com/v21.0").rstrip("/")
MAX_TEXT = 2000  # Messenger's limit per message
FIELDS = "messages,feed"


def default_callback_url() -> str:
    url = os.environ.get("FB_CALLBACK_URL", "").strip()
    if url:
        return url
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}/fb/webhook" if domain else ""


class Messenger:
    def __init__(self, service: cs.CustomerService, app_secret: str, verify_token: str, *,
                 page_token: str = "", user_token: str = "", app_id: str = "", page_id: str = "",
                 callback_url: str = "", state_path: Path | str | None = None,
                 session: ClientSession | None = None, notify=None):
        self.service = service
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.page_token = page_token
        self.user_token = user_token
        self.app_id = app_id
        self.page_id = page_id
        self.page_name = ""
        self.callback_url = callback_url
        self.state_path = Path(state_path) if state_path else None
        self.session = session
        self.notify = notify  # async (text) -> None, to tell the owner once it's connected
        self.connected = False
        self.seen: deque[str] = deque(maxlen=1000)  # Meta sometimes delivers an event twice
        self.last_comment: dict[str, str] = {}  # commenter -> their latest comment, for replies
        self.tasks: set[asyncio.Task] = set()
        service.register_channel(CHANNEL, self.send)
        service.register_channel(COMMENT_CHANNEL, self.reply_comment)

    @classmethod
    def from_env(cls, service: cs.CustomerService, notify=None) -> "Messenger | None":
        env = {k: os.environ.get(k, "").strip() for k in (
            "FB_APP_SECRET", "FB_VERIFY_TOKEN", "FB_PAGE_TOKEN", "FB_USER_TOKEN", "FB_APP_ID", "FB_PAGE_ID")}
        if not (env["FB_APP_SECRET"] and env["FB_VERIFY_TOKEN"]):
            return None
        if not (env["FB_PAGE_TOKEN"] or (env["FB_USER_TOKEN"] and env["FB_APP_ID"])):
            return None
        return cls(service, env["FB_APP_SECRET"], env["FB_VERIFY_TOKEN"], page_token=env["FB_PAGE_TOKEN"],
                   user_token=env["FB_USER_TOKEN"], app_id=env["FB_APP_ID"], page_id=env["FB_PAGE_ID"],
                   callback_url=default_callback_url(),
                   state_path=Path(cs.CS_DB_PATH).with_name("fb_state.json"), notify=notify)

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/fb/webhook", self.verify)
        app.router.add_post("/fb/webhook", self.receive)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.session is not None and not self.session.closed:
            await self.session.close()

    # ---- Graph API -----------------------------------------------------------------

    async def _graph(self, method: str, path: str, params: dict | None = None, data: dict | None = None) -> dict:
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=30))
        async with self.session.request(method, f"{GRAPH_URL}/{path.lstrip('/')}", params=params, json=data) as r:
            text = await r.text()
            try:
                body = json.loads(text) if text else {}
            except ValueError:
                body = {}
            if r.status >= 400 or "error" in body:
                err = body.get("error") or {}
                raise RuntimeError(f"Facebook 接口出错（{r.status}）：{err.get('message') or text[:300]}")
            return body

    # ---- one-time setup, run after the web server is up ------------------------------

    def _token_fingerprint(self) -> str:
        return hashlib.sha256(self.user_token.encode()).hexdigest()[:16] if self.user_token else ""

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        state = {"page_token": self.page_token, "page_id": self.page_id, "page_name": self.page_name,
                 "user_token_fingerprint": self._token_fingerprint()}
        try:
            self.state_path.write_text(json.dumps(state), encoding="utf-8")
        except OSError:
            logger.warning("Facebook 口令存不进 %s，下次重启会重新换一次", self.state_path)

    async def connect(self) -> bool:
        """Page token, Page subscription, webhook registration. Errors are logged, never raised."""
        try:
            fresh = False
            if not self.page_token:
                state = self._load_state()
                if state.get("page_token") and state.get("user_token_fingerprint") == self._token_fingerprint():
                    self.page_token, self.page_id = state["page_token"], state.get("page_id", "")
                    self.page_name = state.get("page_name", "")
                else:
                    await self._exchange_user_token()
                    fresh = True
            if not self.page_id:
                me = await self._graph("GET", "me", {"fields": "id,name", "access_token": self.page_token})
                self.page_id, self.page_name = str(me.get("id", "")), str(me.get("name", ""))
            self._save_state()
            await self._graph("POST", f"{self.page_id}/subscribed_apps",
                              {"subscribed_fields": FIELDS, "access_token": self.page_token})
            if self.app_id and self.callback_url:
                await self._graph("POST", f"{self.app_id}/subscriptions", {
                    "object": "page", "callback_url": self.callback_url, "verify_token": self.verify_token,
                    "fields": FIELDS, "include_values": "true", "access_token": f"{self.app_id}|{self.app_secret}"})
            self.connected = True
            logger.info("Facebook 已接上：专页 %s（%s），私信、评论、发帖都开了", self.page_name, self.page_id)
            if fresh and self.notify is not None:
                await self.notify(f"📘 Facebook 接好了：专页「{self.page_name}」\n"
                                  "以后 X 每次发帖会同时发到专页；专页的私信和评论 AI 会自动回复。")
            return True
        except Exception as exc:  # noqa: BLE001 - the rest of the bot keeps running
            logger.exception("Facebook 连接失败")
            if self.notify is not None:
                try:
                    await self.notify(f"⚠️ Facebook 没接上：{exc}")
                except Exception:  # noqa: BLE001
                    logger.exception("连 Facebook 失败的通知也没发出去")
            return False

    async def _exchange_user_token(self) -> None:
        """Short-lived User token -> long-lived one -> the Page's token, which then never expires."""
        long = await self._graph("GET", "oauth/access_token", {
            "grant_type": "fb_exchange_token", "client_id": self.app_id,
            "client_secret": self.app_secret, "fb_exchange_token": self.user_token})
        pages = await self._graph("GET", "me/accounts", {
            "fields": "id,name,access_token", "access_token": long["access_token"]})
        pages = pages.get("data") or []
        if self.page_id:
            pages = [p for p in pages if str(p.get("id")) == self.page_id]
        if not pages:
            raise RuntimeError("这个口令下面找不到专页：生成口令时要勾选你的专页，并给 pages_show_list 等权限")
        page = pages[0]
        self.page_token, self.page_id, self.page_name = page["access_token"], str(page["id"]), page.get("name", "")

    # ---- webhook -----------------------------------------------------------------

    async def verify(self, request: web.Request) -> web.Response:
        """Meta's handshake when the webhook is registered."""
        q = request.query
        if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""), self.verify_token):
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403, text="forbidden")

    def _signed(self, body: bytes, header: str) -> bool:
        if not header.startswith("sha256="):
            return False
        expected = hmac.new(self.app_secret.encode(), body, hashlib.sha256).hexdigest()
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
                self._spawn(self._message(event))
            for change in entry.get("changes") or []:
                if change.get("field") == "feed":
                    self._spawn(self._comment(change.get("value") or {}))
        return web.Response(text="EVENT_RECEIVED")

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _first_time(self, event_id: str) -> bool:
        if not event_id:
            return True
        if event_id in self.seen:
            return False
        self.seen.append(event_id)
        return True

    async def _message(self, event: dict) -> None:
        sender = str((event.get("sender") or {}).get("id") or "")
        message = event.get("message") or {}
        if not sender or not message or message.get("is_echo") or sender == self.page_id:
            return  # reads, deliveries, our own sent messages
        if not self._first_time(str(message.get("mid") or "")):
            return
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
            logger.exception("Facebook 私信处理失败（%s）", sender)

    async def _comment(self, value: dict) -> None:
        if value.get("item") != "comment" or value.get("verb") != "add":
            return  # likes, edits, deletions, new posts
        who = value.get("from") or {}
        author, comment_id = str(who.get("id") or ""), str(value.get("comment_id") or "")
        text = str(value.get("message") or "").strip()
        if not author or not comment_id or not text or author == self.page_id:
            return  # our own replies come back as comments too
        if not self._first_time(comment_id):
            return
        self.last_comment[author] = comment_id
        inbound = cs.Inbound(channel=COMMENT_CHANNEL, chat_id=author, text=text,
                             display_name=str(who.get("name") or f"Facebook 评论 {author[-4:]}"))
        try:
            reply = await self.service.handle(inbound)
            if reply:
                await self.reply_comment(author, reply)
        except Exception:  # noqa: BLE001
            logger.exception("Facebook 评论处理失败（%s）", comment_id)

    # ---- sending -----------------------------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        # Past 24 hours since the customer's last message Messenger refuses
        # ordinary replies; the owner then sees the error in Telegram.
        await self._graph("POST", "me/messages", {"access_token": self.page_token}, {
            "recipient": {"id": chat_id}, "messaging_type": "RESPONSE", "message": {"text": text[:MAX_TEXT]}})

    async def reply_comment(self, author: str, text: str) -> None:
        comment_id = self.last_comment.get(author)
        if not comment_id:
            raise RuntimeError("找不到这位客人最近的评论（服务器重启过），请到 Facebook 上直接回复")
        await self._graph("POST", f"{comment_id}/comments", {"access_token": self.page_token}, {"message": text[:MAX_TEXT]})

    async def post(self, text: str) -> str:
        """Publish on the Page; returns the post's link."""
        if not self.connected:
            raise RuntimeError("Facebook 还没接上")
        body = await self._graph("POST", f"{self.page_id}/feed", {"access_token": self.page_token}, {"message": text})
        post_id = str(body.get("id", ""))
        return f"https://www.facebook.com/{post_id}" if post_id else ""
