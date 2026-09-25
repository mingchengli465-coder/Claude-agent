"""Verify the Telegram wiring: handlers, admin gate, and the daily job.

No network, no token needed: run `python test_bot_wiring.py`.
"""
import asyncio, datetime as dt, os, pathlib, sys, tempfile, types

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
# X (Twitter): generate and publish automatically, no approval step
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

# Pause state lives in a temp file for the test.
tweet_mod.X_TWEET_STATE_FILE = pathlib.Path(tempfile.mkdtemp()) / "tw.json"

class NoteBot:
    def __init__(self): self.msgs = []
    async def send_message(self, **k):
        self.msgs.append((k.get("text"), k.get("reply_markup")))
    async def send_chat_action(self, **k): pass

class AdminUpd:
    def __init__(self):
        self.effective_chat = types.SimpleNamespace(id=424242)
        self.replies = []
        outer = self
        class M:
            async def reply_text(self, text, **k): outer.replies.append(text)
        self.effective_message = M()

def run_cmd(fn, upd=None):
    upd = upd or AdminUpd()
    b = NoteBot()
    asyncio.run(fn(upd, types.SimpleNamespace(bot=b)))
    return upd, b

# --- the approval path must be gone -----------------------------------------
for gone in ("pending_tweets", "tweet_keyboard", "tweet_button", "send_tweet_preview"):
    assert not hasattr(bot, gone), f"{gone} should have been deleted"
print("PASS the approval flow is gone from bot.py")

# --- /tweet generates AND publishes, no buttons anywhere --------------------
published.clear()
tweet_mod.set_paused(False)
upd, b = run_cmd(bot.tweet_command)
assert len(published) == 1 and published[0] is draft, published
texts = [m for m, _ in b.msgs]
assert any("已發推" in (x or "") for x in texts), texts
assert any("https://x.com/" in (x or "") for x in texts), texts
assert all(mk is None for _, mk in b.msgs), "notifications must carry no buttons"
print("PASS /tweet publishes straight away and notifies with the link, no buttons")

# --- the scheduled job publishes too ----------------------------------------
published.clear()
b = NoteBot()
asyncio.run(bot.tweet_daily_job(types.SimpleNamespace(bot=b)))
assert len(published) == 1, published
assert all(mk is None for _, mk in b.msgs)
print("PASS the scheduled job publishes without asking")

# --- the Telegram link reply is reported, and its failure doesn't hide the post --
async def reply_ok(url): return "555"
tweet_mod.post_link_reply = reply_ok
upd, b = run_cmd(bot.tweet_command)
sent = [m for m, _ in b.msgs if m and "已發推" in m]
assert sent and "已在底下回覆 Telegram 連結" in sent[0], sent

async def reply_boom(url): raise RuntimeError("回覆被拒")
tweet_mod.post_link_reply = reply_boom
upd, b = run_cmd(bot.tweet_command)
sent = [m for m, _ in b.msgs if m and "已發推" in m]
assert sent and "回覆被拒" in sent[0] and "推文已發出" in sent[0], sent

async def reply_off(url): return None
tweet_mod.post_link_reply = reply_off
upd, b = run_cmd(bot.tweet_command)
sent = [m for m, _ in b.msgs if m and "已發推" in m]
assert sent and "Telegram" not in sent[0], "no link configured, nothing to say"
print("PASS the link reply is reported, and a failed reply still reports the tweet as posted")

# --- a publish failure is reported, with the text so it isn't lost ----------
async def boom_publish(item): raise RuntimeError("X 拒絕了這則")
tweet_mod.publish = boom_publish
published.clear()
upd, b = run_cmd(bot.tweet_command)
texts = [m for m, _ in b.msgs]
assert any("發布失敗" in (x or "") for x in texts), texts
assert any("X 拒絕了這則" in (x or "") for x in texts), "the error must be relayed"
assert any(draft.text in (x or "") for x in texts), "the text must survive a failed post"
print("PASS a publish failure relays the error and keeps the text recoverable")
tweet_mod.publish = fake_publish

# --- a generation failure is reported ---------------------------------------
async def boom_gen(*a, **k): raise tweet_mod.GenerationError("模型掛了")
tweet_mod.generate_tweet = boom_gen
published.clear()
upd, b = run_cmd(bot.tweet_command)
assert published == [], "a failed generation must not publish"
assert any("模型掛了" in (m or "") for m, _ in b.msgs), b.msgs
print("PASS a generation failure is reported and publishes nothing")
tweet_mod.generate_tweet = draft_ok

