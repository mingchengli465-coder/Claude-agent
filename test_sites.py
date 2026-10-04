"""Reading businesses' websites: "paste your website" trials, personal demos that have read the shop's
own site, and web designers found through "Website by" credits, offered a partnership.
No network (a local test server stands in for the web): run `python test_sites.py`.
"""
import asyncio, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242",
                   "CS_DB_PATH": str(TMP / "bot.sqlite3"), "LEADS_PAUSE": "0", "LEADS_BUSY_WAIT": "0"})
from aiohttp import ClientSession, web as aioweb
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import leadfinder
import outreach as om
import sitetext
import trials
import web

V = "0f8e2c3a-1111-2222-3333-444455556666"

ROSE_HOME = """<html><head><title>Rose Cottage B&amp;B | Keswick | Home</title>
<meta name="description" content="A cosy B&amp;B in Keswick"><style>body{color:red}</style>
<script>var tracking = 1;</script></head><body>
<nav><a href="/rooms">Rooms &amp; Rates</a> <a href="/blog/2019">Our blog</a> <a href="/faq">FAQ</a>
<a href="https://www.instagram.com/rose">Instagram</a> <a href="/brochure.pdf">Brochure</a></nav>
<h1>Welcome to Rose Cottage</h1><p>Five minutes from the lake.</p>
<p>Email <a href="mailto:stay@rosecottage.test">stay@rosecottage.test</a></p>
<footer>© Rose Cottage · Website by <a href="https://studionorth.test/?ref=rose">Studio North</a>
· Powered by <a href="https://wordpress.org">WordPress</a></footer></body></html>"""
NAV = ROSE_HOME[ROSE_HOME.index("<nav>"):ROSE_HOME.index("</nav>") + 6]
ROSE_ROOMS = """<html><body>""" + NAV + """
<h2>Double room</h2><p>£95 a night including breakfast</p><p>Dogs welcome in the garden room, £10 a night</p></body></html>"""
ROSE_FAQ = "<html><body><h2>Check-in</h2><p>From 3pm to 8pm</p></body></html>"
STUDIO = '<html><body><h1>Studio North</h1><p>Websites for small businesses. hello@studionorth.test</p></body></html>'

# --- reading one page --------------------------------------------------------------------------
assert sitetext.title_of(ROSE_HOME) == "Rose Cottage B&B"
text = sitetext.text_of(ROSE_HOME)
assert "A cosy B&B in Keswick" in text and "Five minutes from the lake." in text
assert "tracking" not in text and "color:red" not in text
links = sitetext.useful_links(ROSE_HOME, "http://rose.test/")
assert links == ["http://rose.test/rooms", "http://rose.test/faq"], links
assert sitetext.credits(ROSE_HOME, "http://rose.test/") == [("https://studionorth.test/", "Studio North")]
assert sitetext.credits('Website by <a href="https://www.wix.com/">Wix</a>', "http://rose.test") == [], "a builder isn't a designer"
assert sitetext.credits('Visit our website - <a href="https://partner.test">Partner</a>', "http://rose.test") == []
assert sitetext.credits('<a href="https://bluefox.test">Website by Blue Fox Digital</a>', "http://rose.test") == \
    [("https://bluefox.test/", "Blue Fox Digital")]
assert sitetext.normalise("rose.test/rooms") == "http://rose.test/rooms"
for bad in ("ftp://rose.test", "http://user:pw@rose.test", "http://rose.test:6379/", "javascript:alert(1)"):
    assert sitetext.normalise(bad) == "", bad
for private in ("localhost", "127.0.0.1", "10.1.2.3", "192.168.0.1", "169.254.169.254", "nonexistent.invalid"):
    assert not sitetext._public(private), private
print("PASS a page's text, its useful links and its web designer's credit are read; private addresses are refused")


