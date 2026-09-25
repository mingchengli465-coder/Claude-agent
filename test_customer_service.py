"""Customer-service mode, end to end, with a fake model and a fake owner.

No network, no keys: run `python test_customer_service.py`.
"""
import asyncio, inspect, json, os, pathlib, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import customer_service as cs

# --- the module stays channel-agnostic -----------------------------------------
src = inspect.getsource(cs)
assert "import telegram" not in src and "from telegram" not in src, "customer_service.py must not depend on Telegram"
print("PASS customer_service.py has no Telegram dependency")

# --- products.yaml -------------------------------------------------------------
catalog = cs.load_catalog(pathlib.Path(__file__).with_name("products.yaml"))
import yaml
data = yaml.safe_load(pathlib.Path(__file__).with_name("products.yaml").read_text(encoding="utf-8"))
names = [s["名称"] for s in data["服务"]]
assert names == ["写代码", "做 PPT", "写文案 / 小红书文案", "做简单网站", "帮商户搭建 AI 客服", "帮你装好 Claude Code / Codex"], names
for s in data["服务"]:
    for field in ("说明", "价格区间", "交付周期", "需要客户提供"):
        assert field in s, (s["名称"], field)
for field in ("付款方式", "修改次数", "常见问题"):
    assert field in data, field
assert "例如" not in catalog, "comments (examples) must never reach the model"
assert "https://t.me/Vinceeeeentttt" in catalog and "Vinc100327" in catalog, "the AI can hand out the owner's contacts"
for s in data["服务"]:
    assert cs.PLACEHOLDER not in str(s["价格区间"]), (s["名称"], "every service has a price the AI can quote")
system = cs.build_system_prompt(catalog)
assert catalog in system and "只能依据" in system and "待填" in system
# Prices the catalog has are answered, never handed off; vague messages get a question back.
for rule in ("不许因为这些问题转给本人", "把几种都报出来", "不要转给本人", "联系本人"):
    assert rule in system, rule
print(f"PASS products.yaml has the {len(names)} services with every field; examples stay out of the prompt ({catalog.count('待填')} blanks)")


# --- fakes -------------------------------------------------------------------------
class FakeModel:
    def __init__(self): self.calls, self.next = [], None
    async def __call__(self, system, messages):
        self.calls.append((system, messages))
        if isinstance(self.next, Exception):
            raise self.next
        return self.next or cs.Decision(reply="好的～你想做几页、什么时候要呀？", summary="PPT｜未说明｜未说明")

class Owner:
    def __init__(self): self.notices, self.next_id = [], 1000
    async def __call__(self, text):
        self.next_id += 1
        self.notices.append((self.next_id, text))
        return self.next_id

class Clock:
    def __init__(self): self.t = 1_760_000_000.0
    def __call__(self): return self.t

def fresh(responder="model"):
    model, owner, clock = FakeModel(), Owner(), Clock()
    store = cs.Store(pathlib.Path(tempfile.mkdtemp()) / "cs.sqlite3")
    svc = cs.CustomerService(store, catalog, model if responder == "model" else None,
                             owner_notify=owner, clock=clock)
    sent = []
    async def send(chat_id, text): sent.append((chat_id, text))
    svc.register_channel("telegram", send)
    return svc, model, owner, clock, sent

def msg(text, chat="555", user="xiaohong", name="小红"):
    return cs.Inbound(channel="telegram", chat_id=chat, text=text, username=user, display_name=name)

run = asyncio.run

# --- a normal answer ---------------------------------------------------------------
svc, model, owner, clock, sent = fresh()
reply = run(svc.handle(msg("你好，想做个PPT")))
assert reply == "好的～你想做几页、什么时候要呀？", reply
system_sent, messages = model.calls[-1]
assert catalog in system_sent
assert messages == [{"role": "user", "content": "你好，想做个PPT"}], messages
assert svc.store.customer("telegram", "555")["summary"] == "PPT｜未说明｜未说明"
assert len(owner.notices) == 1 and "有客户来咨询了" in owner.notices[0][1], owner.notices
for must in ("Telegram", "小红", "Chat ID：555", "你好，想做个PPT", reply, "AI 正在接待"):
    assert must in owner.notices[0][1], (must, owner.notices[0][1])