# --- /pause and /resume ------------------------------------------------------
tweet_mod.set_paused(False)
upd, b = run_cmd(bot.pause_command)
assert tweet_mod.is_paused() is True
assert any("暫停" in r for r in upd.replies), upd.replies
upd, b = run_cmd(bot.pause_command)
assert any("本來就是暫停" in r for r in upd.replies), upd.replies
print("PASS /pause pauses, and says so when already paused")

# paused: the schedule skips and publishes nothing
published.clear()
b = NoteBot()
asyncio.run(bot.tweet_daily_job(types.SimpleNamespace(bot=b)))
assert published == [], "the schedule must not publish while paused"
assert any("已暫停" in (m or "") for m, _ in b.msgs), b.msgs
print("PASS while paused the scheduled job skips and publishes nothing")

# paused: /tweet is a deliberate manual action, so it still posts
published.clear()
upd, b = run_cmd(bot.tweet_command)
assert len(published) == 1, "manual /tweet should still work while paused"
assert any("暫停" in r for r in upd.replies), upd.replies
print("PASS /tweet still posts while paused, and says the schedule is paused")

upd, b = run_cmd(bot.resume_command)
assert tweet_mod.is_paused() is False
assert any("恢復" in r for r in upd.replies), upd.replies
upd, b = run_cmd(bot.resume_command)
assert any("本來就在跑" in r for r in upd.replies), upd.replies
print("PASS /resume resumes, and says so when already running")

# --- pause survives a restart, and a wiped file defaults to running ---------
tweet_mod.set_paused(True)
assert tweet_mod.is_paused() is True, "pause must be readable back from the file"
tweet_mod.X_TWEET_STATE_FILE.unlink()
assert tweet_mod.is_paused() is False, "a wiped file must default to running"
print("PASS pause persists in the file; a wiped file (Railway redeploy) resumes")

# --- non-admin is refused at every door -------------------------------------
class NonAdmin(AdminUpd):
    def __init__(self):
        super().__init__()
        self.effective_chat = types.SimpleNamespace(id=999)

called = {"gen": 0}
async def never_gen(*a, **k):
    called["gen"] += 1
    raise AssertionError("must not generate for a non-admin")
tweet_mod.generate_tweet = never_gen
published.clear()
tweet_mod.set_paused(False)
for fn in (bot.tweet_command, bot.pause_command, bot.resume_command):
    upd, b = run_cmd(fn, NonAdmin())
    assert upd.replies and ("沒有對你開放" in upd.replies[0] or "没有对你开放" in upd.replies[0]), upd.replies
assert called["gen"] == 0 and published == []
assert tweet_mod.is_paused() is False, "a non-admin must not be able to pause"
print("PASS a non-admin can't post, pause or resume")
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

app5 = Application.builder().token("123:fake").build()
bot.X_DAILY_TIMES = "12:00,nonsense"
bot.schedule_daily_tweets(app5)
assert len(app5.job_queue.jobs()) == 1, [j.name for j in app5.job_queue.jobs()]
print("PASS a malformed time is skipped without losing the valid one")

# --- startup env check ------------------------------------------------------
# The Railway outage this guards against: the placeholder from .env.example was
# pasted into the dashboard, so the token looked set and telegram raised
# InvalidToken far from the cause.
real = "123456789:AAH_fake_looking_but_well_formed_token"
assert bot.check_env("TELEGRAM_BOT_TOKEN", real) == ""
for bad in ("", "   ", "your-telegram-bot-token", "YOUR_TELEGRAM_BOT_TOKEN",
            "<token>", "changeme", "sk-not-a-telegram-token"):
    problem = bot.check_env("TELEGRAM_BOT_TOKEN", bad)
    assert problem, f"{bad!r} should have been rejected"
    assert "TELEGRAM_BOT_TOKEN" in problem, problem
# a real-looking token must never be echoed in full
assert real not in bot.check_env("TELEGRAM_BOT_TOKEN", real + " oops")
assert bot.check_env("OPENROUTER_API_KEY", "sk-or-v1-abc") == ""
assert "OPENROUTER_API_KEY" in bot.check_env("OPENROUTER_API_KEY", "your-openrouter-api-key")
assert "OPENROUTER_API_KEY" in bot.check_env("OPENROUTER_API_KEY", "")
print("PASS startup check rejects empty and placeholder values, naming the variable")

# .env.example must not hand anyone a placeholder to paste into Railway
example = pathlib.Path(__file__).with_name(".env.example").read_text()
for line in example.splitlines():
    if line.startswith(("TELEGRAM_BOT_TOKEN=", "OPENROUTER_API_KEY=", "ANTHROPIC_API_KEY=",
                        "X_API_KEY=", "X_API_SECRET=", "X_ACCESS_TOKEN=",
                        "X_ACCESS_TOKEN_SECRET=")):
        assert line.split("=", 1)[1] == "", f".env.example still ships a value: {line}"
