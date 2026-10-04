"""Reading a business's own website: its text (for an assistant that answers from it), and who built it.

    read_site(session, url) -> (title, text)   the homepage plus a few pages that matter (prices, rooms,
                                                menu, services, FAQ…), as plain text, repeated lines dropped
    credits(html, url) -> [(url, name)]         "Website by …" links: the web designer behind the site

Every URL may come from a stranger (the /trial form) or from the open map, so fetch() only goes to
public addresses, checks each redirect itself, and reads at most MAX_BYTES.
"""

from __future__ import annotations

import asyncio
import html as _html
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

from aiohttp import ClientSession, ClientTimeout

MAX_BYTES = 600_000
# websites live on the usual ports; anything else is refused (None: any port, for tests)
PORTS: tuple | None = (None, 80, 443)
MAX_TEXT = 6000
AGENT = "Mozilla/5.0 (compatible; vinc-assistant/1.1; +https://mingchengli465-coder.github.io/Claude-agent/)"
# links worth reading after the homepage
_USEFUL = re.compile(r"price|pricing|rate|tariff|room|stay|accommodation|menu|service|treatment|book|faq|question|"
                     r"about|course|fee|cake|order|deliver|wedding|groom|class|lesson|contact", re.I)
_LINK = re.compile(r'<a\b[^>]*?href\s*=\s*["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.I | re.S)
_DROP = re.compile(r"<(script|style|noscript|svg|template|iframe)\b.*?</\1\s*>|<!--.*?-->", re.I | re.S)
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|tr|td|th|h[1-6]|section|article|header|footer|nav|table|dd|dt)\b[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_META = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', re.I)

# "Website by", "Designed by", "Site by", "Web design:" … followed (soon) by a link
_CREDIT = re.compile(
    r"(?:"
    r"\b(?:web\s*site|site)\s+(?:(?:made|built|designed|design|created|developed|crafted)\s+(?:(?:&amp;|&|and)\s*\w+\s+)?)?by"
    r"|\b(?:web\s*design(?:ed)?|designed|design|built|developed|crafted)(?:\s*(?:&amp;|&|and)\s*\w+)?\s+by"
    r"|\b(?:web\s*design|designed|design)\s*:"
    r")\s*(?:<(?!a\b)[^>]*>\s*){0,3}"
    r'<a\b[^>]*?href\s*=\s*["\'](https?://[^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
# the link text itself says it: <a href="…">Website by Studio North</a>
_CREDIT_IN_LINK = re.compile(
    r'<a\b[^>]*?href\s*=\s*["\'](https?://[^"\']+)["\'][^>]*>\s*(?:web\s*site|site|web\s*design|designed|built|made)'
    r"[^<]{0,12}?\bby\s+([^<]{2,40})</a>", re.I)
# builders and platforms are not a designer we can partner with
PLATFORMS = ("wix.", "squarespace.", "wordpress.", "shopify.", "godaddy.", "weebly.", "jimdo.", "webflow.", "elementor.",
             "themeforest.", "envato.", "facebook.", "instagram.", "google.", "yell.", "booking.", "airbnb.",
             "tripadvisor.", "strikingly.", "site123.", "hostinger.", "ionos.", "1and1.", "123-reg.", "bigcommerce.",
             "freeindex.", "linkedin.", "twitter.", "x.com", "youtube.", "tiktok.", "pinterest.", "w3.org", "apple.",
             "microsoft.", "cloudflare.", "gstatic.", "astra", "generatepress", "kadence", "divi", "oceanwp")


def _public(host: str) -> bool:
    """True when every address the host resolves to is on the public internet."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global or addr.is_multicast:
            return False
    return bool(infos)


def normalise(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" in url and not re.match(r"^https?://", url, re.I):
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "http://" + url
    parts = urlparse(url)
    try:
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return ""
    if PORTS is not None and port not in PORTS:
        return ""
    return url


async def fetch(session: ClientSession, url: str, max_bytes: int = MAX_BYTES) -> tuple[str, str]:
    """(final url, html) of a public web page, or ("", "")."""
    url = normalise(url)
    for _ in range(5):
        if not url or not await asyncio.to_thread(_public, urlparse(url).hostname):
            return "", ""
        try:
            async with session.get(url, allow_redirects=False, timeout=ClientTimeout(total=12),
                                   headers={"User-Agent": AGENT, "Accept": "text/html,*/*;q=0.5"}) as r:
                if r.status in (301, 302, 303, 307, 308):
                    url = normalise(urljoin(url, r.headers.get("Location", "")))
                    continue
                if r.status != 200 or "html" not in r.headers.get("Content-Type", "text/html").lower():
                    return "", ""
                return str(r.url), (await r.content.read(max_bytes)).decode(r.charset or "utf-8", "replace")
        except Exception:  # noqa: BLE001 - a site that's down or slow just has nothing for us
            return "", ""
    return "", ""


def title_of(page: str) -> str:
    match = _TITLE.search(page)
    title = re.sub(r"\s+", " ", _html.unescape(match.group(1))).strip() if match else ""
    # "Rose Cottage B&B | Keswick | Home" -> "Rose Cottage B&B"
    return re.split(r"\s+[|–—-]\s+", title)[0][:80]


def text_of(page: str) -> str:
    """The words a visitor reads, one block per line."""
    meta = _META.search(page)
    body = _DROP.sub(" ", page)
    body = _BLOCK.sub("\n", body)
    body = _html.unescape(_TAG.sub(" ", body))
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in body.split("\n")]
    text = "\n".join(line for line in lines if len(line) > 1)
    return (_html.unescape(meta.group(1)).strip() + "\n" + text) if meta else text


def useful_links(page: str, base: str, limit: int = 3) -> list[str]:
    host = urlparse(base).netloc
    out = []
    for href, label in _LINK.findall(page):
        url = urljoin(base, _html.unescape(href.strip()))
        if (urlparse(url).netloc == host and url.rstrip("/") != base.rstrip("/") and url not in out
                and _USEFUL.search(href + " " + _TAG.sub(" ", label))
                and not re.search(r"\.(pdf|jpe?g|png|gif|webp|zip|docx?)$", url, re.I)):
            out.append(url)
    return out[:limit]


async def read_site(session: ClientSession, url: str, limit: int = MAX_TEXT) -> tuple[str, str]:
    """(title, text) of a business's website: its homepage and the pages that matter, repeated
    lines (menus, footers) kept once."""
    home_url, home = await fetch(session, url)
    if not home:
        return "", ""
    pages = [(home_url, home)]
    for link in useful_links(home, home_url):
        found_url, page = await fetch(session, link)
        if page:
            pages.append((found_url, page))
    seen, parts, size = set(), [], 0
    for page_url, page in pages:
        lines = [line for line in text_of(page).split("\n") if line not in seen]
        seen.update(lines)
        if not lines:
            continue
        block = f"## {urlparse(page_url).path or '/'}\n" + "\n".join(lines)
        parts.append(block[:max(0, limit - size)])
        size += len(parts[-1]) + 2
        if size >= limit:
            break
    return title_of(home), "\n\n".join(p for p in parts if p).strip()


def credits(page: str, url: str) -> list[tuple[str, str]]:
    """The web designer a site credits ("Website by Studio North"), as (their url, their name)."""
    own = (urlparse(normalise(url)).hostname or "").removeprefix("www.")
    out = []
    found = [(u, n) for u, n in _CREDIT.findall(page)] + [(u, n) for u, n in _CREDIT_IN_LINK.findall(page)]
    for link, label in found:
        link = _html.unescape(link)
        host = (urlparse(link).hostname or "").lower().removeprefix("www.")
        if not host or host == own or host.endswith("." + own) or any(p in host for p in PLATFORMS):
            continue
        name = re.sub(r"\s+", " ", _html.unescape(_TAG.sub(" ", label))).strip(" .|-–") or host
        if len(name) > 40 or "@" in name:
            name = host
        home = f"{urlparse(link).scheme}://{urlparse(link).netloc}/"
        if all(h != home for h, _ in out):
            out.append((home, name))
    return out