async def main():
    hits = []

    def page(body):
        async def handler(request):
            hits.append(request.path)
            return aioweb.Response(text=body, content_type="text/html")
        return handler

    async def moved(request):
        raise aioweb.HTTPFound("/")

    async def sneaky(request):
        raise aioweb.HTTPFound(f"http://localhost:{request.url.port}/secret")

    app = aioweb.Application()
    for path, body in (("/", ROSE_HOME), ("/rooms", ROSE_ROOMS), ("/faq", ROSE_FAQ), ("/studio/", STUDIO),
                       ("/secret", "internal")):
        app.router.add_get(path, page(body))
    app.router.add_get("/old", moved)
    app.router.add_get("/sneaky", sneaky)
    async with TestServer(app) as server:
        base = str(server.make_url("/")).rstrip("/")
        real_public = sitetext._public
        async with ClientSession() as session:
            assert await sitetext.fetch(session, base + "/") == ("", ""), "127.0.0.1 is private"
            # the test server stands in for the public web; "localhost" stays private
            sitetext._public = lambda host: host == "127.0.0.1"
            sitetext.PORTS = None
            title, text = await sitetext.read_site(session, base + "/old")
            assert title == "Rose Cottage B&B" and "£95 a night including breakfast" in text and "From 3pm to 8pm" in text
            assert text.count("Rooms & Rates") == 1, "menus repeated on every page are kept once"
            assert "/blog/2019" not in hits and "/brochure.pdf" not in hits
            assert await sitetext.fetch(session, base + "/sneaky") == ("", ""), "a redirect to a private address is refused"
            assert "/secret" not in hits
        print("PASS a website is read: homepage plus the pages that matter, redirects checked one by one")

        # --- "paste your website" on /trial ------------------------------------------------------
        store = cs.Store(TMP / "chats.sqlite3")
        db = trials.Trials(TMP / "trials.sqlite3")
        told, systems = [], []

        class Model:
            async def __call__(self, system, messages):
                systems.append(system)
                return cs.Decision(reply="A double room is £95 a night with breakfast.", summary="?｜?｜?")

        async def owner(text):
            return 1

        async def on_trial(event, row):
            told.append((event, row["name"]))

        svc = cs.CustomerService(store, cs.load_catalog(), Model(), owner_notify=owner)
        chat = web.WebChat(svc, title="vinc", trials=db, on_trial=on_trial,
                           trial_service=lambda r: cs.CustomerService(store, trials.catalog_for(r), Model(),
                                                                      owner_notify=owner, persona=trials.persona_for(r)))
        async with TestClient(TestServer(chat.app())) as client:
            r = await client.post("/api/trial", json={"url": base + "/", "kind": "bnb"})
            slug = (await r.json())["slug"]
            row = db.get(slug)
            assert row["name"] == "Rose Cottage B&B" and row["site"] == base + "/" and "£95 a night" in row["info"]
            assert told == [("new", "Rose Cottage B&B")]
            info = await (await client.get(f"/api/trial/{slug}")).json()
            assert info["site"] == base + "/" and not info["sample"]
            await client.post("/api/chat", json={"v": V, "text": "How much is a double?", "demo": f"t-{slug}"})
            assert "£95 a night including breakfast" in systems[-1] and "read automatically" in systems[-1]
            assert "网站上没写的" in systems[-1], "it doesn't make up what the site doesn't say"
            r = await client.post("/api/trial", json={"url": base + "/nothing-here", "name": "Ghost"})
            assert r.status == 422 and (await r.json())["error"] == "site"
            r = await client.post("/api/trial", json={"url": "ftp://x"})
            assert r.status == 400 and (await r.json())["error"] == "url"
            r = await client.post("/api/trial", json={"name": "Typed Info", "info": "Cakes £20", "url": base + "/"})
            assert db.get((await r.json())["slug"])["site"] == "", "typed information wins over the website"
        page_html = (pathlib.Path(__file__).parent / "web_static" / "trial.html").read_text(encoding="utf-8")
        assert 'name="url"' in page_html and "url: url" in page_html
        shop_html = (pathlib.Path(__file__).parent / "web_static" / "trial-shop.html").read_text(encoding="utf-8")
        assert "wa.me/?text=" in shop_html and "t.me/share/url" in shop_html and "from=share" in shop_html
        print("PASS a shop pastes its website and gets an assistant that answers from it; it can pass the page on")

        # --- the map: shops' emails and their web designers, from their websites ------------------
        async def overpass(request):
            return aioweb.json_response({"elements": [
                {"tags": {"name": "Rose Cottage B&B", "tourism": "guest_house", "website": base + "/",
                          "addr:city": "Keswick"}}]})

        app2 = aioweb.Application()
        app2.router.add_post("/map", overpass)
        async with TestServer(app2) as mapserver:
            leadfinder.OVERPASS = [str(mapserver.make_url("/map"))]
            real_credits = sitetext.credits
            # the credited studio lives on the test server too
            sitetext.credits = lambda html, url: [(base + "/studio/", "Studio North")] if "Studio North" in html else []
            leadfinder.best_email = (lambda real: lambda emails, site: real(emails, site) or
                                     next((e for e in emails if e.endswith(".test")), ""))(leadfinder.best_email)
            leads = await leadfinder.find("uk", 1)
            again = await leadfinder.find("uk", 1, skip={"studio north"})
            sitetext.credits = real_credits
        by_email = {lead["email"]: lead for lead in leads}
        assert sorted(by_email) == ["hello@studionorth.test", "stay@rosecottage.test"], by_email
        studio = by_email["hello@studionorth.test"]
        assert studio["kind"] == "agency" and studio["name"] == "Studio North" and studio["client"] == "Rose Cottage B&B"
        assert studio["client_site"] == base + "/" and studio["cat"] == "bnb" and studio["city"] == "Keswick"
        assert "Rose Cottage B&B's website in Keswick and saw that you built it" in studio["first"]
        assert [lead["email"] for lead in again] == ["stay@rosecottage.test"], "a designer already on the list isn't added twice"
        print("PASS the map's shops lead to the web designers their websites credit")

        # --- the bot: personal demos that have read the website ----------------------------------
        import bot
        bot.DEEPSEEK_API_KEY = "sk-test"
        bot.trials_db = trials.Trials(TMP / "bot-trials.sqlite3")
        shop = {"email": "stay@rosecottage.test", "name": "Rose Cottage B&B", "region": "uk", "cat": "bnb", "site": base + "/"}
        lead = await bot.with_personal_demo(shop)
        assert lead["personal_read"] and "t.html?s=rose-cottage-b-b-" in lead["personal"]
        row = bot.trials_db.existing("stay@rosecottage.test")
        assert "£95 a night" in row["info"] and row["site"] == base + "/"
        before = len(hits)
        assert (await bot.with_personal_demo(shop))["personal"] == lead["personal"] and len(hits) == before, "read once"
        designer = await bot.with_personal_demo({**studio, "email": "hello@studionorth.test"})
        drow = bot.trials_db.existing("hello@studionorth.test")
        assert designer["personal_read"] and drow["name"] == "Rose Cottage B&B" and "£95" in drow["info"], \
            "a designer's demo is their client's site"
        offline = await bot.with_personal_demo({"email": "x@gone.test", "name": "Gone Bakery", "region": "uk",
                                                "cat": "bakery", "site": base + "/nothing-here"})
        assert offline["personal"] and not offline["personal_read"], "no website to read: the sample demo"
        sitetext._public = real_public
        print("PASS each email's demo has read the business's own website (a designer's: their client's)")
        return lead, designer