assert svc.lookup_owner_message(owner.notices[0][0]) == ("telegram", "555"), "replying to the notice reaches them"
clock.t += 30
run(svc.handle(msg("大概10页")))
assert len(owner.notices) == 1, "the rest of the conversation doesn't bother the owner"
print("PASS an ordinary question is answered by the model; the owner hears once that someone is asking")

# --- context: the last 10 messages, customer turn first and last ----------------------
for i in range(8):
    clock.t += 30
    run(svc.handle(msg(f"第{i}条")))
_, messages = model.calls[-1]
assert len(messages) <= cs.CS_HISTORY, len(messages)
assert messages[0]["role"] == "user" and messages[-1] == {"role": "user", "content": "第7条"}, messages[-1]
print(f"PASS the model sees at most {cs.CS_HISTORY} recent messages, starting with a customer turn")

# --- the model asks for a handoff ------------------------------------------------------
svc, model, owner, clock, sent = fresh()
run(svc.handle(msg("做一个毕业答辩PPT，下周五要，预算300")))
model.next = cs.Decision(reply="", handoff=True, reason="complex", summary="答辩PPT｜下周五｜300元")
reply = run(svc.handle(msg("里面要放很多实验数据和动画，能做吗")))
assert reply == cs.HANDOFF_TEXT, reply
notice_id, notice = owner.notices[-1]
for must in ("需求复杂", "小红", "@xiaohong", "Chat ID：555", "答辩PPT｜下周五｜300元", "实验数据", "回复这条消息"):
    assert must in notice, (must, notice)
assert svc.lookup_owner_message(notice_id) == ("telegram", "555")
print("PASS a handoff replies the fixed line and sends the owner name, chat ID, summary and transcript")

# --- keywords hand off even when the model wouldn't -------------------------------------
for text, reason in (("好的我要下单，怎么付款", "要下单/付款"), ("能便宜点吗", "砍价"),
                     ("你是真人吗？我想找本人聊", "要求找真人")):
    svc, model, owner, clock, sent = fresh()
    assert run(svc.handle(msg(text))) == cs.HANDOFF_TEXT, text
    assert reason in owner.notices[-1][1], (text, owner.notices[-1][1])
print("PASS ordering/paying, bargaining and asking for a human always reach the owner")

# --- the owner replies; the customer gets it; the AI sees it next time -------------------
svc, model, owner, clock, sent = fresh()
run(svc.handle(msg("能便宜点吗")))
notice_id = owner.notices[-1][0]
assert run(svc.deliver_owner_reply(notice_id, "可以，给你打九折～")) == ("telegram", "555")
assert sent == [("555", "可以，给你打九折～")], sent
clock.t += 10
model.next = None
run(svc.handle(msg("好的谢谢")))
_, messages = model.calls[-1]
assert {"role": "assistant", "content": "【本人回复】可以，给你打九折～"} in messages, messages
assert run(svc.deliver_owner_reply(99999, "x")) is None, "an unrelated reply is not forwarded"
print("PASS the owner's reply reaches the customer and becomes context the AI can rely on")

# --- /ai off: no AI reply, every message goes to the owner -------------------------------
svc, model, owner, clock, sent = fresh()
run(svc.handle(msg("在吗")))
found, key = svc.set_ai("555", False)
assert found and key == ("telegram", "555")
calls = len(model.calls)
assert run(svc.handle(msg("我想加急"))) is None
assert len(model.calls) == calls, "AI paused must not call the model"
assert "我想加急" in owner.notices[-1][1] and "AI 已暂停" in owner.notices[-1][1]
assert svc.lookup_owner_message(owner.notices[-1][0]) == ("telegram", "555")
assert svc.set_ai("555", True)[0]
assert run(svc.handle(msg("在吗"))) is not None
assert svc.set_ai("404", True)[0] is False
print("PASS /ai off forwards the customer to the owner silently; /ai on resumes; unknown IDs are reported")

# --- rate limit: 5 a minute, a single polite warning, then silence -------------------------
svc, model, owner, clock, sent = fresh()
replies = []
for i in range(8):
    clock.t += 1
    replies.append(run(svc.handle(msg(f"刷屏{i}"))))
assert all(r for r in replies[:5]), replies[:5]
assert replies[5] == cs.RATE_LIMITED_TEXT.format(limit=5), replies[5]
assert replies[6] is None and replies[7] is None, replies[6:]
assert len(model.calls) == 5, "no model calls over the limit"
clock.t += 61
assert run(svc.handle(msg("过了一分钟"))) is not None
print("PASS a customer gets 5 messages a minute, one polite warning, then quiet until the window passes")

