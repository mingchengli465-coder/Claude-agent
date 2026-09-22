"""X (Twitter) post generation for the Telegram bot.

Generates one opinionated tweet in Traditional Chinese via OpenRouter and
publishes it straight to X. Telegram only gets a notification afterwards.

The JSON tolerance, the hard content rules and the recent-topic window are
imported from `xhs` rather than copied, so the two features cannot drift apart.
Publishing uses tweepy with the same four X credentials as `x_bot.py`.
"""

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import tweepy
from openai import AsyncOpenAI, BadRequestError

import xhs

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Falls back to the chat model, so one MODEL setting still configures everything.
X_MODEL = os.environ.get("X_MODEL") or os.environ.get("MODEL", "deepseek/deepseek-chat-v3.1:free")
X_REQUEST_TIMEOUT = float(os.environ.get("X_REQUEST_TIMEOUT", "120"))
X_MAX_TOKENS = int(os.environ.get("X_MAX_TOKENS", "2000"))
X_TWEET_STATE_FILE = Path(os.environ.get("X_TWEET_STATE_FILE", "x_tweet_state.json"))
X_AVOID_DAYS = int(os.environ.get("X_AVOID_DAYS", "7"))
# 140 CJK characters is exactly X's 280-weight budget, since CJK counts double.
TWEET_CHAR_LIMIT = int(os.environ.get("X_TWEET_CHAR_LIMIT", "140"))
X_WEIGHTED_LIMIT = 280

# X credentials — the same four x_bot.py uses.
X_API_KEY = os.environ.get("X_API_KEY", "")
X_API_SECRET = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET", "")

# Same six areas as the 小红书 rotation, written in Traditional Chinese.
DOMAINS = [
    "搞錢與職場",
    "消費觀",
    "感情與生活選擇",
    "AI 與未來",
    "年輕人現狀",
    "反常識觀點",
]

_json_mode_supported = os.environ.get("X_JSON_MODE", "true").lower() != "false"

client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
    timeout=X_REQUEST_TIMEOUT,
) if OPENROUTER_API_KEY else None

TEXT_KEYS = ("text", "tweet", "正文", "內容", "内容", "content")
TOPIC_KEYS = ("topic", "選題", "选题", "主題", "主题")
TAGS_KEYS = ("tags", "hashtags", "標籤", "标签")

_URL_RE = re.compile(r"(https?://\S+|www\.\S+)", re.I)
# Sentence ends, used to trim a too-long tweet without cutting mid-thought.
_SENTENCE_END = "。！？!?…"


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


def fit_tweet(text: str) -> str:
    """Trim to the limit at a sentence boundary, so the tweet still lands.

    A tweet's point is usually its last line, so whole trailing sentences are
    dropped rather than cutting mid-word.
    """
    text = text.strip()
    if len(text) <= TWEET_CHAR_LIMIT and weighted_length(text) <= X_WEIGHTED_LIMIT:
        return text

    # Split keeping the punctuation attached to its sentence.
    parts = re.findall(rf"[^{_SENTENCE_END}]*[{_SENTENCE_END}]|[^{_SENTENCE_END}]+", text)
    kept: list[str] = []
    for part in parts:
        candidate = "".join(kept + [part])
        if len(candidate) > TWEET_CHAR_LIMIT or weighted_length(candidate) > X_WEIGHTED_LIMIT:
            break
        kept.append(part)

    trimmed = "".join(kept).strip()
    if trimmed:
        logger.warning("推文過長，從 %d 字裁到 %d 字", len(text), len(trimmed))
        return trimmed

    # A single sentence longer than the whole budget: hard cut, flagged.
    hard = text[:TWEET_CHAR_LIMIT - 1].rstrip() + "…"
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


def take_domain(state: dict) -> str:
    index = int(state.get("domain_index", 0)) % len(DOMAINS)
    state["domain_index"] = (index + 1) % len(DOMAINS)
    return DOMAINS[index]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """你是一個觀點鮮明的 X（Twitter）帳號寫手，擅長寫出會被轉發和爭論的短貼文。

你的輸出會被程式直接用 json.loads() 解析，所以：
- 第一個字元必須是 {，最後一個字元必須是 }
- 不要寫 ```json，不要寫任何程式碼區塊標記
- 不要在 JSON 前後加「好的」「以下是」之類的話
- 貼文內容用繁體中文

只輸出那一個 JSON 物件，其他什麼都不要輸出。"""


