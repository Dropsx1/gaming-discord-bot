"""Live AI news feed: dedupe, 24h cutoff, limits, quiet hours, breaking news, bad feeds, /news.

No network, Discord token or xAI key needed (feeds and AI are faked).
Run:  python tests/test_news.py      (or: python -m pytest tests/)
"""

import asyncio
import itertools
import os
import sys
import tempfile
import types
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_FILE = os.path.join(tempfile.mkdtemp(), "news.db")  # own DB, so tests never share state
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

import discord  # noqa: E402

import config  # noqa: E402
from cogs import news as news_cog  # noqa: E402
from cogs import setup as setup_cog  # noqa: E402
from utils import ai, content  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils import news as newsfeed  # noqa: E402
from utils.db import Database  # noqa: E402

_ids = itertools.count(90_000)
ET = ZoneInfo("America/New_York")
NOON = datetime(2026, 10, 14, 12, 0, tzinfo=ET).timestamp()  # a Wednesday, outside quiet hours
HOUR = 3600


class FakeText(discord.TextChannel):  # passes isinstance(..., TextChannel)
    __hash__ = object.__hash__
    mention = property(lambda self: f"<#{self.id}>")

    def __init__(self, guild, name):
        self.id, self.guild, self.name, self.sent = next(_ids), guild, name, []

    def permissions_for(self, who):
        return types.SimpleNamespace(send_messages=True, embed_links=True)

    async def send(self, content=None, embed=None, allowed_mentions=None, **kw):
        self.sent.append(types.SimpleNamespace(content=content, embed=embed, allowed_mentions=allowed_mentions))
        return types.SimpleNamespace(id=next(_ids), jump_url=f"https://discord.com/channels/x/{self.id}")


class FakeRole:
    def __init__(self, name):
        self.id, self.name = next(_ids), name
        self.mention = f"<@&{self.id}>"


class Guild:
    def __init__(self):
        self.id, self.name = next(_ids), "Test Server"
        self.news = FakeText(self, "ai-news")
        self.modlog = FakeText(self, "mod-log")
        self.roles = []
        self.me = types.SimpleNamespace()

    def get_channel(self, cid):
        return {self.news.id: self.news, self.modlog.id: self.modlog}.get(cid)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


class Ctx:
    def __init__(self, guild):
        self.guild, self.author, self.replies = guild, "admin", []

    async def send(self, content=None, **kw):
        self.replies.append(content)

    async def defer(self, **kw):
        pass


def art(title, age_hours, feed="https://feed.example/a", url=None, image=None, summary="It happened. More text. And more."):
    published = 0 if age_hours is None else int(clock.t - age_hours * HOUR)
    return newsfeed.Article(
        url=newsfeed.canonical_url(url or f"https://news.example/{title.replace(' ', '-').lower()}"),
        title=title, source="Example News", summary=summary, image=image, published_at=published, feed=feed,
    )


class Clock:
    t = NOON

    def __call__(self):
        return self.t


clock = Clock()


