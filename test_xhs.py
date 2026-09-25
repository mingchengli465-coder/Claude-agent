"""Exercise the 小红书 note pipeline: JSON parsing, normalising, cover rendering.

No network and no credentials needed: run `python test_xhs.py`.
"""
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("XHS_STATE_FILE", os.path.join(tempfile.mkdtemp(), "state.json"))

from PIL import Image

import xhs

# --- JSON extraction tolerates fences and chatter --------------------------
payload = {"title": "标题", "body": "正文", "tags": ["a"], "cover": {}}
raw = json.dumps(payload, ensure_ascii=False)
for wrapped in (raw, f"```json\n{raw}\n```", f"好的，这是结果：\n{raw}\n希望有帮助"):
    assert xhs._extract_json(wrapped)["title"] == "标题", wrapped[:30]
print("PASS _extract_json handles bare / fenced / chatty replies")

for bad in ("完全不是 JSON", "```\nnot json\n```", "{broken"):
    try:
        xhs._extract_json(bad)
        raise AssertionError(f"{bad!r} should have been rejected")
    except xhs.GenerationError:
        pass
print("PASS malformed replies raise GenerationError")

# --- the parser must survive how small models actually fail ----------------
GOOD = {"topic": "t", "title": "标题", "body": "第一段\n\n第二段",
        "tags": ["a", "b"], "cover": {"main": ["一", "二"], "question": "q?",
                                      "small": ["s1", "s2", "s3"]}}
raw = json.dumps(GOOD, ensure_ascii=False)

cases = {
    "bare": raw,
    "fenced": f"```json\n{raw}\n```",
    "fenced, no language tag": f"```\n{raw}\n```",
    "unterminated fence": f"```json\n{raw}",
    "preamble": f"好的，这是你要的笔记：\n\n{raw}",
    "postamble with a brace": f"{raw}\n\n希望有帮助！如果要改格式 {{ 告诉我 }}",
    "both sides": f"当然可以！\n```json\n{raw}\n```\n还需要我调整吗？",
    "trailing comma in object": raw.replace('"cover":', '"extra": 1, "cover":').replace("}}", "},}"),
    "literal newlines in body": raw.replace("第一段\\n\\n第二段", "第一段\n\n第二段"),
}
for label, payload in cases.items():
    try:
        got = xhs._extract_json(payload)
    except xhs.GenerationError as exc:
        raise AssertionError(f"{label!r} should have parsed, got {exc}") from exc
    assert got["title"] == "标题", f"{label}: {got}"
    assert got["body"].startswith("第一段"), f"{label}: {got['body'][:20]!r}"
print(f"PASS parser recovers from {len(cases)} real-world malformations:")
for label in cases:
    print(f"       · {label}")

# a brace inside prose must not be mistaken for the object
tricky = f"这里有个 {{ 花括号\n\n{raw}"
assert xhs._extract_json(tricky)["title"] == "标题", "must find the real object"
print("PASS a stray brace in the preamble doesn't derail extraction")

# --- genuinely unparseable input still fails, and logs the raw text --------
import logging
class Capture(logging.Handler):
    def __init__(self): super().__init__(); self.msgs = []
    def emit(self, r): self.msgs.append(r.getMessage())

cap = Capture()
xhs.logger.addHandler(cap)
xhs.logger.setLevel(logging.ERROR)
for label, bad in {
    "empty": "",
    "whitespace only": "   \n  ",
    "pure prose": "抱歉，我不能帮你写这个内容。",
    "broken json": "{\"title\": \"x\", \"body\": }",
}.items():
    try:
        xhs._extract_json(bad)
        raise AssertionError(f"{label!r} should have been rejected")
    except xhs.GenerationError:
        pass
xhs.logger.removeHandler(cap)
assert len(cap.msgs) >= 4, cap.msgs
assert any("抱歉，我不能帮你写这个内容" in m for m in cap.msgs), "raw reply must reach the log"
assert any("空内容" in m for m in cap.msgs), "an empty reply must say so"
print("PASS unparseable input is rejected AND the raw reply is logged for debugging")

