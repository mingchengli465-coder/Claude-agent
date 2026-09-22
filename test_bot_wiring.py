"""Verify the Telegram wiring: handlers, admin gate, and the daily job.

No network, no token needed: run `python test_bot_wiring.py`.
"""
import asyncio, datetime as dt, os, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test",
    "ADMIN_CHAT_ID": "424242",
    "XHS_STATE_FILE": os.path.join(tempfile.mkdtemp(), "s.json"),
    "XHS_DAILY_TIME": "09:00", "XHS_TIMEZONE": "Asia/Taipei",
})
import bot, xhs
from telegram.ext import Application

# --- admin gate -------------------------------------------------------------
assert bot.is_admin(424242) and bot.is_admin("424242")
assert not bot.is_admin(999) and not bot.is_admin(None)
saved = bot.ADMIN_CHAT_ID
bot.ADMIN_CHAT_ID = ""
assert not bot.is_admin(424242), "with ADMIN_CHAT_ID unset nobody may be admin"
bot.ADMIN_CHAT_ID = saved
print("PASS admin gate: only ADMIN_CHAT_ID passes, unset locks everyone out")

# --- JobQueue must exist (needs the [job-queue] extra) ----------------------
app = Application.builder().token("123:fake").build()
assert app.job_queue is not None, "JobQueue missing — requirements needs the [job-queue] extra"
bot.schedule_daily_note(app)
jobs = app.job_queue.jobs()
assert len(jobs) == 1 and jobs[0].name == "xhs-daily", [j.name for j in jobs]
# next_t only exists once the scheduler runs, so assert on the trigger itself.
trigger = jobs[0].job.trigger
fields = {f.name: str(f) for f in trigger.fields}
assert fields["hour"] == "9" and fields["minute"] == "0", fields
assert str(trigger.timezone) == "Asia/Taipei", trigger.timezone
print(f"PASS daily job scheduled at {fields['hour']}:{fields['minute']:0>2} {trigger.timezone}")

# a bad time string must not schedule, and must not raise
app2 = Application.builder().token("123:fake").build()
bot.XHS_DAILY_TIME = "not-a-time"
bot.schedule_daily_note(app2)
assert not app2.job_queue.jobs(), "an unparseable time must skip scheduling"
bot.XHS_DAILY_TIME = "09:00"
print("PASS a malformed XHS_DAILY_TIME is refused without crashing startup")

# --- handler registration ---------------------------------------------------
app3 = Application.builder().token("123:fake").build()
app3.add_handler(bot.CommandHandler("xhs", bot.xhs_command))
app3.add_handler(bot.CallbackQueryHandler(bot.xhs_button, pattern=r"^xhs:"))
names = [type(h).__name__ for h in app3.handlers[0]]
assert "CommandHandler" in names and "CallbackQueryHandler" in names, names
print("PASS /xhs and the button handler register")

# --- non-admin /xhs is refused and never generates --------------------------
called = {"gen": 0}
async def never(*a, **k):
    called["gen"] += 1
    raise AssertionError("generation must not run for a non-admin")
xhs.generate_note = never

replies = []
class FakeMsg:
    async def reply_text(self, text, **k): replies.append(text)
class FakeUpdate:
    effective_chat = types.SimpleNamespace(id=999)     # not the admin
    effective_message = FakeMsg()
class FakeBot:
    async def send_chat_action(self, **k): pass
    async def send_message(self, **k): replies.append(k.get("text"))
    async def send_photo(self, **k): replies.append("<photo>")
ctx = types.SimpleNamespace(bot=FakeBot())

asyncio.run(bot.xhs_command(FakeUpdate(), ctx))
assert called["gen"] == 0, "a non-admin must never reach generation"
assert replies and "没有对你开放" in replies[0], replies
print("PASS /xhs from a non-admin is refused before any model call")