async def _run():
    gc.config.GUILD_ID = 0
    news_cog.now = clock
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    real_key_set, real_review = ai.key_set, ai.news_review
    try:
        g = Guild()
        bot = types.SimpleNamespace(db=db, guilds=[g])
        cog = news_cog.News(bot)
        feeds_now: list = []

        async def fake_fetch_all(feeds):
            return [newsfeed.Article(**vars(a)) for a in feeds_now], []

        cog.fetch_all = fake_fetch_all
        ai.key_set = lambda: False

        # Nothing happens until a news channel is linked.
        assert cog.guilds() == [] and await cog.dispatch(g) is None
        await gc.set_many(g, {"NEWS_CHANNEL_ID": g.news.id, "MOD_LOG_CHANNEL_ID": g.modlog.id})
        assert cog.guilds() == [g]

        # 1. First run: recent articles queue up; >24h old and undated ones are skipped (no flood).
        feeds_now[:] = [art("Fresh one", 1, image="https://img.example/1.png"), art("Fresh two", 2),
                        art("Old news", 30), art("Undated", None)]
        result = await cog.poll()
        assert (result.new, result.fresh, result.reviewed) == (4, 2, 0), result
        assert await cog.dispatch(g) == "normal"
        post = g.news.sent[-1]
        assert post.embed.title == "Fresh one" and post.embed.url == "https://news.example/fresh-one"
        assert post.embed.image.url == "https://img.example/1.png" and "Read the full story" in post.embed.description
        assert "Example News" in post.embed.author.name and post.content is None
        assert post.embed.color == discord.Color.blurple()

        # 2. Max 1 normal post per 30 minutes; the rest wait.
        assert await cog.dispatch(g) is None
        clock.t += 29 * 60
        assert await cog.dispatch(g) is None
        clock.t += 2 * 60
        assert await cog.dispatch(g) == "normal" and g.news.sent[-1].embed.title == "Fresh two"
        clock.t += 31 * 60
        assert await cog.dispatch(g) is None  # queue empty: old + undated never post
        assert len(g.news.sent) == 2

        # 3. Dedupe by URL: the same articles (even with tracking params) are never new again.
        feeds_now[:] = [art("Fresh one", 1, url="https://NEWS.example/fresh-one/?utm_source=rss#top"), art("Fresh two", 2)]
        assert (await cog.poll()).new == 0
        # An undated item showing up later in a feed we already follow counts as new now.
        feeds_now[:] = [art("Later undated", None)]
        assert (await cog.poll()).fresh == 1
        clock.t += 31 * 60
        assert await cog.dispatch(g) == "normal" and g.news.sent[-1].embed.title == "Later undated"

        # 4. Daily limit (12): normal posts stop once the day's total is reached.
        old_limit = config.NEWS_DAILY_LIMIT
        config.NEWS_DAILY_LIMIT = 4
        try:
            feeds_now[:] = [art(f"Batch {i}", 0.1) for i in range(3)]
            await cog.poll()
            clock.t += 31 * 60
            assert await cog.dispatch(g) == "normal"  # 4th post today
            clock.t += 31 * 60
            assert await cog.dispatch(g) is None      # limit reached, 2 left waiting
        finally:
            config.NEWS_DAILY_LIMIT = old_limit

        # 5. Quiet hours (1-7 AM ET): normal posts wait.
        clock.t = datetime(2026, 10, 15, 3, 0, tzinfo=ET).timestamp()
        assert news_cog.quiet_hours()
        feeds_now[:] = [art("Night story", 0.5)]
        await cog.poll()
        assert await cog.dispatch(g) is None
        clock.t = datetime(2026, 10, 15, 7, 0, tzinfo=ET).timestamp()
        assert not news_cog.quiet_hours()
        assert await cog.dispatch(g) == "normal"

        # 6. With an xAI key: AI summary + score; 9+ is BREAKING (red, pings AI News, ignores quiet hours/gap).
        calls = []

        async def fake_review(title, source, description):
            calls.append(title)
            score = 10 if "GPT-7" in title else 6
            return ai.Reply(f'```json\n{{"summary": "AI says: {title} matters. Second sentence. Third.", "score": {score}}}\n```', 42)

        ai.key_set, ai.news_review = (lambda: True), fake_review
        role = FakeRole("🔔 AI News")
        g.roles.append(role)
        clock.t = datetime(2026, 10, 15, 2, 0, tzinfo=ET).timestamp()  # quiet hours
        feeds_now[:] = [art("OpenAI releases GPT-7", 0.2, summary="Big."), art("Minor update", 0.3)]
        result = await cog.poll()
        assert result.reviewed == 2 and sorted(calls) == ["Minor update", "OpenAI releases GPT-7"], (result, calls)
        assert await cog.dispatch(g) == "breaking"
        post = g.news.sent[-1]
        assert post.embed.color == discord.Color.red() and post.embed.author.name.startswith("🚨 BREAKING")
        assert role.mention in post.content and post.allowed_mentions.roles == [role]
        assert post.embed.description.startswith("AI says: OpenAI releases GPT-7 matters. Second sentence.")
        assert "Third" not in post.embed.description  # 1-2 sentences
        assert post.embed.footer.text == "AI importance: 10/10"
        assert await cog.dispatch(g) is None  # the 6/10 story waits for morning
        usage = await db.fetchone("SELECT COUNT(*), SUM(tokens) FROM ai_usage WHERE feature = 'news'")
        assert tuple(usage) == (2, 84)

        # 7. A bad feed never crashes the poll; after 3 failures in a row staff hear about it once.
        cog2 = news_cog.News(bot)

        async def flaky_fetch(session, name, url):
            if "bad" in url:
                raise ValueError("not a feed")
            return [art("From the good feed", 0.1, feed=url)]

        cog2.fetch = flaky_fetch
        feeds = [("Good", "https://good.example/rss"), ("Bad", "https://bad.example/rss")]
        before = len(g.modlog.sent)
        for _ in range(4):
            articles, failed = await cog2.fetch_all(feeds)
            assert len(articles) == 1 and failed == ["https://bad.example/rss"]
        alerts = g.modlog.sent[before:]
        assert len(alerts) == 1 and "keeps failing" in alerts[0].embed.title and "bad.example" in alerts[0].embed.description

        # A Discord rejection of one article is logged to the mod log and that article is skipped.
        class Rejecting(FakeText):
            async def send(self, *a, **kw):
                raise discord.HTTPException(types.SimpleNamespace(status=400, reason="Bad Request"), "Invalid embed")

        good_news = g.news
        g.news = Rejecting(g, "ai-news")
        g.news.id = good_news.id
        clock.t = datetime(2026, 10, 15, 9, 0, tzinfo=ET).timestamp()
        assert await cog.dispatch(g) is None
        assert "couldn't post" in g.modlog.sent[-1].embed.title
        g.news = good_news
        assert await cog.dispatch(g) == "normal"  # the queue moves on (a leftover from step 4)...
        assert g.news.sent[-1].embed.title.startswith("Batch")  # ...and the rejected one isn't retried
        row = await db.fetchone("SELECT kind FROM news_posts WHERE url = ?", (newsfeed.canonical_url("https://news.example/minor-update"),))
        assert row["kind"] == "failed"

        # 8. /news off stops posting; /news test posts the latest item now; status works.
        ctx = Ctx(g)
        await cog.news_off_cmd.callback(cog, ctx)
        assert not news_cog.news_on(g) and cog.guilds() == []
        feeds_now[:] = [art("While off", 0.1)]
        await cog.poll()
        clock.t += HOUR
        assert await cog.dispatch(g) is None
        await cog.news_on_cmd.callback(cog, ctx)
        assert news_cog.news_on(g) and "on" in ctx.replies[-1]
        count = len(g.news.sent)
        await cog.news_test.callback(cog, ctx)
        assert len(g.news.sent) == count + 1 and ctx.replies[-1].startswith("✅"), ctx.replies[-1]
        await cog.news.callback(cog, ctx)
        assert "AI news" in ctx.replies[-1] and g.news.mention in ctx.replies[-1]

        # 9. Config: NEWS_CHANNEL_ID is a /setup-link setting and shows in /setup-show.
        assert "NEWS_CHANNEL_ID" in setup_cog.LINK_KEYS and "NEWS_CHANNEL_ID" in gc.CHANNEL_KEYS
        assert setup_cog.resolve_link_key("news") == "NEWS_CHANNEL_ID"
        assert setup_cog.resolve_link_key("NEWS_CHANNEL_ID") == "NEWS_CHANNEL_ID"
        assert gc.saved(g)["NEWS_CHANNEL_ID"] == g.news.id
    finally:
        ai.key_set, ai.news_review = real_key_set, real_review
        await db.close()