# the log excerpt is capped
long_junk = "不是 JSON " * 500
cap2 = Capture(); xhs.logger.addHandler(cap2)
try: xhs._extract_json(long_junk)
except xhs.GenerationError: pass
xhs.logger.removeHandler(cap2)
logged = [m for m in cap2.msgs if "不是 JSON" in m]
assert logged and len(logged[0]) < len(long_junk), "the log excerpt must be truncated"
print(f"PASS the logged excerpt is capped at {xhs.RAW_LOG_CHARS} chars, not the whole reply")

# --- normalising ------------------------------------------------------------
n = xhs._normalize({
    "topic": "话题", "title": "这个标题故意写得超过二十个字用来测试截断行为",
    "body": "正文内容", "tags": ["#带井号", "标签2", "标签3", "标签4",
                                "标签5", "标签6", "标签7", "标签8", "第九个要被丢掉"],
    "cover": {"main": ["一行", "二行", "三行多余"], "question": "问句",
              "small": ["小1", "小2", "小3", "小4"]},
}, "消费观")
assert len(n.title) == 20, len(n.title)
assert len(n.tags) == 8 and n.tags[0] == "带井号", n.tags
assert n.cover_main == ["一行", "二行"], n.cover_main
assert n.cover_small == ["小1", "小2", "小3"], n.cover_small
print("PASS normalize trims title/tags/cover lines to spec")

# too few tags get padded, missing cover fields get filled
n2 = xhs._normalize({"title": "短标题", "body": "正文", "tags": ["只有一个"], "cover": {}}, "消费观")
assert len(n2.tags) >= 6 and n2.cover_question and len(n2.cover_small) == 3
assert n2.cover_main, "cover main must never be empty"
print("PASS sparse replies are padded rather than crashing the cover")

for missing in ({"body": "x"}, {"title": "x"}, {"title": "", "body": ""}):
    try:
        xhs._normalize(missing, "消费观")
        raise AssertionError(f"{missing} should have been rejected")
    except xhs.GenerationError:
        pass
print("PASS empty title/body is a hard failure")

# --- domain rotation + recency ---------------------------------------------
st = {"domain_index": 0, "history": []}
seen = [xhs.take_domain(st) for _ in range(len(xhs.DOMAINS) + 2)]
assert seen[:len(xhs.DOMAINS)] == xhs.DOMAINS, seen
assert seen[len(xhs.DOMAINS)] == xhs.DOMAINS[0], "rotation must wrap"
print("PASS domains rotate in order and wrap:", " → ".join(seen[:3]), "…")

from datetime import date, timedelta
st = {"history": [
    {"date": (date.today() - timedelta(days=1)).isoformat(), "topic": "昨天", "title": "T1"},
    {"date": (date.today() - timedelta(days=6)).isoformat(), "topic": "六天前", "title": "T2"},
    {"date": (date.today() - timedelta(days=9)).isoformat(), "topic": "九天前", "title": "T3"},
    {"date": "not-a-date", "topic": "坏数据", "title": "T4"},
]}
rec = " ".join(xhs.recent_topics(st, days=7))
assert "昨天" in rec and "六天前" in rec, rec
assert "九天前" not in rec, "topics older than the window must drop out"
assert "坏数据" not in rec, "an unparseable date must not crash or leak in"
print("PASS recent_topics keeps a 7-day window and survives bad rows")

# --- wrapping ---------------------------------------------------------------
f = xhs.load_font(40, "Bold")
lines = xhs.wrap_text("这是一段没有任何换行符的长文本用来验证按字符换行是否正常工作", f, 300)
assert len(lines) > 1 and all(l for l in lines), lines
assert "".join(lines) == "这是一段没有任何换行符的长文本用来验证按字符换行是否正常工作"
print(f"PASS wrap_text splits CJK into {len(lines)} lines without losing characters")

punct = xhs.wrap_text("这句话结束了。下一句开始", xhs.load_font(40, "Bold"), 330)
assert not any(l.startswith("。") for l in punct), punct
print("PASS punctuation never starts a line")

