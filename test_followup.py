"""The one follow-up: five days on, to businesses that haven't answered, within the day's Gmail
allowance, with the same mockup. No network: run `python test_followup.py`.
"""
import asyncio, base64, datetime as dt, os, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242",
                   "OUTREACH_GAP_SECONDS": "0", "OUTREACH_BATCH_GAP_SECONDS": "0", "OUTREACH_RETRY_SECONDS": "0"})
from aiohttp import web
from aiohttp.test_utils import TestServer

import outreach as om
import bot

UK = {"email": "jill@galegreen.com", "name": "Gale Green Cottage", "region": "uk", "mockup": "gale-green",
      "cat": "bnb", "host": "Jill", "first": "I came across Gale Green Cottage on the Ingleton village website."}
HK = {"email": "info@vive.hk", "name": "Vive Cake Boutique", "region": "hk", "lang": "en"}

# --- the letters ------------------------------------------------------------------------------
subject, body = om.compose_followup(UK)
assert subject == "Re: A quick mockup for Gale Green Cottage"
assert body.startswith("Hi Jill,\n\nJust bringing this back") and body.count(om.MOCKUP_MARK) == 1
assert 'reply "no thanks"' in body and "from=followup" in body
text = om.plain(body, UK)
assert "(mockup: https://worker-production-42fb.up.railway.app/mockups/gale-green.png)" in text and "{" not in text
assert '<img src="cid:mockup"' in om.to_html(body, UK)
subject, body = om.compose_followup(HK)
assert subject.startswith("Re: Vive Cake Boutique 的客人查詢") and body.startswith("Vive Cake Boutique 你好")
assert body.count(om.MOCKUP_MARK) == 1 and "(the mockup is above)" in body and "Hi Vive Cake Boutique team," in body
assert "不用了" in body
print("PASS the follow-up answers the first email, with the same mockup once, in the same languages")

# --- who is due -------------------------------------------------------------------------------
leads = om.Leads(os.path.join(tempfile.mkdtemp(), "cs.sqlite3"))
now = dt.datetime(2026, 10, 12, 6, 0, tzinfo=dt.timezone.utc)
rows = {"old@ex.uk": (6, "sent", "shop"), "recent@ex.uk": (3, "sent", "shop"), "replied@ex.uk": (6, "replied", "shop"),
        "agency@ex.sg": (6, "sent", "agency"), "done@ex.uk": (8, "sent", "shop")}
leads.add([{**UK, "email": e, "name": e, "kind": k} for e, (_, _, k) in rows.items()])
with leads._db() as db:
    for email, (days, status, _) in rows.items():
        db.execute("UPDATE leads SET status=?, sent_at=? WHERE email=?", (status, (now - dt.timedelta(days=days)).isoformat(), email))
    db.execute("UPDATE leads SET followed_at=? WHERE email='done@ex.uk'", ((now - dt.timedelta(days=1)).isoformat(),))
assert [l["email"] for l in leads.followup_due(now)] == ["old@ex.uk"]
assert leads.sent_last_24h(now) == 1, "a follow-up yesterday counts towards the day's Gmail allowance"
sg_tue_2pm = dt.datetime(2026, 10, 13, 6, 0, tzinfo=dt.timezone.utc)
assert om.followup_window(HK, sg_tue_2pm) and not om.followup_window(HK, dt.datetime(2026, 10, 13, 2, 0, tzinfo=dt.timezone.utc))
assert not om.followup_window(HK, dt.datetime(2026, 10, 11, 6, 0, tzinfo=dt.timezone.utc)), "not on Sunday"
assert om.followup_window(UK, dt.datetime(2026, 10, 13, 13, 0, tzinfo=dt.timezone.utc))   # 14:00 London
print("PASS only unanswered businesses from 5+ days ago are followed up, once, on weekday afternoons")


# --- the bot ----------------------------------------------------------------------------------
async def main():
    sent, results = [], {}

    async def exec_(request):
        req = await request.json()
        out = {"ok": True, "email": "me@gmail.com", "version": 2} if req["action"] == "ping" else {"ok": True}
        if req["action"] == "send":
            sent.append(req)
        key = str(len(results)); results[key] = out
        raise web.HTTPFound(f"/echo?k={key}")

    async def echo(request):
        return web.json_response(results[request.query["k"]])

    app = web.Application()
    app.router.add_post("/exec", exec_)
    app.router.add_get("/echo", echo)
    async with TestServer(app) as server:
        told = []

        class Msg:
            async def reply_text(self, text, **k): told.append(text)

        class Bot:
            async def send_message(self, **k): told.append(k["text"]); return Msg()

        bot.OWNER_CHAT_ID = "424242"
        bot.leads_db = om.Leads(os.path.join(tempfile.mkdtemp(), "cs.sqlite3"))
        bot.leads_db.set_setting("secret", "s3cret")
        bot.leads_db.set_setting("url", str(server.make_url("/exec")))
        bot.leads_db.add([UK, HK, {**UK, "email": "third@ex.uk", "name": "Third"}])
        week_ago = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6)).isoformat()
        with bot.leads_db._db() as db:
            db.execute("UPDATE leads SET status='sent', sent_at=?", (week_ago,))
        ctx = types.SimpleNamespace(bot=Bot())
        real_window, real_cap = om.followup_window, om.SEND_CAP_24H
        om.followup_window = lambda lead, now=None: True
        om.SEND_CAP_24H = 2   # room for two today

        await bot.followup_job(ctx)
        for _ in range(300):
            if any(t.startswith("📧") for t in told[1:]):
                break
            await asyncio.sleep(0.02)
        assert "🔁 这 2 家" in told[0] and len(sent) == 2
        assert all(s["subject"].startswith("Re: ") and '<img src="cid:mockup"' in s["html"] for s in sent)
        assert base64.b64decode(sent[0]["images"]["mockup"])[:4] == b"\x89PNG"
        assert "跟进邮件发好了 2 封" in told[-1]
        followed = [e for e in ("jill@galegreen.com", "info@vive.hk", "third@ex.uk") if bot.leads_db.get(e)["followed_at"]]
        assert len(followed) == 2 and bot.leads_db.get(followed[0])["status"] == "sent"

        om.SEND_CAP_24H = 95
        sent.clear(); told.clear()
        await bot.followup_job(ctx)
        for _ in range(300):
            if any(t.startswith("📧") for t in told[1:]):
                break
            await asyncio.sleep(0.02)
        assert len(sent) == 1 and "🔁 这 1 家" in told[0], "the one left over goes next time"
        sent.clear(); told.clear()
        await bot.followup_job(ctx)
        await asyncio.sleep(0.05)
        assert not sent, "each business is followed up once"

        om.followup_window = lambda lead, now=None: False
        bot.leads_db.add([{**UK, "email": "fourth@ex.uk", "name": "Fourth"}])
        with bot.leads_db._db() as db:
            db.execute("UPDATE leads SET status='sent', sent_at=? WHERE email='fourth@ex.uk'", (week_ago,))
        await bot.followup_job(ctx)
        assert not sent and not told, "nothing outside their weekday afternoon"
        om.followup_window, om.SEND_CAP_24H = real_window, real_cap
        print("PASS follow-ups go out within the day's Gmail allowance, each once, with the mockup")

    from telegram.ext import Application
    application = Application.builder().token("123:fake").build()
    bot.schedule_outreach(application)
    assert "outreach-followup" in [j.name for j in application.job_queue.jobs()]
    print("PASS follow-ups are checked every half hour")

asyncio.run(main())
print("\nALL FOLLOW-UP TESTS PASSED")
