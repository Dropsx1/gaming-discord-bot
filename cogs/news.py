"""Live AI news: polls AI news RSS feeds and posts each new article once.

- Feeds: `ai_news` + `ai` in data/rss_feeds.json, polled every NEWS_POLL_MINUTES (20).
- Each article (deduped by URL in SQLite) is posted once per server in NEWS_CHANNEL_ID
  (/setup-link NEWS_CHANNEL_ID) with its title, source, a 1-2 sentence summary, link and image.
- With XAI_API_KEY the AI writes the summary and scores importance 1-10. 9+ = BREAKING:
  red embed, pings the "AI News" role, posts right away (even in quiet hours).
- Normal posts: best first, at most 1 per 30 minutes and 12 per day, none 1-7 AM (TIMEZONE).
- Never posts anything older than 24 hours, so the first run doesn't flood the channel.
- A bad feed is skipped (and reported to the mod log if it keeps failing); nothing here crashes the bot.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import ai, content
from utils import guildconfig as gc
from utils import news as newsfeed
from utils.helpers import send_log, trim
from utils.names import normalize_name

logger = logging.getLogger("bot.news")

FEED_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; GamingDiscordBot/1.0; AI news feed)"}
FEED_TIMEOUT = aiohttp.ClientTimeout(total=20)
FEED_MAX_BYTES = 5_000_000
DISPATCH_MINUTES = 2          # how often queued news is checked against the limits
AI_REVIEWS_PER_POLL = 20      # cost guard: later new articles that poll keep the feed's own summary
FEED_ALERT_AFTER = 3          # consecutive failed polls before a feed is reported to the mod log
REPORT_COOLDOWN = 3600        # same problem reported to a server's mod log at most once an hour
KEEP_DAYS = 30
ROLE_NAME = "AI News"


def now() -> float:
    return time.time()  # tests replace this


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def local_now() -> datetime:
    return datetime.fromtimestamp(now(), _tz())


def day_start() -> int:
    return int(local_now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def quiet_hours() -> bool:
    start, end, hour = config.NEWS_QUIET_START, config.NEWS_QUIET_END, local_now().hour
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def news_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    ch = gc.channel(guild, "NEWS_CHANNEL_ID")
    return ch if isinstance(ch, discord.TextChannel) else None


def news_on(guild: discord.Guild) -> bool:
    return bool(gc.saved(guild).get("NEWS_ENABLED", 1))  # on by default once a channel is set


def news_role(guild: discord.Guild) -> Optional[discord.Role]:
    role = gc.role(guild, "NEWS_ROLE_ID")
    if role:
        return role
    wanted = normalize_name(ROLE_NAME)
    return discord.utils.find(lambda r: normalize_name(r.name) == wanted, guild.roles)


def build_embed(row, breaking: bool = False) -> discord.Embed:
    summary = row["summary"] or ""
    link = f"[Read the full story →]({row['url']})"
    embed = discord.Embed(
        title=trim(row["title"], 256),
        url=row["url"],
        description=trim(f"{summary}\n\n{link}" if summary else link, 4096),
        color=discord.Color.red() if breaking else discord.Color.blurple(),
    )
    embed.set_author(name=trim(f"🚨 BREAKING · {row['source']}" if breaking else f"📰 {row['source']}", 256))
    if row["score"]:
        embed.set_footer(text=f"AI importance: {row['score']}/10")
    if row["image"]:
        embed.set_image(url=row["image"])
    if row["published_at"]:
        embed.timestamp = datetime.fromtimestamp(row["published_at"], _tz())
    return embed


@dataclass
class PollResult:
    feeds: int = 0
    failed: list[str] = field(default_factory=list)
    new: int = 0
    fresh: int = 0
    reviewed: int = 0


class News(commands.Cog):
    """Live AI news feed."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.feed_failures: dict[str, int] = {}
        self.reported: dict[tuple[int, str], float] = {}
        self.poll_lock = asyncio.Lock()
        self.post_lock = asyncio.Lock()
        self.last_poll: Optional[PollResult] = None

    @property
    def db(self):
        return self.bot.db

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.poll_loop.is_running():
            self.poll_loop.change_interval(minutes=config.NEWS_POLL_MINUTES)
            self.poll_loop.start()
            self.dispatch_loop.start()
            logger.info("AI news feed started (poll every %d min)", config.NEWS_POLL_MINUTES)

    async def cog_unload(self):
        self.poll_loop.cancel()
        self.dispatch_loop.cancel()

    def guilds(self) -> list[discord.Guild]:
        return [g for g in self.bot.guilds if news_on(g) and news_channel(g)]

    # ------------------------------------------------------------- reporting
    async def report(self, guild: discord.Guild, key: str, title: str, detail: str) -> None:
        """Tell staff in the mod log, at most once an hour per problem. Never raises."""
        stamp = now()
        if stamp - self.reported.get((guild.id, key), -1e12) < REPORT_COOLDOWN:
            return
        self.reported[(guild.id, key)] = stamp
        embed = discord.Embed(title=f"📰 AI news: {title}", description=trim(detail, 4000), color=discord.Color.orange())
        try:
            await send_log(guild, gc.get(guild, "MOD_LOG_CHANNEL_ID"), embed)
        except Exception:
            logger.exception("Could not report to the mod log in %s", guild)

    async def report_all(self, key: str, title: str, detail: str) -> None:
        for guild in self.guilds():
            await self.report(guild, key, title, detail)

    # ----------------------------------------------------------------- feeds
    async def fetch(self, session: aiohttp.ClientSession, name: Optional[str], url: str) -> list[newsfeed.Article]:
        """Articles from one feed, newest first. Raises on any failure (the caller catches it)."""
        async with session.get(url, headers=FEED_HEADERS) as resp:
            resp.raise_for_status()
            data = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                data += chunk
                if len(data) > FEED_MAX_BYTES:
                    raise ValueError("feed larger than 5 MB")
        parsed = await asyncio.to_thread(feedparser.parse, bytes(data))
        articles = newsfeed.parse_entries(parsed, url, name)
        if not articles:
            raise ValueError("no articles in the feed" + (f" ({parsed.bozo_exception})" if parsed.get("bozo") else ""))
        return articles

    async def fetch_all(self, feeds) -> tuple[list[newsfeed.Article], list[str]]:
        async with aiohttp.ClientSession(timeout=FEED_TIMEOUT, trust_env=True) as session:
            results = await asyncio.gather(*(self.fetch(session, n, u) for n, u in feeds), return_exceptions=True)
        articles, failed = [], []
        for (name, url), result in zip(feeds, results):
            if isinstance(result, BaseException):
                failed.append(url)
                count = self.feed_failures[url] = self.feed_failures.get(url, 0) + 1
                reason = f"{type(result).__name__}: {result}"[:300]
                logger.warning("AI news feed failed (%d in a row): %s (%s)", count, url, reason)
                if count == FEED_ALERT_AFTER:
                    await self.report_all(
                        f"feed:{url}", "a feed keeps failing",
                        f"**{name or url}** failed {count} checks in a row and is being skipped until it works again.\n"
                        f"`{url}`\nLast error: `{reason}`\nEdit `data/rss_feeds.json` to change or remove it.",
                    )
            else:
                if self.feed_failures.pop(url, 0) >= FEED_ALERT_AFTER:
                    logger.info("AI news feed is working again: %s", url)
                articles.extend(result)
        return articles, failed

    async def review(self, article: newsfeed.Article, billed_guild: int) -> bool:
        reply = await ai.news_review(article.title, article.source, article.summary)
        try:
            await self.db.log_ai(billed_guild, 0, "news", reply.tokens if reply else 0, ok=bool(reply))
        except Exception:
            logger.exception("Could not log AI usage")
        summary, score = newsfeed.parse_review(reply.text if reply else "")
        if summary:
            article.summary = summary
        article.score = score
        return score is not None

    async def poll(self) -> PollResult:
        """Fetch every feed and store new articles (AI-reviewed when there's a key)."""
        async with self.poll_lock:
            feeds = content.load_news_feeds()
            result = PollResult(feeds=len(feeds))
            articles, result.failed = await self.fetch_all(feeds)

            stamp = int(now())
            unique: dict[str, newsfeed.Article] = {}
            for a in articles:
                unique.setdefault(a.url, a)
            known = await self.db.news_known(list(unique))
            new = [a for a in unique.values() if a.url not in known]

            seen_feeds: dict[str, bool] = {}
            for a in new:
                if a.published_at > stamp:
                    a.published_at = stamp  # a feed dated it in the future
                elif not a.published_at:
                    # Undated: new now if we already follow this feed; on a feed's first run it counts as old.
                    if a.feed not in seen_feeds:
                        seen_feeds[a.feed] = await self.db.news_feed_seen(a.feed)
                    a.published_at = stamp if seen_feeds[a.feed] else 0

            cutoff = stamp - config.NEWS_MAX_AGE_HOURS * 3600
            fresh = [a for a in new if a.published_at >= cutoff]
            if fresh and ai.key_set():
                guilds = self.guilds()
                billed = guilds[0].id if guilds else 0
                fresh.sort(key=lambda a: a.published_at, reverse=True)
                outcomes = await asyncio.gather(
                    *(self.review(a, billed) for a in fresh[:AI_REVIEWS_PER_POLL]), return_exceptions=True
                )
                result.reviewed = sum(1 for o in outcomes if o is True)

            await self.db.add_news_items(new, stamp)
            await self.db.prune_news(stamp - KEEP_DAYS * 86400)
            result.new, result.fresh = len(new), len(fresh)
            self.last_poll = result
            logger.info("AI news poll: %d new (%d recent, %d AI-reviewed), %d/%d feeds failed",
                        result.new, result.fresh, result.reviewed, len(result.failed), result.feeds)
            return result

    # --------------------------------------------------------------- posting
    async def post(self, guild: discord.Guild, channel: discord.TextChannel, row, kind: str) -> Optional[discord.Message]:
        breaking = kind == "breaking"
        role = news_role(guild) if breaking else None
        try:
            msg = await channel.send(
                content=f"🚨 {role.mention}" if role else None,
                embed=build_embed(row, breaking),
                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role] if role else False),
            )
        except discord.HTTPException as e:
            permanent = getattr(e, "status", 0) == 400  # Discord rejected this article's embed: skip it
            if permanent:
                await self.db.add_news_post(guild.id, row["url"], "failed", None, int(now()))
            logger.warning("Could not post AI news in %s #%s: %s", guild, channel, e)
            await self.report(
                guild, "skipped" if permanent else "post", "couldn't post",
                f"Posting in {channel.mention} failed: `{e}`."
                + (f"\nSkipped: {row['url']}" if permanent else
                   "\nCheck that I can **View Channel**, **Send Messages** and **Embed Links** there."),
            )
            return None
        await self.db.add_news_post(guild.id, row["url"], kind, msg.id, int(now()))
        logger.info("Posted %s AI news in %s: %s", kind, guild, row["url"])
        return msg

    async def dispatch(self, guild: discord.Guild) -> Optional[str]:
        """Post at most one queued article if the limits allow. Returns its kind, or None."""
        channel = news_channel(guild)
        if channel is None or not news_on(guild):
            return None
        async with self.post_lock:
            stamp = int(now())
            today = day_start()
            if ai.key_set():
                breaking = await self.db.news_candidates(
                    guild.id, stamp - config.NEWS_BREAKING_MAX_AGE_HOURS * 3600, min_score=config.NEWS_BREAKING_SCORE
                )
                if breaking and await self.db.count_news_posts(guild.id, today, ("breaking",)) < config.NEWS_BREAKING_DAILY_LIMIT:
                    return "breaking" if await self.post(guild, channel, breaking[0], "breaking") else None

            if quiet_hours():
                return None
            if await self.db.count_news_posts(guild.id, today, ("normal", "breaking")) >= config.NEWS_DAILY_LIMIT:
                return None
            if stamp - await self.db.last_news_post(guild.id, "normal") < config.NEWS_MIN_GAP_MINUTES * 60:
                return None
            queue = await self.db.news_candidates(guild.id, stamp - config.NEWS_MAX_AGE_HOURS * 3600)
            if not queue:
                return None
            return "normal" if await self.post(guild, channel, queue[0], "normal") else None

    # ----------------------------------------------------------------- loops
    @tasks.loop(minutes=20)
    async def poll_loop(self):
        if not self.guilds():
            return  # nobody has a news channel: don't fetch or spend AI calls
        try:
            await self.poll()
            await self.dispatch_all()
        except Exception as e:
            logger.exception("AI news poll failed")
            await self.report_all("poll", "check failed", f"`{type(e).__name__}: {e}`"[:500] + "\nI'll try again next time.")

    @tasks.loop(minutes=DISPATCH_MINUTES)
    async def dispatch_loop(self):
        await self.dispatch_all()

    async def dispatch_all(self):
        for guild in self.guilds():
            try:
                await self.dispatch(guild)
            except Exception as e:
                logger.exception("AI news dispatch failed in %s", guild)
                await self.report(guild, "dispatch", "posting failed", f"`{type(e).__name__}: {e}`"[:500])

    @poll_loop.before_loop
    @dispatch_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------- commands
    @commands.hybrid_group(name="news", description="Live AI news feed (admin)", fallback="status")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def news(self, ctx: commands.Context):
        channel = news_channel(ctx.guild)
        stamp, today = int(now()), day_start()
        posted = await self.db.count_news_posts(ctx.guild.id, today, ("normal", "breaking"))
        queued = len(await self.db.news_candidates(ctx.guild.id, stamp - config.NEWS_MAX_AGE_HOURS * 3600))
        lines = [
            f"**Status:** {'on' if news_on(ctx.guild) else 'off'}",
            f"**Channel:** {channel.mention if channel else 'not set (`/setup-link NEWS_CHANNEL_ID`)'}",
            f"**AI summaries + breaking news:** {'on' if ai.key_set() else 'off (no XAI_API_KEY; feed summaries are used)'}",
            f"**Posted today:** {posted}/{config.NEWS_DAILY_LIMIT} · **Waiting:** {queued}",
            f"**Limits:** 1 post per {config.NEWS_MIN_GAP_MINUTES} min, quiet {config.NEWS_QUIET_START}:00–"
            f"{config.NEWS_QUIET_END}:00 ({config.TIMEZONE}); breaking news skips both",
        ]
        role = news_role(ctx.guild)
        lines.append(f"**Breaking ping:** {role.mention if role else f'none (create a role named **{ROLE_NAME}**)'}")
        if self.feed_failures:
            lines.append(f"**Feeds failing:** {len(self.feed_failures)}")
        await ctx.send("📰 **AI news**\n" + "\n".join(lines), ephemeral=True,
                       allowed_mentions=discord.AllowedMentions.none())

    @news.command(name="on", description="Turn the AI news feed on (admin)")
    @commands.has_permissions(manage_guild=True)
    async def news_on_cmd(self, ctx: commands.Context):
        await gc.set_many(ctx.guild, {"NEWS_ENABLED": 1})
        logger.info("%s turned AI news on in %s", ctx.author, ctx.guild)
        channel = news_channel(ctx.guild)
        msg = "📰 AI news is **on**."
        if channel is None:
            msg += "\n⚠️ No news channel yet: run `/setup-link key:NEWS_CHANNEL_ID channel:#your-channel`."
        else:
            msg += f" New articles will appear in {channel.mention}."
            perms = channel.permissions_for(ctx.guild.me)
            if not (perms.send_messages and perms.embed_links):
                msg += f"\n⚠️ I need **Send Messages** and **Embed Links** in {channel.mention}."
        if not ai.key_set():
            msg += "\nℹ️ No xAI key: summaries come from the feeds, and there are no BREAKING alerts."
        await ctx.send(msg, ephemeral=True)

    @news.command(name="off", description="Turn the AI news feed off (admin)")
    @commands.has_permissions(manage_guild=True)
    async def news_off_cmd(self, ctx: commands.Context):
        await gc.set_many(ctx.guild, {"NEWS_ENABLED": 0})
        logger.info("%s turned AI news off in %s", ctx.author, ctx.guild)
        await ctx.send("📰 AI news is **off**. Turn it back on with `/news on`.", ephemeral=True)

    @news.command(name="test", description="Post the latest AI news item now (admin)")
    @commands.has_permissions(manage_guild=True)
    async def news_test(self, ctx: commands.Context):
        channel = news_channel(ctx.guild)
        if channel is None:
            await ctx.send("⚠️ No news channel yet: run `/setup-link key:NEWS_CHANNEL_ID channel:#your-channel`.",
                           ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        row = await self.db.latest_news()
        if row is None:
            try:
                await self.poll()
            except Exception as e:
                logger.exception("AI news test poll failed")
                await ctx.send(f"⚠️ Couldn't load the feeds: `{e}`", ephemeral=True)
                return
            row = await self.db.latest_news()
        if row is None:
            await ctx.send("⚠️ None of the feeds returned an article. Check the bot logs.", ephemeral=True)
            return
        msg = await self.post(ctx.guild, channel, row, "test")
        if msg is None:
            await ctx.send(f"⚠️ I couldn't post in {channel.mention}. Check my permissions there.", ephemeral=True)
            return
        await ctx.send(f"✅ Posted the latest item in {channel.mention}: {msg.jump_url}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(News(bot))