print("PASS .env.example ships every secret blank")


# ===========================================================================
# Customer-service mode: routing only (the logic is tested in test_customer_service.py)
# ===========================================================================
import customer_service as cs_mod

assert bot.OWNER_CHAT_ID == "424242", "OWNER_CHAT_ID falls back to ADMIN_CHAT_ID"
assert bot.is_owner(424242) and not bot.is_owner(555)

class CSModel:
    def __init__(self): self.calls = 0
    async def __call__(self, system, messages):
        self.calls += 1
        return cs_mod.Decision(reply="你好呀～想做什么呢？", summary="未说明｜未说明｜未说明")

owner_inbox = []
async def owner_notify(text):
    owner_inbox.append(text)
    return 7000 + len(owner_inbox)

cs_model = CSModel()
bot.service = cs_mod.CustomerService(cs_mod.Store(":memory:"), "服务: 测试", cs_model, owner_notify=owner_notify)
delivered = []
async def tg_send(chat_id, text): delivered.append((chat_id, text))
bot.service.register_channel("telegram", tg_send)

old_chat_calls = []
real_chat = bot.chat
async def fake_chat(update, context): old_chat_calls.append(update.effective_chat.id)
bot.chat = fake_chat

class Msg:
    def __init__(self, text=None, reply_to=None, message_id=1, photo=None, caption=None, chat_id=0):
        self.text, self.reply_to_message, self.message_id = text, reply_to, message_id
        self.photo, self.caption, self.chat_id = photo, caption, chat_id
        self.document = self.voice = self.video = self.audio = self.sticker = None
        self.video_note = self.animation = None
        self.replies = []
    async def reply_text(self, text, **k): self.replies.append(text)

class Upd:
    def __init__(self, chat_id, text=None, chat_type="private", reply_to=None, photo=None, caption=None):
        self.effective_chat = types.SimpleNamespace(id=chat_id, type=chat_type)
        self.effective_user = types.SimpleNamespace(username="xiaohong", full_name="小红")
        self.effective_message = Msg(text, reply_to, photo=photo, caption=caption, chat_id=chat_id)

class CSBot:
    def __init__(self): self.sent, self.copied = [], []
    async def send_chat_action(self, **k): pass
    async def send_message(self, **k): self.sent.append(k)
    async def copy_message(self, **k):
        self.copied.append(k)
        return types.SimpleNamespace(message_id=9100 + len(self.copied))

def run_route(fn, upd, args=None):
    b = CSBot()
    asyncio.run(fn(upd, types.SimpleNamespace(bot=b, args=args or [])))
    return b

# the owner's own messages keep going to the old chat, untouched
run_route(bot.route_text, Upd(424242, "帮我想个标题"))
assert old_chat_calls == [424242] and cs_model.calls == 0 and owner_inbox == []
print("PASS the owner's messages still go to the normal chat, not customer service")

# anyone else, in private, gets customer service
u = Upd(555, "你好，想做PPT")
run_route(bot.route_text, u)
assert u.effective_message.replies == ["你好呀～想做什么呢？"], u.effective_message.replies
assert cs_model.calls == 1 and old_chat_calls == [424242]
print("PASS a stranger's private message is answered by customer service")

# groups are left alone
run_route(bot.route_text, Upd(-100123, "hi all", chat_type="group"))
assert cs_model.calls == 1 and old_chat_calls[-1] == -100123
print("PASS group chats don't enter customer-service mode")

# a handoff reaches the owner; replying to it reaches the customer
u = Upd(555, "我要下单，怎么付款")
run_route(bot.route_text, u)
assert u.effective_message.replies == [cs_mod.HANDOFF_TEXT]
assert "要下单/付款" in owner_inbox[-1] and "@xiaohong" in owner_inbox[-1]
notice_id = 7000 + len(owner_inbox)
reply = Upd(424242, "定金 50，发你收款码～", reply_to=types.SimpleNamespace(message_id=notice_id))
run_route(bot.route_text, reply)
assert delivered == [("555", "定金 50，发你收款码～")], delivered
assert any("已转给客户 555" in r for r in reply.effective_message.replies)
assert old_chat_calls == [424242, -100123], "a forwarded reply must not also go to the chat AI"
print("PASS the owner replies to a notice and the text reaches that customer")

# replying to something unrelated is just a normal chat message
run_route(bot.route_text, Upd(424242, "随便说说", reply_to=types.SimpleNamespace(message_id=1)))
assert old_chat_calls[-1] == 424242
print("PASS a reply to an ordinary message is not mistaken for a customer reply")

