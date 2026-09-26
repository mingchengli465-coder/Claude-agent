"""Customer-service mode: answers customers from products.yaml and hands off to the owner.

Nothing in here knows about Telegram. A messaging channel plugs in with three
things, which is all a future 企业微信 entry needs:

    service.register_channel("wecom", send)      # send(chat_id, text) -> awaitable
    reply = await service.handle(Inbound(channel="wecom", chat_id=..., text=...))
    if reply: await send(chat_id, reply)

The owner is always reached through the one `owner_notify` callback given to
CustomerService (Telegram today), whichever channel the customer came from, and
their replies come back through `deliver_owner_reply`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Protocol
from zoneinfo import ZoneInfo

import yaml

logger = logging.getLogger(__name__)

CS_MODEL = os.environ.get("CS_MODEL", "claude-opus-5")
# DeepSeek is used when there's no Anthropic key: it can be topped up with Alipay.
# deepseek-chat / deepseek-reasoner were retired in July 2026; deepseek-flash replaced them.
CS_DEEPSEEK_MODEL = os.environ.get("CS_DEEPSEEK_MODEL", "deepseek-flash")
CS_DEEPSEEK_BASE_URL = os.environ.get("CS_DEEPSEEK_BASE_URL", "https://api.deepseek.com")
CS_EFFORT = os.environ.get("CS_EFFORT", "low")
CS_PRODUCTS_PATH = Path(os.environ.get("CS_PRODUCTS_PATH", Path(__file__).parent / "products.yaml"))
CS_DB_PATH = Path(os.environ.get("CS_DB_PATH", "cs.sqlite3"))
CS_HISTORY = int(os.environ.get("CS_HISTORY", "10"))
# A customer waiting longer than this gets handed to the owner instead.
CS_REQUEST_TIMEOUT = float(os.environ.get("CS_REQUEST_TIMEOUT", "60"))
CS_RATE_LIMIT = int(os.environ.get("CS_RATE_LIMIT", "5"))
CS_RATE_WINDOW = float(os.environ.get("CS_RATE_WINDOW", "60"))
CS_TIMEZONE = ZoneInfo(os.environ.get("CS_TIMEZONE", "Asia/Shanghai"))
# A customer silent this long (hours) counts as new again: the owner hears about
# them once more, and their purchase intent starts over.
CS_NEW_SESSION_GAP = float(os.environ.get("CS_NEW_SESSION_GAP_HOURS", "6")) * 3600
# Tell the owner when someone starts chatting / looks ready to buy, even while
# the AI is handling it fine. "false" turns either off.
CS_NOTIFY_NEW = os.environ.get("CS_NOTIFY_NEW", "true").lower() != "false"
CS_NOTIFY_INTENT = os.environ.get("CS_NOTIFY_INTENT", "true").lower() != "false"

HANDOFF_TEXT = "我请本人来跟你确认，稍等哦"
RATE_LIMITED_TEXT = "消息有点多啦 🙏 我一分钟最多回 {limit} 条，稍等一下再发哦～"
ATTACHMENT_TEXT = "收到文件啦～我转给本人看一下，稍等哦"
AI_ERROR_TEXT = HANDOFF_TEXT

# The model answers in the customer's own language; these fixed lines only come
# in Chinese and English, so anyone not writing Chinese gets English.
CANNED = {
    "zh": {"handoff": HANDOFF_TEXT, "rate_limited": RATE_LIMITED_TEXT, "attachment": ATTACHMENT_TEXT},
    "en": {
        "handoff": "Let me get the owner to confirm this with you, one moment please.",
        "rate_limited": "That's a lot of messages 🙏 I can reply to {limit} a minute, please give me a moment.",
        "attachment": "Got your file! I'll pass it to the owner, one moment please.",
    },
}
_CJK = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_LETTERS = re.compile(r"[^\W\d_]")


def detect_lang(text: str) -> str:
    """'zh' for Chinese, 'en' for any other written language, '' when there's
    nothing to go on (only emoji, numbers, punctuation)."""
    if _CJK.search(text or ""):
        return "zh"
    if _LETTERS.search(text or ""):
        return "en"
    return ""
# How often a customer may see the "no AI right now" reply, so a burst of
# messages doesn't get the same line back each time.
NO_AI_REPLY_EVERY = 600

PLACEHOLDER = "待填"

HANDOFF_REASONS = {
    "order_or_payment": "要下单/付款",
    "bargain": "砍价",
    "complex": "需求复杂",
    "out_of_scope": "超出资料范围",
    "wants_human": "要求找真人",
    "attachment": "发来文件",
    "ai_error": "AI 出错",
    "ai_unavailable": "AI 未启用",
    "ai_paused": "AI 已对这位客户暂停",
}

# How keen the customer is to buy, as judged by the model. Only ever rises
# within a conversation; each rise tells the owner.
INTENT_LEVELS = {"": 0, "interested": 1, "ready": 2}
INTENT_LABELS = {1: "有购买意向", 2: "准备购买"}

CHANNEL_LABELS = {"telegram": "Telegram", "web": "网站聊天窗口"}

# A safety net under the model's own judgement: these always go to the owner.
_HANDOFF_KEYWORDS = [
    ("wants_human", re.compile(
        r"真人|人工|转人工|找本人|本人在吗|老板在吗|机器人吗|是AI吗|是 AI 吗"
        r"|\b(real person|human|talk to (the )?(owner|someone|a person)|are you (a )?(bot|robot|ai))\b", re.I)),
    ("order_or_payment", re.compile(
        r"付款|付钱|转账|怎么付|下单|拍下|定金|订金|收款码|发票"
        r"|\b(pay|payment|paying|deposit|invoice|checkout|place an order|i want to order|how do i order)\b", re.I)),
    ("bargain", re.compile(
        r"便宜点|便宜些|便宜一点|优惠|打折|砍价|少点|少一点|能不能少"
        r"|\b(discount|cheaper|lower (the )?price|better price|best price|negotiat\w*)\b", re.I)),
]


# --------------------------------------------------------------------------- #
# Data shapes
# --------------------------------------------------------------------------- #

@dataclass
class Inbound:
    channel: str
    chat_id: str
    text: str
    username: str = ""
    display_name: str = ""
    # The channel's guess at the customer's language ("zh"/"en"), e.g. from their
    # app settings. Only used when the message itself doesn't show it.
    lang: str = ""


@dataclass
class Decision:
    """What the model decided for one customer message."""
    reply: str
    handoff: bool = False
    reason: str = ""
    summary: str = ""
    intent: str = ""


class Responder(Protocol):
    async def __call__(self, system: str, messages: list[dict]) -> Decision: ...


OwnerNotify = Callable[[str], Awaitable["int | None"]]
ChannelSend = Callable[[str, str], Awaitable[None]]


# --------------------------------------------------------------------------- #
# Business data
# --------------------------------------------------------------------------- #

def load_catalog(path: Path = CS_PRODUCTS_PATH) -> str:
    """products.yaml as the text the model reads. Comments are dropped on purpose,
    so examples written as comments never reach the model as if they were real."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
    blanks = text.count(PLACEHOLDER)
    if blanks:
        logger.warning("products.yaml 还有 %d 处「%s」，客户问到这些会直接转给你本人", blanks, PLACEHOLDER)
    return text


