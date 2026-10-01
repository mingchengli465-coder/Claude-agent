"""Telegram AI chatbot backed by an OpenRouter chat model.

Reads its configuration from the environment, keeps a short rolling history for
each chat, and relays messages between Telegram and OpenRouter.
"""

import asyncio
import datetime as dt
import json
import logging
import os
import re
import secrets
from collections import defaultdict, deque
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import customer_service as cs
import llm
import tweet as tweet_mod
import agent as agent_mod
import bluesky
import messenger as messenger_mod
import outreach as outreach_mod
import visits as visits_mod
import web
import xhs

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# .strip() because a value pasted into a hosting dashboard often carries a
# trailing newline or space, which the Telegram API rejects as a bad token.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.environ.get("MODEL", "deepseek/deepseek-chat-v3.1:free")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful assistant chatting with a user on Telegram. "
    "Keep answers clear and concise, and reply in the user's language.",
)
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "60"))

# Only this chat may use /xhs and the note buttons; everyone else just chats.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
# The owner: their messages and every scheduled job behave as before; anyone else
# gets customer-service mode. Falls back to ADMIN_CHAT_ID, which is the same person.
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID", "").strip() or ADMIN_CHAT_ID
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
XHS_DAILY_TIME = os.environ.get("XHS_DAILY_TIME", "09:00")
XHS_TIMEZONE = os.environ.get("XHS_TIMEZONE", "Asia/Taipei")
# Tweets go out twice a day by default, in the same timezone.
X_DAILY_TIMES = os.environ.get("X_DAILY_TIMES", "12:00,20:00")
X_TIMEZONE = os.environ.get("X_TIMEZONE", "Asia/Taipei")
# A one-off tweet (same format as /post) published a few seconds after startup,
# for when the post is prepared without the owner at the keyboard.
X_POST_ON_START = os.environ.get("X_POST_ON_START", "").strip()
# Added under every post that's copied to the Facebook Page (Facebook allows links in posts).
FB_POST_SUFFIX = os.environ.get(
    "FB_POST_SUFFIX",
    "\n\nSee what I build and chat with my AI assistant:\nhttps://mingchengli465-coder.github.io/Claude-agent/?from=fb",
).replace("\\n", "\n")
FB_MANUAL_COPY = os.environ.get("FB_MANUAL_COPY", "true").lower() != "false"
# A public Telegram channel (@name, t.me/name or -100… id) that gets each post too.
# The bot must be an admin of the channel.
TG_CHANNEL = os.environ.get("TG_CHANNEL", "").strip()
TG_CHANNEL_LINK = os.environ.get("TG_CHANNEL_LINK", "https://mingchengli465-coder.github.io/Claude-agent/?from=tgch").strip()
# The website visitor report (visits.py) goes to the owner every morning.
VISITS_REPORT_TIME = os.environ.get("VISITS_REPORT_TIME", "09:00")
VISITS_TIMEZONE = os.environ.get("VISITS_TIMEZONE", "Asia/Shanghai")
# The owner's two copies of the site, for the links that mark their own devices.
SITE_URLS = [u.strip() for u in os.environ.get(
    "SITE_URLS",
    "https://worker-production-42fb.up.railway.app/,https://mingchengli465-coder.github.io/Claude-agent/",
).split(",") if u.strip()]

# One "round" is a user message plus the assistant's reply.
MAX_HISTORY_ROUNDS = int(os.environ.get("MAX_HISTORY_ROUNDS", "20"))
MAX_HISTORY_MESSAGES = MAX_HISTORY_ROUNDS * 2

# Telegram rejects text messages longer than 4096 characters.
TELEGRAM_MESSAGE_LIMIT = 4096

# A personal assistant bot built for one client: only these chats get answers
# (comma-separated Telegram IDs). Empty = everyone, as before.
ALLOWED_CHAT_IDS = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
PRIVATE_BOT_TEXT = os.environ.get("PRIVATE_BOT_TEXT", "").replace("\\n", "\n").strip() or (
    "🔒 这是私人助理，只为主人服务。\nThis is a private assistant."
)
# The /start greeting of the general chat (a client's personal bot sets its own).
WELCOME_TEXT = os.environ.get("WELCOME_TEXT", "").replace("\\n", "\n").strip() or (
    "👋 Hi! I'm an AI chatbot.\n\n"
    "Just send me a message and I'll reply. I remember the last "
    f"{MAX_HISTORY_ROUNDS} rounds of our conversation.\n\n"
    "Commands:\n"
    "/start - show this message\n"
    "/reset - clear the conversation history\n"
    f"\nCurrent model: {llm.DEEPSEEK_MODEL if llm.USE_DEEPSEEK else MODEL}"
)

GENERIC_ERROR_TEXT = "😵 Something went wrong while contacting the AI. Please try again in a moment."
RATE_LIMIT_TEXT = "🐢 The AI is rate limited right now. Please wait a moment and try again."
TIMEOUT_TEXT = "⏳ The AI took too long to answer. Please try again."
CONNECTION_ERROR_TEXT = "🔌 I couldn't reach the AI service. Please try again shortly."

# chat_id -> rolling history of messages (system prompt is added at call time).
histories: dict[int, deque] = defaultdict(lambda: deque(maxlen=MAX_HISTORY_MESSAGES))
# chat_id -> lock, so concurrent messages from one chat don't interleave.
chat_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
    timeout=REQUEST_TIMEOUT,
)
# DeepSeek, when DEEPSEEK_API_KEY is set: answers first, OpenRouter's MODEL behind it.
ds_client = llm.deepseek_client(REQUEST_TIMEOUT)


def chat_attempts() -> list[tuple]:
    """(client, model) in the order the owner's chat tries them."""
    tries = [(ds_client, llm.DEEPSEEK_MODEL)] if ds_client is not None else []
    return tries + [(client, MODEL)]


def split_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split text into Telegram-sized chunks, preferring paragraph/line/word breaks."""
    chunks: list[str] = []
    remaining = text

    while len(remaining) > limit:
        window = remaining[:limit]
        # Use the nicest break point available inside the window, if there is one.
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        if cut <= 0:
            cut = limit  # No natural break point: hard split.
        chunk, remaining = remaining[:cut].rstrip(), remaining[cut:].lstrip()
        if chunk:
            chunks.append(chunk)

    remaining = remaining.strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def send_long_message(update: Update, text: str) -> None:
    """Send text to the chat, splitting it when it exceeds Telegram's limit."""
    for chunk in split_message(text):
        await update.effective_message.reply_text(chunk)


async def ask_model(chat_id: int, user_text: str) -> str:
    """Send the chat history plus the new message to OpenRouter and return the reply."""
    history = histories[chat_id]
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    attempts = chat_attempts()
    for index, (api, model) in enumerate(attempts):
        try:
            # A hard deadline on top of the SDK timeout: OpenRouter keeps a slow
            # request alive by trickling whitespace, so the socket timeout never fires.
            response = await asyncio.wait_for(
                api.chat.completions.create(model=model, messages=messages), REQUEST_TIMEOUT
            )
            break
        except Exception as exc:  # noqa: BLE001 - the last attempt's error is raised to chat()
            if index == len(attempts) - 1:
                raise
            logger.warning("%s 回答失败，改用 %s：%s", model, attempts[index + 1][1], exc)

    if not response.choices:
        raise RuntimeError("OpenRouter returned a response with no choices")

    reply = (response.choices[0].message.content or "").strip()
    if not reply:
        reply = "🤔 I got an empty answer from the model. Could you rephrase that?"

    # Only remember exchanges that actually completed.
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    return reply


# --------------------------------------------------------------------------- #
# The owner's agent (agent.py): websites and post drafts from their own chat
# --------------------------------------------------------------------------- #

AGENT_ENABLED = os.environ.get("AGENT_ENABLED", "true").lower() != "false"
site_store: agent_mod.SiteStore | None = None
# draft id -> (chat_id, text, platforms), waiting for 发布 / 取消
pending_posts: dict[str, tuple[int, str, list[str]]] = {}


def public_base_url() -> str:
    url = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if url:
        return url.rstrip("/")
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    return f"https://{domain}" if domain else f"http://localhost:{web.WEB_PORT}"


def available_platforms() -> list[str]:
    found = []
    if not tweet_mod.missing_credentials():
        found.append("x")
    if bluesky.enabled():
        found.append("bluesky")
    if channel_chat_id():
        found.append("channel")
    return found


def agent_for(chat_id: int) -> agent_mod.Agent | None:
    """The agent for the owner (or a personal bot's allowed chats); None = plain chat."""
    if not AGENT_ENABLED or ds_client is None or site_store is None or telegram_bot is None:
        return None
    if not (is_owner(chat_id) or str(chat_id) in ALLOWED_CHAT_IDS):
        return None
    return agent_mod.Agent(ds_client, llm.DEEPSEEK_MODEL, site_store, public_base_url(), available_platforms(),
                           on_draft=send_post_draft, on_progress=send_progress)


