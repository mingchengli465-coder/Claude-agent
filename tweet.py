"""X (Twitter) post generation for the Telegram bot.

Generates one tweet in English via OpenRouter and publishes it straight to X.
Telegram only gets a notification afterwards.

The model is asked for bare tweet text, not JSON, so the reply is used as-is
after URL stripping, a hashtag cap and a length trim.

The content rules live in the prompt itself. `xhs` is still used for the
reasoning-trace fallback and for unwrapping a stray JSON reply.
Publishing uses tweepy with the same four X credentials as `x_bot.py`.
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import tweepy
from openai import AsyncOpenAI

import xhs

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# No fallback to MODEL any more: MODEL is the Telegram chat model and is
# normally set, so falling back to it would quietly stop tweets using this one.
X_MODEL = os.environ.get("X_MODEL", "google/gemma-4-26b-a4b-it:free")
# The retry goes here instead of back to X_MODEL. A single :free model shares
# one upstream pool with everyone, so when it is rate-limited an immediate retry
# on it fails too; OpenRouter's free router picks whichever free model is up.
# Set X_FALLBACK_MODEL to the same value as X_MODEL to retry on it instead.
X_FALLBACK_MODEL = os.environ.get("X_FALLBACK_MODEL", "openrouter/free").strip() or X_MODEL
X_REQUEST_TIMEOUT = float(os.environ.get("X_REQUEST_TIMEOUT", "120"))
X_MAX_TOKENS = int(os.environ.get("X_MAX_TOKENS", "2000"))
X_TWEET_STATE_FILE = Path(os.environ.get("X_TWEET_STATE_FILE", "x_tweet_state.json"))
X_AVOID_DAYS = int(os.environ.get("X_AVOID_DAYS", "7"))
# The prompt sets a different ceiling per language, so the trim needs both.
# 270 English characters weigh 270; 130 Chinese characters weigh 260. Both sit
# inside X's 280 budget.
TWEET_CHAR_LIMIT = int(os.environ.get("X_TWEET_CHAR_LIMIT", "270"))
CHINESE_CHAR_LIMIT = int(os.environ.get("X_TWEET_CHAR_LIMIT_ZH", "130"))

ENGLISH = "English"
CHINESE = "Simplified Chinese"
LANGUAGE_LIMITS = {ENGLISH: TWEET_CHAR_LIMIT, CHINESE: CHINESE_CHAR_LIMIT}
# Share of tweets written in English; the rest are Simplified Chinese. The
# account sells design work to English-speaking clients, so English only.
ENGLISH_RATIO = float(os.environ.get("X_ENGLISH_RATIO", "1.0"))
X_WEIGHTED_LIMIT = 280
# Two at most; more reads as spam on a promotional account.
MAX_TAGS = int(os.environ.get("X_MAX_TAGS", "2"))

# X credentials — the same four x_bot.py uses.
X_API_KEY = os.environ.get("X_API_KEY", "")
X_API_SECRET = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

# Each tweet promotes one service line, rotated so the feed isn't one pitch
# on repeat (X also treats identical promotional posts as spam).
DOMAINS = [
    "custom websites for small businesses",
    "pitch decks for founders",
    "landing pages",
    "presentation redesign: turning a cluttered deck into a clear one",
    "portfolio and personal-brand websites",
    "sales and client-proposal decks",
]

# Everything a tweet may claim about the service. The model is told it can't
# add to this, so set X_SERVICE_BRIEF in Railway to change the offer (prices,
# turnaround, a new service) instead of editing the prompt.
DEFAULT_SERVICE_BRIEF = """I design custom websites and presentations for clients.
Websites: small-business sites, landing pages, portfolio and personal-brand sites. Designed around the client's brand and content, not a template.
Presentations: pitch decks, sales and proposal decks, talks and conference or class presentations. Clear structure, clean visuals.
Clients get the finished website, or an editable deck file (PowerPoint, which also opens in Keynote and Google Slides).
Pricing: quoted per project.
How to start: send me a DM."""
X_SERVICE_BRIEF = os.environ.get("X_SERVICE_BRIEF", "").strip() or DEFAULT_SERVICE_BRIEF

client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
    timeout=X_REQUEST_TIMEOUT,
) if OPENROUTER_API_KEY else None

# Only used when a model wraps the tweet in JSON despite the prompt.
TEXT_KEYS = ("text", "tweet", "正文", "內容", "内容", "content")

_URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)
# Sentence ends, used to trim a too-long tweet without cutting mid-thought.
_SENTENCE_END = ".。！？!?…"


def _is_usable(data: dict) -> bool:
    """A tweet we could actually post: some text in it.

    The note test in `xhs` looks for title+body, which a tweet never has — so
    without this a reasoning model's `{"text": ""}` sketch would win.
    """
    text = xhs._pick(data, TEXT_KEYS)
    return bool(isinstance(text, str) and text.strip())


class GenerationError(xhs.GenerationError):
    """Raised when the model could not produce a usable tweet."""


@dataclass
class Tweet:
    domain: str
    topic: str
    text: str
    tags: list[str] = field(default_factory=list)

    def full_text(self) -> str:
        """The text as it will be posted, hashtags included."""
        if not self.tags:
            return self.text
        return f"{self.text}\n\n" + " ".join(f"#{t}" for t in self.tags)


# --------------------------------------------------------------------------- #
# Length
# --------------------------------------------------------------------------- #


def weighted_length(text: str) -> int:
    """X's own count: CJK and full-width characters weigh 2, everything else 1."""
    total = 0
    for ch in text:
        code = ord(ch)
        if (0x1100 <= code <= 0x11FF or 0x2E80 <= code <= 0xA4CF
                or 0xA960 <= code <= 0xA97F or 0xAC00 <= code <= 0xD7FF
                or 0xF900 <= code <= 0xFAFF or 0xFE10 <= code <= 0xFE19
                or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60
                or 0xFFE0 <= code <= 0xFFE6 or 0x1F300 <= code <= 0x1FAFF):
            total += 2
        else:
            total += 1
    return total


