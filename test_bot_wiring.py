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

print("\nALL BOT WIRING TESTS PASSED")
