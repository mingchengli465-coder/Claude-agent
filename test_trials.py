"""Free trials and personal demos: a shop's own assistant on /t/<slug>, made from a form or for
each business we email. No network: run `python test_trials.py`.
"""
import asyncio, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242"})
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import outreach as om
import trials
import web

V = "0f8e2c3a-1111-2222-3333-444455556666"
TMP = pathlib.Path(tempfile.mkdtemp())


class Model:
    def __init__(self, reply): self.reply, self.systems = reply, []
    async def __call__(self, system, messages):
        self.systems.append(system)
        return cs.Decision(reply=self.reply, summary="?｜?｜?")


# ---- the store ----------------------------------------------------------------------------------
db = trials.Trials(TMP / "cs.sqlite3")
row = db.create("Rose Cottage B&B", "bnb", "Double room US$99 a night\nCheck-in 3pm", "rose@example.com")
assert row["slug"].startswith("rose-cottage-b-b-") and db.get(row["slug"])["info"].startswith("Double room")
assert db.create("店", "spaceship")["kind"] == "bnb", "an unknown kind falls back to the default"
assert db.get("../etc") is None and db.get("nope-0000") is None
lead = {"email": "Jill@GaleGreen.com", "name": "Gale Green Cottage", "region": "uk"}
mine = db.for_lead(lead, "bnb")
assert db.for_lead(lead, "bnb")["slug"] == mine["slug"] and mine["source"] == "lead", "one demo per business"
assert db.count("lead") == 1 and db.count("self") == 2
# what each one answers from
own = trials.catalog_for(row)
assert own.startswith("shop: Rose Cottage B&B\nDouble room US$99") and "Willow" not in own
sample = trials.catalog_for(mine)
assert sample.startswith("shop: Gale Green Cottage (sample information") and "US$120 a night" in sample
assert "Keswick" not in sample and "Willow" not in sample, "the sample keeps no other shop's name or town"
assert "店主自己" in trials.persona_for(row) and "示范样本" in trials.persona_for(mine)
pub = trials.public(mine)
assert pub["sample"] and pub["lead"] and "email" not in str(pub).lower() and "contact" not in pub
print("PASS trials are stored, one personal demo per business, each answering from its own information")


# ---- the web side -------------------------------------------------------------------------------
async def main():
    store = cs.Store(TMP / "chats.sqlite3")
    model, notices, told = Model("A double room is US$99 a night."), [], []

    async def owner(text):
        notices.append(text); return len(notices)

    svc = cs.CustomerService(store, cs.load_catalog(), Model("Vincent's assistant"), owner_notify=owner)
    built = []

    def trial_service(r):
        built.append(r["slug"])
        return cs.CustomerService(store, trials.catalog_for(r), model, owner_notify=owner, persona=trials.persona_for(r))

    async def on_trial(event, r):
        told.append((event, r["name"]))

    clock = [1000.0]
    chat = web.WebChat(svc, title="vinc", trials=db, trial_service=trial_service, on_trial=on_trial,
                       clock=lambda: clock[0])
    async with TestClient(TestServer(chat.app())) as client:
        page = await (await client.get("/trial")).text()
        assert 'name="info"' in page and '<option value="florist">花店 · Florist</option>' in page and "{{" not in page
        r = await client.post("/api/trial", json={"name": "Bloom Room", "kind": "florist", "info": "Roses US$60",
                                                  "contact": "+44 7000 000000"})
        slug = (await r.json())["slug"]
        assert slug.startswith("bloom-room-") and told[-1] == ("new", "Bloom Room")
        assert (await client.post("/api/trial", json={"name": "x"})).status == 400
        for _ in range(2):
            assert (await client.post("/api/trial", json={"name": "Again"})).status == 200
        assert (await client.post("/api/trial", json={"name": "Spam"})).status == 429, "a few per address an hour"
        print("PASS a shop makes its own trial from the form, and the owner is told")

        # its page and its chat
        r = await client.get(f"/t/{slug}")
        html = await r.text()
        assert r.status == 200 and '<base href="/">' in html and "/api/trial/" in html and "{{" not in html
        assert (await client.get("/t/nope-0000")).status == 404
        info = await (await client.get(f"/api/trial/{slug}")).json()
        assert info["name"] == "Bloom Room" and info["label"] == "Florist" and not info["sample"] and info["questions"]
        r = await client.post("/api/chat", json={"v": V, "text": "How much are roses?", "demo": f"t-{slug}"})
        assert (await r.json())["reply"] == "A double room is US$99 a night."
        assert "Roses US$60" in model.systems[-1] and "Bloom Room" in model.systems[-1]
        rows = (await (await client.get(f"/api/messages?v={V}&after=0&d=t-{slug}")).json())["messages"]
        assert [m["role"] for m in rows] == ["customer", "assistant"]
        await client.post("/api/chat", json={"v": V, "text": "And tulips?", "demo": f"t-{slug}"})
        assert built.count(slug) == 1, "each assistant is built once"
        assert (await client.post("/api/chat", json={"v": V, "text": "hi", "demo": "t-nope-0000"})).status == 400
        print("PASS the shop's own page and assistant answer from what it typed in")

        # the owner's Telegram reply to a trial visitor's notice reaches that visitor
        msg_id = await owner("notice")
        svc.link_owner_message(msg_id, f"web-t-{slug}", V)
        assert await svc.deliver_owner_reply(msg_id, "I'll call you") == (f"web-t-{slug}", V)
        print("PASS the owner can answer a trial's visitor from Telegram")

        # the personal demo: its business opening it is news, once in a while
        info = await (await client.get(f"/api/trial/{mine['slug']}")).json()
        assert info["sample"] and info["lead"] and told[-1] == ("view", "Gale Green Cottage")
        await client.get(f"/api/trial/{mine['slug']}")
        assert sum(e == "view" for e, _ in told) == 1, "not again within hours"
        clock[0] += 7 * 3600
        await client.get(f"/api/trial/{mine['slug']}")
        assert sum(e == "view" for e, _ in told) == 2
        print("PASS the owner hears when an emailed business opens the demo made in its name")

