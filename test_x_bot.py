"""Exercise x_bot's mention handling against a fake X API and a fake Claude.

No credentials and no network needed: run `python test_x_bot.py`.
"""
import json
import os
import pathlib
import sys
import tempfile
import types

tmp = tempfile.mkdtemp()
os.environ.update({
    "ANTHROPIC_API_KEY": "sk-test",
    "X_API_KEY": "k", "X_API_SECRET": "s",
    "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts",
    "STATE_FILE": os.path.join(tmp, "state.json"),
    "MAX_REPLIES_PER_CYCLE": "2",
    "LOG_LEVEL": "INFO",
})
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import x_bot

class Ref:
    def __init__(self, type, id): self.type, self.id = type, id
class Tweet:
    def __init__(self, id, author_id, text, refs=None):
        self.id, self.author_id, self.text = id, author_id, text
        self.referenced_tweets = refs
class User:
    def __init__(self, id, username): self.id, self.username = id, username
class Resp:
    def __init__(self, data, includes=None, meta=None):
        self.data, self.includes, self.meta = data, includes or {}, meta or {}

ME = User(1, "mybot")
ALICE, BOB = User(2, "alice"), User(3, "bob")

class FakeX:
    def __init__(self, pages): self.pages, self.posted, self.get_tweet_calls = pages, [], 0
    def get_me(self, **kw): return Resp(ME)
    def get_users_mentions(self, id, **kw):
        assert kw.get("user_auth") is True, "mentions must use OAuth 1.0a user context"
        return self.pages.pop(0) if self.pages else Resp(None)
    def get_tweet(self, id, **kw):
        self.get_tweet_calls += 1
        return Resp(Tweet(id, 3, f"ancestor {id}"), includes={"users": [BOB]})
    def create_tweet(self, text=None, in_reply_to_tweet_id=None):
        self.posted.append((text, in_reply_to_tweet_id))
        return types.SimpleNamespace(data={"id": 9000 + len(self.posted)})

prompts = []
def fake_ask(system, user_content):
    prompts.append(user_content)
    return "Hi! Here's a short answer."
x_bot.ask_claude = fake_ask

# --- run 1: first run must skip the backlog but record the high-water mark ---
m1 = Tweet(101, 2, "@mybot hello there")
fx = FakeX([Resp([m1], includes={"users": [ALICE, ME]}, meta={"newest_id": 101})])
state = x_bot.load_state()
x_bot.poll_once(fx, ME, state)
assert fx.posted == [], f"first run should not post, got {fx.posted}"
assert state["since_id"] == "101", state
print("PASS first run skips backlog, since_id =", state["since_id"])

# --- run 2: new mentions, oldest first, capped at MAX_REPLIES_PER_CYCLE ---
m2 = Tweet(102, 2, "@mybot what's the weather?", refs=[Ref("replied_to", 55)])
m3 = Tweet(103, 3, "@mybot and you?")
m4 = Tweet(104, 2, "@mybot third one")
m_self = Tweet(105, 1, "@mybot talking to myself")
m_rt = Tweet(106, 3, "RT something", refs=[Ref("retweeted", 77)])
parent = Tweet(55, 3, "the original tweet")
fx.pages = [Resp([m_rt, m_self, m4, m3, m2],
                 includes={"users": [ALICE, BOB, ME], "tweets": [parent]},
                 meta={"newest_id": 106})]
x_bot.poll_once(fx, ME, state)
print("posted:", fx.posted)
assert len(fx.posted) == 2, f"expected cap of 2 replies, got {len(fx.posted)}"
assert fx.posted[0][1] == "102" and fx.posted[1][1] == "103", fx.posted
assert fx.get_tweet_calls == 0, "parent was in includes; no extra API call expected"
assert "the original tweet" in prompts[0], prompts[0]
assert prompts[0].index("the original tweet") < prompts[0].index("what's the weather"), "thread must be oldest-first"
assert "104" not in [p[1] for p in fx.posted]
print("PASS cap, ordering, self/RT filtering, includes reuse")

# --- run 3: dedupe via replied_to even if since_id rewinds ---
fx.posted.clear()
state["since_id"] = "101"
fx.pages = [Resp([m2], includes={"users": [ALICE, ME], "tweets": [parent]}, meta={"newest_id": 102})]
x_bot.poll_once(fx, ME, state)
assert fx.posted == [], f"already-answered mention must not be re-answered: {fx.posted}"
print("PASS dedupe on replay")

# --- run 4: long reply becomes a thread ---
x_bot.ask_claude = lambda s, u: "word " * 200
fx.posted.clear()
m7 = Tweet(107, 2, "@mybot tell me everything")
fx.pages = [Resp([m7], includes={"users": [ALICE, ME]}, meta={"newest_id": 107})]
x_bot.poll_once(fx, ME, state)
ids = [p[1] for p in fx.posted]
assert len(fx.posted) >= 2 and ids[0] == "107" and ids[1] == "9001", fx.posted
assert all(len(p[0]) <= 280 for p in fx.posted), [len(p[0]) for p in fx.posted]
print("PASS long reply threads:", [len(p[0]) for p in fx.posted], "chained as", ids)

# --- run 5: ancestor not in includes triggers exactly one get_tweet ---
x_bot.ask_claude = fake_ask
fx.posted.clear(); fx.get_tweet_calls = 0
m8 = Tweet(108, 2, "@mybot follow-up", refs=[Ref("replied_to", 66)])
fx.pages = [Resp([m8], includes={"users": [ALICE, ME]}, meta={"newest_id": 108})]
x_bot.poll_once(fx, ME, state)
assert fx.get_tweet_calls == 1, fx.get_tweet_calls
print("PASS ancestor fetch fallback")

# --- run 6: Claude refusal marks handled without posting ---
x_bot.ask_claude = lambda s, u: None
fx.posted.clear()
m9 = Tweet(109, 2, "@mybot something disallowed")
fx.pages = [Resp([m9], includes={"users": [ALICE, ME]}, meta={"newest_id": 109})]
x_bot.poll_once(fx, ME, state)
assert fx.posted == [] and "109" in state["replied_to"], (fx.posted, state["replied_to"][-3:])
print("PASS refusal handled")

# --- state round-trip ---
on_disk = json.loads(pathlib.Path(os.environ["STATE_FILE"]).read_text())
assert on_disk["since_id"] == "109" and "102" in on_disk["replied_to"], on_disk
print("PASS state persisted:", on_disk["since_id"], len(on_disk["replied_to"]), "ids")
print("\nALL TESTS PASSED")
