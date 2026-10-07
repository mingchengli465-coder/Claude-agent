"""企业微信: callback verification, encrypted messages, AI replies, owner replies, retries and
failure notices, against a fake WeCom API. No network, no keys: run `python test_wecom.py`.
"""
import asyncio, json, os, pathlib, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import web as web_mod
import wecom as wc

TOKEN, AES, CORP = "QDG6eK", "jWmYm7qr5nMoAUwZRjGtBxmz3KA1tkAj3ykkR6q2B2C", "wx5823bf96d3bd56c7"
AGENT = 1000002
REPLY = "国内 200 元搭建，之后每月 49 元～你是国内还是海外的店呀？"
crypt = wc.WXCrypt(TOKEN, AES, CORP)


class Model:
    async def __call__(self, system, messages):
        return cs.Decision(reply=REPLY, summary="AI客服｜未说明｜未说明", intent="interested")


class Owner:
    def __init__(self): self.notices, self.n = [], 100
    async def __call__(self, text):
        self.n += 1; self.notices.append(text); return self.n


class FakeResp:
    def __init__(self, status, body): self.status, self._text = status, json.dumps(body)
    async def text(self): return self._text
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): return False


class FakeApi:
    """Stands in for the aiohttp session that talks to qyapi.weixin.qq.com."""
    closed = False

    def __init__(self, handler): self.handler, self.calls = handler, []

    def request(self, method, url, params=None, json=None):
        self.calls.append((method, url.rsplit("/cgi-bin/", 1)[-1] if "/cgi-bin/" in url else url, dict(params or {}), json))
        return FakeResp(*self.handler(method, url, dict(params or {}), json))

    async def close(self): self.closed = True

    def sent(self): return [c for c in self.calls if c[1].endswith("message/send")]


def callback(xml: str):
    """(query, body) the way WeCom delivers one message."""
    enc, ts, nonce = crypt.encrypt(xml), "1700000000", "abc123"
    body = (f"<xml><ToUserName><![CDATA[{CORP}]]></ToUserName><AgentID><![CDATA[{AGENT}]]></AgentID>"
            f"<Encrypt><![CDATA[{enc}]]></Encrypt></xml>").encode()
    return {"msg_signature": crypt.signature(ts, nonce, enc), "timestamp": ts, "nonce": nonce}, body


def message(user, content, msg_id, kind="text"):
    return (f"<xml><ToUserName><![CDATA[{CORP}]]></ToUserName><FromUserName><![CDATA[{user}]]></FromUserName>"
            f"<CreateTime>1700000000</CreateTime><MsgType><![CDATA[{kind}]]></MsgType>"
            f"<Content><![CDATA[{content}]]></Content><MsgId>{msg_id}</MsgId><AgentID>{AGENT}</AgentID></xml>")


async def until(cond, tries=100):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


