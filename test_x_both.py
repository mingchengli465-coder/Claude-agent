"""Exercise the combined post+reply loop."""
import os, sys, tempfile, types, time as _time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

tmp = tempfile.mkdtemp()
topics = os.path.join(tmp, "topics.txt"); open(topics,"w").write("theme one\ntheme two\n")
os.environ.update({
    "ANTHROPIC_API_KEY":"sk-test","X_API_KEY":"k","X_API_SECRET":"s",
    "X_ACCESS_TOKEN":"t","X_ACCESS_TOKEN_SECRET":"ts",
    "STATE_FILE": os.path.join(tmp,"state.json"), "TOPICS_FILE": topics,
    "POST_TIMEZONE":"Asia/Shanghai", "POST_JITTER_MINUTES":"30",
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import x_bot, tweepy, anthropic
TZ = ZoneInfo("Asia/Shanghai")

# --- jitter must be stable for a given slot, or the post time would drift ---
now = datetime(2026, 9, 20, 7, 0, tzinfo=TZ)
times = [(9, 0)]
a = [x_bot.slot_target(now, times) for _ in range(5)]
b = x_bot.slot_target(now + timedelta(minutes=1), times)
assert len({t for _, t in a}) == 1, f"jitter drifted across calls: {a}"
assert a[0] == b, "same slot must keep the same target as the loop re-checks"
sid, target = a[0]
assert sid.startswith("2026-09-20T09:00") and 0 <= (target - datetime(2026,9,20,9,0,tzinfo=TZ)).total_seconds()/60 <= 30
print(f"PASS jitter is stable: slot {sid} -> fires {target.strftime('%H:%M')}")

# different slots get different (independent) jitter
sid2, t2 = x_bot.slot_target(datetime(2026,9,21,7,0,tzinfo=TZ), times)
assert sid2.startswith("2026-09-21T09:00")
print("PASS each day gets its own jitter:", t2.strftime("%H:%M"))

# --- run_post_slot return semantics -----------------------------------------
class FakeX:
    def __init__(self): self.posted=[]
    def create_tweet(self, text=None, in_reply_to_tweet_id=None):
        self.posted.append(text); return types.SimpleNamespace(data={"id":1})
fx = FakeX()

x_bot.ask_claude = lambda s,u: "a post"
st = {}
assert x_bot.run_post_slot(fx, st, "S1") is True and st["last_slot"] == "S1"
print("PASS success burns the slot")

def boom(exc):
    def f(*a, **k): raise exc
    return f

def fake_resp(code):
    """tweepy's HTTP exceptions read several fields off the response."""
    return types.SimpleNamespace(status_code=code, json=lambda: {"errors": []},
                                 text="", headers={}, reason="test")
for exc, expect_done in (
    (tweepy.TooManyRequests(fake_resp(429)), False),
    (anthropic.APIConnectionError(request=None), False),
):
    st2 = {}
    orig = x_bot.publish_one; x_bot.publish_one = boom(exc)
    got = x_bot.run_post_slot(fx, st2, "S2")
    x_bot.publish_one = orig
    assert got is expect_done, f"{type(exc).__name__}: expected {expect_done}, got {got}"
    assert "last_slot" not in st2, f"{type(exc).__name__} must not burn the slot"
print("PASS transient failures leave the slot retryable")

st3 = {}
orig = x_bot.publish_one
x_bot.publish_one = boom(tweepy.Forbidden(fake_resp(403)))
assert x_bot.run_post_slot(fx, st3, "S3") is True and st3["last_slot"] == "S3"
x_bot.publish_one = orig
print("PASS a permanent refusal burns the slot instead of looping forever")

# --- run_mention_poll swallows everything -----------------------------------
orig_poll = x_bot.poll_once
for exc in (tweepy.TweepyException("x"), ValueError("y")):
    x_bot.poll_once = boom(exc)
    x_bot.run_mention_poll(None, None, {})   # must not raise
x_bot.poll_once = orig_poll
print("PASS mention failures never escape the loop")

# --- integration: the merged loop does both jobs ----------------------------
polls, posts = [], []
x_bot.poll_once = lambda c, me, s: polls.append(1)
x_bot.publish_one = lambda c, s, topic=None: posts.append(1)
x_bot.build_x_client = lambda: types.SimpleNamespace(
    get_me=lambda **k: types.SimpleNamespace(data=types.SimpleNamespace(id=1, username="me")))
x_bot.POLL_INTERVAL_SECONDS = 60
x_bot.POST_JITTER_MINUTES = 0

real_dt, real_sleep, real_mono = x_bot.datetime, _time.sleep, _time.monotonic
BASE = datetime.now(tz=TZ).replace(second=0, microsecond=0)
clock = {"offset": 0.0}          # seconds advanced by our fake sleeps

# Post two minutes into the run; mentions poll every 60s.
fire = BASE + timedelta(minutes=2)
x_bot.POST_TIMES = f"{fire.hour:02d}:{fire.minute:02d}"

class FakeDT:
    @staticmethod
    def now(tz=None): return BASE + timedelta(seconds=clock["offset"])
class Stop(Exception): pass

steps = {"n": 0}
def fake_sleep(s):
    steps["n"] += 1
    if steps["n"] > 12: raise Stop
    clock["offset"] += max(s, 0.001)   # never advance by zero: that would spin

x_bot.datetime = FakeDT
x_bot.time.sleep = fake_sleep
x_bot.time.monotonic = lambda: clock["offset"]
try:
    x_bot.cmd_both(types.SimpleNamespace())
except Stop:
    pass
finally:
    x_bot.datetime = real_dt
    x_bot.time.sleep = real_sleep
    x_bot.time.monotonic = real_mono

assert len(polls) >= 2, f"mentions should be polled repeatedly, got {len(polls)}"
assert len(posts) == 1, f"the slot should fire exactly once, got {len(posts)}"
assert steps["n"] > 1, "the loop must sleep between jobs, never spin"
print(f"PASS merged loop: {len(polls)} mention checks, {len(posts)} post, no spin")
print("\nALL COMBINED-LOOP TESTS PASSED")
