"""企业微信智能机器人 (long connection): subscribe, AI replies as a stream, group @-mentions, owner
replies, a refused Secret, reconnecting after a drop, against a fake WeCom WebSocket server.
No network, no keys: run `python test_wecom_bot.py`.
"""
import asyncio, json, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

import customer_service as cs
import wecom_bot as wb

REPLY = "国内 200 元搭建，之后每月 49 元～你是国内还是海外的店呀？"
BOT, SECRET = "aib-test-bot", "s3cr3t"


class Model:
    def __init__(self): self.calls = 0
    async def __call__(self, system, messages):
        self.calls += 1
        return cs.Decision(reply=REPLY, summary="AI客服｜未说明｜未说明", intent="interested")


class Owner:
    def __init__(self): self.notices, self.n = [], 100
    async def __call__(self, text):
        self.n += 1; self.notices.append(text); return self.n


class FakeWeCom:
    """The WeCom side of the long connection: checks the subscription, acknowledges every frame."""

    def __init__(self, secret=SECRET):
        self.secret, self.frames, self.sockets, self.connections = secret, [], [], 0
        self.ready = asyncio.Event()

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            frame = json.loads(msg.data)
            self.frames.append(frame)
            rid = frame["headers"]["req_id"]
            if frame["cmd"] == "aibot_subscribe":
                ok = frame["body"] == {"bot_id": BOT, "secret": self.secret}
                await ws.send_str(json.dumps({"headers": {"req_id": rid}, "errcode": 0 if ok else 40058,
                                              "errmsg": "ok" if ok else "invalid secret"}))
                if not ok:
                    await ws.close()
                    break
                self.sockets.append(ws)
                self.ready.set()
            else:
                await ws.send_str(json.dumps({"headers": {"req_id": rid}, "errcode": 0, "errmsg": "ok"}))
        return ws

    async def push(self, body, rid="cb-1"):
        await self.sockets[-1].send_str(json.dumps({"cmd": "aibot_msg_callback", "headers": {"req_id": rid},
                                                     "body": body}, ensure_ascii=False))

    def sent(self, cmd): return [f for f in self.frames if f["cmd"] == cmd]


def text_msg(user, content, msgid, group=""):
    body = {"msgid": msgid, "aibotid": BOT, "chattype": "group" if group else "single",
            "from": {"userid": user}, "msgtype": "text", "text": {"content": content}}
    if group:
        body["chatid"] = group
    return body