async def send_progress(chat_id: int, text: str) -> None:
    await telegram_bot.send_message(chat_id=chat_id, text=text)


async def send_post_draft(chat_id: int, text: str, platforms: list[str]) -> None:
    draft_id = secrets.token_hex(4)
    pending_posts[draft_id] = (chat_id, text, platforms)
    where = ", ".join(agent_mod.platform_name(p) for p in platforms)
    buttons = InlineKeyboardMarkup([[InlineKeyboardButton(agent_mod.ui("publish"), callback_data=f"agpost:{draft_id}:ok"),
                                     InlineKeyboardButton(agent_mod.ui("cancel"), callback_data=f"agpost:{draft_id}:no")]])
    await telegram_bot.send_message(chat_id=chat_id, reply_markup=buttons, disable_web_page_preview=True,
                                    text=agent_mod.ui("draft").format(where=where) + f"\n\n{text}\n\n" + agent_mod.ui("draft_tip"))


async def publish_to(platform: str, text: str) -> str:
    """Post on one platform; returns a line for the owner."""
    if platform == "x":
        links = re.findall(r"https?://\S+", text)
        main = re.sub(r"\s*https?://\S+", "", text).strip()  # X refuses links in the post itself
        if tweet_mod.x_length(main) > tweet_mod.X_WEIGHTED_LIMIT:
            return f"⚠️ X 没发：太长了（{tweet_mod.x_length(main)}/{tweet_mod.X_WEIGHTED_LIMIT}，中文一个字算 2）"
        url = await tweet_mod.publish(tweet_mod.Tweet(domain="助理", topic=main[:60], text=main))
        if links:
            await tweet_mod.post_reply(url, "\n".join(links))
        return f"🐦 X：{url}"
    if platform == "bluesky":
        return f"🦋 Bluesky：{await bluesky.post(text)}"
    if platform == "channel":
        note = await crosspost_channel(text)
        return note.strip() or "⚠️ Telegram 频道没接上"
    return f"⚠️ 不认识的平台 {platform}"


async def agent_post_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """发布 / 取消 under a post draft."""
    query = update.callback_query
    _, draft_id, action = (query.data or "::").split(":", 2)
    draft = pending_posts.get(draft_id)
    if draft is None or draft[0] != query.message.chat_id:
        await query.answer(agent_mod.ui("done_already"), show_alert=True)
        return
    await query.answer()
    pending_posts.pop(draft_id, None)
    _, text, platforms = draft
    if action != "ok":
        await query.edit_message_text(agent_mod.ui("cancelled") + f"\n\n{text}")
        return
    await query.edit_message_text(agent_mod.ui("publishing") + f"\n\n{text}")
    results = []
    for platform in platforms:
        try:
            results.append(await publish_to(platform, text))
        except Exception as exc:  # noqa: BLE001 - report each platform on its own
            logger.exception("助理发帖失败（%s）", platform)
            results.append(f"⚠️ {agent_mod.PLATFORM_NAMES.get(platform, platform)} 没发成功：{exc}")
    await query.edit_message_text(agent_mod.ui("results") + "\n" + "\n".join(results) + f"\n\n{text}",
                                  disable_web_page_preview=True)


async def sites_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/sites — the websites the agent has built in this chat."""
    if await refused(update):
        return
    chat_id = update.effective_chat.id
    if site_store is None or not (is_owner(chat_id) or str(chat_id) in ALLOWED_CHAT_IDS):
        await update.effective_message.reply_text(agent_mod.ui("denied"))
        return
    rows = site_store.list(chat_id)
    if not rows:
        await update.effective_message.reply_text(agent_mod.ui("no_sites"))
        return
    lines = [f"• {r['title'] or r['slug']}\n  {public_base_url()}/s/{r['slug']}" for r in rows]
    await update.effective_message.reply_text(agent_mod.ui("sites") + "\n\n" + "\n".join(lines), disable_web_page_preview=True)


async def refused(update: Update) -> bool:
    """True (after telling them once per message) when this chat may not use a private bot."""
    if not ALLOWED_CHAT_IDS or str(update.effective_chat.id) in ALLOWED_CHAT_IDS:
        return False
    logger.info("私人机器人拒绝了 chat %s", update.effective_chat.id)
    if update.effective_message is not None:
        await update.effective_message.reply_text(PRIVATE_BOT_TEXT)
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await refused(update):
        return
    chat_id = update.effective_chat.id
    logger.info("/start from chat %s", chat_id)
    if customer_mode_for(update):
        await update.effective_message.reply_text(for_customer(update, CS_WELCOME_TEXT, CS_WELCOME_TEXT_EN))
        return
    await update.effective_message.reply_text(WELCOME_TEXT)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await refused(update):
        return
    chat_id = update.effective_chat.id
    histories.pop(chat_id, None)
    logger.info("/reset cleared history for chat %s", chat_id)
    await update.effective_message.reply_text("🧹 Conversation history cleared. Let's start fresh!")


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every plain text message lands here first."""
    if await refused(update):
        return
    if is_owner(update.effective_chat.id):
        if await forward_owner_reply(update, context):
            return
        if await connect_mailer(update):
            return
        await chat(update, context)
        return
    if customer_mode_for(update):
        await customer_message(update, context)
        return
    await chat(update, context)


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    chat_id = update.effective_chat.id
    user_text = message.text.strip()
    if not user_text:
        return

    logger.info("Message from chat %s (%d chars)", chat_id, len(user_text))

    async with chat_locks[chat_id]:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except TelegramError:
            logger.debug("Could not send typing action to chat %s", chat_id, exc_info=True)

        try:
            agent = agent_for(chat_id)
            if agent is not None:
                try:
                    reply = await agent.run(chat_id, list(histories[chat_id]), user_text)
                    histories[chat_id].append({"role": "user", "content": user_text})
                    histories[chat_id].append({"role": "assistant", "content": reply})
                except Exception:  # noqa: BLE001 - fall back to plain chat rather than fail
                    logger.exception("助理模式出错，改用普通聊天")
                    reply = await ask_model(chat_id, user_text)
            else:
                reply = await ask_model(chat_id, user_text)
        except RateLimitError:
            logger.warning("Rate limited by OpenRouter for chat %s", chat_id, exc_info=True)
            await message.reply_text(RATE_LIMIT_TEXT)
            return
        except (APITimeoutError, asyncio.TimeoutError):
            logger.warning("OpenRouter request timed out for chat %s", chat_id, exc_info=True)
            await message.reply_text(TIMEOUT_TEXT)
            return
        except APIConnectionError:
            logger.warning("Could not reach OpenRouter for chat %s", chat_id, exc_info=True)
            await message.reply_text(CONNECTION_ERROR_TEXT)
            return
        except APIStatusError as exc:
            logger.error(
                "OpenRouter returned HTTP %s for chat %s: %s", exc.status_code, chat_id, exc
            )
            await message.reply_text(GENERIC_ERROR_TEXT)
            return
        except Exception:
            logger.exception("Unexpected error while answering chat %s", chat_id)
            await message.reply_text(GENERIC_ERROR_TEXT)
            return

    try:
        await send_long_message(update, reply)
    except TelegramError:
        logger.exception("Failed to deliver reply to chat %s", chat_id)


# --------------------------------------------------------------------------- #
# 小红书 note generation
# --------------------------------------------------------------------------- #

XHS_DENIED_TEXT = "这个功能没有对你开放，直接发消息就能聊天 🙂"
XHS_WORKING_TEXT = "正在生成小红书笔记，大概要十几秒…"
XHS_FAILED_TEXT = "😵 小红书笔记生成失败了（已经重试过一次）。\n\n{error}"

# chat_id -> the note last sent there, so the buttons have something to act on.
last_notes: dict[int, xhs.Note] = {}
# chat_id -> which cover layout that chat is currently on.
cover_variants: dict[int, int] = defaultdict(int)


def is_admin(chat_id: int | str | None) -> bool:
    """True only for ADMIN_CHAT_ID. With it unset, nobody is admin."""
    return bool(ADMIN_CHAT_ID) and str(chat_id) == ADMIN_CHAT_ID


def is_owner(chat_id: int | str | None) -> bool:
    """True only for OWNER_CHAT_ID (or ADMIN_CHAT_ID when that is unset)."""
    return bool(OWNER_CHAT_ID) and str(chat_id) == OWNER_CHAT_ID


def note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🔁 重写文案", callback_data="xhs:rewrite"),
            InlineKeyboardButton("🎨 换封面", callback_data="xhs:cover"),
        ]]
    )


