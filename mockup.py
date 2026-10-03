"""A mockup of a business's own website with the AI assistant open on it, drawn with Pillow.

Every cold email carries one (inline in the HTML version): the business's name, town and kind
of shop, and a chat where a customer asks something typical and the assistant takes it down and
hands it to the owner. It never states facts about the business it doesn't know.

    render(lead) -> PNG bytes, 1200x800
"""

from __future__ import annotations

import hashlib
import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS = Path(__file__).with_name("fonts")
S = 2  # drawn at 2x, then scaled down: smooth edges without a browser
W, H = 1200, 800

# Each kind of business: the look of its pretend site and what customers typically ask.
KINDS = {
    "bnb": dict(bg="#f3f1ea", ink="#23302a", soft="#5d6b62", art="hills", label="Bed & Breakfast",
                nav=["Rooms", "Breakfast", "Find us", "Book"], cta="Check availability",
                sub="A warm welcome, a hearty breakfast and a comfortable bed in {city}."),
    "florist": dict(bg="#f8efec", ink="#3a2a2c", soft="#7d6467", art="flowers", label="Florist",
                    nav=["Bouquets", "Weddings", "Delivery", "Contact"], cta="Order flowers",
                    sub="Fresh, seasonal flowers for every occasion, arranged by hand in {city}."),
    "bakery": dict(bg="#f7f0e6", ink="#3b2a1e", soft="#7a6553", art="cake", label="Cakes & Bakes",
                   nav=["Cakes", "Celebrations", "Order", "Contact"], cta="Order a cake",
                   sub="Celebration cakes and fresh bakes, made to order in {city}."),
    "beauty": dict(bg="#f4eef0", ink="#2f2328", soft="#76656b", art="drops", label="Beauty Studio",
                   nav=["Treatments", "Prices", "About", "Book"], cta="Book a treatment",
                   sub="Facials, nails and treatments in a calm little studio in {city}."),
    "groomer": dict(bg="#eef3f1", ink="#1f2e2a", soft="#5a6d68", art="paws", label="Dog Grooming",
                    nav=["Services", "Prices", "About", "Book"], cta="Book a groom",
                    sub="Gentle, careful grooming for dogs of every size in {city}."),
    "tutor": dict(bg="#eef1f6", ink="#1f2a44", soft="#5b667d", art="maths", label="Tuition Centre",
                  nav=["Courses", "Exams", "Fees", "Contact"], cta="Book an assessment",
                  sub="Friendly, small-group tuition in Maths and English, in {city}."),
}
CURRENCY = {"uk": "£", "ie": "€", "au": "A$", "nz": "NZ$", "sg": "S$", "hk": "HK$", "my": "RM"}
BUDGET = {"uk": 45, "ie": 50, "au": 70, "nz": 75, "sg": 70, "hk": 350, "my": 150}