# --- the model fails: the customer is told, the owner is told --------------------------------
svc, model, owner, clock, sent = fresh()
model.next = RuntimeError("boom")
assert run(svc.handle(msg("在吗"))) == cs.HANDOFF_TEXT
assert "AI 出错" in owner.notices[-1][1]
print("PASS a model error hands off instead of going silent")

# --- no API key: everything goes to the owner, the canned line at most every 10 min ----------
svc, model, owner, clock, sent = fresh(responder=None)
assert run(svc.handle(msg("在吗"))) == cs.HANDOFF_TEXT
clock.t += 5
assert run(svc.handle(msg("有人吗"))) is None, "don't repeat the canned line"
assert len(owner.notices) == 2, "but the owner hears about every message"
clock.t += cs.NO_AI_REPLY_EVERY
assert run(svc.handle(msg("还在吗"))) == cs.HANDOFF_TEXT
print("PASS without ANTHROPIC_API_KEY every message is handed to the owner")

# --- attachments ---------------------------------------------------------------------------------
svc, model, owner, clock, sent = fresh()
reply, owner_id = run(svc.handle_attachment(msg("这是老师的要求"), "图片"))
assert reply == cs.ATTACHMENT_TEXT and owner_id and "发来文件" in owner.notices[-1][1]
assert "[图片] 这是老师的要求" in svc.transcript(("telegram", "555"))
print("PASS a customer's file is acknowledged and flagged to the owner")

# --- /customers ----------------------------------------------------------------------------------
svc, model, owner, clock, sent = fresh()
run(svc.handle(msg("想做网站", chat="1", name="阿明", user="aming")))
clock.t += 60
model.next = cs.Decision(reply="好呀，要几页？", summary="网站｜未说明｜500元")
run(svc.handle(msg("预算500", chat="2", name="小芳", user="")))
report = svc.customers_report()
assert report.index("小芳") < report.index("阿明"), "newest first"
for must in ("网站｜未说明｜500元", "@aming", "Chat ID：2", "AI 开", "最后消息"):
    assert must in report, (must, report)
assert svc.customers_report() and cs.CustomerService(cs.Store(":memory:"), catalog, None).customers_report() == "还没有客户来咨询。"
print("PASS /customers lists the newest customers with summary and last message time")

# --- another channel plugs in with no Telegram code --------------------------------------------------
svc, model, owner, clock, sent = fresh()
wecom_sent = []
async def wecom_send(chat_id, text): wecom_sent.append((chat_id, text))
svc.register_channel("wecom", wecom_send)
run(svc.handle(cs.Inbound(channel="wecom", chat_id="wx_abc", text="能便宜点吗", display_name="企业微信客户")))
notice_id, notice = owner.notices[-1]
assert "Chat ID：wecom:wx_abc" in notice and "/ai wecom:wx_abc off" in notice
run(svc.deliver_owner_reply(notice_id, "可以聊～"))
assert wecom_sent == [("wx_abc", "可以聊～")] and sent == []
assert svc.set_ai("wecom:wx_abc", False) == (True, ("wecom", "wx_abc"))
print("PASS a second channel (企业微信) works through register_channel + handle, no Telegram involved")

# --- the real Claude request ------------------------------------------------------------------------
class FakeMessages:
    def __init__(self, response): self.response, self.kwargs = response, None
    async def create(self, **kw):
        self.kwargs = kw
        return self.response

def claude_with(response):
    r = cs.ClaudeResponder.__new__(cs.ClaudeResponder)
    r.model, r.effort = cs.CS_MODEL, cs.CS_EFFORT
    fm = FakeMessages(response)
    r.client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=fm))
    return r, fm

payload = {"reply": "你好呀～", "handoff": False, "reason": "", "summary": "PPT｜未说明｜未说明"}
ok = types.SimpleNamespace(stop_reason="end_turn", stop_details=None,
                           content=[types.SimpleNamespace(type="text", text=json.dumps(payload, ensure_ascii=False))])