SYSTEM_TEMPLATE = """你是一位博主的客服助理，帮博主本人接待来咨询 AI 相关服务的客户。客户可能来自小红书、X、Telegram 或博主的个人网站。

【说话方式】
- 用客户最近一条消息的语言回复：客户写简体中文就用简体，写繁体中文就用繁体，写英文就用英文，写其他语言就用那种语言。
- 业务资料是中文的，用客户的语言转述，意思不能变。价格照原数字说，并注明是人民币（CNY / RMB）。
- 亲切、自然、像博主本人的助理在私信里聊天，不要像官方客服。句子短，可以偶尔用一个 emoji，不要每句都用。
- 你是助理，不是博主本人。被问到是不是真人/机器人时如实说你是助理，并转给本人。

【只能依据下面的业务资料回答】
- 价格、交付周期、付款方式、修改次数、能做什么、不接什么，全部只看业务资料，不许编造、不许估算、不许给资料以外的数字或承诺。
- 资料里写着「{placeholder}」的字段表示还没定。客户问到这些，不要猜，直接转给本人。
- 对话里以「【本人回复】」开头的内容是博主本人亲自说的，可以作为依据。
- 不承诺资料以外的内容或时间（比如"今晚就能好""保证满意""肯定能过"）。

【问价格、时间：自己回答，不要转给本人】
- 资料里写了的价格、交付周期、修改次数、能做什么，直接照资料回答。这是你最重要的工作，不许因为这些问题转给本人。
- 价格分几种情况（比如国内商户 / 海外商户）而客户没说是哪种时，把几种都报出来，再顺便问一句他是哪种。
- 客户说得太短或看不懂（比如只发了"?"、"在吗"、"hi"），就简单打个招呼，问他想了解哪项服务，或者把上一条回答换个说法再讲一遍。不要转给本人。
- 客户要本人的联系方式（微信、Telegram），直接把资料里「联系本人」的内容给他。

【主动问清需求】
客户需求没说清楚时，一次问一两个问题，帮本人收集报价需要的信息：
1. 要做什么（哪项服务、具体内容、页数/篇数/功能）
2. 截止时间
3. 预算
再参考资料里该服务的「需要客户提供」。已经知道的不要重复问。

【必须转给本人的情况】（handoff=true）
- order_or_payment：客户要下单、付款、问怎么付钱、要定金/发票
- bargain：客户砍价、问能不能便宜/优惠
- complex：需求复杂、要定制报价，资料里的说明不足以回答（能按资料报价的不算）
- out_of_scope：问到资料没写或写着「{placeholder}」的内容，或超出服务范围
- wants_human：客户要求找真人/本人
转给本人时，reply 留空即可，系统会自动回复客户。

【summary】
每次都更新一句话的需求摘要，格式：做什么｜截止时间｜预算。不知道的写"未说明"。

【intent：客户有多想买】
看整段对话判断，每次都填：
- ""：只是随便问问、打听一下
- "interested"：认真在考虑，比如问了具体价格、交付时间、要提供什么，或者说了自己的具体需求
- "ready"：明确想买/想开始，比如"那就做这个吧""怎么开始""我要了""什么时候能开工"

【业务资料 products.yaml】
{catalog}"""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "发给客户的话；handoff 为 true 时可留空"},
        "handoff": {"type": "boolean"},
        "reason": {
            "type": "string",
            "enum": ["", "order_or_payment", "bargain", "complex", "out_of_scope", "wants_human"],
        },
        "summary": {"type": "string", "description": "做什么｜截止时间｜预算"},
        "intent": {"type": "string", "enum": ["", "interested", "ready"]},
    },
    "required": ["reply", "handoff", "reason", "summary", "intent"],
    "additionalProperties": False,
}