async def send_note(context: ContextTypes.DEFAULT_TYPE, chat_id: int, note: xhs.Note) -> None:
    """Send the note as four separate messages, so each is easy to long-press."""
    variant = cover_variants[chat_id]
    cover = await xhs.render_cover_async(note, variant)

    await context.bot.send_photo(chat_id=chat_id, photo=cover)
    await context.bot.send_message(chat_id=chat_id, text=note.title)
    for chunk in split_message(note.body):
        await context.bot.send_message(chat_id=chat_id, text=chunk)
    await context.bot.send_message(
        chat_id=chat_id, text=note.tags_text(), reply_markup=note_keyboard()
    )

    last_notes[chat_id] = note


async def produce_and_send(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Generate a fresh note and deliver it, reporting failures to the admin."""
    try:
        note = await xhs.generate_note()
    except Exception as exc:  # noqa: BLE001 - already retried inside generate_note
        logger.exception("小红书笔记生成失败")
        await context.bot.send_message(chat_id=chat_id, text=XHS_FAILED_TEXT.format(error=exc))
        return

    logger.info("生成了小红书笔记：[%s] %s", note.domain, note.title)
    await send_note(context, chat_id, note)


async def xhs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/xhs - generate a note on demand."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        logger.info("拒绝了来自 chat %s 的 /xhs", chat_id)
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return

    await update.effective_message.reply_text(XHS_WORKING_TEXT)
    try:
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except TelegramError:
        logger.debug("Could not send typing action to chat %s", chat_id, exc_info=True)
    await produce_and_send(context, chat_id)


async def xhs_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The 09:00 Asia/Taipei run."""
    chat_id = int(ADMIN_CHAT_ID)
    logger.info("每日小红书任务触发，发送到 chat %s", chat_id)
    await produce_and_send(context, chat_id)


async def xhs_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 重写文案 / 换封面."""
    query = update.callback_query
    chat_id = query.message.chat_id

    if not is_admin(chat_id):
        await query.answer(XHS_DENIED_TEXT, show_alert=True)
        return

    action = (query.data or "").split(":", 1)[-1]

    if action == "cover":
        note = last_notes.get(chat_id)
        if note is None:
            await query.answer("这条笔记我这边已经没有记录了，重新 /xhs 一次吧", show_alert=True)
            return
        await query.answer("换个封面…")
        cover_variants[chat_id] += 1
        try:
            cover = await xhs.render_cover_async(note, cover_variants[chat_id])
        except Exception as exc:  # noqa: BLE001
            logger.exception("封面渲染失败")
            await context.bot.send_message(chat_id=chat_id, text=f"😵 封面生成失败：{exc}")
            return
        await context.bot.send_photo(chat_id=chat_id, photo=cover, reply_markup=note_keyboard())
        return

    if action == "rewrite":
        await query.answer("重写中…")
        await produce_and_send(context, chat_id)
        return

    await query.answer()


# --------------------------------------------------------------------------- #
# X (Twitter) posts — generated and published automatically
# --------------------------------------------------------------------------- #

TWEET_WORKING_TEXT = "正在生成並發布推文…"
TWEET_FAILED_TEXT = "😵 推文生成失敗了（已經換模型重試過）。沒有發任何東西。\n\n{error}"
# The text is included so a failed post isn't lost — it can be posted by hand.
TWEET_PUBLISH_FAILED_TEXT = (
    "😵 推文發布失敗。\n\n{error}\n\n———\n這則的內容，要手動發的話：\n\n{text}"
)
TWEET_SENT_TEXT = "🚀 已發推（{domain}）\n\n{text}\n\n———\n{url}"
TWEET_LINK_REPLY_OK = "\n\n💬 已在底下回覆 Telegram 連結"
# The tweet itself is up, so this is a warning, not a failure.
TWEET_LINK_REPLY_FAILED = "\n\n⚠️ 推文已發出，但底下的 Telegram 連結回覆失敗：{error}"
TWEET_PAUSED_TEXT = "⏸ 自動發推已暫停。/resume 恢復。"
POST_USAGE_TEXT = (
    "用法：/post 后面接推文内容，原样发到 X。\n\n"
    "想在推文底下再回复一条（比如放链接），另起一行只写 ---，下面写回复内容。\n"
    "没有 --- 的话，底下会自动回复机器人链接。"
)
TWEET_RESUMED_TEXT = "▶️ 自動發推已恢復。"
TWEET_ALREADY_PAUSED = "⏸ 本來就是暫停狀態。/resume 恢復。"
TWEET_ALREADY_RUNNING = "▶️ 本來就在跑了。/pause 可以暫停。"
TWEET_SKIPPED_TEXT = "⏸ 已暫停，這次排程跳過。"


async def crosspost_facebook(text: str) -> str:
    """Also publish on the Facebook Page, if it's connected. Returns a line for the owner's report.

    Until it is, the report carries the Facebook version ready to copy, so the
    owner can paste it onto the Page by hand (FB_MANUAL_COPY=false turns that off)."""
    fb = web_chat.messenger if web_chat is not None else None
    if fb is None or not fb.connected:
        if not FB_MANUAL_COPY:
            return ""
        return "\n\n📘 Facebook 版（长按复制，发到你的专页）：\n\n" + text + FB_POST_SUFFIX
    try:
        url = await fb.post(text + FB_POST_SUFFIX)
    except Exception as exc:  # noqa: BLE001 - X is unaffected
        logger.exception("Facebook 专页发帖失败")
        return f"\n\n⚠️ Facebook 专页没发成功：{exc}"
    logger.info("Facebook 专页已发帖：%s", url)
    return f"\n\n📘 Facebook 专页也发了：{url}"


def channel_chat_id(raw: str = "") -> str | int:
    """TG_CHANNEL as Telegram wants it: @name, or the numeric id of a private channel."""
    value = (raw or TG_CHANNEL).strip()
    value = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me)/", "", value, flags=re.I).strip("/ ")
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return "@" + value.lstrip("@") if value else ""


async def crosspost_channel(text: str) -> str:
    """Also post in the owner's Telegram channel, if one is set."""
    channel = channel_chat_id()
    if not channel or telegram_bot is None:
        return ""
    body = text.strip()
    if TG_CHANNEL_LINK:
        body += f"\n\n👉 {TG_CHANNEL_LINK}"
    bot_link = tweet_mod.telegram_link()
    if bot_link:
        body += f"\n💬 {bot_link}"
    try:
        await telegram_bot.send_message(chat_id=channel, text=body)
    except TelegramError as exc:
        logger.exception("Telegram 频道发帖失败")
        return f"\n\n⚠️ Telegram 频道没发成功：{exc}（机器人要是频道管理员才能发）"
    logger.info("Telegram 频道已发帖：%s", channel)
    return f"\n\n📣 Telegram 频道也发了：{channel}"


CHANNEL_HELLO = (
    "👋 欢迎来到 vinc AI studio\n\n"
    "我帮小店搭 24 小时在线的 AI 客服，也帮你把 Claude Code / Codex 装进自己的电脑。"
    "这里会分享 AI 小技巧和实用案例。\n\n"
    "👋 Welcome! I build 24/7 AI customer assistants for small businesses and set up "
    "Claude Code / Codex on your computer. AI tips and real examples, here."
)


async def hello_channel_once() -> None:
    """The first time a channel is set, post an introduction there and tell the owner
    whether it worked (it doubles as the check that the bot is an admin)."""
    channel = channel_chat_id()
    if not channel or telegram_bot is None or visits is None:
        return
    flag = f"channel_hello:{channel}"
    if not visits.take_flag(flag):
        return
    note = await crosspost_channel(CHANNEL_HELLO)
    if "⚠️" in note:
        visits.db.execute("DELETE FROM visit_settings WHERE key = ?", (flag,))  # try again next start
        visits.db.commit()
    if OWNER_CHAT_ID:
        text = ("📣 Telegram 频道接好了，已经发了第一条欢迎介绍。以后 X 每次发帖都会同步到频道。"
                if "⚠️" not in note else "⚠️ Telegram 频道还发不了：" + note.strip().lstrip("⚠️ "))
        try:
            await telegram_bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
        except TelegramError:
            logger.exception("频道状态通知发送失败")


BLUESKY_HELLO = (
    "Hi Bluesky 👋 I'm Vinc. I build 24/7 AI customer assistants for small businesses "
    "and set up Claude Code / Codex on your computer, one-on-one, in English or 中文. "
    "Sharing practical AI tips here."
)


async def hello_bluesky_once() -> None:
    """The first start with a Bluesky account posts an introduction and tells the owner
    whether the handle and app password work. Retried on the next start if it failed."""
    if not bluesky.enabled() or visits is None:
        return
    # Once per account: a changed handle (same account) must not introduce it again.
    if visits.db.execute("SELECT 1 FROM visit_settings WHERE key LIKE 'bluesky_hello:%'").fetchone():
        return
    flag = f"bluesky_hello:{bluesky.HANDLE}"
    if not visits.take_flag(flag):
        return
    note = await crosspost_bluesky(BLUESKY_HELLO)
    failed = "⚠️" in note
    if failed:
        visits.db.execute("DELETE FROM visit_settings WHERE key = ?", (flag,))
        visits.db.commit()
    if OWNER_CHAT_ID and telegram_bot is not None:
        text = ("🦋 Bluesky 接好了，已经发了第一条自我介绍：" + note.split("：", 1)[-1].strip()
                + "\n以后 X 每次发帖都会同步到 Bluesky。") if not failed else (
                "⚠️ Bluesky 还接不上：" + note.split("：", 1)[-1].strip()
                + "\n多半是用户名或应用密码不对，重新生成一个应用密码发给我就行。")
        try:
            await telegram_bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text, disable_web_page_preview=True)
        except TelegramError:
            logger.exception("Bluesky 状态通知发送失败")


