"""New businesses to email, found on OpenStreetMap (the open world map) through the Overpass API.

Google Maps can't be read by a program without a paid API key; OpenStreetMap lists the same
kinds of small shops, many with their public email, and is free to query. Only independent
businesses that publish an email are kept: no chains (anything with a brand), nothing closed.

    find(region, day) -> [{"email", "name", "region", "cat", "city", "site", "first"}]

A whole country at once is too heavy for the public Overpass servers (they time out), so each
search covers one town and its surroundings; the town changes from day to day.
"""

from __future__ import annotations

import logging
import math
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
# towns searched in turn: (name, lat, lon, radius in metres)
TOWNS = {
    "uk": [("London", 51.507, -0.128, 15000), ("Manchester", 53.48, -2.24, 20000), ("Bristol", 51.45, -2.59, 20000),
           ("Edinburgh", 55.95, -3.19, 20000), ("Leeds", 53.80, -1.55, 20000), ("Brighton", 50.82, -0.14, 20000),
           ("York", 53.96, -1.08, 25000), ("Bath", 51.38, -2.36, 25000), ("Cambridge", 52.21, 0.12, 25000),
           ("Oxford", 51.75, -1.26, 25000), ("Glasgow", 55.86, -4.25, 20000), ("Cardiff", 51.48, -3.18, 25000),
           ("Norwich", 52.63, 1.30, 25000), ("Keswick", 54.60, -3.13, 30000), ("Inverness", 57.48, -4.22, 30000),
           ("Harrogate", 53.99, -1.54, 30000), ("Birmingham", 52.48, -1.90, 20000), ("Liverpool", 53.41, -2.98, 20000)],
    "ie": [("Dublin", 53.35, -6.26, 20000), ("Cork", 51.90, -8.47, 25000), ("Galway", 53.27, -9.05, 30000),
           ("Limerick", 52.66, -8.63, 30000), ("Kilkenny", 52.65, -7.25, 30000), ("Killarney", 52.06, -9.51, 30000)],
    "au": [("Sydney", -33.87, 151.21, 25000), ("Melbourne", -37.81, 144.96, 25000), ("Brisbane", -27.47, 153.03, 25000),
           ("Perth", -31.95, 115.86, 25000), ("Adelaide", -34.93, 138.60, 25000), ("Hobart", -42.88, 147.33, 30000),
           ("Canberra", -35.28, 149.13, 25000), ("Gold Coast", -28.02, 153.40, 25000)],
    "nz": [("Auckland", -36.85, 174.76, 25000), ("Wellington", -41.29, 174.78, 25000), ("Christchurch", -43.53, 172.64, 25000),
           ("Queenstown", -45.03, 168.66, 30000), ("Nelson", -41.27, 173.28, 30000), ("Dunedin", -45.87, 170.50, 30000)],
    "sg": [("Singapore", 1.35, 103.82, 20000)],
    "hk": [("Hong Kong", 22.30, 114.17, 15000)],
    "my": [("Kuala Lumpur", 3.14, 101.69, 20000), ("Penang", 5.41, 100.33, 20000), ("Johor Bahru", 1.49, 103.74, 20000)],
}
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


def query(lat: float, lon: float, radius: int) -> str:
    """Every kind of business we write to, with an email, around one town."""
    # a box around the town: much lighter for the servers than "around"
    dlat, dlon = radius / 111_320, radius / (111_320 * max(0.2, math.cos(math.radians(lat))))
    near = f"({lat - dlat:.4f},{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f})"
    parts = "".join(f'nwr["{k}"="{v}"]["{key}"]{near};' for tags in TAGS.values() for k, v in tags
                    for key in ("email", "contact:email"))
    return f"[out:json][timeout:50];({parts});out tags 600;"


def kind_of(tags: dict) -> str:
    return next((kind for kind, pairs in TAGS.items() if any(tags.get(k) == v for k, v in pairs)), "")


def pick(elements: list[dict], region: str) -> list[dict]:
    """The independent, open businesses with a usable email, as leads."""
    found, seen = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        kind = kind_of(tags)
        if not kind:
            continue
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


def town_for(region: str, day: int, attempt: int = 0) -> tuple:
    towns = TOWNS[region]
    return towns[(day * 7 + attempt * 3 + len(region)) % len(towns)]


async def find(region: str, day: int, attempt: int = 0, session: ClientSession | None = None) -> list[dict]:
    """Ask the map about one town (a different one each day); the first Overpass server that
    answers wins."""
    _, lat, lon, radius = town_for(region, day, attempt)
    own = session is None
    session = session or ClientSession(timeout=ClientTimeout(total=120),
                                       headers={"User-Agent": "vinc-leads/1.0 (small-business outreach)"})
    try:
        for url in OVERPASS:
            try:
                async with session.post(url, data={"data": query(lat, lon, radius)}) as r:
                    if r.status != 200:
                        logger.warning("地图查询 %s 返回 %s", url, r.status)
                        continue
                    data = await r.json(content_type=None)
            except Exception as exc:  # noqa: BLE001 - try the next server
                logger.warning("地图查询 %s 失败：%s", url, exc)
                continue
            leads = pick(data.get("elements") or [], region)
            random.shuffle(leads)
            return leads
        return []
    finally:
        if own:
            await session.close()
