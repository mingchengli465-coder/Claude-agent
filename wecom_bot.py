"""企业微信「智能机器人」(API 模式，长连接): the same AI customer service as Telegram, the website,
Facebook and the self-built app (wecom.py), without a callback URL.

Why this exists: a self-built app's callback URL must be on a domain whose ICP 备案 belongs to the
enterprise, and its outbound IP must be in the app's 企业可信IP. A smart robot in long-connection
mode needs neither: we open a WebSocket to WeCom and messages arrive on it.

    1. we connect to wss://openws.work.weixin.qq.com and send  aibot_subscribe {bot_id, secret}
    2. a message to the robot arrives as                       aibot_msg_callback (headers.req_id, body)
    3. we answer on the same req_id with                       aibot_respond_msg  (a "stream" message:
       a "正在输入…" placeholder at once, then the AI's reply with finish=true)
    4. owner replies from Telegram go out as                   aibot_send_msg {chatid, markdown}
    5. every 30 s we send                                       ping
WeCom acknowledges each frame we send with a frame carrying the same req_id and an errcode.
One robot keeps one connection: a new one (say, the next deploy) closes the old one.

Who can talk to it: members of the enterprise, one-to-one or by @-mentioning it in an internal
group. WeCom does not put smart robots in chats with external contacts or personal WeChat users.

Setup, in the 企业微信 admin console (work.weixin.qq.com):
    工作台 > 智能机器人 > 创建机器人 > API 模式 > 连接方式「长连接」 -> BotID and Secret

Environment (both are needed; without them the robot stays off):
    WECOM_BOT_ID       the robot's BotID
    WECOM_BOT_SECRET   the robot's Secret
    WECOM_BOT_WS_URL   only for tests (default wss://openws.work.weixin.qq.com)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
from collections import deque

from aiohttp import ClientSession, ClientTimeout, WSMsgType

import customer_service as cs
from wecom import split_bytes

logger = logging.getLogger(__name__)

CHANNEL = "wecombot"
WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT = 30          # seconds between two pings
MISSED_PINGS = 2        # pings without an answer before the connection counts as dead
ACK_TIMEOUT = 10        # seconds to wait for WeCom to acknowledge a frame
RETRY_MAX = 30          # longest wait between reconnects
AUTH_RETRY = 300        # wait after WeCom refuses the BotID / Secret
REPORT_EVERY = 600      # seconds between two "企业微信机器人出错" notices to the owner
SEND_BYTES = 4000       # one markdown message
THINKING = "正在输入…"
RECEIVED = "收到啦，稍后回复你～"
ATTACHMENT_LABELS = {"image": "图片", "file": "文件", "video": "视频"}
_MENTION = re.compile(r"^(?:@\S+\s*)+")


class AuthError(RuntimeError):
    """WeCom refused the BotID / Secret."""


def req_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}_{secrets.token_hex(4)}"


def message_text(body: dict) -> tuple[str, str]:
    """(text, attachment kind) of one message: the words the customer wrote (a voice message
    arrives already transcribed), or, for a picture or file without words, what it is."""
    kind = body.get("msgtype", "")
    if kind == "text":
        return (body.get("text") or {}).get("content", ""), ""
    if kind == "voice":
        return (body.get("voice") or {}).get("content", ""), ""
    if kind == "mixed":
        items = (body.get("mixed") or {}).get("msg_item") or []
        words = " ".join((i.get("text") or {}).get("content", "") for i in items if i.get("msgtype") == "text")
        return words, "" if words.strip() else "image"
    return "", kind if kind in ATTACHMENT_LABELS else ""


class WeComBot:
    def __init__(self, service: cs.CustomerService, bot_id: str, secret: str, *, url: str = WS_URL,
                 notify=None, session: ClientSession | None = None):
        self.service = service
        self.bot_id = bot_id
        self.secret = secret
        self.url = url
        self.notify = notify  # async (text) -> None, tells the owner when something is wrong
        self.session = session
        self.ws = None
        self.connected = False
        self.pending: dict[str, asyncio.Future] = {}
        self.send_lock = asyncio.Lock()
        self.seen: deque[str] = deque(maxlen=1000)
        self.tasks: set[asyncio.Task] = set()
        self.runner: asyncio.Task | None = None
        self._missed = 0
        self._last_report = 0.0
        service.register_channel(CHANNEL, self.send)

    @classmethod
    def from_env(cls, service: cs.CustomerService, notify=None) -> "WeComBot | None":
        bot_id = os.environ.get("WECOM_BOT_ID", "").strip()
        secret = os.environ.get("WECOM_BOT_SECRET", "").strip()
        if not (bot_id and secret):
            if bot_id or secret:
                logger.warning("企业微信机器人没有启用：WECOM_BOT_ID 和 WECOM_BOT_SECRET 要一起填")
            return None
        url = os.environ.get("WECOM_BOT_WS_URL", "").strip() or WS_URL
        return cls(service, bot_id, secret, url=url, notify=notify)

    # ---- lifecycle ------------------------------------------------------------------

    def start(self) -> None:
        self.runner = asyncio.get_running_loop().create_task(self.run())

    async def close(self) -> None:
        for task in [self.runner, *self.tasks]:
            if task is not None:
                task.cancel()
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self.session is not None and not self.session.closed:
            await self.session.close()

    async def run(self) -> None:
        """Stay connected: reconnect after any drop, waiting 1, 2, 4 … up to RETRY_MAX seconds."""
        failures = 0
        while True:
            try:
                await self._session_once()
                failures = 0  # it was up; a drop after that starts the backoff again
                logger.warning("企业微信机器人的连接断了，马上重连")
                delay = 1
            except asyncio.CancelledError:
                raise
            except AuthError as exc:
                logger.error("企业微信机器人认证失败：%s。检查 WECOM_BOT_ID / WECOM_BOT_SECRET", exc)
                await self._report(f"认证失败：{exc}。检查 WECOM_BOT_ID / WECOM_BOT_SECRET")
                delay = AUTH_RETRY
            except Exception as exc:  # noqa: BLE001 - the network: try again
                failures += 1
                delay = min(2 ** (failures - 1), RETRY_MAX)
                logger.warning("企业微信机器人连接失败（%s），%s 秒后重连", exc, delay)
                if failures == 5:
                    await self._report(f"连不上企业微信：{exc}")
            finally:
                self.connected = False
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("企业微信机器人的连接断了"))
                        future.exception()  # whoever waits still gets it; nobody waiting is fine too
                self.pending.clear()
            await asyncio.sleep(delay)

    async def _session_once(self) -> None:
        """One connection: subscribe, then read frames until it closes. Returns when it drops."""
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=None, connect=20))
        async with self.session.ws_connect(self.url, heartbeat=None, autoping=True) as ws:
            self.ws = ws
            await self._subscribe(ws)
            self.connected = True
            self._missed = 0
            logger.info("企业微信已接上：智能机器人长连接（BotID %s）", self.bot_id)
            pinger = asyncio.get_running_loop().create_task(self._heartbeat(ws))
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        self._frame(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        break
            finally:
                pinger.cancel()
                self.ws = None

    async def _subscribe(self, ws) -> None:
        rid = req_id("aibot_subscribe")
        await ws.send_str(json.dumps({"cmd": "aibot_subscribe", "headers": {"req_id": rid},
                                      "body": {"bot_id": self.bot_id, "secret": self.secret}}))
        while True:  # the answer to the subscription comes first
            msg = await asyncio.wait_for(ws.receive(), ACK_TIMEOUT)
            if msg.type != WSMsgType.TEXT:
                raise ConnectionError(f"企业微信在认证前断开了连接（{msg.type.name}）")
            frame = json.loads(msg.data)
            if (frame.get("headers") or {}).get("req_id") == rid:
                if frame.get("errcode"):
                    raise AuthError(f"{frame.get('errmsg') or ''}（errcode {frame.get('errcode')}）")
                return

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT)
            if self._missed >= MISSED_PINGS:
                logger.warning("企业微信机器人 %s 次心跳没有回应，重新连接", self._missed)
                await ws.close()
                return
            self._missed += 1
            await ws.send_str(json.dumps({"cmd": "ping", "headers": {"req_id": req_id("ping")}}))

    async def _report(self, text: str) -> None:
        """Tell the owner something is wrong, at most once every REPORT_EVERY seconds."""
        if self.notify is None or time.time() - self._last_report < REPORT_EVERY:
            return
        self._last_report = time.time()
        try:
            await self.notify(f"⚠️ 企业微信机器人：{text}")
        except Exception:  # noqa: BLE001
            logger.exception("企业微信机器人的出错通知没发出去")

    # ---- frames ---------------------------------------------------------------------

    def _frame(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            logger.warning("企业微信机器人收到看不懂的数据：%s", raw[:200])
            return
        cmd = frame.get("cmd")
        rid = (frame.get("headers") or {}).get("req_id", "")
        if cmd == "aibot_msg_callback":
            self._spawn(self._message(rid, frame.get("body") or {}))
        elif cmd == "aibot_event_callback":
            return  # someone opened the chat, clicked a card...: nothing to answer
        elif rid in self.pending:
            future = self.pending.pop(rid)
            if not future.done():
                future.set_result(frame)
        elif rid.startswith("ping"):
            self._missed = 0

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _request(self, cmd: str, rid: str, body: dict) -> dict:
        """Send one frame and wait for WeCom's acknowledgement (one at a time: replies to the
        same message must arrive in order)."""
        async with self.send_lock:
            ws = self.ws
            if ws is None or ws.closed or not self.connected:
                raise ConnectionError("企业微信机器人现在没连上")
            future = asyncio.get_running_loop().create_future()
            self.pending[rid] = future
            try:
                await ws.send_str(json.dumps({"cmd": cmd, "headers": {"req_id": rid}, "body": body},
                                             ensure_ascii=False))
                ack = await asyncio.wait_for(future, ACK_TIMEOUT)
            finally:
                self.pending.pop(rid, None)
        if ack.get("errcode"):
            raise RuntimeError(f"企业微信机器人发送失败：{ack.get('errmsg') or ''}（errcode {ack.get('errcode')}）")
        return ack

    async def _stream(self, rid: str, stream_id: str, content: str, finish: bool) -> None:
        await self._request("aibot_respond_msg", rid, {
            "msgtype": "stream", "stream": {"id": stream_id, "finish": finish, "content": content}})

    # ---- messages -------------------------------------------------------------------

    async def _message(self, rid: str, body: dict) -> None:
        user = (body.get("from") or {}).get("userid", "")
        msg_id = body.get("msgid", "")
        if not user or not rid or (msg_id and msg_id in self.seen):
            return
        if msg_id:
            self.seen.append(msg_id)
        group = body.get("chattype") == "group"
        chat = body.get("chatid", "") if group else user  # where the conversation is, and where replies go
        if not chat:
            return
        text, attachment = message_text(body)
        if group:
            text = _MENTION.sub("", text.strip())
        if not text.strip() and not attachment:
            return
        stream_id = req_id("stream")
        try:
            await self._stream(rid, stream_id, THINKING, False)
            inbound = cs.Inbound(channel=CHANNEL, chat_id=chat, text=text,
                                 display_name=f"企业微信 {user}" + ("（群聊）" if group else ""))
            if text.strip():
                reply = await self.service.handle(inbound)
            else:
                reply, _ = await self.service.handle_attachment(inbound, ATTACHMENT_LABELS[attachment])
            final = (reply or RECEIVED).strip()
            pieces = split_bytes(final, 20000)
            await self._stream(rid, stream_id, pieces[0] if pieces else RECEIVED, True)
            for extra in pieces[1:]:
                await self.send(chat, extra)
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the others
            logger.exception("企业微信机器人消息处理失败（%s）", chat)
            await self._report(f"一条消息没回成（{chat}）：{exc}")

    # ---- sending (owner replies from Telegram) -------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        for chunk in split_bytes(text, SEND_BYTES):
            await self._request("aibot_send_msg", req_id("aibot_send_msg"),
                                {"chatid": chat_id, "msgtype": "markdown", "markdown": {"content": chunk}})