async def crosspost_bluesky(text: str) -> str:
    """Also post on Bluesky, if it's set up."""
    if not bluesky.enabled():
        return ""
    try:
        url = await bluesky.post(text)
    except Exception as exc:  # noqa: BLE001 - the other platforms are unaffected
        logger.exception("Bluesky 发帖失败")
        return f"\n\n⚠️ Bluesky 没发成功：{exc}"
    logger.info("Bluesky 已发帖：%s", url)
    return f"\n\n🦋 Bluesky 也发了：{url}"


async def crosspost_everywhere(text: str) -> str:
    """Every platform besides X. Returns the lines for the owner's report."""
    return "".join([await crosspost_facebook(text), await crosspost_bluesky(text), await crosspost_channel(text)])


async def produce_and_post_tweet(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Generate a tweet, publish it, and report the result. No approval step."""
    try:
        item = await tweet_mod.generate_tweet()
    except Exception as exc:  # noqa: BLE001 - already retried inside generate_tweet
        logger.exception("推文生成失敗")
        await context.bot.send_message(chat_id=chat_id, text=TWEET_FAILED_TEXT.format(error=exc))
        return

    logger.info("生成了推文：[%s] %s", item.domain, item.topic)
    full = item.full_text()
    fb_note = await crosspost_everywhere(full)

    try:
        url = await tweet_mod.publish(item)
    except Exception as exc:  # noqa: BLE001 - any failure is reported, never swallowed
        logger.exception("推文發布失敗")
        await context.bot.send_message(
            chat_id=chat_id,
            text=TWEET_PUBLISH_FAILED_TEXT.format(error=exc, text=full) + fb_note,
        )
        return

    logger.info("推文已發布：%s", url)

    reply_note = ""
    try:
        if await tweet_mod.post_link_reply(url):
            reply_note = TWEET_LINK_REPLY_OK
    except Exception as exc:  # noqa: BLE001 - the tweet is already out
        logger.exception("Telegram 連結回覆失敗")
        reply_note = TWEET_LINK_REPLY_FAILED.format(error=exc)

    await context.bot.send_message(
        chat_id=chat_id,
        text=TWEET_SENT_TEXT.format(domain=item.domain, text=full, url=url) + reply_note + fb_note,
    )


async def tweet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/tweet - generate and post one now, even while the schedule is paused."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        logger.info("拒絕了來自 chat %s 的 /tweet", chat_id)
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return

    note = TWEET_WORKING_TEXT
    if tweet_mod.is_paused():
        # /pause stops the schedule, not a deliberate manual post.
        note += "\n（目前排程是暫停狀態，這則是你手動發的）"
    await update.effective_message.reply_text(note)
    await produce_and_post_tweet(context, chat_id)


def split_post(body: str) -> tuple[str, str]:
    """'/post' body -> (tweet, reply). A line of just --- separates them."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i + 1:]).strip()
    return body.strip(), ""


async def post_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/post <text> — publish the owner's own text to X, exactly as written."""
    chat_id = update.effective_chat.id
    message = update.effective_message
    if not is_admin(chat_id):
        await message.reply_text(XHS_DENIED_TEXT)
        return

    body = re.sub(r"^/post(@\w+)?", "", message.text or "", count=1).strip()
    await message.reply_text(await publish_manual(body))


async def publish_manual(body: str) -> str:
    """Post the owner's own text (optionally '---' + a reply). Returns the report for the owner."""
    main, reply = split_post(body)
    if not main:
        return POST_USAGE_TEXT
    for label, text in (("推文", main), ("底下的回复", reply)):
        if text and tweet_mod.x_length(text) > tweet_mod.X_WEIGHTED_LIMIT:
            return (
                f"⚠️ {label}太长了：{tweet_mod.x_length(text)}/{tweet_mod.X_WEIGHTED_LIMIT}"
                "（中文一个字算 2，链接一条算 23）。删短一点再发。"
            )

    fb_note = await crosspost_everywhere(main)
    try:
        url = await tweet_mod.publish(tweet_mod.Tweet(domain="手動", topic=main[:60], text=main))
    except Exception as exc:  # noqa: BLE001 - tell the owner exactly why
        logger.exception("手動推文發布失敗")
        return TWEET_PUBLISH_FAILED_TEXT.format(error=exc, text=main) + fb_note
    logger.info("手動推文已發布：%s", url)

    note = ""
    try:
        if reply:
            await tweet_mod.post_reply(url, reply)
            note = "\n\n💬 底下的回复也发了"
        elif await tweet_mod.post_link_reply(url):
            note = TWEET_LINK_REPLY_OK
    except Exception as exc:  # noqa: BLE001 - the tweet itself is already out
        logger.exception("手動推文的回覆失敗")
        note = TWEET_LINK_REPLY_FAILED.format(error=exc)
    return f"🚀 已发推\n\n{main}\n\n———\n{url}{note}{fb_note}"


async def post_on_start_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Publish X_POST_ON_START once, then tell the owner. Clear the variable afterwards;
    a restart before that can't double-post, since X rejects duplicate text."""
    report = await publish_manual(X_POST_ON_START)
    logger.info("X_POST_ON_START 处理完毕")
    if ADMIN_CHAT_ID:
        await context.bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=report)


