"""Build a static copy of the site for GitHub Pages.

X refuses posts that link to *.up.railway.app, so the pages people click from
X are also published at https://<user>.github.io/<repo>/. The copy is the same
HTML the bot serves, with the owner's contacts filled in from products.yaml;
the chat window and its API still come from the Railway server.

    python tools/build_pages.py _site

PAGES_API_ORIGIN  where the chat server lives (default: the Railway address)
PAGES_BOT_LINK    the Telegram bot link used on the site
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import demos  # noqa: E402
import web  # noqa: E402

API = os.environ.get("PAGES_API_ORIGIN", "https://worker-production-42fb.up.railway.app").rstrip("/")
BOT = os.environ.get("PAGES_BOT_LINK", "https://t.me/emilyhanbot")
PAGES = {"site.html": "index.html", "demo.html": "demo.html", "video.html": "video.html",
         "trial.html": "trial.html", "trial-shop.html": "t.html"}


class _NoService:
    """WebChat only needs register_channel to render pages."""

    def register_channel(self, channel, send):
        pass


def static_copy(html: str) -> str:
    """Point the page's server paths at the Pages copy or the chat server."""
    return (html.replace("url(/fonts/", "url(fonts/")
                .replace('href="/fonts/', 'href="fonts/')
                .replace('src="/widget.js"', f'src="{API}/widget.js"')
                .replace('href="/demo"', 'href="demo.html"')
                .replace('href="/"', 'href="./"')
                .replace('var API = "";', f'var API = "{API}";'))


SITE = os.environ.get("PAGES_SITE_URL", "https://mingchengli465-coder.github.io/Claude-agent").rstrip("/")


def write_sitemap(out: Path) -> None:
    """sitemap.xml and robots.txt, so search engines find the guides."""
    pages = ["", "demo.html", "video.html", "trial.html", "guides/"] + [f"demo-{kind}.html" for kind in demos.DEMOS] + [f"guides/{p.name}" for p in sorted((out / "guides").glob("*.html"))
                                             if p.name != "index.html"]
    urls = "".join(f"  <url><loc>{SITE}/{p}</loc></url>\n" for p in pages)
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                     f"{urls}</urlset>\n", encoding="utf-8")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8")


def build(out: Path) -> None:
    chat = web.WebChat(_NoService(), title=web.shop_name(), contact_link=BOT)
    if out.exists():
        shutil.rmtree(out)
    (out / "fonts").mkdir(parents=True)
    for source, target in PAGES.items():
        html = chat._render(source).text
        (out / target).write_text(static_copy(html), encoding="utf-8")
    for kind in demos.DEMOS:
        html = chat._render("demo-industry.html", demo=kind).text
        (out / f"demo-{kind}.html").write_text(static_copy(html), encoding="utf-8")
    (out / "guides").mkdir()
    for guide in sorted((ROOT / "web_static" / "guides").glob("*.html")):
        html = chat._render(f"guides/{guide.name}").text
        (out / "guides" / guide.name).write_text(static_copy(html).replace('href="../demo"', 'href="../demo.html"'), encoding="utf-8")
    write_sitemap(out)
    (out / "media").mkdir()
    # the mockups the English-market emails link to (their plain-text version)
    shutil.copytree(ROOT / "web_static" / "mockups", out / "mockups")
    for name in web._MEDIA:
        shutil.copy(ROOT / "media" / name, out / "media" / name)
    for font in (ROOT / "web_static" / "fonts").iterdir():
        shutil.copy(font, out / "fonts" / font.name)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"built {', '.join(PAGES.values())} into {out} (chat server: {API})")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "_site"))
