"""Telegram AI chatbot backed by an OpenRouter chat model.

Reads its configuration from the environment, keeps a short rolling history for
each chat, and relays messages between Telegram and OpenRouter.
"""

import asyncio
import datetime as dt
import logging
import os
import re
from collections import defaultdict, deque
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
import tweet as tweet_mod
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
XHS_DAILY_TIME = os.environ.get("XHS_DAILY_TIME", "09:00")
XHS_TIMEZONE = os.environ.get("XHS_TIMEZONE", "Asia/Taipei")
# Tweets go out twice a day by default, in the same timezone.
X_DAILY_TIMES = os.environ.get("X_DAILY_TIMES", "12:00,20:00")
X_TIMEZONE = os.environ.get("X_TIMEZONE", "Asia/Taipei")

# One "round" is a user message plus the assistant's reply.
MAX_HISTORY_ROUNDS = int(os.environ.get("MAX_HISTORY_ROUNDS", "20"))
MAX_HISTORY_MESSAGES = MAX_HISTORY_ROUNDS * 2

# Telegram rejects text messages longer than 4096 characters.
TELEGRAM_MESSAGE_LIMIT = 4096

WELCOME_TEXT = (
    "👋 Hi! I'm an AI chatbot running on OpenRouter.\n\n"
    "Just send me a message and I'll reply. I remember the last "
    f"{MAX_HISTORY_ROUNDS} rounds of our conversation.\n\n"
    "Commands:\n"
    "/start - show this message\n"
    "/reset - clear the conversation history\n"
    f"\nCurrent model: {MODEL}"
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

    response = await client.chat.completions.create(model=MODEL, messages=messages)

    if not response.choices:
        raise RuntimeError("OpenRouter returned a response with no choices")

    reply = (response.choices[0].message.content or "").strip()
    if not reply:
        reply = "🤔 I got an empty answer from the model. Could you rephrase that?"

    # Only remember exchanges that actually completed.
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": reply})
    return reply


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    logger.info("/start from chat %s", chat_id)
    if customer_mode_for(update):
        await update.effective_message.reply_text(CS_WELCOME_TEXT)
        return
    await update.effective_message.reply_text(WELCOME_TEXT)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    histories.pop(chat_id, None)
    logger.info("/reset cleared history for chat %s", chat_id)
    await update.effective_message.reply_text("🧹 Conversation history cleared. Let's start fresh!")


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every plain text message lands here first."""
    if is_owner(update.effective_chat.id):
        if await forward_owner_reply(update, context):
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
            reply = await ask_model(chat_id, user_text)
        except RateLimitError:
            logger.warning("Rate limited by OpenRouter for chat %s", chat_id, exc_info=True)
            await message.reply_text(RATE_LIMIT_TEXT)
            return
        except APITimeoutError:
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
TWEET_RESUMED_TEXT = "▶️ 自動發推已恢復。"
TWEET_ALREADY_PAUSED = "⏸ 本來就是暫停狀態。/resume 恢復。"
TWEET_ALREADY_RUNNING = "▶️ 本來就在跑了。/pause 可以暫停。"
TWEET_SKIPPED_TEXT = "⏸ 已暫停，這次排程跳過。"


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

    try:
        url = await tweet_mod.publish(item)
    except Exception as exc:  # noqa: BLE001 - any failure is reported, never swallowed
        logger.exception("推文發布失敗")
        await context.bot.send_message(
            chat_id=chat_id,
            text=TWEET_PUBLISH_FAILED_TEXT.format(error=exc, text=full),
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
        text=TWEET_SENT_TEXT.format(domain=item.domain, text=full, url=url) + reply_note,
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

CS_WELCOME_TEXT = (
    "你好呀～我是博主的助理 👋\n\n"
    "想做 PPT、写代码、写文案还是做个简单网站？跟我说说要做什么、什么时候要、预算大概多少，"
    "我帮你问清楚，再请本人给你报价～"
)
CS_ERROR_TEXT = "不好意思，我这边出了点小问题，我请本人来跟你确认，稍等哦"
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
    if message is None:
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

    responder = None
    if not ANTHROPIC_API_KEY:
        logger.warning("没有 ANTHROPIC_API_KEY，客服 AI 停用，客户消息会全部转给你")
    elif catalog:
        responder = cs.ClaudeResponder(ANTHROPIC_API_KEY)

    async def notify_owner(text: str) -> int | None:
        sent = await bot.send_message(chat_id=int(OWNER_CHAT_ID), text=_fit_notice(text))
        return sent.message_id

    async def send_telegram(chat_id: str, text: str) -> None:
        for chunk in split_message(text):
            await bot.send_message(chat_id=int(chat_id), text=chunk)

    svc = cs.CustomerService(cs.Store(), catalog, responder, owner_notify=notify_owner)
    svc.register_channel("telegram", send_telegram)
    logger.info("客服模式已启用：模型 %s，AI %s，本人 chat %s",
                cs.CS_MODEL, "开" if responder else "关（只转人工）", OWNER_CHAT_ID)
    return svc


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all so an unexpected failure never takes the bot down."""
    logger.exception("Unhandled exception while processing update", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        text = CS_ERROR_TEXT if customer_mode_for(update) else GENERIC_ERROR_TEXT
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

    logger.info("Starting bot with model %s via %s", MODEL, OPENROUTER_BASE_URL)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CommandHandler("xhs", xhs_command))
    application.add_handler(CallbackQueryHandler(xhs_button, pattern=r"^xhs:"))
    application.add_handler(CommandHandler("tweet", tweet_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("ai", ai_command))
    application.add_handler(CommandHandler("customers", customers_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    application.add_handler(MessageHandler(filters.ATTACHMENT & ~filters.COMMAND, route_media))
    application.add_error_handler(on_error)

    global service
    service = build_customer_service(application.bot)

    schedule_daily_note(application)
    schedule_daily_tweets(application)

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
