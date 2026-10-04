"""New businesses to email, found on OpenStreetMap (the open world map) through the Overpass API.

Google Maps can't be read by a program without a paid API key; OpenStreetMap lists the same
kinds of small shops, many with their public email, and is free to query. Only independent
businesses that publish an email are kept: no chains (anything with a brand), nothing closed.

    find(region, day) -> [{"email", "name", "region", "cat", "city", "site", "first"}]

A whole country at once is too heavy for the public Overpass servers (they time out), so each
search covers one town and its surroundings; the town changes from day to day.

Few shops put their email on the map, but many put their website: for those, the email is read
from the shop's own homepage or contact page (mailto links, plain text, Cloudflare-hidden ones).
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import math
import os
import random
import re
from urllib.parse import urljoin, urlparse

from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)

OVERPASS = [u.strip() for u in os.environ.get(
    "OVERPASS_URLS", "https://overpass-api.de/api/interpreter,https://overpass.private.coffee/api/interpreter,"
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter,https://overpass.kumi.systems/api/interpreter").split(",") if u.strip()]
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
    "bnb": [("tourism", "guest_house"), ("tourism", "hotel"), ("tourism", "chalet"), ("tourism", "apartment")],
    "florist": [("shop", "florist")],
    "bakery": [("shop", "pastry"), ("shop", "bakery"), ("shop", "confectionery")],
    "beauty": [("shop", "beauty"), ("shop", "hairdresser"), ("shop", "massage")],
    "groomer": [("shop", "pet_grooming")],
    "tutor": [("amenity", "prep_school")],
}
PLURAL = {"bnb": "places to stay", "florist": "florists", "bakery": "cake shops and bakeries",
          "beauty": "beauty and hair salons", "groomer": "dog groomers", "tutor": "tuition centres"}
# shops with only a website: how many to look up per town, and how many at once
SITE_LOOKUPS = int(os.environ.get("LEADS_SITE_LOOKUPS", "60"))
SITE_PARALLEL = 6
# seconds to wait after a map server says "too many requests", and between towns (bot.py)
BUSY_WAIT = float(os.environ.get("LEADS_BUSY_WAIT", "30"))
PAUSE = float(os.environ.get("LEADS_PAUSE", "15"))
EMAIL = re.compile(r"^[\w.+'-]+@[\w-]+(\.[\w-]+)+$")
# addresses that belong to a platform, not the business
NOT_THEIRS = ("booking.com", "airbnb", "example.", "wix.com", "wixpress", "sentry", "facebook.com", "noreply", "no-reply",
              "domain.com", "email.com", "yourname", "yourdomain", "godaddy", "squarespace", "wordpress", "@sentry",
              "privacy", "gdpr", "abuse@", "webmaster@", "postmaster@")
# the free mailboxes small shops often use instead of their own domain
FREE_MAIL = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "live.com", "live.co.uk",
             "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com", "btinternet.com", "btconnect.com", "aol.com",
             "bigpond.com", "bigpond.net.au", "xtra.co.nz", "eircom.net", "sky.com", "talktalk.net", "virginmedia.com")


def query(lat: float, lon: float, radius: int) -> str:
    """Every kind of business we write to around one town (with an email or a website; pick() sorts them)."""
    # a box around the town: much lighter for the servers than "around"
    dlat, dlon = radius / 111_320, radius / (111_320 * max(0.2, math.cos(math.radians(lat))))
    near = f"({lat - dlat:.4f},{lon - dlon:.4f},{lat + dlat:.4f},{lon + dlon:.4f})"
    # one clause per tag key (a regex over the values) keeps the query cheap for busy servers
    by_key: dict[str, list[str]] = {}
    for pairs in TAGS.values():
        for k, v in pairs:
            by_key.setdefault(k, []).append(v)
    parts = "".join(f'nwr["{k}"~"^({"|".join(values)})$"]["name"]{near};' for k, values in by_key.items())
    return f"[out:json][timeout:60];({parts});out tags 3000;"


def kind_of(tags: dict) -> str:
    return next((kind for kind, pairs in TAGS.items() if any(tags.get(k) == v for k, v in pairs)), "")


def usable(email: str) -> bool:
    return bool(EMAIL.match(email)) and not any(bad in email for bad in NOT_THEIRS) and len(email) <= 80


def _lead(tags: dict, kind: str, region: str, email: str) -> dict:
    name = tags["name"].strip()
    city = next((tags[k] for k in ("addr:city", "addr:town", "addr:village", "addr:suburb", "addr:place")
                 if tags.get(k)), "").strip()
    site = (tags.get("website") or tags.get("contact:website") or "").strip()
    where = city or COUNTRY_NAME[region]
    return {"email": email, "name": name[:60], "region": region, "lang": "en", "kind": "shop",
            "cat": kind, "city": city[:40], "site": site[:80],
            "first": f"I came across {name} on the map while looking at {PLURAL[kind]} in {where}."}


def _independent(el: dict) -> tuple[dict, str]:
    """The element's tags and kind, or ({}, "") for a chain, a closed shop or another kind of business."""
    tags = el.get("tags") or {}
    kind = kind_of(tags)
    if not kind or not (tags.get("name") or "").strip():
        return {}, ""
    if any(k in tags for k in ("brand", "brand:wikidata", "franchise", "disused:shop", "end_date")):
        return {}, ""
    if tags.get("opening_hours") == "closed":
        return {}, ""
    return tags, kind