# a customer's photo is copied to the owner under the notice
u = Upd(555, photo=[object()], caption="老师的要求")
b = run_route(bot.route_media, u)
assert u.effective_message.replies == [cs_mod.ATTACHMENT_TEXT]
assert b.copied and b.copied[0]["chat_id"] == 424242 and b.copied[0]["from_chat_id"] == 555
assert bot.service.lookup_owner_message(9101) == ("telegram", "555"), "the copied file is replyable too"
print("PASS a customer's photo is forwarded to the owner and acknowledged")

# owner commands
u = Upd(555, "/customers"); run_route(bot.customers_command, u)
assert u.effective_message.replies == [bot.XHS_DENIED_TEXT]
u = Upd(424242, "/customers"); run_route(bot.customers_command, u)
assert "小红" in u.effective_message.replies[0] and "Chat ID：555" in u.effective_message.replies[0]
u = Upd(424242, "/ai"); run_route(bot.ai_command, u, ["555", "off"])
assert "已暂停" in u.effective_message.replies[0], u.effective_message.replies
calls = cs_model.calls
u = Upd(555, "在吗"); run_route(bot.route_text, u)
assert u.effective_message.replies == [] and cs_model.calls == calls, "AI paused"
u = Upd(424242, "/ai"); run_route(bot.ai_command, u, ["555", "on"])
assert "已恢复" in u.effective_message.replies[0]
u = Upd(424242, "/ai"); run_route(bot.ai_command, u, ["555"])
assert "用法" in u.effective_message.replies[0]
u = Upd(555, "/ai"); run_route(bot.ai_command, u, ["555", "on"])
assert u.effective_message.replies == [bot.XHS_DENIED_TEXT]
print("PASS /customers and /ai are owner-only and work")

# /start: customers get the customer greeting
u = Upd(555, "/start"); run_route(bot.start, u)
assert u.effective_message.replies[0].startswith(bot.CS_WELCOME_TEXT)
u = Upd(424242, "/start"); run_route(bot.start, u)
assert u.effective_message.replies == [bot.WELCOME_TEXT]
u = Upd(557, "/start"); u.effective_user.language_code = "en"; run_route(bot.start, u)
greeting = u.effective_message.replies[0]
assert bot.CS_WELCOME_TEXT in greeting and bot.CS_WELCOME_TEXT_EN in greeting, "English Telegram: both languages"
u = Upd(558, "/start"); u.effective_user.language_code = "zh-hans"; run_route(bot.start, u)
assert u.effective_message.replies == [bot.CS_WELCOME_TEXT], "Chinese Telegram: Chinese only"
assert bot._inbound(u, "x").lang == "zh" and bot._inbound(Upd(557, "x"), "x").lang == ""
print("PASS /start greets customers as the assistant and the owner as before")

# with the mode off (no owner), strangers get the old chat
saved = bot.service
bot.service = None
run_route(bot.route_text, Upd(556, "hello"))
assert old_chat_calls[-1] == 556
bot.service = saved
bot.chat = real_chat
print("PASS with customer service off, everyone gets the old chat")

# the scheduled jobs are still registered exactly as before
app6 = Application.builder().token("123:fake").build()
bot.X_DAILY_TIMES = "12:00,20:00"
bot.schedule_daily_note(app6); bot.schedule_daily_tweets(app6)
assert sorted(j.name for j in app6.job_queue.jobs()) == ["tweet-daily-1200", "tweet-daily-2000", "xhs-daily"]
print("PASS the 小红书 and tweet schedules are unchanged")

# --- which AI customer service uses --------------------------------------------------------------
fake_bot = types.SimpleNamespace()
saved_keys = (bot.ANTHROPIC_API_KEY, bot.DEEPSEEK_API_KEY)
cs_mod.CS_DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "cs.sqlite3"
cs_mod.Store.__init__.__defaults__ = (cs_mod.CS_DB_PATH,)
bot.ANTHROPIC_API_KEY, bot.DEEPSEEK_API_KEY = "", ""
assert bot.build_customer_service(fake_bot).responder is None
bot.DEEPSEEK_API_KEY = "sk-deepseek-test"
assert type(bot.build_customer_service(fake_bot).responder).__name__ == "OpenAICompatibleResponder"
bot.ANTHROPIC_API_KEY = "sk-ant-test"
assert type(bot.build_customer_service(fake_bot).responder).__name__ == "ClaudeResponder", "Claude wins when both are set"
bot.ANTHROPIC_API_KEY, bot.DEEPSEEK_API_KEY = saved_keys
print("PASS customer service picks Claude, else DeepSeek, else hands everything to the owner")

print("\nALL BOT WIRING TESTS PASSED")