async def tweet_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The 12:00 and 20:00 Asia/Taipei runs."""
    chat_id = int(ADMIN_CHAT_ID)
    if tweet_mod.is_paused():
        logger.info("自動發推已暫停，跳過這次排程")
        await context.bot.send_message(chat_id=chat_id, text=TWEET_SKIPPED_TEXT)
        return
    logger.info("每日推文任務觸發，送到 chat %s", chat_id)
    await produce_and_post_tweet(context, chat_id)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pause - stop the scheduled posting."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    if tweet_mod.is_paused():
        await update.effective_message.reply_text(TWEET_ALREADY_PAUSED)
        return
    tweet_mod.set_paused(True)
    await update.effective_message.reply_text(TWEET_PAUSED_TEXT)


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/resume - start the scheduled posting again."""
    chat_id = update.effective_chat.id
    if not is_admin(chat_id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    if not tweet_mod.is_paused():
        await update.effective_message.reply_text(TWEET_ALREADY_RUNNING)
        return
    tweet_mod.set_paused(False)
    await update.effective_message.reply_text(TWEET_RESUMED_TEXT)


# --------------------------------------------------------------------------- #
# Customer-service mode — the Telegram entry. The logic lives in customer_service.py
# --------------------------------------------------------------------------- #

# A client's bot sets its own greeting in CS_WELCOME / CS_WELCOME_EN (\n for new lines).
CS_WELCOME_TEXT = os.environ.get("CS_WELCOME", "").replace("\\n", "\n").strip() or (
    "你好呀～我是博主的助理 👋\n\n"
    "想做 PPT、写代码、写文案还是做个简单网站？跟我说说要做什么、什么时候要、预算大概多少，"
    "我帮你问清楚，再请本人给你报价～"
)
CS_WELCOME_TEXT_EN = os.environ.get("CS_WELCOME_EN", "").replace("\\n", "\n").strip() or (
    "Hi there! I'm the assistant here 👋\n\n"
    "Need slides, some code, copywriting or a simple website? Tell me what you need, "
    "your deadline and your budget, and I'll get the owner to quote you. "
    "Feel free to write in any language."
)
CS_ERROR_TEXT = "不好意思，我这边出了点小问题，我请本人来跟你确认，稍等哦"
CS_ERROR_TEXT_EN = "Sorry, something went wrong on my side. Let me get the owner to help you, one moment please."


def customer_lang(update: Update) -> str:
    """'zh' if the customer's Telegram is set to Chinese, otherwise '' (unknown).

    Never 'en': Telegram has no official Chinese interface, so plenty of Chinese
    speakers run it in English. What they actually write decides the rest.
    """
    user = update.effective_user
    code = (getattr(user, "language_code", "") or "").lower()
    return "zh" if code.startswith("zh") else ""


def for_customer(update: Update, zh: str, en: str) -> str:
    """Chinese for a Chinese Telegram, otherwise both, Chinese first."""
    return zh if customer_lang(update) == "zh" else f"{zh}\n\n———\n\n{en}"
OWNER_NOTICE_LIMIT = 4000

# Set in main() (or by tests). None means customer-service mode is off.
service: cs.CustomerService | None = None


def customer_mode_for(update: Update) -> bool:
    """Customer mode is for private chats with anyone who isn't the owner."""
    chat = update.effective_chat
    return (
        service is not None
        and chat is not None
        and chat.type == "private"
        and not is_owner(chat.id)
    )


def _inbound(update: Update, text: str) -> cs.Inbound:
    user = update.effective_user
    return cs.Inbound(
        channel="telegram",
        chat_id=str(update.effective_chat.id),
        text=text,
        username=(user.username or "") if user else "",
        display_name=(user.full_name or "") if user else "",
        lang=customer_lang(update),
    )


async def customer_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except TelegramError:
        logger.debug("Could not send typing action", exc_info=True)
    reply = await service.handle(_inbound(update, message.text))
    if reply:
        for chunk in split_message(reply):
            await message.reply_text(chunk)


def _attachment_kind(message) -> str:
    for attr, label in (("photo", "图片"), ("document", "文件"), ("voice", "语音"),
                        ("video", "视频"), ("audio", "音频"), ("sticker", "表情"),
                        ("video_note", "视频"), ("animation", "动图")):
        if getattr(message, attr, None):
            return label
    return "附件"


async def route_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Photos, files, voice: the owner's go to a customer, a customer's go to the owner."""
    message = update.effective_message
    if message is None or await refused(update):
        return
    if is_owner(update.effective_chat.id):
        await forward_owner_reply(update, context)
        return
    if not customer_mode_for(update):
        return
    reply, owner_message_id = await service.handle_attachment(
        _inbound(update, message.caption or ""), _attachment_kind(message)
    )
    if owner_message_id is not None:
        try:
            copied = await context.bot.copy_message(
                chat_id=int(OWNER_CHAT_ID), from_chat_id=update.effective_chat.id,
                message_id=message.message_id, reply_to_message_id=owner_message_id,
            )
            service.link_owner_message(copied.message_id, "telegram", str(update.effective_chat.id))
        except TelegramError:
            logger.exception("客户文件转给本人失败")
    await message.reply_text(reply)


async def forward_owner_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """If the owner replied to a customer notice, pass the reply on. True if handled."""
    message = update.effective_message
    target = message.reply_to_message if message else None
    if service is None or target is None:
        return False
    key = service.lookup_owner_message(target.message_id)
    if key is None:
        return False
    try:
        if message.text:
            await service.deliver_owner_reply(target.message_id, message.text)
        elif key[0] == "telegram":
            await context.bot.copy_message(chat_id=int(key[1]), from_chat_id=message.chat_id,
                                           message_id=message.message_id)
            service.record_owner_message(*key, f"[{_attachment_kind(message)}]")
        else:
            await message.reply_text(f"⚠️ {key[0]} 渠道暂时只能转文字")
            return True
    except Exception as exc:  # noqa: BLE001 - tell the owner rather than fail silently
        logger.exception("转发给客户失败")
        await message.reply_text(f"⚠️ 没转成功：{exc}")
        return True
    ref = key[1] if key[0] == "telegram" else f"{key[0]}:{key[1]}"
    await message.reply_text(f"✅ 已转给客户 {ref}")
    return True


async def ai_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ai <chat_id> on|off — pause or resume AI replies for one customer."""
    if not is_owner(update.effective_chat.id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    if service is None:
        await update.effective_message.reply_text("客服模式没有启用。")
        return
    args = context.args or []
    if len(args) != 2 or args[1].lower() not in ("on", "off"):
        await update.effective_message.reply_text("用法：/ai <chat_id> on 或 /ai <chat_id> off")
        return
    enabled = args[1].lower() == "on"
    found, key = service.set_ai(args[0], enabled)
    if not found:
        await update.effective_message.reply_text(f"找不到客户 {args[0]}，用 /customers 看看 chat ID")
        return
    state = "恢复" if enabled else "暂停"
    extra = "" if enabled else "\n之后这位客户的消息会直接转给你，回复那条消息就能回客户。"
    await update.effective_message.reply_text(f"✅ 已{state}对客户 {args[0]} 的 AI 回复{extra}")


async def customers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/customers — recent customers, what they want, when they last wrote."""
    if not is_owner(update.effective_chat.id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    if service is None:
        await update.effective_message.reply_text("客服模式没有启用。")
        return
    for chunk in split_message(service.customers_report()):
        await update.effective_message.reply_text(chunk)


# Set in post_init together with the website chat. None means no visitor numbers.
visits: visits_mod.Visits | None = None


def owner_device_links() -> str:
    token = visits.owner_token() if visits is not None else ""
    links = "\n".join(f"{url}{'&' if '?' in url else '?'}me={token}" for url in SITE_URLS)
    return ("把你自己的设备标记一下，你自己看网站就不会算进访客里。"
            "在你的手机和 iPad 上，这几个链接各点开一次就行（网页会弹出“好了 ✅”）：\n" + links)


async def visits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/visits — who looked at the website yesterday, today so far, and this week."""
    if not is_owner(update.effective_chat.id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    if visits is None:
        await update.effective_message.reply_text("访客统计没有启用（网站没有开）。")
        return
    text = visits.report(ZoneInfo(VISITS_TIMEZONE), today_too=True)
    text += f"\n\n你已经标记了 {visits.owner_devices()} 个自己的设备。\n" + owner_device_links()
    for chunk in split_message(text):
        await update.effective_message.reply_text(chunk, disable_web_page_preview=True)


async def visits_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if visits is None or not OWNER_CHAT_ID:
        return
    text = visits.report(ZoneInfo(VISITS_TIMEZONE))
    if not visits.owner_devices():
        text += "\n\n" + owner_device_links()
    try:
        await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text, disable_web_page_preview=True)
    except TelegramError:
        logger.exception("每日访客报告发送失败")


async def send_owner_links_once(application: Application) -> None:
    """Right after the visitor count first goes live, tell the owner how to leave themselves out."""
    if visits is None or not OWNER_CHAT_ID or not visits.take_flag("owner_links_sent"):
        return
    try:
        await application.bot.send_message(
            chat_id=int(OWNER_CHAT_ID), disable_web_page_preview=True,
            text="📊 网站访客统计开好了：每天早上 " + VISITS_REPORT_TIME + " 告诉你昨天几个人来看、从哪来、"
                 "有没有人跟 AI 客服聊。随时发 /visits 看最新的。\n\n" + owner_device_links())
    except TelegramError:
        logger.exception("访客统计说明发送失败")


# --- the business intro video (media/), sent to the owner once and on /video ---------------
INTRO_VIDEOS = [
    (Path(__file__).with_name("media") / "vinc-intro.mp4",
     "🎬 嘻哈版业务介绍视频 · 一般版：发 X、Telegram 频道、给客人看用这个。"),
    (Path(__file__).with_name("media") / "vinc-intro-fiverr.mp4",
     "🎬 Fiverr 版：结尾没有联系方式，放进 Fiverr gig 的视频栏用这个。"),
]


async def send_intro_videos(bot, chat_id: int) -> None:
    for path, caption in INTRO_VIDEOS:
        if not path.is_file():
            continue
        with path.open("rb") as video:
            await bot.send_video(chat_id=chat_id, video=video, caption=caption,
                                 width=1920, height=1080, supports_streaming=True)


async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/video — the business intro videos, ready to save or forward."""
    if not is_owner(update.effective_chat.id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    try:
        await send_intro_videos(context.bot, update.effective_chat.id)
    except TelegramError:
        logger.exception("介绍视频发送失败")
        await update.effective_message.reply_text("视频发送失败了，过一会儿再发 /video 试试。")


async def send_intro_videos_once(application: Application) -> None:
    """The business bot sends the owner the new intro videos once, so they needn't ask."""
    if visits is None or service is None or not OWNER_CHAT_ID or not visits.take_flag("intro_video_v3"):
        return
    try:
        await send_intro_videos(application.bot, int(OWNER_CHAT_ID))
    except TelegramError:
        logger.exception("介绍视频发送失败")


def schedule_visits_report(application: Application) -> None:
    if not OWNER_CHAT_ID or application.job_queue is None:
        return
    try:
        hour, minute = (int(part) for part in VISITS_REPORT_TIME.split(":"))
        when = dt.time(hour=hour, minute=minute, tzinfo=ZoneInfo(VISITS_TIMEZONE))
    except (ValueError, KeyError):
        logger.error("VISITS_REPORT_TIME=%r 或 VISITS_TIMEZONE=%r 无法解析，每日访客报告跳过",
                     VISITS_REPORT_TIME, VISITS_TIMEZONE)
        return
    application.job_queue.run_daily(visits_daily_job, time=when, name="visits-daily")
    logger.info("每日访客报告：%s %s", VISITS_REPORT_TIME, VISITS_TIMEZONE)


# --- cold emails, sent from the owner's Gmail ---------------------------------------------
leads_db: outreach_mod.Leads | None = None
MAIL_WEBHOOK_URL = os.environ.get("MAIL_WEBHOOK_URL", "").strip()
MAIL_SETUP_TEXT = (
    "📧 让我用你的 Gmail 自动发邮件，只要设置一次（5 分钟，要开 VPN）：\n\n"
    "1. Safari 打开 https://script.google.com ，登录你的 Gmail，点左上角「新项目 / New project」\n"
    "2. 把编辑框里原来的字全删掉，粘贴我下一条发的代码，点上面的💾保存\n"
    "3. 点右上角蓝色「部署 / Deploy」→「新部署 / New deployment」→ 左边齿轮⚙️ 选「网页应用 / Web app」\n"
    "4. 「执行身份」选「我」，「谁可以访问」选「任何人 / Anyone」→ 点「部署」\n"
    "5. 点「授权访问」→ 选你的账号 → 出现「Google 未验证此应用」就点「高级 / Advanced」→「转至…（不安全）」→「允许」\n"
    "   （这是你自己的脚本，只有你的机器人知道密码，放心）\n"
    "6. 复制最后出现的「网页应用网址」（结尾是 /exec），直接发给我\n\n"
    "邮件从你自己的 Gmail 发出，已发送里看得到，对方回复也会进你的收件箱，我会在这里提醒你。"
)


def mailer() -> outreach_mod.Mailer | None:
    if leads_db is None:
        return None
    url = MAIL_WEBHOOK_URL or leads_db.setting("url")
    return outreach_mod.Mailer(url, leads_db.secret()) if url else None


async def mail_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mail — connect the owner's Gmail (or check it is still connected)."""
    if not is_owner(update.effective_chat.id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    if leads_db is None:
        await update.effective_message.reply_text("邮件功能没有启用。")
        return
    box = mailer()
    if box is not None:
        try:
            email = await box.ping()
        except outreach_mod.MailError as exc:
            await update.effective_message.reply_text(f"⚠️ 之前接的 Gmail 现在用不了：{exc}\n\n照下面重新弄一次：")
        else:
            await update.effective_message.reply_text(f"✅ Gmail 已经接好了（{email}）。发 /outreach 看今天要发的邮件。")
            return
    await update.effective_message.reply_text(MAIL_SETUP_TEXT, disable_web_page_preview=True)
    await update.effective_message.reply_text(outreach_mod.script_for(leads_db.secret()))


async def connect_mailer(update: Update) -> bool:
    """The owner pasted their Apps Script /exec link: check it works and keep it."""
    found = outreach_mod.SCRIPT_URL.search(update.effective_message.text or "")
    if not found or leads_db is None:
        return False
    box = outreach_mod.Mailer(found.group(0), leads_db.secret())
    try:
        email = await box.ping()
    except outreach_mod.MailError as exc:
        await update.effective_message.reply_text(
            f"⚠️ 这个链接还用不了：{exc}\n\n检查一下代码是不是我发的最新那份、「谁可以访问」是不是「任何人」。发 /mail 可以重新看步骤。")
        return True
    leads_db.set_setting("url", found.group(0))
    after = ("我自动发，发完告诉你" if outreach_mod.AUTO else "我把要发的邮件给你看，你点 ✅ 我就发")
    await update.effective_message.reply_text(f"✅ Gmail 接好了（{email}）！以后每天 {outreach_mod.SEND_TIME} {after}。")
    await offer_emails(update.get_bot(), manual=True)
    return True


def _offer_text(batch: list[dict]) -> str:
    subject, body = outreach_mod.compose(batch[0])
    lines = [f"{i}. {lead['name']} · {lead['email']}" for i, lead in enumerate(batch, 1)]
    return (f"📧 今天要发这 {len(batch)} 封（每天最多 {outreach_mod.DAILY_LIMIT} 封，每封隔一会儿发）：\n\n"
            + "\n".join(lines)
            + f"\n\n———— 第一封长这样 ————\n标题：{subject}\n\n{body}"
            + "\n\n每封开头那句都按店家改好了。点 ✅ 就开始发。")


async def offer_emails(bot, manual: bool = False) -> None:
    """Show the owner today's batch with ✅ / ❌. Nothing is sent without the tap."""
    if leads_db is None or not OWNER_CHAT_ID or mailer() is None:
        return
    batch = leads_db.due()
    if not batch:
        if manual:
            left = leads_db.counts().get("new", 0)
            text = (f"今天的 {outreach_mod.DAILY_LIMIT} 封已经发完了，剩下 {left} 家明天再发。" if left
                    else "名单里的商家都发过了。要新名单就跟 Claude 说一声。")
            await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text)
        return
    if outreach_mod.AUTO:
        lines = "\n".join(f"{i}. {lead['name']} · {lead['email']}" for i, lead in enumerate(batch, 1))
        note = await bot.send_message(
            chat_id=int(OWNER_CHAT_ID), disable_web_page_preview=True,
            text=f"📧 自动发今天的 {len(batch)} 封邮件（每封隔一会儿）：\n\n{lines}\n\n想停就发 /outreach_stop。")
        start_sending(note, [lead["email"] for lead in batch])
        return
    offer = secrets.token_hex(4)
    leads_db.set_setting(f"offer:{offer}", json.dumps([lead["email"] for lead in batch]))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"✅ 发这 {len(batch)} 封", callback_data=f"mail:{offer}:go"),
        InlineKeyboardButton("❌ 今天不发", callback_data=f"mail:{offer}:no"),
    ]])
    text = _offer_text(batch)
    if len(text) > TELEGRAM_MESSAGE_LIMIT:
        text = text[:TELEGRAM_MESSAGE_LIMIT - 20] + "\n…"
    await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text, reply_markup=keyboard,
                           disable_web_page_preview=True)


