"""/live: the latest picture from a public camera in San Francisco and Singapore, against a fake of
each city's camera list; and the owner-only Telegram command."""

import asyncio
import datetime as dt
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")
os.environ.setdefault("OPENROUTER_API_KEY", "x")
os.environ["OWNER_CHAT_ID"] = "424242"

from aiohttp import web
from aiohttp.test_utils import TestServer

import livecam

JPEG = b"\xff\xd8\xff\xe0" + b"fake jpeg" * 50


def caltrans(base):
    def cam(name, url, service="true"):
        return {"cctv": {"index": "1", "location": {"district": "4", "locationName": name, "nearbyPlace": "San Francisco"},
                         "inService": service, "imageData": {"imageDescription": "", "static": {
                             "currentImageUpdateFrequency": "2", "currentImageURL": url}}}}
    return {"data": [cam("I-80 : Bay Bridge Toll Plaza", base + "/sf/closed.jpg", "false"),
                     cam("US-101 : Army St", base + "/sf/army.jpg"),
                     cam("I-80 : SFOBB West Span Tower", base + "/sf/bridge.jpg")]}


def singapore(base):
    return {"items": [{"timestamp": "2026-10-13T19:05:00+08:00", "cameras": [
        {"camera_id": "1001", "image": base + "/sg/1001.jpg"},
        {"camera_id": "1502", "image": base + "/sg/1502.jpg"},
        {"camera_id": "9999", "image": ""}]}]}


async def main():
    asked = []

    async def listing(request):
        base = str(request.url.origin())
        return web.json_response(caltrans(base) if request.path == "/caltrans.json" else singapore(base))

    async def image(request):
        asked.append(request.path)
        if request.path.endswith("broken.jpg"):
            return web.Response(text="<html>not a picture</html>", content_type="text/html")
        return web.Response(body=JPEG, content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/caltrans.json", listing)
    app.router.add_get("/sg.json", listing)
    app.router.add_get("/{city}/{name}", image)
    async with TestServer(app) as server:
        livecam.CALTRANS_LIST = str(server.make_url("/caltrans.json"))
        livecam.SG_LIST = str(server.make_url("/sg.json"))
        now = dt.datetime(2026, 10, 13, 11, 5, tzinfo=dt.timezone.utc)

        shot = await livecam.snapshot("sf", now=now)
        assert shot.jpeg == JPEG and asked[-1] == "/sf/bridge.jpg", asked
        assert "旧金山" in shot.caption and "当地时间 04:05" in shot.caption and "夜里" in shot.caption
        assert "北京时间 19:05" in shot.caption and "SFOBB West Span Tower" in shot.caption, shot.caption
        print("PASS San Francisco: the Bay Bridge camera in service, with the time there and in Beijing")

        shot = await livecam.snapshot("sg", now=now)
        assert asked[-1] == "/sg/1502.jpg" and "摄像头 1502" in shot.caption and "当地时间 19:05" in shot.caption
        print("PASS Singapore: the camera nearest Marina Bay that has a picture")

        livecam.SG_LIST = str(server.make_url("/missing.json"))
        try:
            await livecam.snapshot("sg")
            raise AssertionError("must fail")
        except livecam.LiveCamError as exc:
            assert "返回 404" in str(exc), exc
        livecam.SG_LIST = str(server.make_url("/sg.json"))
        print("PASS a camera list that isn't there is a readable error")

        # the Telegram command: owner only, one city or both
        import bot

        class Msg:
            def __init__(self): self.texts, self.photos = [], []
            async def reply_text(self, text, **k): self.texts.append(text)
            async def reply_photo(self, photo=None, caption="", **k): self.photos.append((photo, caption))

        class TG:
            async def send_chat_action(self, **k): pass

        def update(chat):
            return types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=chat), effective_message=Msg())

        u = update(555)
        await bot.live_command(u, types.SimpleNamespace(args=[], bot=TG()))
        assert u.effective_message.photos == [] and u.effective_message.texts == [bot.XHS_DENIED_TEXT]
        u = update(424242)
        await bot.live_command(u, types.SimpleNamespace(args=[], bot=TG()))
        assert [c.split("，")[0] for _, c in u.effective_message.photos] == ["📷 旧金山 · 海湾大桥", "📷 新加坡"]
        u = update(424242)
        await bot.live_command(u, types.SimpleNamespace(args=["新加坡"], bot=TG()))
        assert len(u.effective_message.photos) == 1 and "新加坡" in u.effective_message.photos[0][1]
        u = update(424242)
        await bot.live_command(u, types.SimpleNamespace(args=["火星"], bot=TG()))
        assert u.effective_message.texts == [bot.LIVE_USAGE] and not u.effective_message.photos
        livecam.CALTRANS_LIST = str(server.make_url("/nothing.json"))
        u = update(424242)
        await bot.live_command(u, types.SimpleNamespace(args=["旧金山"], bot=TG()))
        assert "打不开" in u.effective_message.texts[0] and not u.effective_message.photos
        print("PASS /live: owner only; both cities or one; a camera that fails says so")


asyncio.run(main())
assert livecam.caltrans_cameras({"weird": []}) == []
print("\nALL LIVE CAMERA TESTS PASSED")