r, fm = claude_with(ok)
d = run(r(system, [{"role": "user", "content": "你好"}]))
assert d == cs.Decision(reply="你好呀～", handoff=False, reason="", summary="PPT｜未说明｜未说明"), d
kw = fm.kwargs
assert kw["model"] == "claude-opus-5"
assert kw["output_config"]["format"]["type"] == "json_schema"
assert kw["output_config"]["format"]["schema"] == cs.DECISION_SCHEMA
assert kw["output_config"]["effort"] == "low"
assert kw["thinking"] == {"type": "adaptive"}
assert kw["fallbacks"] == "default" and kw["betas"] == ["server-side-fallback-2026-07-01"]
assert kw["system"][0]["text"] == system and kw["system"][0]["cache_control"] == {"type": "ephemeral"}
assert "temperature" not in kw and "budget_tokens" not in json.dumps(kw["thinking"])

refused = types.SimpleNamespace(stop_reason="refusal", stop_details=types.SimpleNamespace(category="cyber"), content=[])
r, fm = claude_with(refused)
d = run(r(system, [{"role": "user", "content": "x"}]))
assert d.handoff and d.reason == "out_of_scope"
print("PASS the Claude request uses structured output, adaptive thinking at low effort, caching and fallbacks; a refusal hands off")

assert cs.parse_customer_ref("123") == ("telegram", "123")
assert cs.parse_customer_ref("wecom:abc") == ("wecom", "abc")

# --- other languages ---------------------------------------------------------------------------------
assert cs.detect_lang("你好，想做PPT") == "zh"
assert cs.detect_lang("Hi, I need a pitch deck") == "en"
assert cs.detect_lang("Bonjour, je voudrais un site") == "en", "non-Chinese falls back to the English lines"
assert cs.detect_lang("👍👍 123") == ""
assert "写繁体中文就用繁体，写英文就用英文" in system and "CNY" in system
print("PASS the model is told to answer in the customer's language, prices in CNY")

svc, model, owner, clock, sent = fresh()
model.next = cs.Decision(reply="Sure! How many slides, and when do you need it?", summary="PPT｜未说明｜未说明")
assert run(svc.handle(msg("Hi, I need slides for a class talk"))) == "Sure! How many slides, and when do you need it?"
model.next = None
for text, reason in (("How do I pay?", "要下单/付款"), ("Any discount if I order two?", "砍价"),
                     ("Can I talk to a real person?", "要求找真人")):
    clock.t += 30
    assert run(svc.handle(msg(text))) == cs.CANNED["en"]["handoff"], text
    assert reason in owner.notices[-1][1], (text, owner.notices[-1][1])
clock.t += 30
assert run(svc.handle(msg("in order to make it clear, the talk is 10 minutes"))) != cs.CANNED["en"]["handoff"], \
    "'in order to' is not an order"
clock.t += 30
reply, _ = run(svc.handle_attachment(msg(""), "文件"))
assert reply == cs.CANNED["en"]["attachment"], "a file with no caption keeps the customer's language"
print("PASS English customers get English handoffs, and English ordering/bargaining/human requests reach the owner")

svc, model, owner, clock, sent = fresh()
replies = []
for i in range(6):
    clock.t += 1
    replies.append(run(svc.handle(msg(f"message {i}"))))
assert replies[5] == cs.CANNED["en"]["rate_limited"].format(limit=5), replies[5]
svc, model, owner, clock, sent = fresh(responder=None)
assert run(svc.handle(cs.Inbound(channel="telegram", chat_id="9", text="👍", lang="en"))) == cs.CANNED["en"]["handoff"], \
    "no text to go on: use the channel's hint"
print("PASS the rate-limit notice and the no-AI line follow the customer's language")

# --- a database from before `lang` existed is upgraded in place --------------------------------------
import sqlite3
old_db = pathlib.Path(tempfile.mkdtemp()) / "old.sqlite3"
con = sqlite3.connect(old_db)
con.execute("CREATE TABLE customers (channel TEXT NOT NULL, chat_id TEXT NOT NULL, username TEXT DEFAULT '', "
            "display_name TEXT DEFAULT '', ai_enabled INTEGER DEFAULT 1, summary TEXT DEFAULT '', first_seen REAL, "
            "last_message_at REAL, last_ai_off_reply REAL DEFAULT 0, PRIMARY KEY (channel, chat_id))")