async def mail_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, offer, action = (query.data or "::").split(":", 2)
    raw = leads_db.setting(f"offer:{offer}") if leads_db is not None else ""
    if not is_owner(query.message.chat_id) or not raw:
        await query.answer("这批已经处理过了", show_alert=True)
        return
    await query.answer()
    leads_db.set_setting(f"offer:{offer}", "")
    if action != "go":
        await query.edit_message_text("好，今天不发。明天同一时间再问你，想现在发就发 /outreach。")
        return
    emails = json.loads(raw)
    await query.edit_message_text(f"🚀 开始发了，一共 {len(emails)} 封，每封隔一会儿，发完告诉你。")
    start_sending(query.message, emails)


def start_sending(message, emails: list[str]) -> None:
    task = asyncio.get_running_loop().create_task(send_emails(message, emails))
    fb_setup_tasks.add(task)
    task.add_done_callback(fb_setup_tasks.discard)


async def send_emails(message, emails: list[str]) -> None:
    box, done, problem, failed_in_a_row = mailer(), [], "", 0
    for email in emails:
        if leads_db.setting("stopped"):
            problem = "你让我停了，剩下的没发。发 /outreach 可以重新开始。"
            break
        lead = leads_db.get(email)
        if lead is None or lead["status"] != "new":
            continue
        if leads_db.room_today() <= 0:
            problem = f"今天已经发满 {outreach_mod.DAILY_LIMIT} 封，剩下的明天发。"
            break
        if done or failed_in_a_row:
            await asyncio.sleep(outreach_mod.GAP_SECONDS)
        subject, body = outreach_mod.compose(lead)
        try:
            await box.send(email, subject, body)
        except outreach_mod.MailError as exc:
            logger.warning("邮件没发出去（%s）：%s", email, exc)
            failed_in_a_row += 1
            if failed_in_a_row >= 2:
                # twice in a row is Gmail or the script, not one bad address: keep the rest for later
                problem = f"发到 {lead['name']} 时又出错了，后面的先停了：{exc}"
                break
            leads_db.mark(email, "failed", str(exc)[:200])
            done.append(f"⚠️ {lead['name']} 发不出去（{exc}）")
            continue
        failed_in_a_row = 0
        leads_db.mark(email, "sent")
        done.append(f"✅ {lead['name']}")
    ok = sum(line.startswith("✅") for line in done)
    text = f"📧 发好了 {ok} 封：\n" + "\n".join(done) if done else "📧 这次一封都没发出去。"
    if problem:
        text += f"\n\n⚠️ {problem}"
    text += "\n\n有人回复我会马上告诉你。"
    try:
        await message.reply_text(text)
    except TelegramError:
        logger.exception("邮件发送结果没能告诉本人")


