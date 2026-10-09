"""The daily world news: reading RSS / RDF / YouTube feeds, the AI's pick, the message, once a day.
No network, no keys: run `python test_news.py`.
"""
import asyncio, datetime as dt, json, os, pathlib, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news

NOW = dt.datetime(2026, 10, 9, 0, 30, tzinfo=dt.timezone.utc)

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>BBC</title>
<item><title>Ceasefire agreed after talks in Cairo</title><link>https://www.bbc.co.uk/news/world-1</link>
<description><![CDATA[<p>Both sides agreed to a <b>ceasefire</b> &amp; prisoner swap.</p>]]></description>
<pubDate>Wed, 08 Oct 2026 21:00:00 GMT</pubDate></item>
<item><title>Earthquake hits coastal city</title><link>https://www.bbc.co.uk/news/world-2</link>
<pubDate>Wed, 08 Oct 2026 18:00:00 +0000</pubDate></item>
<item><title>Last week's story</title><link>https://www.bbc.co.uk/news/old</link>
<pubDate>Mon, 29 Sep 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""

RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
 xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>Central bank cuts rates</title><link>https://www.dw.com/en/rates</link>
<description>A surprise cut.</description><dc:date>2026-10-08T15:00:00Z</dc:date></item>
<item><title>Ceasefire agreed after talks in Cairo</title><link>https://www.dw.com/en/ceasefire</link>
<dc:date>2026-10-08T20:00:00Z</dc:date></item>
</rdf:RDF>"""

YT = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"
 xmlns:media="http://search.yahoo.com/mrss/"><title>BBC News</title>
<entry><yt:videoId>abc123XYZ_0</yt:videoId><title>Ceasefire deal explained</title>
<link rel="alternate" href="https://www.youtube.com/watch?v=abc123XYZ_0"/>
<published>2026-10-08T22:00:00+00:00</published><media:group><media:description>What the deal means</media:description></media:group></entry>
</feed>"""

# --- parsing ------------------------------------------------------------------------------
bbc = news.parse_feed(RSS, "BBC")
assert [i.title for i in bbc] == ["Ceasefire agreed after talks in Cairo", "Earthquake hits coastal city", "Last week's story"]
assert bbc[0].summary == "Both sides agreed to a ceasefire & prisoner swap.", bbc[0].summary
assert bbc[0].published == dt.datetime(2026, 10, 8, 21, tzinfo=dt.timezone.utc)
dw = news.parse_feed(RDF, "DW")
assert dw[0].title == "Central bank cuts rates" and dw[0].link == "https://www.dw.com/en/rates"
assert dw[0].published == dt.datetime(2026, 10, 8, 15, tzinfo=dt.timezone.utc)
yt = news.parse_feed(YT, "BBC News")
assert yt[0].link == "https://www.youtube.com/watch?v=abc123XYZ_0" and yt[0].summary == "What the deal means"
print("PASS RSS 2.0, RSS 1.0 (RDF) and YouTube's Atom feeds")

# --- fresh items only, every feed gets a turn, the same headline once -----------------------
mixed = news.recent([bbc, dw], NOW)
assert [i.title for i in mixed] == ["Ceasefire agreed after talks in Cairo", "Central bank cuts rates",
                                    "Earthquake hits coastal city"], [i.title for i in mixed]
print("PASS last week's story dropped, the duplicate ceasefire kept once, feeds interleaved")


# --- fetching: one broken feed doesn't stop the rest -------------------------------------------
class Resp:
    def __init__(self, status, body): self.status, self.body = status, body
    async def read(self): return self.body
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class Session:
    def __init__(self, pages): self.pages, self.asked = pages, []
    def get(self, url, headers=None):
        self.asked.append((url, headers))
        return Resp(*self.pages.get(url, (404, b"")))


many = b"<rss><channel>" + b"".join(
    f"<item><title>Story number {n}</title><link>https://n.example/{n}</link>"
    f"<pubDate>Wed, 08 Oct 2026 20:00:00 GMT</pubDate></item>".encode() for n in range(12)) + b"</channel></rss>"
