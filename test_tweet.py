"""Exercise tweet generation, length fitting, and the approval flow.

No credentials, no network: run `python test_tweet.py`.
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test",
    "ADMIN_CHAT_ID": "424242",
    "XHS_STATE_FILE": os.path.join(tempfile.mkdtemp(), "xhs.json"),
    "X_TWEET_STATE_FILE": os.path.join(tempfile.mkdtemp(), "tw.json"),
})
import openai
import tweet as tw
import xhs

# --- weighted length: CJK counts double, which is why 140 is the limit ------
assert tw.weighted_length("abc") == 3
assert tw.weighted_length("中文") == 4
assert tw.weighted_length("中a") == 3
assert tw.weighted_length("！") == 2, "full-width punctuation counts 2"
assert tw.weighted_length("中" * 140) == 280, "140 CJK chars is exactly X's budget"
print("PASS weighted_length matches X's counting (140 CJK == 280)")

# --- fitting: drop whole sentences, never cut mid-thought -------------------
short = "同事為了加薪三千換工作。你會換嗎？"
assert tw.fit_tweet(short) == short
print("PASS a short tweet is left alone")

long_text = "。".join(f"這是第{i}句話，寫得有點長用來測試裁切行為" for i in range(1, 12)) + "。"
fitted = tw.fit_tweet(long_text)
assert len(fitted) <= tw.TWEET_CHAR_LIMIT, len(fitted)
assert tw.weighted_length(fitted) <= 280, tw.weighted_length(fitted)
assert fitted.endswith("。"), f"must end on a sentence boundary: {fitted[-12:]!r}"
assert fitted and long_text.startswith(fitted[:10])
print(f"PASS an over-long tweet trims to {len(fitted)} chars, ending on a sentence")

one_sentence = "這是一整句沒有任何標點符號的超長句子" * 12
hard = tw.fit_tweet(one_sentence)
assert len(hard) <= tw.TWEET_CHAR_LIMIT and hard.endswith("…"), (len(hard), hard[-5:])
print("PASS a single over-long sentence is hard-cut with an ellipsis")

# --- URLs are stripped ------------------------------------------------------
assert "http" not in tw.strip_urls("看這個 https://example.com/x 很讚")
assert "www." not in tw.strip_urls("去 www.example.com 看看")
assert tw.strip_urls("沒有網址的文字") == "沒有網址的文字"
print("PASS URLs are removed, plain text untouched")

# --- the prompt is stored verbatim ------------------------------------------
SUPPLIED = """You are a sharp, independent builder who posts on X about AI and AI agents. Write ONE original tweet in English.