# --- admin /xhs sends exactly four messages, buttons on the last ------------
note = xhs.Note(domain="消费观", topic="t", title="标题", body="正文一段\n\n正文二段",
                tags=["标签一", "标签二"], cover_main=["主标一行", "主标二行"],
                cover_question="问句？", cover_small=["小一", "小二", "小三"])
async def ok(*a, **k): return note
xhs.generate_note = ok

sent = []
class AdminBot:
    async def send_chat_action(self, **k): pass
    async def send_message(self, **k): sent.append(("text", k.get("text"), k.get("reply_markup")))
    async def send_photo(self, **k): sent.append(("photo", None, k.get("reply_markup")))
class AdminUpdate:
    effective_chat = types.SimpleNamespace(id=424242)
    effective_message = FakeMsg()

replies.clear()
asyncio.run(bot.xhs_command(AdminUpdate(), types.SimpleNamespace(bot=AdminBot())))
kinds = [s[0] for s in sent]
assert kinds == ["photo", "text", "text", "text"], kinds
assert sent[1][1] == "标题" and "正文一段" in sent[2][1]
assert sent[3][1] == note.tags_text() and sent[3][1].startswith("#标签一")
assert sent[3][2] is not None, "the last message must carry the buttons"
assert all(s[2] is None for s in sent[:3]), "only the last message gets buttons"
labels = [b.text for row in sent[3][2].inline_keyboard for b in row]
assert any("重写文案" in l for l in labels) and any("换封面" in l for l in labels), labels
print(f"PASS admin /xhs sends 4 messages {kinds} with buttons {labels} on the last")

# --- generation failure notifies instead of crashing ------------------------
async def boom(*a, **k): raise xhs.GenerationError("模型挂了")
xhs.generate_note = boom
sent.clear()
asyncio.run(bot.produce_and_send(types.SimpleNamespace(bot=AdminBot()), 424242))
assert len(sent) == 1 and "失败" in sent[0][1] and "模型挂了" in sent[0][1], sent
print("PASS a generation failure notifies the admin instead of raising")



# ===========================================================================
# X (Twitter): nothing may reach X without the 發布 button
# ===========================================================================
import tweet as tweet_mod

draft = tweet_mod.Tweet(domain="消費觀", topic="t", text="一句有觀點的話。你怎麼看？",
                        tags=["職場"])
async def draft_ok(*a, **k): return draft
tweet_mod.generate_tweet = draft_ok

published = []
async def fake_publish(item):
    published.append(item)
    return "https://x.com/someone/status/1234567890"
tweet_mod.publish = fake_publish

class BtnBot:
    def __init__(self): self.msgs = []
    async def send_message(self, **k): self.msgs.append(k.get("text"))
    async def send_chat_action(self, **k): pass

class Query:
    def __init__(self, data, chat_id=424242):
        self.data, self.answered, self.markup_cleared = data, [], False
        self.message = types.SimpleNamespace(chat_id=chat_id)
    async def answer(self, text=None, show_alert=False): self.answered.append(text)
    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None

def press(data, chat_id=424242):
    q = Query(data, chat_id)
    b = BtnBot()
    upd = types.SimpleNamespace(callback_query=q)
    asyncio.run(bot.tweet_button(upd, types.SimpleNamespace(bot=b)))
    return q, b

# --- /tweet drafts but must NOT publish -------------------------------------
published.clear(); bot.pending_tweets.clear()
sent.clear()
asyncio.run(bot.tweet_command(AdminUpdate(), types.SimpleNamespace(bot=AdminBot())))
assert published == [], "drafting must never publish"
assert bot.pending_tweets.get(424242) is draft, "the draft must be held for approval"
preview = [s[1] for s in sent if s[0] == "text"]
assert any("待審核" in (p or "") for p in preview), preview
labels = [b.text for row in sent[-1][2].inline_keyboard for b in row]
assert len(labels) == 3 and any("發布" in l for l in labels) \
    and any("重寫" in l for l in labels) and any("取消" in l for l in labels), labels