# (question, answer). {host} is who the enquiry goes to; {city}, {budget} fill in.
CHATS = {
    "bnb": [
        ("Hi! Is a double room free next Friday and Saturday? Two of us.",
         "Lovely! I've noted a double room for 2 guests, next Friday and Saturday. I'll pass this to {host} to confirm by email. What's the best email for you?"),
        ("We might not arrive until about 9:30pm. Is that OK?",
         "Thanks for letting us know! I've noted a late arrival around 9:30pm and sent it to {host}, who'll confirm check-in details with you directly."),
        ("Do you take dogs? We have a small, very well-behaved terrier.",
         "Thanks for asking! I've noted your terrier and your dates, and passed them to {host}, who'll reply about the room and dog policy. Could I take your email?"),
        ("Could you do a gluten-free breakfast? We're booked for Saturday.",
         "Of course I'll ask! I've noted gluten-free for Saturday's breakfast and passed it to {host}, so it's sorted before you arrive."),
    ],
    "florist": [
        ("Can I get a birthday bouquet delivered in {city} tomorrow? Budget about {budget}.",
         "Happy to help! I've noted: birthday bouquet, about {budget}, delivery in {city} tomorrow. Any colours she loves? I'll send it to {host} to confirm a slot."),
        ("We're getting married next June. Do you do bouquets and table flowers?",
         "Congratulations! I've noted a June wedding with a bridal bouquet and table flowers. What's the venue? I'll pass it to {host} to arrange a consultation."),
    ],
    "bakery": [
        ("Could you make a 2-tier birthday cake for next Saturday? About 30 people.",
         "Happy to help! I've noted a 2-tier birthday cake for about 30 people, next Saturday. Any flavour or theme in mind? I'll send it to {host} to confirm."),
        ("Do you do gluten-free cupcakes? I'd need 24 for Friday.",
         "Good question! I've noted 24 gluten-free cupcakes for Friday and passed it to {host}, who'll confirm and send you a price."),
    ],
    "beauty": [
        ("Do you have anything free on Friday after 5pm for a facial?",
         "Let me help! I've noted a facial for Friday after 5pm and sent it to {host}, who'll confirm the exact time with you. What's your name?"),
        ("How long does a gel manicure take? Could I come Saturday morning?",
         "Thanks! I've noted a gel manicure on Saturday morning and passed it to {host}, who'll reply with a time and how long to allow."),
    ],
    "groomer": [
        ("How much is a full groom for a cockapoo, and do you have space next week?",
         "Thanks! I've noted a full groom for your cockapoo next week and passed it to {host}, who'll reply with the price and a time."),
        ("My puppy is 14 weeks old. Can she come for her first groom?",
         "How exciting! I've noted a first puppy groom at 14 weeks and sent it to {host}, who'll suggest a gentle first visit."),
    ],
    "tutor": [
        ("My daughter is in Year 5. Can you help her prepare for entrance exams?",
         "Yes, we can help! I've passed your enquiry to {host}, who'll be in touch about an assessment and available times."),
        ("Do you offer maths tuition for a 15-year-old? Which days?",
         "Yes, we do. I've noted maths tuition for a 15-year-old and sent it to {host} to reply with the days available."),
    ],
}
DEFAULT_HOST = {"bnb": "the owners", "florist": "the shop", "bakery": "the bakery", "beauty": "the studio",
                "groomer": "the salon", "tutor": "the centre"}


def kind_of(lead: dict) -> str:
    cat = (lead.get("cat") or "").strip()
    if cat in KINDS:
        return cat
    name = (lead.get("name") or "").lower()
    for words, kind in ((("cake", "bake", "bakery", "pâtisserie", "patisserie", "蛋糕", "餅"), "bakery"),
                        (("flower", "florist", "floral", "bloom", "花"), "florist"),
                        (("beauty", "spa", "salon", "facial", "nail", "aesthetic", "美容"), "beauty"),
                        (("tuition", "tutor", "learning", "education", "edu", "補習"), "tutor"),
                        (("groom", "dog", "pet", "paw"), "groomer")):
        if any(w in name for w in words):
            return kind
    return "bnb" if any(w in name for w in ("guest", "b&b", "house", "lodge", "inn", "cottage")) else ""


def chat_for(lead: dict) -> tuple[str, str]:
    kind = kind_of(lead)
    options = CHATS[kind]
    pick = int(hashlib.sha1((lead.get("email") or lead["name"]).encode()).hexdigest(), 16) % len(options)
    q, a = options[pick]
    region = lead.get("region") or "uk"
    fill = {"host": (lead.get("host") or "").strip() or DEFAULT_HOST[kind], "city": city_of(lead),
            "budget": f"{CURRENCY.get(region, '£')}{BUDGET.get(region, 45)}"}
    return q.format(**fill), a.format(**fill)


def city_of(lead: dict) -> str:
    return (lead.get("city") or "").strip() or {"uk": "the UK", "ie": "Ireland", "au": "Australia", "nz": "New Zealand",
                                                "sg": "Singapore", "hk": "Hong Kong", "my": "Malaysia"}.get(lead.get("region") or "", "town")


# ---- drawing ---------------------------------------------------------------------------------
def _cjk(text: str) -> bool:
    return any(ord(c) > 0x2E80 for c in text)


