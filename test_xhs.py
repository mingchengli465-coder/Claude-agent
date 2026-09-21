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
print("\nALL XHS TESTS PASSED")
