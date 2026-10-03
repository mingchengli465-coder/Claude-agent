"""New businesses to email, found on OpenStreetMap (the open world map) through the Overpass API.

Google Maps can't be read by a program without a paid API key; OpenStreetMap lists the same
kinds of small shops, many with their public email, and is free to query. Only independent
businesses that publish an email are kept: no chains (anything with a brand), nothing closed.

    find(region, kind) -> [{"email", "name", "region", "cat", "city", "site", "first"}]
"""

from __future__ import annotations

import logging
import os
import random
import re

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

OVERPASS = [u.strip() for u in os.environ.get(
    "OVERPASS_URLS", "https://overpass-api.de/api/interpreter,https://overpass.kumi.systems/api/interpreter").split(",") if u.strip()]
COUNTRY = {"uk": "GB", "ie": "IE", "au": "AU", "nz": "NZ", "sg": "SG", "hk": "HK", "my": "MY"}
COUNTRY_NAME = {"uk": "the UK", "ie": "Ireland", "au": "Australia", "nz": "New Zealand", "sg": "Singapore",
                "hk": "Hong Kong", "my": "Malaysia"}
# what each kind of business is tagged as on the map
TAGS = {
    "bnb": [("tourism", "guest_house")],
    "florist": [("shop", "florist")],
    "bakery": [("shop", "pastry"), ("shop", "bakery")],
    "beauty": [("shop", "beauty")],
    "groomer": [("shop", "pet_grooming")],
}
PLURAL = {"bnb": "B&Bs", "florist": "florists", "bakery": "cake shops and bakeries", "beauty": "beauty studios",
          "groomer": "dog groomers"}
EMAIL = re.compile(r"^[\w.+'-]+@[\w-]+(\.[\w-]+)+$")
# addresses that belong to a platform, not the business
NOT_THEIRS = ("booking.com", "airbnb", "example.", "wix.com", "sentry.", "facebook.com", "noreply", "no-reply")


def query(region: str, kind: str, limit: int = 1500) -> str:
    country = COUNTRY[region]
    parts = "".join(f'nwr["{k}"="{v}"]["{key}"](area.c);' for k, v in TAGS[kind] for key in ("email", "contact:email"))
    return (f'[out:json][timeout:90];area["ISO3166-1"="{country}"][admin_level=2]->.c;'
            f"({parts});out tags {limit};")


def pick(elements: list[dict], region: str, kind: str) -> list[dict]:
    """The independent, open businesses with a usable email, as leads."""
    found, seen = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name or any(k in tags for k in ("brand", "brand:wikidata", "franchise", "disused:shop", "end_date")):
            continue
        if tags.get("opening_hours") == "closed":
            continue
        raw = tags.get("email") or tags.get("contact:email") or ""
        email = raw.split(";")[0].strip().removeprefix("mailto:").lower()
        if not EMAIL.match(email) or any(bad in email for bad in NOT_THEIRS) or email in seen:
            continue
        seen.add(email)
        city = next((tags[k] for k in ("addr:city", "addr:town", "addr:village", "addr:suburb", "addr:place")
                     if tags.get(k)), "").strip()
        site = (tags.get("website") or tags.get("contact:website") or "").strip()
        where = city or COUNTRY_NAME[region]
        found.append({"email": email, "name": name[:60], "region": region, "lang": "en", "kind": "shop",
                      "cat": kind, "city": city[:40], "site": site[:80],
                      "first": f"I came across {name} on the map while looking at {PLURAL[kind]} in {where}."})
    return found


async def find(region: str, kind: str, session: ClientSession | None = None) -> list[dict]:
    """Ask the map; the first Overpass server that answers wins. Shuffled, so each day differs."""
    own = session is None
    session = session or ClientSession(timeout=ClientTimeout(total=120),
                                       headers={"User-Agent": "vinc-leads/1.0 (small-business outreach)"})
    try:
        for url in OVERPASS:
            try:
                async with session.post(url, data={"data": query(region, kind)}) as r:
                    if r.status != 200:
                        logger.warning("地图查询 %s 返回 %s", url, r.status)
                        continue
                    data = await r.json(content_type=None)
            except Exception as exc:  # noqa: BLE001 - try the next server
                logger.warning("地图查询 %s 失败：%s", url, exc)
                continue
            leads = pick(data.get("elements") or [], region, kind)
            random.shuffle(leads)
            return leads
        return []
    finally:
        if own:
            await session.close()
