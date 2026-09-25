"""Which AI the owner's chat, 小红书 notes and tweets talk to.

With DEEPSEEK_API_KEY set, each of them asks DeepSeek first (paid, cheap, one
steady model) and falls back to OpenRouter's free models if that call fails,
so an empty DeepSeek balance degrades to the old behaviour instead of silence.
USE_DEEPSEEK=false switches back to OpenRouter only. Customer service picks its
model separately, in customer_service.py.
"""

from __future__ import annotations

import os

from openai import AsyncOpenAI

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-chat / deepseek-reasoner were retired in July 2026; deepseek-flash replaced them.
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-flash")
USE_DEEPSEEK = bool(DEEPSEEK_API_KEY) and os.environ.get("USE_DEEPSEEK", "true").lower() != "false"


def deepseek_client(timeout: float) -> AsyncOpenAI | None:
    """A DeepSeek client, or None when DeepSeek isn't in use.

    No SDK retries: callers move on to their OpenRouter fallback instead, and
    every call is also wrapped in asyncio.wait_for by the caller.
    """
    if not USE_DEEPSEEK:
        return None
    return AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=timeout, max_retries=0)
