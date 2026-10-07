"""企业微信 (WeCom): the owner's self-built 企业微信 app gets the same AI customer
service as Telegram, the website and Facebook.

- Messages: whoever writes to the app gets an AI reply; owner replies from
  Telegram go back out the same way (service.deliver_owner_reply -> send).
- Files and pictures are recorded and passed to the owner, like on every channel.

WeCom calls our callback with each message, encrypted and signed. We answer
"success" at once and reply from a background task (WeCom retries anything
slower than 5 s, the AI is usually slower than that, so replies are sent
through the app message API instead of in the callback's response).

Setup, in the 企业微信 admin console (work.weixin.qq.com):
    1. 应用管理 > 自建 > 创建应用  ->  AgentId and Secret
    2. 我的企业 > 企业信息         ->  企业ID (corp id)
    3. 应用 > 接收消息 > 设置API接收: URL = https://<your domain>/wecom/callback,
       click "随机获取" for Token and EncodingAESKey, put them in the variables
       below, deploy, THEN click save (WeCom calls the URL to check it).
    4. 应用 > 企业可信IP: add this server's outbound IP, or sending is refused (60020).

Environment (all five are needed; without them the channel stays off):
    WECOM_CORP_ID      企业ID
    WECOM_AGENT_ID     the app's AgentId (a number)
    WECOM_SECRET       the app's Secret
    WECOM_TOKEN        the callback's Token
    WECOM_AES_KEY      the callback's EncodingAESKey (43 characters)
    WECOM_CALLBACK_URL the public callback URL, only used in the log
                       (default: PUBLIC_BASE_URL or RAILWAY_PUBLIC_DOMAIN + /wecom/callback)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import struct
import time
import xml.etree.ElementTree as ET
from collections import deque

from aiohttp import ClientSession, ClientTimeout, web

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:  # a missing library must only switch 企业微信 off, never stop the bot (see WXCrypt)
    Cipher = algorithms = modes = None

import customer_service as cs

logger = logging.getLogger(__name__)

CHANNEL = "wecom"
API_URL = os.environ.get("WECOM_API_URL", "https://qyapi.weixin.qq.com/cgi-bin").rstrip("/")
MAX_BYTES = 2000  # the API takes 2048 bytes of text per message
TOKEN_ERRORS = {40001, 40014, 42001}  # bad or expired access_token: fetch a new one and retry once
REPORT_EVERY = 600  # seconds between two "企业微信出错" notices to the owner
ATTACHMENT_LABELS = {"image": "图片", "voice": "语音", "video": "视频", "file": "文件"}
HINTS = {
    60020: "这台服务器的出口 IP 没有加到应用的「企业可信IP」里（应用 > 企业可信IP）",
    40013: "WECOM_CORP_ID 不对",
    40089: "WECOM_CORP_ID 或 WECOM_SECRET 不对",
    40091: "WECOM_SECRET 不对",
    301002: "应用没有权限，检查应用的可见范围",
    81013: "这个用户不在应用的可见范围内",
}
_ENCRYPT = re.compile(r"<Encrypt>\s*(?:<!\[CDATA\[(.*?)\]\]>|([^<]*))\s*</Encrypt>", re.S)


def default_callback_url() -> str:
    url = os.environ.get("WECOM_CALLBACK_URL", "").strip()
    if url:
        return url
    base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if base:
        return f"{base}/wecom/callback"
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}/wecom/callback" if domain else ""


# --------------------------------------------------------------------------- #
# The callback's encryption (Tencent's "企业微信消息加解密": AES-256-CBC, 32-byte PKCS#7)
# --------------------------------------------------------------------------- #

class WXCrypt:
    BLOCK = 32

    def __init__(self, token: str, aes_key: str, corp_id: str):
        if Cipher is None:
            raise ValueError("没装 cryptography 库（pip install -r requirements.txt）")
        try:
            key = base64.b64decode(aes_key + "=", validate=True)
        except ValueError:
            key = b""
        if len(aes_key) != 43 or len(key) != 32:
            raise ValueError("WECOM_AES_KEY 应该是 43 位的 EncodingAESKey")
        self.token, self.key, self.corp_id = token, key, corp_id
        self.iv = key[:16]

    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        return hashlib.sha1("".join(sorted([self.token, timestamp, nonce, encrypt])).encode()).hexdigest()

    def verify(self, signature: str, timestamp: str, nonce: str, encrypt: str) -> bool:
        if not (signature and timestamp and nonce and encrypt):
            return False
        return hmac.compare_digest(signature, self.signature(timestamp, nonce, encrypt))

    def decrypt(self, encrypt: str) -> str:
        """The plain message inside `encrypt`; raises ValueError for anything not ours."""
        try:
            raw = base64.b64decode(encrypt, validate=True)
            decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
            plain = decryptor.update(raw) + decryptor.finalize()
        except Exception as exc:  # noqa: BLE001 - wrong length, wrong characters: all the same to the caller
            raise ValueError(f"解密失败：{exc}") from exc
        pad = plain[-1] if plain else 0
        if not 1 <= pad <= self.BLOCK:
            raise ValueError("解密失败：填充不对（AES key 不匹配？）")
        content = plain[:-pad][16:]  # 16 random bytes first
        if len(content) < 4:
            raise ValueError("解密失败：内容太短")
        size = struct.unpack(">I", content[:4])[0]
        message, receiver = content[4:4 + size], content[4 + size:]
        if len(message) != size:
            raise ValueError("解密失败：长度对不上")
        if receiver.decode("utf-8", "replace") != self.corp_id:
            raise ValueError("这条消息不是发给这个企业ID的")
        return message.decode("utf-8")

    def encrypt(self, text: str) -> str:
        """The reverse of decrypt; the app itself never needs it, the tests do."""
        body = secrets.token_bytes(16) + struct.pack(">I", len(text.encode())) + text.encode() + self.corp_id.encode()
        pad = self.BLOCK - len(body) % self.BLOCK
        body += bytes([pad]) * pad
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        return base64.b64encode(encryptor.update(body) + encryptor.finalize()).decode()


def extract_encrypt(body: bytes) -> str:
    """The <Encrypt> value of a callback, without parsing the (not yet verified) XML."""
    found = _ENCRYPT.search(body.decode("utf-8", "replace"))
    return ((found.group(1) or found.group(2) or "") if found else "").strip()


def parse_message(xml: str) -> dict[str, str]:
    """The fields of a decrypted message. Only ever called on text that passed the signature check."""
    return {child.tag: (child.text or "").strip() for child in ET.fromstring(xml)}


def split_bytes(text: str, limit: int = MAX_BYTES) -> list[str]:
    """Pieces of at most `limit` UTF-8 bytes (WeCom counts bytes, not characters)."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in text.strip():
        width = len(char.encode("utf-8"))
        if size + width > limit:
            chunks.append("".join(current).strip())
            current, size = [], 0
        current.append(char)
        size += width
    chunks.append("".join(current).strip())
    return [c for c in chunks if c]


