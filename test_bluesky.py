"""Bluesky posting against a fake server. No network, no keys: run `python test_bluesky.py`."""
import asyncio, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp import web
from aiohttp.test_utils import TestServer

import bluesky as bs

# --- text helpers ---------------------------------------------------------------------------
assert bs.fit("short") == "short"
long = "word " * 80
cut = bs.fit(long)
assert len(cut) <= bs.MAX_GRAPHEMES and cut.endswith("…") and not cut.endswith(" …"), cut
text = "Try it 👉 https://example.com/a?b=1. Or 中文 https://t.me/emilyhanbot"
facets = bs.link_facets(text)
raw = text.encode()
assert [raw[f["index"]["byteStart"]:f["index"]["byteEnd"]].decode() for f in facets] == \
    ["https://example.com/a?b=1", "https://t.me/emilyhanbot"], "byte offsets, trailing dot dropped"
print("PASS long posts are cut at a word; links become clickable with UTF-8 byte offsets")


async def main():
    seen = []

    async def session(request):
        body = await request.json()
        if body["password"] != "app-pass":
            return web.json_response({"error": "AuthenticationRequired", "message": "Invalid identifier or password"}, status=401)
        return web.json_response({"did": "did:plc:abc", "handle": "vinc.bsky.social", "accessJwt": "jwt1"})

    async def create(request):
        assert request.headers["Authorization"] == "Bearer jwt1"
        seen.append(await request.json())
        return web.json_response({"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "cid": "c"})

    app = web.Application()
    app.router.add_post("/xrpc/com.atproto.server.createSession", session)
    app.router.add_post("/xrpc/com.atproto.repo.createRecord", create)
    async with TestServer(app) as server:
        bs.SERVICE = str(server.make_url("")).rstrip("/")
        bs.HANDLE, bs.APP_PASSWORD = "", ""
        assert not bs.enabled()
        try:
            await bs.post("hi"); raise AssertionError("must refuse without settings")
        except bs.BlueskyError:
            pass
        bs.HANDLE, bs.APP_PASSWORD = "vinc.bsky.social", "app-pass"
        url = await bs.post("Small shops lose customers at night. An AI assistant answers in seconds.")
        assert url == "https://bsky.app/profile/vinc.bsky.social/post/3kxyz", url
        rec = seen[0]["record"]
        assert seen[0]["repo"] == "did:plc:abc" and seen[0]["collection"] == "app.bsky.feed.post"
        assert rec["langs"] == ["en"] and rec["createdAt"].endswith("Z")
        assert rec["embed"]["external"]["uri"].endswith("?from=bsky"), "a link card to the website"
        print("PASS a post goes out with a link card to the website and comes back as a bsky.app link")

        bs.APP_PASSWORD = "wrong"
        try:
            await bs.post("hi"); raise AssertionError("bad password must raise")
        except bs.BlueskyError as exc:
            assert "Invalid identifier or password" in str(exc)
        print("PASS a wrong app password is reported clearly")

asyncio.run(main())
print("\nALL BLUESKY TESTS PASSED")
