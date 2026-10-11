"""IndexNow: the sitemap's pages go to Bing once per version of the sitemap, and a refusal is retried."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from aiohttp import web
from aiohttp.test_utils import TestServer

import indexnow

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{site}/</loc></url>
  <url><loc>{site}/guides/set-up-deepseek-api.html</loc></url>
</urlset>"""


async def main():
    posted, answer, pages = [], {"status": 202}, {"sitemap": SITEMAP}

    async def sitemap(request):
        return web.Response(text=pages["sitemap"].format(site=indexnow.SITE), content_type="application/xml")

    async def submit(request):
        posted.append(await request.json())
        return web.Response(status=answer["status"], text="")

    app = web.Application()
    app.router.add_get("/Claude-agent/sitemap.xml", sitemap)
    app.router.add_post("/indexnow", submit)
    async with TestServer(app) as server:
        indexnow.SITE = str(server.make_url("/Claude-agent")).rstrip("/")
        indexnow.ENDPOINT = str(server.make_url("/indexnow"))
        state = Path(tempfile.mkdtemp()) / "indexnow.json"

        answer["status"] = 403  # Bing hasn't seen the key file yet
        assert "失败（403）" in await indexnow.submit(state) and not state.exists(), "a refusal isn't remembered"
        answer["status"] = 202
        report = await indexnow.submit(state)
        assert "已把 2 个网页提交给 Bing" in report, report
        sent = posted[-1]
        assert sent["urlList"] == [indexnow.SITE + "/", indexnow.SITE + "/guides/set-up-deepseek-api.html"]
        assert sent["key"] == indexnow.KEY and sent["keyLocation"] == f"{indexnow.SITE}/{indexnow.KEY}.txt"
        assert sent["host"] == server.make_url("/").host + f":{server.port}"
        assert "没变" in await indexnow.submit(state) and len(posted) == 2, "the same sitemap isn't sent twice"
        pages["sitemap"] = SITEMAP.replace("</urlset>", "  <url><loc>{site}/guides/new.html</loc></url>\n</urlset>")
        assert "已把 3 个网页" in await indexnow.submit(state) and len(posted) == 3, "a new page sends the sitemap again"
        assert json.loads(state.read_text())["count"] == 3
    print("PASS the sitemap's pages go to Bing once per version; a refusal (key not published yet) is retried")


asyncio.run(main())
assert indexnow.urls_in("<loc> https://a.b/x </loc><loc>https://a.b/y</loc>") == ["https://a.b/x", "https://a.b/y"]
print("\nALL INDEXNOW TESTS PASSED")
