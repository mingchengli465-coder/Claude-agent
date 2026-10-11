"""Tell Bing when the site's pages change (IndexNow), so the guides and demos get found by search.

Google can't be told without a Search Console account the owner signs in to; Bing and the other
IndexNow engines (Yandex, Seznam, Naver) can, with a key file published on the site itself
(tools/build_pages.py writes it). Bing's index also feeds DuckDuckGo, Yahoo and ChatGPT's search.

    submit(state_path) -> str    sends the sitemap's pages if the sitemap changed since last time

Only the main bot sends (INDEXNOW=on in Railway), not the other bots that share this code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import ClientSession, ClientTimeout

SITE = os.environ.get("PAGES_SITE_URL", "https://mingchengli465-coder.github.io/Claude-agent").rstrip("/")
# Not a secret: IndexNow checks that the same key is published on the site.
KEY = os.environ.get("INDEXNOW_KEY", "5f1e1de859211f3217ae8a0420de3773")
ENDPOINT = os.environ.get("INDEXNOW_ENDPOINT", "https://api.indexnow.org/indexnow")
ON = os.environ.get("INDEXNOW", "").lower() in ("on", "true", "1")


def key_file_name() -> str:
    return f"{KEY}.txt"


def urls_in(sitemap: str) -> list[str]:
    return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", sitemap)


def payload(urls: list[str]) -> dict:
    return {"host": urlparse(SITE).netloc, "key": KEY, "keyLocation": f"{SITE}/{key_file_name()}", "urlList": urls}


async def submit(state_path: Path, session: ClientSession | None = None) -> str:
    """Send every page in the sitemap, once per version of the sitemap. A line for the log."""
    own = session is None
    session = session or ClientSession(timeout=ClientTimeout(total=60))
    try:
        async with session.get(f"{SITE}/sitemap.xml") as r:
            if r.status != 200:
                return f"sitemap 打不开（{r.status}），下次再提交"
            sitemap = await r.text()
        urls = urls_in(sitemap)
        if not urls:
            return "sitemap 里没有网页"
        digest = hashlib.sha256("\n".join(sorted(urls)).encode()).hexdigest()
        try:
            last = json.loads(state_path.read_text(encoding="utf-8")).get("sitemap", "")
        except (OSError, ValueError):
            last = ""
        if digest == last:
            return "网页没变，不用重新提交"
        async with session.post(ENDPOINT, json=payload(urls)) as r:
            body = (await r.text())[:200]
            if r.status not in (200, 202):
                # 403: Bing hasn't seen the key file yet (the site is still publishing); try again later
                return f"提交给 Bing 失败（{r.status}）：{body}"
        state_path.write_text(json.dumps({"sitemap": digest, "count": len(urls)}), encoding="utf-8")
        return f"已把 {len(urls)} 个网页提交给 Bing（IndexNow）"
    finally:
        if own:
            await session.close()
