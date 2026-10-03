"""Businesses found on the map, a mockup drawn for each, and the everyday English-market emails.
No network: run `python test_leads_map.py`.
"""
import asyncio, base64, datetime as dt, io, os, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242",
                   "OUTREACH_GAP_SECONDS": "0", "OUTREACH_BATCH_GAP_SECONDS": "0"})
from aiohttp import web
from aiohttp.test_utils import TestServer
from PIL import Image

import leadfinder
import mockup
import outreach as om
import bot

# --- reading the map -------------------------------------------------------------------------
q = leadfinder.query(-31.95, 115.86, 25000)
assert '["shop"~"^(florist|pastry|bakery|beauty|pet_grooming)$"]["email"](-32.1746,115.5953,-31.7254,116.1247)' in q
assert '["tourism"~"^(guest_house)$"]["contact:email"]' in q and q.count("nwr[") == 4
assert leadfinder.town_for("au", 10) != leadfinder.town_for("au", 11), "a different town each day"
assert leadfinder.town_for("au", 10, 0) != leadfinder.town_for("au", 10, 1)
ELEMENTS = [
    {"tags": {"name": "Rosie's Cakes", "shop": "pastry", "email": "Hello@RosiesCakes.com.au", "addr:city": "Perth", "website": "https://rosiescakes.com.au"}},
    {"tags": {"name": "Not Ours", "shop": "hardware", "email": "x@hardware.au"}},
    {"tags": {"name": "Big Chain Bakery", "shop": "bakery", "email": "info@chain.com", "brand": "Chain"}},
    {"tags": {"email": "noname@ex.com"}},
    {"tags": {"name": "Bad Email", "shop": "bakery", "email": "not an email"}},
    {"tags": {"name": "Via Booking", "shop": "bakery", "email": "x@booking.com"}},
    {"tags": {"name": "Closed Shop", "shop": "bakery", "contact:email": "a@closed.au", "disused:shop": "bakery"}},
    {"tags": {"name": "Two Emails", "tourism": "guest_house", "contact:email": "first@two.au; second@two.au", "addr:town": "Margaret River"}},
    {"tags": {"name": "Rosie's Again", "shop": "bakery", "email": "hello@rosiescakes.com.au"}},
]
found = leadfinder.pick(ELEMENTS, "au")
assert [l["email"] for l in found] == ["hello@rosiescakes.com.au", "first@two.au"]
rosie = found[0]
assert rosie["city"] == "Perth" and rosie["site"] == "https://rosiescakes.com.au" and rosie["cat"] == "bakery"
assert rosie["first"] == "I came across Rosie's Cakes on the map while looking at cake shops and bakeries in Perth."
assert found[1]["city"] == "Margaret River" and found[1]["cat"] == "bnb"
print("PASS the map gives independent, open businesses with a real email; chains and platforms are left out")


async def overpass():
    calls = []

    async def broken(request):
        calls.append("broken"); return web.Response(status=504)

    async def good(request):
        calls.append((await request.post())["data"]); return web.json_response({"elements": ELEMENTS})

    app = web.Application()
    app.router.add_post("/a", broken)
    app.router.add_post("/b", good)
    async with TestServer(app) as server:
        leadfinder.OVERPASS = [str(server.make_url("/a")), str(server.make_url("/b"))]
        leads = await leadfinder.find("au", 3)
    _, lat, lon, radius = leadfinder.town_for("au", 3)
    assert calls[0] == "broken" and calls[1] == leadfinder.query(lat, lon, radius)
    assert sorted(l["email"] for l in leads) == ["first@two.au", "hello@rosiescakes.com.au"]
    leadfinder.OVERPASS = ["http://127.0.0.1:1/x"]
    assert await leadfinder.find("au", 3) == [], "no map, no leads, no crash"

asyncio.run(overpass())
print("PASS when one map server is down the next one answers")

