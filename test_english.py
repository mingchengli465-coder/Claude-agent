"""English-speaking countries (UK, Ireland, Australia, New Zealand, US, Canada) get English-only pages;
Singapore, Hong Kong and Malaysia the 繁體 + English ones. No network: run `python test_english.py`.
"""
import asyncio, os, pathlib, re, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242"})
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import demos
import outreach as om
import trials
import web

CJK = re.compile(r"[㐀-鿿]")
TMP = pathlib.Path(tempfile.mkdtemp())


def links(text):
    return re.findall(r"https://mingchengli465-coder\.github\.io/Claude-agent/[^\s)\"<]*", text)


def english_link(url):
    return re.search(r"/(demo-[a-z]+-en|t-en|video-en|trial-en)\.html|[?&]lang=en", url) is not None


# --- the letters ---------------------------------------------------------------------------------
uk = {"email": "jill@galegreen.test", "name": "Gale Green Cottage", "region": "uk", "cat": "bnb", "host": "Jill"}
au = {"email": "hi@rosies.test", "name": "Rosie's Cakes", "region": "au", "cat": "bakery"}
for lead in (uk, au, {**uk, "personal": om.trial_link("gale-green-1234", uk)}):
    _, body = om.compose(lead)
    found = links(body)
    assert found and all(english_link(u) for u in found), found
    assert not CJK.search(body)
    _, follow = om.compose_followup(lead)
    assert all(english_link(u) for u in links(follow)), links(follow)
assert om.trial_link("x-1", uk) == "https://mingchengli465-coder.github.io/Claude-agent/t-en.html?s=x-1&from=email"
designer = {"email": "hello@studio.test", "name": "Studio North", "region": "uk", "kind": "agency", "cat": "bnb",
            "client": "Gale Green Cottage", "client_site": "https://galegreen.test/"}
_, body = om.compose(designer)
assert all(english_link(u) for u in links(body)), links(body)
system = om.reply_messages(uk, "Re: hi", "How much?")[0]["content"]
assert all(english_link(u) for u in links(system)), links(system)

hk = {"email": "info@vive.test", "name": "Vive Cake Boutique", "region": "hk"}
sg = {"email": "a@b.test", "name": "Glow Studio", "region": "sg", "cat": "beauty"}
for lead in (hk, sg, {**sg, "personal": om.trial_link("glow-1234", sg)}):
    _, body = om.compose(lead)
    assert links(body) and not any(english_link(u) for u in links(body)), links(body)
    assert CJK.search(body), "繁體 + English"
assert om.trial_link("x-1", hk) == "https://mingchengli465-coder.github.io/Claude-agent/t.html?s=x-1&from=email"
print("PASS English-speaking countries' emails link only English pages; Singapore / Hong Kong / Malaysia the bilingual ones")

# the how-to for a trial made on the English page is in English only
subject, body = om.compose_trial("Rose Cottage", "https://x.test/t-en.html?s=r-1", "<script></script>", english=True)
assert subject == "Your AI assistant for Rose Cottage is ready" and not CJK.search(subject + body) and "US$70" in body
subject, body = om.compose_trial("Rose Cottage", "https://x.test/t.html?s=r-1", "<script></script>")
assert CJK.search(subject) and "繁體中文" in body


# --- the pages, on the server -----------------------------------------------------------------------
async def server():
    store, db, told = cs.Store(TMP / "c.sqlite3"), trials.Trials(TMP / "t.sqlite3"), []

    async def owner(text):
        return 1

    async def on_trial(event, row):
        told.append(row)

    svc = cs.CustomerService(store, cs.load_catalog(), None, owner_notify=owner)
    chat = web.WebChat(svc, title=web.shop_name(), trials=db, on_trial=on_trial,
                       trial_service=lambda r: cs.CustomerService(store, trials.catalog_for(r), None, owner_notify=owner))
    row = db.create("Gale Green Cottage", "bnb")
    async with TestClient(TestServer(chat.app())) as client:
        for path in ("/demo-bnb-en", "/demo-florist-en", "/trial-en", "/video-en", f"/t/{row['slug']}?lang=en"):
            r = await client.get(path)
            page = await r.text()
            assert r.status == 200 and not CJK.search(page), (path, CJK.findall(page)[:10])
            assert '<html lang="en">' in page, path
        page = await (await client.get("/demo-bnb-en")).text()
        assert 'window.DEEP_LANG = "en"' in page and '"trial-en"' in page and '"name": "Willow Cottage B&B"' in page
        assert 'data-en="video-en"' in page and "Willow Cottage B&amp;B" in page
        assert CJK.search(await (await client.get("/demo-bnb")).text()), "the bilingual page stays"
        assert (await client.get("/demo-nope-en")).status == 404
        shop = await (await client.get(f"/t/{row['slug']}?lang=en")).text()
        assert 'window.DEEP_LANG = "en"' in shop and 'w.dataset.lang = ZH ? "zh-TW" : "en"' in shop and "var SHOP = null" in shop
        form = await (await client.get("/trial-en")).text()
        assert 'lang: "en"' in form and '"/t/" + res[1].slug + "?lang=en&new=1"' in form
        r = await client.post("/api/trial", json={"name": "Rose Cottage", "info": "Double £95", "lang": "en"})
        assert r.status == 200 and told[-1]["lang"] == "en"
        await client.post("/api/trial", json={"name": "Rose Two", "info": "Double £95"})
        assert told[-1]["lang"] == ""

asyncio.run(server())
print("PASS the English pages are served with no Chinese at all; the bilingual ones stay")

# --- the published site ------------------------------------------------------------------------------
sys.path.insert(0, str(pathlib.Path(__file__).parent / "tools"))
import build_pages
out = TMP / "_site"
build_pages.build(out)
for name in ["t-en.html", "trial-en.html", "video-en.html"] + [f"demo-{k}-en.html" for k in demos.DEMOS]:
    page = (out / name).read_text(encoding="utf-8")
    assert not CJK.search(page), (name, CJK.findall(page)[:10])
assert f'var API = "{build_pages.API}";' in (out / "t-en.html").read_text(encoding="utf-8")
assert 'href="demo.html?lang=en"' in (out / "video-en.html").read_text(encoding="utf-8")
sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
assert "/demo-bnb-en.html</loc>" in sitemap and "/trial-en.html</loc>" in sitemap
assert "lang=en" in (out / "index.html").read_text(encoding="utf-8"), "the homepage opens in English with ?lang=en"
print("PASS the English pages are published with the site")
print("\nALL ENGLISH PAGE TESTS PASSED")