con.execute("INSERT INTO customers (channel, chat_id, first_seen, last_message_at) VALUES ('telegram', '1', 1, 1)")
con.commit(); con.close()
upgraded = cs.Store(old_db)
upgraded.set_lang("telegram", "1", "en")
assert upgraded.customer("telegram", "1")["lang"] == "en"
print("PASS an existing conversation database gains the language column without losing data")

assert "Alipay" in catalog and "银行卡" in catalog
print("PASS overseas customers are told they can pay by Alipay or card")

# --- DeepSeek (OpenAI-compatible, JSON mode) ----------------------------------------------------------
class FakeCompletions:
    def __init__(self, content, finish="stop"): self.content, self.finish, self.kwargs = content, finish, None
    async def create(self, **kw):
        self.kwargs = kw
        m = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=m, finish_reason=self.finish)])

def deepseek_with(content, finish="stop"):
    r = cs.OpenAICompatibleResponder.__new__(cs.OpenAICompatibleResponder)
    r.model = cs.CS_DEEPSEEK_MODEL
    fc = FakeCompletions(content, finish)
    r.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fc))
    return r, fc

r, fc = deepseek_with(json.dumps({"reply": "可以的～几页呀？", "handoff": False, "reason": "",
                                  "summary": "PPT｜未说明｜未说明"}, ensure_ascii=False))
d = run(r(system, [{"role": "user", "content": "想做PPT"}]))
assert d == cs.Decision(reply="可以的～几页呀？", handoff=False, reason="", summary="PPT｜未说明｜未说明"), d
kw = fc.kwargs
assert kw["model"] == "deepseek-flash" and kw["response_format"] == {"type": "json_object"}
assert kw["messages"][0]["role"] == "system" and kw["messages"][0]["content"].startswith(system)
assert "JSON" in kw["messages"][0]["content"], "json_object mode needs the word JSON in the prompt"
assert kw["messages"][1:] == [{"role": "user", "content": "想做PPT"}]

# tolerant of fences and chatter, strict about the shape
assert cs.parse_decision('```json\n{"reply":"hi","handoff":false,"reason":"","summary":""}\n```').reply == "hi"
assert cs.parse_decision('好的：{"reply":"","handoff":"true","reason":"bargain","summary":"x"}').handoff is True
assert cs.parse_decision('{"reply":"x","handoff":false,"reason":"made_up","summary":""}').reason == ""
for bad in ("我不知道", "[1, 2]", ""):
    try:
        cs.parse_decision(bad)
        raise AssertionError(f"accepted {bad!r}")
    except ValueError:
        pass
r, fc = deepseek_with('{"reply": "被截', finish="length")
try:
    run(r(system, [{"role": "user", "content": "x"}]))
    raise AssertionError("a truncated reply must not be used")
except RuntimeError:
    pass

# end to end: garbage from the model hands off instead of reaching the customer
svc, model, owner, clock, sent = fresh()
svc.responder, _ = deepseek_with("抱歉我无法回答")
assert run(svc.handle(msg("在吗"))) == cs.HANDOFF_TEXT and "AI 出错" in owner.notices[-1][1]
print("PASS DeepSeek uses JSON mode; fences are tolerated, anything unusable hands off")

# --- a customer never waits on a hung model ----------------------------------------------------------
class HangingCompletions:
    async def create(self, **kw):
        await asyncio.sleep(3600)
r = cs.OpenAICompatibleResponder.__new__(cs.OpenAICompatibleResponder)
r.model = cs.CS_DEEPSEEK_MODEL
r.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=HangingCompletions()))
saved = cs.CS_REQUEST_TIMEOUT
cs.CS_REQUEST_TIMEOUT = 0.05
svc, model, owner, clock, sent = fresh()
svc.responder = r
assert run(svc.handle(msg("在吗"))) == cs.HANDOFF_TEXT
assert "AI 出错" in owner.notices[-1][1]
cs.CS_REQUEST_TIMEOUT = saved
print("PASS a hung model hands the customer to the owner at the deadline")

# --- new-customer notice: once per conversation, again after a long silence ----------------
svc, model, owner, clock, sent = fresh()
run(svc.handle(msg("在吗")))
clock.t += 3600
run(svc.handle(msg("还在吗")))
assert len(owner.notices) == 1, owner.notices
clock.t += cs.CS_NEW_SESSION_GAP
run(svc.handle(msg("我又来了")))
assert len(owner.notices) == 2 and "有客户来咨询了" in owner.notices[-1][1] and "我又来了" in owner.notices[-1][1]
svc, model, owner, clock, sent = fresh()
assert run(svc.handle(msg("我要下单，怎么付款"))) == cs.HANDOFF_TEXT
assert len(owner.notices) == 1 and "需要你接手" in owner.notices[0][1], "a handoff is the only notice"
assert cs.HANDOFF_TEXT in owner.notices[0][1], "the notice shows what the customer was told"
print("PASS the owner hears about a new customer once, again after a long silence, and never twice for one message")