# --- the mockup ------------------------------------------------------------------------------
assert mockup.kind_of({"name": "Happy Oven"}) == "" and mockup.kind_of({"name": "Butter Cake Studio"}) == "bakery"
assert mockup.kind_of({"name": "Glow Spa & Salon"}) == "beauty" and mockup.kind_of({"name": "x", "cat": "florist"}) == "florist"
assert mockup.kind_of({"name": "思思蛋糕"}) == "bakery"
q_, a_ = mockup.chat_for({"name": "Petals", "email": "p@x.ie", "region": "ie", "cat": "florist", "city": "Galway"})
assert "€50" in q_ + a_ or "June" in q_, "a florist asks with the local currency"
for lead in (rosie, {"name": "InCake 3D 立體蛋糕專門店", "email": "order@incake.com.hk", "region": "hk"},
             {"name": "The Old Rectory Bed and Breakfast With A Very Long Name Indeed", "email": "a@b.nz", "region": "nz", "cat": "bnb"},
             {"name": "Paws", "email": "p@q.au", "region": "au", "cat": "groomer"},
             {"name": "Glow", "email": "g@h.sg", "region": "sg", "cat": "beauty"},
             {"name": "Maths Hub", "email": "m@h.uk", "region": "uk", "cat": "tutor"}):
    png = mockup.render(lead)
    assert Image.open(io.BytesIO(png)).size == (1200, 800) and len(png) > 50_000, lead["name"]
print("PASS a mockup is drawn for every kind of business, Chinese names and long names included")

# --- letters and the list --------------------------------------------------------------------
cache = os.path.join(tempfile.mkdtemp(), "mockups")
from pathlib import Path
slug = om.mockup_slug(rosie)
assert slug.startswith("m-") and om.mockup_slug({"name": "Prima9", "email": "a@b", "kind": "agency"}) == ""
assert om.mockup_slug({"name": "Gale Green", "email": "j@g", "mockup": "gale-green"}) == "gale-green"
png = om.mockup_png(rosie, Path(cache))
assert png and (Path(cache) / f"{slug}.png").read_bytes() == png, "drawn once and kept"
assert om.mockup_png(rosie, Path(cache)) == png
subject, body = om.compose({**rosie, "region": "au"})
assert "US$70 (about A$110)" in body and f"/mockups/{slug}.png" in body and "Cake orders often start" in body
assert "cakes, flavours, prices" in body and "你好" not in body
html = om.compose_html({"email": "info@vive.hk", "name": "Vive Cake Boutique", "region": "hk"})
assert html.index("我為 Vive Cake Boutique 做了一張示意圖") < html.index('<img src="cid:mockup"') < html.index("你好")
print("PASS each country gets its own prices; Hong Kong letters show the mockup above the 繁體 + English text")

leads = om.Leads(os.path.join(tempfile.mkdtemp(), "cs.sqlite3"))
leads.add([{**rosie, "email": f"au{i}@ex.au", "name": f"AU {i}"} for i in range(6)]
          + [{"email": "hk@ex.hk", "name": "HK Shop", "region": "hk"}])
monday_perth_morning = dt.datetime(2026, 10, 5, 2, 0, tzinfo=dt.timezone.utc)   # 10:00 in Perth? Sydney 13:00
assert [l["email"] for l in leads.due()] == ["hk@ex.hk"], "the HK/SG batch leaves Australia alone"
assert len(leads.intl_due("au", monday_perth_morning)) == om.INTL_MIX["au"] == 4
for lead in leads.intl_due("au", monday_perth_morning)[:3]:
    leads.mark(lead["email"], "sent")
assert len(leads.intl_due("au")) == 1 and leads.room_today() == om.DAILY_LIMIT, "Australia has its own count"
assert leads.waiting("au") == 3 and "au0@ex.au" in leads.known()[0] and "au 0" in leads.known()[1]
assert leads.get("au5@ex.au")["city"] == "Perth" and leads.get("au5@ex.au")["site"]
assert om.daily_window("au", dt.datetime(2026, 10, 5, 0, 0, tzinfo=dt.timezone.utc))      # Mon 11:00 Sydney
assert not om.daily_window("au", dt.datetime(2026, 10, 4, 0, 0, tzinfo=dt.timezone.utc))  # Sunday
assert not om.daily_window("uk", dt.datetime(2026, 10, 5, 20, 0, tzinfo=dt.timezone.utc))  # Mon 21:00 London
print("PASS each English-speaking country has its own daily share, on weekday mornings by its own clock")


