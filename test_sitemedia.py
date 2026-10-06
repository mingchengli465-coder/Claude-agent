"""The owner's whale video, sent to the bot, goes behind the homepage and every demo page.
No network: run `python test_sitemedia.py`.
"""
import asyncio, os, pathlib, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242",
                   "CS_DB_PATH": str(TMP / "cs.sqlite3")})
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import web
import bot


class File:
    def __init__(self, data): self.data = data
    async def download_as_bytearray(self): return bytearray(self.data)


class Bot:
    def __init__(self, files): self.files = files
    async def get_file(self, file_id): return File(self.files[file_id])


class Message:
    def __init__(self, caption="", photo=None, video=None, document=None):
        self.caption, self.photo, self.video, self.document = caption, photo or [], video, document
        self.replies, self.reply_to_message, self.text, self.chat_id = [], None, None, 424242
    async def reply_text(self, text, **k): self.replies.append(text)


def update(message):
    return types.SimpleNamespace(effective_message=message, effective_chat=types.SimpleNamespace(id=424242))


media = lambda fid, size=1000, mime=None: types.SimpleNamespace(file_id=fid, file_size=size, mime_type=mime)  # noqa: E731
ctx = types.SimpleNamespace(bot=Bot({"v": b"\x00\x00\x00 ftypmp42 fake video", "p": b"picture"}))
folder = web.site_media_dir()


async def pages(expect_video):
    svc = cs.CustomerService(cs.Store(TMP / "chats.sqlite3"), cs.load_catalog(), None)
    async with TestClient(TestServer(web.WebChat(svc, title="vinc").app())) as client:
        home = await (await client.get("/")).text()
        demo = await (await client.get("/demo-bnb-en")).text()
        src = 'data-src="/site-media/hero.mp4"' if expect_video else 'data-src=""'
        assert src in home and src in demo, src
        r = await client.get("/site-media/hero.mp4")
        assert r.status == (200 if expect_video else 404)
        assert (await client.get("/site-media/art-top.jpg")).status == 404

asyncio.run(pages(False))
m = Message(photo=[media("p")])
asyncio.run(bot.save_site_media(update(m), ctx))
assert not m.replies and not (folder / "hero.mp4").exists(), "pictures aren't used on the site"
m = Message(video=media("v", mime="video/mp4"))
asyncio.run(bot.save_site_media(update(m), ctx))
assert (folder / "hero.mp4").read_bytes().startswith(b"\x00\x00\x00 ftyp") and "首页背景视频（鲸鱼）" in m.replies[0]
m = Message(video=media("v", size=30 * 1024 * 1024))
asyncio.run(bot.save_site_media(update(m), ctx))
assert "超过 20MB" in m.replies[0]
bot.service = None
m = Message(document=media("v", mime="video/mp4"))
asyncio.run(bot.route_media(update(m), ctx))
assert m.replies and "已经换上网站了" in m.replies[0], "the owner's video that isn't a reply to a customer is for the site"
asyncio.run(pages(True))
print("PASS the owner's whale video, sent to the bot, plays behind the homepage and the demo pages")

sys.path.insert(0, str(pathlib.Path(__file__).parent / "tools"))
import build_pages
out = TMP / "_site"
build_pages.build(out)
index = (out / "index.html").read_text(encoding="utf-8")
assert f'data-src="{build_pages.API}/site-media/hero.mp4"' in index and not (out / "art").exists()
print("PASS GitHub Pages plays it from the chat server")
print("\nALL SITE MEDIA TESTS PASSED")
