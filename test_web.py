"""The website chat window, over real HTTP on a local port, with a fake model.

No network, no keys: run `python test_web.py`.
"""
import asyncio, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aiohttp
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import web


class FakeModel:
    def __init__(self): self.next = None
    async def __call__(self, system, messages):
        return self.next or cs.Decision(reply="Hi! What would you like to build?", summary="?｜?｜?")

class Owner:
    def __init__(self): self.notices, self.next_id = [], 500
    async def __call__(self, text):
        self.next_id += 1
        self.notices.append((self.next_id, text))
        return self.next_id

class Clock:
    def __init__(self): self.t = 1_760_000_000.0
    def __call__(self): return self.t


def fresh():
    model, owner, clock = FakeModel(), Owner(), Clock()
    store = cs.Store(pathlib.Path(tempfile.mkdtemp()) / "cs.sqlite3")
    svc = cs.CustomerService(store, cs.load_catalog(), model, owner_notify=owner, clock=clock)
    chat = web.WebChat(svc, title="小店<b>", contact_link="https://t.me/emilyhanbot", clock=clock)
    return chat, svc, model, owner, clock


async def main():
    V = "0f8e2c3a-1111-2222-3333-444455556666"

    # --- pages -------------------------------------------------------------------
    chat, svc, model, owner, clock = fresh()
    async with TestClient(TestServer(chat.app())) as client:
        r = await client.get("/")
        site = await r.text()
        assert r.status == 200 and "text/html" in r.headers["Content-Type"]
        assert "Claude Code" in site and "AI 客服" in site and '<script src="/widget.js"' in site
        assert "https://t.me/emilyhanbot" in site and "{{" not in site
        for tag in ("zh-CN", "zh-TW", "en"):
            assert f'data-lang="{tag}"' in site, tag
        assert 'href="/demo"' in site, "the site links to the merchant demo"
        # The owner's own contacts come from products.yaml.
        assert 'href="https://t.me/Vinceeeeentttt"' in site and "@Vinceeeeentttt" in site
        assert 'data-copy="Vinc100327"' in site and 'data-copy="Abide2837"' in site
        assert 'href="https://www.facebook.com/profile.php?id=61574166461675"' in site
        assert 'href="https://x.com/Vincent40769988"' in site and 'href="mailto:mingchengli465@gmail.com"' in site
        r = await client.get("/demo")
        page = await r.text()
        assert r.status == 200 and "text/html" in r.headers["Content-Type"]
        assert "小店&lt;b&gt;" in page and "小店<b>" not in page, "the title is escaped"
        assert "{{" not in page
        assert "Vinc100327 / Abide2837" in page and "https://t.me/Vinceeeeentttt" in page
        assert '<script src="/widget.js"' in page
        r = await client.get("/widget.js")
        js = await r.text()
        assert r.status == 200 and "javascript" in r.headers["Content-Type"]
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        assert "textContent" in js and "innerHTML = text" not in js, "messages are never parsed as HTML"
        assert (await client.get("/healthz")).status == 200
        r = await client.get("/favicon.ico")
        assert r.status == 200 and "svg" in r.headers["Content-Type"]
        assert 'rel="icon"' in page, "the demo page has its own icon"
        for font in ("serif-sc.woff2", "serif-tc.woff2", "instrument-serif-latin-400-normal.woff2"):
            assert f"/fonts/{font}" in page and (await client.get(f"/fonts/{font}")).status == 200, font
        for font in ("instrument-serif-latin-400-italic.woff2", "geist-sans-latin-500-normal.woff2", "geist-mono-latin-400-normal.woff2"):
            assert f"/fonts/{font}" in site, font
            r = await client.get(f"/fonts/{font}")
            assert r.status == 200 and r.headers["Content-Type"] == "font/woff2" and len(await r.read()) > 1000, font
        for bad in ("../web.py", "LICENSE.txt", "nope.woff2", "..%2Fweb.py"):
            assert (await client.get(f"/fonts/{bad}")).status == 404, bad
        assert "fonts.googleapis.com" not in site, "Google Fonts doesn't load in mainland China"
        r = await client.options("/api/chat")
        assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "*"
    print("PASS the personal site, the demo page, the widget script and CORS are served; the shop name is escaped")

    # --- a visitor chats; the owner is told and replies; the widget picks it up ----------
    chat, svc, model, owner, clock = fresh()
    async with TestClient(TestServer(chat.app())) as client:
        r = await client.post("/api/chat", json={"v": V, "text": "Do you build websites?", "lang": "en-US"})
        data = await r.json()
        assert r.status == 200 and data["reply"] == "Hi! What would you like to build?", data
        assert r.headers["Access-Control-Allow-Origin"] == "*"
        c = svc.store.customer("web", V)
        assert c and c["display_name"] == "网页访客 0f8e" and c["lang"] == "en"
        assert len(owner.notices) == 1 and "网站聊天窗口" in owner.notices[0][1] and f"web:{V}" in owner.notices[0][1]

        r = await client.get(f"/api/messages?v={V}&after=0")
        msgs = (await r.json())["messages"]
        assert [m["role"] for m in msgs] == ["customer", "assistant"], msgs
        assert msgs[1]["id"] == data["reply_id"], "the widget can tell it already showed the reply"
        cursor = msgs[-1]["id"]

        assert await svc.deliver_owner_reply(owner.notices[0][0], "Hi, it's me — yes we do!") == ("web", V)
        r = await client.get(f"/api/messages?v={V}&after={cursor}")
        msgs = (await r.json())["messages"]
        assert msgs == [{"id": cursor + 1, "role": "owner", "text": "Hi, it's me — yes we do!", "ts": clock.t}], msgs
        assert (await (await client.get(f"/api/messages?v=other-visitor-1&after=0")).json())["messages"] == []

        # A keen visitor: the owner hears about it.
        model.next = cs.Decision(reply="Great, a 3-page site is ¥99.", summary="网站｜未说明｜99", intent="ready")
        clock.t += 30
        await client.post("/api/chat", json={"v": V, "text": "Let's do it"})
        assert "💰 客户准备购买了（网站聊天窗口）" in owner.notices[-1][1], owner.notices[-1]
    print("PASS a website visitor gets AI replies, the owner is notified and can reply from Telegram")

    # --- bad input and abuse -------------------------------------------------------------
    chat, svc, model, owner, clock = fresh()
    async with TestClient(TestServer(chat.app())) as client:
        for body in ({"v": "short", "text": "hi"}, {"v": "../../etc/passwd", "text": "hi"},
                     {"v": V, "text": "   "}, ["not", "an", "object"]):
            assert (await client.post("/api/chat", json=body)).status == 400, body
        assert (await client.post("/api/chat", data="not json")).status == 400
        assert (await client.get("/api/messages?v=bad")).status == 400
        r = await client.post("/api/chat", data="x" * 100_000, headers={"Content-Type": "application/json"})
        assert r.status in (400, 413), r.status
        await client.post("/api/chat", json={"v": V, "text": "长" * 3000})
        assert len(svc.store.recent_messages("web", V, 1)[0]["text"]) <= web.MAX_TEXT + 50

        # New visitor ids from one address are capped (each one pings the owner).
        statuses = []
        for i in range(web.IP_NEW_VISITORS_PER_HOUR + 2):
            r = await client.post("/api/chat", json={"v": f"visitor-{i:04d}", "text": "hi"})
            statuses.append(r.status)
        assert statuses.count(429) >= 2, statuses
        assert len(owner.notices) <= web.IP_NEW_VISITORS_PER_HOUR, len(owner.notices)
        # ...but a known visitor keeps chatting.
        clock.t += 61
        assert (await client.post("/api/chat", json={"v": V, "text": "还在吗"})).status == 200
    print("PASS bad requests are refused, long messages are cut, and one address can't spam new visitors")

    # --- a failing service doesn't take the server down --------------------------------------
    chat, svc, model, owner, clock = fresh()
    async def boom(msg): raise RuntimeError("db gone")
    svc.handle = boom
    async with TestClient(TestServer(chat.app())) as client:
        assert (await client.post("/api/chat", json={"v": V, "text": "hi"})).status == 500
        assert (await client.get("/healthz")).status == 200
    print("PASS an internal error is a 500 for that request, not a crash")

    # --- start/stop on a real port -------------------------------------------------------------
    chat, svc, model, owner, clock = fresh()
    await chat.start(port=0)
    await chat.stop()
    print("PASS the server starts and stops cleanly")

    assert web.shop_name() == "vinc的ai铺子", web.shop_name()
    bad = pathlib.Path(tempfile.mkdtemp()) / "p.yaml"
    bad.write_text("联系本人:\n  Telegram: javascript:alert(1)\n  微信号: ['ok_id123', '<script>', 'x']\n", encoding="utf-8")
    assert web.owner_contacts(bad) == ("", ["ok_id123"]), "malformed contacts never reach the page"
    assert web.owner_contacts(pathlib.Path("/nonexistent.yaml")) == ("", [])
    links = pathlib.Path(tempfile.mkdtemp()) / "p.yaml"
    links.write_text("联系本人:\n  X: https://x.com/someone\n  Facebook: https://www.facebook.com/profile.php?id=123\n"
                     "  邮箱: 'a@b.co\"><script>'\n", encoding="utf-8")
    assert web.owner_links(links) == [("X", "https://x.com/someone", "@someone"),
                                      ("Facebook", "https://www.facebook.com/profile.php?id=123", "→")]
    links.write_text("联系本人:\n  X: javascript:alert(1)\n  Facebook:\n  邮箱: me@example.com\n", encoding="utf-8")
    assert web.owner_links(links) == [("Email", "mailto:me@example.com", "me@example.com")], "bad links and blanks are dropped"
    print("PASS the shop name comes from products.yaml")


asyncio.run(main())
print("\nALL WEB CHAT TESTS PASSED")