def build_system_prompt(catalog: str) -> str:
    return SYSTEM_TEMPLATE.format(placeholder=PLACEHOLDER, catalog=catalog)


class ClaudeResponder:
    """Calls the Claude API with a structured-output schema, so every reply is parseable."""

    def __init__(self, api_key: str, model: str = CS_MODEL, effort: str = CS_EFFORT):
        import anthropic  # only needed when customer service is switched on

        self._anthropic = anthropic
        self.client = anthropic.AsyncAnthropic(api_key=api_key, timeout=60.0)
        self.model = model
        self.effort = effort

    async def __call__(self, system: str, messages: list[dict]) -> Decision:
        response = await asyncio.wait_for(self._request(system, messages), CS_REQUEST_TIMEOUT)
        return self._decision(response)

    async def _request(self, system: str, messages: list[dict]):
        return await self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            # The catalog is the same on every call, so cache it.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
            },
            # A declined request is re-run on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def _decision(self, response) -> Decision:
        if response.stop_reason == "refusal":
            logger.warning("Claude 拒绝回答（%s），转给本人", getattr(response.stop_details, "category", None))
            return Decision(reply="", handoff=True, reason="out_of_scope")
        if response.stop_reason == "max_tokens":
            raise RuntimeError("Claude 回复被截断（max_tokens）")
        text = next((b.text for b in response.content if b.type == "text"), "")
        data = json.loads(text)
        return Decision(
            reply=str(data.get("reply") or "").strip(),
            handoff=bool(data.get("handoff")),
            reason=str(data.get("reason") or ""),
            summary=str(data.get("summary") or "").strip(),
            intent=_intent(data.get("intent")),
        )