async def outreach_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/outreach — the cold email numbers, and today's batch if there is one."""
    if not is_owner(update.effective_chat.id):
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    if leads_db is None:
        await update.effective_message.reply_text("邮件功能没有启用。")
        return
    leads_db.set_setting("stopped", "")
    counts = leads_db.counts()
    await update.effective_message.reply_text(
        f"📧 名单一共 {sum(counts.values())} 家：已发 {sum(counts.values()) - counts.get('new', 0) - counts.get('failed', 0)}，"
        f"有回复 {counts.get('replied', 0)}，不要了 {counts.get('optout', 0)}，退信 {counts.get('bounced', 0)}，"
        f"发不出去 {counts.get('failed', 0)}，"
        f"还没发 {counts.get('new', 0)}。")
    if mailer() is None:
        await update.effective_message.reply_text("还没接 Gmail。发 /mail，照着弄一次就能自动发。")
        return
    await offer_emails(context.bot, manual=True)


async def outreach_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/outreach_stop — no more cold emails until /outreach."""
    if not is_owner(update.effective_chat.id) or leads_db is None:
        await update.effective_message.reply_text(XHS_DENIED_TEXT)
        return
    leads_db.set_setting("stopped", "1")
    await update.effective_message.reply_text("⏸ 好，邮件停了。想重新开始就发 /outreach。")


async def send_mail_setup_once(bot) -> None:
    """Gmail isn't connected yet: send the owner the steps once, so they needn't ask."""
    if leads_db is None or not OWNER_CHAT_ID or mailer() is not None or not leads_db.counts().get("new"):
        return
    if leads_db.setting("setup_sent"):
        return
    leads_db.set_setting("setup_sent", "1")
    try:
        await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=MAIL_SETUP_TEXT, disable_web_page_preview=True)
        await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=outreach_mod.script_for(leads_db.secret()))
    except TelegramError:
        logger.exception("Gmail 设置步骤发送失败")


async def outreach_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if leads_db is not None and leads_db.setting("stopped"):
        return
    try:
        await offer_emails(context.bot)
    except Exception:  # noqa: BLE001 - tomorrow is another try
        logger.exception("今天的邮件没能给本人看")


async def email_replies_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tell the owner when a business answers (or the address bounced)."""
    box = mailer()
    if box is None or not OWNER_CHAT_ID:
        return
    try:
        found = await box.replies(leads_db.emailed())
    except outreach_mod.MailError as exc:
        logger.warning("查邮件回复失败：%s", exc)
        return
    for reply in found:
        email = str(reply.get("lead", "")).lower()
        lead = leads_db.get(email)
        if lead is None or not leads_db.first_sight(str(reply.get("id")), email):
            continue
        if reply.get("bounce"):
            leads_db.mark(email, "bounced")
            text = f"⚠️ 发给 {lead['name']}（{email}）的邮件被退回来了，这个邮箱可能不用了。不用管它。"
        else:
            no = bool(outreach_mod.OPT_OUT.search(reply.get("text", "")[:400]))
            leads_db.mark(email, "optout" if no else "replied")
            head = (f"🙅 {lead['name']} 回复说不需要，不会再发给他们了。" if no
                    else f"📬 {lead['name']} 回你邮件了！去 Gmail 回复他们（要我帮你写就截图给 Claude）：")
            text = f"{head}\n\n标题：{reply.get('subject', '')}\n\n{str(reply.get('text', ''))[:1500]}"
        try:
            await context.bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text, disable_web_page_preview=True)
        except TelegramError:
            logger.exception("邮件回复提醒发送失败")


def start_outreach() -> None:
    global leads_db
    if not OWNER_CHAT_ID or web.WEB_SITES_ONLY:
        return
    try:
        leads_db = outreach_mod.Leads(cs.CS_DB_PATH)
        added = leads_db.add(outreach_mod.parse_leads(os.environ.get("OUTREACH_LEADS", "")))
    except Exception:  # noqa: BLE001 - the bot runs without emails
        logger.exception("邮件名单打不开，自动发邮件关闭")
        leads_db = None
        return
    if added:
        logger.info("邮件名单新加了 %d 家", added)


def schedule_outreach(application: Application) -> None:
    if not OWNER_CHAT_ID or application.job_queue is None:
        return
    try:
        hour, minute = (int(part) for part in outreach_mod.SEND_TIME.split(":"))
        when = dt.time(hour=hour, minute=minute, tzinfo=ZoneInfo(outreach_mod.TIMEZONE))
    except (ValueError, KeyError):
        logger.error("OUTREACH_TIME=%r 无法解析，每天的邮件不会自动问你", outreach_mod.SEND_TIME)
    else:
        application.job_queue.run_daily(outreach_daily_job, time=when, name="outreach-daily")
    application.job_queue.run_repeating(email_replies_job, interval=1800, first=120, name="email-replies")


def _fit_notice(text: str) -> str:
    """Owner notices carry a transcript; keep the head (who/why) and the newest lines."""
    if len(text) <= OWNER_NOTICE_LIMIT:
        return text
    return text[:1200] + "\n…（中间省略）…\n" + text[-(OWNER_NOTICE_LIMIT - 1300):]


