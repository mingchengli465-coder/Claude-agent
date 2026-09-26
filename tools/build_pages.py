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

import web  # noqa: E402

API = os.environ.get("PAGES_API_ORIGIN", "https://worker-production-42fb.up.railway.app").rstrip("/")
BOT = os.environ.get("PAGES_BOT_LINK", "https://t.me/emilyhanbot")
PAGES = {"site.html": "index.html", "demo.html": "demo.html"}


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
                .replace('href="/"', 'href="./"'))


def build(out: Path) -> None:
    chat = web.WebChat(_NoService(), title=web.shop_name(), contact_link=BOT)
    if out.exists():
        shutil.rmtree(out)
    (out / "fonts").mkdir(parents=True)
    for source, target in PAGES.items():
        html = chat._render(source).text
        (out / target).write_text(static_copy(html), encoding="utf-8")
    for font in (ROOT / "web_static" / "fonts").iterdir():
        shutil.copy(font, out / "fonts" / font.name)
    (out / ".nojekyll").write_text("", encoding="utf-8")
    print(f"built {', '.join(PAGES.values())} into {out} (chat server: {API})")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "_site"))
