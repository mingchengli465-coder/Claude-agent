"""The owner's agent: websites and post drafts, with a scripted fake model.
No network, no keys: run `python test_agent.py`.
"""
import asyncio, json, os, pathlib, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp.test_utils import TestClient, TestServer

import agent as ag
import customer_service as cs
import web

PAGE = "<!doctype html><html><head><title>Mia 蛋糕</title></head><body><h1>Mia 蛋糕</h1></body></html>"


def call(name, args, cid="c1"):
    return types.SimpleNamespace(id=cid, function=types.SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)))


def reply(content="", calls=None):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content, tool_calls=calls))])


class FakeModel:
    """Answers chat calls from `script` and page calls (those without tools) from `pages`."""
    def __init__(self, script, pages=()):
        self.script, self.pages, self.seen = list(script), list(pages), []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))
    async def create(self, model, messages, **kw):
        self.seen.append((messages, kw))
        return self.script.pop(0) if "tools" in kw else reply(self.pages.pop(0))


# --- helpers -------------------------------------------------------------------------------
assert ag.clean_html("```html\n" + PAGE + "\n```") == PAGE
assert ag.clean_html("Here you go:\n" + PAGE + "\nEnjoy") == PAGE
try:
    ag.clean_html("sorry, I can't"); raise AssertionError("no page must raise")
except ValueError:
    pass
assert ag.page_title(PAGE) == "Mia 蛋糕"
store = ag.SiteStore(pathlib.Path(tempfile.mkdtemp()) / "s.sqlite3")
assert store.free_slug("Cake Shop!") == "cake-shop" and store.free_slug("蛋糕").startswith("site-")
print("PASS pages are cut out of the model's answer; slugs are safe and unique")


async def main():
    drafts, progress = [], []
    async def on_draft(chat_id, text, platforms): drafts.append((chat_id, text, platforms))
    async def on_progress(chat_id, text): progress.append(text)

    # build a site
    model = FakeModel([reply(calls=[call("make_website", {"request": "蛋糕店网站，粉色", "slug": "mia-cake"})]),
                       reply("做好了：https://bot.example/s/mia-cake")], pages=["```html\n" + PAGE + "\n```"])
    a = ag.Agent(model, "m", store, "https://bot.example/", ["x", "channel"], on_draft, on_progress)
    out = await a.run(42, [], "帮我做一个蛋糕店网站，粉色")
    assert "mia-cake" in out and store.get("mia-cake")["html"] == PAGE and store.get("mia-cake")["chat_id"] == "42"
    tool_msg = model.seen[2][0][-1]  # [0] chat, [1] the page itself, [2] chat again
    assert tool_msg["role"] == "tool" and json.loads(tool_msg["content"])["url"] == "https://bot.example/s/mia-cake"
    assert model.seen[1][0][1]["content"] == "蛋糕店网站，粉色", "the request goes to the page builder"
    assert progress and "网站" in progress[0]
    assert "X、Telegram 频道" in model.seen[0][0][0]["content"], "the model is told which platforms work"
    print("PASS make_website: generated, stored under a slug and the link comes back")

    # edit it; someone else's chat can't
    new = PAGE.replace("Mia 蛋糕", "Mia Bakery")
    model = FakeModel([reply(calls=[call("edit_website", {"slug": "mia-cake", "change": "改成英文"})]), reply("改好了")],
                      pages=[new])
    a.client = model
    assert await a.run(42, [], "改成英文") == "改好了"
    assert store.get("mia-cake")["html"] == new and "【现在的 HTML】" in model.seen[1][0][1]["content"]
    other = await a.edit_website(99, "mia-cake", "x")
    assert other["ok"] is False
    assert (await a.list_websites(42))["sites"][0]["url"] == "https://bot.example/s/mia-cake"
    print("PASS edit_website keeps the address; other chats can't edit or see it")

    # drafts are only drafts
    model = FakeModel([reply(calls=[call("draft_post", {"text": "新品上市！", "platforms": ["x", "bluesky", "channel"]})]),
                       reply("草稿发你了，点发布就会发出去")])
    a.client = model
    out = await a.run(42, [], "发一条新品帖子到所有平台")
    assert drafts == [(42, "新品上市！", ["x", "channel"])], "only platforms that are set up"
    assert "发布" in out
    print("PASS draft_post only drafts, and only for platforms that are set up")

    # a failing tool is reported to the model, not raised
    model = FakeModel([reply(calls=[call("make_website", {"request": "x"})]), reply("出了点问题")], pages=["no html here"])
    a.client = model
    assert await a.run(42, [], "做网站") == "出了点问题"
    assert "error" in json.loads(model.seen[2][0][-1]["content"])
    print("PASS a failed tool becomes an error the model explains")

    # the site is served, sandboxed
    chat = web.WebChat(types.SimpleNamespace(register_channel=lambda *a: None), sites=store)
    async with TestClient(TestServer(chat.app())) as client:
        r = await client.get("/s/mia-cake")
        assert r.status == 200 and "Mia Bakery" in await r.text()
        assert r.headers["Content-Security-Policy"].startswith("sandbox"), "model-written pages run in their own origin"
        assert (await client.get("/s/nope")).status == 404
    print("PASS /s/<slug> serves the site in a sandbox")

    # a personal-assistant bot's address serves its websites and nothing of the owner's
    chat = web.WebChat(None, sites=store)
    async with TestClient(TestServer(chat.app())) as client:
        assert (await client.get("/s/mia-cake")).status == 200
        home = await client.get("/")
        assert home.status == 200 and "vinc" not in (await home.text()).lower()
        for path in ("/demo", "/widget.js", "/privacy"):
            assert (await client.get(path)).status == 404, path
        assert (await client.post("/api/chat", json={"v": "abcdefgh1", "text": "hi"})).status in (404, 405)
    print("PASS a personal bot's address serves only its own websites")

asyncio.run(main())
# --- the fixed lines follow AGENT_LANG ---------------------------------------------------------
ag.AGENT_LANG = "fr"
assert ag.ui("publish") == "✅ Publier" and "site" in ag.ui("building") and ag.platform_name("channel") == "chaîne Telegram"
assert "français" in ag.AGENT_SYSTEM.format(platforms="X", lang=ag.ui("name"))
for lang in ag.UI:
    assert set(ag.UI[lang]) == set(ag.UI["zh"]), lang
ag.AGENT_LANG = "xx"
assert ag.ui("publish") == "✅ 发布", "an unknown language falls back to Chinese"
ag.AGENT_LANG = "zh"
print("PASS the agent's buttons and notices come in the bot's language (zh / en / fr)")

print("\nALL AGENT TESTS PASSED")