async def main():
    # --- the encryption itself ---------------------------------------------------------------
    for text in ("hi", "你好，多少钱？" * 50, ""):
        assert crypt.decrypt(crypt.encrypt(text)) == text
    enc = crypt.encrypt("hello")
    assert crypt.verify(crypt.signature("1", "2", enc), "1", "2", enc)
    assert not crypt.verify(crypt.signature("1", "2", enc), "1", "3", enc) and not crypt.verify("", "1", "2", enc)
    for decrypt, junk in ((wc.WXCrypt(TOKEN, AES, "wxOTHER").decrypt, enc), (crypt.decrypt, "not base64!!"),
                          (crypt.decrypt, "QUJD"), (crypt.decrypt, "")):
        try:
            decrypt(junk)
            raise AssertionError("must be rejected")
        except ValueError:
            pass
    for bad_key in ("short", "!" * 43):
        try:
            wc.WXCrypt(TOKEN, bad_key, CORP)
            raise AssertionError("a bad EncodingAESKey must be rejected")
        except ValueError:
            pass
    assert wc.extract_encrypt(f"<xml><Encrypt><![CDATA[{enc}]]></Encrypt></xml>".encode()) == enc
    assert wc.extract_encrypt(f"<xml><Encrypt>{enc}</Encrypt></xml>".encode()) == enc and wc.extract_encrypt(b"<xml/>") == ""
    parts = wc.split_bytes("你好吗？" * 800)
    assert "".join(parts) == "你好吗？" * 800 and all(len(p.encode()) <= wc.MAX_BYTES for p in parts) and len(parts) == 5
    print("PASS encryption: round trip, tampering, other corp id, junk and bad keys; Encrypt extraction; byte splitting")

    tmp = pathlib.Path(tempfile.mkdtemp())
    owner, told = Owner(), []
    svc = cs.CustomerService(cs.Store(tmp / "cs.sqlite3"), cs.load_catalog(), Model(), owner_notify=owner)
    state = {"tokens": 0, "expire_next": False, "invalid": ""}

    def api(method, url, params, body):
        if url.endswith("/gettoken"):
            assert params == {"corpid": CORP, "corpsecret": "sec"}
            state["tokens"] += 1
            return 200, {"errcode": 0, "errmsg": "ok", "access_token": f"tok{state['tokens']}", "expires_in": 7200}
        assert url.endswith("/message/send")
        if state["expire_next"]:
            state["expire_next"] = False
            return 200, {"errcode": 42001, "errmsg": "access_token expired"}
        assert params["access_token"] == f"tok{state['tokens']}"
        return 200, {"errcode": 0, "errmsg": "ok", "invaliduser": state["invalid"]}

    session = FakeApi(api)

    async def notify(text): told.append(text)

    app = wc.WeCom(svc, CORP, AGENT, "sec", TOKEN, AES, callback_url="https://bot.example/wecom/callback",
                   session=session, notify=notify)
    assert svc.senders["wecom"] == app.send
    chat = web_mod.WebChat(svc, wecom=app)
    async with TestClient(TestServer(chat.app())) as client:
        # --- saving the callback URL in the WeCom console -----------------------------------------
        echo = crypt.encrypt("1616140317555161061")
        good = {"msg_signature": crypt.signature("5", "6", echo), "timestamp": "5", "nonce": "6", "echostr": echo}
        r = await client.get("/wecom/callback", params=good)
        assert r.status == 200 and await r.text() == "1616140317555161061"
        assert (await client.get("/wecom/callback", params={**good, "msg_signature": "0" * 40})).status == 403
        assert (await client.get("/wecom/callback")).status == 403
        foreign = wc.WXCrypt(TOKEN, AES, "wxOTHER").encrypt("x")
        r = await client.get("/wecom/callback", params={
            "msg_signature": crypt.signature("5", "6", foreign), "timestamp": "5", "nonce": "6", "echostr": foreign})
        assert r.status == 403
        print("PASS callback verification: echostr comes back decrypted; bad signature, nothing, other corp id -> 403")

        assert await app.connect() is True and app.connected and state["tokens"] == 1
        print("PASS connect() checks the credentials with one access token")

        # --- a customer writes ------------------------------------------------------------------------
        query, body = callback(message("ZhangSan", "多少钱", "m1"))
        r = await client.post("/wecom/callback", params=query, data=body)
        assert r.status == 200 and await r.text() == "success"
        await until(lambda: session.sent())
        assert session.sent()[0][3] == {"touser": "ZhangSan", "msgtype": "text", "agentid": AGENT,
                                        "text": {"content": REPLY}}, session.sent()[0]
        assert state["tokens"] == 1, "the access token is kept between messages"
        assert "企业微信" in owner.notices[0] and "wecom:ZhangSan" in owner.notices[0], owner.notices[0]
        print("PASS a signed message gets the AI reply once; the owner is told, with the chat id wecom:ZhangSan")

        # WeCom's retry, events, bad signatures, undecryptable bodies
        await client.post("/wecom/callback", params=query, data=body)
        q2, b2 = callback("<xml><FromUserName>ZhangSan</FromUserName><MsgType>event</MsgType><Event>enter_agent</Event></xml>")
        await client.post("/wecom/callback", params=q2, data=b2)
        q3, b3 = callback(message("Evil", "hi", "m9"))
        assert (await client.post("/wecom/callback", params={**q3, "msg_signature": "0" * 40}, data=b3)).status == 403
        assert (await client.post("/wecom/callback", data=b"<xml></xml>")).status == 403
        garbled = {"msg_signature": crypt.signature("1", "2", "AAAA"), "timestamp": "1", "nonce": "2"}
        assert (await client.post("/wecom/callback", params=garbled, data=b"<xml><Encrypt>AAAA</Encrypt></xml>")).status == 400
        await asyncio.sleep(0.1)
        assert len(session.sent()) == 1
        print("PASS the retry, event messages, bad signatures and undecryptable bodies send nothing")

        # --- the owner answers from Telegram -------------------------------------------------------------
        key = await svc.deliver_owner_reply(owner.n, "我是本人，可以的！")
        assert key == ("wecom", "ZhangSan") and session.sent()[-1][3]["text"]["content"] == "我是本人，可以的！"
        before = len(session.sent())
        await app.send("ZhangSan", "长" * 1500)
        pieces = session.sent()[before:]
        assert len(pieces) == 3 and all(len(c[3]["text"]["content"].encode()) <= 2000 for c in pieces)
        print("PASS the owner's Telegram reply reaches the WeCom user; long text goes out in pieces under 2000 bytes")

        # --- API errors -------------------------------------------------------------------------------------
        state["expire_next"] = True
        tokens, before = state["tokens"], len(session.sent())
        await app.send("ZhangSan", "again")
        assert state["tokens"] == tokens + 1 and len(session.sent()) == before + 2
        assert session.sent()[-1][2]["access_token"] == f"tok{state['tokens']}"
        state["invalid"] = "Ghost"
        try:
            await app.send("Ghost", "hello")
            raise AssertionError("delivering to nobody must raise")
        except RuntimeError as exc:
            assert "Ghost" in str(exc)
        state["invalid"] = ""
        print("PASS an expired access token is replaced and the send retried; errcode 0 with invaliduser is an error")

        # --- pictures and files -----------------------------------------------------------------------------
        before = len(session.sent())
        q4, b4 = callback(message("LiSi", "", "m20", kind="image"))
        await client.post("/wecom/callback", params=q4, data=b4)
        await until(lambda: len(session.sent()) > before)
        assert session.sent()[-1][3]["touser"] == "LiSi" and "收到文件" in session.sent()[-1][3]["text"]["content"]
        assert any("发来文件" in n and "wecom:LiSi" in n for n in owner.notices)
        print("PASS a picture is recorded, the owner is told, the customer gets the standard reply")

    # --- a refused send reaches the owner once ----------------------------------------------------------------
    def refuse(method, url, params, body):
        if url.endswith("/gettoken"):
            return 200, {"errcode": 0, "access_token": "t", "expires_in": 7200}
        return 200, {"errcode": 60020, "errmsg": "not allow to access from your ip, hint: [1], from ip: 1.2.3.4"}

    blocked = wc.WeCom(svc, CORP, AGENT, "sec", TOKEN, AES, session=FakeApi(refuse), notify=notify)
    told.clear()
    async with TestClient(TestServer(web_mod.WebChat(svc, wecom=blocked).app())) as client:
        for n, text in enumerate(("你好", "还在吗")):
            q, b = callback(message("Wang", text, f"m3{n}"))
            await client.post("/wecom/callback", params=q, data=b)
            await asyncio.sleep(0.15)
    assert len(told) == 1 and "60020" in told[0] and "1.2.3.4" in told[0] and "企业可信IP" in told[0], told
    svc.register_channel("wecom", app.send)
    print("PASS a refused send (IP not trusted) is reported to the owner once, with the IP and what to do")

    told.clear()
    wrong = wc.WeCom(svc, CORP, AGENT, "sec", TOKEN, AES, notify=notify,
                     session=FakeApi(lambda *a: (200, {"errcode": 40013, "errmsg": "invalid corpid"})))
    assert await wrong.connect() is False and not wrong.connected and "WECOM_CORP_ID" in told[0]
    print("PASS bad credentials at startup are reported to the owner; the bot keeps running")

    await app.close()
    assert session.closed

    # --- configuration ---------------------------------------------------------------------------------------------
    names = ("WECOM_CORP_ID", "WECOM_AGENT_ID", "WECOM_SECRET", "WECOM_TOKEN", "WECOM_AES_KEY", "WECOM_CALLBACK_URL",
             "PUBLIC_BASE_URL", "RAILWAY_PUBLIC_DOMAIN")
    for k in names:
        os.environ.pop(k, None)
    assert wc.WeCom.from_env(svc) is None
    os.environ.update(WECOM_CORP_ID=CORP, WECOM_SECRET="s")
    assert wc.WeCom.from_env(svc) is None, "partly filled in is still off"
    os.environ.update(WECOM_AGENT_ID="abc", WECOM_TOKEN=TOKEN, WECOM_AES_KEY=AES)
    assert wc.WeCom.from_env(svc) is None, "the AgentId must be a number"
    os.environ.update(WECOM_AGENT_ID=str(AGENT), WECOM_AES_KEY="tooshort")
    assert wc.WeCom.from_env(svc) is None, "a mistyped EncodingAESKey must not stop the bot"
    os.environ.update(WECOM_AES_KEY=AES, RAILWAY_PUBLIC_DOMAIN="bot.up.railway.app")
    env_app = wc.WeCom.from_env(svc)
    assert env_app is not None and env_app.agent_id == AGENT
    assert env_app.callback_url == "https://bot.up.railway.app/wecom/callback"
    os.environ["PUBLIC_BASE_URL"] = "https://shop.example/"
    assert wc.WeCom.from_env(svc).callback_url == "https://shop.example/wecom/callback"
    for k in names:
        os.environ.pop(k, None)
    svc.register_channel("wecom", app.send)

    # without it there is no route, and Telegram's channel is not touched
    async with TestClient(TestServer(web_mod.WebChat(svc).app())) as client:
        assert (await client.get("/wecom/callback")).status == 404
    assert svc.senders.get("telegram") is None or svc.senders["telegram"] != app.send
    print("PASS 企业微信 is off until all five variables are valid; no route, no change to the other channels")

asyncio.run(main())
print("\nALL 企业微信 TESTS PASSED")