pages = {"https://bbc/rss": (200, RSS), "https://dw/rdf": (200, RDF), "https://more/rss": (200, many),
         news.YT_FEED.format("UCbbc"): (200, YT)}
session = Session(pages)
lists, failed = asyncio.run(news.fetch_all([("BBC", "https://bbc/rss"), ("Gone", "https://gone/rss")], session))
assert len(lists[0]) == 3 and lists[1] == [] and failed == ["Gone（HTTP 404）"], failed
assert "vinc-news" in session.asked[0][1]["User-Agent"]
print("PASS a feed that fails is listed and skipped")


# --- the AI's pick becomes the message -------------------------------------------------------
class FakeAI:
    def __init__(self, answer=None, fail=False, base_url="https://openrouter.ai/api/v1"):
        self.answer, self.fail, self.prompts, self.kwargs, self.base_url = answer, fail, [], [], base_url
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    async def create(self, model, messages, **kw):
        self.prompts.append(messages[-1]["content"])
        self.kwargs.append(kw)
        if self.fail:
            raise RuntimeError("no balance")
        msg = types.SimpleNamespace(content=self.answer)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


answer = "好的：\n" + json.dumps({"events": [
    {"title": "开罗谈判达成停火", "summary": "双方同意停火并交换囚犯。", "source": 1, "video": 1, "video_title": "停火协议解读",
     "x_query": "Cairo ceasefire"},
    {"title": "央行意外降息", "summary": "降息出乎市场预期。", "source": "H2", "video": None, "x_query": "central bank rate cut"},
    {"title": "", "summary": "没有标题的会被跳过", "source": 3},
    {"title": "沿海城市地震", "summary": "", "source": 99, "video": 42, "x_query": ""},
]}, ensure_ascii=False)
broken, chatty, good = FakeAI(fail=True), FakeAI("好的，我来整理今天的新闻。", base_url="https://api.deepseek.com"), FakeAI(answer)
messages = asyncio.run(news.build_digest(
    [(broken, "deepseek-flash"), (chatty, "chatty"), (good, "free-model")], NOW, session=session,
    feeds=[("BBC", "https://bbc/rss"), ("DW", "https://dw/rdf"), ("More", "https://more/rss"), ("Gone", "https://gone/rss")],
    channels=[("BBC News", "UCbbc")], day=dt.date(2026, 10, 9)))
prompt = good.prompts[0]
assert "H1 [BBC] Ceasefire agreed after talks in Cairo" in prompt and "V1 [BBC News] Ceasefire deal explained" in prompt
assert "Last week's story" not in prompt
assert len(broken.prompts) == 2, "an erroring model: JSON mode, then plain, then the next model"
assert [k.get("extra_body") for k in chatty.kwargs] == [{"thinking": {"type": "disabled"}}] * 2 + [None], chatty.kwargs
assert "response_format" in chatty.kwargs[0] and "response_format" not in chatty.kwargs[1]
assert chatty.kwargs[2]["max_tokens"] == 32000, "DeepSeek with thinking on gets room to think, last"
assert good.kwargs[0]["response_format"] == {"type": "json_object"} and "extra_body" not in good.kwargs[0]
text = "\n".join(messages)
assert text.startswith("🌍 <b>今日世界十大事件</b>　10月9日 周五"), text[:60]
assert "1. <b>开罗谈判达成停火</b>\n双方同意停火并交换囚犯。" in text
assert '<a href="https://www.bbc.co.uk/news/world-1">原文·BBC</a>' in text
assert '<a href="https://www.youtube.com/watch?v=abc123XYZ_0">视频·BBC News：停火协议解读</a>' in text
assert 'href="https://x.com/search?q=Cairo+ceasefire&amp;src=typed_query&amp;f=top"' in text
assert "2. <b>央行意外降息</b>" in text and '<a href="https://www.dw.com/en/rates">原文·DW</a>' in text
assert 'href="https://www.youtube.com/results?search_query=central+bank+rate+cut&amp;sp=EgIIAg%3D%3D">YouTube 今日相关视频</a>' in text
assert "没有标题" not in text, "an event without a title is skipped"
assert "3. <b>沿海城市地震</b>" in text and "x.com/search?q=%E6%B2%BF" in text, "no keywords: search the title"
assert "（1 个来源今天没打开）" in text
print("PASS the AI's ten become a Chinese list with article, YouTube and X links")

