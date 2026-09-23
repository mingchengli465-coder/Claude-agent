# AI Chatbots

Two bots live in this repo:

| Bot | File | Talks to | Model |
| --- | ---- | -------- | ----- |
| Telegram chatbot | `bot.py` | Telegram DMs and groups | any [OpenRouter](https://openrouter.ai) model |
| 小红书 note generator | `xhs.py` (via `bot.py`) | the admin's Telegram chat | any OpenRouter model |
| X auto-poster | `tweet.py` (via `bot.py`) | AI takes, posted to X | any OpenRouter model |
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

| Command  | Description                              |
| -------- | ---------------------------------------- |
| `/start` | Show the welcome message                 |
| `/reset` | Clear this chat's history                |
| `/xhs`   | Generate a 小红书 note (admin only)        |
| `/tweet` | Generate and post to X now (admin only)   |
| `/pause` | Stop the scheduled posting (admin only)   |
| `/resume`| Start it again (admin only)               |

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

# 小红书 Note Generator

`bot.py` can also write 小红书 (Xiaohongshu) notes: one opinionated, argument-
starting post per day, delivered to your Telegram chat ready to copy out. The
generation lives in `xhs.py`; `bot.py` only wires it to Telegram.

## How it runs

- **Every day at 09:00 Asia/Taipei**, via python-telegram-bot's `JobQueue`
- **`/xhs`** generates one on demand

Both are restricted to `ADMIN_CHAT_ID`. Everyone else can still chat with the
bot as usual; `/xhs` from another chat is politely refused before any model
call is made.

## What arrives

Four separate messages, so each can be long-pressed and copied on its own:

1. the cover image (1080×1440 PNG)
2. the title
3. the body
4. the hashtags — this one carries two buttons:
   - **🔁 重写文案** — generate a fresh note
   - **🎨 换封面** — re-render the cover in a different layout

## Topic selection

Six domains rotate one per day, so no single area takes over the account:

> 搞钱与职场 · 消费观 · 感情与生活选择 · AI 与未来 · 年轻人现状 · 反常识观点

Topics written in the last `XHS_AVOID_DAYS` days (7 by default) are passed back
to the model as things not to repeat — including "the same thing said
differently". The history lives in `xhs_state.json`.

## Content rules

Four rules are written into every request, and the model is told that breaking
them means the note is a failure:

- no attacks on any group — gender, region, ethnicity, occupation, age, and so on
- no invented news, statistics, study findings or quotes from real people
  (personal anecdotes are fine; anything dressed up as fact is not)
- no medical advice, no investment advice
- nothing political

## The cover

Rendered locally with Pillow at 1080×1440: yellow `#FFE14D` ground, black
headline, the argumentative question in a red `#E8322E` rounded box, and a
black 「真实经历」 tag in the top-left corner.

The font (**Noto Sans SC**) is committed to `fonts/` rather than taken from the
system — a server without a CJK font renders every character as tofu. One
variable file supplies every weight the cover uses.

Long text never overflows: each block shrinks to the largest size that fits its
box and re-wraps character by character if it still doesn't, keeping punctuation
off the start of a line. The remaining height is shared out as spacing so the
composition fills the frame instead of pooling at the top.

## Reasoning models

A reasoning model can spend its whole token budget thinking and return an
**empty `content`**, which is what `inclusionai/ling-3.0-flash-vl:free` did.
Three things address it:

- `max_tokens` is 4000, so there is room for the thinking *and* a ~1000-token
  Chinese note.
- Every request carries OpenRouter's `reasoning: {exclude: true, effort: "low"}`,
  which keeps the thinking short and drops it from the response.
- If `content` still comes back empty, the JSON is looked for in the reasoning
  trace instead — `reasoning`, `reasoning_content`, or `reasoning_details`,
  since providers differ. Note this is a safety net: with `exclude: true` the
  trace usually isn't returned at all, so it only helps where a provider ignores
  the flag.

If a note still fails on a reasoning model, raise `XHS_MAX_TOKENS` or set
`XHS_REASONING_EFFORT=minimal`. `XHS_MODEL` lets you point 小红书 generation at
a non-reasoning model while the chat bot keeps using `MODEL`.

## Surviving small models

Free OpenRouter models are loose about output format, so the reply is not
trusted to be clean JSON:

- The request asks for `response_format: json_object`. Many free models reject
  that parameter — the first rejection falls back to a plain request inside the
  same attempt and is remembered, so later calls skip it. Set
  `XHS_JSON_MODE=false` to never send it.
- The prompt says JSON-only in both the system and user message, and carries a
  complete worked example (itself valid JSON, and the right length) to imitate.
- Parsing strips ``` fences — including one the model never closed — then scans
  for balanced `{...}` spans and takes the first that parses and looks like a
  note. Scanning every candidate rather than the first brace matters when the
  model writes something like `这里有个 { 花括号` before the real object.
- A candidate is only accepted once it has a **non-empty** title and body. A
  reasoning trace often sketches the schema first (`{"title": "", "body": ""}`),
  and that skeleton has the right keys — it must not beat the real note that
  follows.
- Alternate key names are accepted: `标题`/`正文`/`content`, a `{"note": {...}}`
  wrapper, and a comma-separated tag string instead of a list.
- Two common malformations are repaired before giving up: trailing commas, and
  real line breaks inside a string where `\n` was meant — the usual cause of
  "Invalid control character" on a multi-paragraph 正文.

## Failure handling

A failed generation — a transport error, or a reply that isn't usable JSON — is
retried once. If the second attempt also fails, the error is sent to
`ADMIN_CHAT_ID` rather than disappearing into the log.

Errors name what actually came back — the keys present, the title and body
lengths, and whether `finish_reason` was `length` (truncated output, meaning
`XHS_MAX_TOKENS` is too low).

**When parsing fails, the first 300 characters of the raw reply go to the log**
(`RAW_LOG_CHARS`). An empty reply is reported as 模型返回了空内容 rather than
模型没有返回 JSON, since the two have different causes — the former usually means
a free-model rate limit.

## Configuration

| Variable              | Required | Default              | Description                                          |
| --------------------- | -------- | -------------------- | ---------------------------------------------------- |
| `ADMIN_CHAT_ID`       | yes*     | —                    | The only chat that may use `/xhs` and the buttons, and where the daily note goes. Unset disables the feature |
| `XHS_DAILY_TIME`      | no       | `09:00`              | Daily generation time                                |
| `XHS_TIMEZONE`        | no       | `Asia/Taipei`        | Timezone for that time                               |
| `XHS_MODEL`           | no       | falls back to `MODEL`| Model used for notes                                 |
| `XHS_REQUEST_TIMEOUT` | no       | `120`                | Seconds to wait for a note                           |
| `XHS_MAX_TOKENS`      | no       | `4000`               | Token budget; reasoning models need room to think and still write |
| `XHS_REASONING_EFFORT`| no       | `low`                | OpenRouter reasoning effort                          |
| `XHS_REASONING_EXCLUDE`| no      | `true`               | Keep the reasoning trace out of the response         |
| `XHS_REASONING`       | no       | `true`               | Send the reasoning block at all; auto-disables if the model rejects it |
| `XHS_STATE_FILE`      | no       | `xhs_state.json`     | Domain rotation and topic history                    |
| `XHS_AVOID_DAYS`      | no       | `7`                  | Don't reuse a topic from the last N days             |
| `XHS_FONT_PATH`       | no       | `fonts/NotoSansSC-VF.ttf` | Override the bundled cover font                 |
| `XHS_JSON_MODE`       | no       | `true`               | Send `response_format: json_object`; auto-disables if the model rejects it |

\* Required for this feature only. The chat bot works without it.

Find your chat id by messaging [@userinfobot](https://t.me/userinfobot).

## Testing without credentials

```bash
python test_xhs.py          # JSON parsing, topic rotation, cover rendering
python test_bot_wiring.py   # admin gate, the daily job, the four-message send
```

Neither needs a token, an API key, or a network connection.

---

# X Auto-Poster

`tweet.py` writes one short, opinionated post about **AI and AI agents** in
Traditional Chinese and publishes it straight to X. There is no approval step —
Telegram only gets told afterwards.

## How it runs

- **12:00 and 20:00 Asia/Taipei** (`X_DAILY_TIMES`), via `JobQueue`
- **`/tweet`** posts one immediately

## Commands

| Command  | What it does                                              |
| -------- | --------------------------------------------------------- |
| `/tweet` | Generates and posts one now, even while paused             |
| `/pause` | Stops the scheduled posting                                |
| `/resume`| Starts it again                                            |

All three are gated on `ADMIN_CHAT_ID`.

`/pause` stops the **schedule**. `/tweet` is a deliberate manual action, so it
still posts while paused — and says so in its reply, rather than looking like
pause did nothing.

## What you get told

After a successful post, a plain notification with the text and the tweet's
URL. No buttons.

If posting fails, the error comes through **with the generated text included**,
so a tweet that cost a model call isn't lost — it can be posted by hand. A
generation failure is reported too, and posts nothing.

## Pause and restarts

The pause flag lives in `x_tweet_state.json`, so it survives a restart. On a
platform with an ephemeral disk (Railway) a redeploy wipes it.

**The default is "running"**, which is the safe way round: a wiped file resumes
posting rather than silently staying off forever. Re-issue `/pause` after a
redeploy if you want it to stay off.

## Length

The limit is **140 characters**, which is exactly X's 280-weight budget since
CJK characters count double. Over-long drafts drop whole trailing sentences
rather than cutting mid-word — a tweet's point is usually its last line. URLs
are stripped (the spec forbids them, and each costs 23 characters), and
hashtags are dropped if they would push the post over.

## What it writes about

Six sub-areas rotate, one per post:

> AI agent 的實際能力與限制 · 用 AI 寫程式的體驗和踩坑 · AI 工具比較與選擇 ·
> AI 對工作和職業的影響 · 自動化工作流的想法 · 對 AI 產業趨勢的觀察

The prompt pushes for something **specific** — how an agent differed from its
pitch, a mistake a model keeps making, a concrete difference between two tools —
rather than general commentary.

## Style

- **2 to 4 sentences.** Not a three-part story; no scene-setting.
- The opinion or finding goes in the first sentence.
- The ending doesn't have to be a question — an assertion often lands harder.
- Traditional Chinese, with technical terms left in English (agent, context,
  prompt, token, API, MCP).
- At most **2 hashtags**, preferring the usual English ones (`AI`, `AIAgent`,
  `LLM`, `Claude`, `Cursor`, `vibecoding`).

Three worked examples are embedded in the prompt. A test asserts they obey the
same 2–4 sentence and 2-tag rules they teach, and that they don't all end on a
question — otherwise the model would copy whatever the examples actually do.

## Content rules

No fabricated benchmark numbers, market shares, funding figures, product
capabilities, or quotes attributed to anyone. Nothing political. No medical or
investment advice, and no attacks on people, companies or groups.

**These are this module's own rules, not `xhs.py`'s.** The 小红书 set requires
every detail to be a personal life experience, which would rule out exactly the
hands-on tooling findings this account exists to post.

## Shared with 小红书

Still reused from `xhs.py` rather than copied: the JSON tolerance (fences,
braces in prose, trailing commas, reasoning-trace fallback), the recent-topic
window, and the key-alias lookup. `_extract_json` takes the caller's definition
of a usable object, because a tweet's "done" looks nothing like a note's.

## Configuration

| Variable                 | Required | Default            | Description                                   |
| ------------------------ | -------- | ------------------ | --------------------------------------------- |
| `X_API_KEY`              | to post  | —                  | X app credentials, same four as `x_bot.py`    |
| `X_API_SECRET`           | to post  | —                  |                                               |
| `X_ACCESS_TOKEN`         | to post  | —                  | Must be regenerated after setting Read+Write  |
| `X_ACCESS_TOKEN_SECRET`  | to post  | —                  |                                               |
| `X_MODEL`                | no       | falls back to `MODEL` | Model used for tweets                      |
| `X_DAILY_TIMES`          | no       | `12:00,20:00`      | Daily posting times                           |
| `X_TIMEZONE`             | no       | `Asia/Taipei`      | Timezone for those times                      |
| `X_TWEET_CHAR_LIMIT`     | no       | `140`              | Character limit                               |
| `X_MAX_TOKENS`           | no       | `2000`             | Token budget for generation                   |
| `X_TWEET_STATE_FILE`     | no       | `x_tweet_state.json` | Rotation, topic history and the pause flag  |
| `X_AVOID_DAYS`           | no       | `7`                | Don't reuse a topic from the last N days      |
| `X_MAX_TAGS`             | no       | `2`                | Hashtag cap                                   |

Since posting is now unattended, the X credentials are needed for it to work at
all. A missing credential is named in the failure message.

## Testing

```bash
python test_tweet.py        # length, URL stripping, parsing, rotation
python test_bot_wiring.py   # auto-posting, pause/resume, the admin gate
```

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
python x_bot.py doctor            # check every credential and permission
python x_bot.py doctor --write    # same, plus post and delete a test tweet
python x_bot.py whoami            # print the authenticated account
python x_bot.py ask "..."         # ask Claude, print the answer, post nothing
python x_bot.py post "..."        # compose a standalone tweet and post it
python x_bot.py autopost          # post once from the topic rotation, then exit
python x_bot.py schedule          # post automatically, every day, on a timetable
python x_bot.py run               # poll mentions and reply
python x_bot.py both              # do both, in one process
```

`doctor` is the one to start with: it tests each credential separately and
prints the exact fix and link for whatever is broken.

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
   python x_bot.py doctor --write     # fix anything it reports, then re-run
   DRY_RUN=true python x_bot.py run   # watch what it would post
   python x_bot.py run                # for real
   ```

   `.env` is loaded automatically — no `export` step needed.

On its first run the bot records the newest existing mention and starts from
there, so it won't answer a backlog. Set `REPLY_TO_BACKLOG=true` if you want it
to.

## Testing without credentials

`test_x_bot.py` drives the mention loop against a fake X API and a fake Claude,
covering the backlog skip, the per-cycle cap, thread ordering, deduplication,
long-reply threading and refusal handling:

```bash
python test_x_bot.py        # the mentions loop
python test_x_schedule.py   # the posting schedule
python test_x_both.py       # the combined loop
```

It needs no keys and makes no network calls.


## Running both jobs together

`both` does the scheduled posting *and* answers mentions from a single
process, so you pay for one instance instead of two. It sleeps until whichever
job is due next, and a failure in one never stops the other.

```bash
DRY_RUN=true python x_bot.py both
```

This is the `xbot` process type in the `Procfile`. Run `schedule` or `run`
alone if you only want one half.

Note the cost asymmetry: the posting half is cheap (one post per slot), while
the mentions half reads posts, which is the metered side of the X API. If the
bill matters more than the coverage, `MAX_THREAD_CONTEXT` and
`POLL_INTERVAL_SECONDS` are the two dials — the latter is floored at 60s,
since polling faster only burns quota.

## Posting on a schedule

`schedule` is the cheap half of this bot: it only writes, so it never touches
the expensive read quota. It walks `topics.txt` in order — every theme gets
used before any repeats — and shows Claude the last dozen posts so it doesn't
say the same thing twice.

```bash
cp topics.txt my-topics.txt   # then edit it: one theme per line
POST_TIMES=08:30,19:00 POST_TIMEZONE=Asia/Shanghai DRY_RUN=true python x_bot.py schedule
```

Keep each theme narrow — *"a mistake beginners make in X"* produces better
posts than *"productivity"*.

Two ways to run it:

- **`schedule`** stays running and posts at each time in `POST_TIMES`. This is
  the `xpost` process type in the `Procfile`.
- **`autopost`** posts once and exits, for platforms with their own cron. Add
  `--topic "..."` to post something specific without disturbing the rotation.

It remembers the last slot it posted, so a restart inside the same minute won't
double-post. A slot where Claude declines is skipped rather than retried.

## What X API access costs

X replaced its flat tiers with pay-per-use pricing for new developers in
February 2026, and closed the old free tier to new signups. At the time of
writing that means roughly **$0.005 per post read** and **$0.015 per post
created** — but a post containing a **link costs about $0.20**, which is why
`AVOID_LINKS` defaults to `true`. Legacy Basic and Pro subscriptions continue
only for accounts that already had them.

Prices and tier names move around, and these figures come from secondary
sources rather than X's own docs, so **treat them as a rough guide and confirm
in the portal**: [developer.x.com/en/portal/products](https://developer.x.com/en/portal/products)

What this means in practice:

- Idle polling is nearly free — a cycle that finds no mentions reads no posts.
- Each answered mention costs roughly one read per tweet of context plus one
  post. `MAX_THREAD_CONTEXT` is therefore a direct cost dial.
- `POLL_INTERVAL_SECONDS` defaults to 900 s to keep request volume modest.

Rather than guess what your account can do, run `python x_bot.py doctor` — it
calls the mentions endpoint and tells you whether your plan allows it.

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
| `AVOID_LINKS`              | no       | `true`         | Ask Claude for no URLs — posts with links cost far more       |
| `TOPICS_FILE`              | no       | `topics.txt`   | Themes for scheduled posting, one per line                    |
| `POST_TIMES`               | no       | `09:00`        | Daily posting times, e.g. `08:30,19:00`                       |
| `POST_TIMEZONE`            | no       | `UTC`          | Timezone those times are in, e.g. `Asia/Shanghai`             |
| `POST_HISTORY_SIZE`        | no       | `12`           | Recent posts shown to Claude to avoid repetition              |
| `POST_JITTER_MINUTES`      | no       | `0`            | Random delay after the slot, so posting looks less robotic    |
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