# --------------------------------------------------------------------------- #
# The channel
# --------------------------------------------------------------------------- #

class WeCom:
    def __init__(self, service: cs.CustomerService, corp_id: str, agent_id: int, secret: str, token: str,
                 aes_key: str, *, callback_url: str = "", session: ClientSession | None = None, notify=None):
        self.service = service
        self.corp_id = corp_id
        self.agent_id = agent_id
        self.secret = secret
        self.crypt = WXCrypt(token, aes_key, corp_id)
        self.callback_url = callback_url
        self.session = session
        self.notify = notify  # async (text) -> None, to tell the owner when something is wrong
        self.connected = False
        self.seen: deque[str] = deque(maxlen=1000)  # WeCom redelivers a message it got no answer for
        self.tasks: set[asyncio.Task] = set()
        self._access_token = ""
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._last_report = 0.0
        service.register_channel(CHANNEL, self.send)

    @classmethod
    def from_env(cls, service: cs.CustomerService, notify=None) -> "WeCom | None":
        names = ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_TOKEN", "WECOM_AES_KEY")
        env = {k: os.environ.get(k, "").strip() for k in names}
        if not all(env.values()):
            missing = [k for k, v in env.items() if not v]
            if len(missing) < len(names):  # started to fill it in: say what's left
                logger.warning("企业微信没有启用，还缺这些变量：%s", ", ".join(missing))
            return None
        try:
            agent_id = int(env["WECOM_AGENT_ID"])
        except ValueError:
            logger.error("企业微信没有启用：WECOM_AGENT_ID 要是数字（应用页上的 AgentId）")
            return None
        try:
            return cls(service, env["WECOM_CORP_ID"], agent_id, env["WECOM_SECRET"],
                       env["WECOM_TOKEN"], env["WECOM_AES_KEY"], callback_url=default_callback_url(), notify=notify)
        except ValueError as exc:  # a mistyped EncodingAESKey must not stop the bot from starting
            logger.error("企业微信没有启用：%s", exc)
            return None

    def routes(self, app: web.Application) -> None:
        app.router.add_get("/wecom/callback", self.verify)
        app.router.add_post("/wecom/callback", self.receive)

    async def close(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self.session is not None and not self.session.closed:
            await self.session.close()

    # ---- API ------------------------------------------------------------------------

    async def _call(self, method: str, path: str, *, params: dict | None = None, data: dict | None = None,
                    auth: bool = True, retry: bool = True) -> dict:
        params = dict(params or {})
        if auth:
            params["access_token"] = await self._token()
        if self.session is None or self.session.closed:
            self.session = ClientSession(timeout=ClientTimeout(total=30))
        async with self.session.request(method, f"{API_URL}/{path}", params=params, json=data) as r:
            status, text = r.status, await r.text()
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = {}
        code = int(body.get("errcode") or 0)
        if auth and retry and code in TOKEN_ERRORS:
            self._access_token = ""
            return await self._call(method, path, params=params, data=data, retry=False)
        if status >= 400 or code:
            hint = f"。{HINTS[code]}" if code in HINTS else ""
            raise RuntimeError(f"企业微信接口出错（{code or status}）：{body.get('errmsg') or text[:300]}{hint}")
        return body

    async def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at:
            return self._access_token
        async with self._token_lock:
            if self._access_token and time.time() < self._expires_at:
                return self._access_token
            body = await self._call("GET", "gettoken", auth=False,
                                    params={"corpid": self.corp_id, "corpsecret": self.secret})
            self._access_token = str(body["access_token"])
            self._expires_at = time.time() + int(body.get("expires_in") or 7200) - 300
            return self._access_token

    async def connect(self) -> bool:
        """Check the credentials once at startup. Errors are logged and sent to the owner, never raised."""
        try:
            await self._token()
        except Exception as exc:  # noqa: BLE001 - the rest of the bot keeps running
            logger.exception("企业微信连接失败")
            await self._report(exc)
            return False
        self.connected = True
        logger.info("企业微信已接上：应用 %s。回调地址：%s", self.agent_id,
                    self.callback_url or "（没有公网地址：设 PUBLIC_BASE_URL 或 WECOM_CALLBACK_URL）")
        return True

    async def _report(self, exc: BaseException) -> None:
        """Tell the owner something is wrong, at most once every REPORT_EVERY seconds."""
        if self.notify is None or time.time() - self._last_report < REPORT_EVERY:
            return
        self._last_report = time.time()
        try:
            await self.notify(f"⚠️ 企业微信出错了：{exc}")
        except Exception:  # noqa: BLE001
            logger.exception("企业微信的出错通知没发出去")

    # ---- callback ---------------------------------------------------------------------

    async def verify(self, request: web.Request) -> web.Response:
        """WeCom's check when the callback URL is saved: decrypt echostr and send it back."""
        q = request.query
        echostr = q.get("echostr", "")
        if not self.crypt.verify(q.get("msg_signature", ""), q.get("timestamp", ""), q.get("nonce", ""), echostr):
            return web.Response(status=403, text="forbidden")
        try:
            return web.Response(text=self.crypt.decrypt(echostr))
        except ValueError:
            logger.warning("企业微信回调验证：echostr 解不开，WECOM_AES_KEY 或 WECOM_CORP_ID 不对")
            return web.Response(status=403, text="forbidden")

    async def receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        q = request.query
        encrypt = extract_encrypt(body)
        if not self.crypt.verify(q.get("msg_signature", ""), q.get("timestamp", ""), q.get("nonce", ""), encrypt):
            return web.Response(status=403, text="bad signature")
        try:
            fields = parse_message(self.crypt.decrypt(encrypt))
        except (ValueError, ET.ParseError):
            logger.warning("企业微信消息解不开（WECOM_AES_KEY / WECOM_CORP_ID 不对？）")
            return web.Response(status=400, text="bad message")
        self._spawn(self._message(fields))
        return web.Response(text="success")

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _first_time(self, event_id: str) -> bool:
        if event_id in self.seen:
            return False
        self.seen.append(event_id)
        return True

    async def _message(self, fields: dict[str, str]) -> None:
        user, kind = fields.get("FromUserName", ""), fields.get("MsgType", "")
        if not user or kind == "event":  # someone opened the app, subscribed...: nothing to answer
            return
        if not self._first_time(fields.get("MsgId") or f"{user}:{fields.get('CreateTime', '')}:{kind}"):
            return
        text = fields.get("Content", "") if kind == "text" else ""
        inbound = cs.Inbound(channel=CHANNEL, chat_id=user, text=text, display_name=f"企业微信 {user}")
        try:
            if kind == "text":
                reply = await self.service.handle(inbound) if text else None
            else:
                reply, _ = await self.service.handle_attachment(inbound, ATTACHMENT_LABELS.get(kind, "附件"))
            if reply:
                await self.send(user, reply)
        except Exception as exc:  # noqa: BLE001 - one bad message must not stop the others
            logger.exception("企业微信消息处理失败（%s）", user)
            await self._report(exc)

    # ---- sending ----------------------------------------------------------------------

    async def send(self, chat_id: str, text: str) -> None:
        for chunk in split_bytes(text):
            body = await self._call("POST", "message/send", data={
                "touser": chat_id, "msgtype": "text", "agentid": self.agent_id, "text": {"content": chunk}})
            if body.get("invaliduser"):  # the API answers errcode 0 even when it delivered to nobody
                raise RuntimeError(f"企业微信用户 {body['invaliduser']} 不存在，或不在应用的可见范围内")