# whatever the AI leaves in English gets a second, translating pass
english = json.dumps({"events": [
    {"title": "Ceasefire agreed in Cairo", "summary": "Both sides agreed to stop fighting.", "source": 1, "video": 1,
     "x_query": "Cairo ceasefire"},
    {"title": "央行意外降息", "summary": "降息出乎市场预期。", "source": 2, "video": None, "x_query": "rate cut"}]})
translation = json.dumps({"items": [{"title": "开罗达成停火", "summary": "双方同意停止交火。", "video_title": "停火协议解读"}]},
                         ensure_ascii=False)


class TwoAnswers(FakeAI):
    def __init__(self, *answers):
        super().__init__(answers[0]); self.answers = list(answers)
    async def create(self, model, messages, **kw):
        self.answer = self.answers[min(len(self.prompts), len(self.answers) - 1)]
        return await super().create(model, messages, **kw)


ai = TwoAnswers(english, translation)
out = "\n".join(asyncio.run(news.build_digest([(ai, "m")], NOW, session=session, channels=[("BBC News", "UCbbc")],
                                              feeds=[("BBC", "https://bbc/rss"), ("More", "https://more/rss")])))
assert "1. <b>开罗达成停火</b>\n双方同意停止交火。" in out and "Ceasefire agreed" not in out, out
assert "视频·BBC News：停火协议解读" in out and "2. <b>央行意外降息</b>" in out
assert len(ai.prompts) == 2 and "Ceasefire agreed in Cairo" in ai.prompts[1] and "央行意外降息" not in ai.prompts[1]
assert news.chinese("NATO 峰会在海牙召开") and not news.chinese("Ceasefire agreed in Cairo") and news.chinese("")
print("PASS anything left in English is translated into Chinese in a second pass")

# an empty answer whose JSON sits in the thinking is still used
thinker = types.SimpleNamespace(choices=[types.SimpleNamespace(finish_reason="stop", message=types.SimpleNamespace(
    content="", model_extra={"reasoning_content": "想一想… " + answer}))])
assert news._answer(thinker)[0].endswith("}") and "思考" in news._answer(thinker)[1]

# every model failing, or too few headlines, is an error to report, not an empty message
for kwargs, why in ((dict(attempts=[(FakeAI(fail=True), "m")]), "AI 都失败了"),
                    (dict(attempts=[(good, "m")], feeds=[("Gone", "https://gone/rss")]), "只拿到 0 条新闻")):
    try:
        asyncio.run(news.build_digest(kwargs.pop("attempts"), NOW, session=session, channels=[],
                                      feeds=kwargs.get("feeds", [("More", "https://more/rss")])))
    except RuntimeError as exc:
        assert why in str(exc), exc
    else:
        raise AssertionError(why)
print("PASS no AI or no news raises, so the bot can retry and then say so")

# --- long lists are split between events, never inside a link --------------------------------
items = [news.Event(f"事件{n}", "很长的摘要" * 120, news.Item("BBC", "t", f"https://e.example/{n}"), None, "q")
         for n in range(10)]
parts = news.render(items, dt.date(2026, 10, 9))
assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
assert all(p.count("<a ") == p.count("</a>") for p in parts)
assert sum(p.count("<b>事件") for p in parts) == 10
print(f"PASS a long list goes out as {len(parts)} messages, each under Telegram's limit")

