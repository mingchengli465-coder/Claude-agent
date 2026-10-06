"""The owner's own whale video for the site, sent to the bot in Telegram.

    hero.mp4        the video behind the homepage (and every demo page's first screen)

Until it's sent, the pages draw their own deep sea (web_static/partials/deep.js).
"""

from __future__ import annotations

HERO = "hero.mp4"
LABEL = "首页背景视频（鲸鱼）"
TELEGRAM_LIMIT = 20 * 1024 * 1024  # what a bot may download