def fit_tweet(text: str, limit: int | None = None) -> str:
    """Trim to `limit` at a sentence boundary, so the tweet still lands.

    A tweet's point is usually its last line, so whole trailing sentences are
    dropped rather than cutting mid-word.
    """
    limit = TWEET_CHAR_LIMIT if limit is None else limit
    text = text.strip()
    if len(text) <= limit and weighted_length(text) <= X_WEIGHTED_LIMIT:
        return text

    # Split keeping the punctuation attached to its sentence.
    parts = re.findall(rf"[^{_SENTENCE_END}]*[{_SENTENCE_END}]|[^{_SENTENCE_END}]+", text)
    kept: list[str] = []
    for part in parts:
        candidate = "".join(kept + [part])
        if len(candidate) > limit or weighted_length(candidate) > X_WEIGHTED_LIMIT:
            break
        kept.append(part)

    trimmed = "".join(kept).strip()
    if trimmed:
        logger.warning("推文過長，從 %d 字裁到 %d 字", len(text), len(trimmed))
        return trimmed

    # A single sentence longer than the whole budget: hard cut, flagged.
    hard = text[:limit - 1].rstrip() + "…"
    logger.warning("推文是一整句且超長，硬截到 %d 字", len(hard))
    return hard


def strip_urls(text: str) -> str:
    """Remove URLs. The spec forbids them, and they eat 23 characters each."""
    cleaned = _URL_RE.sub("", text)
    if cleaned != text:
        logger.warning("推文裡有網址，已移除")
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    if not X_TWEET_STATE_FILE.exists():
        return {"domain_index": 0, "history": []}
    try:
        state = json.loads(X_TWEET_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("讀不了 %s，從空狀態開始", X_TWEET_STATE_FILE, exc_info=True)
        return {"domain_index": 0, "history": []}
    state.setdefault("domain_index", 0)
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    state["history"] = (state.get("history") or [])[-60:]
    tmp = X_TWEET_STATE_FILE.with_suffix(X_TWEET_STATE_FILE.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, X_TWEET_STATE_FILE)
    except OSError:
        logger.error("寫不了 %s", X_TWEET_STATE_FILE, exc_info=True)


def is_paused() -> bool:
    """Whether the scheduled posting is paused.

    Kept in the state file, so it survives a restart but not a redeploy on a
    platform with an ephemeral disk. Defaulting to "running" is the safe way
    round: a wiped file resumes posting rather than silently staying off.
    """
    return bool(load_state().get("paused", False))


def set_paused(paused: bool) -> None:
    state = load_state()
    state["paused"] = bool(paused)
    save_state(state)
    logger.info("自動發推已%s", "暫停" if paused else "恢復")


def recent_tweet_texts(state: dict, days: int = X_AVOID_DAYS) -> list[str]:
    """Recent tweets, newest first, for the {recent_tweets} placeholder.

    Falls back to the truncated `title` written by older versions so history
    from before this change is still useful.
    """
    cutoff = date.today() - timedelta(days=days)
    out: list[str] = []
    for entry in reversed(state.get("history") or []):
        try:
            when = date.fromisoformat(entry.get("date", ""))
        except ValueError:
            continue
        if when < cutoff:
            continue
        text = entry.get("text") or entry.get("title") or ""
        if text:
            out.append(" ".join(str(text).split()))
    return out


def take_domain(state: dict) -> str:
    index = int(state.get("domain_index", 0)) % len(DOMAINS)
    state["domain_index"] = (index + 1) % len(DOMAINS)
    return DOMAINS[index]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

# Placeholders are filled by str.replace rather than str.format, so nothing
# else in the text has to be escaped.
TWEET_PROMPT = """You run a small independent design studio and post on X to win clients. Write ONE original tweet in {language} that promotes your custom design work.

What you sell (you may only claim what is written here):
{service}

This tweet's focus: {domain}

Rules:
- Write entirely in {language}.
- If English: under 270 characters total, including hashtags.
- If Chinese: under 130 Chinese characters total, including hashtags.
- Sound like a real designer talking to potential clients, not an ad agency or a press release.
- Pick ONE angle: a design tip a client can use today, a common mistake you fix, what "custom" gets you that a template doesn't, a sign someone needs a redesign, or a direct offer.
- Every tweet must make it clear you take on this kind of work, and end with a short call to action such as "DMs open." or "DM me if that's you."
- Be specific. One concrete detail beats three adjectives.
- Never invent clients, projects, results, numbers, reviews or quotes. No "I just shipped a site for...", no "conversions up 40%".
- No prices, discounts, deadlines or turnaround times unless they are in the list above.
- Don't name or knock other companies, tools or designers.
- Short sentences. Line breaks are fine. At most 1 emoji, or none.
- 0\u20132 relevant hashtags at the end, such as #WebDesign #PitchDeck. Never more than 2.
- No links, no @mentions, no quotation marks around the whole tweet.

Recent tweets (do not repeat these topics or openings):
{recent_tweets}

Output ONLY the tweet text. No explanation, no preamble."""


def pick_language() -> str:
    """Draw the language: ENGLISH_RATIO of the time English, otherwise Chinese."""
    return ENGLISH if random.random() < ENGLISH_RATIO else CHINESE


def _build_prompt(language: str, recent: list[str], domain: str = DOMAINS[0]) -> str:
    """Fill the placeholders; leave the rest of the prompt alone."""
    block = "\n".join(f"- {t}" for t in recent) if recent else "(none yet)"
    return (TWEET_PROMPT
            .replace("{service}", X_SERVICE_BRIEF)
            .replace("{domain}", domain)
            .replace("{language}", language)
            .replace("{recent_tweets}", block))


_HASHTAG_RE = re.compile(r"#\w+")


def _enforce_hashtag_cap(text: str) -> str:
    """Keep at most MAX_TAGS hashtags, dropping the extras from the end."""
    tags = _HASHTAG_RE.findall(text)
    if len(tags) <= MAX_TAGS:
        return text
    for extra in tags[MAX_TAGS:]:
        text = text.replace(extra, "", 1)
        logger.warning("超過 %d 個標籤，移除 %s", MAX_TAGS, extra)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _parse_tweet(raw: str, domain: str, language: str = ENGLISH) -> Tweet:
    """Turn the model's plain-text reply into a Tweet.

    The prompt asks for bare text, so that is the main path. A model that wraps
    it in JSON anyway is still unwrapped, since that costs one cheap check and
    saves a whole generation.
    """
    text = (raw or "").strip()
    if text.startswith("{"):
        try:
            text = str(xhs._pick(xhs._extract_json(text, is_usable=_is_usable),
                                 TEXT_KEYS) or "").strip()
            logger.info("模型仍然回傳了 JSON，已取出 text 欄位")
        except xhs.GenerationError:
            pass  # Not JSON after all; treat the whole thing as the tweet.

    # The prompt forbids wrapping the tweet in quotes; models do it anyway.
    if len(text) > 1 and text[0] in "\"'\u201c\u2018" and text[-1] in "\"'\u201d\u2019":
        text = text[1:-1].strip()

    if not text:
        raise GenerationError("模型回傳了空的貼文內容")

    limit = LANGUAGE_LIMITS.get(language, TWEET_CHAR_LIMIT)
    text = fit_tweet(_enforce_hashtag_cap(strip_urls(text)), limit)

    # Hashtags now live inside the text, so `tags` stays empty and full_text()
    # returns the tweet exactly as the model wrote it.
    return Tweet(domain=domain, topic=text[:60], text=text, tags=[])


async def _one_call(domain: str, recent: list[str], language: str,
                    model: str = X_MODEL) -> Tweet:
    if client is None:
        raise GenerationError("OPENROUTER_API_KEY 沒有設定")

    # No response_format here: the prompt asks for bare text, and JSON mode
    # would fight it.
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": _build_prompt(language, recent, domain)}],
        temperature=1.0,
        max_tokens=X_MAX_TOKENS,
    )

    if not response.choices:
        raise GenerationError("模型回傳了空的 choices")

    choice = response.choices[0]
    finish = getattr(choice, "finish_reason", "?")
    text = (choice.message.content or "").strip()
    if not text:
        # A reasoning model can burn the budget thinking and return empty
        # content, leaving the tweet in its trace.
        text = xhs._reasoning_text(choice.message).strip()
        if text:
            logger.warning("content 為空，改從 reasoning 欄位取內容")
    if not text:
        raise GenerationError(f"模型回傳了空內容（finish_reason={finish}）")

    try:
        return _parse_tweet(text, domain, language)
    except GenerationError as exc:
        if finish == "length":
            raise GenerationError(f"{exc}。finish_reason=length，輸出被截斷，建議調高 X_MAX_TOKENS") from exc
        raise


