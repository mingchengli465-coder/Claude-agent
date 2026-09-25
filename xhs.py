"""Xiaohongshu (小红书) note generation: topic pick, copy, and cover image.

Each note is one OpenRouter call that returns JSON, plus a cover rendered
locally with Pillow. Nothing here touches Telegram — bot.py wires it up.
"""

import asyncio
import io
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from openai import AsyncOpenAI, BadRequestError
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Falls back to the chat model so a single MODEL setting configures both.
XHS_MODEL = os.environ.get("XHS_MODEL") or os.environ.get("MODEL", "deepseek/deepseek-chat-v3.1:free")
# The retry goes here rather than back to XHS_MODEL, so one overloaded or
# flaky free model can't fail both attempts. Same idea as X_FALLBACK_MODEL.
XHS_FALLBACK_MODEL = os.environ.get("XHS_FALLBACK_MODEL", "openrouter/free").strip() or XHS_MODEL
XHS_REQUEST_TIMEOUT = float(os.environ.get("XHS_REQUEST_TIMEOUT", "90"))
# Generous, because a reasoning model (which openrouter/free may route to)
# spends part of this budget thinking and the note still needs ~1000 tokens.
XHS_MAX_TOKENS = int(os.environ.get("XHS_MAX_TOKENS", "8000"))
# OpenRouter's reasoning controls: "low" keeps the thinking short, and exclude
# drops it from the response so the token budget goes to the note itself.
XHS_REASONING_EFFORT = os.environ.get("XHS_REASONING_EFFORT", "low")
XHS_REASONING_EXCLUDE = os.environ.get("XHS_REASONING_EXCLUDE", "true").lower() != "false"
XHS_STATE_FILE = Path(os.environ.get("XHS_STATE_FILE", "xhs_state.json"))
# Topics from the last N days are shown to the model as things to avoid.
XHS_AVOID_DAYS = int(os.environ.get("XHS_AVOID_DAYS", "7"))

FONT_PATH = Path(os.environ.get("XHS_FONT_PATH", Path(__file__).parent / "fonts" / "NotoSansSC-VF.ttf"))

# Each note promotes one service line; rotating keeps the account from posting
# the same pitch every day.
DOMAINS = [
    "课程汇报 PPT",
    "简历优化排版",
    "毕业答辩 PPT",
    "Excel 表格整理",
    "比赛与路演 PPT",
    "小红书 / 公众号文案",
]

# What the notes are allowed to say about the service. The model may only use
# what is written here, so change the offer (add prices, drop a line) by
# setting XHS_SERVICE_BRIEF in Railway rather than editing the prompt.
DEFAULT_SERVICE_BRIEF = """我是在校大学生，个人接单，用 Claude 和 Codex 做东西，交付前自己逐页检查。
为什么不直接用豆包、千问：它们免费、方便，日常问问题很好用，我自己也用；
但做一整份要直接交出去的东西（整套 PPT、简历、表格公式），我的体验是 Claude 和 Codex
结构更清楚、返工更少，拿到就是能直接改的文件。
为什么不自己去用 Claude、Codex：国内没法直接开通，要海外支付卡、海外手机号，
注册和付费一步一卡，光折腾这些就要花掉一个晚上。这些我已经弄好了，你不用折腾。
能做：
- PPT 制作：课程汇报、小组展示、毕业答辩、比赛和路演
- 简历优化与排版：实习、校招、考研复试
- 小红书 / 公众号文案
- Excel 表格整理：数据清洗、公式、图表
交付：可编辑的源文件，WPS 和 Office 都能打开；PPT 可以带演讲备注。
不接：作业代写、论文代写、代考。不卖账号、不代充值、不代注册。
价格：按页数、难度和截止时间报价，私信沟通。"""
SERVICE_BRIEF = os.environ.get("XHS_SERVICE_BRIEF", "").strip() or DEFAULT_SERVICE_BRIEF

# --- cover design ----------------------------------------------------------
COVER_W, COVER_H = 1080, 1440
YELLOW = (255, 225, 77)      # #FFE14D
BLACK = (17, 17, 17)
RED = (232, 50, 46)          # #E8322E
WHITE = (255, 255, 255)
MARGIN = 76
# The notes are ads, so the old 「真实经历」 badge would be misleading.
BADGE_TEXT = os.environ.get("XHS_BADGE_TEXT", "接单中")