print(f"PASS /tweet drafts only, holds it for approval, buttons {labels}")

# --- 取消 discards, publishes nothing ---------------------------------------
q, b = press("tweet:cancel")
assert published == [] and 424242 not in bot.pending_tweets
assert q.markup_cleared, "the buttons must be cleared so it can't be pressed again"
print("PASS 取消 discards the draft and publishes nothing")

# --- 發布 with nothing pending must not post --------------------------------
bot.pending_tweets.clear()
q, b = press("tweet:publish")
assert published == [], "publishing a forgotten draft must not post"
assert any("沒有記錄" in (a or "") for a in q.answered), q.answered
print("PASS 發布 with no pending draft refuses instead of posting")

# --- 發布 posts once and returns the link -----------------------------------
bot.pending_tweets[424242] = draft
q, b = press("tweet:publish")
assert len(published) == 1 and published[0] is draft, published
assert any("https://x.com/" in (m or "") for m in b.msgs), b.msgs
assert 424242 not in bot.pending_tweets, "the draft must be consumed"
assert q.markup_cleared, "buttons cleared before posting, so a double tap can't repost"
print(f"PASS 發布 posts once and returns the link: {[m for m in b.msgs if 'x.com' in (m or '')][0]}")

# a second press of the same (now stale) button posts nothing more
q2, b2 = press("tweet:publish")
assert len(published) == 1, f"double tap must not post twice: {len(published)}"
print("PASS a second tap on the same draft cannot post twice")

# --- a publish failure keeps the draft so it can be retried -----------------
async def boom_publish(item): raise RuntimeError("X 拒絕了")
tweet_mod.publish = boom_publish
bot.pending_tweets[424242] = draft
q, b = press("tweet:publish")
assert bot.pending_tweets.get(424242) is draft, "a failed publish must keep the draft"
assert any("發布失敗" in (m or "") for m in b.msgs), b.msgs
print("PASS a failed publish reports it and keeps the draft for a retry")
tweet_mod.publish = fake_publish

# --- non-admin is refused at every door -------------------------------------
published.clear()
called = {"gen": 0}
async def never_gen(*a, **k):
    called["gen"] += 1
    raise AssertionError("must not generate for a non-admin")
tweet_mod.generate_tweet = never_gen
replies.clear()
asyncio.run(bot.tweet_command(FakeUpdate(), types.SimpleNamespace(bot=FakeBot())))
assert called["gen"] == 0 and replies and "沒有對你開放" in replies[0] or "没有对你开放" in replies[0]
bot.pending_tweets[999] = draft
q, b = press("tweet:publish", chat_id=999)
assert published == [], "a non-admin must never publish"
print("PASS a non-admin can neither draft nor publish")
tweet_mod.generate_tweet = draft_ok

# --- both tweet times are scheduled -----------------------------------------
app4 = Application.builder().token("123:fake").build()
bot.X_DAILY_TIMES = "12:00,20:00"
bot.schedule_daily_tweets(app4)
jobs4 = sorted(app4.job_queue.jobs(), key=lambda j: j.name)
assert len(jobs4) == 2, [j.name for j in jobs4]
hours = sorted(str(f) for j in jobs4 for f in j.job.trigger.fields if f.name == "hour")
assert hours == ["12", "20"], hours
assert all(str(j.job.trigger.timezone) == "Asia/Taipei" for j in jobs4)
print(f"PASS both daily tweet jobs scheduled at {hours} Asia/Taipei")

# a malformed entry is skipped, the good one still schedules
app5 = Application.builder().token("123:fake").build()
bot.X_DAILY_TIMES = "12:00,nonsense"
bot.schedule_daily_tweets(app5)
assert len(app5.job_queue.jobs()) == 1, [j.name for j in app5.job_queue.jobs()]
print("PASS a malformed time is skipped without losing the valid one")

print("\nALL BOT WIRING TESTS PASSED")
