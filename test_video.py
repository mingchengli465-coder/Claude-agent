"""The intro videos: the business bot sends them to the owner once, and again on /video.
No network: run `python test_video.py`.
"""
import asyncio, os, sys, tempfile, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.update({"TELEGRAM_BOT_TOKEN": "123:fake", "OPENROUTER_API_KEY": "sk-test", "OWNER_CHAT_ID": "424242"})

import bot
import visits as visits_mod

assert all(path.is_file() and path.stat().st_size > 100_000 for path, _ in bot.INTRO_VIDEOS)
sent = []


class Bot:
    async def send_video(self, chat_id, video, caption, **k):
        sent.append((chat_id, os.path.basename(video.name), caption))


async def main():
    app = types.SimpleNamespace(bot=Bot())
    bot.visits = visits_mod.Visits(os.path.join(tempfile.mkdtemp(), "v.db"))
    bot.service = None
    await bot.send_intro_videos_once(app)
    assert not sent, "a personal-assistant bot (no customer service) never sends them"
    bot.service = object()
    await bot.send_intro_videos_once(app); await bot.send_intro_videos_once(app)
    assert [s[:2] for s in sent] == [(424242, "vinc-intro.mp4"), (424242, "vinc-intro-fiverr.mp4")]
    assert "Fiverr" in sent[1][2]
    print("PASS the owner gets both intro videos once, unasked")

    told = []
    class Msg:
        async def reply_text(self, text, **k): told.append(text)
    def update(chat):
        return types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=chat), effective_message=Msg())
    ctx = types.SimpleNamespace(bot=Bot())
    sent.clear()
    await bot.video_command(update(999), ctx)
    assert not sent and told, "strangers don't get the videos"
    await bot.video_command(update(424242), ctx)
    assert len(sent) == 2
    print("PASS /video sends them again to the owner only")

asyncio.run(main())
print("\nALL VIDEO TESTS PASSED")