# Both flip off for the process once a model rejects the parameter.
_json_mode_supported = os.environ.get("XHS_JSON_MODE", "true").lower() != "false"
_reasoning_supported = os.environ.get("XHS_REASONING", "true").lower() != "false"

client = AsyncOpenAI(
    api_key=OPENROUTER_API_KEY,
    base_url=OPENROUTER_BASE_URL,
    timeout=XHS_REQUEST_TIMEOUT,
    # generate_note() already retries once on another model. The SDK's own two
    # retries on top of that let a slow free model hold /xhs silent for minutes.
    max_retries=0,
) if OPENROUTER_API_KEY else None


@dataclass
class Note:
    """One generated note: the copy plus the text that goes on the cover."""
    domain: str
    topic: str
    title: str
    body: str
    tags: list[str]
    cover_main: list[str] = field(default_factory=list)
    cover_question: str = ""
    cover_small: list[str] = field(default_factory=list)

    def tags_text(self) -> str:
        return "  ".join(f"#{t}" for t in self.tags)


class GenerationError(RuntimeError):
    """Raised when the model could not produce a usable note."""


# --------------------------------------------------------------------------- #
# State: which domain is next, and what we've already written
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    if not XHS_STATE_FILE.exists():
        return {"domain_index": 0, "history": []}
    try:
        state = json.loads(XHS_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("Could not read %s; starting fresh", XHS_STATE_FILE, exc_info=True)
        return {"domain_index": 0, "history": []}
    state.setdefault("domain_index", 0)
    state.setdefault("history", [])
    return state


def save_state(state: dict) -> None:
    state["history"] = (state.get("history") or [])[-60:]
    tmp = XHS_STATE_FILE.with_suffix(XHS_STATE_FILE.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, XHS_STATE_FILE)
    except OSError:
        logger.error("Could not persist %s", XHS_STATE_FILE, exc_info=True)


def recent_topics(state: dict, days: int = XHS_AVOID_DAYS) -> list[str]:
    """Topics written within the last `days` days, newest first."""
    cutoff = date.today() - timedelta(days=days)
    out = []
    for entry in reversed(state.get("history") or []):
        try:
            when = date.fromisoformat(entry.get("date", ""))
        except ValueError:
            continue
        if when >= cutoff:
            out.append(f"{entry.get('topic', '')}（{entry.get('title', '')}）")
    return out


def take_domain(state: dict) -> str:
    index = int(state.get("domain_index", 0)) % len(DOMAINS)
    state["domain_index"] = (index + 1) % len(DOMAINS)
    return DOMAINS[index]


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = """你是资深的小红书内容写手，专门帮个人接单的大学生写推广笔记：
读起来像同学在分享，不像硬广，看完的人愿意私信问一句。

你的输出会被程序直接用 json.loads() 解析，所以：
- 第一个字符必须是 {，最后一个字符必须是 }
- 不要写 ```json，不要写 ```，不要写任何代码块标记
- 不要在 JSON 前后加"好的""以下是""希望对你有帮助"之类的话
- 不要在 JSON 里写注释
- 正文里的换行必须写成 \\n 转义，不能直接敲回车
- 所有内容用简体中文

只输出那一个 JSON 对象，其他什么都不要输出。"""

# Non-negotiable and repeated in every request. 2 keeps the ads honest; 4 and 5
# keep the account from being throttled or banned by 小红书.
HARD_RULES = """【必须遵守的硬性规则，违反即视为失败】
1. 不攻击、不贬低任何群体，也不贬低同行（不写"别家都是套模板"之类）。
   和豆包、千问对比时要客观：先承认它们免费、方便、日常好用，再用"我的体验"讲差别；
   不说它们"垃圾""没用""智商低"，不编造测评、跑分或对比数据（广告法禁止贬低其他商品）。
2. 不编造：不写接单数量、好评、客户原话、成交金额、"已帮 XX 人"、通过率、排名等任何无法证实的数字和案例。
   痛点场景用"你""很多人"的写法描述，不要写成某个具体客户的真实故事。
   关于服务本身，只能写【服务信息】里有的内容，不能添加价格、时效、售后承诺。
3. 不写、不暗示作业代写、论文代写、代考、代课；不承诺成绩、答辩通过、面试录用。
4. 不写任何站外联系方式或平台：微信、QQ、手机号、闲鱼、淘宝、链接、二维码、"加V""滴滴我"等都不行。
   引导只用"评论区留言"或"私信"。
5. 可以点名 Claude、Codex、豆包、千问，但只写【服务信息】里的说法。
   不提翻墙、梯子、VPN、科学上网；不卖账号、不代充值、不代注册、不做中转，只卖"帮你做出来"。
6. 不给医疗建议、不给投资理财建议，不涉及政治和时政。"""


def _build_user_prompt(domain: str, avoid: list[str]) -> str:
    avoid_block = "\n".join(f"- {t}" for t in avoid) if avoid else "（暂无，随便挑）"
    return f"""请为下面这个接单服务写一篇小红书推广笔记，这篇主推【{domain}】。

【服务信息：只能写这里有的，不能添加】
{SERVICE_BRIEF}

【最近 {XHS_AVOID_DAYS} 天已经写过的选题，不要重复，也不要换个说法写同一件事】
{avoid_block}

【选题要求】
从大学生或应届生的一个具体痛点切入，比如 DDL 前一晚还没动、小组作业没人管排版、
简历投了很多没回音、表格对到半夜还对不上。要具体到一个场景，不要空喊"专业靠谱"。

【文案要求】
- 标题：20 字以内，戳中痛点或直接给出好处，可以带 emoji；不要"震惊""必看"这类标题党
- 正文：300-600 字，第一人称（"我"就是接单的人），口语化，多分段
  （每段 1-3 句，段与段之间空一行）。要有说服力，按这个顺序写：
  1）用"你"描述一个跟【{domain}】有关的痛点场景
  2）预先回答"用豆包、千问不就行了"：先承认它们好用，再讲做整份交付物时的差别
  3）预先回答"那我自己去用 Claude 不就行了"：讲清楚门槛（海外支付卡、海外手机号、注册付费折腾），
     然后说这些我已经弄好了，你不用折腾
  4）我能帮你做什么、怎么交付（只用【服务信息】里的内容）
  5）送 1 条跟【{domain}】有关的实用小建议，让不下单的人也有收获
  6）结尾一句轻引导，比如"需要的评论区留言或私信我"
- 话题标签：6-8 个，不要带 # 号
- 封面文案：主标 2 行（每行 6-10 字，短促有力）、
  痛点问句 1 句（15 字以内）、小字 3 行（每行 8-14 字，写服务范围或交付亮点）

{HARD_RULES}

【输出格式】
只输出一个 JSON 对象。第一个字符是 {{，最后一个字符是 }}，前后不要有任何其他文字，
也不要用 ``` 包起来。正文的换行写成 \\n，不要直接敲回车。

下面是一个完整的示例，照这个结构和长度来写（内容不要抄，换你自己的）：

{EXAMPLE_JSON}

注意：示例里 body 的换行用的是 \\n，不是真的回车。你也必须这样写。

现在按同样的结构，主推【{domain}】写一篇新的。只输出 JSON。"""


# Kept as data so a test can check the example obeys the rules it teaches.
EXAMPLE_NOTE = {
    "topic": "不想折腾 Claude，DDL 前的课程汇报 PPT 交给别人做",
    "title": "不想折腾Claude？PPT交给我来做",
    "body": (
        "是不是很熟悉：明早就要汇报，你打开豆包、千问，让它帮你做 PPT。\n\n"
        "大纲一下就出来了，可要变成一份能上台的 PPT，还得自己一页页排版、理逻辑、对数据，折腾到半夜。\n\n"
        "豆包、千问日常问问题真的方便，免费又顺手，我自己也在用。但做一整份要直接交的 PPT，"
        "我的体验是 Claude 和 Codex 返工少很多：结构更清楚，图表能对上数据，拿到就是能直接改的文件。\n\n"
        "那自己去用 Claude 不就行了？\n\n"
        "试过就知道：国内没法直接开通，要海外支付卡，要海外手机号，注册、付费一步一卡。"
        "光折腾这些，一个晚上就没了，DDL 可不会等你。\n\n"
        "这些麻烦我已经替你踩完了。我是在校大学生，平时就用 Claude 和 Codex 接单。"
        "你把老师的要求、讲稿或大纲发我，我按页数、难度和截止时间报价，交付可编辑的源文件，"
        "WPS、Office 都能打开，需要的话每页带演讲备注。交之前我会自己逐页检查。\n\n"
        "顺手送一个小建议：每页标题直接写结论，比如「拖延不是懒，是不想开始」，比写「原因分析」好讲得多。\n\n"
        "作业代写、论文代写不接，我只负责把你的内容做得清楚、好看。\n\n"
        "需要的评论区留言或私信我，告诉我主题、页数和什么时候要。"
    ),
    "tags": ["PPT制作", "课程汇报", "大学生", "Claude", "AI工具", "期末周", "小组作业"],
    "cover": {
        "main": ["海外AI不好开？", "PPT我来做"],
        "question": "DDL前还在改PPT？",
        "small": ["用Claude和Codex做", "你不用办海外支付卡", "作业论文代写不接"],
    },
}
EXAMPLE_JSON = json.dumps(EXAMPLE_NOTE, ensure_ascii=False, indent=2)


# How much of a bad reply to put in the log. Small models fail in ways you
# cannot guess at, so the raw text is the only way to see what went wrong.
RAW_LOG_CHARS = 300

_FENCE_RE = re.compile(r"```[a-zA-Z]*\s*\n?(.*?)(?:```|\Z)", re.S)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_fences(text: str) -> str:
    """Drop a ``` wrapper, including one the model never closed."""
    if "```" not in text:
        return text
    match = _FENCE_RE.search(text)
    if match and match.group(1).strip():
        return match.group(1).strip()
    return text.replace("```", " ")


# Models rename these often enough to be worth accepting. First match wins.
TITLE_KEYS = ("title", "标题", "headline")
BODY_KEYS = ("body", "正文", "content", "内容", "text")
TAGS_KEYS = ("tags", "话题标签", "标签", "hashtags")
TOPIC_KEYS = ("topic", "选题", "主题")
COVER_KEYS = ("cover", "封面", "封面文案")
COVER_MAIN_KEYS = ("main", "主标", "主标题")
COVER_QUESTION_KEYS = ("question", "问句", "争议问句")
COVER_SMALL_KEYS = ("small", "小字", "小字文案")

# Used to tell the real object apart from a stray "{}" in the preamble.
_EXPECTED_KEYS = set(TITLE_KEYS + BODY_KEYS + TAGS_KEYS + TOPIC_KEYS + COVER_KEYS)


def _pick(data: dict, keys: tuple[str, ...]):
    """First present, non-empty value among `keys`."""
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _unwrap(data: dict) -> dict:
    """Unwrap {"note": {...}} / {"result": {...}} so the real object is used."""
    if _EXPECTED_KEYS & set(data):
        return data
    for value in data.values():
        if isinstance(value, dict) and _EXPECTED_KEYS & set(value):
            return value
    return data


def _is_usable(data: dict) -> bool:
    """A note we could actually send: both a title and a body with text in them."""
    title = _pick(data, TITLE_KEYS)
    body = _pick(data, BODY_KEYS)
    return bool(isinstance(title, str) and title.strip()
                and isinstance(body, str) and body.strip())
# Cap the candidate scan so a pathological reply can't make this quadratic.
_MAX_CANDIDATES = 20


def _object_candidates(text: str) -> list[str]:
    """Every balanced {...} span, one per opening brace, outermost first.

    Trying only the first brace breaks when the model writes something like
    "这里有个 { 花括号" before the real object: the depth counter starts on the
    prose brace and never balances.
    """
    candidates: list[str] = []
    for start in range(len(text)):
        if text[start] != "{":
            continue
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
        if len(candidates) >= _MAX_CANDIDATES:
            break

    # Nothing balanced: fall back to the widest span, which _repair may salvage.
    if not candidates:
        first, last = text.find("{"), text.rfind("}")
        if first != -1 and last > first:
            candidates.append(text[first:last + 1])
    return candidates


def _escape_raw_control_chars(text: str) -> str:
    """Escape literal newlines/tabs inside strings.

    Small models routinely write a multi-paragraph 正文 with real line breaks
    instead of \\n, which json.loads rejects as an invalid control character.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in "\n\r\t":
            out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch])
            continue
        out.append(ch)
    return "".join(out)


def _repair(text: str) -> str:
    """Last-resort fixes for the ways small models bend JSON."""
    return _TRAILING_COMMA_RE.sub(r"\1", _escape_raw_control_chars(text))


def _extract_json(raw: str, is_usable=None) -> dict:
    """Pull a JSON object out of a reply that may be fenced, padded or malformed.

    `is_usable` decides which candidate is the real one when several parse —
    it defaults to the note test (title + body), and other callers pass their
    own, since a tweet's "done" looks nothing like a note's.

    Raises GenerationError with the raw text logged, so a failure is diagnosable
    instead of just "没有返回 JSON".
    """
    if is_usable is None:
        is_usable = _is_usable
    text = (raw or "").strip()
    if not text:
        logger.error("模型返回了空内容（免费模型限流，或把内容放进了别的字段）")
        raise GenerationError("模型返回了空内容")

    candidates = _object_candidates(_strip_fences(text))
    if not candidates:
        logger.error("模型回复里找不到 JSON 对象。原始回复前 %d 字：\n%s",
                     RAW_LOG_CHARS, text[:RAW_LOG_CHARS])
        raise GenerationError("模型没有返回 JSON")

    keyed: list[dict] = []     # right shape, but empty title/body
    parsed: list[dict] = []    # parsed, but not note-shaped at all
    last_error: Exception | None = None
    for candidate in candidates:
        for attempt in (candidate, _repair(candidate)):
            try:
                data = json.loads(attempt)
            except ValueError as exc:
                last_error = exc
                continue
            if isinstance(data, dict):
                data = _unwrap(data)
                # A reasoning trace often sketches the schema first, e.g.
                # {"title": "", "body": ""}. That has the right keys but no
                # content, so it must not beat the real note further down.
                if is_usable(data):
                    return data
                if _EXPECTED_KEYS & set(data):
                    keyed.append(data)
                else:
                    parsed.append(data)
            break

    if keyed:
        logger.warning(
            "只找到了空的 JSON 骨架（键：%s）。原始回复前 %d 字：\n%s",
            sorted(keyed[0]), RAW_LOG_CHARS, text[:RAW_LOG_CHARS],
        )
        return keyed[0]
    if parsed:
        return parsed[0]

    logger.error("JSON 解析失败（%s）。原始回复前 %d 字：\n%s",
                 last_error, RAW_LOG_CHARS, text[:RAW_LOG_CHARS])
    raise GenerationError(f"返回的 JSON 无法解析：{last_error}")


def _clean_tag(tag: str) -> str:
    return re.sub(r"^#+", "", str(tag)).strip()


def _normalize(data: dict, domain: str) -> Note:
    """Coerce the model's JSON into the shape the sender and cover expect.

    Soft violations (a 22-character title, 9 tags) are trimmed rather than
    retried — a retry costs a call and usually returns the same shape.
    """
    title = str(_pick(data, TITLE_KEYS) or "").strip()
    body = str(_pick(data, BODY_KEYS) or "").strip()
    if not title or not body:
        # Name what did come back, so the next failure diagnoses itself.
        raise GenerationError(
            f"标题或正文为空（模型返回的键：{sorted(data)}，"
            f"标题 {len(title)} 字，正文 {len(body)} 字）"
        )

    if len(title) > 20:
        title = title[:20]

    raw_tags = _pick(data, TAGS_KEYS) or []
    if isinstance(raw_tags, str):  # some models return "a,b,c" instead of a list
        raw_tags = re.split(r"[,，\s]+", raw_tags)
    tags = [t for t in (_clean_tag(t) for t in raw_tags) if t][:8]
    while len(tags) < 6:
        tags.append(domain.replace(" ", ""))

    cover = _pick(data, COVER_KEYS) or {}
    if not isinstance(cover, dict):
        cover = {}
    raw_main = _pick(cover, COVER_MAIN_KEYS) or []
    if isinstance(raw_main, str):
        raw_main = raw_main.splitlines()
    raw_small = _pick(cover, COVER_SMALL_KEYS) or []
    if isinstance(raw_small, str):
        raw_small = raw_small.splitlines()
    main = [str(x).strip() for x in raw_main if str(x).strip()][:2]
    small = [str(x).strip() for x in raw_small if str(x).strip()][:3]
    question = str(_pick(cover, COVER_QUESTION_KEYS) or "").strip()

    # The cover must always have something to draw, even on a lazy reply.
    if not main:
        main = [title[:10], title[10:20]] if len(title) > 10 else [title]
    if not question:
        question = "DDL 前还在熬夜？"
    while len(small) < 3:
        small.append("")

    return Note(
        domain=domain,
        topic=str(_pick(data, TOPIC_KEYS) or title).strip(),
        title=title,
        body=body,
        tags=tags,
        cover_main=main,
        cover_question=question,
        cover_small=small,
    )


def _reasoning_text(message) -> str:
    """Pull the reasoning trace off a message, wherever the provider put it.

    The OpenAI SDK has no `reasoning` field, so it arrives as an extra. Some
    providers use `reasoning`, some `reasoning_content`, some a list of
    `reasoning_details`.
    """
    extra = getattr(message, "model_extra", None) or {}

    for key in ("reasoning", "reasoning_content"):
        for value in (getattr(message, key, None), extra.get(key)):
            if isinstance(value, str) and value.strip():
                return value

    details = getattr(message, "reasoning_details", None) or extra.get("reasoning_details")
    if isinstance(details, list):
        parts = []
        for item in details:
            text = (item.get("text") or item.get("summary")) if isinstance(item, dict) else (
                getattr(item, "text", None) or getattr(item, "summary", None)
            )
            if isinstance(text, str) and text.strip():
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return ""


async def _create(kwargs: dict, use_json: bool, use_reasoning: bool):
    """One request, with the optional parameters the model may or may not accept."""
    params = dict(kwargs)
    if use_json:
        params["response_format"] = {"type": "json_object"}
    if use_reasoning:
        # OpenRouter-specific, so it rides along in extra_body.
        params["extra_body"] = {
            "reasoning": {"exclude": XHS_REASONING_EXCLUDE, "effort": XHS_REASONING_EFFORT}
        }
    # Hard deadline: OpenRouter trickles whitespace to keep slow requests open,
    # so the SDK's socket timeout alone would let /xhs hang for minutes.
    return await asyncio.wait_for(client.chat.completions.create(**params), XHS_REQUEST_TIMEOUT)


async def _one_call(domain: str, avoid: list[str], model: str = XHS_MODEL) -> Note:
    global _json_mode_supported, _reasoning_supported

    if client is None:
        raise GenerationError("OPENROUTER_API_KEY 没有设置")

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(domain, avoid)},
        ],
        "temperature": 1.0,
        "max_tokens": XHS_MAX_TOKENS,
    }

    try:
        response = await _create(kwargs, _json_mode_supported, _reasoning_supported)
    except BadRequestError as exc:
        # The error rarely names the offending parameter, so read what we can
        # and otherwise drop both rather than failing the whole attempt.
        message = str(exc).lower()
        dropped = []
        if _json_mode_supported and ("response_format" in message or "json" in message):
            _json_mode_supported = False
            dropped.append("response_format")
        if _reasoning_supported and "reasoning" in message:
            _reasoning_supported = False
            dropped.append("reasoning")
        if not dropped:
            if _json_mode_supported:
                _json_mode_supported = False
                dropped.append("response_format")
            if _reasoning_supported:
                _reasoning_supported = False
                dropped.append("reasoning")
        if not dropped:
            raise
        logger.warning("%s 拒绝了 %s，之后不再发送。原始错误：%s",
                       model, "、".join(dropped), exc)
        response = await _create(kwargs, _json_mode_supported, _reasoning_supported)

    if not response.choices:
        raise GenerationError("模型返回了空的 choices")

    choice = response.choices[0]
    finish = getattr(choice, "finish_reason", "?")
    text = (choice.message.content or "").strip()

    if not text:
        # Reasoning models can burn the whole budget thinking and return an
        # empty content field, with the JSON left in the reasoning trace.
        reasoning = _reasoning_text(choice.message)
        logger.error(
            "模型 content 为空（finish_reason=%s，reasoning 长度=%d）。"
            "如果是推理模型，可以调高 XHS_MAX_TOKENS 或降低 XHS_REASONING_EFFORT。",
            finish, len(reasoning),
        )
        if reasoning.strip():
            logger.warning("改从 reasoning 字段里提取 JSON")
            text = reasoning.strip()

    if not text:
        raise GenerationError(
            f"模型返回了空内容（finish_reason={finish}，reasoning 里也没有内容）"
        )

    try:
        return _normalize(_extract_json(text), domain)
    except GenerationError as exc:
        if finish == "length":
            raise GenerationError(
                f"{exc}。finish_reason=length，说明输出被截断了，建议调高 XHS_MAX_TOKENS"
            ) from exc
        raise


async def generate_note(domain: str | None = None, state: dict | None = None) -> Note:
    """Generate one note, retrying once (on XHS_FALLBACK_MODEL) before giving up.

    A malformed reply counts as a failure just like a transport error, since
    both leave us without a usable note.
    """
    own_state = state is None
    state = load_state() if own_state else state
    chosen = domain or take_domain(state)
    avoid = recent_topics(state)

    last_error: Exception | None = None
    for attempt, model in enumerate((XHS_MODEL, XHS_FALLBACK_MODEL), start=1):
        try:
            note = await _one_call(chosen, avoid, model)
        except Exception as exc:  # noqa: BLE001 - retry on anything, then report
            last_error = exc
            logger.warning("小红书生成第 %d 次失败（%s）：%s", attempt, model, exc, exc_info=True)
            continue
        if attempt > 1:
            logger.info("改用备用模型 %s 生成成功", model)

        state.setdefault("history", []).append(
            {
                "date": date.today().isoformat(),
                "domain": chosen,
                "topic": note.topic,
                "title": note.title,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        save_state(state)
        return note

    if own_state:
        save_state(state)  # Keep the domain rotation moving past a dead topic.
    raise GenerationError(f"连续两次生成失败：{last_error}") from last_error


# --------------------------------------------------------------------------- #
# Cover rendering
# --------------------------------------------------------------------------- #

_font_cache: dict[tuple[int, str], ImageFont.FreeTypeFont] = {}


def load_font(size: int, weight: str = "Bold") -> ImageFont.FreeTypeFont:
    """Load the bundled variable font at a named weight, cached by (size, weight)."""
    key = (size, weight)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    if not FONT_PATH.exists():
        raise GenerationError(
            f"找不到字体文件 {FONT_PATH}。仓库的 fonts/ 目录里应该有 NotoSansSC-VF.ttf。"
        )
    font = ImageFont.truetype(str(FONT_PATH), size)
    try:
        font.set_variation_by_name(weight)
    except Exception:  # noqa: BLE001 - a static build just stays at its own weight
        logger.debug("字体不支持可变字重 %s，使用默认字重", weight, exc_info=True)
    _font_cache[key] = font
    return font


# Punctuation that must not start a line.
_NO_LEAD = "。，、；：？！）」』】》%…·.,;:?!)]}"


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Wrap CJK text character by character, keeping punctuation off line starts."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    lines: list[str] = []
    current = ""
    for ch in text:
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + ch
        if current and draw.textlength(trial, font=font) > max_width:
            if ch in _NO_LEAD and len(current) > 1:
                # Pull one character down so the punctuation isn't orphaned.
                lines.append(current[:-1])
                current = current[-1] + ch
            else:
                lines.append(current)
                current = ch
        else:
            current = trial
    if current:
        lines.append(current)
    return lines or [""]


def fit_lines(
    lines: list[str],
    box_w: int,
    box_h: int,
    weight: str,
    max_size: int,
    min_size: int = 22,
    spacing: float = 1.22,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest size at which `lines` fit the box, re-wrapping if they still don't."""
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text = [ln for ln in lines if ln]
    for size in range(max_size, min_size - 1, -2):
        font = load_font(size, weight)
        if all(draw.textlength(ln, font=font) <= box_w for ln in text):
            if len(text) * size * spacing <= box_h:
                return font, text
    # Still too big at the minimum: re-flow everything and clip to the box.
    font = load_font(min_size, weight)
    flowed: list[str] = []
    for ln in text:
        flowed.extend(wrap_text(ln, font, box_w))
    max_lines = max(1, int(box_h // (min_size * spacing)))
    return font, flowed[:max_lines]


def _draw_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    x: int,
    y: int,
    fill,
    spacing: float = 1.22,
    center_w: int | None = None,
) -> int:
    """Draw lines top-down; returns the y just below the block."""
    step = int(font.size * spacing)
    for i, line in enumerate(lines):
        lx = x
        if center_w is not None:
            lx = x + (center_w - draw.textlength(line, font=font)) / 2
        draw.text((lx, y + i * step), line, font=font, fill=fill)
    return y + len(lines) * step


def _draw_badge(draw: ImageDraw.ImageDraw, x: int, y: int) -> int:
    """Black tag in the top-left corner. Returns the y below it."""
    font = load_font(40, "Bold")
    tw = draw.textlength(BADGE_TEXT, font=font)
    pad_x, pad_y = 26, 14
    w, h = int(tw + pad_x * 2), int(font.size + pad_y * 2)
    draw.rounded_rectangle([x, y, x + w, y + h], radius=10, fill=BLACK)
    draw.text((x + pad_x, y + pad_y - 4), BADGE_TEXT, font=font, fill=WHITE)
    return y + h


def _question_layout(question: str, box_w: int) -> tuple[ImageFont.FreeTypeFont, list[str], int, int]:
    """Measure the red question box before drawing it: (font, lines, box_h, pad_x)."""
    pad_x, pad_y = 34, 28
    inner_w = box_w - pad_x * 2
    font, lines = fit_lines([question], inner_w, 320, "Bold", 64, min_size=30)
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    if len(lines) == 1 and draw.textlength(lines[0], font=font) > inner_w:
        lines = wrap_text(lines[0], font, inner_w)
    box_h = len(lines) * int(font.size * 1.24) + pad_y * 2
    return font, lines, box_h, pad_x


def _draw_question(
    draw: ImageDraw.ImageDraw,
    font: ImageFont.FreeTypeFont,
    lines: list[str],
    x: int,
    y: int,
    box_w: int,
    box_h: int,
    pad_x: int,
) -> int:
    """Red rounded box holding the argumentative question. Returns y below it."""
    draw.rounded_rectangle([x, y, x + box_w, y + box_h], radius=26, fill=RED)
    _draw_block(draw, lines, font, x + pad_x, y + 28, WHITE, 1.24, center_w=box_w - pad_x * 2)
    return y + box_h


def render_cover(note: Note, variant: int = 0) -> bytes:
    """Render the 1080x1440 cover as PNG bytes. `variant` reorders the blocks."""
    img = Image.new("RGB", (COVER_W, COVER_H), YELLOW)
    draw = ImageDraw.Draw(img)
    x = MARGIN
    box_w = COVER_W - MARGIN * 2

    badge_bottom = _draw_badge(draw, x, MARGIN)

    # Lay every block out first, so the leftover height can be shared as gaps
    # instead of pooling at the bottom of the frame.
    main_spacing, small_spacing = 1.16, 1.38
    main_font, main_lines = fit_lines(note.cover_main, box_w, 620, "Black", 168, min_size=48)
    small_font, small_lines = fit_lines(
        [ln for ln in note.cover_small if ln], box_w, 300, "Medium", 50, min_size=24
    )
    q_font, q_lines, q_h, q_pad = _question_layout(note.cover_question, box_w)

    main_h = len(main_lines) * int(main_font.size * main_spacing)
    small_h = len(small_lines) * int(small_font.size * small_spacing)

    blocks = {"main": main_h, "question": q_h, "small": small_h}
    order = ["main", "question", "small"]
    if variant % 3 == 1:
        order = ["main", "small", "question"]
    elif variant % 3 == 2:
        order = ["question", "main", "small"]

    top = badge_bottom + 60
    available = (COVER_H - MARGIN) - top
    slack = available - sum(blocks.values())
    # Two gaps between three blocks; keep them generous but bounded, and let
    # whatever is left push the group down so it sits off the badge.
    gap = max(40, min(int(slack / 2), 150)) if slack > 0 else 34
    y = top + max(0, min(int((slack - gap * 2) * 0.45), 220))

    for i, kind in enumerate(order):
        if kind == "main":
            y = _draw_block(draw, main_lines, main_font, x, y, BLACK, main_spacing)
        elif kind == "question":
            y = _draw_question(draw, q_font, q_lines, x, y, box_w, q_h, q_pad)
        else:
            y = _draw_block(draw, small_lines, small_font, x, y, BLACK, small_spacing)
        if i < len(order) - 1:
            y += gap

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def render_cover_async(note: Note, variant: int = 0) -> bytes:
    """Pillow is blocking; keep it off the event loop."""
    return await asyncio.to_thread(render_cover, note, variant)
