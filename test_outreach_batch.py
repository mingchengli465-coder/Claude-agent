"""The English-market batch: English-only letters with a mockup shown inline, sent outside the daily
limit in the recipients' working hours, once the owner's Gmail script can carry images.
No network: run `python test_outreach_batch.py`.
"""
import asyncio, base64, datetime as dt, os, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242",
                   "OUTREACH_GAP_SECONDS": "0", "OUTREACH_BATCH_GAP_SECONDS": "0",
                   "OUTREACH_RETRY_SECONDS": "0"})
from aiohttp import web
from aiohttp.test_utils import TestServer

import outreach as om
import bot

UK = {"email": "Jill@GaleGreen.com", "name": "Gale Green Cottage", "region": "uk", "batch": "uk1",
      "mockup": "gale-green", "cat": "bnb", "host": "Jill",
      "first": "I came across Gale Green Cottage on the Ingleton village website."}

# --- the letter ------------------------------------------------------------------------------
subject, body = om.compose(UK)
assert subject == "A quick mockup for Gale Green Cottage"
assert body.startswith("Hi Jill,\n\nI came across Gale Green Cottage on the Ingleton village website.")
assert "(mockup: https://worker-production-42fb.up.railway.app/mockups/gale-green.png)" in body
assert "你好" not in body and "English below" not in body and "{" not in body and 'reply "no thanks"' in body
assert "?from=uk" in body and "US$70 (about £55)" in body
html = om.compose_html({**UK, "name": "Bob's <B&B>"})
assert '<img src="cid:mockup"' in html and "Bob&#x27;s &lt;B&amp;B&gt;" in html and "<B&B>" not in html
assert '<a href="https://mingchengli465-coder.github.io/Claude-agent/video.html?from=uk">' in html
assert om.mockup_png(UK)[:8] == b"\x89PNG\r\n\x1a\n"
assert om.mockup_png({**UK, "mockup": "../bot"}) is None and om.mockup_png({**UK, "mockup": "nope"}) is None
_, florist = om.compose({**UK, "cat": "florist", "host": ""})
assert florist.startswith("Hello,") and "flower orders" in florist
_, hk = om.compose({"email": "a@b.hk", "name": "思思蛋糕", "region": "hk"})
assert "思思蛋糕你好" in hk, "Hong Kong keeps the 繁體 + English letter"
print("PASS English-market leads get an English letter with their mockup; HTML is escaped; HK is unchanged")

# --- working hours ---------------------------------------------------------------------------
noon_london = dt.datetime(2026, 10, 3, 11, 0, tzinfo=dt.timezone.utc)
night_london = dt.datetime(2026, 10, 3, 21, 0, tzinfo=dt.timezone.utc)
assert om.in_work_hours(UK, noon_london) and not om.in_work_hours(UK, night_london)
assert om.in_work_hours({"region": "hk"}, night_london), "only English-market leads wait for their hours"
print("PASS English-market emails wait for 8:30–18:00 on the recipient's clock")

# --- the list --------------------------------------------------------------------------------
leads = om.Leads(os.path.join(tempfile.mkdtemp(), "cs.sqlite3"))
leads.add([UK, {"email": "shop@ex.sg", "name": "Shop", "region": "sg"}])
row = leads.get("jill@galegreen.com")
assert (row["batch"], row["mockup"], row["cat"], row["host"]) == ("uk1", "gale-green", "bnb", "Jill")
assert [l["email"] for l in leads.due()] == ["shop@ex.sg"], "the daily batch leaves the English list alone"
assert [l["email"] for l in leads.batch_waiting()] == ["jill@galegreen.com"]
leads.mark("jill@galegreen.com", "sent")
assert leads.sent_today() == 0 and leads.room_today() == om.DAILY_LIMIT, "it doesn't eat the daily limit"
leads.add([{**UK, "first": "changed"}])
assert leads.get("jill@galegreen.com")["first"] == UK["first"], "a sent lead keeps its wording"
print("PASS batch leads keep their fields, stay out of the daily batch and its limit")