# --- once a day ---------------------------------------------------------------------------------
eight = dt.time(8, 0)
shanghai = dt.timezone(dt.timedelta(hours=8))
assert news.due(dt.datetime(2026, 10, 9, 9, 15, tzinfo=shanghai), eight, "2026-10-08")
assert not news.due(dt.datetime(2026, 10, 9, 9, 15, tzinfo=shanghai), eight, "2026-10-09"), "already sent today"
assert not news.due(dt.datetime(2026, 10, 9, 7, 59, tzinfo=shanghai), eight, "2026-10-08"), "too early"
with tempfile.TemporaryDirectory() as tmp:
    state = news.State(pathlib.Path(tmp) / "sub" / "news_state.json")
    assert state.last() == ""
    state.mark(dt.date(2026, 10, 9))
    assert news.State(state.path).last() == "2026-10-09"
print("PASS sent once a day, and a restart after the time catches up")

# --- the bot: daily job, catch-up after a restart, /news, retries -------------------------------
tmp = tempfile.mkdtemp()
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "NEWS_CHAT_ID": "6368422599",
                   "NEWS_TIME": "08:00", "NEWS_TIMEZONE": "Asia/Shanghai",
                   "NEWS_STATE_PATH": os.path.join(tmp, "news_state.json")})
import bot
from telegram.ext import Application

app = Application.builder().token("123:fake").build()
bot.NEWS_TIME = "00:00"  # always past: today's not sent yet, so a catch-up is queued
bot.schedule_news(app)
names = sorted(j.name for j in app.job_queue.jobs())
assert names == ["news-catchup", "news-daily"], names
daily = next(j for j in app.job_queue.jobs() if j.name == "news-daily").job.trigger
assert str(daily.timezone) == "Asia/Shanghai"
bot.news_state().mark(dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date())
app2 = Application.builder().token("123:fake").build()
bot.schedule_news(app2)
assert [j.name for j in app2.job_queue.jobs()] == ["news-daily"], "already sent today: no catch-up"
print("PASS daily at 08:00 Asia/Shanghai, plus a catch-up only when today's is missing")


class FakeBot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append((chat_id, text, kw))


class Queue:
    def __init__(self): self.once = []
    def run_once(self, cb, when, data=None, name=None): self.once.append((when, data, name))


async def fake_digest(attempts, now, **kw):
    return ["🌍 <b>今日世界十大事件</b>", "第二条"]


async def failing_digest(attempts, now, **kw):
    raise RuntimeError("只拿到 3 条新闻")


bot.news_mod.build_digest = fake_digest
pathlib.Path(os.environ["NEWS_STATE_PATH"]).unlink()
fb, queue = FakeBot(), Queue()
ctx = types.SimpleNamespace(bot=fb, job=types.SimpleNamespace(data=None), job_queue=queue)
asyncio.run(bot.news_job(ctx))
assert [(c, t) for c, t, _ in fb.sent] == [(6368422599, "🌍 <b>今日世界十大事件</b>"), (6368422599, "第二条")]
assert fb.sent[0][2] == {"parse_mode": "HTML", "disable_web_page_preview": True}
asyncio.run(bot.news_job(ctx))
assert len(fb.sent) == 2, "sent once a day, even if the job runs again"

bot.news_mod.build_digest = failing_digest
pathlib.Path(os.environ["NEWS_STATE_PATH"]).unlink()
fb, queue = FakeBot(), Queue()
asyncio.run(bot.news_job(types.SimpleNamespace(bot=fb, job=types.SimpleNamespace(data=None), job_queue=queue)))
assert queue.once == [(1800, {"attempt": 1}, "news-retry")] and fb.sent == [], "first failure: try again in 30 min"
asyncio.run(bot.news_job(types.SimpleNamespace(bot=fb, job=types.SimpleNamespace(data={"attempt": 2}), job_queue=queue)))
assert "今天的世界新闻没整理出来：只拿到 3 条新闻" in fb.sent[-1][1], "last try failed: the chat is told why"
print("PASS sent once, retried twice on failure, then the chat is told")

assert 'CommandHandler("news", news_command)' in pathlib.Path(bot.__file__).read_text(encoding="utf-8")
print("PASS /news is registered")
print("ALL PASS")