def _intent(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in INTENT_LEVELS else ""


# Models without schema-enforced output are told the shape in words, with an
# example; json_object mode then keeps the reply to a single JSON object.
JSON_FORMAT_SUFFIX = """

【输出格式】
只输出一个 JSON 对象，不要任何其他文字，不要用 ``` 包起来。字段：
- reply：发给客户的话（字符串）；handoff 为 true 时写空字符串 ""
- handoff：要不要转给本人（true / false）
- reason：转给本人的原因，只能是 "order_or_payment"、"bargain"、"complex"、"out_of_scope"、"wants_human" 之一；不转就写 ""
- summary：需求摘要，格式「做什么｜截止时间｜预算」，不知道的写"未说明"
- intent：客户有多想买，只能是 ""、"interested"、"ready" 之一

示例（内容不要照抄）：
{"reply": "可以的～想做几页、什么时候要呀？", "handoff": false, "reason": "", "summary": "课程汇报PPT｜未说明｜未说明", "intent": "interested"}"""


def parse_decision(text: str) -> Decision:
    """A Decision from a model's JSON reply; raises on anything unusable, which
    CustomerService turns into a handoff rather than a bad message to a customer."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"模型没有返回 JSON：{text[:200]!r}")
        data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError(f"模型返回的不是 JSON 对象：{text[:200]!r}")
    reason = str(data.get("reason") or "")
    if reason not in DECISION_SCHEMA["properties"]["reason"]["enum"]:
        reason = ""
    handoff = data.get("handoff")
    if isinstance(handoff, str):
        handoff = handoff.strip().lower() in ("true", "1", "yes", "是")
    return Decision(
        reply=str(data.get("reply") or "").strip(),
        handoff=bool(handoff),
        reason=reason,
        summary=str(data.get("summary") or "").strip(),
        intent=_intent(data.get("intent")),
    )


class OpenAICompatibleResponder:
    """DeepSeek (or any OpenAI-compatible API) with JSON mode.

    DeepSeek's JSON mode sometimes comes back with an empty message. Rather than
    hand every such customer to the owner, a failed JSON-mode call is retried
    once as a plain chat call, and a plain answer that isn't JSON is used as the
    reply itself (the keyword safety net in CustomerService still applies)."""

    def __init__(self, api_key: str, model: str = CS_DEEPSEEK_MODEL, base_url: str = CS_DEEPSEEK_BASE_URL):
        from openai import AsyncOpenAI  # only needed when this provider is chosen

        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=60.0, max_retries=1)
        self.model = model
        # Set once JSON mode has come back empty: later messages skip straight to
        # the plain call instead of paying for a wasted round trip each time.
        self.json_mode_broken = False

    async def _ask(self, system: str, messages: list[dict], json_mode: bool) -> str:
        extra = {"response_format": {"type": "json_object"}} if json_mode else {}
        response = await asyncio.wait_for(self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system + JSON_FORMAT_SUFFIX}] + messages,
            temperature=0.3,
            # Room for a model that thinks before answering; the reply itself is short.
            max_tokens=4000,
            **extra,
        ), CS_REQUEST_TIMEOUT)
        if not response.choices:
            raise RuntimeError("模型返回了空的 choices")
        choice = response.choices[0]
        if getattr(choice, "finish_reason", "") == "length":
            raise RuntimeError("模型回复被截断（max_tokens）")
        return (choice.message.content or "").strip()

    async def __call__(self, system: str, messages: list[dict]) -> Decision:
        if not getattr(self, "json_mode_broken", False):
            try:
                return parse_decision(await self._ask(system, messages, json_mode=True))
            except ValueError as exc:  # empty or not JSON; timeouts and API errors go up as before
                self.json_mode_broken = True
                logger.warning("JSON 模式没拿到可用的回复（%s），之后都直接用普通模式", str(exc)[:120])
        text = await self._ask(system, messages, json_mode=False)
        try:
            return parse_decision(text)
        except ValueError:
            if not text:
                raise
            reply = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
            logger.warning("模型没按 JSON 格式回答，直接用它的原话回复客户：%r", reply[:80])
            return Decision(reply=reply[:1500])


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

class Store:
    """Conversations per (channel, chat_id), in SQLite."""

    def __init__(self, path: Path | str = CS_DB_PATH):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                username TEXT DEFAULT '',
                display_name TEXT DEFAULT '',
                ai_enabled INTEGER DEFAULT 1,
                summary TEXT DEFAULT '',
                first_seen REAL,
                last_message_at REAL,
                last_ai_off_reply REAL DEFAULT 0,
                lang TEXT DEFAULT '',
                intent INTEGER DEFAULT 0,
                PRIMARY KEY (channel, chat_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('customer', 'assistant', 'owner')),
                text TEXT NOT NULL,
                ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS messages_by_chat ON messages (channel, chat_id, id);
            -- Which customer an owner-side message is about, so a reply can find its way back.
            CREATE TABLE IF NOT EXISTS owner_links (
                owner_message_id INTEGER PRIMARY KEY,
                channel TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        # Databases created before these columns existed.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(customers)")}
        if "lang" not in columns:
            self.db.execute("ALTER TABLE customers ADD COLUMN lang TEXT DEFAULT ''")
        if "intent" not in columns:
            self.db.execute("ALTER TABLE customers ADD COLUMN intent INTEGER DEFAULT 0")
        self.db.commit()

    def touch_customer(self, channel: str, chat_id: str, username: str, display_name: str, now: float) -> None:
        self.db.execute(
            """INSERT INTO customers (channel, chat_id, username, display_name, first_seen, last_message_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (channel, chat_id) DO UPDATE SET
                   username = COALESCE(NULLIF(excluded.username, ''), username),
                   display_name = COALESCE(NULLIF(excluded.display_name, ''), display_name),
                   last_message_at = excluded.last_message_at""",
            (channel, chat_id, username, display_name, now, now),
        )
        self.db.commit()

    def customer(self, channel: str, chat_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM customers WHERE channel = ? AND chat_id = ?", (channel, chat_id)
        ).fetchone()

    def add_message(self, channel: str, chat_id: str, role: str, text: str, now: float) -> int:
        cur = self.db.execute(
            "INSERT INTO messages (channel, chat_id, role, text, ts) VALUES (?, ?, ?, ?, ?)",
            (channel, chat_id, role, text, now),
        )
        self.db.commit()
        return cur.lastrowid

    def messages_after(self, channel: str, chat_id: str, after_id: int, limit: int = 50) -> list[sqlite3.Row]:
        """Messages newer than `after_id`, oldest first (at most the newest `limit`)."""
        rows = self.db.execute(
            "SELECT id, role, text, ts FROM messages WHERE channel = ? AND chat_id = ? AND id > ?"
            " ORDER BY id DESC LIMIT ?",
            (channel, chat_id, after_id, limit),
        ).fetchall()
        return list(reversed(rows))

    def recent_messages(self, channel: str, chat_id: str, limit: int) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT role, text, ts FROM messages WHERE channel = ? AND chat_id = ? ORDER BY id DESC LIMIT ?",
            (channel, chat_id, limit),
        ).fetchall()
        return list(reversed(rows))

    def set_summary(self, channel: str, chat_id: str, summary: str) -> None:
        self.db.execute(
            "UPDATE customers SET summary = ? WHERE channel = ? AND chat_id = ?", (summary, channel, chat_id)
        )
        self.db.commit()

    def set_ai(self, channel: str, chat_id: str, enabled: bool) -> bool:
        cur = self.db.execute(
            "UPDATE customers SET ai_enabled = ? WHERE channel = ? AND chat_id = ?",
            (1 if enabled else 0, channel, chat_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    def set_intent(self, channel: str, chat_id: str, level: int) -> None:
        self.db.execute("UPDATE customers SET intent = ? WHERE channel = ? AND chat_id = ?", (level, channel, chat_id))
        self.db.commit()

    def set_lang(self, channel: str, chat_id: str, lang: str) -> None:
        self.db.execute("UPDATE customers SET lang = ? WHERE channel = ? AND chat_id = ?", (lang, channel, chat_id))
        self.db.commit()

    def set_ai_off_reply(self, channel: str, chat_id: str, now: float) -> None:
        self.db.execute(
            "UPDATE customers SET last_ai_off_reply = ? WHERE channel = ? AND chat_id = ?", (now, channel, chat_id)
        )
        self.db.commit()

    def recent_customers(self, limit: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM customers ORDER BY last_message_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def link_owner_message(self, owner_message_id: int, channel: str, chat_id: str, now: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO owner_links (owner_message_id, channel, chat_id, created_at) VALUES (?, ?, ?, ?)",
            (owner_message_id, channel, chat_id, now),
        )
        self.db.commit()

    def lookup_owner_message(self, owner_message_id: int) -> tuple[str, str] | None:
        row = self.db.execute(
            "SELECT channel, chat_id FROM owner_links WHERE owner_message_id = ?", (owner_message_id,)
        ).fetchone()
        return (row["channel"], row["chat_id"]) if row else None


class RateLimiter:
    """At most `limit` messages per `window` seconds per customer, warning once per window."""

    def __init__(self, limit: int = CS_RATE_LIMIT, window: float = CS_RATE_WINDOW):
        self.limit, self.window = limit, window
        self.hits: dict[str, deque] = defaultdict(deque)
        self.warned_at: dict[str, float] = {}

    def check(self, key: str, now: float) -> tuple[bool, bool]:
        """(allowed, should_warn)."""
        hits = self.hits[key]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) < self.limit:
            hits.append(now)
            return True, False
        warned = self.warned_at.get(key, 0)
        if now - warned >= self.window:
            self.warned_at[key] = now
            return False, True
        return False, False


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #

def parse_customer_ref(ref: str, default_channel: str = "telegram") -> tuple[str, str]:
    """'123' -> ('telegram', '123'); 'wecom:abc' -> ('wecom', 'abc')."""
    ref = ref.strip()
    if ":" in ref:
        channel, chat_id = ref.split(":", 1)
        return channel.strip(), chat_id.strip()
    return default_channel, ref


def detect_handoff(text: str) -> str:
    for reason, pattern in _HANDOFF_KEYWORDS:
        if pattern.search(text):
            return reason
    return ""


class CustomerService:
    def __init__(
        self,
        store: Store,
        catalog: str,
        responder: Responder | None,
        owner_notify: OwnerNotify | None = None,
        rate_limiter: RateLimiter | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.store = store
        self.system = build_system_prompt(catalog)
        self.responder = responder
        self.owner_notify = owner_notify
        self.rate = rate_limiter or RateLimiter()
        self.clock = clock
        self.senders: dict[str, ChannelSend] = {}
        self.locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)

    # ---- wiring --------------------------------------------------------------

    def register_channel(self, channel: str, send: ChannelSend) -> None:
        self.senders[channel] = send

    # ---- customer side ---------------------------------------------------------

    async def handle(self, msg: Inbound) -> str | None:
        """Process one customer message; return what to send back, or None for silence."""
        text = msg.text.strip()
        if not text:
            return None
        key = (msg.channel, msg.chat_id)
        async with self.locks[key]:
            now = self.clock()
            before = self.store.customer(*key)
            is_new = before is None or now - (before["last_message_at"] or 0) >= CS_NEW_SESSION_GAP
            self.store.touch_customer(msg.channel, msg.chat_id, msg.username, msg.display_name, now)
            if is_new:
                self.store.set_intent(*key, 0)
            self.store.add_message(msg.channel, msg.chat_id, "customer", text, now)
            lang = self._lang(key, text, msg.lang)

            allowed, warn = self.rate.check(f"{msg.channel}:{msg.chat_id}", now)
            if not allowed:
                logger.info("客户 %s:%s 超过频率限制", *key)
                return CANNED[lang]["rate_limited"].format(limit=self.rate.limit) if warn else None

            customer = self.store.customer(*key)
            if not customer["ai_enabled"]:
                await self._notify(key, "ai_paused", forward_only=text)
                return None

            if self.responder is None:
                await self._notify(key, "ai_unavailable")
                if now - (customer["last_ai_off_reply"] or 0) >= NO_AI_REPLY_EVERY:
                    self.store.set_ai_off_reply(*key, now)
                    return self._say(key, CANNED[lang]["handoff"])
                return None

            try:
                decision = await self.responder(self.system, self._context(key))
            except Exception:  # noqa: BLE001 - any failure goes to the owner, never silence
                logger.exception("客服 AI 出错（%s:%s）", *key)
                await self._notify(key, "ai_error")
                return self._say(key, CANNED[lang]["handoff"])

            if decision.summary:
                self.store.set_summary(*key, decision.summary)
            intent = INTENT_LEVELS.get(decision.intent, 0)
            intent_rose = intent > (customer["intent"] or 0)  # already reset to 0 for a new conversation
            if intent_rose:
                self.store.set_intent(*key, intent)

            keyword_reason = detect_handoff(text)
            if decision.handoff or keyword_reason or not decision.reply:
                reason = decision.reason if decision.handoff and decision.reason else (keyword_reason or "complex")
                # The handoff notice already carries everything, so it's the only one.
                reply = self._say(key, CANNED[lang]["handoff"])
                await self._notify(key, reason)
                return reply

            reply = self._say(key, decision.reply)
            if intent_rose and CS_NOTIFY_INTENT:
                await self._notify_intent(key, intent, is_new)
            elif is_new and CS_NOTIFY_NEW:
                await self._notify_new(key)
            return reply

    async def handle_attachment(self, msg: Inbound, kind: str) -> tuple[str, int | None]:
        """A photo/file from a customer: record it and tell the owner. Returns
        (reply, owner_message_id) so the channel can put the file itself under it."""
        key = (msg.channel, msg.chat_id)
        now = self.clock()
        self.store.touch_customer(msg.channel, msg.chat_id, msg.username, msg.display_name, now)
        label = f"[{kind}]" + (f" {msg.text.strip()}" if msg.text.strip() else "")
        self.store.add_message(msg.channel, msg.chat_id, "customer", label, now)
        lang = self._lang(key, msg.text, msg.lang)
        owner_id = await self._notify(key, "attachment")
        return self._say(key, CANNED[lang]["attachment"]), owner_id

    def _lang(self, key: tuple[str, str], text: str, hint: str = "") -> str:
        """Language for the fixed lines: this message's, else the customer's last
        known one, else the channel's hint, else Chinese. Remembered per customer."""
        lang = detect_lang(text)
        if lang:
            self.store.set_lang(*key, lang)
            return lang
        c = self.store.customer(*key)
        return (c["lang"] if c and c["lang"] else "") or hint or "zh"

    def _say(self, key: tuple[str, str], text: str) -> str:
        self.store.add_message(*key, "assistant", text, self.clock())
        return text

    def _context(self, key: tuple[str, str]) -> list[dict]:
        """Recent history as Claude messages; the newest customer message is last."""
        rows = self.store.recent_messages(*key, CS_HISTORY)
        messages = []
        for row in rows:
            if row["role"] == "customer":
                messages.append({"role": "user", "content": row["text"]})
            elif row["role"] == "owner":
                messages.append({"role": "assistant", "content": f"【本人回复】{row['text']}"})
            else:
                messages.append({"role": "assistant", "content": row["text"]})
        while messages and messages[0]["role"] != "user":
            messages.pop(0)  # the API wants a user turn first
        return messages

    # ---- owner side --------------------------------------------------------------

    def transcript(self, key: tuple[str, str], limit: int = CS_HISTORY) -> str:
        who = {"customer": "客户", "assistant": "助理", "owner": "你"}
        lines = []
        for row in self.store.recent_messages(*key, limit):
            at = datetime.fromtimestamp(row["ts"], CS_TIMEZONE).strftime("%m-%d %H:%M")
            lines.append(f"{who[row['role']]} {at}：{row['text']}")
        return "\n".join(lines)

    def describe(self, key: tuple[str, str]) -> str:
        c = self.store.customer(*key)
        name = (c["display_name"] if c else "") or "（无昵称）"
        user = f" @{c['username']}" if c and c["username"] else ""
        ref = key[1] if key[0] == "telegram" else f"{key[0]}:{key[1]}"
        return f"客户：{name}{user}\nChat ID：{ref}"

    def _where(self, key: tuple[str, str]) -> str:
        return CHANNEL_LABELS.get(key[0], key[0])

    def _intent_line(self, key: tuple[str, str]) -> str:
        c = self.store.customer(*key)
        label = INTENT_LABELS.get(c["intent"] if c else 0)
        return f"购买意向：{label}\n" if label else ""

    async def _notify_new(self, key: tuple[str, str]) -> int | None:
        """Someone started chatting; the AI is on it, this is just so the owner knows."""
        ref = key[1] if key[0] == "telegram" else f"{key[0]}:{key[1]}"
        text = (
            f"👋 有客户来咨询了（{self._where(key)}）\n"
            f"{self.describe(key)}\n\n"
            f"{self.transcript(key, 4)}\n\n"
            f"AI 正在接待，你不用管。想亲自说话就回复这条消息；/ai {ref} off 可暂停 AI"
        )
        return await self._send_owner(key, text)

    async def _notify_intent(self, key: tuple[str, str], level: int, is_new: bool) -> int | None:
        """The customer looks keen to buy; the AI keeps chatting, the owner may want to step in."""
        ref = key[1] if key[0] == "telegram" else f"{key[0]}:{key[1]}"
        c = self.store.customer(*key)
        summary = (c["summary"] if c else "") or "（还没问清楚）"
        head = "💰 客户准备购买了" if level >= 2 else "🔥 客户有购买意向"
        text = (
            f"{head}（{self._where(key)}{'，新客户' if is_new else ''}）\n"
            f"{self.describe(key)}\n"
            f"需求摘要：{summary}\n\n"
            f"—— 最近对话 ——\n{self.transcript(key)}\n\n"
            f"AI 还在继续聊。想亲自跟进就回复这条消息，内容会转给客户；/ai {ref} off 可暂停 AI"
        )
        return await self._send_owner(key, text)

    async def _notify(self, key: tuple[str, str], reason: str, forward_only: str | None = None) -> int | None:
        ref = key[1] if key[0] == "telegram" else f"{key[0]}:{key[1]}"
        if forward_only is not None:
            text = f"💬 {self.describe(key)}\n（AI 已暂停，/ai {ref} on 恢复）\n\n{forward_only}\n\n↩️ 回复这条消息，内容会转给客户"
        else:
            c = self.store.customer(*key)
            summary = (c["summary"] if c else "") or "（还没问清楚）"
            text = (
                f"🔔 需要你接手：{HANDOFF_REASONS.get(reason, reason)}（{self._where(key)}）\n"
                f"{self.describe(key)}\n"
                f"需求摘要：{summary}\n"
                f"{self._intent_line(key)}\n"
                f"—— 最近对话 ——\n{self.transcript(key)}\n\n"
                f"↩️ 回复这条消息，内容会转给客户。/ai {ref} off 可暂停 AI"
            )
        return await self._send_owner(key, text)

    async def _send_owner(self, key: tuple[str, str], text: str) -> int | None:
        """Send a notice about this customer to the owner, linked so replying to it reaches them."""
        if self.owner_notify is None:
            logger.warning("没有设置 OWNER_CHAT_ID，客户 %s:%s 的通知发不出去", *key)
            return None
        try:
            owner_message_id = await self.owner_notify(text)
        except Exception:  # noqa: BLE001 - a failed notice must not break the customer's reply
            logger.exception("给本人的通知发送失败（%s:%s）", *key)
            return None
        if owner_message_id is not None:
            self.store.link_owner_message(owner_message_id, *key, self.clock())
        return owner_message_id

    def link_owner_message(self, owner_message_id: int, channel: str, chat_id: str) -> None:
        self.store.link_owner_message(owner_message_id, channel, chat_id, self.clock())

    def lookup_owner_message(self, owner_message_id: int) -> tuple[str, str] | None:
        return self.store.lookup_owner_message(owner_message_id)

    async def deliver_owner_reply(self, owner_message_id: int, text: str) -> tuple[str, str] | None:
        """Send the owner's reply to the customer that message was about."""
        key = self.store.lookup_owner_message(owner_message_id)
        if key is None:
            return None
        send = self.senders.get(key[0])
        if send is None:
            raise RuntimeError(f"没有注册 {key[0]} 渠道，发不出去")
        await send(key[1], text)
        self.store.add_message(*key, "owner", text, self.clock())
        return key

    def record_owner_message(self, channel: str, chat_id: str, text: str) -> None:
        """For owner replies a channel delivered itself (files), so the AI sees them."""
        self.store.add_message(channel, chat_id, "owner", text, self.clock())

    def set_ai(self, ref: str, enabled: bool) -> tuple[bool, tuple[str, str]]:
        key = parse_customer_ref(ref)
        return self.store.set_ai(*key, enabled), key

    def customers_report(self, limit: int = 15) -> str:
        rows = self.store.recent_customers(limit)
        if not rows:
            return "还没有客户来咨询。"
        blocks = []
        for c in rows:
            key = (c["channel"], c["chat_id"])
            at = datetime.fromtimestamp(c["last_message_at"], CS_TIMEZONE).strftime("%m-%d %H:%M")
            last = self.store.recent_messages(*key, 1)
            last_text = last[0]["text"] if last else ""
            if len(last_text) > 40:
                last_text = last_text[:40] + "…"
            ai = "AI 开" if c["ai_enabled"] else "AI 关"
            blocks.append(
                f"{self.describe(key)}（{self._where(key)}，{ai}）\n"
                f"{self._intent_line(key)}"
                f"需求：{c['summary'] or '（还没问清楚）'}\n"
                f"最后消息 {at}：{last_text}"
            )
        return f"最近 {len(rows)} 位客户：\n\n" + "\n\n".join(blocks)