# --- cover rendering: the hard requirement is nothing leaves the canvas -----
def check_cover(note, variant, label):
    png = xhs.render_cover(note, variant)
    img = Image.open(io.BytesIO(png))
    assert img.size == (1080, 1440), img.size
    # Any ink in the bottom 12px row band means content ran off the design.
    bottom = img.crop((0, 1428, 1080, 1440)).convert("RGB")
    colors = {c for _, c in bottom.getcolors(maxcolors=100000)}
    assert colors <= {xhs.YELLOW}, f"{label}: content reached the bottom edge: {colors}"
    return len(png)

normal = xhs.Note(
    domain="消费观", topic="t", title="标题", body="正文", tags=["a"],
    cover_main=["月薪三万", "还在吃泡面"], cover_question="这算会过日子吗？",
    cover_small=["攒钱是为了什么", "省下来的是时间吗", "我们到底在怕什么"],
)
# Deliberately abusive input: the spec says it must shrink or wrap, not overflow.
huge = xhs.Note(
    domain="消费观", topic="t", title="标题", body="正文", tags=["a"],
    cover_main=["这一行主标故意写得非常非常长用来测试自动缩小字号",
                "第二行同样很长很长很长很长很长很长很长很长"],
    cover_question="这个争议问句也被故意写得远远超过十五个字的限制用来压测红框",
    cover_small=["小字第一行也写得特别长特别长特别长特别长",
                 "小字第二行同样超长超长超长超长超长超长",
                 "小字第三行还是很长很长很长很长很长很长"],
)
for variant in range(3):
    size_n = check_cover(normal, variant, f"normal/v{variant}")
    size_h = check_cover(huge, variant, f"oversized/v{variant}")
    print(f"PASS cover variant {variant}: normal {size_n//1024}KB, oversized {size_h//1024}KB, both in bounds")

# variants must actually differ, or 换封面 does nothing
a, b = xhs.render_cover(normal, 0), xhs.render_cover(normal, 1)
assert a != b, "换封面 must produce a visibly different layout"
print("PASS the 换封面 variants render differently")

xhs.render_cover(normal, 0).__len__()  # cached fonts stay usable


# --- response_format=json_object with a fallback for models that reject it ---
import asyncio, types
import openai

calls = []

class FakeCompletions:
    def __init__(self, reject_json_mode): self.reject = reject_json_mode
    async def create(self, **kw):
        calls.append("json_object" if "response_format" in kw else "plain")
        if self.reject and "response_format" in kw:
            raise openai.BadRequestError(
                "response_format is not supported",
                response=types.SimpleNamespace(status_code=400, headers={},
                                               request=None, text=""),
                body=None,
            )
        payload = json.dumps(GOOD, ensure_ascii=False)
        msg = types.SimpleNamespace(content=payload)
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg, finish_reason="stop")]
        )

def fake_client(reject):
    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeCompletions(reject)))

# A model that supports it: one call, with response_format.
calls.clear(); xhs.client = fake_client(reject=False); xhs._json_mode_supported = True
note = asyncio.run(xhs._one_call("消费观", []))
assert calls == ["json_object"], calls
assert note.title == "标题"
print("PASS json_object is sent when the model accepts it")

# A model that rejects it: falls back within the same attempt, then remembers.
calls.clear(); xhs.client = fake_client(reject=True); xhs._json_mode_supported = True
note = asyncio.run(xhs._one_call("消费观", []))
assert calls == ["json_object", "plain"], calls
assert note.title == "标题", "the fallback call must still produce a note"
assert xhs._json_mode_supported is False, "the rejection must be remembered"
calls.clear()
asyncio.run(xhs._one_call("消费观", []))
assert calls == ["plain"], f"later calls must skip json_object: {calls}"
print("PASS a model rejecting json_object falls back in-place and isn't retried with it")

# Empty content is reported clearly rather than as "没有返回 JSON".
class EmptyCompletions:
    async def create(self, **kw):
        msg = types.SimpleNamespace(content="")
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=msg, finish_reason="length")])
xhs.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=EmptyCompletions()))
try:
    asyncio.run(xhs._one_call("消费观", []))
    raise AssertionError("empty content should raise")
except xhs.GenerationError as exc:
    assert "空内容" in str(exc), exc