Rules:
- English only. No Chinese characters, no translation-style phrasing.
- Under 270 characters total, including hashtags.
- Sound like a real person sharing a thought, not a brand or a press release.
- Pick ONE angle per tweet: a practical tip, a hot take, a lesson from building with AI agents, a tool you find useful, or a prediction.
- Be specific. Concrete examples beat vague hype. Avoid words like "revolutionary", "game-changer", "unlock", "delve".
- Short sentences. Line breaks are fine. At most 1 emoji, or none.
- 0\u20132 relevant hashtags at the end (e.g. #AI #AIAgents). Never more than 2.
- No links, no @mentions, no quotation marks around the whole tweet.

Recent tweets (do not repeat these topics or openings):
{recent_tweets}

Output ONLY the tweet text. No explanation, no preamble."""

assert tw.TWEET_PROMPT == SUPPLIED, "the stored prompt must stay byte-identical"
print("PASS TWEET_PROMPT matches the supplied prompt exactly")

built = tw._build_prompt(["Agents fail on undefined tasks.", "AI code review is the bottleneck."])
assert "{recent_tweets}" not in built, "the placeholder must be filled"
assert built.replace(
    "- Agents fail on undefined tasks.\n- AI code review is the bottleneck.",
    "{recent_tweets}") == SUPPLIED, "only the placeholder may differ"
print("PASS {recent_tweets} is filled from history and nothing else is altered")

assert "(none yet)" in tw._build_prompt([])
print("PASS an empty history renders '(none yet)', not a bare placeholder")

# --- plain-text parsing ------------------------------------------------------
raw = "Most agent failures aren't reasoning failures.\n\nThey're underspecified tasks.\n\n#AIAgents"
t1 = tw._parse_tweet(raw, "d")
assert t1.text == raw and t1.tags == [] and t1.full_text() == raw
print("PASS bare text passes through untouched, hashtags stay inline")

assert tw._parse_tweet('"Quoted whole tweet."', "d").text == "Quoted whole tweet."
assert tw._parse_tweet("\u201cSmart quotes too.\u201d", "d").text == "Smart quotes too."
print("PASS surrounding quotes are stripped (the prompt forbids them)")

got = tw._parse_tweet("Tip. #AI #AIAgents #LLM #MLOps", "d").text
assert got.count("#") == tw.MAX_TAGS and "#LLM" not in got, got
print(f"PASS hashtags capped at {tw.MAX_TAGS}: {got!r}")

assert "http" not in tw._parse_tweet("See https://example.com for more. #AI", "d").text
print("PASS URLs are stripped")

wrapped = json.dumps({"text": "Unwrapped from JSON. #AI"}, ensure_ascii=False)
assert tw._parse_tweet(wrapped, "d").text == "Unwrapped from JSON. #AI"
print("PASS a model that returns JSON anyway is still unwrapped")

long_en = " ".join(f"Sentence number {i} here." for i in range(1, 40))
fitted = tw._parse_tweet(long_en, "d").text
assert len(fitted) <= tw.TWEET_CHAR_LIMIT and fitted.endswith("."), len(fitted)
print(f"PASS an over-long English tweet trims to {len(fitted)} chars on a sentence end")

for bad in ("", "   ", '""'):
    try:
        tw._parse_tweet(bad, "d")
        raise AssertionError(f"{bad!r} should raise")
    except tw.GenerationError:
        pass
print("PASS empty content still raises")

# --- recent_tweet_texts ------------------------------------------------------
from datetime import date, timedelta
st = {"history": [
    {"date": (date.today() - timedelta(days=1)).isoformat(), "text": "New style entry."},
    {"date": (date.today() - timedelta(days=2)).isoformat(), "title": "Old style entry"},
    {"date": (date.today() - timedelta(days=99)).isoformat(), "text": "Too old."},
    {"date": "garbage", "text": "Bad row."},
]}
got = tw.recent_tweet_texts(st, days=7)
assert "New style entry." in got and "Old style entry" in got, got
assert "Too old." not in got and "Bad row." not in got, got
assert got[0] == "Old style entry", f"newest first: {got}"
print("PASS recent_tweet_texts honours the window, reads pre-change rows, skips bad dates")

# --- generation reuses the xhs JSON tolerance ------------------------------
payload = "Agents don't fail on reasoning. They fail on tasks nobody defined. #AIAgents"

class Comp:
    def __init__(self, content, reasoning=None): self.content, self.reasoning = content, reasoning
    async def create(self, **kw):
        self.seen = kw
        m = types.SimpleNamespace(content=self.content, model_extra={})
        if self.reasoning is not None:
            m.reasoning = self.reasoning
            m.model_extra = {"reasoning": self.reasoning}
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=m, finish_reason="stop")])

def with_model(content, reasoning=None):
    c = Comp(content, reasoning)
    tw.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=c))
    tw._json_mode_supported = True
    return c

c = with_model(payload)
t1 = asyncio.run(tw._one_call("d", []))
assert t1.text == payload, t1.text
assert c.seen["max_tokens"] == tw.X_MAX_TOKENS and c.seen["model"] == tw.X_MODEL
assert "response_format" not in c.seen, "JSON mode would fight a bare-text prompt"
print("PASS a plain-text reply is used as-is, and no response_format is sent")

# empty content -> reasoning trace
c = with_model("", reasoning=payload)
t2 = asyncio.run(tw._one_call("d", []))
assert t2.text == payload
print("PASS empty content still falls back to the reasoning trace")

print("\nALL TWEET TESTS PASSED")