async def until(check, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def fresh(tmp):
    store = cs.Store(pathlib.Path(tmp) / f"cs{os.urandom(3).hex()}.sqlite3")
    owner, model = Owner(), Model()
    return cs.CustomerService(store, "AI 客服搭建：国内 200 元", model, owner_notify=owner), owner, model


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        # --- off unless both variables are set -------------------------------------------
        service, owner, model = fresh(tmp)
        for k in ("WECOM_BOT_ID", "WECOM_BOT_SECRET", "WECOM_BOT_WS_URL"):
            os.environ.pop(k, None)
        assert wb.WeComBot.from_env(service) is None
        os.environ["WECOM_BOT_ID"] = BOT
        assert wb.WeComBot.from_env(service) is None, "the Secret too"
        print("PASS off unless WECOM_BOT_ID and WECOM_BOT_SECRET are both set")

        # --- subscribe, then a one-to-one message gets the AI reply as a stream -------------
        fake = FakeWeCom()
        app = web.Application(); app.router.add_get("/", fake.handler)
        server = TestServer(app); await server.start_server()
        os.environ["WECOM_BOT_SECRET"] = SECRET
        os.environ["WECOM_BOT_WS_URL"] = str(server.make_url("/"))
        bot = wb.WeComBot.from_env(service, notify=owner)
        assert bot is not None and "wecombot" in service.senders
        bot.start()
        await asyncio.wait_for(fake.ready.wait(), 5)
        await until(lambda: bot.connected)
        assert fake.sent("aibot_subscribe")[0]["body"] == {"bot_id": BOT, "secret": SECRET}
        print("PASS subscribes with the BotID and Secret")

        await fake.push(text_msg("zhangsan", "做一个AI客服多少钱", "m1"), rid="cb-1")
        await until(lambda: len(fake.sent("aibot_respond_msg")) == 2)
        first, last = fake.sent("aibot_respond_msg")
        assert first["headers"]["req_id"] == last["headers"]["req_id"] == "cb-1", "answers on the message's req_id"
        assert first["body"]["stream"]["content"] == wb.THINKING and not first["body"]["stream"]["finish"]
        assert last["body"]["stream"] == {"id": first["body"]["stream"]["id"], "finish": True, "content": REPLY}
        assert [m["role"] for m in service.store.messages_after("wecombot", "zhangsan", 0)] == ["customer", "assistant"]
        print("PASS a message gets a typing placeholder, then the AI's reply on the same stream")

        # WeCom delivering the same message twice: answered once
        await fake.push(text_msg("zhangsan", "做一个AI客服多少钱", "m1"), rid="cb-1b")
        await asyncio.sleep(0.3)
        assert len(fake.sent("aibot_respond_msg")) == 2 and model.calls == 1
        print("PASS a message delivered twice is answered once")

        # --- a group: the @mention is dropped, the conversation is the group ------------------
        await fake.push(text_msg("lisi", "@vinc客服 价格多少", "m2", group="wrGroup1"), rid="cb-2")
        await until(lambda: len(fake.sent("aibot_respond_msg")) == 4)
        assert service.store.messages_after("wecombot", "wrGroup1", 0)[0]["text"] == "价格多少"
        print("PASS in a group the @mention is dropped and the group is one conversation")

        # --- a picture: recorded and passed to the owner -----------------------------------
        await fake.push({"msgid": "m3", "aibotid": BOT, "chattype": "single", "from": {"userid": "zhangsan"},
                         "msgtype": "image", "image": {"url": "https://example/x", "aeskey": "k"}}, rid="cb-3")
        await until(lambda: len(fake.sent("aibot_respond_msg")) == 6)
        assert fake.sent("aibot_respond_msg")[-1]["body"]["stream"]["content"] == cs.ATTACHMENT_TEXT
        assert any("企业微信机器人" in n for n in owner.notices), owner.notices
        print("PASS a picture is passed to the owner and the customer is told")

        # --- the owner replies from Telegram ----------------------------------------------------
        assert await service.deliver_owner_reply(owner.n, "我是本人，给你算个优惠价") == ("wecombot", "zhangsan")  # the picture notice
        sent = fake.sent("aibot_send_msg")[-1]["body"]
        assert sent == {"chatid": "zhangsan", "msgtype": "markdown", "markdown": {"content": "我是本人，给你算个优惠价"}}
        print("PASS owner replies from Telegram go out with aibot_send_msg")

        # --- the connection drops: it comes back by itself ------------------------------------
        await fake.sockets[-1].close()
        await until(lambda: fake.connections == 2 and bot.connected, timeout=8)
        await fake.push(text_msg("zhangsan", "还在吗", "m4"), rid="cb-4")
        await until(lambda: len(fake.sent("aibot_respond_msg")) == 8)
        print("PASS after a drop it reconnects and answers again")
        await bot.close()
        await server.close()

        # --- a wrong Secret: the owner hears about it, nothing crashes -----------------------------
        service2, owner2, _ = fresh(tmp)
        fake2 = FakeWeCom(secret="the-right-one")
        app2 = web.Application(); app2.router.add_get("/", fake2.handler)
        server2 = TestServer(app2); await server2.start_server()
        os.environ["WECOM_BOT_WS_URL"] = str(server2.make_url("/"))
        bot2 = wb.WeComBot.from_env(service2, notify=owner2)
        bot2.start()
        await until(lambda: owner2.notices)
        assert "认证失败" in owner2.notices[0] and not bot2.connected
        await bot2.close()
        await server2.close()
        print("PASS a refused Secret is reported to the owner and the bot keeps running")

        # --- message shapes ---------------------------------------------------------------------
        assert wb.message_text({"msgtype": "voice", "voice": {"content": "多少钱"}}) == ("多少钱", "")
        assert wb.message_text({"msgtype": "mixed", "mixed": {"msg_item": [
            {"msgtype": "text", "text": {"content": "看这个"}}, {"msgtype": "image", "image": {}}]}}) == ("看这个", "")
        assert wb.message_text({"msgtype": "file", "file": {}}) == ("", "file")
        assert wb.message_text({"msgtype": "sticker"}) == ("", "")
        print("PASS voice (already transcribed), mixed, file and unknown messages")
        for k in ("WECOM_BOT_ID", "WECOM_BOT_SECRET", "WECOM_BOT_WS_URL"):
            os.environ.pop(k, None)


asyncio.run(main())
print("ALL PASS")