# --- the bot: finding, then sending ------------------------------------------------------------
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
        bot.leads_db.add([{"email": "old@ex.au", "name": "Already Known", "region": "hk"}])
        ctx = types.SimpleNamespace(bot=Bot())

        asked = []
        async def fake_find(region, day, attempt=0):
            asked.append((region, attempt))
            kind = "bnb"
            return [{**rosie, "region": region, "email": f"{region}-{attempt}-{i}@ex.com", "name": f"{region} {attempt} {i}",
                     "cat": kind} for i in range(10)] + [{**rosie, "email": "old@ex.au", "name": "Already Known"}]
        real_find = leadfinder.find
        leadfinder.find = fake_find
        await bot.find_leads_job(ctx)
        leadfinder.find = real_find
        assert bot.leads_db.waiting("au") == om.INTL_MIX["au"] * 3, "a few days waiting in each country"
        assert ("uk", 1) in asked and ("ie", 1) not in asked, "a second town only when the first wasn't enough"
        assert bot.leads_db.waiting("uk") == om.INTL_MIX["uk"] * 3
        assert "在地图上新找到" in told[0] and "澳洲" in told[0]
        told.clear()

        # Monday 11:00 in Sydney: Australia's share goes out, each with its own mockup
        real_window, real_hours = om.daily_window, om.in_work_hours
        om.daily_window = lambda region, now=None: region == "au"
        om.in_work_hours = lambda lead, now=None: True
        await bot.intl_daily_job(ctx)
        for _ in range(300):
            if any(t.startswith("📧 发好了") for t in told):
                break
            await asyncio.sleep(0.02)
        assert "发今天的 4 封" in told[0] and "澳洲 ·" in told[0]
        assert len(sent) == 4 and all(s["to"].startswith("au-") for s in sent), ([s["to"] for s in sent], told)
        pngs = [base64.b64decode(s["images"]["mockup"]) for s in sent]
        assert all(p[:4] == b"\x89PNG" for p in pngs) and len(set(pngs)) == 4, "a different mockup for each"
        assert all("A$110" in s["body"] and '<img src="cid:mockup"' in s["html"] for s in sent)
        await bot.intl_daily_job(ctx)
        await asyncio.sleep(0.05)
        assert len(sent) == 4, "only the day's share"
        om.daily_window = lambda region, now=None: False
        sent.clear()
        await bot.intl_daily_job(ctx)
        assert not sent, "nothing outside their working hours"
        om.daily_window, om.in_work_hours = real_window, real_hours
        print("PASS the map keeps a week of businesses waiting; each country's share goes out in its morning, each with its own mockup")

    from telegram.ext import Application
    application = Application.builder().token("123:fake").build()
    bot.schedule_outreach(application)
    names = [j.name for j in application.job_queue.jobs()]
    assert {"outreach-intl", "find-leads", "find-leads-now"} <= set(names), names
    print("PASS the map is read every day, and the countries' mornings are checked every 15 minutes")

asyncio.run(main())


# --- the drawn mockups are served for the plain-text link ----------------------------------------
async def served():
    import web as web_mod
    import customer_service as cs
    folder = Path(cs.CS_DB_PATH).with_name("mockups")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "m-test123.png").write_bytes(b"\x89PNGdrawn")
    chat = web_mod.WebChat(types.SimpleNamespace(register_channel=lambda *a: None))
    app = web.Application()
    app.router.add_get("/mockups/{name}", chat.mockup)
    async with TestServer(app) as server:
        from aiohttp import ClientSession
        async with ClientSession() as s:
            r = await s.get(server.make_url("/mockups/gale-green.png"))
            assert r.status == 200 and r.headers["Content-Type"] == "image/png"
            r = await s.get(server.make_url("/mockups/m-test123.png"))
            assert r.status == 200 and await r.read() == b"\x89PNGdrawn"
            for bad in ("nope.png", "..%2Fbot.py", "x.jpg"):
                assert (await s.get(server.make_url(f"/mockups/{bad}"))).status == 404, bad
    (folder / "m-test123.png").unlink()

asyncio.run(served())
print("PASS committed and drawn mockups are served at /mockups/")
print("\nALL MAP LEAD TESTS PASSED")