@lru_cache(maxsize=64)
def _font(face: str, size: int) -> ImageFont.FreeTypeFont:
    files = {"serif": "InstrumentSerif-Regular.ttf", "italic": "InstrumentSerif-Italic.ttf", "sans": "Geist-Regular.ttf",
             "medium": "Geist-Medium.ttf", "bold": "Geist-SemiBold.ttf", "cjk": "NotoSansSC-VF.ttf"}
    font = ImageFont.truetype(str(FONTS / files[face]), size * S)
    if face == "cjk":
        try:
            font.set_variation_by_name("Medium")
        except Exception:  # noqa: BLE001 - a static build has no named instances
            pass
    return font


def font(face: str, size: int, text: str = "") -> ImageFont.FreeTypeFont:
    return _font("cjk" if _cjk(text) or face == "cjk" else face, size)


def rgb(hex_: str, alpha: int = 255) -> tuple[int, int, int, int]:
    h = hex_.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha


def wrap(text: str, face: str, size: int, width: int) -> list[str]:
    f = font(face, size, text)
    lines, line = [], ""
    tokens = list(text) if _cjk(text) else text.split(" ")
    joiner = "" if _cjk(text) else " "
    for token in tokens:
        trial = (line + joiner + token) if line else token
        if f.getlength(trial) <= width * S or not line:
            line = trial
        else:
            lines.append(line)
            line = token
    if line:
        lines.append(line)
    return lines


def text(d: ImageDraw.ImageDraw, xy, s: str, face: str, size: int, fill, spacing: float = 0) -> None:
    x, y = xy
    f = font(face, size, s)
    if spacing:
        for ch in s:
            d.text((x * S, y * S), ch, font=f, fill=fill)
            x += f.getlength(ch) / S + spacing
        return
    d.text((x * S, y * S), s, font=f, fill=fill)


def width_of(s: str, face: str, size: int) -> float:
    return font(face, size, s).getlength(s) / S


def rr(d, box, r, fill, outline=None, width=1):
    x0, y0, x1, y1 = box
    d.rounded_rectangle((x0 * S, y0 * S, x1 * S, y1 * S), radius=r * S, fill=fill, outline=outline, width=width * S)


def _curve(points, steps=24):
    """Catmull-Rom through the points, for soft hills."""
    out = []
    pts = [points[0]] + points + [points[-1]]
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for k in range(steps):
            t = k / steps
            out.append(tuple(0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t * t
                                    + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t ** 3) for j in range(2)))
    out.append(points[-1])
    return out


