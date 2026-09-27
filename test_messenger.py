"""Facebook Messenger: webhook check, signed deliveries, AI replies and owner replies,
against a fake Graph API. No network, no keys: run `python test_messenger.py`.
"""
import asyncio, hashlib, hmac, json, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import messenger as ms
import web as web_mod

SECRET, VERIFY = "app-secret", "my-verify"


class Model:
    async def __call__(self, system, messages):
        return cs.Decision(reply="国内 200 元搭建，之后每月 49 元～你是国内还是海外的店呀？", summary="AI客服｜未说明｜未说明",
                           intent="interested")


class Owner:
    def __init__(self): self.notices, self.n = [], 100
    async def __call__(self, text):
        self.n += 1; self.notices.append(text); return self.n


def signed(body: bytes) -> dict:
    return {"X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
            "Content-Type": "application/json"}


def event(sender, text=None, mid="m1", **extra):
    msg = {"mid": mid, **extra}
    if text is not None:
        msg["text"] = text
    return json.dumps({"object": "page", "entry": [{"id": "PAGE", "time": 1, "messaging": [
        {"sender": {"id": sender}, "recipient": {"id": "PAGE"}, "timestamp": 1, "message": msg}]}]}).encode()


async def main():
    sent = []

    async def graph(request):
        assert request.query["access_token"] == "page-token"
        sent.append(await request.json())
        return web.json_response({"recipient_id": "x", "message_id": "y"})
    fake = web.Application(); fake.router.add_post("/me/messages", graph)
    async with TestServer(fake) as graph_server:
        ms.GRAPH_URL = str(graph_server.make_url("")).rstrip("/")
        store = cs.Store(pathlib.Path(tempfile.mkdtemp()) / "cs.sqlite3")
        owner = Owner()
        svc = cs.CustomerService(store, cs.load_catalog(), Model(), owner_notify=owner)
        fb = ms.Messenger(svc, "page-token", SECRET, VERIFY)
        chat = web_mod.WebChat(svc, messenger=fb)
        async with TestClient(TestServer(chat.app())) as client:
            # Meta's webhook check
            r = await client.get("/fb/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42"})
            assert r.status == 200 and await r.text() == "42"
            r = await client.get("/fb/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
            assert r.status == 403
            print("PASS the webhook answers Meta's check only with the right verify token")

            # unsigned or forged deliveries are refused
            body = event("PSID123", "多少钱")
            assert (await client.post("/fb/webhook", data=body, headers={"Content-Type": "application/json"})).status == 403
            assert (await client.post("/fb/webhook", data=body, headers={"X-Hub-Signature-256": "sha256=00"})).status == 403
            assert not sent

            # a real message: 200 at once, the AI's reply goes out through the Graph API
            r = await client.post("/fb/webhook", data=body, headers=signed(body))
            assert r.status == 200
            for _ in range(50):
                if sent: break
                await asyncio.sleep(0.02)
            assert sent == [{"recipient": {"id": "PSID123"}, "messaging_type": "RESPONSE",
                             "message": {"text": "国内 200 元搭建，之后每月 49 元～你是国内还是海外的店呀？"}}], sent
            assert owner.notices and "Facebook Messenger" in owner.notices[0], owner.notices
            print("PASS a Messenger message gets the AI's reply, and the owner is told it came from Facebook")

            # the same delivery again (Meta retries) is ignored; echoes of our own messages too
            await client.post("/fb/webhook", data=body, headers=signed(body))
            echo = event("PAGE", "hi", mid="m2", is_echo=True)
            await client.post("/fb/webhook", data=echo, headers=signed(echo))
            await asyncio.sleep(0.1)
            assert len(sent) == 1, sent
            print("PASS duplicate deliveries and echoes are ignored")

            # a photo is passed to the owner with a short note back to the customer
            pic = event("PSID123", mid="m3", attachments=[{"type": "image", "payload": {"url": "https://x/y.jpg"}}])
            await client.post("/fb/webhook", data=pic, headers=signed(pic))
            for _ in range(50):
                if len(sent) == 2: break
                await asyncio.sleep(0.02)
            assert len(sent) == 2 and sent[1]["recipient"]["id"] == "PSID123"

            # the owner answers from Telegram: it reaches the customer on Messenger
            key = await svc.deliver_owner_reply(owner.n, "我是本人，可以的！")
            assert key == ("facebook", "PSID123") and sent[-1]["message"]["text"] == "我是本人，可以的！"
            print("PASS attachments go to the owner, and the owner's Telegram reply reaches Messenger")

            # the privacy page Meta asks for
            r = await client.get("/privacy")
            page = await r.text()
            assert r.status == 200 and "Privacy Policy" in page and "隐私政策" in page and "{{" not in page
            assert "mailto:mingchengli465@gmail.com" in page
            print("PASS /privacy is served with the owner's email")
        await fb.close()

    # without the three settings Messenger stays off and the route doesn't exist
    for k in ("FB_PAGE_TOKEN", "FB_APP_SECRET", "FB_VERIFY_TOKEN"):
        os.environ.pop(k, None)
    assert ms.Messenger.from_env(svc) is None
    async with TestClient(TestServer(web_mod.WebChat(svc).app())) as client:
        assert (await client.get("/fb/webhook")).status == 404
    print("PASS Messenger is off until FB_PAGE_TOKEN, FB_APP_SECRET and FB_VERIFY_TOKEN are all set")

asyncio.run(main())
print("\nALL MESSENGER TESTS PASSED")
