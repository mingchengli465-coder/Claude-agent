"""Facebook: setup from a User token, Messenger replies, comment replies, Page posts and
owner replies, against a fake Graph API. No network, no keys: run `python test_messenger.py`.
"""
import asyncio, hashlib, hmac, json, os, pathlib, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import messenger as ms
import web as web_mod

SECRET, VERIFY, APP, PAGE = "app-secret", "my-verify", "APP1", "PAGE1"
REPLY = "国内 200 元搭建，之后每月 49 元～你是国内还是海外的店呀？"


class Model:
    async def __call__(self, system, messages):
        return cs.Decision(reply=REPLY, summary="AI客服｜未说明｜未说明", intent="interested")


class Owner:
    def __init__(self): self.notices, self.n = [], 100
    async def __call__(self, text):
        self.n += 1; self.notices.append(text); return self.n


def signed(body: bytes) -> dict:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def message(sender, text=None, mid="m1", **extra):
    msg = {"mid": mid, **extra}
    if text is not None:
        msg["text"] = text
    return json.dumps({"object": "page", "entry": [{"id": PAGE, "time": 1, "messaging": [
        {"sender": {"id": sender}, "recipient": {"id": PAGE}, "timestamp": 1, "message": msg}]}]}).encode()


def comment(author, text, cid, verb="add", name="Ann"):
    return json.dumps({"object": "page", "entry": [{"id": PAGE, "time": 1, "changes": [{"field": "feed", "value": {
        "item": "comment", "verb": verb, "comment_id": cid, "post_id": f"{PAGE}_9", "message": text,
        "from": {"id": author, "name": name}}}]}]}).encode()