# --- the mailer and the batch job --------------------------------------------------------------
async def main():
    sent, results, state = [], {}, {"version": 1}

    async def exec_(request):
        req = await request.json()
        if req["action"] == "ping":
            out = {"ok": True, "email": "me@gmail.com", **({"version": 2} if state["version"] == 2 else {})}
        else:
            sent.append(req); out = {"ok": True}
        key = str(len(results)); results[key] = out
        raise web.HTTPFound(f"/echo?k={key}")

    async def echo(request):
        return web.json_response(results[request.query["k"]])

    app = web.Application()
    app.router.add_post("/exec", exec_)
    app.router.add_get("/echo", echo)
    async with TestServer(app) as server:
        url = str(server.make_url("/exec"))
        box = om.Mailer(url, "s3cret")
        assert await box.version() == 1
        await box.send("a@ex.com", "Hi", "Body")
        assert "html" not in sent[-1], "plain letters stay plain"
        await box.send("a@ex.com", "Hi", "Body", html="<p>x</p>", image=b"PNG")
        assert sent[-1]["html"] == "<p>x</p>" and base64.b64decode(sent[-1]["images"]["mockup"]) == b"PNG"
        sent.clear()
        print("PASS the mailer reports its version and carries the HTML and the image")

        told = []

        class Msg:
            async def reply_text(self, text, **k): told.append(text)

        class Bot:
            async def send_message(self, **k): told.append(k["text"]); return Msg()

        bot.OWNER_CHAT_ID = "424242"
        bot.leads_db = om.Leads(os.path.join(tempfile.mkdtemp(), "cs.sqlite3"))
        bot.leads_db.set_setting("secret", "s3cret")
        bot.leads_db.set_setting("url", url)
        second = {**UK, "email": "info@harlinghouse.co.uk", "name": "Harling House", "mockup": "harling-house", "host": ""}
        bot.leads_db.add([UK, second, {"email": "shop@ex.sg", "name": "Shop", "region": "sg"}])
        ctx = types.SimpleNamespace(bot=Bot())
        real_hours = om.in_work_hours

        # after hours in the UK: nothing happens, not even the update request
        om.in_work_hours = lambda lead, now=None: not om.is_intl(lead)
        await bot.batch_job(ctx)
        assert not told and not sent

        # in hours, old script: the owner is asked once to update it, and nothing is sent
        om.in_work_hours = lambda lead, now=None: True
        await bot.batch_job(ctx); await bot.batch_job(ctx)
        assert len(told) == 2 and "管理部署" in told[0] and "version: 2" in told[1] and "s3cret" in told[1]
        assert not sent
        told.clear()

        # the script is updated: both go out with their own mockup, then the owner hears
        state["version"] = 2
        await bot.batch_job(ctx)
        for _ in range(200):
            if any(t.startswith("📧 发好了") for t in told):
                break
            await asyncio.sleep(0.01)
        assert "开始发英国这批 2 封" in told[0] and "Harling House · info@harlinghouse.co.uk" in told[0]
        assert [s["to"] for s in sent] == ["jill@galegreen.com", "info@harlinghouse.co.uk"]
        assert all(s["html"] and '<img src="cid:mockup"' in s["html"] for s in sent)
        assert base64.b64decode(sent[0]["images"]["mockup"]) == om.mockup_png(UK)
        assert base64.b64decode(sent[1]["images"]["mockup"]) != base64.b64decode(sent[0]["images"]["mockup"])
        assert "📧 发好了 2 封" in told[-1] and not bot.leads_db.setting("batch_running")
        assert bot.leads_db.get("shop@ex.sg")["status"] == "new", "the daily list waits for its own time"
        await bot.batch_job(ctx)
        assert len(sent) == 2, "each business once"
        print("PASS the batch waits for the new script, then sends each business its own mockup, once")

        # a batch cut off at the end of the working day says so
        sent.clear(); told.clear()
        bot.leads_db.add([{**UK, "email": "late1@ex.co.uk", "name": "Late One"}, {**UK, "email": "late2@ex.co.uk", "name": "Late Two"}])
        calls = {"n": 0}
        def closing(lead, now=None):
            calls["n"] += 1
            return calls["n"] <= 3   # in hours when the batch starts and for the first email, closed by the second
        om.in_work_hours = closing
        await bot.batch_job(ctx)
        for _ in range(200):
            if any(t.startswith("📧") for t in told[1:]):
                break
            await asyncio.sleep(0.01)
        assert [s["to"] for s in sent] == ["late1@ex.co.uk"] and "下班" in told[-1]
        assert bot.leads_db.get("late2@ex.co.uk")["status"] == "new" and not bot.leads_db.setting("batch_running")
        om.in_work_hours = real_hours
        print("PASS at the end of their working day the rest wait for tomorrow")

        # Gmail hiccups (an HTML page instead of JSON): one retry; still failing, it stays on the list
        hiccups = {"once@ex.co.uk": 1, "twice@ex.co.uk": 2}
        real_exec = exec_

        async def flaky(request):
            req = await request.json()
            if req["action"] == "send" and hiccups.get(req["to"], 0) > 0:
                hiccups[req["to"]] -= 1
                return web.Response(text="<html><title>Error</title>Exception: Service error: Gmail</html>",
                                    content_type="text/html")
            request._read_bytes = None
            return await real_exec_json(req)

        async def real_exec_json(req):
            if req["action"] == "ping":
                out = {"ok": True, "email": "me@gmail.com", "version": 2}
            else:
                sent.append(req); out = {"ok": True}
            key = str(len(results)); results[key] = out
            raise web.HTTPFound(f"/echo?k={key}")

        app2 = web.Application()
        app2.router.add_post("/exec", flaky)
        app2.router.add_get("/echo", echo)
        async with TestServer(app2) as server2:
            bot.leads_db.set_setting("url", str(server2.make_url("/exec")))
            sent.clear(); told.clear()
            bot.leads_db.add([{**UK, "email": "once@ex.co.uk", "name": "Once"}, {**UK, "email": "twice@ex.co.uk", "name": "Twice"},
                              {**UK, "email": "fine@ex.co.uk", "name": "Fine"}])
            om.in_work_hours = lambda lead, now=None: True
            await bot.batch_job(ctx)
            for _ in range(300):
                if any(t.startswith("📧") for t in told[1:]):
                    break
                await asyncio.sleep(0.01)
            om.in_work_hours = real_hours
            assert [s["to"] for s in sent] == ["late2@ex.co.uk", "once@ex.co.uk", "fine@ex.co.uk"], [s["to"] for s in sent]
            assert "⏳ Twice" in told[-1] and "Service error: Gmail" not in told[-1]
            assert bot.leads_db.get("twice@ex.co.uk")["status"] == "new", "it waits for the next round"
            bot.leads_db.mark("twice@ex.co.uk", "failed", "Gmail 发信脚本没有正常回应：x")
            bot.leads_db.mark("fine@ex.co.uk", "failed", "Invalid email: fine@ex.co.uk")
            assert bot.leads_db.retry_transient() == 1
            assert bot.leads_db.get("twice@ex.co.uk")["status"] == "new" and bot.leads_db.get("fine@ex.co.uk")["status"] == "failed"
        print("PASS a Gmail hiccup gets one retry, then waits for the next round; a bad address doesn't")

    # the batch job is scheduled every five minutes
    from telegram.ext import Application
    application = Application.builder().token("123:fake").build()
    bot.schedule_outreach(application)
    assert "outreach-batch" in [j.name for j in application.job_queue.jobs()]
    print("PASS the batch is checked every five minutes")

asyncio.run(main())
print("\nALL OUTREACH BATCH TESTS PASSED")