def test_feed_files():
    assert "ai_news" not in content.load_feeds()  # the weekly digest is unchanged
    names = {n for n, _ in content.load_news_feeds() if n}
    for source in ("OpenAI", "Anthropic", "Google DeepMind", "The Verge", "TechCrunch",
                   "Ars Technica", "MIT Technology Review"):
        assert source in names, source
    assert "VentureBeat" not in names  # removed (feed kept rate-limiting / failing)
    assert not any("venturebeat" in u for _, u in content.load_news_feeds())
    urls = [u for _, u in content.load_news_feeds()]
    assert len(urls) == len(set(urls))


def test_parsing():
    assert newsfeed.canonical_url("HTTPS://Example.com/a/?utm_source=x&id=3#frag") == "https://example.com/a?id=3"
    assert newsfeed.parse_review('{"summary": "One. Two. Three.", "score": 12}') == ("One. Two.", 10)
    assert newsfeed.parse_review("garbage") == (None, None)
    assert newsfeed.first_sentences("<p>Hello <b>world</b>. Second! Third?</p>") == "Hello world. Second!"
    entry = {"title": "T", "link": "https://x.example/p", "summary": '<img src="https://img.example/a.jpg"> Text.'}
    parsed = types.SimpleNamespace(feed={"title": "Feed"}, entries=[entry])
    [a] = newsfeed.parse_entries(parsed, "https://x.example/rss")
    assert a.image == "https://img.example/a.jpg" and a.source == "Feed" and a.summary == "Text."


def test_news():
    asyncio.run(_run())


if __name__ == "__main__":
    test_feed_files()
    test_parsing()
    test_news()
    print("tests/test_news.py: all checks passed")
