"""News upgrades: 429 backoff (no alerts), cross-outlet duplicate merging, AI recaps (weekly digest + daily).

No network, token or xAI key needed.
Run:  python tests/test_news_ai.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
DB_FILE = os.path.join(tempfile.mkdtemp(), "newsai.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

from aifakes import Ctx, Guild, patched  # noqa: E402
from cogs import news as news_cog  # noqa: E402
from cogs import schedule as schedule_cog  # noqa: E402
from utils import ai  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils import news as newsfeed  # noqa: E402
from utils.db import Database  # noqa: E402

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=ET).timestamp()


class Clock:
    t = T0

    def __call__(self):
        return self.t


def art(title, source, hours_ago, clock):
    return newsfeed.Article(url=newsfeed.canonical_url(f"https://{source.replace(' ', '').lower()}.example/"
                                                       f"{title.replace(' ', '-').lower()}"),
                            title=title, source=source, summary="Something happened.", image=None,
                            published_at=int(clock.t - hours_ago * 3600), feed=f"https://{source}.example/rss")


def test_same_story():
    assert newsfeed.same_story("OpenAI launches GPT-6 with real-time voice", "OpenAI's GPT-6 launches with real-time voice mode")
    assert newsfeed.same_story("Google DeepMind unveils Gemini 4 Ultra model", "Gemini 4 Ultra: Google DeepMind unveils new model")
    assert not newsfeed.same_story("OpenAI launches GPT-6", "Anthropic launches Claude 6")
    assert not newsfeed.same_story("Fresh one", "Fresh two")
    assert not newsfeed.same_story("Nvidia stock rises after earnings beat", "Nvidia unveils new GPU at Computex")


async def _backoff(cog, g, clock):
    calls = []

    async def fetch(session, name, url):
        calls.append(url)
        if "limited" in url:
            raise news_cog.FeedRateLimited(None)
        if "bad" in url:
            raise ValueError("not a feed")
        return []

    cog.fetch = fetch
    feeds = [("Limited", "https://limited.example/rss"), ("Good", "https://good.example/rss")]
    before = len(g.modlog.sent)
    articles, failed = await cog.fetch_all(feeds)
    assert failed == [] and "https://limited.example/rss" not in cog.feed_failures  # 429 is not a failure
    retry_at, wait = cog.backoff["https://limited.example/rss"]
    assert wait == news_cog.BACKOFF_START and retry_at == clock.t + wait
    for _ in range(5):  # while backing off the feed isn't fetched at all, and staff never hear about it
        await cog.fetch_all(feeds)
    assert calls.count("https://limited.example/rss") == 1 and len(g.modlog.sent) == before
    clock.t += wait + 1
    await cog.fetch_all(feeds)
    assert cog.backoff["https://limited.example/rss"][1] == 2 * news_cog.BACKOFF_START  # doubles
    assert cog.back_off("https://x.example", 120) == 120  # Retry-After wins
    for _ in range(10):
        cog.back_off("https://y.example", None)
    assert cog.backoff["https://y.example"][1] == news_cog.BACKOFF_MAX
    # the feed works again: backoff cleared
    cog.backoff["https://limited.example/rss"] = (0, 999)
    calls.clear()

    async def ok(session, name, url):
        return []
    cog.fetch = ok
    await cog.fetch_all(feeds)
    assert "https://limited.example/rss" not in cog.backoff
    assert news_cog._retry_after("120") == 120 and news_cog._retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


async def _duplicates(cog, g, db, clock):
    reviewed = []

    async def review(title, source, description):
        reviewed.append(title)
        return ai.Reply('{"summary": "AI summary.", "score": %d}' % (8 if "GPT" in title else 6), 10)

    feeds_now = [art("OpenAI launches GPT-6 with real-time voice", "The Verge", 1, clock),
                 art("OpenAI's GPT-6 launches with real-time voice mode", "TechCrunch", 0.5, clock),
                 art("Anthropic launches Claude 6", "Ars Technica", 0.2, clock)]

    async def fake_fetch_all(feeds):
        return [newsfeed.Article(**vars(a)) for a in feeds_now], []

    cog.fetch_all = fake_fetch_all
    with patched(ai, news_review=review, XAI_API_KEY="k"):
        result = await cog.poll()
        assert result.merged == 1 and sorted(reviewed) == ["Anthropic launches Claude 6",
                                                            "OpenAI launches GPT-6 with real-time voice"]
        # a later poll with another outlet's take on an already stored story merges into it too
        feeds_now[:] = [art("GPT-6 launches: OpenAI adds real-time voice", "MIT Technology Review", 0.1, clock)]
        result = await cog.poll()
        assert result.merged == 1 and len(reviewed) == 2
        queue = await db.news_candidates(g.id, int(clock.t) - 86400)
        assert len(queue) == 2  # duplicates never post
        assert await cog.dispatch(g) == "normal"
        post = g.news.sent[-1]
        assert post.embed.title == "OpenAI launches GPT-6 with real-time voice"
        assert "Also covered by MIT Technology Review, TechCrunch" in post.embed.footer.text


async def _recaps(cog, g, db, clock):
    seen = []

    async def recap(stories, period="week"):
        seen.append((period, [s["title"] for s in stories]))
        return ai.Reply("What a week!\n• GPT-6 shipped.", 30)

    with patched(ai, news_recap=recap, XAI_API_KEY="k"):
        embed = await news_cog.build_recap(db, int(clock.t) - 7 * 86400, "week", g.id)
        assert seen[-1][0] == "week" and seen[-1][1][0].startswith("OpenAI launches GPT-6")
        assert not any("TechCrunch" in t for t in seen[-1][1])  # duplicates aren't recapped twice
        assert "top stories of the week" in embed.title and "GPT-6 shipped" in embed.description
        assert "https://" in embed.fields[0].value

        # daily recap: off by default; /news daily now posts; the loop posts once a day when on
        ctx = Ctx(g, g.news, "admin")
        await cog.news_daily_cmd.callback(cog, ctx, enabled="on")
        assert gc.saved(g)["NEWS_DAILY_RECAP"] == 1 and "on" in ctx.replies[-1]
        n = len(g.news.sent)
        await cog.recap_loop.coro(cog)
        await cog.recap_loop.coro(cog)
        assert len(g.news.sent) == n + 1 and "today's top stories" in g.news.sent[-1].embed.title
        await cog.news_daily_cmd.callback(cog, ctx, enabled="off")
        clock.t += 86400
        await cog.recap_loop.coro(cog)
        assert len(g.news.sent) == n + 1

        # weekly digest: recap computed once per week and shared by servers
        sched = types.SimpleNamespace(db=db)
        calls = len(seen)
        with patched(schedule_cog, utc_now=lambda: int(clock.t)):
            e1 = await schedule_cog.Schedule.weekly_recap(sched, g)
            e2 = await schedule_cog.Schedule.weekly_recap(sched, g)
        assert e1 is not None and e1 is e2 and len(seen) == calls + 1

    with patched(ai, XAI_API_KEY=""):
        assert await news_cog.build_recap(db, 0, "week", g.id) is None


async def _run():
    clock = Clock()
    real_now = news_cog.now
    news_cog.now = clock
    gc.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    try:
        g = Guild()
        g.news, g.modlog = g.add("ai-news"), g.add("mod-log", public=False)
        await gc.set_many(g, {"NEWS_CHANNEL_ID": g.news.id, "MOD_LOG_CHANNEL_ID": g.modlog.id})
        bot = types.SimpleNamespace(db=db, guilds=[g])
        cog = news_cog.News(bot)
        await _backoff(cog, g, clock)
        await _duplicates(cog, g, db, clock)
        await _recaps(cog, g, db, clock)
    finally:
        news_cog.now = real_now
        await db.close()


def test_news_upgrades():
    asyncio.run(_run())


if __name__ == "__main__":
    test_same_story()
    test_news_upgrades()
    print("tests/test_news_ai.py: all checks passed")
