"""Telegram AI chatbot backed by an OpenRouter chat model.

Reads its configuration from the environment, keeps a short rolling history for
each chat, and relays messages between Telegram and OpenRouter.
"""

import asyncio
import logging
import os
from collections import defaultdict, deque

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
MODEL = os.environ.get("MODEL", "deepseek/deepseek-chat-v3.1:free")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful assistant chatting with a user on Telegram. "
    "Keep answers clear and concise, and reply in the user's language.",
)
REQUEST_TIMEOUT = float(os.environ.get("REQUEST_TIMEOUT", "60"))

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
    await update.effective_message.reply_text(WELCOME_TEXT)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    histories.pop(chat_id, None)
    logger.info("/reset cleared history for chat %s", chat_id)
    await update.effective_message.reply_text("🧹 Conversation history cleared. Let's start fresh!")


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


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catch-all so an unexpected failure never takes the bot down."""
    logger.exception("Unhandled exception while processing update", exc_info=context.error)

    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(GENERIC_ERROR_TEXT)
        except TelegramError:
            logger.debug("Could not report the error back to the chat", exc_info=True)


def main() -> None:
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("OPENROUTER_API_KEY", OPENROUTER_API_KEY),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "See .env.example for the full list."
        )

    logger.info("Starting bot with model %s via %s", MODEL, OPENROUTER_BASE_URL)

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    application.add_error_handler(on_error)

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