def _build_prompt(domain: str, avoid: list[str]) -> str:
    avoid_block = "\n".join(f"- {t}" for t in avoid) if avoid else "（暫無，隨便挑）"
    return f"""請就下面這個領域，寫一則 X（Twitter）貼文。

【領域】{domain}

【最近 {X_AVOID_DAYS} 天已經寫過的選題，不要重複，也不要換個說法寫同一件事】
{avoid_block}

【選題要求】
挑一個觀點對立、留言區會吵起來的話題。要具體到一個場景或一個決定，不要空泛的大道理。

【貼文要求】
- 繁體中文，**不超過 {TWEET_CHAR_LIMIT} 個字**（這是硬限制，超過就沒用了）
- 立場鮮明，第一句就要把觀點丟出來，不要鋪陳
- 可以有一個具體細節（金額、時間、場景），但整則要短
- 結尾留一個讓人想回話的鉤子：一個反問，或一個二選一
- 不要放網址、不要放任何連結
- 標籤最多 3 個，放在 tags 欄位，不要帶 # 號，也不要寫進 text 裡

{xhs.HARD_RULES}

【輸出格式】只輸出這個 JSON：

{{
  "topic": "這則的選題，一句話",
  "text": "貼文正文，繁體中文，{TWEET_CHAR_LIMIT} 字以內",
  "tags": ["標籤一", "標籤二"]
}}

下面是一個好例子的長度和語氣（內容不要抄）：

{{
  "topic": "加薪 3000 但要每天多通勤一小時，值不值得",
  "text": "同事為了加薪三千，換到一個單程多四十分鐘的公司。\\n\\n一年多賺三萬六，換掉三百個小時。算下來一小時一百二，比他的時薪還低。\\n\\n他說這是往上走的必經之路。我覺得他只是把自己賣便宜了。\\n\\n你會換嗎？",
  "tags": ["職場", "通勤"]
}}

現在就【{domain}】寫一則新的。只輸出 JSON。"""


def _normalize(data: dict, domain: str) -> Tweet:
    """Coerce the model's JSON into a Tweet, trimming soft violations."""
    text = str(xhs._pick(data, TEXT_KEYS) or "").strip()
    if not text:
        raise GenerationError(
            f"貼文內容為空（模型回傳的鍵：{sorted(data)}）"
        )

    text = fit_tweet(strip_urls(text))

    raw_tags = xhs._pick(data, TAGS_KEYS) or []
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,，\s]+", raw_tags)
    tags = [t for t in (xhs._clean_tag(t) for t in raw_tags) if t][:3]

    tweet = Tweet(
        domain=domain,
        topic=str(xhs._pick(data, TOPIC_KEYS) or text[:20]).strip(),
        text=text,
        tags=tags,
    )

    # Hashtags count too, so drop them rather than overflow the post.
    while tweet.tags and weighted_length(tweet.full_text()) > X_WEIGHTED_LIMIT:
        dropped = tweet.tags.pop()
        logger.warning("加上標籤會超長，移除 #%s", dropped)
    return tweet


async def _create(kwargs: dict, use_json: bool):
    params = dict(kwargs)
    if use_json:
        params["response_format"] = {"type": "json_object"}
    return await client.chat.completions.create(**params)


async def _one_call(domain: str, avoid: list[str]) -> Tweet:
    global _json_mode_supported

    if client is None:
        raise GenerationError("OPENROUTER_API_KEY 沒有設定")

    kwargs = {
        "model": X_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(domain, avoid)},
        ],
        "temperature": 1.0,
        "max_tokens": X_MAX_TOKENS,
    }

    try:
        response = await _create(kwargs, _json_mode_supported)
    except BadRequestError as exc:
        if not _json_mode_supported:
            raise
        logger.warning("%s 不支援 response_format=json_object，之後改用純提示詞約束：%s",
                       X_MODEL, exc)
        _json_mode_supported = False
        response = await _create(kwargs, False)

    if not response.choices:
        raise GenerationError("模型回傳了空的 choices")

    choice = response.choices[0]
    finish = getattr(choice, "finish_reason", "?")
    text = (choice.message.content or "").strip()
    if not text:
        # Same trick as 小红书: a reasoning model may leave the JSON in its trace.
        text = xhs._reasoning_text(choice.message).strip()
        if text:
            logger.warning("content 為空，改從 reasoning 欄位取 JSON")
    if not text:
        raise GenerationError(f"模型回傳了空內容（finish_reason={finish}）")

    try:
        return _normalize(xhs._extract_json(text, is_usable=_is_usable), domain)
    except xhs.GenerationError as exc:
        if finish == "length":
            raise GenerationError(f"{exc}。finish_reason=length，輸出被截斷，建議調高 X_MAX_TOKENS") from exc
        raise


async def generate_tweet(domain: str | None = None) -> Tweet:
    """Generate one tweet, retrying once before giving up."""
    state = load_state()
    chosen = domain or take_domain(state)
    avoid = xhs.recent_topics(state, days=X_AVOID_DAYS)

    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            tweet = await _one_call(chosen, avoid)
        except Exception as exc:  # noqa: BLE001 - retry once, then report
            last_error = exc
            logger.warning("推文生成第 %d 次失敗：%s", attempt, exc, exc_info=True)
            continue

        state.setdefault("history", []).append(
            {
                "date": date.today().isoformat(),
                "domain": chosen,
                "topic": tweet.topic,
                "title": tweet.text[:30],
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
