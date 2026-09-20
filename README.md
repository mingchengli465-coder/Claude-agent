# Telegram AI Chatbot

A Telegram bot that lets you chat with an AI model through
[OpenRouter](https://openrouter.ai). Built with
[python-telegram-bot](https://python-telegram-bot.org) and the
[openai](https://github.com/openai/openai-python) SDK.

## Features

- Chat with any OpenRouter model straight from Telegram
- Per-chat conversation memory, keeping the last 20 rounds
- Long answers are split automatically to fit Telegram's 4096-character limit
- Friendly error messages on API failures — the bot stays up
- Logging for startup, incoming messages, and failures

## Commands

| Command  | Description                    |
| -------- | ------------------------------ |
| `/start` | Show the welcome message       |
| `/reset` | Clear this chat's history      |

Any other text message is sent to the model.

## Setup

1. **Get a Telegram bot token** — talk to [@BotFather](https://t.me/BotFather),
   send `/newbot`, and copy the token it gives you.
2. **Get an OpenRouter API key** — sign up at
   [openrouter.ai/keys](https://openrouter.ai/keys).
3. **Install dependencies:**

   ```bash
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. **Configure the environment:**

   ```bash
   cp .env.example .env
   # then edit .env and fill in your token and key
   ```

5. **Run the bot:**

   ```bash
   export $(grep -v '^#' .env | xargs)   # or use your own env loader
   python bot.py
   ```

   Then open Telegram, find your bot, and send it `/start`.

## Configuration

All configuration comes from environment variables.

| Variable              | Required | Default                           | Description                                             |
| --------------------- | -------- | --------------------------------- | ------------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`  | yes      | —                                 | Bot token from @BotFather                                |
| `OPENROUTER_API_KEY`  | yes      | —                                 | API key from OpenRouter                                  |
| `MODEL`               | no       | `deepseek/deepseek-chat-v3.1:free`| Any [OpenRouter model](https://openrouter.ai/models) slug |
| `OPENROUTER_BASE_URL` | no       | `https://openrouter.ai/api/v1`    | OpenRouter API base URL                                  |
| `SYSTEM_PROMPT`       | no       | a short assistant prompt          | System prompt sent with every request                    |
| `MAX_HISTORY_ROUNDS`  | no       | `20`                              | Rounds remembered per chat (1 round = message + reply)   |
| `REQUEST_TIMEOUT`     | no       | `60`                              | Seconds to wait for a model response                     |
| `LOG_LEVEL`           | no       | `INFO`                            | `DEBUG`, `INFO`, `WARNING`, or `ERROR`                   |

The bot exits at startup with a clear message if a required variable is missing.

## Deploying

A `Procfile` is included, so the bot runs on any Heroku-style platform
(Heroku, Railway, Render, Fly.io, …) as a worker process:

```
worker: python bot.py
```

Set `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY` in the platform's config
vars, then scale the worker to one instance. The bot uses long polling, so it
needs no public URL or webhook — but run only **one** instance, since Telegram
allows a single polling client per bot.

## How it works

- Each `chat_id` gets its own `deque` of messages, capped at 20 rounds, so old
  turns fall off automatically and memory stays bounded.
- History is only updated after a successful reply, so a failed request never
  leaves a dangling user message in the context.
- A per-chat lock keeps concurrent messages from the same chat from
  interleaving.
- Replies longer than 4096 characters are split at paragraph, line, or word
  boundaries where possible, falling back to a hard split.

## Notes

- Conversation history is kept in memory, so it is cleared on restart.
- Free OpenRouter models are rate limited; the bot reports this rather than
  crashing.
