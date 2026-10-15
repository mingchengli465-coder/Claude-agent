"""WhatsApp: customers who message the owner's WhatsApp Business number get the same AI customer
service as Telegram and the website, and the owner's replies from Telegram go back out the same way.

Only WhatsApp's official Business Platform (the Cloud API) may be run by a program: a personal
WhatsApp account driven by a bot breaks WhatsApp's terms and gets banned. So the bot has a number of
its own, added to a Meta app (WhatsApp > API Setup), and Meta calls our webhook with each message.
We answer 200 at once and reply from a background task (Meta retries anything slow). Replies are
allowed, and free, within 24 hours of the customer's last message, which is when the AI writes.

Environment (all from the Meta app):
    WHATSAPP_TOKEN            a permanent token (Business Settings > System users), or the 24-hour
                              test token from WhatsApp > API Setup to try it out
    WHATSAPP_PHONE_NUMBER_ID  the number's ID from API Setup (an ID, not the phone number)
    WHATSAPP_APP_SECRET       Settings > Basic > App Secret: proves a webhook call comes from Meta
                              (FB_APP_SECRET is used when this isn't set)
    WHATSAPP_VERIFY_TOKEN     any string; typed into the webhook settings as well

The webhook to give Meta: https://<this server>/wa/webhook, subscribed to the "messages" field.
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

CHANNEL = "whatsapp"
PATH = "/wa/webhook"
GRAPH_URL = os.environ.get("WHATSAPP_GRAPH_URL", "https://graph.facebook.com/v23.0").rstrip("/")
MAX_TEXT = 4096  # WhatsApp's limit for one text message
# what a customer can send besides text, named as in the owner's transcript (like Telegram's)
ATTACHMENTS = {"image": "图片", "sticker": "贴图", "video": "视频", "audio": "语音", "voice": "语音",
               "document": "文件", "location": "位置", "contacts": "名片"}
# Meta's error for a reply more than 24 hours after the customer's last message
WINDOW_CLOSED = 131047


class WhatsApp:
    def __init__(self, service: cs.CustomerService, token: str, phone_number_id: str, app_secret: str,
                 verify_token: str, *, state_path: Path | str | None = None, session: ClientSession | None = None,
                 notify=None):
        self.service = service
        self.token = token
        self.phone_number_id = phone_number_id
        self.app_secret = app_secret
        self.verify_token = verify_token
        self.state_path = Path(state_path) if state_path else None
        self.session = session
        self.notify = notify  # async (text) -> None: tell the owner once the number is live
        self.number = ""
        self.seen: deque[str] = deque(maxlen=1000)  # Meta sometimes delivers a message twice
        self.tasks: set[asyncio.Task] = set()
        service.register_channel(CHANNEL, self.send)

    @classmethod
    def from_env(cls, service: cs.CustomerService, notify=None) -> "WhatsApp | None":
        env = {k: os.environ.get(k, "").strip() for k in (
            "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN", "FB_APP_SECRET")}
        secret = env["WHATSAPP_APP_SECRET"] or env["FB_APP_SECRET"]
        if not (env["WHATSAPP_TOKEN"] and env["WHATSAPP_PHONE_NUMBER_ID"] and secret and env["WHATSAPP_VERIFY_TOKEN"]):
            return None
        return cls(service, env["WHATSAPP_TOKEN"], env["WHATSAPP_PHONE_NUMBER_ID"], secret, env["WHATSAPP_VERIFY_TOKEN"],
                   state_path=Path(cs.CS_DB_PATH).with_name("wa_state.json"), notify=notify)

    def routes(self, app: web.Application) -> None:
        app.router.add_get(PATH, self.verify)
        app.router.add_post(PATH, self.receive)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.session is not None and not self.session.closed:
            await self.session.close()

    # ---- Graph API -----------------------------------------------------------------

    async def _graph(self, method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=30))
        async with self.session.request(method, f"{GRAPH_URL}/{path.lstrip('/')}", json=data, params=params,
                                        headers={"Authorization": f"Bearer {self.token}"}) as r:
            text = await r.text()
            try:
                body = json.loads(text) if text else {}
            except ValueError:
                body = {}
            if r.status >= 400 or "error" in body:
                err = body.get("error") or {}
                if err.get("code") == WINDOW_CLOSED:
                    raise RuntimeError("这位客人最后一条消息已经超过 24 小时，WhatsApp 不允许再直接回复；"
                                       "请用你自己的 WhatsApp 联系他")
                raise RuntimeError(f"WhatsApp 接口出错（{r.status}）：{err.get('message') or text[:300]}")
            return body

    async def connect(self) -> bool:
        """Check the token and the number once after a start. Errors are logged, never raised."""
        try:
            info = await self._graph("GET", self.phone_number_id, params={"fields": "display_phone_number,verified_name"})
            self.number = str(info.get("display_phone_number") or "")
            name = str(info.get("verified_name") or "")
            logger.info("WhatsApp 已接上：%s（%s）", self.number, name)
            if self.notify is not None and self._first_connect():
                await self.notify(f"🟢 WhatsApp 接好了：{self.number}（{name}）\n"
                                  "客人给这个号码发 WhatsApp，AI 会自动回复；要下单、砍价时转给你，回复通知就能回他。")
            return True
        except Exception as exc:  # noqa: BLE001 - the rest of the bot keeps running
            logger.exception("WhatsApp 连接失败")
            if self.notify is not None:
                try:
                    await self.notify(f"⚠️ WhatsApp 没接上：{exc}")
                except Exception:  # noqa: BLE001
                    logger.exception("连 WhatsApp 失败的通知也没发出去")
            return False

    def _first_connect(self) -> bool:
        """True the first time this number connects, so a restart doesn't announce it again."""
        if self.state_path is None:
            return True
        try:
            if json.loads(self.state_path.read_text(encoding="utf-8")).get("phone_number_id") == self.phone_number_id:
                return False
        except (OSError, ValueError):
            pass
        try:
            self.state_path.write_text(json.dumps({"phone_number_id": self.phone_number_id}), encoding="utf-8")
        except OSError:
            logger.warning("WhatsApp 的状态存不进 %s", self.state_path)
        return True

    # ---- webhook -----------------------------------------------------------------

    async def verify(self, request: web.Request) -> web.Response:
        """Meta's handshake when the webhook is saved in the app."""
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
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                if change.get("field") != "messages":
                    continue
                if str((value.get("metadata") or {}).get("phone_number_id") or "") != self.phone_number_id:
                    continue  # another number in the same WhatsApp account
                names = {str(c.get("wa_id")): str((c.get("profile") or {}).get("name") or "")
                         for c in value.get("contacts") or []}
                for message in value.get("messages") or []:  # "statuses" (sent, delivered, read) are skipped
                    self._spawn(self._message(message, names))
        return web.Response(text="EVENT_RECEIVED")

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _first_time(self, message_id: str) -> bool:
        if not message_id:
            return True
        if message_id in self.seen:
            return False
        self.seen.append(message_id)
        return True

    @staticmethod
    def _text(message: dict) -> str:
        kind = message.get("type")
        if kind == "text":
            return str((message.get("text") or {}).get("body") or "")
        if kind == "button":
            return str((message.get("button") or {}).get("text") or "")
        if kind == "interactive":
            reply = (message.get("interactive") or {})
            chosen = reply.get("button_reply") or reply.get("list_reply") or {}
            return str(chosen.get("title") or "")
        return ""

    async def _message(self, message: dict, names: dict[str, str]) -> None:
        sender = str(message.get("from") or "")
        if not sender or not self._first_time(str(message.get("id") or "")):
            return
        name = names.get(sender) or "WhatsApp 客人"
        inbound = cs.Inbound(channel=CHANNEL, chat_id=sender, text=self._text(message).strip(),
                             display_name=f"{name}（+{sender}）")
        try:
            if inbound.text:
                reply = await self.service.handle(inbound)
            elif message.get("type") in ATTACHMENTS:
                reply, _ = await self.service.handle_attachment(inbound, ATTACHMENTS[message["type"]])
            else:
                return  # reactions, deleted or unsupported messages
            if reply:
                await self.send(sender, reply)
        except Exception:  # noqa: BLE001 - one bad message must not stop the others
            logger.exception("WhatsApp 消息处理失败（%s）", sender)

    # ---- sending -----------------------------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        await self._graph("POST", f"{self.phone_number_id}/messages", {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": chat_id,
            "type": "text", "text": {"preview_url": False, "body": text[:MAX_TEXT]}})
