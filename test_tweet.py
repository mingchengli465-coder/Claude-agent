"""Exercise tweet generation, length fitting, and the approval flow.

No credentials, no network: run `python test_tweet.py`.
"""
import asyncio
import json
import os
import pathlib
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

# --- the prompt sells the design service, in English -------------------------
built = tw._build_prompt(tw.ENGLISH, ["Your homepage has one job.", "Slide 1 should say what you do."],
                         tw.DOMAINS[1])
for slot in ("{language}", "{recent_tweets}", "{service}", "{domain}"):
    assert slot not in built, f"{slot} left unfilled"
assert tw.X_SERVICE_BRIEF in built, "the model must see what it is selling"
assert "This tweet's focus: pitch decks for founders" in built
assert "- Your homepage has one job.\n- Slide 1 should say what you do." in built
assert "Write ONE original tweet in English" in built
assert "220\u2013270 characters" in built and "never over 270" in built, "tweets should use the full length"
for rule in ("Never invent clients", "No prices", "call to action", "No links"):
    assert rule in built, f"rule missing: {rule}"
assert "(none yet)" in tw._build_prompt(tw.ENGLISH, [])
print("PASS the prompt carries the service brief, the focus, the history and the honesty rules")

assert all(any(k in d for k in ("website", "deck", "landing", "presentation")) for d in tw.DOMAINS), tw.DOMAINS
assert all(ord(c) < 128 for d in tw.DOMAINS for c in d), "domains are English now"
for word in ("website", "PowerPoint", "DM"):
    assert word in tw.DEFAULT_SERVICE_BRIEF
print(f"PASS {len(tw.DOMAINS)} design service lines rotate")

# --- English only by default -------------------------------------------------
assert tw.ENGLISH_RATIO == 1.0, tw.ENGLISH_RATIO
assert {tw.pick_language() for _ in range(200)} == {tw.ENGLISH}
saved = tw.ENGLISH_RATIO
tw.ENGLISH_RATIO = 0.0
assert {tw.pick_language() for _ in range(50)} == {tw.CHINESE}
tw.ENGLISH_RATIO = saved
print("PASS every tweet is drawn in English; X_ENGLISH_RATIO still overrides it")

# --- the model -------------------------------------------------------------
assert tw.X_MODEL == "google/gemma-4-26b-a4b-it:free", tw.X_MODEL
print(f"PASS the tweet model is {tw.X_MODEL}")

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

# ~1.5x the limit: a real over-long draft. Far longer is treated as notes (below).
long_en = " ".join(f"Sentence number {i} here." for i in range(1, 18))
fitted = tw._parse_tweet(long_en, "d", tw.ENGLISH).text
assert len(fitted) <= tw.TWEET_CHAR_LIMIT and fitted.endswith("."), len(fitted)
print(f"PASS an over-long English tweet trims to {len(fitted)} chars (limit {tw.TWEET_CHAR_LIMIT})")

# Chinese has its own, lower ceiling.
long_zh = "。".join(f"这是第{i}句话用来测试中文的裁切" for i in range(1, 14)) + "。"
fitted_zh = tw._parse_tweet(long_zh, "d", tw.CHINESE).text
assert len(fitted_zh) <= tw.CHINESE_CHAR_LIMIT, (len(fitted_zh), tw.CHINESE_CHAR_LIMIT)
assert tw.weighted_length(fitted_zh) <= 280, tw.weighted_length(fitted_zh)
assert fitted_zh.endswith("。")
print(f"PASS a Chinese tweet trims to {len(fitted_zh)} chars (limit {tw.CHINESE_CHAR_LIMIT}), "
      f"weight {tw.weighted_length(fitted_zh)}/280")

# The same over-long Chinese text would be left far too long under the English
# limit, which is why the limit is per language rather than global.
assert len(tw._parse_tweet(long_zh, "d", tw.ENGLISH).text) > tw.CHINESE_CHAR_LIMIT
print("PASS the English limit would not have caught it — the split matters")

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
t1 = asyncio.run(tw._one_call("d", [], tw.ENGLISH))
assert t1.text == payload, t1.text
assert c.seen["max_tokens"] == tw.X_MAX_TOKENS
assert c.seen["model"] == "google/gemma-4-26b-a4b-it:free", c.seen["model"]
assert "response_format" not in c.seen, "JSON mode would fight a bare-text prompt"
assert "This tweet's focus: d\n" in c.seen["messages"][0]["content"], "the rotated domain must reach the model"
print("PASS a plain-text reply is used as-is, and no response_format is sent")

# empty content must NOT fall back to the reasoning trace (it got posted once)
c = with_model("", reasoning=payload)
try:
    asyncio.run(tw._one_call("d", [], tw.ENGLISH))
    raise AssertionError("empty content must fail, not borrow the reasoning trace")
except tw.GenerationError as exc:
    assert "空內容" in str(exc), exc
print("PASS empty content fails instead of posting the reasoning trace")

