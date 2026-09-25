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

Type each value in by hand — don't paste the whole of `.env.example` into the
dashboard. The bot refuses to start on an empty or placeholder value and names
the variable in the error, e.g.:

```
環境變數有問題，無法啟動：
  - TELEGRAM_BOT_TOKEN 還是範例裡的佔位字串 'your-telegram-bot-token'，請換成真正的值
```

A `telegram.error.InvalidToken` on a platform like Railway means
`TELEGRAM_BOT_TOKEN` there is not the real @BotFather token.

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

Every note promotes the 代做 service (PPT, résumés, copywriting, Excel for
university students) and answers the two objections a reader will have:
"why not just use 豆包/千问?" and "why not use Claude myself?" (overseas
card, overseas phone number, a fiddly sign-up). Six service lines rotate one per note, so the account
doesn't post the same pitch every day:

> 课程汇报 PPT · 简历优化排版 · 毕业答辩 PPT · Excel 表格整理 · 比赛与路演 PPT · 小红书 / 公众号文案

What the notes may say about the service comes from one block of text, and the
model is told it can't add anything to it. To change the offer (add prices,
drop a line), set `XHS_SERVICE_BRIEF` in Railway. The default is
`DEFAULT_SERVICE_BRIEF` in `xhs.py`.

Topics written in the last `XHS_AVOID_DAYS` days (7 by default) are passed back
to the model as things not to repeat — including "the same thing said
differently". The history lives in `xhs_state.json`.

## Content rules

Six rules are written into every request, and the model is told that breaking
them means the note is a failure:

- no attacks on any group, and no running down competitors. Comparisons with
  豆包/千问 must concede what they do well first, then speak from "my
  experience" — no invented benchmarks (China's 广告法 bans disparaging
  other products)
- nothing invented: no order counts, reviews, customer quotes, pass rates or
  case stories, and nothing about the service beyond the brief
- no 作业/论文代写 or 代考, and no promises of grades, passing a defence or a job
- no off-platform contact of any kind (微信, QQ, 闲鱼, links, QR codes, 加V) —
  小红书 throttles or bans accounts for it. Only "评论区留言" or "私信"
- Claude, Codex, 豆包 and 千问 may be named, only as the brief puts it; never
  翻墙/梯子/VPN, and never selling accounts, top-ups or relay access — only 代做
- no medical or investment advice, nothing political

## The cover

Rendered locally with Pillow at 1080×1440: yellow `#FFE14D` ground, black
headline, the argumentative question in a red `#E8322E` rounded box, and a
black 「接单中」 tag in the top-left corner (`XHS_BADGE_TEXT` to change it).

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

- `max_tokens` is 8000, so there is room for the thinking *and* a ~1000-token
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
| `XHS_FALLBACK_MODEL`  | no       | `openrouter/free`    | Model the one retry uses, so a flaky first model can't fail both attempts |
| `XHS_REQUEST_TIMEOUT` | no       | `90`                 | Seconds to wait per attempt; the SDK does not retry on top, so /xhs answers within ~3 min |
| `XHS_MAX_TOKENS`      | no       | `8000`               | Token budget; reasoning models need room to think and still write |
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

`tweet.py` writes one short English post promoting **custom design work** —
client websites, landing pages and presentation decks — and publishes it
straight to X. There is no approval step —
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

Six service lines rotate, one per post, so the feed isn't one pitch on repeat:

> custom websites for small businesses · pitch decks for founders · landing
> pages · presentation redesign · portfolio and personal-brand websites · sales
> and client-proposal decks

Each tweet takes one angle — a design tip a client can use today, a common
mistake, what custom gets you over a template, a sign someone needs a redesign,
or a direct offer — and ends with a short call to action ("DMs open.").

What a tweet may claim about the service comes from one block of text, and the
model is told it can't add to it. To change the offer (add prices, turnaround,
a new service), set `X_SERVICE_BRIEF` in Railway. The default is
`DEFAULT_SERVICE_BRIEF` in `tweet.py`.

The prompt forbids invented clients, projects, results, numbers, reviews and
quotes; prices or deadlines not in the brief; and naming or knocking other
companies. No links or @mentions.

## Telegram link

Set `X_TELEGRAM_LINK` (`https://t.me/name`, `@name` or `name`) and every tweet
gets a reply underneath with that link. The link goes in a reply rather than the
tweet because X shows posts containing links to fewer people. The Telegram
notification says whether the reply went up; if it fails, the tweet is still
reported as posted. Change the wording with `X_LINK_REPLY_TEXT` (`{link}` is
filled in). Note that the reply is a second post, and on X's pay-per-use API a
post with a link costs more than one without.

## Language

