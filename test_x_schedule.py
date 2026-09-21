"""Exercise the scheduled-posting logic against a fake X API and a fake Claude."""
import json, os, sys, tempfile, types
from datetime import datetime
from zoneinfo import ZoneInfo

tmp = tempfile.mkdtemp()
topics = os.path.join(tmp, "topics.txt")
open(topics, "w").write("# comment\n\nalpha theme\nbeta theme\n  gamma theme  \n")
os.environ.update({
    "ANTHROPIC_API_KEY": "sk-test", "X_API_KEY": "k", "X_API_SECRET": "s",
    "X_ACCESS_TOKEN": "t", "X_ACCESS_TOKEN_SECRET": "ts",
    "STATE_FILE": os.path.join(tmp, "state.json"),
    "TOPICS_FILE": topics, "POST_HISTORY_SIZE": "3", "POST_TIMEZONE": "Asia/Shanghai",
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x_bot

TZ = ZoneInfo("Asia/Shanghai")

# --- parse_times ---
assert x_bot.parse_times("09:00") == [(9, 0)]
assert x_bot.parse_times(" 18:30 , 08:05,18:30 ") == [(8, 5), (18, 30)]
for bad in ("9am", "25:00", "09:60", ""):
    try:
        x_bot.parse_times(bad); raise AssertionError(f"{bad!r} should be rejected")
    except SystemExit:
        pass
print("PASS parse_times accepts good input and rejects bad")

# --- next_slot ---
times = [(8, 5), (18, 30)]
cases = [
    (datetime(2026, 9, 20, 7, 0, tzinfo=TZ),  datetime(2026, 9, 20, 8, 5, tzinfo=TZ)),
    (datetime(2026, 9, 20, 8, 5, tzinfo=TZ),  datetime(2026, 9, 20, 18, 30, tzinfo=TZ)),  # strictly after
    (datetime(2026, 9, 20, 12, 0, tzinfo=TZ), datetime(2026, 9, 20, 18, 30, tzinfo=TZ)),
    (datetime(2026, 9, 20, 23, 0, tzinfo=TZ), datetime(2026, 9, 21, 8, 5, tzinfo=TZ)),   # rolls to tomorrow
]
for now, want in cases:
    got = x_bot.next_slot(now, times)
    assert got == want, f"from {now} expected {want}, got {got}"
print("PASS next_slot picks the right slot and rolls over midnight")

# --- topics ---
loaded = x_bot.load_topics()
assert loaded == ["alpha theme", "beta theme", "gamma theme"], loaded
st = {}
seq = [x_bot.take_topic(st, loaded) for _ in range(7)]
assert seq == ["alpha theme","beta theme","gamma theme"]*2 + ["alpha theme"], seq
print("PASS topics parse and rotate in order, wrapping cleanly")

# --- publish_one ---
class FakeX:
    def __init__(self): self.posted = []
    def create_tweet(self, text=None, in_reply_to_tweet_id=None):
        self.posted.append(text)
        return types.SimpleNamespace(data={"id": 500 + len(self.posted)})

seen_prompts = []
x_bot.ask_claude = lambda s, u: (seen_prompts.append(u), f"post #{len(seen_prompts)}")[1]

fx = FakeX(); state = x_bot.load_state()
for _ in range(4):
    x_bot.publish_one(fx, state)
assert fx.posted == ["post #1","post #2","post #3","post #4"], fx.posted
assert [p["topic"] for p in state["posts"]] == ["alpha theme","beta theme","gamma theme","alpha theme"]
print("PASS publish_one posts and records topic rotation:", [p["topic"] for p in state["posts"]])

# Claude must be shown recent posts, capped at POST_HISTORY_SIZE (3)
last = seen_prompts[-1]
assert "post #1" in last and "post #3" in last, "recent posts must reach the prompt"
assert last.count("\n- post #") == 3, last.count("\n- post #")
assert "Do not repeat their ideas" in last
print("PASS anti-repetition: last prompt carried exactly 3 recent posts")

# --- refusal ---
x_bot.ask_claude = lambda s, u: None
before = len(state["posts"]); fx.posted.clear()
assert x_bot.publish_one(fx, state) is None
assert fx.posted == [] and len(state["posts"]) == before
print("PASS refusal posts nothing and records nothing")

# --- explicit topic override skips the rotation ---
x_bot.ask_claude = lambda s, u: "manual one"
idx_before = state["topic_index"]
x_bot.publish_one(fx, state, topic="a one-off announcement")
assert state["topic_index"] == idx_before, "explicit topic must not advance the rotation"
assert state["posts"][-1]["topic"] == "a one-off announcement"
print("PASS --topic override does not disturb the rotation")

on_disk = json.loads(open(os.environ["STATE_FILE"]).read())
assert on_disk["topic_index"] == idx_before and len(on_disk["posts"]) >= 4
print("PASS state persisted across the run")
print("\nALL SCHEDULER TESTS PASSED")
