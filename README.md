# AI Chatbots

Two bots live in this repo:

| Bot | File | Talks to | Model |
| --- | ---- | -------- | ----- |
| Telegram chatbot | `bot.py` | Telegram DMs and groups | any [OpenRouter](https://openrouter.ai) model |
| X (Twitter) bot  | `x_bot.py` | mentions on X | [Claude](https://docs.claude.com) |

They share a `requirements.txt` and a `.env`, but run as separate processes —
set up only the one you need.

---

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

---

# X (Twitter) Bot powered by Claude

`x_bot.py` puts Claude behind your X account: it polls your mentions, reads the
thread each mention sits in, asks Claude for a reply, and posts it back.

## Features

- Replies to mentions with [Claude](https://docs.claude.com), in the language the
  person used
- Pulls the parent tweets into context, so replies follow the thread
- Replies longer than a tweet are posted as a self-replying thread
- Remembers `since_id` and the tweets it already answered across restarts, so a
  restart never double-posts
- Never answers itself or retweets; optional handle allowlist and a per-cycle cap
- `DRY_RUN=true` generates replies and logs them without posting

## Commands

```bash
python x_bot.py whoami            # check the X credentials
python x_bot.py ask "..."         # ask Claude, print the answer, post nothing
python x_bot.py post "..."        # compose a standalone tweet and post it
python x_bot.py run               # poll mentions and reply (the default)
```

## Setup

1. **Get an Anthropic API key** at
   [console.anthropic.com](https://console.anthropic.com/settings/keys).
2. **Create an X app** at [developer.x.com](https://developer.x.com): make a
   Project and an App, set **User authentication settings** to **Read and
   write**, then from **Keys and tokens** copy the API key/secret and generate an
   Access token/secret. Regenerate the access token if you changed the
   permissions after creating it — otherwise it stays read-only.
3. **Install dependencies** and **configure the environment** as in the Telegram
   setup above; `.env.example` covers both bots.
4. **Check the credentials, then start it:**

   ```bash
   export $(grep -v '^#' .env | xargs)
   python x_bot.py whoami
   DRY_RUN=true python x_bot.py run   # watch what it would post
   python x_bot.py run                # for real
   ```

On its first run the bot records the newest existing mention and starts from
there, so it won't answer a backlog. Set `REPLY_TO_BACKLOG=true` if you want it
to.

## Testing without credentials

`test_x_bot.py` drives the mention loop against a fake X API and a fake Claude,
covering the backlog skip, the per-cycle cap, thread ordering, deduplication,
long-reply threading and refusal handling:

```bash
python test_x_bot.py
```

It needs no keys and makes no network calls.

## X API access tiers

Reading mentions needs at least the **Basic** X API tier. The **Free** tier only
allows posting plus `users/me`, so on Free the `run` command cannot fetch
mentions — `post`, `ask` and `whoami` still work. Check the current limits on
[developer.x.com](https://developer.x.com/en/portal/products) before picking a
`POLL_INTERVAL_SECONDS`; the 900 s default is deliberately conservative, and
tweepy is configured to wait out rate limits rather than fail.

## Configuration

| Variable                   | Required | Default        | Description                                                  |
| -------------------------- | -------- | -------------- | ------------------------------------------------------------ |
| `ANTHROPIC_API_KEY`        | yes      | —              | API key from the Anthropic Console                            |
| `X_API_KEY`                | yes      | —              | X app API key                                                 |
| `X_API_SECRET`             | yes      | —              | X app API secret                                              |
| `X_ACCESS_TOKEN`           | yes      | —              | Access token for your account (read **and write**)            |
| `X_ACCESS_TOKEN_SECRET`    | yes      | —              | Access token secret                                           |
| `CLAUDE_MODEL`             | no       | `claude-opus-5`| Model id                                                      |
| `CLAUDE_EFFORT`            | no       | `low`          | `low`…`max` — thinking depth and token spend                  |
| `CLAUDE_MAX_TOKENS`        | no       | `4096`         | Response cap (thinking counts toward it)                      |
| `ENABLE_REFUSAL_FALLBACK`  | no       | `true`         | Re-run a declined request on a fallback model, server-side    |
| `X_SYSTEM_PROMPT`          | no       | a short persona| The account's voice (separate from the Telegram `SYSTEM_PROMPT`) |
| `POLL_INTERVAL_SECONDS`    | no       | `900`          | Seconds between mention checks                                |
| `MAX_REPLIES_PER_CYCLE`    | no       | `5`            | Most mentions answered per cycle                              |
| `MAX_THREAD_CONTEXT`       | no       | `4`            | Ancestor tweets used as context                               |
| `TWEET_CHAR_LIMIT`         | no       | `280`          | Per-tweet character budget                                    |
| `MAX_TWEETS_PER_REPLY`     | no       | `3`            | Tweets one reply may be split across                          |
| `ALLOWED_USERS`            | no       | everyone       | Comma-separated handles to answer, without `@`                |
| `STATE_FILE`               | no       | `x_bot_state.json` | Where `since_id` and answered ids are stored              |
| `REPLY_TO_BACKLOG`         | no       | `false`        | Answer mentions from before the first run                     |
| `DRY_RUN`                  | no       | `false`        | Generate replies but post nothing                             |
| `LOG_LEVEL`                | no       | `INFO`         | `DEBUG`, `INFO`, `WARNING`, or `ERROR`                        |

## How it works

- Claude is called with adaptive thinking and a configurable `effort`. Effort is
  the cost lever here — `low` suits tweet-length replies; raise it if the account
  answers hard questions.
- Tweets are handed to Claude as untrusted data, and the prompt tells it not to
  follow instructions found in them. That blunts prompt injection from a stranger
  replying to your account, but does not eliminate it — keep `ALLOWED_USERS` set
  while you are testing.
- If Claude declines a request (`stop_reason: "refusal"`), the mention is marked
  handled and nothing is posted. With `ENABLE_REFUSAL_FALLBACK` on, Anthropic
  first retries the request on a fallback model server-side; if your account
  can't use that beta, the bot logs it once and carries on without it.
- The immediate parent of a mention comes free in the mentions payload's
  `includes`; deeper ancestors cost one API call each, which is why
  `MAX_THREAD_CONTEXT` is small by default.
- `TWEET_CHAR_LIMIT` is measured with plain `len()`. X weights characters
  differently — a URL always counts as 23 and CJK counts double — so lower the
  limit for headroom if the account posts a lot of links or CJK text.

## Deploying

The `Procfile` declares the X bot as its own process type:

```
xbot: python x_bot.py run
```

Scale `xbot` to **one** instance — two would answer the same mention twice. Note
that `STATE_FILE` lives on local disk, which is ephemeral on Heroku-style
platforms: a restart there resets `since_id`, and the bot then starts from the
newest mention rather than replaying old ones. Mount a volume if you need the
state to survive.
