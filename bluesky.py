"""Bluesky: each X post is also posted on the owner's Bluesky account.

Bluesky's API is open: a handle and an app password (Settings > Privacy and
security > App passwords) are all it needs. The post carries a link card to
the website, so the link doesn't eat into the 300-character limit.

    BSKY_HANDLE        e.g. vinc.bsky.social
    BSKY_APP_PASSWORD  an app password, never the real account password
    BSKY_LINK          the website the card points to
"""

from __future__ import annotations

import datetime as dt
import os
import re

from aiohttp import ClientSession, ClientTimeout

SERVICE = os.environ.get("BSKY_SERVICE", "https://bsky.social").rstrip("/")
HANDLE = os.environ.get("BSKY_HANDLE", "").strip().lstrip("@")
APP_PASSWORD = os.environ.get("BSKY_APP_PASSWORD", "").strip()
LINK = os.environ.get("BSKY_LINK", "https://mingchengli465-coder.github.io/Claude-agent/?from=bsky").strip()
CARD_TITLE = os.environ.get("BSKY_CARD_TITLE", "vinc · AI assistants & Claude Code setup")
CARD_TEXT = os.environ.get("BSKY_CARD_TEXT",
                           "24/7 AI customer assistants for small businesses, and Claude Code / Codex set up on your computer.")
MAX_GRAPHEMES = 300
_URL = re.compile(r"https?://[^\s)\]]+")


def enabled() -> bool:
    return bool(HANDLE and APP_PASSWORD)


def fit(text: str, limit: int = MAX_GRAPHEMES) -> str:
    """Bluesky counts characters (roughly graphemes); cut at a word if it's too long."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    cut = cut[:cut.rfind(" ")] if " " in cut[limit // 2:] else cut
    return cut.rstrip(" ,.;:") + "…"


def link_facets(text: str) -> list[dict]:
    """Links in the text made clickable. Bluesky wants UTF-8 byte offsets."""
    facets = []
    for m in _URL.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        start = len(text[:m.start()].encode())
        facets.append({"index": {"byteStart": start, "byteEnd": start + len(url.encode())},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]})
    return facets


class BlueskyError(RuntimeError):
    pass


async def _call(session: ClientSession, path: str, body: dict, token: str = "") -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with session.post(f"{SERVICE}/xrpc/{path}", json=body, headers=headers) as r:
        data = await r.json(content_type=None) if r.content_length != 0 else {}
        if r.status >= 400:
            raise BlueskyError(f"Bluesky 出错（{r.status}）：{(data or {}).get('message') or (data or {}).get('error') or ''}")
        return data or {}


async def post(text: str, session: ClientSession | None = None) -> str:
    """Post on Bluesky; returns the post's link."""
    if not enabled():
        raise BlueskyError("没有设置 BSKY_HANDLE / BSKY_APP_PASSWORD")
    own = session is None
    session = session or ClientSession(timeout=ClientTimeout(total=30))
    try:
        auth = await _call(session, "com.atproto.server.createSession",
                           {"identifier": HANDLE, "password": APP_PASSWORD})
        text = fit(text)
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "langs": ["zh"] if re.search(r"[一-鿿]", text) else ["en"],
        }
        facets = link_facets(text)
        if facets:
            record["facets"] = facets
        if LINK:
            record["embed"] = {"$type": "app.bsky.embed.external",
                               "external": {"uri": LINK, "title": CARD_TITLE, "description": CARD_TEXT}}
        made = await _call(session, "com.atproto.repo.createRecord",
                           {"repo": auth["did"], "collection": "app.bsky.feed.post", "record": record},
                           token=auth["accessJwt"])
        rkey = str(made.get("uri", "")).rsplit("/", 1)[-1]
        return f"https://bsky.app/profile/{auth.get('handle') or HANDLE}/post/{rkey}"
    finally:
        if own:
            await session.close()