# --- working notes are never posted -------------------------------------------
# The reply that went public on 2026-09-25, as the model produced it.
LEAKED = ("1. **Analyze the Request:**\n    * **Role:** Small independent design studio owner.\n"
          "    * **Platform:** X (Twitter).\n    * **Goal:** Win clients.\n"
          "2. **Draft 1:** Your homepage has five seconds...\n" + "More planning. " * 400)
for bad in (LEAKED, LEAKED[:300], "Okay, so the user wants a tweet about landing pages.",
            "Constraints: under 270 chars. Tone: friendly."):
    assert tw._looks_like_notes(bad), bad[:60]
    try:
        tw._parse_tweet(bad, "d")
        raise AssertionError(f"notes were accepted as a tweet: {bad[:60]!r}")
    except tw.GenerationError as exc:
        assert "不是推文" in str(exc), exc
c = with_model(LEAKED)
try:
    asyncio.run(tw._one_call("d", [], tw.ENGLISH))
    raise AssertionError("the leaked trace must not become a tweet")
except tw.GenerationError:
    pass

GOOD_TWEETS = [
    "3 signs your site needs a redesign:\n1. It takes a scroll to see what you do.\n"
    "2. The button says Submit.\n3. It looks different on a phone.\nI build custom sites that fix all three. DMs open.\n#WebDesign",
    "Your pitch deck isn't a script. Let me say that again: slides back you up, they don't talk for you.\n"
    "One idea per slide, a headline that states the point.\nI design decks built that way. DM me if that's you.",
    payload,
]
for good in GOOD_TWEETS:
    assert tw._looks_like_notes(good) == "", (good[:40], tw._looks_like_notes(good))
    assert tw._parse_tweet(good, "d").text
print("PASS drafting notes are refused; lists and 'let me' in real tweets still pass")

# --- a rate-limited X_MODEL retries on the fallback model -------------------
# The 429 that broke the 12:00 post: gemma's shared free pool was exhausted and
# the immediate retry on the same model failed the same way.
assert tw.X_FALLBACK_MODEL == "openrouter/free", tw.X_FALLBACK_MODEL

class Flaky:
    def __init__(self): self.models = []
    async def create(self, **kw):
        self.models.append(kw["model"])
        if kw["model"] == tw.X_MODEL:
            raise RuntimeError("Error code: 429 - temporarily rate-limited upstream")
        m = types.SimpleNamespace(content=payload, model_extra={})
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=m, finish_reason="stop")])

flaky = Flaky()
tw.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=flaky))
tw.X_TWEET_STATE_FILE = pathlib.Path(tempfile.mkdtemp()) / "fallback.json"
got = asyncio.run(tw.generate_tweet("d"))
assert got.text == payload, got.text
assert flaky.models == [tw.X_MODEL, "openrouter/free"], flaky.models
print("PASS a rate-limited X_MODEL is retried once on openrouter/free")

class Down(Flaky):
    async def create(self, **kw):
        self.models.append(kw["model"])
        raise RuntimeError("429")
down = Down()
tw.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=down))
try:
    asyncio.run(tw.generate_tweet("d"))
    raise AssertionError("should have failed")
except tw.GenerationError:
    pass
assert down.models == [tw.X_MODEL, "openrouter/free"], "still exactly two calls"
print("PASS when both fail it still stops after two calls")

# --- the Telegram link reply --------------------------------------------------
for raw in ("https://t.me/vinc_design", "t.me/vinc_design", "@vinc_design", "vinc_design",
            "https://telegram.me/vinc_design/", "  http://www.t.me/vinc_design "):
    assert tw.telegram_link(raw) == "https://t.me/vinc_design", (raw, tw.telegram_link(raw))
assert tw.telegram_link("") == "" and tw.telegram_link("@") == ""

tw.X_TELEGRAM_LINK = ""
assert tw.link_reply_text() == ""
assert asyncio.run(tw.post_link_reply("https://x.com/u/status/1")) is None, "off when unset"

tw.X_TELEGRAM_LINK = "@vinc_design"
reply = tw.link_reply_text()
assert "https://t.me/vinc_design" in reply and "{link}" not in reply, reply
assert tw.weighted_length(reply) <= 280, tw.weighted_length(reply)

class FakeX:
    def __init__(self): self.calls = []
    def create_tweet(self, **kw):
        self.calls.append(kw)
        return types.SimpleNamespace(data={"id": "999"})
fx = FakeX()
saved_build = tw.build_x_client
tw.build_x_client = lambda: fx
got = asyncio.run(tw.post_link_reply("https://x.com/Vincent40769988/status/2103354135085252739"))
tw.build_x_client = saved_build
tw.X_TELEGRAM_LINK = ""
assert got == "999"
assert fx.calls == [{"text": reply, "in_reply_to_tweet_id": "2103354135085252739"}], fx.calls
print("PASS the Telegram link is normalised and posted as a reply under the tweet")

print("\nALL TWEET TESTS PASSED")
