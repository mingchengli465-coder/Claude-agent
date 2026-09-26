"""Website visitor numbers: classification, the owner's own devices, the report, /api/hit.

No network, no keys: run `python test_visits.py`.
"""
import asyncio, datetime as dt, os, pathlib, sys, tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aiohttp.test_utils import TestClient, TestServer

import customer_service as cs
import visits as vm
import web

TZ = ZoneInfo("Asia/Shanghai")
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
WECHAT = IPHONE + " MicroMessenger/8.0.50 NetType/WIFI Language/zh_CN"


class Clock:
    def __init__(self): self.t = dt.datetime(2026, 9, 26, 10, 0, tzinfo=TZ).timestamp()
    def __call__(self): return self.t


# --- where people came from ---------------------------------------------------------------
assert vm.classify("https://t.co/abc", IPHONE) == "X"
assert vm.classify("https://x.com/Vincent40769988/status/1", IPHONE) == "X"
assert vm.classify("", WECHAT) == "微信"
assert vm.classify("", IPHONE + " Telegram-iOS") == "Telegram" or vm.classify("https://t.me/x", IPHONE) == "Telegram"
assert vm.classify("https://t.me/emilyhanbot", IPHONE) == "Telegram"
assert vm.classify("https://www.google.com/", IPHONE) == "搜索引擎"
assert vm.classify("https://www.baidu.com/s?wd=ai", IPHONE) == "搜索引擎"
assert vm.classify("", IPHONE, "xhs") == "小红书", "?from=xhs on a link the owner hands out"
assert vm.classify("", IPHONE) == "直接打开"
assert vm.classify("https://mingchengli465-coder.github.io/Claude-agent/", IPHONE) == "直接打开", "moving between own pages"
assert vm.classify("https://example.com/blog/post", IPHONE) == "example.com"
for ua in ("TelegramBot (like TwitterBot)", "Mozilla/5.0 (compatible; Applebot/0.1)",
           "Mozilla/5.0 (Linux; Android 12; WeTestEQ4 Build/SP1A) MicroMessenger/8.0", "python-requests/2.31", ""):
    assert vm.is_bot(ua), ua
assert not vm.is_bot(IPHONE) and not vm.is_bot(WECHAT)
assert vm.page_name("/Claude-agent/demo.html") == "AI 客服演示页" and vm.page_name("/") == "首页"
print("PASS sources, pages and bots are told apart")

# --- counting, the owner left out -----------------------------------------------------------
path = pathlib.Path(tempfile.mkdtemp()) / "cs.sqlite3"
store = cs.Store(path)
clock = Clock()
v = vm.Visits(path, clock=clock)
yesterday = clock.t - 86400
clock.t = yesterday
assert v.record("owner-ipad-1", "/", "", IPHONE)
assert v.record("owner-ipad-1", "/demo", "", IPHONE)
assert v.record("friend-0001", "/", "", WECHAT)
assert v.record("friend-0001", "/demo", "https://mingchengli465-coder.github.io/", WECHAT)
assert v.record("tweeter-001", "/Claude-agent/", "https://t.co/xyz", IPHONE, lang="en-US")
assert not v.record("scanner-001", "/", "", "Mozilla/5.0 (compatible; Applebot/0.1)")
store.add_message("web", "tweeter-001", "customer", "how much?", clock.t)
store.add_message("web", "tweeter-001", "assistant", "US$70–85", clock.t)
store.add_message("web", "tweeter-001", "customer", "ok", clock.t)
store.add_message("web", "owner-ipad-1", "customer", "测试", clock.t)
clock.t = yesterday + 86400  # today, 10:00

assert not v.mark_owner("owner-ipad-1", "wrong"), "only the secret link marks a device"
assert v.mark_owner("owner-ipad-1", v.owner_token()) and v.owner_devices() == 1
assert v.owner_token() == v.owner_token(), "the token is stable"

report = v.report(TZ)
assert report.startswith("📊 9月25日 网站访客"), report
assert "来了 2 个人，一共打开页面 3 次" in report, report
assert "微信 1" in report and "X 1" in report, report
assert "首页 2" in report and "AI 客服演示页 1" in report, report
assert "1 个人的手机/电脑不是中文" in report, report
assert "跟 AI 客服聊天：1 人，发了 2 条消息" in report, report
assert "最近 7 天：2 人" in report, report
assert v.take_flag("sent") and not v.take_flag("sent"), "the owner's links are sent once"
empty = vm.Visits(pathlib.Path(tempfile.mkdtemp()) / "x.sqlite3", clock=clock).report(TZ, today_too=True)
assert "没有人来看网站" in empty and "今天到现在：0 人" in empty, empty
print("PASS the report counts people once, leaves the owner and bots out, and counts chats")


# --- over HTTP ------------------------------------------------------------------------------
class NoModel:
    async def __call__(self, system, messages): return cs.Decision(reply="hi")


async def http():
    p = pathlib.Path(tempfile.mkdtemp()) / "cs.sqlite3"
    st = cs.Store(p)
    svc = cs.CustomerService(st, cs.load_catalog(), NoModel(), owner_notify=lambda t: None)
    counter = vm.Visits(p)
    chat = web.WebChat(svc, visits=counter)
    V = "0f8e2c3a-1111-2222-3333-444455556666"
    async with TestClient(TestServer(chat.app())) as client:
        for page in ("/", "/demo"):
            assert 'data-track' in await (await client.get(page)).text(), "the owner's pages count views"
        r = await client.post("/api/hit", json={"v": V, "page": "/", "ref": "https://t.co/a", "lang": "en"},
                              headers={"User-Agent": IPHONE})
        assert r.status == 200 and (await r.json()) == {"ok": True, "owner": False}
        assert r.headers["Access-Control-Allow-Origin"] == "*", "the GitHub Pages copy posts here too"
        r = await client.post("/api/hit", json={"v": V, "page": "/", "me": counter.owner_token()},
                              headers={"User-Agent": IPHONE})
        assert (await r.json())["owner"] is True and counter.owner_devices() == 1
        assert (await client.post("/api/hit", json={"v": "<bad>"})).status == 400
        assert (await client.post("/api/hit", data="nope")).status == 400
        for _ in range(web.IP_HITS_PER_MINUTE):
            await client.post("/api/hit", json={"v": V, "page": "/"}, headers={"User-Agent": IPHONE})
        assert (await client.post("/api/hit", json={"v": V, "page": "/"})).status == 429, "one IP can't flood it"
    rows = counter.db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    assert rows >= 2
    # a site that only embeds the chat window is never counted
    js = (pathlib.Path(__file__).parent / "web_static" / "widget.js").read_text(encoding="utf-8")
    assert 'hasAttribute("data-track")' in js
    demo = (pathlib.Path(__file__).parent / "web_static" / "demo.html").read_text(encoding="utf-8")
    assert "/widget.js\" defer><\\/script>" in demo, "the snippet merchants copy has no data-track"
    print("PASS /api/hit records views and marks the owner's device")

asyncio.run(http())
print("\nALL VISIT TESTS PASSED")
