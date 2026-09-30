"""Cold emails: the letters, the daily limit, the Apps Script mailer (redirect and all) and the
Telegram flow from pasting the link to a reply notice. No network: run `python test_outreach.py`.
"""
import asyncio, datetime as dt, json, os, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242",
                   "OUTREACH_GAP_SECONDS": "0"})
from aiohttp import web
from aiohttp.test_utils import TestServer

import outreach as om
import bot

LEADS = [
    {"email": "Info@Whyzee.com.sg", "name": "Whyzee Bakery", "region": "sg", "lang": "en",
     "first": "I noticed your WhatsApp orders are answered 10am–6pm."},
    {"email": "bakers@bakingmaniachk.com", "name": "Baking Maniac", "region": "hk", "lang": "en"},
    {"email": "order@incake.com.hk", "name": "InCake 3D", "region": "hk", "lang": "zh"},
    {"email": "", "name": "no email"}, {"email": "x@y.z"},
]

# --- the letters -----------------------------------------------------------------------------
leads = om.parse_leads(json.dumps(LEADS))
assert [l["name"] for l in leads] == ["Whyzee Bakery", "Baking Maniac", "InCake 3D"]
assert om.parse_leads("not json") == []
subject, body = om.compose(leads[0])
assert subject == "Quick idea for Whyzee Bakery's customer enquiries"
assert body.startswith("Hi Whyzee Bakery team,\n\nI noticed your WhatsApp") and "from US$70, then US$9.9/month" in body
assert "?from=email" in body and 'reply "no thanks"' in body and "{" not in body
subject, body = om.compose(leads[1])
assert "after hours" in body and "HK$550" in body
subject, body = om.compose(leads[2])
assert subject.startswith("InCake 3D的客人查詢") and "HK$550" in body and "不用了" in body and "{" not in body
script = om.script_for("s3cret")
assert "const SECRET = 's3cret';" in script and "GmailApp.sendEmail" in script and "{secret}" not in script
print("PASS each business gets its own first line, the right price line and an opt-out line")

# --- the list and the daily limit ------------------------------------------------------------
tmp = tempfile.mkdtemp()
db = om.Leads(os.path.join(tmp, "cs.sqlite3"))
assert db.add(leads) == 3 and db.add(leads) == 0
assert db.get("INFO@whyzee.com.sg")["status"] == "new"
assert db.secret() == db.secret() and len(db.secret()) > 20
many = [{"email": f"shop{i}@ex.com", "name": f"Shop {i}"} for i in range(12)]
db.add(many)
om.DAILY_LIMIT = 10
assert len(db.due()) == 10 and db.due()[0]["email"] == "info@whyzee.com.sg"
for lead in db.due()[:4]:
    db.mark(lead["email"], "sent")
assert db.sent_today() == 4 and len(db.due()) == 6
tomorrow = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
assert db.room_today(tomorrow) == 10, "the limit starts again the next day"
db.add([{"email": "info@whyzee.com.sg", "name": "again"}])
assert db.get("info@whyzee.com.sg")["status"] == "sent", "a business already emailed is never emailed again"
assert db.first_sight("m1", "a") and not db.first_sight("m1", "a")
print("PASS at most 10 a day, each business once, the list survives re-seeding")