def pick(elements: list[dict], region: str) -> list[dict]:
    """The independent, open businesses with a usable email on the map, as leads."""
    found, seen = [], set()
    for el in elements:
        tags, kind = _independent(el)
        raw = (tags.get("email") or tags.get("contact:email") or "") if kind else ""
        email = raw.split(";")[0].strip().removeprefix("mailto:").lower()
        if not kind or not usable(email) or email in seen:
            continue
        seen.add(email)
        found.append(_lead(tags, kind, region, email))
    return found


def with_site(elements: list[dict]) -> list[tuple[dict, str, str]]:
    """The independent businesses with a website but no email on the map: (tags, kind, url)."""
    out, seen = [], set()
    for el in elements:
        tags, kind = _independent(el)
        if not kind or tags.get("email") or tags.get("contact:email"):
            continue
        url = (tags.get("website") or tags.get("contact:website") or "").split(";")[0].strip()
        if not url:
            continue
        url = url if url.startswith("http") else "http://" + url
        host = urlparse(url).netloc.lower().removeprefix("www.")
        # a page on a booking site or social network isn't the shop's own website
        if not host or host in seen or any(p in host for p in ("facebook.", "instagram.", "booking.com", "airbnb.",
                                                                "tripadvisor.", "google.", "linktr.ee", "yell.com")):
            continue
        seen.add(host)
        out.append((tags, kind, url))
    return out


_MAILTO = re.compile(r"mailto:([^\"'?>\s]+)", re.I)
_TEXT_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}", re.I)
_CFEMAIL = re.compile(r'data-cfemail="([0-9a-f]+)"', re.I)
_CONTACT_LINK = re.compile(r'href="([^"#]*contact[^"#]*)"', re.I)
_IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


def cf_decode(hexed: str) -> str:
    """Cloudflare's hidden email: the first byte is the key, every other byte is XORed with it."""
    try:
        key = int(hexed[:2], 16)
        return "".join(chr(int(hexed[i:i + 2], 16) ^ key) for i in range(2, len(hexed), 2))
    except ValueError:
        return ""


def emails_in(page: str) -> list[str]:
    """Every address on a page, mailto links and hidden ones first."""
    found = [_html.unescape(m).strip() for m in _MAILTO.findall(page)]
    found += [cf_decode(h) for h in _CFEMAIL.findall(page)]
    found += _TEXT_EMAIL.findall(_html.unescape(page))
    out = []
    for email in found:
        email = email.lower().strip(".")
        if usable(email) and not email.endswith(_IMAGE) and email not in out:
            out.append(email)
    return out


