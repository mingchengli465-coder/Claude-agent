"""The owner's whale video and dead-wood pictures, sent to the bot, go onto the site.
No network: run `python test_sitemedia.py`.
"""
import asyncio, io, os, pathlib, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TMP = pathlib.Path(tempfile.mkdtemp())
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242",
                   "CS_DB_PATH": str(TMP / "cs.sqlite3")})
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import customer_service as cs
import sitemedia
import web

ART = pathlib.Path(__file__).parent / "web_static" / "art"
dead, bloom = (ART / "wood-top.jpg").read_bytes(), (ART / "wood-bloom.jpg").read_bytes()

# --- where each one goes ----------------------------------------------------------------------
assert sitemedia.slot_for(True, "") == "hero.mp4"
assert sitemedia.slot_for(False, "枯木生春") == "art-bottom.jpg", "the bloom's name contains 枯木"
assert sitemedia.slot_for(False, "枯木") == "art-top.jpg" and sitemedia.slot_for(False, "上层") == "art-top.jpg"
assert sitemedia.slot_for(False, "底层.jpg") == "art-bottom.jpg"
assert sitemedia.slot_for(False, "", bloom) == "art-bottom.jpg", "green: the moss"
assert sitemedia.slot_for(False, "", dead) == "art-top.jpg", "embers: the dead wood"
big = io.BytesIO(); Image.new("RGB", (5000, 3000), (20, 20, 20)).save(big, "PNG")
assert Image.open(io.BytesIO(sitemedia.as_jpeg(big.getvalue()))).size == (2560, 1536)
print("PASS a video is the whale; a picture goes where its caption, or its colour, says")


# --- the bot keeps what the owner sends ----------------------------------------------------------
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
ctx = types.SimpleNamespace(bot=Bot({"m": bloom, "d": dead, "v": b"\x00\x00\x00 ftypmp42 fake video"}))
folder = web.site_media_dir()

m = Message(caption="苔藓", photo=[media("x"), media("m")])
asyncio.run(bot.save_site_media(update(m), ctx))
assert (folder / "art-bottom.jpg").is_file() and "第二页底层" in m.replies[0] and "还差" in m.replies[0]
m = Message(document=media("d", mime="image/jpeg"))
asyncio.run(bot.save_site_media(update(m), ctx))
assert (folder / "art-top.jpg").is_file() and "第二页上层（枯木）" in m.replies[0]
m = Message(video=media("v", mime="video/mp4"))
asyncio.run(bot.save_site_media(update(m), ctx))
assert (folder / "hero.mp4").read_bytes().startswith(b"\x00\x00\x00 ftyp") and "三个素材都齐了" in m.replies[0]
m = Message(video=media("v", size=30 * 1024 * 1024))
asyncio.run(bot.save_site_media(update(m), ctx))
assert "超过 20MB" in m.replies[0]
# the owner's attachment that isn't a reply to a customer is for the site
bot.service = None
m = Message(caption="枯木生春", photo=[media("m")])
asyncio.run(bot.route_media(update(m), ctx))
assert m.replies and "第二页底层" in m.replies[0]
print("PASS the owner's video and pictures, sent to the bot, are kept for the site; a 30MB file is explained")


# --- the pages use them ------------------------------------------------------------------------------
async def pages():
    svc = cs.CustomerService(cs.Store(TMP / "chats.sqlite3"), cs.load_catalog(), None)
    async with TestClient(TestServer(web.WebChat(svc, title="vinc").app())) as client:
        home = await (await client.get("/")).text()
        assert 'data-src="/site-media/hero.mp4"' in home
        assert "url('/site-media/art-top.jpg'), url('art/wood-top.jpg')" in home, "the owner's picture, the drawn one behind"
        assert "url('/site-media/art-bottom.jpg'), url('art/wood-bloom.jpg')" in home
        r = await client.get("/site-media/hero.mp4")
        assert r.status == 200 and r.headers["Content-Type"] == "video/mp4"
        assert (await client.get("/site-media/secret.txt")).status == 404
        demo = await (await client.get("/demo-bnb-en")).text()
        assert 'data-src="/site-media/hero.mp4"' in demo, "the demo pages use them too"

asyncio.run(pages())
sys.path.insert(0, str(pathlib.Path(__file__).parent / "tools"))
import build_pages
out = TMP / "_site"
build_pages.build(out)
index = (out / "index.html").read_text(encoding="utf-8")
assert f'data-src="{build_pages.API}/site-media/hero.mp4"' in index and (out / "art" / "wood-top.jpg").is_file()
assert (out / "about.html").is_file() and "Claude Code" in (out / "about.html").read_text(encoding="utf-8")
print("PASS the homepage and demo pages show the owner's video and pictures, on Railway and GitHub Pages")
print("\nALL SITE MEDIA TESTS PASSED")
