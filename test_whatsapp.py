"""WhatsApp (the official Business Platform): the webhook handshake, signed messages answered by the
AI once, the owner's Telegram reply going back out, attachments, and the 24-hour rule, against a fake
Graph API. No network, no keys: run `python test_whatsapp.py`.
"""
import asyncio, hashlib, hmac, json, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import web as web_mod
import whatsapp as wa

SECRET, VERIFY, TOKEN, NUMBER_ID = "app-secret", "my-verify", "wa-token", "PN1"
REPLY = "Hi! Setup is a one-off US$70, then US$9.9 a month. Is it for a shop in Singapore?"


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


def inbound(*messages, number_id=NUMBER_ID, statuses=None, name="Kyanne", wa_id="6591234567"):
    value = {"messaging_product": "whatsapp", "metadata": {"display_phone_number": "6580000000", "phone_number_id": number_id},
             "contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": list(messages)}
    if statuses:
        value = {"messaging_product": "whatsapp", "metadata": value["metadata"], "statuses": statuses}
    return json.dumps({"object": "whatsapp_business_account",
                       "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": value}]}]}).encode()


def text(body, mid="wamid.1", sender="6591234567"):
    return {"from": sender, "id": mid, "timestamp": "1760000000", "type": "text", "text": {"body": body}}


async def until(cond, tries=60):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def main():
    calls, answer = [], {"error": None}  # (method, path, auth, json)

    async def graph(request):
        path = request.match_info["tail"]
        body = await request.json() if request.can_read_body else None
        calls.append((request.method, path, request.headers.get("Authorization"), body))
        if answer["error"] and request.method == "POST":
            return web.json_response({"error": answer["error"]}, status=400)
        if path == NUMBER_ID:
            return web.json_response({"display_phone_number": "+65 8000 0000", "verified_name": "vinc AI studio", "id": NUMBER_ID})
        return web.json_response({"messaging_product": "whatsapp", "messages": [{"id": "wamid.out"}]})
    fake = web.Application(); fake.router.add_route("*", "/{tail:.*}", graph)

    async with TestServer(fake) as graph_server:
        wa.GRAPH_URL = str(graph_server.make_url("")).rstrip("/")
        tmp = pathlib.Path(tempfile.mkdtemp())
        owner, told = Owner(), []
        svc = cs.CustomerService(cs.Store(tmp / "cs.sqlite3"), cs.load_catalog(), Model(), owner_notify=owner)

        async def notify(t): told.append(t)
        bot = wa.WhatsApp(svc, TOKEN, NUMBER_ID, SECRET, VERIFY, state_path=tmp / "wa_state.json", notify=notify)
        chat = web_mod.WebChat(svc, whatsapp=bot)
        async with TestClient(TestServer(chat.app())) as client:
            # --- the webhook handshake and the start-up check ----------------------------------
            r = await client.get(wa.PATH, params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
            assert r.status == 403
            r = await client.get(wa.PATH, params={"hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42"})
            assert r.status == 200 and await r.text() == "42"
            assert await bot.connect() is True and bot.number == "+65 8000 0000"
            assert calls[-1][:3] == ("GET", NUMBER_ID, f"Bearer {TOKEN}") and "+65 8000 0000" in told[0]
            again = wa.WhatsApp(svc, TOKEN, NUMBER_ID, SECRET, VERIFY, state_path=tmp / "wa_state.json", notify=notify)
            assert await again.connect() and len(told) == 1, "a restart doesn't announce it again"
            await again.close()
            svc.register_channel(wa.CHANNEL, bot.send)
            print("PASS the webhook handshake checks the verify token; the number is checked once and announced once")

            # --- a customer's message gets the AI's reply, once -------------------------------
            calls.clear()
            body = inbound(text("How does the AI assistant work?"))
            assert (await client.post(wa.PATH, data=body, headers={"Content-Type": "application/json"})).status == 403
            assert (await client.post(wa.PATH, data=body, headers=signed(body))).status == 200
            await until(lambda: any(c[1] == f"{NUMBER_ID}/messages" for c in calls))
            sent = [c for c in calls if c[1] == f"{NUMBER_ID}/messages"]
            assert sent[0][2] == f"Bearer {TOKEN}"
            assert sent[0][3] == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "6591234567",
                                  "type": "text", "text": {"preview_url": False, "body": REPLY}}
            assert "WhatsApp" in owner.notices[0] and "Kyanne" in owner.notices[0], owner.notices
            await client.post(wa.PATH, data=body, headers=signed(body))  # Meta's retry
            receipts = inbound(statuses=[{"id": "wamid.out", "status": "delivered", "recipient_id": "6591234567"}])
            other = inbound(text("hello other number", mid="wamid.9"), number_id="PN2")
            for extra in (receipts, other):
                await client.post(wa.PATH, data=extra, headers=signed(extra))
            await asyncio.sleep(0.1)
            assert len([c for c in calls if c[1] == f"{NUMBER_ID}/messages"]) == 1, "retries, receipts and other numbers ignored"
            print("PASS a signed message gets the AI's reply once; retries, delivery receipts and other numbers are ignored")

            # --- the owner answers from Telegram -----------------------------------------------
            key = await svc.deliver_owner_reply(owner.n, "This is Vincent, happy to set up a free trial!")
            assert key == ("whatsapp", "6591234567") and calls[-1][3]["text"]["body"].startswith("This is Vincent")
            print("PASS the owner's reply to the Telegram notice goes back out on WhatsApp")

            # --- a photo: recorded, the owner told, the customer answered ------------------------
            calls.clear()
            photo = inbound({"from": "6591234567", "id": "wamid.2", "type": "image", "image": {"id": "MEDIA1"}})
            await client.post(wa.PATH, data=photo, headers=signed(photo))
            await until(lambda: any(c[1] == f"{NUMBER_ID}/messages" for c in calls))
            assert "[图片]" in svc.transcript(("whatsapp", "6591234567"))
            print("PASS a photo is recorded for the owner and the customer gets the usual answer")

            # --- after 24 hours WhatsApp refuses a reply: the owner hears why ---------------------
            answer["error"] = {"message": "Re-engagement message", "code": wa.WINDOW_CLOSED}
            try:
                await bot.send("6591234567", "Are you still interested?")
                raise AssertionError("must fail")
            except RuntimeError as exc:
                assert "24 小时" in str(exc), exc
            answer["error"] = None
            print("PASS a reply after the 24-hour window says why it can't go")
        await bot.close()

    # only with every variable set (FB_APP_SECRET stands in for the App Secret)
    for k in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_APP_SECRET", "WHATSAPP_VERIFY_TOKEN", "FB_APP_SECRET"):
        os.environ.pop(k, None)
    assert wa.WhatsApp.from_env(svc) is None
    os.environ.update({"WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "p", "WHATSAPP_VERIFY_TOKEN": "v"})
    assert wa.WhatsApp.from_env(svc) is None, "no App Secret, no webhook"
    os.environ["FB_APP_SECRET"] = "s"
    assert wa.WhatsApp.from_env(svc) is not None
    print("PASS WhatsApp only switches on with its token, number ID, verify token and the App Secret")


asyncio.run(main())
print("\nALL WHATSAPP TESTS PASSED")