async def generate_tweet(domain: str | None = None) -> Tweet:
    """Generate one tweet, retrying once (on X_FALLBACK_MODEL) before giving up."""
    state = load_state()
    chosen = domain or take_domain(state)
    recent = recent_tweet_texts(state)
    language = pick_language()
    logger.info("這則用 %s 生成", language)

    last_error: Exception | None = None
    for attempt, model in enumerate((X_MODEL, X_FALLBACK_MODEL), start=1):
        try:
            tweet = await _one_call(chosen, recent, language, model)
        except Exception as exc:  # noqa: BLE001 - retry once, then report
            last_error = exc
            logger.warning("推文生成第 %d 次失敗（%s）：%s", attempt, model, exc, exc_info=True)
            continue
        if attempt > 1:
            logger.info("改用備用模型 %s 生成成功", model)

        state.setdefault("history", []).append(
            {
                "date": date.today().isoformat(),
                "domain": chosen,
                "language": language,
                "topic": tweet.topic,
                # Full text, so {recent_tweets} can show real openings.
                "text": tweet.text,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        save_state(state)
        return tweet

    save_state(state)  # Keep the rotation moving past a dead topic.
    raise GenerationError(f"連續兩次生成失敗：{last_error}") from last_error


# --------------------------------------------------------------------------- #
# Publishing
# --------------------------------------------------------------------------- #

_username_cache: str | None = None


def missing_credentials() -> list[str]:
    return [
        name for name, value in (
            ("X_API_KEY", X_API_KEY),
            ("X_API_SECRET", X_API_SECRET),
            ("X_ACCESS_TOKEN", X_ACCESS_TOKEN),
            ("X_ACCESS_TOKEN_SECRET", X_ACCESS_TOKEN_SECRET),
        ) if not value
    ]


def build_x_client() -> tweepy.Client:
    """The same four OAuth 1.0a credentials x_bot.py uses."""
    return tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
        wait_on_rate_limit=True,
    )


def tweet_url(tweet_id: str, client_: tweepy.Client | None = None) -> str:
    """Link to the posted tweet, with the real handle when we can get it."""
    global _username_cache
    if _username_cache is None and client_ is not None:
        try:
            me = client_.get_me(user_fields=["username"]).data
            _username_cache = me.username
        except Exception:  # noqa: BLE001 - the /i/ form works without it
            logger.debug("拿不到使用者名稱，改用 /i/ 連結", exc_info=True)
            _username_cache = ""
    if _username_cache:
        return f"https://x.com/{_username_cache}/status/{tweet_id}"
    return f"https://x.com/i/status/{tweet_id}"


def _publish_sync(text: str) -> str:
    missing = missing_credentials()
    if missing:
        raise GenerationError(f"缺少 X 金鑰：{'、'.join(missing)}")
    client_ = build_x_client()
    response = client_.create_tweet(text=text)
    tweet_id = str(response.data["id"])
    logger.info("已發布推文 %s", tweet_id)
    return tweet_url(tweet_id, client_)


async def publish(tweet: Tweet) -> str:
    """Post the tweet and return its URL. tweepy is blocking, so use a thread."""
    return await asyncio.to_thread(_publish_sync, tweet.full_text())