# --- the Apps Script mailer, against a fake that redirects like the real one ------------------
async def mailer_tests():
    sent, results = [], {}

    async def exec_(request):
        req = await request.json()
        if req.get("secret") != "s3cret":
            out = {"ok": False, "error": "bad secret"}
        elif req["action"] == "ping":
            out = {"ok": True, "email": "me@gmail.com"}
        elif req["action"] == "send":
            if req["to"] == "broken@ex.com":
                out = {"ok": False, "error": "Invalid email: broken@ex.com"}
            else:
                sent.append(req); out = {"ok": True}
        elif req["action"] == "replies":
            out = {"ok": True, "replies": [
                {"id": "r1", "lead": "info@whyzee.com.sg", "bounce": False, "subject": "Re: Quick idea",
                 "text": "Hi Vincent, sounds interesting. How does the demo work?"},
                {"id": "r2", "lead": "shop0@ex.com", "bounce": False, "subject": "Re", "text": "No thanks."},
                {"id": "r3", "lead": "shop1@ex.com", "bounce": True, "subject": "Delivery Status Notification",
                 "text": "Address not found"}]}
        key = str(len(results)); results[key] = out
        # Apps Script answers a POST with a 302 to googleusercontent, where a GET reads the result
        raise web.HTTPFound(f"/echo?k={key}")

    async def echo(request):
        return web.json_response(results[request.query["k"]])

    async def html(request):
        return web.Response(text="<html>Sign in</html>", content_type="text/html")

    app = web.Application()
    app.router.add_post("/macros/s/ABC/exec", exec_)
    app.router.add_get("/echo", echo)
    app.router.add_post("/private/exec", html)
    async with TestServer(app) as server:
        base = str(server.make_url("")).rstrip("/")
        box = om.Mailer(base + "/macros/s/ABC/exec", "s3cret")
        assert await box.ping() == "me@gmail.com"
        await box.send("a@ex.com", "Hi", "Body")
        assert sent[-1]["to"] == "a@ex.com" and sent[-1]["name"] == "Vincent"
        for bad, words in ((om.Mailer(base + "/macros/s/ABC/exec", "wrong"), "bad secret"),
                           (om.Mailer(base + "/private/exec", "s3cret"), "任何人"),
                           (om.Mailer("http://127.0.0.1:1/exec", "s3cret"), "连不上")):
            try:
                await bad.ping()
                raise AssertionError("must fail")
            except om.MailError as exc:
                assert words in str(exc), exc
        assert await box.replies([]) == []
        print("PASS the mailer follows the Apps Script redirect; a wrong secret, a private deployment "
              "and no network are readable errors")

        # --- the Telegram flow ---------------------------------------------------------------
        told, edits = [], []

        class Bot:
            async def send_message(self, **k): told.append(k)

        class Msg:
            def __init__(self, text=""): self.text, self.chat_id = text, 424242
            async def reply_text(self, text, **k): told.append({"text": text, **k})
            async def edit_text(self, text, **k): edits.append(text)

        class Update:
            def __init__(self, text, chat=424242):
                self.effective_chat = types.SimpleNamespace(id=chat)
                self.effective_message = Msg(text)
            def get_bot(self): return Bot()

        # the bot only ever talks to the fake, whatever the pasted link says
        real = om.Mailer
        class Local(real):
            def __init__(self, url, secret, session=None):
                super().__init__(base + "/macros/s/ABC/exec", secret, session)
        om.Mailer = Local
        bot.OWNER_CHAT_ID = "424242"
        bot.leads_db = om.Leads(os.path.join(tempfile.mkdtemp(), "cs.sqlite3"))
        bot.leads_db.set_setting("secret", "s3cret")
        bot.leads_db.add(leads + [{"email": "broken@ex.com", "name": "Broken"}] + many[:2])
        ctx = types.SimpleNamespace(bot=Bot())

        # not connected yet: /mail shows the steps and the script
        await bot.mail_command(Update("/mail"), ctx)
        assert "script.google.com" in told[0]["text"] and "const SECRET = 's3cret'" in told[1]["text"]
        await bot.mail_command(Update("/mail", chat=999), ctx)
        assert "没有对你开放" in told[-1]["text"]
        await bot.offer_emails(Bot())
        assert len(told) == 3, "nothing is offered before Gmail is connected"
        told.clear()

        # pasting the link connects and offers today's batch
        link = "https://script.google.com/macros/s/AKfycbx-123_abc/exec"
        assert await bot.connect_mailer(Update(f"好了 {link}"))
        assert bot.leads_db.setting("url") == link and "Gmail 接好了（me@gmail.com）" in told[0]["text"]
        offer = told[1]
        assert "今天要发这 6 封" in offer["text"] and "Whyzee Bakery · info@whyzee.com.sg" in offer["text"]
        assert "Hi Whyzee Bakery team" in offer["text"]
        go, no = [b.callback_data for b in offer["reply_markup"].inline_keyboard[0]]
        assert not await bot.connect_mailer(Update("hello"))
        print("PASS /mail gives the steps and a script with this bot's secret; pasting the link connects "
              "and shows today's emails")

        # a stranger can't press it; ✅ sends them all, stopping at an address Gmail refuses
        class Query:
            def __init__(self, data, chat=424242):
                self.data, self.message = data, Msg(); self.message.chat_id = chat; self.alerts = []
            async def answer(self, text=None, show_alert=False): self.alerts.append(text)
            async def edit_message_text(self, text, **k): edits.append(text)

        q = Query(go, chat=999)
        await bot.mail_button(types.SimpleNamespace(callback_query=q), ctx)
        assert q.alerts == ["这批已经处理过了"] and not sent[1:]
        told.clear()
        await bot.mail_button(types.SimpleNamespace(callback_query=Query(go)), ctx)
        for _ in range(100):
            if told:
                break
            await asyncio.sleep(0.02)
        to = [s["to"] for s in sent[1:]]
        assert to == ["info@whyzee.com.sg", "bakers@bakingmaniachk.com", "order@incake.com.hk"], to
        assert sent[3]["subject"].startswith("InCake 3D的客人查詢")
        assert "发好了 3 封" in told[0]["text"] and "Broken" in told[0]["text"] and "后面的先停了" in told[0]["text"]
        assert bot.leads_db.get("broken@ex.com")["status"] == "new"
        q = Query(go)
        await bot.mail_button(types.SimpleNamespace(callback_query=q), ctx)
        assert q.alerts == ["这批已经处理过了"] and len(sent) == 4, "the same batch can't be sent twice"
        print("PASS ✅ sends the batch one by one from Gmail, only the owner can press it, and only once")

        # ❌ sends nothing
        told.clear()
        await bot.offer_emails(Bot(), manual=True)
        _, no = [b.callback_data for b in told[0]["reply_markup"].inline_keyboard[0]]
        await bot.mail_button(types.SimpleNamespace(callback_query=Query(no)), ctx)
        assert "今天不发" in edits[-1] and len(sent) == 4
        print("PASS ❌ sends nothing")

        # replies: an answer, a no-thanks and a bounce, each told once
        for email in ("shop0@ex.com", "shop1@ex.com"):
            bot.leads_db.mark(email, "sent")
        told.clear()
        await bot.email_replies_job(ctx)
        await bot.email_replies_job(ctx)
        texts = [t["text"] for t in told]
        assert len(texts) == 3, texts
        assert "Whyzee Bakery 回你邮件了" in texts[0] and "demo work" in texts[0]
        assert "不需要" in texts[1] and "退回来了" in texts[2]
        assert [bot.leads_db.get(e)["status"] for e in ("info@whyzee.com.sg", "shop0@ex.com", "shop1@ex.com")] \
            == ["replied", "optout", "bounced"]
        print("PASS replies, no-thanks and bounces reach the owner once each")

        # /outreach shows the numbers
        told.clear()
        await bot.outreach_command(Update("/outreach"), ctx)
        assert "已发 5" in told[0]["text"] and "有回复 1" in told[0]["text"], told[0]["text"]
        om.Mailer = real

asyncio.run(mailer_tests())

# --- the daily question is scheduled, with reply checks -----------------------------------------
from telegram.ext import Application
app = Application.builder().token("123:fake").build()
bot.schedule_outreach(app)
names = sorted(j.name for j in app.job_queue.jobs())
assert names == ["email-replies", "outreach-daily"], names
daily = [j for j in app.job_queue.jobs() if j.name == "outreach-daily"][0]
fields = {f.name: str(f) for f in daily.job.trigger.fields}
assert fields["hour"] == "10" and str(daily.job.trigger.timezone) == "Asia/Singapore"
print("PASS every day at 10:00 Singapore time the owner is asked; replies are checked every 30 minutes")

print("\nALL OUTREACH TESTS PASSED")