print("PASS an empty reply says so instead of 模型没有返回 JSON")



# --- reasoning models: budget, params, and the empty-content fallback -------
class RecordingCompletions:
    """Captures kwargs and returns whatever message the test wants."""
    def __init__(self, message, reject=None):
        self.message, self.reject, self.seen = message, reject, []
    async def create(self, **kw):
        self.seen.append(kw)
        if self.reject:
            has_json = "response_format" in kw
            has_reason = "reasoning" in (kw.get("extra_body") or {})
            if (self.reject == "json" and has_json) or \
               (self.reject == "reasoning" and has_reason) or \
               (self.reject == "both" and (has_json or has_reason)):
                raise openai.BadRequestError(
                    self.reject_message,
                    response=types.SimpleNamespace(status_code=400, headers={},
                                                   request=None, text=""),
                    body=None,
                )
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=self.message, finish_reason="stop")])

def msg(content=None, **extra):
    m = types.SimpleNamespace(content=content, model_extra=extra)
    for k, v in extra.items():
        setattr(m, k, v)
    return m

def run_call(message, reject=None, reject_message="bad request"):
    comp = RecordingCompletions(message, reject)
    comp.reject_message = reject_message
    xhs.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp))
    xhs._json_mode_supported = True
    xhs._reasoning_supported = True
    return comp, asyncio.run(xhs._one_call("消费观", []))

payload = json.dumps(GOOD, ensure_ascii=False)

# 1. max_tokens and the reasoning block are sent
comp, note = run_call(msg(content=payload))
sent = comp.seen[0]
assert sent["max_tokens"] == 8000, sent["max_tokens"]
assert sent["extra_body"]["reasoning"] == {"exclude": True, "effort": "low"}, sent["extra_body"]
assert sent["response_format"] == {"type": "json_object"}
print(f"PASS request carries max_tokens={sent['max_tokens']} and reasoning={sent['extra_body']['reasoning']}")

# 2. empty content -> JSON recovered from the reasoning trace
for label, message in {
    "reasoning": msg(content="", reasoning=f"先想一下…\n\n{payload}"),
    "reasoning_content": msg(content=None, reasoning_content=payload),
    "reasoning_details": msg(content="", reasoning_details=[{"type": "t", "text": payload}]),
}.items():
    _, note = run_call(message)
    assert note.title == "标题", f"{label}: {note}"
print("PASS empty content falls back to reasoning / reasoning_content / reasoning_details")

# 3. empty content AND empty reasoning -> a clear error naming finish_reason
try:
    run_call(msg(content="", reasoning=""))
    raise AssertionError("should have raised")
except xhs.GenerationError as exc:
    assert "空内容" in str(exc) and "finish_reason" in str(exc), exc
print("PASS empty content with no reasoning reports finish_reason, not a parse error")

# 4. a model rejecting only `reasoning` keeps JSON mode
comp, note = run_call(msg(content=payload), reject="reasoning",
                      reject_message="unsupported parameter: reasoning")
assert len(comp.seen) == 2, comp.seen
assert "extra_body" not in comp.seen[1], comp.seen[1]
assert comp.seen[1]["response_format"] == {"type": "json_object"}, "json mode must survive"
assert xhs._reasoning_supported is False and xhs._json_mode_supported is True
print("PASS a reasoning-only rejection drops reasoning and keeps json_object")

# 5. a model rejecting only response_format keeps reasoning
comp, note = run_call(msg(content=payload), reject="json",
                      reject_message="response_format is not supported")
assert "response_format" not in comp.seen[1] and "extra_body" in comp.seen[1], comp.seen[1]
assert xhs._json_mode_supported is False and xhs._reasoning_supported is True
print("PASS a response_format-only rejection drops json mode and keeps reasoning")

# 6. an unhelpful 400 drops both rather than failing the attempt
comp, note = run_call(msg(content=payload), reject="both", reject_message="invalid request")
assert "response_format" not in comp.seen[1] and "extra_body" not in comp.seen[1], comp.seen[1]
assert note.title == "标题", "the degraded call must still produce a note"
print("PASS an ambiguous 400 drops both parameters and still returns a note")