def art(d: ImageDraw.ImageDraw, kind: str, ox: int, oy: int, w: int, h: int) -> None:
    """The picture on the right of the pretend site, in the site's own colours."""
    style = KINDS[kind]["art"]
    P = lambda x, y: ((ox + x) * S, (oy + y) * S)  # noqa: E731
    if style == "hills":
        d.ellipse((*P(400, 100), *P(540, 240)), fill=rgb("#f1d9a6"))
        for top, colour in ((280, "#cdd8c6"), (360, "#a9bca3"), (450, "#7f977c")):
            ridge = _curve([(0, top + 60), (160, top - 10), (330, top + 20), (480, top - 30), (w, top - 20)])
            d.polygon([P(x, y) for x, y in ridge] + [P(w, h), P(0, h)], fill=rgb(colour))
        x, y = 250, 392
        d.rectangle((*P(x, y + 34), *P(x + 86, y + 92)), fill=rgb("#efe9dc"))
        d.polygon([P(x - 8, y + 38), P(x + 43, y), P(x + 94, y + 38)], fill=rgb("#5d6b62"))
        d.rectangle((*P(x + 34, y + 62), *P(x + 52, y + 92)), fill=rgb("#5d6b62"))
        for wx in (10, 60):
            d.rectangle((*P(x + wx, y + 50), *P(x + wx + 16, y + 64)), fill=rgb("#f1d9a6"))
    elif style == "flowers":
        d.line([P(30, h), P(110, 470), P(200, 380)], fill=rgb("#8aa58a"), width=6 * S, joint="curve")
        for cx, cy, r, colour in ((300, 150, 90, "#f2c4c0"), (420, 270, 70, "#e9a7a3"), (260, 310, 110, "#f6d7d2"),
                                  (470, 110, 46, "#e4b7a0"), (180, 190, 54, "#efc9b4"), (380, 420, 80, "#ecb9b5")):
            petals = Image.new("RGBA", (int(r * 2.6 * S), int(r * 2.6 * S)), (0, 0, 0, 0))
            pd = ImageDraw.Draw(petals)
            c = r * 1.3 * S
            for angle in range(0, 360, 72):
                petal = Image.new("RGBA", petals.size, (0, 0, 0, 0))
                ImageDraw.Draw(petal).ellipse((c - r * .42 * S, c - r * 1.15 * S, c + r * .42 * S, c + r * .25 * S), fill=rgb(colour, 230))
                petals.alpha_composite(petal.rotate(angle, center=(c, c)))
            pd.ellipse((c - r * .22 * S, c - r * .22 * S, c + r * .22 * S, c + r * .22 * S), fill=rgb("#c98a6b"))
            d._image.alpha_composite(petals, (int((ox + cx) * S - c), int((oy + cy) * S - c)))
    elif style == "cake":
        # left of the chat window, which covers the lower right
        d.ellipse((*P(200, 120), *P(560, 480)), fill=rgb("#efe2cf"))
        cx, base = 150, 560
        for tw, th, colour in ((170, 62, "#e8c9b8"), (124, 52, "#f2dccd"), (84, 44, "#f7ebe0")):
            top = base - th
            d.rounded_rectangle((*P(cx - tw / 2, top), *P(cx + tw / 2, base)), radius=12 * S, fill=rgb(colour))
            d.rectangle((*P(cx - tw / 2, top + 12), *P(cx + tw / 2, top + 20)), fill=rgb("#d9a48f"))
            base = top
        d.rectangle((*P(cx - 5, base - 36), *P(cx + 5, base)), fill=rgb("#f3d36b"))
        d.ellipse((*P(cx - 8, base - 54), *P(cx + 8, base - 34)), fill=rgb("#f0a04b"))
        d.rounded_rectangle((*P(cx - 110, 560), *P(cx + 110, 572)), radius=6 * S, fill=rgb("#cdb59c"))
    elif style == "drops":
        for cx, cy, r, colour in ((360, 230, 140, "#ead9de"), (470, 360, 95, "#dcc0c8"), (270, 400, 70, "#f1e3e6"),
                                  (520, 170, 45, "#cfa9b4")):
            d.ellipse((*P(cx - r, cy - r), *P(cx + r, cy + r)), fill=rgb(colour))
        d.rounded_rectangle((*P(330, 260), *P(390, 430)), radius=18 * S, fill=rgb("#b98b98"))
        d.rounded_rectangle((*P(344, 226), *P(376, 266)), radius=6 * S, fill=rgb("#8f6672"))
    elif style == "paws":
        d.ellipse((*P(190, 70), *P(570, 450)), fill=rgb("#dbe8e3"))
        for cx, cy, sc in ((380, 270, 1.0), (250, 150, .55), (500, 140, .45), (520, 420, .5)):
            colour = rgb("#7e9b92") if sc == 1.0 else rgb("#a8c0b8")
            d.ellipse((*P(cx - 60 * sc, cy - 40 * sc), *P(cx + 60 * sc, cy + 50 * sc)), fill=colour)
            for dx, dy in ((-62, -70), (-22, -98), (22, -98), (62, -70)):
                d.ellipse((*P(cx + dx * sc - 20 * sc, cy + dy * sc - 24 * sc), *P(cx + dx * sc + 20 * sc, cy + dy * sc + 24 * sc)), fill=colour)
    else:  # maths
        d.ellipse((*P(160, 0), *P(460, 300)), fill=rgb("#dfe6f2"))
        for i, (bw, colour) in enumerate(((250, "#c9d4e8"), (210, "#aebcd8"), (170, "#8fa0c4"))):
            d.rounded_rectangle((*P(310 - bw / 2 + 25, 380 - i * 34), *P(310 + bw / 2 + 25, 414 - i * 34)), radius=6 * S, fill=rgb(colour))
        text(d, (ox + 285, oy + 70), "π", "cjk", 96, rgb("#1f2a44"))
        text(d, (ox + 395, oy + 60), "x²", "cjk", 40, rgb("#5b667d"))
        text(d, (ox + 210, oy + 60), "A+", "serif", 40, rgb("#5b667d"))