lead, designer = asyncio.run(main())

# --- the letters ---------------------------------------------------------------------------------
_, body = om.compose(lead)
assert f"I also set up a working demo for Rose Cottage B&B that has already read your website, so you can ask it what your customers ask: {lead['personal']}" in body
_, body = om.compose({**lead, "region": "sg"})
assert "我已經讓 AI 先讀了「Rose Cottage B&B」的網站" in body and "that has read your website" in body
subject, body = om.compose(designer)
assert subject == "Rose Cottage B&B's website + a 24/7 assistant (a partnership idea)", subject
assert body.startswith("Hi Studio North team,\n\nI came across Rose Cottage B&B's website in Keswick and saw that you built it.")
assert f"I also made a working demo for Rose Cottage B&B, built from its website, that you could show them: {designer['personal']}" in body
assert "you keep 30% of every setup fee" in body and "(mockup: https://" in body and 'reply "no thanks"' in body
assert om.mockup_slug(designer).startswith("a-")
html = om.compose_html(designer)
assert 'src="cid:mockup"' in html and "Mockup of Rose Cottage B&amp;B&#x27;s website" in html
png = om.mockup_png(designer, TMP / "mockups")
assert png and png[:4] == b"\x89PNG"
_, body = om.compose({**designer, "region": "hk", "personal_read": False})
assert "我看到Rose Cottage B&B的網站是你們做的" in body and "我也用Rose Cottage B&B的名字做了一個能直接聊天的示範版" in body
assert "in Rose Cottage B&B's name (with sample prices for now)" in body
assert "我用你們做的 Rose Cottage B&amp;B 網站畫了一張示意圖" in om.compose_html({**designer, "region": "hk"})
system = om.reply_messages(designer, "Re: partnership", "How does the commission work?")[0]["content"]
assert "keep 30% of every setup fee" in system
leads_db = om.Leads(TMP / "leads.sqlite3")
leads_db.add([designer])
saved = leads_db.get("hello@studionorth.test")
assert saved["client"] == "Rose Cottage B&B" and saved["kind"] == "agency" and saved["client_site"].endswith("/")
print("PASS web designers get a partnership letter about their own client, with its mockup and demo")
print("\nALL SITE TESTS PASSED")