# --- the empty-skeleton bug: a schema sketch must not beat the real note ----
skeleton = '{"topic": "", "title": "", "body": "", "tags": [], "cover": {}}'
real = json.dumps(GOOD, ensure_ascii=False)

# This is the shape a reasoning trace actually produces: plan, then output.
trace = f"我先想一下结构：\n{skeleton}\n\n好，现在正式写：\n{real}"
got = xhs._extract_json(trace)
assert got["title"] == "标题" and got["body"].strip(), f"picked the skeleton: {got}"
print("PASS a schema sketch before the real note no longer wins")

# Reversed order: the real note first, skeleton after, still picks the real one.
got = xhs._extract_json(f"{real}\n\n（模板留档）{skeleton}")
assert got["title"] == "标题", got
print("PASS a trailing skeleton doesn't override an earlier real note")

# Only a skeleton available: returned, but the error must name the keys.
try:
    xhs._normalize(xhs._extract_json(skeleton), "消费观")
    raise AssertionError("an empty skeleton must not normalize")
except xhs.GenerationError as exc:
    assert "标题或正文为空" in str(exc) and "键" in str(exc), exc
    assert "title" in str(exc), f"the error must list what came back: {exc}"
    skeleton_error = str(exc)   # `exc` is unbound once the block exits
print(f"PASS a bare skeleton fails with the keys named: {skeleton_error}")

# --- alternate key names ----------------------------------------------------
chinese = {"选题": "t", "标题": "中文键标题", "正文": "中文键正文",
           "标签": ["一", "二"], "封面": {"主标": ["甲", "乙"], "问句": "问？",
                                          "小字": ["s1", "s2", "s3"]}}
n = xhs._normalize(chinese, "消费观")
assert n.title == "中文键标题" and n.body == "中文键正文", n
assert n.cover_main == ["甲", "乙"] and n.cover_question == "问？", n
print("PASS Chinese key names are accepted")

alt = {"title": "x", "content": "用 content 当正文", "hashtags": "标签一, 标签二 标签三"}
n = xhs._normalize(alt, "消费观")
assert n.body == "用 content 当正文", n.body
assert "标签一" in n.tags and "标签三" in n.tags, n.tags
print("PASS 'content' as body and a comma-separated tag string both work")

# --- wrapper objects --------------------------------------------------------
wrapped = json.dumps({"note": GOOD}, ensure_ascii=False)
assert xhs._extract_json(wrapped)["title"] == "标题"
print("PASS a {\"note\": {...}} wrapper is unwrapped")

# --- truncation is named ----------------------------------------------------
class TruncatedCompletions:
    async def create(self, **kw):
        m = types.SimpleNamespace(content='{"title": "有标题", "body": ""',
                                  model_extra={})
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=m, finish_reason="length")])
xhs.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=TruncatedCompletions()))
xhs._json_mode_supported = xhs._reasoning_supported = False
try:
    asyncio.run(xhs._one_call("消费观", []))
    raise AssertionError("truncated output should raise")
except xhs.GenerationError as exc:
    assert "截断" in str(exc) and "XHS_MAX_TOKENS" in str(exc), exc
print("PASS finish_reason=length is reported as truncation, pointing at XHS_MAX_TOKENS")

# --- a failed first model retries on the fallback, and the SDK doesn't pile on --
# The 2:29 PM hang: a slow free model plus the SDK's own two retries at 120 s
# each kept /xhs silent for minutes.
assert xhs.XHS_FALLBACK_MODEL == "openrouter/free", xhs.XHS_FALLBACK_MODEL
assert xhs.XHS_REQUEST_TIMEOUT == 90
import inspect
client_src = inspect.getsource(xhs).split("client = AsyncOpenAI(", 1)[1].split("if OPENROUTER_API_KEY else None", 1)[0]
assert "max_retries=0" in client_src, client_src

class FirstModelEmpty:
    def __init__(self): self.models = []
    async def create(self, **kw):
        self.models.append(kw["model"])
        content = "" if kw["model"] == xhs.XHS_MODEL else json.dumps(GOOD, ensure_ascii=False)
        m = types.SimpleNamespace(content=content, model_extra={})
        return types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=m, finish_reason="stop")])