def best_email(emails: list[str], site: str) -> str:
    """The shop's own address: on its own domain, else a free mailbox it uses; nothing from elsewhere."""
    host = urlparse(site).netloc.lower().removeprefix("www.")
    own = [e for e in emails if host and (e.split("@")[1] == host or host.endswith("." + e.split("@")[1])
                                          or e.split("@")[1].endswith("." + host))]
    pool = own or [e for e in emails if e.split("@")[1] in FREE_MAIL]
    # info@ / hello@ / bookings@ before a person's own address
    pool.sort(key=lambda e: 0 if e.split("@")[0] in ("info", "hello", "enquiries", "enquiry", "bookings", "booking",
                                                    "contact", "stay", "reservations", "office", "admin") else 1)
    return pool[0] if pool else ""


async def _page(session: ClientSession, url: str) -> str:
    try:
        async with session.get(url, timeout=ClientTimeout(total=12), allow_redirects=True) as r:
            if r.status != 200 or "html" not in r.headers.get("Content-Type", "html"):
                return ""
            return (await r.content.read(600_000)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a site that's down just has no email for us
        return ""


async def site_email(session: ClientSession, url: str) -> str:
    """The email a shop shows on its homepage, or else on its contact page."""
    home = await _page(session, url)
    if not home:
        return ""
    email = best_email(emails_in(home), url)
    if email:
        return email
    tried = set()
    links = [urljoin(url, h) for h in _CONTACT_LINK.findall(home)][:2] + [urljoin(url, "/contact"), urljoin(url, "/contact-us")]
    for link in links:
        if urlparse(link).netloc != urlparse(url).netloc or link in tried or len(tried) >= 2:
            continue
        tried.add(link)
        email = best_email(emails_in(await _page(session, link)), url)
        if email:
            return email
    return ""


async def from_sites(session: ClientSession, shops: list[tuple[dict, str, str]], region: str) -> list[dict]:
    """Leads for the shops whose email is on their own website."""
    gate = asyncio.Semaphore(SITE_PARALLEL)

    async def one(tags, kind, url):
        async with gate:
            email = await site_email(session, url)
        return _lead(tags, kind, region, email) if email else None

    found = await asyncio.gather(*(one(*shop) for shop in shops))
    out, seen = [], set()
    for lead in found:
        if lead and lead["email"] not in seen:
            seen.add(lead["email"])
            out.append(lead)
    return out


def town_for(region: str, day: int, attempt: int = 0) -> tuple:
    towns = TOWNS[region]
    return towns[(day * 7 + attempt * 3 + len(region)) % len(towns)]


async def find(region: str, day: int, attempt: int = 0, session: ClientSession | None = None,
               skip: set[str] | None = None) -> list[dict]:
    """Ask the map about one town (a different one each day); the first Overpass server that
    answers wins. Shops with only a website get their email from it. `skip`: names (lowercase)
    already on the list, not looked up again."""
    _, lat, lon, radius = town_for(region, day, attempt)
    skip = skip or set()
    own = session is None
    session = session or ClientSession(timeout=ClientTimeout(total=120),
                                       headers={"User-Agent": "Mozilla/5.0 (compatible; vinc-leads/1.1)"})
    try:
        for url in OVERPASS:
            try:
                async with session.post(url, data={"data": query(lat, lon, radius)}) as r:
                    if r.status != 200:
                        logger.warning("地图查询 %s 返回 %s", url, r.status)
                        if r.status == 429:
                            await asyncio.sleep(BUSY_WAIT)  # too many requests: give it a moment
                        continue
                    data = await r.json(content_type=None)
            except Exception as exc:  # noqa: BLE001 - try the next server
                logger.warning("地图查询 %s 失败：%s", url, exc)
                continue
            elements = data.get("elements") or []
            leads = [lead for lead in pick(elements, region) if lead["name"].lower() not in skip]
            have = {lead["name"].lower() for lead in leads} | skip
            shops = [s for s in with_site(elements) if s[0]["name"].strip()[:60].lower() not in have]
            random.shuffle(shops)
            more = await from_sites(session, shops[:SITE_LOOKUPS], region)
            logger.info("地图 %s：地图上有邮箱 %d 家，从 %d 个网站里找到 %d 个邮箱", town_for(region, day, attempt)[0],
                        len(leads), min(len(shops), SITE_LOOKUPS), len(more))
            leads += more
            random.shuffle(leads)
            return leads
        return []
    finally:
        if own:
            await session.close()