def build_customer_service(bot) -> cs.CustomerService | None:
    """Wire customer_service.py to Telegram. None (mode off) without an owner to hand off to."""
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID 和 ADMIN_CHAT_ID 都没设置，客服模式关闭，所有人照旧聊天")
        return None

    try:
        catalog = cs.load_catalog()
    except Exception:  # noqa: BLE001 - a typo in products.yaml must not take the bot down
        logger.exception("products.yaml 读取失败，客服 AI 停用，客户消息会全部转给你")
        catalog = ""

    responder, model = None, ""
    if not catalog:
        pass
    elif ANTHROPIC_API_KEY:
        responder, model = cs.ClaudeResponder(ANTHROPIC_API_KEY), cs.CS_MODEL
    elif DEEPSEEK_API_KEY:
        responder, model = cs.OpenAICompatibleResponder(DEEPSEEK_API_KEY), cs.CS_DEEPSEEK_MODEL
    else:
        logger.warning("没有 ANTHROPIC_API_KEY 也没有 DEEPSEEK_API_KEY，客服 AI 停用，客户消息会全部转给你")

    async def notify_owner(text: str) -> int | None:
        sent = await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=_fit_notice(text))
        return sent.message_id

    async def send_telegram(chat_id: str, text: str) -> None:
        for chunk in split_message(text):
            await bot.send_message(chat_id=int(chat_id), text=chunk)

    svc = cs.CustomerService(cs.Store(), catalog, responder, owner_notify=notify_owner)
    svc.register_channel("telegram", send_telegram)
    logger.info("客服模式已启用：AI %s，本人 chat %s",
                f"开（{model}）" if responder else "关（只转人工）", OWNER_CHAT_ID)
    return svc


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all so an unexpected failure never takes the bot down."""
    logger.exception("Unhandled exception while processing update", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        if customer_mode_for(update):
            text = for_customer(update, CS_ERROR_TEXT, CS_ERROR_TEXT_EN)
        else:
            text = GENERIC_ERROR_TEXT
        try:
            await update.effective_message.reply_text(text)
        except TelegramError:
            logger.debug("Could not report the error back to the chat", exc_info=True)


def schedule_daily_note(application: Application) -> None:
    """Run the note job every day at XHS_DAILY_TIME in XHS_TIMEZONE."""
    if not ADMIN_CHAT_ID:
        logger.warning("ADMIN_CHAT_ID 没有设置，/xhs 和每日任务都不会启用")
        return
    if application.job_queue is None:
        logger.error(
            "JobQueue 不可用，每日任务无法排程。"
            "请安装 python-telegram-bot[job-queue]（见 requirements.txt）。"
        )
        return

    try:
        hour, minute = (int(part) for part in XHS_DAILY_TIME.split(":"))
        tz = ZoneInfo(XHS_TIMEZONE)
        when = dt.time(hour=hour, minute=minute, tzinfo=tz)
    except (ValueError, KeyError):
        logger.error(
            "XHS_DAILY_TIME=%r 或 XHS_TIMEZONE=%r 无法解析，每日任务跳过",
            XHS_DAILY_TIME, XHS_TIMEZONE, exc_info=True,
        )
        return

    application.job_queue.run_daily(xhs_daily_job, time=when, name="xhs-daily")
    logger.info("每日小红书任务已排程：%s %s，发送到 chat %s",
                XHS_DAILY_TIME, XHS_TIMEZONE, ADMIN_CHAT_ID)


def schedule_daily_tweets(application: Application) -> None:
    """Run the tweet job at each time in X_DAILY_TIMES, every day."""
    if not ADMIN_CHAT_ID:
        logger.warning("ADMIN_CHAT_ID 沒有設定，/tweet 和每日推文都不會啟用")
        return
    if application.job_queue is None:
        logger.error("JobQueue 不可用，每日推文無法排程。"
                     "請安裝 python-telegram-bot[job-queue]（見 requirements.txt）。")
        return

    try:
        tz = ZoneInfo(X_TIMEZONE)
    except (ValueError, KeyError):
        logger.error("X_TIMEZONE=%r 無法解析，每日推文跳過", X_TIMEZONE, exc_info=True)
        return

    scheduled = []
    for chunk in X_DAILY_TIMES.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            hour, minute = (int(part) for part in chunk.split(":"))
            when = dt.time(hour=hour, minute=minute, tzinfo=tz)
        except ValueError:
            logger.error("X_DAILY_TIMES 裡的 %r 無法解析，略過這一項", chunk)
            continue
        application.job_queue.run_daily(
            tweet_daily_job, time=when, name=f"tweet-daily-{hour:02d}{minute:02d}"
        )
        scheduled.append(f"{hour:02d}:{minute:02d}")

    if scheduled:
        logger.info("每日推文已排程：%s %s，送到 chat %s",
                    "、".join(scheduled), tz.key, ADMIN_CHAT_ID)
    else:
        logger.error("X_DAILY_TIMES=%r 沒有任何有效時間，每日推文未排程", X_DAILY_TIMES)


# A variable left at its .env.example value ("your-telegram-bot-token") is worse
# than a missing one: it looks set, so the failure surfaces deep inside a library
# instead of at startup. Catch both here and name the variable in the message.
PLACEHOLDER_PATTERN = re.compile(
    r"^(your[-_ ]|<|xxx|changeme|change[-_]me|replace[-_ ]me|todo|dummy|placeholder)",
    re.IGNORECASE,
)
# Same shape python-telegram-bot requires before it raises InvalidToken.
TELEGRAM_TOKEN_PATTERN = re.compile(r"^\d+:[\w-]{20,}$")


def check_env(name: str, value: str) -> str:
    """Return a human-readable problem with this variable, or "" when it looks fine."""
    value = (value or "").strip()
    if not value:
        return f"{name} 沒有設定（環境變數是空的）"
    if PLACEHOLDER_PATTERN.match(value):
        return f"{name} 還是範例裡的佔位字串 {value!r}，請換成真正的值"
    if name == "TELEGRAM_BOT_TOKEN" and not TELEGRAM_TOKEN_PATTERN.match(value):
        # Never log the whole value here: it may be a real, merely mistyped token.
        return (
            f"{name}（開頭是 {value[:6]!r}）不像 Telegram token。"
            "正確格式是 @BotFather 給的 123456789:AA... （數字、冒號、一長串英數字）"
        )
    return ""


# Set in post_init when customer service is on. None means no website chat.
web_chat: web.WebChat | None = None
fb_setup_tasks: set[asyncio.Task] = set()
# The bot itself, for posting in the owner's Telegram channel. Set in post_init.
telegram_bot = None


async def post_init(application: Application) -> None:
    """Learn the bot's own @username, so X_TELEGRAM_LINK=bot can point tweets at it,
    then open the website chat window."""
    global telegram_bot
    telegram_bot = application.bot
    username = ""
    try:
        me = await application.bot.get_me()
        username = me.username or ""
    except TelegramError:
        logger.exception("拿不到机器人自己的用户名，X_TELEGRAM_LINK=bot 暂时不会生效")
    else:
        tweet_mod.set_bot_username(username)
        link = tweet_mod.telegram_link()
        logger.info("机器人是 @%s；推文底下的 Telegram 链接：%s", username, link or "（没设置）")
    schedule_post_on_start(application)
    await start_web_chat(username, application.bot)
    await send_owner_links_once(application)
    await send_intro_videos_once(application)
    await hello_channel_once()
    await hello_bluesky_once()
    await send_mail_setup_once(application.bot)


async def start_web_chat(bot_username: str = "", bot=None) -> None:
    """The website chat window shares the customer service (and its owner notices)."""
    global web_chat, visits, site_store
    if not web.WEB_CHAT:
        return
    if service is None or web.WEB_SITES_ONLY:
        # A personal-assistant bot: only the websites its agent builds are served.
        if not web.WEB_SITES_ONLY:
            return
        try:
            site_store = agent_mod.SiteStore(cs.CS_DB_PATH)
            chat = web.WebChat(None, sites=site_store)
            await chat.start()
        except Exception:  # noqa: BLE001 - the chat works without websites
            logger.exception("网站服务启动失败，助理不能建网站")
            return
        web_chat = chat
        return
    contact = f"https://t.me/{bot_username}" if bot_username else ""
    try:
        counter = visits_mod.Visits(cs.CS_DB_PATH)
    except Exception:  # noqa: BLE001 - the chat window matters more than the numbers
        logger.exception("访客统计打不开，网站照常运行")
        counter = None
    async def tell_owner(text: str) -> None:
        if bot is not None and OWNER_CHAT_ID:
            await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=text, disable_web_page_preview=True)

    fb = messenger_mod.Messenger.from_env(service, notify=tell_owner)
    try:
        site_store = agent_mod.SiteStore(cs.CS_DB_PATH)
    except Exception:  # noqa: BLE001 - chat still works without the agent's websites
        logger.exception("网站仓库打不开，助理不能建网站")
        site_store = None
    chat = web.WebChat(service, title=web.shop_name(), contact_link=contact, visits=counter, messenger=fb,
                       sites=site_store)
    try:
        await chat.start()
    except OSError:
        logger.exception("网站聊天窗口启动失败（端口 %s 被占用？），Telegram 机器人照常运行", web.WEB_PORT)
        return
    web_chat = chat
    visits = counter
    if fb is not None:
        # Meta calls our webhook back while we register it, so the server must be up first.
        fb_setup_tasks.add(asyncio.get_running_loop().create_task(fb.connect()))


async def post_shutdown(application: Application) -> None:
    if web_chat is not None:
        if web_chat.messenger is not None:
            await web_chat.messenger.close()
        await web_chat.stop()


def schedule_post_on_start(application: Application) -> None:
    if X_POST_ON_START and application.job_queue is not None:
        application.job_queue.run_once(post_on_start_job, when=10, name="x-post-on-start")
        logger.info("X_POST_ON_START 已设置，10 秒后发布")


def main() -> None:
    problems = [
        problem
        for problem in (
            check_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            check_env("OPENROUTER_API_KEY", OPENROUTER_API_KEY),
        )
        if problem
    ]
    if problems:
        raise SystemExit(
            "環境變數有問題，無法啟動：\n  - "
            + "\n  - ".join(problems)
            + "\n請到部署平台（Railway → 專案 → Variables）把上面列出的變數設成真正的值。"
        )

    if llm.USE_DEEPSEEK:
        logger.info("聊天、小红书、推文都先用 DeepSeek（%s），失败再用 OpenRouter 免费模型", llm.DEEPSEEK_MODEL)
    else:
        logger.info("Starting bot with model %s via %s", MODEL, OPENROUTER_BASE_URL)

    # Updates are handled concurrently: a /tweet or /xhs waiting minutes on a free
    # model must not hold up customers or the owner's chat. Every handler that
    # shares state per chat already takes a per-chat lock.
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CallbackQueryHandler(agent_post_button, pattern=r"^agpost:"))
    application.add_handler(CommandHandler("sites", sites_command))
    application.add_handler(CommandHandler("tweet", tweet_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("customers", customers_command))
    application.add_handler(CommandHandler("visits", visits_command))
    application.add_handler(CommandHandler("video", video_command))
    application.add_handler(CommandHandler("post", post_command))
    application.add_handler(CommandHandler("mail", mail_command))
    application.add_handler(CommandHandler("outreach", outreach_command))
    application.add_handler(CommandHandler("outreach_stop", outreach_stop_command))
    application.add_handler(CallbackQueryHandler(mail_button, pattern=r"^mail:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    application.add_handler(MessageHandler(filters.ATTACHMENT & ~filters.COMMAND, route_media))
    application.add_error_handler(on_error)

    global service
    service = build_customer_service(application.bot)

    # No more daily 小红书 notes: the owner turned them off. (/xhs is gone too.)
    schedule_daily_tweets(application)
    schedule_visits_report(application)
    start_outreach()
    schedule_outreach(application)

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