# --- purchase intent: the owner hears each time it rises, the AI keeps chatting -------------------
svc, model, owner, clock, sent = fresh()
run(svc.handle(msg("你们做网站吗")))
model.next = cs.Decision(reply="做的～个人主页69-99元，要几页呀？", summary="网站｜未说明｜未说明", intent="interested")
clock.t += 30
assert run(svc.handle(msg("多少钱？大概什么时候能好"))) == "做的～个人主页69-99元，要几页呀？"
notice = owner.notices[-1][1]
assert len(owner.notices) == 2 and "🔥 客户有购买意向" in notice, owner.notices
for must in ("小红", "网站｜未说明｜未说明", "多少钱", "AI 还在继续聊", "回复这条消息"):
    assert must in notice, (must, notice)
assert svc.lookup_owner_message(owner.notices[-1][0]) == ("telegram", "555")
clock.t += 30
run(svc.handle(msg("三页就行")))
assert len(owner.notices) == 2, "the same level doesn't notify again"
model.next = cs.Decision(reply="好的～我请本人跟你确认开工时间", summary="网站3页｜未说明｜99元", intent="ready")
clock.t += 30
run(svc.handle(msg("那就做这个吧")))
assert len(owner.notices) == 3 and "💰 客户准备购买了" in owner.notices[-1][1], owner.notices[-1]
model.next = cs.Decision(reply="", handoff=True, reason="complex", summary="网站3页｜未说明｜99元", intent="ready")
clock.t += 30
run(svc.handle(msg("可以加个后台吗")))
assert "购买意向：准备购买" in owner.notices[-1][1], "a handoff notice says how keen they are"
assert "准备购买" in svc.customers_report()
# A brand-new customer who is keen straight away gets one notice, not two.
svc, model, owner, clock, sent = fresh()
model.next = cs.Decision(reply="可以的～要几页？", summary="PPT｜明天｜未说明", intent="ready")
run(svc.handle(msg("明天要一个PPT，直接开始吧")))
assert len(owner.notices) == 1 and "💰 客户准备购买了（Telegram，新客户）" in owner.notices[0][1], owner.notices
# Keen-ness starts over in a new conversation.
clock.t += cs.CS_NEW_SESSION_GAP
model.next = cs.Decision(reply="你好呀", intent="interested")
run(svc.handle(msg("你好")))
assert "🔥 客户有购买意向" in owner.notices[-1][1]
print("PASS the owner is told when a customer gets interested and again when they're ready to buy")

# --- the model's intent is read from both providers' JSON ---------------------------------------------
assert cs.parse_decision('{"reply": "hi", "handoff": false, "reason": "", "summary": "", "intent": "ready"}').intent == "ready"
assert cs.parse_decision('{"reply": "hi", "intent": "very keen"}').intent == "", "unknown values are ignored"
assert cs.parse_decision('{"reply": "hi"}').intent == ""
assert "intent" in cs.DECISION_SCHEMA["required"] and "intent" in cs.JSON_FORMAT_SUFFIX
print("PASS intent comes through JSON mode and the schema")

# --- messages_after: what the web chat window polls -----------------------------------------------------
store = cs.Store(":memory:")
store.touch_customer("web", "v1", "", "", 1.0)
a = store.add_message("web", "v1", "customer", "hi", 1.0)
b = store.add_message("web", "v1", "assistant", "hello", 2.0)
c = store.add_message("web", "v1", "owner", "I'm here", 3.0)
store.add_message("web", "v2", "owner", "someone else", 3.0)
assert [r["text"] for r in store.messages_after("web", "v1", a)] == ["hello", "I'm here"]
assert [r["id"] for r in store.messages_after("web", "v1", 0)] == [a, b, c]
assert store.messages_after("web", "v1", c) == []
print("PASS messages can be read back after a given id, per customer")

print("\nALL CUSTOMER SERVICE TESTS PASSED")