def render(lead: dict) -> bytes:
    kind = kind_of(lead) or "bnb"
    k = KINDS[kind]
    name = lead["name"]
    city = city_of(lead)
    site = (lead.get("site") or "").strip()
    if not site:
        domain = (lead.get("email") or "@").split("@")[1]
        free = ("gmail", "hotmail", "live", "outlook", "yahoo", "btinternet", "icloud", "me")
        site = "your-website.com" if domain.split(".")[0] in free or not domain else domain
    site = site.replace("https://", "").replace("http://", "").removeprefix("www.").rstrip("/")[:40]
    q, a = chat_for({**lead, "cat": kind})
    host = (lead.get("host") or "").strip() or DEFAULT_HOST[kind]

    img = Image.new("RGBA", (W * S, H * S), rgb("#e9ebef"))
    # the window's shadow
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((50 * S, 66 * S, 1150 * S, 728 * S), radius=18 * S, fill=(20, 30, 50, 70))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(26 * S)))
    # the window, its content clipped to the rounded corners
    win = Image.new("RGBA", (1120 * S, 676 * S), rgb(k["bg"]))
    d = ImageDraw.Draw(win)
    d.rectangle((0, 0, 1120 * S, 46 * S), fill=rgb("#f4f4f6"))
    d.line((0, 46 * S, 1120 * S, 46 * S), fill=rgb("#e3e3e8"), width=S)
    for i, colour in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        d.ellipse(((18 + i * 20) * S, 17 * S, (30 + i * 20) * S, 29 * S), fill=rgb(colour))
    rr(d, (350, 9, 770, 37), 8, rgb("#ffffff"), outline=rgb("#e3e3e8"))
    url = "🔒 " if False else ""
    tw = width_of(site, "sans", 13)
    text(d, (560 - tw / 2, 15), url + site, "sans", 13, rgb("#6e6e73"))

    top = 46
    art(d, kind, 480, top, 640, 630)
    # nav
    text(d, (48, top + 26), name if width_of(name, "serif", 28) < 520 else name[:30] + "…", "serif", 28, rgb(k["ink"]))
    x = 1072
    for item in reversed(k["nav"]):
        x -= width_of(item, "sans", 14)
        text(d, (x, top + 36), item, "sans", 14, rgb(k["soft"]))
        x -= 28
    # hero
    text(d, (48, top + 128), f"{k['label']} · {city}".upper(), "sans", 14, rgb(k["soft"]), spacing=1.8)
    size = 68
    if _cjk(name):
        size = 54
    while size > 40 and len(wrap(name, "serif", size, 520)) > 2:
        size -= 4
    lines = wrap(name, "serif", size, 520)[:3]
    y = top + 160
    for i, line in enumerate(lines):
        if i == len(lines) - 1 and " " in line and not _cjk(line):
            head, last = line.rsplit(" ", 1)
            text(d, (48, y), head, "serif", size, rgb(k["ink"]))
            text(d, (48 + width_of(head + " ", "serif", size), y), last, "italic", size, rgb(k["ink"]))
        else:
            text(d, (48, y), line, "serif" if not (i == len(lines) - 1 and not _cjk(line)) else "italic", size, rgb(k["ink"]))
        y += size * (1.22 if _cjk(line) else 1.02)
    y += 18
    for line in wrap(k["sub"].format(city=city), "sans", 18, 500):
        text(d, (48, y), line, "sans", 18, rgb(k["soft"]))
        y += 27
    y += 26
    bw = width_of(k["cta"], "medium", 15) + 52
    rr(d, (48, y, 48 + bw, y + 46), 23, rgb(k["ink"]))
    text(d, (74, y + 13), k["cta"], "medium", 15, rgb(k["bg"]))

    # the chat window, bottom right
    cw, cx = 372, 1120 - 28 - 372
    qa_q = wrap(q, "sans", 14, 300)
    qa_a = wrap(a, "sans", 14, 290)
    line_h = 20
    body_h = 16 + 16 + 10 + (len(qa_q) * line_h + 22) + 10 + (len(qa_a) * line_h + 22) + 16
    ch = 70 + body_h + 56
    cy = 676 - 24 - ch
    card_shadow = Image.new("RGBA", win.size, (0, 0, 0, 0))
    ImageDraw.Draw(card_shadow).rounded_rectangle((cx * S, (cy + 14) * S, (cx + cw) * S, (cy + ch + 10) * S), radius=22 * S, fill=(0, 0, 0, 80))
    win.alpha_composite(card_shadow.filter(ImageFilter.GaussianBlur(20 * S)))
    d = ImageDraw.Draw(win)
    rr(d, (cx, cy, cx + cw, cy + ch), 22, rgb("#ffffff"))
    d.rectangle((cx * S, (cy + 70) * S, (cx + cw) * S, (cy + 70 + body_h) * S), fill=rgb("#fbfbfc"))
    d.line((cx * S, (cy + 70) * S, (cx + cw) * S, (cy + 70) * S), fill=rgb("#f0f0f2"), width=S)
    d.ellipse(((cx + 18) * S, (cy + 16) * S, (cx + 56) * S, (cy + 54) * S), fill=rgb(k["ink"]))
    initial = name.removeprefix("The ")[:1].upper()
    iw = width_of(initial, "serif", 20)
    text(d, (cx + 37 - iw / 2, cy + 22), initial, "serif", 20, rgb(k["bg"]))
    short = name
    while width_of(f"{short} · Assistant", "bold", 15) > cw - 90 and len(short) > 4:
        short = short[:-2].rstrip(" &,-")
    title = f"{short} · Assistant" if short == name else f"{short}… · Assistant"
    text(d, (cx + 68, cy + 17), title, "bold", 15, rgb("#1d1d1f"))
    d.ellipse(((cx + 68) * S, (cy + 41) * S, (cx + 76) * S, (cy + 49) * S), fill=rgb("#1fa84a"))
    text(d, (cx + 81, cy + 37), "Online now · replies 24/7", "medium", 12, rgb("#1fa84a"))
    y = cy + 70 + 16
    stamp = "Tonight, 11:42 PM"
    text(d, (cx + cw / 2 - width_of(stamp, "sans", 11) / 2, y), stamp, "sans", 11, rgb("#86868b"))
    y += 26
    qw = max(width_of(line, "sans", 14) for line in qa_q) + 28
    qh = len(qa_q) * line_h + 22
    rr(d, (cx + cw - 16 - qw, y, cx + cw - 16, y + qh), 18, rgb("#0071e3"))
    for i, line in enumerate(qa_q):
        text(d, (cx + cw - 16 - qw + 14, y + 10 + i * line_h), line, "sans", 14, rgb("#ffffff"))
    y += qh + 10
    aw = max(width_of(line, "sans", 14) for line in qa_a) + 28
    ah = len(qa_a) * line_h + 22
    rr(d, (cx + 16, y, cx + 16 + aw, y + ah), 18, rgb("#ffffff"), outline=rgb("#ececf0"))
    for i, line in enumerate(qa_a):
        text(d, (cx + 30, y + 10 + i * line_h), line, "sans", 14, rgb("#1d1d1f"))
    y = cy + 70 + body_h + 2
    rr(d, (cx + 16, y, cx + cw - 16, y + 40), 14, rgb("#f0f8f2"))
    d.ellipse(((cx + 28) * S, (y + 9) * S, (cx + 50) * S, (y + 31) * S), fill=rgb("#1fa84a"))
    text(d, (cx + 34, y + 11), "✓", "cjk", 13, rgb("#ffffff"))
    sent = f"Enquiry sent to {host} — reply in one tap"
    text(d, (cx + 60, y + 11), sent, "sans", 13, rgb("#1d1d1f"))

    mask = Image.new("L", win.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, win.size[0], win.size[1]), radius=18 * S, fill=255)
    img.paste(win, (40 * S, 36 * S), mask)
    d = ImageDraw.Draw(img)
    text(d, (40, 750), "Mockup for ", "sans", 15, rgb("#6e6e73"))
    x = 40 + width_of("Mockup for ", "sans", 15)
    text(d, (x, 750), name, "bold", 15, rgb("#1d1d1f"))
    x += width_of(name, "bold", 15)
    text(d, (x, 750), " · a 24/7 assistant on your website", "sans", 15, rgb("#6e6e73"))
    text(d, (1160 - width_of("by Vincent", "sans", 15), 750), "by Vincent", "sans", 15, rgb("#6e6e73"))

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
