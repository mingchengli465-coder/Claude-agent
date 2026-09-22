"""Exercise tweet generation, length fitting, and the approval flow.

No credentials, no network: run `python test_tweet.py`.
"""
import asyncio
import json
import os
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

# --- normalising ------------------------------------------------------------
n = tw._normalize({"topic": "t", "text": "觀點很明確的一句話。你怎麼看？",
                   "tags": ["#職場", "通勤"]}, "消費觀")
assert n.tags == ["職場", "通勤"] and n.text.endswith("你怎麼看？")
assert "#職場" in n.full_text() and n.full_text().startswith("觀點")
print("PASS normalize cleans tags and composes the posted text")

n = tw._normalize({"text": "內容", "tags": "一, 二 三, 四"}, "消費觀")
assert n.tags == ["一", "二", "三"], n.tags
print("PASS a comma-separated tag string is split and capped at 3")

n = tw._normalize({"內容": "用中文鍵的正文"}, "消費觀")
assert n.text == "用中文鍵的正文", n.text
print("PASS Chinese key names work (reusing xhs._pick)")

try:
    tw._normalize({"topic": "x"}, "消費觀")
    raise AssertionError("empty text must raise")
except tw.GenerationError as exc:
    assert "為空" in str(exc) and "鍵" in str(exc), exc
print("PASS empty text fails with the returned keys named")

# tags are dropped rather than overflowing the post
n = tw._normalize({"text": "中" * 138, "tags": ["一個很長的標籤", "另一個"]}, "消費觀")
assert tw.weighted_length(n.full_text()) <= 280, tw.weighted_length(n.full_text())
print(f"PASS hashtags are dropped when they would overflow ({len(n.tags)} kept)")

# --- domain rotation --------------------------------------------------------
st = {"domain_index": 0}
seen = [tw.take_domain(st) for _ in range(len(tw.DOMAINS) + 1)]
assert seen[:len(tw.DOMAINS)] == tw.DOMAINS and seen[-1] == tw.DOMAINS[0]
assert all("一" <= c <= "鿿" or not c.isalpha() or c.isascii()
           for c in "".join(tw.DOMAINS))
print("PASS domains rotate and wrap:", " → ".join(seen[:3]), "…")

# --- generation reuses the xhs JSON tolerance ------------------------------
GOOD = {"topic": "選題", "text": "一句很有觀點的話。你同意嗎？", "tags": ["職場"]}
payload = json.dumps(GOOD, ensure_ascii=False)

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

# fenced + preamble, the usual small-model output
c = with_model(f"好的：\n```json\n{payload}\n```")
t1 = asyncio.run(tw._one_call("消費觀", []))
assert t1.text.startswith("一句很有觀點"), t1
assert c.seen["max_tokens"] == tw.X_MAX_TOKENS and c.seen["model"] == tw.X_MODEL
print("PASS fenced+chatty output parses, and X_MODEL / max_tokens are sent")

# the schema-skeleton trap that bit 小红书
c = with_model(f'先想一下：{{"text": ""}}\n\n正式寫：{payload}')
t2 = asyncio.run(tw._one_call("消費觀", []))
assert t2.text.startswith("一句很有觀點"), f"picked the skeleton: {t2.text!r}"
print("PASS an empty skeleton before the real tweet doesn't win")

# empty content -> reasoning trace
c = with_model("", reasoning=payload)
t3 = asyncio.run(tw._one_call("消費觀", []))
assert t3.text.startswith("一句很有觀點")
print("PASS empty content falls back to the reasoning trace")

print("\nALL TWEET TESTS PASSED")