**English only** by default (`X_ENGLISH_RATIO=1.0`), since the clients are
English-speaking. The ratio still works if you want some Simplified Chinese
posts back: the draw fills the prompt's `{language}` slot and picks the
character ceiling — 270 for English, 130 for Chinese.

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
| `X_MODEL`                | no       | `google/gemma-4-26b-a4b-it:free` | Model used for tweets. No longer falls back to `MODEL` |
| `X_SERVICE_BRIEF`        | no       | `DEFAULT_SERVICE_BRIEF` | Everything a tweet may say about the service |
| `X_TELEGRAM_LINK`        | no       | —                  | Telegram link replied under every tweet; empty turns it off |
| `X_ENGLISH_RATIO`        | no       | `1.0`              | Share of English tweets; the rest are Simplified Chinese |
| `X_DAILY_TIMES`          | no       | `12:00,20:00`      | Daily posting times                           |
| `X_TIMEZONE`             | no       | `Asia/Taipei`      | Timezone for those times                      |
| `X_TWEET_CHAR_LIMIT`     | no       | `140`              | Character limit                               |
| `X_MAX_TOKENS`           | no       | `8000`             | Token budget; roomy so a reasoning fallback can still write |
| `X_FALLBACK_TRIES`       | no       | `2`                | Tries on `X_FALLBACK_MODEL` after `X_MODEL` fails |
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

# 客服模式（Customer Service）

`OWNER_CHAT_ID` 之外的人私聊机器人，就进入客服模式：Claude 按 `products.yaml`
回答 AI 代做服务的问题，问清需求（做什么、截止时间、预算），该你出面时通知你。
你自己的消息、`/xhs`、`/tweet` 和所有定时任务都不受影响。群聊不进客服模式。

逻辑都在 `customer_service.py`，不依赖 Telegram；`bot.py` 只是 Telegram 的入口。

## 客户那边

- 默认简体中文，语气像博主的助理。只根据 `products.yaml` 回答，写着「待填」的字段
  当作不知道。
- 模型带最近 10 条对话（`CS_HISTORY`），存在 SQLite（`CS_DB_PATH`）。
- 每人每分钟最多 5 条（`CS_RATE_LIMIT`），超出只提示一次，之后安静到下一分钟。
- 发图片/文件：回「收到文件啦」，文件转给你。

## 什么时候转给你

回客户「我请本人来跟你确认，稍等哦」，并把客户昵称、@用户名、chat ID、需求摘要、
最近对话推给你：

- 要下单 / 付款 / 定金 / 发票
- 砍价、问优惠
- 需求复杂
- 问到资料里没有或「待填」的内容
- 要找真人 / 本人

模型自己判断之外，还有一层关键词兜底（付款、下单、便宜点、真人……），命中就一定转。
模型出错、被拒答、没有 `ANTHROPIC_API_KEY`，也都转给你，不会让客户干等。

## 你这边

| 操作 | 效果 |
| ---- | ---- |
| 在 Telegram 里「回复」推送给你的那条消息 | 内容转发给那位客户（文字、图片、文件都行）|
| `/ai <chat_id> off` | 这位客户不再由 AI 回复，他的消息直接转给你 |
| `/ai <chat_id> on` | 恢复 AI 回复 |
| `/customers` | 最近 15 位客户：昵称、需求摘要、最后消息时间、AI 开关 |

你的回复会记进对话，之后 AI 会以「【本人回复】」看到它，可以据此继续接待。

## products.yaml

四项服务（写代码、做 PPT、写文案 / 小红书文案、做简单网站），每项有说明、价格区间、
交付周期、需要客户提供什么；另有付款方式、修改次数、不接的内容、常见问题。
把「待填」换成真实内容；以 `#` 开头的注释（包括「例如」）不会被模型看到。

## 模型

`claude-opus-5`（`CS_MODEL`），自适应思考、`effort: low`（`CS_EFFORT`），结构化输出
（每次回复都是固定格式的 JSON，决定回复内容、要不要转人工、需求摘要），系统提示
缓存，并开启了服务端 `fallbacks: "default"`：请求被 Claude 安全机制拒绝时，自动改由
推荐的备用模型处理。

## 接企业微信

```python
service.register_channel("wecom", send)   # send(chat_id, text)
reply = await service.handle(cs.Inbound(channel="wecom", chat_id=..., text=..., display_name=...))
if reply: await send(chat_id, reply)
```

转人工通知和你的回复仍然走 Telegram；`/ai wecom:<id> off` 这样指定渠道。

## 配置

| 变量 | 必填 | 默认 | 说明 |
| ---- | ---- | ---- | ---- |
| `OWNER_CHAT_ID` | 是* | `ADMIN_CHAT_ID` | 你本人的 chat ID；都没设则客服模式关闭 |
| `ANTHROPIC_API_KEY` | 是* | — | Claude API 密钥；没有则客户消息全部转给你 |
| `CS_MODEL` | 否 | `claude-opus-5` | 客服用的 Claude 模型 |
| `CS_EFFORT` | 否 | `low` | 思考深度：low / medium / high |
| `CS_PRODUCTS_PATH` | 否 | `products.yaml` | 业务资料文件 |
| `CS_DB_PATH` | 否 | `cs.sqlite3` | 对话数据库。Railway 重新部署会清空，要保留就挂 Volume 并指向它 |
| `CS_HISTORY` | 否 | `10` | 带给模型的最近消息条数 |
| `CS_RATE_LIMIT` | 否 | `5` | 每位客户每分钟最多几条 |
| `CS_TIMEZONE` | 否 | `Asia/Shanghai` | `/customers` 和对话记录的时间 |

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