fm = FirstModelEmpty()
xhs.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=fm))
got = asyncio.run(xhs.generate_note("课程汇报 PPT", state={}))
assert got.title == GOOD["title"], got.title
assert fm.models == [xhs.XHS_MODEL, "openrouter/free"], fm.models
print("PASS an empty reply from XHS_MODEL is retried once on openrouter/free")

# --- the notes promote the 代做 service, and the example obeys its own rules --
ex = xhs.EXAMPLE_NOTE
assert len(ex["title"]) <= 20, len(ex["title"])
assert 300 <= len(ex["body"]) <= 600, len(ex["body"])
assert 6 <= len(ex["tags"]) <= 8 and not any(t.startswith("#") for t in ex["tags"])
assert len(ex["cover"]["main"]) == 2 and all(6 <= len(m) <= 10 for m in ex["cover"]["main"]), ex["cover"]["main"]
assert len(ex["cover"]["question"]) <= 15
assert len(ex["cover"]["small"]) == 3 and all(8 <= len(m) <= 14 for m in ex["cover"]["small"]), ex["cover"]["small"]
banned = ["微信", "QQ", "闲鱼", "淘宝", "加V", "二维码", "http", "翻墙", "梯子", "VPN", "科学上网",
          "垃圾", "没用", "已帮", "好评", "通过率", "卖号", "代充"]
blob = xhs.EXAMPLE_JSON
for word in banned:
    assert word not in blob, f"example breaks its own rules: {word}"
assert "评论区留言" in ex["body"] and "私信" in ex["body"], "example must show the allowed call to action"
# the two objections the user asked every note to answer
for must in ("豆包", "千问", "Claude", "海外支付卡", "海外手机号"):
    assert must in ex["body"], f"example must make the {must} argument"
assert "豆包" in xhs.SERVICE_BRIEF and "海外支付卡" in xhs.SERVICE_BRIEF
print(f"PASS the example note obeys every rule it teaches (body {len(ex['body'])} chars)")

prompt = xhs._build_user_prompt(xhs.DOMAINS[0], [])
assert xhs.SERVICE_BRIEF in prompt, "the model must see the service it is selling"
assert "论文代写" in prompt and "作业代写" in prompt, "the no-ghostwriting line must reach the model"
assert xhs.DOMAINS[0] in prompt
assert xhs.EXAMPLE_JSON in prompt
assert "{" not in xhs.SERVICE_BRIEF, "a brace in the brief would break nothing, but keep it plain"
# the example parses through the real pipeline
note = xhs._normalize(json.loads(xhs.EXAMPLE_JSON), xhs.DOMAINS[0])
assert note.title == ex["title"] and note.cover_main == ex["cover"]["main"]
assert xhs.BADGE_TEXT == "接单中"
assert all("PPT" in d or "简历" in d or "Excel" in d or "文案" in d for d in xhs.DOMAINS), xhs.DOMAINS
print(f"PASS the prompt carries the service brief and rotates {len(xhs.DOMAINS)} service lines")

# --- /xhs gives up on a hung model at the deadline --------------------------------------------------
class HangingXhs:
    def __init__(self): self.models = []
    async def create(self, **kw):
        self.models.append(kw["model"])
        if kw["model"] == xhs.XHS_MODEL:
            await asyncio.sleep(3600)
        m = types.SimpleNamespace(content=json.dumps(GOOD, ensure_ascii=False), model_extra={})
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=m, finish_reason="stop")])
hx = HangingXhs()
xhs.client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=hx))
saved_t = xhs.XHS_REQUEST_TIMEOUT
xhs.XHS_REQUEST_TIMEOUT = 0.05
assert asyncio.run(xhs.generate_note("课程汇报 PPT", state={})).title == GOOD["title"]
assert hx.models == [xhs.XHS_MODEL, "openrouter/free"], hx.models
xhs.XHS_REQUEST_TIMEOUT = saved_t
print("PASS a hung 小红书 model is dropped at the deadline and the fallback writes the note")

print("\nALL XHS TESTS PASSED")
