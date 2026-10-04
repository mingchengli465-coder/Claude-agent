"""Industry demos: a pretend shop of each kind, whose own assistant answers on /demo-<kind>,
linked from the cold emails. No network: run `python test_demos.py`.
"""
import asyncio, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242"})
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import demos
import outreach as om
import web

V = "0f8e2c3a-1111-2222-3333-444455556666"


class Model:
    def __init__(self, reply): self.reply, self.systems = reply, []
    async def __call__(self, system, messages):
        self.systems.append(system)
        return cs.Decision(reply=self.reply, summary="?｜?｜?")


async def main():
    store = cs.Store(pathlib.Path(tempfile.mkdtemp()) / "cs.sqlite3")
    own, demo_model = Model("Vincent's assistant here"), Model("A double room is US$120 a night for two.")
    notices = []

    async def owner(text):
        notices.append(text); return len(notices)

    svc = cs.CustomerService(store, cs.load_catalog(), own, owner_notify=owner)
    shops = {kind: cs.CustomerService(store, d["catalog"], demo_model, owner_notify=owner,
                                      persona=demos.DEMO_PERSONA.format(name=d["name"]))
             for kind, d in demos.DEMOS.items()}
    chat = web.WebChat(svc, title="vinc", contact_link="https://t.me/emilyhanbot", demos=shops)
    async with TestClient(TestServer(chat.app())) as client:
        # the page
        r = await client.get("/demo-bnb")
        page = await r.text()
        assert r.status == 200 and "Willow Cottage B&amp;B" in page and "民宿的 24 小時 AI 客服" in page
        assert 'data-demo="bnb"' in page and "{{" not in page and "aiChatAsk(this.textContent)" in page
        assert "Is a double room free next Friday and Saturday?" in page and 'href="demo-florist"' in page
        assert 'href="demo-bnb"' not in page, "the other demos are listed, not this one"
        assert (await client.get("/demo-nope")).status == 404
        print("PASS each kind of business has a demo page with ready-made questions")

        # the chat: the demo shop answers, from its own list, under its own channel
        r = await client.post("/api/chat", json={"v": V, "text": "How much is a double room?", "demo": "bnb"})
        assert (await r.json())["reply"] == "A double room is US$120 a night for two."
        assert "Willow Cottage B&B" in demo_model.systems[-1] and "US$120 a night" in demo_model.systems[-1]
        assert "Willow Cottage" not in "".join(own.systems), "the owner's own assistant isn't involved"
        rows = (await (await client.get(f"/api/messages?v={V}&after=0&d=bnb")).json())["messages"]
        assert [m["role"] for m in rows] == ["customer", "assistant"]
        assert (await (await client.get(f"/api/messages?v={V}&after=0")).json())["messages"] == [], "kept apart"
        r = await client.post("/api/chat", json={"v": V, "text": "hi", "demo": "spaceship"})
        assert r.status == 400
        r = await client.post("/api/chat", json={"v": V, "text": "hi"})
        assert (await r.json())["reply"] == "Vincent's assistant here", "no demo: the owner's own assistant"
        print("PASS the demo shop answers from its own prices, apart from the owner's own chats")

    # the widget passes the demo on
    js = (pathlib.Path(__file__).parent / "web_static" / "widget.js").read_text(encoding="utf-8")
    assert 'demo: cfg.demo || ""' in js and '"&d=" + cfg.demo' in js

    # the published site has every demo
    sys.path.insert(0, str(pathlib.Path(__file__).parent / "tools"))
    import build_pages
    out = pathlib.Path(tempfile.mkdtemp()) / "_site"
    build_pages.build(out)
    for kind in demos.DEMOS:
        html = (out / f"demo-{kind}.html").read_text(encoding="utf-8")
        assert f'data-demo="{kind}"' in html and f'src="{build_pages.API}/widget.js"' in html, kind
    assert "/demo-groomer.html</loc>" in (out / "sitemap.xml").read_text(encoding="utf-8")
    print("PASS every demo is published with the site and listed in the sitemap")

    # the emails point at the demo of the business's own kind
    _, body = om.compose({"email": "jill@galegreen.com", "name": "Gale Green Cottage", "region": "uk", "cat": "bnb",
                          "host": "Jill", "mockup": "gale-green"})
    assert "chat with a demo one for a B&B here: https://mingchengli465-coder.github.io/Claude-agent/demo-bnb.html?from=uk" in body
    _, body = om.compose({"email": "info@vive.hk", "name": "Vive Cake Boutique", "region": "hk"})
    assert "demo-bakery.html?from=email" in body and "可以先到我的網站試試看：https://mingchengli465-coder.github.io/Claude-agent/demo-bakery.html" in body
    _, body = om.compose({"email": "a@studio.sg", "name": "Pixel Studio", "region": "sg", "kind": "agency"})
    assert "demo-" not in body.replace("demo.html", ""), "agencies get the general demo"
    _, body = om.compose({"email": "x@y.sg", "name": "Happy Oven", "region": "sg"})
    assert "Claude-agent/?from=email" in body, "a business of unknown kind gets the site"
    print("PASS each email links to the demo shop of the business's own kind")

    # the model that drafts an answer to a reply knows the business, the first email and only real facts
    msgs = om.reply_messages({"email": "info@vive.hk", "name": "Vive Cake Boutique", "region": "hk"},
                             "Re: hello", "多少錢？")
    assert msgs[0]["role"] == "system" and "US$70" in msgs[0]["content"] and "demo-bakery.html" in msgs[0]["content"]
    assert "Traditional Chinese" in msgs[0]["content"] and "Never invent" in msgs[0]["content"]
    assert "Vive Cake Boutique" in msgs[1]["content"] and "多少錢？" in msgs[1]["content"] and "你好" in msgs[1]["content"]
    print("PASS the reply drafter gets the business, the first email and only real facts")

asyncio.run(main())

# the bot builds one assistant per demo, on the same model as the owner's
import bot
os.environ["DEEPSEEK_API_KEY"] = "sk-test"
bot.DEEPSEEK_API_KEY = "sk-test"

class FakeTelegram:
    async def send_message(self, **k): return type("M", (), {"message_id": 1})()

svc = bot.build_customer_service(FakeTelegram())
assert sorted(svc.demo_services) == sorted(demos.DEMOS)
assert "Petal & Stem" in svc.demo_services["florist"].system and svc.demo_services["florist"].store is svc.store
print("PASS the bot runs one assistant per demo shop")
print("\nALL DEMO TESTS PASSED")
