"""Gaming news feed: its own channel key/role, feed list, AI prompt, and separate dedupe from the AI feed.

No network, Discord token or xAI key needed.
Run:  python tests/test_gaming_news.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
DB_FILE = os.path.join(tempfile.mkdtemp(), "gaming_news.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

import test_news as tn  # noqa: E402  (reuse its fakes and clock)
from cogs import news as news_cog  # noqa: E402
from cogs import setup as setup_cog  # noqa: E402
from utils import ai, content  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils import news as newsfeed  # noqa: E402
from utils.db import Database  # noqa: E402


def test_gaming_config_and_feeds():
    assert "GAMING_NEWS_CHANNEL_ID" in gc.CHANNEL_KEYS and "GAMING_NEWS_PING_ROLE_ID" in gc.ROLE_KEYS
    assert "GAMING_NEWS_CHANNEL_ID" in setup_cog.LINK_KEYS and "GAMING_NEWS_PING_ROLE_ID" in setup_cog.LINK_KEYS
    assert setup_cog.resolve_link_key("gaming news") == "GAMING_NEWS_CHANNEL_ID"
    assert "GAMING_NEWS_PING_ROLE_ID" in setup_cog.PING_KEYS
    feeds = content.load_gaming_news_feeds()
    names = {n for n, _ in feeds}
    for src in ("IGN", "GameSpot", "Polygon", "PC Gamer", "Eurogamer", "Rock Paper Shotgun", "VGC", "The Verge"):
        assert src in names, src
    assert "gaming_news" not in content.load_feeds()  # weekly digest unchanged
    ai_urls = {u for _, u in content.load_news_feeds()}
    assert not ai_urls & {u for _, u in feeds}  # gaming feeds don't leak into the AI feed
    assert "Nintendo Direct" in ai.GAMING_NEWS_SYSTEM and "rumor" in ai.GAMING_NEWS_SYSTEM.lower()


async def _run():
    gc.config.GUILD_ID = 0
    news_cog.now = tn.clock
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    real_key_set = ai.key_set
    try:
        g = tn.Guild()
        g.gaming = tn.FakeText(g, "gaming-news")
        chans = {g.news.id: g.news, g.modlog.id: g.modlog, g.gaming.id: g.gaming}
        g.get_channel = chans.get
        g.roles.append(tn.FakeRole("Gaming News"))
        cog = news_cog.News(types.SimpleNamespace(db=db, guilds=[g]))
        G, A = news_cog.GAMING_FEED, news_cog.AI_FEED
        feeds_now: list = []

        async def fake_fetch_all(feeds):
            return [newsfeed.Article(**vars(a)) for a in feeds_now], []

        cog.fetch_all = fake_fetch_all
        ai.key_set = lambda: False

        # Unset channel -> the gaming feed doesn't run, even with the AI feed on.
        await gc.set_many(g, {"NEWS_CHANNEL_ID": g.news.id})
        assert cog.guilds(G) == [] and cog.guilds(A) == [g]
        assert await cog.dispatch(g, G) is None
        await gc.set_many(g, {"GAMING_NEWS_CHANNEL_ID": g.gaming.id})
        assert cog.guilds(G) == [g]

        # Gaming articles post only in the gaming channel, with the gaming footer.
        feeds_now[:] = [tn.art("Big game revealed", 1)]
        await cog.poll(G)
        assert await cog.dispatch(g, A) is None  # never cross-posts to the AI channel
        assert await cog.dispatch(g, G) == "normal"
        post = g.gaming.sent[-1]
        assert post.embed.title == "Big game revealed" and "🎮 Gaming News" in post.embed.footer.text
        assert g.news.sent == []

        # AI articles go to the AI channel, independently (own gap/limits).
        feeds_now[:] = [tn.art("New model launched", 1)]
        await cog.poll(A)
        assert await cog.dispatch(g, G) is None
        assert await cog.dispatch(g, A) == "normal" and g.news.sent[-1].embed.title == "New model launched"

        # Breaking gaming news pings the Gaming News role, scored by the gaming prompt.
        ai.key_set = lambda: True
        calls = []

        async def fake_gaming_review(title, source, desc):
            calls.append(title)
            return ai.Reply('{"summary": "Huge showcase announced.", "score": 10}', 30)

        real = ai.gaming_news_review
        ai.gaming_news_review = fake_gaming_review
        try:
            feeds_now[:] = [tn.art("Nintendo Direct announced", 0.5)]
            await cog.poll(G)
        finally:
            ai.gaming_news_review = real
        assert calls == ["Nintendo Direct announced"]
        assert await cog.dispatch(g, G) == "breaking"
        assert g.gaming.sent[-1].content.endswith(g.roles[0].mention)
        ai.key_set = lambda: False

        # /news commands take a feed choice; ai stays the default.
        ctx = tn.Ctx(g)
        await cog.news_off_cmd.callback(cog, ctx, "gaming")
        assert not news_cog.news_on(g, G) and news_cog.news_on(g, A)
        await cog.news_on_cmd.callback(cog, ctx, "gaming")
        assert news_cog.news_on(g, G)
        n = len(g.gaming.sent)
        await cog.news_test.callback(cog, ctx, "gaming")
        assert len(g.gaming.sent) == n + 1 and ctx.replies[-1].startswith("✅")
        await cog.news.callback(cog, ctx, "gaming")
        assert "Gaming news" in ctx.replies[-1] and g.gaming.mention in ctx.replies[-1]
    finally:
        ai.key_set = real_key_set
        await db.close()


def test_gaming_feed():
    asyncio.run(_run())


if __name__ == "__main__":
    test_gaming_config_and_feeds()
    test_gaming_feed()
    print("tests/test_gaming_news.py: all checks passed")
