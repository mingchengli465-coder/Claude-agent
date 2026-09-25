"""Rebuild web_static/fonts/serif-sc.woff2 and serif-tc.woff2 after changing
the site's headings.

The AI customer-service page's (demo.html) big serif headings are in Chinese too. Google Fonts doesn't load in
mainland China and a full Chinese font is ~10 MB, so we ship only the
characters the serif text on demo.html actually uses, cut from Noto Serif
SC / TC (SIL Open Font License):

    pip install fonttools brotli
    curl -LO https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/SubsetOTF/SC/NotoSerifSC-Regular.otf
    curl -LO https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/SubsetOTF/TC/NotoSerifTC-Regular.otf
    python tools/build_fonts.py NotoSerifSC-Regular.otf NotoSerifTC-Regular.otf
"""

from __future__ import annotations

import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "web_static" / "demo.html"
OUT = ROOT / "web_static" / "fonts"
# Keys shown in the serif face without a "serif" class on their element.
EXTRA_KEYS = {"scenes", "mockName"}


class SerifKeys(HTMLParser):
    def __init__(self):
        super().__init__()
        self.keys: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "serif" in (a.get("class") or "").split():
            key = a.get("data-i") or a.get("data-ih")
            if key:
                self.keys.add(key)


def dictionary(html: str) -> dict:
    """The page's `var I = {...};` translations, read as JSON."""
    block = re.search(r"var I = (\{.*?\n  \});", html, re.S).group(1)
    block = re.sub(r"([{,]\s*)([A-Za-z0-9_]+):", r'\1"\2":', block)
    return json.loads(block)


def strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [s for v in value for s in strings(v)]


def main(sc_font: str, tc_font: str) -> None:
    from fontTools import subset

    html = PAGE.read_text(encoding="utf-8")
    parser = SerifKeys()
    parser.feed(html)
    words = dictionary(html)
    keys = parser.keys | EXTRA_KEYS
    for index, (font, name) in enumerate(((sc_font, "serif-sc"), (tc_font, "serif-tc"))):
        text = "".join(s for k in keys if k in words for s in strings(words[k][index]))
        text = re.sub(r"<[^>]+>", "", text) + "“”「」，。？！、：；（）《》0123456789"
        chars = sorted(set(c for c in text if ord(c) > 0x2000))
        options = subset.Options()
        options.flavor = "woff2"
        options.layout_features = ["*"]
        options.name_IDs = ["*"]
        options.notdef_outline = True
        loaded = subset.load_font(font, options)
        subsetter = subset.Subsetter(options)
        subsetter.populate(text="".join(chars))
        subsetter.subset(loaded)
        out = OUT / f"{name}.woff2"
        subset.save_font(loaded, str(out), options)
        print(f"{out.relative_to(ROOT)}: {len(chars)} characters, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