async def until(cond, tries=60):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def main():
    calls = []  # (method, path, query, json)
    app_client_holder = {}

    async def graph(request):
        path = request.match_info["tail"]
        body = await request.json() if request.can_read_body else None
        q = dict(request.query)
        calls.append((request.method, path, q, body))
        if path == "oauth/access_token":
            assert q["client_id"] == APP and q["client_secret"] == SECRET and q["fb_exchange_token"] == "short-user"
            return web.json_response({"access_token": "long-user"})
        if path == "me/accounts":
            assert q["access_token"] == "long-user"
            return web.json_response({"data": [{"id": PAGE, "name": "vinc AI studio", "access_token": "page-forever"}]})
        if path == f"{APP}/subscriptions":
            # Meta calls our webhook back to check the verify token, as the real one does
            r = await app_client_holder["c"].get("/fb/webhook", params={
                "hub.mode": "subscribe", "hub.verify_token": q["verify_token"], "hub.challenge": "ok"})
            assert r.status == 200 and q["callback_url"] == "https://bot.example/fb/webhook"
            assert q["access_token"] == f"{APP}|{SECRET}" and q["fields"] == "messages,feed"
            return web.json_response({"success": True})
        assert q.get("access_token") == "page-forever", (path, q)
        if path == f"{PAGE}/subscribed_apps":
            assert q["subscribed_fields"] == "messages,feed"
            return web.json_response({"success": True})
        if path == f"{PAGE}/feed":
            return web.json_response({"id": f"{PAGE}_777"})
        return web.json_response({"id": "ok"})
    fake = web.Application(); fake.router.add_route("*", "/{tail:.*}", graph)

    async with TestServer(fake) as graph_server:
        ms.GRAPH_URL = str(graph_server.make_url("")).rstrip("/")
        tmp = pathlib.Path(tempfile.mkdtemp())
        store = cs.Store(tmp / "cs.sqlite3")
        owner, told = Owner(), []
        svc = cs.CustomerService(store, cs.load_catalog(), Model(), owner_notify=owner)

        async def notify(text): told.append(text)
        fb = ms.Messenger(svc, SECRET, VERIFY, user_token="short-user", app_id=APP,
                          callback_url="https://bot.example/fb/webhook", state_path=tmp / "fb_state.json", notify=notify)
        chat = web_mod.WebChat(svc, messenger=fb)
        async with TestClient(TestServer(chat.app())) as client:
            app_client_holder["c"] = client

            # --- setup from one short-lived User token ---------------------------------------
            r = await client.get("/fb/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
            assert r.status == 403
            try:
                await fb.post("too early")
                raise AssertionError("posting before connect must fail")
            except RuntimeError:
                pass
            assert await fb.connect() is True
            assert fb.page_token == "page-forever" and fb.page_id == PAGE and fb.connected
            assert told and "vinc AI studio" in told[0], told
            saved = json.loads((tmp / "fb_state.json").read_text())
            assert saved["page_token"] == "page-forever" and "short-user" not in json.dumps(saved)
            paths = [c[1] for c in calls]
            assert paths[:2] == ["oauth/access_token", "me/accounts"] and f"{PAGE}/subscribed_apps" in paths
            assert f"{APP}/subscriptions" in paths
            print("PASS one User token becomes a Page token that never expires; the Page and webhook are subscribed")

            # a restart reuses the saved Page token instead of exchanging again, and doesn't re-announce
            calls.clear(); told.clear()
            again = ms.Messenger(svc, SECRET, VERIFY, user_token="short-user", app_id=APP,
                                 callback_url="https://bot.example/fb/webhook", state_path=tmp / "fb_state.json", notify=notify)
            assert await again.connect() and again.page_token == "page-forever"
            assert "oauth/access_token" not in [c[1] for c in calls] and not told
            await again.close()
            svc.register_channel(ms.CHANNEL, fb.send); svc.register_channel(ms.COMMENT_CHANNEL, fb.reply_comment)
            print("PASS after a restart the saved Page token is reused")

            # --- Messenger -------------------------------------------------------------------
            calls.clear()
            body = message("PSID123", "多少钱")
            assert (await client.post("/fb/webhook", data=body, headers={"Content-Type": "application/json"})).status == 403
            assert (await client.post("/fb/webhook", data=body, headers=signed(body))).status == 200
            await until(lambda: any(c[1] == "me/messages" for c in calls))
            sent = [c for c in calls if c[1] == "me/messages"]
            assert sent[0][3] == {"recipient": {"id": "PSID123"}, "messaging_type": "RESPONSE", "message": {"text": REPLY}}
            assert "Facebook Messenger" in owner.notices[0]
            await client.post("/fb/webhook", data=body, headers=signed(body))  # Meta's retry
            echo = message(PAGE, "hi", mid="m2", is_echo=True)
            await client.post("/fb/webhook", data=echo, headers=signed(echo))
            await asyncio.sleep(0.1)
            assert len([c for c in calls if c[1] == "me/messages"]) == 1
            key = await svc.deliver_owner_reply(owner.n, "我是本人，可以的！")
            assert key == ("facebook", "PSID123") and calls[-1][3]["message"]["text"] == "我是本人，可以的！"
            print("PASS Messenger: signed messages get the AI reply once; the owner's Telegram reply goes back")

            # --- comments --------------------------------------------------------------------
            calls.clear()
            c1 = comment("USER9", "How much for the AI assistant?", "C1")
            await client.post("/fb/webhook", data=c1, headers=signed(c1))
            await until(lambda: any(c[1] == "C1/comments" for c in calls))
            assert [c for c in calls if c[1] == "C1/comments"][0][3] == {"message": REPLY}
            assert "Facebook 评论" in owner.notices[-1] and "Ann" in owner.notices[-1]
            # our own reply comes back as a comment from the Page, edits and likes are ignored
            for extra in (comment(PAGE, REPLY, "C2"), comment("USER9", "edited", "C1", verb="edited")):
                await client.post("/fb/webhook", data=extra, headers=signed(extra))
            await asyncio.sleep(0.1)
            assert len([c for c in calls if c[1].endswith("/comments")]) == 1
            key = await svc.deliver_owner_reply(owner.n, "私信我哦")
            assert key == ("fb_comment", "USER9") and calls[-1][1] == "C1/comments"
            print("PASS comments get an AI reply underneath; the Page's own comments are ignored; the owner can reply")

            # --- posting ---------------------------------------------------------------------
            url = await fb.post("Hello from the bot")
            assert url == f"https://www.facebook.com/{PAGE}_777" and calls[-1][3] == {"message": "Hello from the bot"}
            print("PASS posts go to the Page feed")

            # the privacy page Meta asks for
            page = await (await client.get("/privacy")).text()
            assert "Privacy Policy" in page and "隐私政策" in page and "{{" not in page and "mailto:mingchengli465@gmail.com" in page
            print("PASS /privacy is served with the owner's email")
        await fb.close()

    # a bad token is reported to the owner, not raised
    async def broken(request):
        return web.json_response({"error": {"message": "Invalid OAuth access token"}}, status=400)
    bad = web.Application(); bad.router.add_route("*", "/{tail:.*}", broken)
    async with TestServer(bad) as s:
        ms.GRAPH_URL = str(s.make_url("")).rstrip("/")
        told = []
        async def notify2(text): told.append(text)
        fb = ms.Messenger(svc, SECRET, VERIFY, user_token="expired", app_id=APP, notify=notify2)
        assert await fb.connect() is False and not fb.connected and "Invalid OAuth" in told[0]
        await fb.close()
    print("PASS a bad token is reported to the owner; the bot keeps running")

    # without the settings Facebook stays off and the route doesn't exist
    for k in ("FB_PAGE_TOKEN", "FB_USER_TOKEN", "FB_APP_ID", "FB_APP_SECRET", "FB_VERIFY_TOKEN"):
        os.environ.pop(k, None)
    assert ms.Messenger.from_env(svc) is None
    os.environ.update(FB_APP_SECRET="s", FB_VERIFY_TOKEN="v")
    assert ms.Messenger.from_env(svc) is None, "a secret alone isn't enough"
    os.environ.update(FB_USER_TOKEN="u", FB_APP_ID="a", RAILWAY_PUBLIC_DOMAIN="bot.up.railway.app")
    fb = ms.Messenger.from_env(svc)
    assert fb is not None and fb.callback_url == "https://bot.up.railway.app/fb/webhook"
    for k in ("FB_USER_TOKEN", "FB_APP_ID", "FB_APP_SECRET", "FB_VERIFY_TOKEN", "RAILWAY_PUBLIC_DOMAIN"):
        os.environ.pop(k, None)
    async with TestClient(TestServer(web_mod.WebChat(svc).app())) as client:
        assert (await client.get("/fb/webhook")).status == 404
    print("PASS Facebook is off until the App ID, App Secret, verify token and a token are set")

asyncio.run(main())
print("\nALL FACEBOOK TESTS PASSED")