asyncio.run(main())

# ---- the letters --------------------------------------------------------------------------------
url = om.TRIAL_PAGE.format(slug=mine["slug"])
assert url == f"https://mingchengli465-coder.github.io/Claude-agent/t.html?s={mine['slug']}&from=email"
uk = {"email": "jill@galegreen.com", "name": "Gale Green Cottage", "region": "uk", "cat": "bnb", "host": "Jill",
      "mockup": "gale-green"}
_, body = om.compose({**uk, "personal": url})
assert f"I also set up a working demo in Gale Green Cottage's name, with sample prices for now, that you can chat with: {url}" in body
assert "demo-bnb.html" not in body
_, body = om.compose(uk)
assert "chat with a demo one for a B&B here: https://mingchengli465-coder.github.io/Claude-agent/demo-bnb.html?from=uk" in body
assert f'<a href="{url.replace("&", "&amp;")}">' in om.compose_html({**uk, "personal": url}), "the HTML letter links it too"
hk = {"email": "info@vive.hk", "name": "Vive Cake Boutique", "region": "hk", "personal": url}
_, body = om.compose(hk)
assert f"我用「Vive Cake Boutique」的名字先做了一個能直接聊天的示範版（價格先用示範資料）：{url}" in body
assert f"I've already set up a working demo in Vive Cake Boutique's name (with sample prices for now) that you can chat with: {url}" in body
assert "可以先到我的網站試試看" not in body
_, follow = om.compose_followup({**uk, "personal": url})
assert url.replace("from=email", "from=followup") in follow
_, follow = om.compose_followup(hk)
assert "用你們店名做的示範版還在" in follow and "The demo I made in your name is still here" in follow
_, follow = om.compose_followup(uk)
assert "t.html" not in follow
assert url in om.reply_messages(hk, "Re: hi", "how much?")[0]["content"]
print("PASS each letter and follow-up links the demo made in the business's own name")

# ---- the bot: a personal demo for each shop it emails, and an assistant for each trial ----------
os.environ["CS_DB_PATH"] = str(TMP / "bot.sqlite3")
cs.CS_DB_PATH = TMP / "bot.sqlite3"
import bot
bot.DEEPSEEK_API_KEY = "sk-test"

class FakeTelegram:
    async def send_message(self, **k): return type("M", (), {"message_id": 1})()

svc = bot.build_customer_service(FakeTelegram())
assert bot.trials_db is not None and svc.trials is bot.trials_db
built = svc.trial_service(bot.trials_db.create("Paws Up", "groomer"))
assert "Paws Up" in built.system and "cockapoo" in built.system.lower() and built.store is svc.store
withdemo = bot.with_personal_demo({"email": "a@florist.uk", "name": "Lily Florist", "region": "uk"})
assert withdemo["personal"].startswith("https://mingchengli465-coder.github.io/Claude-agent/t.html?s=lily-florist-")
assert bot.trials_db.get(withdemo["personal"].split("s=")[1].split("&")[0])["kind"] == "florist"
assert "personal" not in bot.with_personal_demo({"email": "a@x.sg", "name": "Pixel Studio", "kind": "agency"})
assert "personal" not in bot.with_personal_demo({"email": "a@x.sg", "name": "Happy Oven"}), "unknown kind: none"
print("PASS the bot makes a personal demo for each shop it emails and builds each trial's assistant")

# ---- the published site -------------------------------------------------------------------------
sys.path.insert(0, str(pathlib.Path(__file__).parent / "tools"))
import build_pages
out = TMP / "_site"
build_pages.build(out)
t = (out / "t.html").read_text(encoding="utf-8")
assert f'var API = "{build_pages.API}";' in t and "<base" not in t
assert f'var API = "{build_pages.API}";' in (out / "trial.html").read_text(encoding="utf-8")
assert "/trial.html</loc>" in (out / "sitemap.xml").read_text(encoding="utf-8")
print("PASS the trial form and the shop page are published with the site")
print("\nALL TRIAL TESTS PASSED")
