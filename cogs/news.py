"""Live news: polls RSS feeds and posts each new article once. Two independent feeds share this code:

- AI news (`ai_news` + `ai` feeds -> NEWS_CHANNEL_ID, "AI News" ping role) and
- Gaming news (`gaming_news` feeds -> GAMING_NEWS_CHANNEL_ID, GAMING_NEWS_PING_ROLE_ID or a "Gaming News" role),
  scored by the AI for importance to gamers. It only runs where GAMING_NEWS_CHANNEL_ID is set.
  Articles and posts are tagged with their feed topic, so the two feeds never cross-post.

Details below describe the AI feed; the gaming feed uses the same limits.

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
from datetime import datetime, time as dtime
from typing import Literal, Optional
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
BACKOFF_START = 30 * 60       # first wait after an HTTP 429 (unless the feed sends Retry-After)
BACKOFF_MAX = 6 * 3600        # waits double on repeated 429s up to this
DUPLICATE_WINDOW_HOURS = 48   # cross-outlet duplicates are merged against stories this recent
RECAP_MIN_SCORE = 5           # daily/weekly AI recaps use stories scored at least this


class FeedRateLimited(Exception):
    """The feed answered HTTP 429: back off; this is not a failure and never alerts staff."""

    def __init__(self, retry_after: Optional[float]):
        super().__init__(f"HTTP 429 (retry after {retry_after or '?'}s)")
        self.retry_after = retry_after


def _retry_after(value: Optional[str]) -> Optional[float]:
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None  # an HTTP date: use our own backoff
ROLE_NAME = "AI News"


@dataclass(frozen=True)
class FeedSpec:
    """One independent live news feed. Loader/review are looked up by name so tests can patch them."""
    topic: str
    label: str
    channel_key: str
    enabled_key: str
    role_key: str
    role_name: str
    color: discord.Color
    feeds_loader: str      # function name in utils.content
    reviewer: str          # function name in utils.ai
    footer: Optional[str] = None

    def load_feeds(self):
        return getattr(content, self.feeds_loader)()

    async def review(self, *args):
        return await getattr(ai, self.reviewer)(*args)


AI_FEED = FeedSpec("ai", "AI news", "NEWS_CHANNEL_ID", "NEWS_ENABLED", "NEWS_ROLE_ID", ROLE_NAME,
                   discord.Color.blurple(), "load_news_feeds", "news_review")
GAMING_FEED = FeedSpec("gaming", "Gaming news", "GAMING_NEWS_CHANNEL_ID", "GAMING_NEWS_ENABLED",
                       "GAMING_NEWS_PING_ROLE_ID", "Gaming News", discord.Color.from_str("#00E5FF"),
                       "load_gaming_news_feeds", "gaming_news_review", footer="🎮 Gaming News")
FEEDS = {"ai": AI_FEED, "gaming": GAMING_FEED}


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


def news_channel(guild: discord.Guild, spec: FeedSpec = AI_FEED) -> Optional[discord.TextChannel]:
    ch = gc.channel(guild, spec.channel_key)
    return ch if isinstance(ch, discord.TextChannel) else None


def news_on(guild: discord.Guild, spec: FeedSpec = AI_FEED) -> bool:
    return bool(gc.saved(guild).get(spec.enabled_key, 1))  # on by default once a channel is set


def news_role(guild: discord.Guild, spec: FeedSpec = AI_FEED) -> Optional[discord.Role]:
    role = gc.role(guild, spec.role_key)
    if role:
        return role
    wanted = normalize_name(spec.role_name)
    return discord.utils.find(lambda r: normalize_name(r.name) == wanted, guild.roles)


def build_embed(row, breaking: bool = False, also: Optional[list[str]] = None,
                spec: FeedSpec = AI_FEED) -> discord.Embed:
    summary = row["summary"] or ""
    link = f"[Read the full story →]({row['url']})"
    embed = discord.Embed(
        title=trim(row["title"], 256),
        url=row["url"],
        description=trim(f"{summary}\n\n{link}" if summary else link, 4096),
        color=discord.Color.red() if breaking else spec.color,
    )
    embed.set_author(name=trim(f"🚨 BREAKING · {row['source']}" if breaking else f"📰 {row['source']}", 256))
    footer = [spec.footer] if spec.footer else []
    if row["score"]:
        footer.append(f"AI importance: {row['score']}/10")
    if also:
        footer.append("Also covered by " + ", ".join(also[:4]))
    if footer:
        embed.set_footer(text=trim(" • ".join(footer), 2048))
    if row["image"]:
        embed.set_image(url=row["image"])
    if row["published_at"]:
        embed.timestamp = datetime.fromtimestamp(row["published_at"], _tz())
    return embed


async def build_recap(db, since: int, period: str, billed_guild: int, limit: int = 8) -> Optional[discord.Embed]:
    """AI-written recap of the top-scored stories since `since` ("week" or "day"). None if AI is off,
    there are no scored stories, or the AI call failed (callers just skip the recap)."""
    if not ai.key_set():
        return None
    rows = await db.top_news(since, limit=limit, min_score=RECAP_MIN_SCORE)
    if not rows:
        return None
    stories = [{"title": r["title"], "source": r["source"], "summary": r["summary"], "score": r["score"]} for r in rows]
    reply = await ai.news_recap(stories, period)
    try:
        await db.log_ai(billed_guild, 0, "digest" if period == "week" else "news-recap",
                        reply.tokens if reply else 0, ok=bool(reply))
    except Exception:
        logger.exception("Could not log AI usage")
    if reply is None:
        return None
    links = "\n".join(f"[{trim(r['title'], 90)}]({r['url']}) · {r['source']}" for r in rows)
    title = "🤖 AI recap: top stories of the week" if period == "week" else "🤖 AI recap: today's top stories"
    embed = discord.Embed(title=title, description=trim(reply.text, 3000), color=discord.Color.dark_purple())
    embed.add_field(name="Read more", value=trim(links, 1024), inline=False)
    embed.set_footer(text="✨ AI-written from the highest-scored stories • can be wrong")
    return embed


def recap_time():
    return dtime(min(23, max(0, config.NEWS_DAILY_RECAP_HOUR)), 30, tzinfo=_tz())


@dataclass
class PollResult:
    feeds: int = 0
    failed: list[str] = field(default_factory=list)
    new: int = 0
    fresh: int = 0
    reviewed: int = 0
    merged: int = 0


class News(commands.Cog):
    """Live AI news feed."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.feed_failures: dict[str, int] = {}
        self.backoff: dict[str, tuple[float, float]] = {}  # url -> (retry at, last wait) after HTTP 429
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
            logger.info("News feeds started (poll every %d min)", config.NEWS_POLL_MINUTES)

        if not self.recap_loop.is_running():
            self.recap_loop.start()

    async def cog_unload(self):
        self.poll_loop.cancel()
        self.dispatch_loop.cancel()
        self.recap_loop.cancel()

    def guilds(self, spec: FeedSpec = AI_FEED) -> list[discord.Guild]:
        return [g for g in self.bot.guilds if news_on(g, spec) and news_channel(g, spec)]

    def active_guilds(self) -> list[discord.Guild]:
        """Servers where any feed is running (for mod-log reports)."""
        seen: dict[int, discord.Guild] = {}
        for spec in FEEDS.values():
            for g in self.guilds(spec):
                seen.setdefault(g.id, g)
        return list(seen.values())

    # ------------------------------------------------------------- reporting
    async def report(self, guild: discord.Guild, key: str, title: str, detail: str) -> None:
        """Tell staff in the mod log, at most once an hour per problem. Never raises."""
        stamp = now()
        if stamp - self.reported.get((guild.id, key), -1e12) < REPORT_COOLDOWN:
            return
        self.reported[(guild.id, key)] = stamp
        embed = discord.Embed(title=f"📰 News feed: {title}", description=trim(detail, 4000), color=discord.Color.orange())
        try:
            await send_log(guild, gc.get(guild, "MOD_LOG_CHANNEL_ID"), embed)
        except Exception:
            logger.exception("Could not report to the mod log in %s", guild)

    async def report_all(self, key: str, title: str, detail: str) -> None:
        for guild in self.active_guilds():
            await self.report(guild, key, title, detail)

    # ----------------------------------------------------------------- feeds
    async def fetch(self, session: aiohttp.ClientSession, name: Optional[str], url: str) -> list[newsfeed.Article]:
        """Articles from one feed, newest first. Raises on any failure (the caller catches it)."""
        async with session.get(url, headers=FEED_HEADERS) as resp:
            if resp.status == 429:
                raise FeedRateLimited(_retry_after(resp.headers.get("Retry-After")))
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

    def back_off(self, url: str, retry_after: Optional[float]) -> float:
        """Remember a 429: skip the feed until the wait is over. Returns the wait in seconds."""
        _, last = self.backoff.get(url, (0.0, 0.0))
        wait = retry_after if retry_after else min(BACKOFF_MAX, max(BACKOFF_START, last * 2))
        wait = min(BACKOFF_MAX, max(60.0, wait))
        self.backoff[url] = (now() + wait, wait)
        return wait

    async def fetch_all(self, feeds) -> tuple[list[newsfeed.Article], list[str]]:
        stamp = now()
        due = [(n, u) for n, u in feeds if self.backoff.get(u, (0.0, 0.0))[0] <= stamp]
        skipped = len(feeds) - len(due)
        if skipped:
            logger.info("AI news: %d feed(s) backing off after HTTP 429", skipped)
        async with aiohttp.ClientSession(timeout=FEED_TIMEOUT, trust_env=True) as session:
            results = await asyncio.gather(*(self.fetch(session, n, u) for n, u in due), return_exceptions=True)
        articles, failed = [], []
        for (name, url), result in zip(due, results):
            if isinstance(result, FeedRateLimited):
                wait = self.back_off(url, result.retry_after)
                logger.info("AI news feed rate-limited (429): %s; trying again in %.0f min", url, wait / 60)
                continue  # not a failure: no failure count, no mod-log alert
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
                self.backoff.pop(url, None)
                if self.feed_failures.pop(url, 0) >= FEED_ALERT_AFTER:
                    logger.info("AI news feed is working again: %s", url)
                articles.extend(result)
        return articles, failed

    async def review(self, article: newsfeed.Article, billed_guild: int, spec: FeedSpec = AI_FEED) -> bool:
        reply = await spec.review(article.title, article.source, article.summary)
        try:
            await self.db.log_ai(billed_guild, 0, "news", reply.tokens if reply else 0, ok=bool(reply))
        except Exception:
            logger.exception("Could not log AI usage")
        summary, score = newsfeed.parse_review(reply.text if reply else "")
        if summary:
            article.summary = summary
        article.score = score
        return score is not None

    async def poll(self, spec: FeedSpec = AI_FEED) -> PollResult:
        """Fetch every feed of one topic and store new articles (AI-reviewed when there's a key)."""
        async with self.poll_lock:
            feeds = spec.load_feeds()
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

            # Same story from several outlets: keep the first, merge the rest (never reviewed or posted).
            recent = await self.db.recent_news_titles(stamp - DUPLICATE_WINDOW_HOURS * 3600, spec.topic)
            result.merged = newsfeed.merge_duplicates(new, recent)

            cutoff = stamp - config.NEWS_MAX_AGE_HOURS * 3600
            fresh = [a for a in new if a.published_at >= cutoff and not a.dup_of]
            if fresh and ai.key_set():
                guilds = self.guilds(spec)
                billed = guilds[0].id if guilds else 0
                fresh.sort(key=lambda a: a.published_at, reverse=True)
                outcomes = await asyncio.gather(
                    *(self.review(a, billed, spec) for a in fresh[:AI_REVIEWS_PER_POLL]), return_exceptions=True
                )
                result.reviewed = sum(1 for o in outcomes if o is True)

            await self.db.add_news_items(new, stamp, spec.topic)
            await self.db.prune_news(stamp - KEEP_DAYS * 86400)
            result.new, result.fresh = len(new), len(fresh)
            self.last_poll = result
            logger.info("%s poll: %d new (%d recent, %d AI-reviewed, %d duplicates merged), %d/%d feeds failed",
                        spec.label, result.new, result.fresh, result.reviewed, result.merged, len(result.failed), result.feeds)
            return result

    # --------------------------------------------------------------- posting
    async def post(self, guild: discord.Guild, channel: discord.TextChannel, row, kind: str,
                   spec: FeedSpec = AI_FEED) -> Optional[discord.Message]:
        breaking = kind == "breaking"
        role = news_role(guild, spec) if breaking else None
        try:
            msg = await channel.send(
                content=f"🚨 {role.mention}" if role else None,
                embed=build_embed(row, breaking, await self.db.news_dup_sources(row["url"]), spec),
                allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role] if role else False),
            )
        except discord.HTTPException as e:
            permanent = getattr(e, "status", 0) == 400  # Discord rejected this article's embed: skip it
            if permanent:
                await self.db.add_news_post(guild.id, row["url"], "failed", None, int(now()), spec.topic)
            logger.warning("Could not post AI news in %s #%s: %s", guild, channel, e)
            await self.report(
                guild, "skipped" if permanent else "post", "couldn't post",
                f"Posting in {channel.mention} failed: `{e}`."
                + (f"\nSkipped: {row['url']}" if permanent else
                   "\nCheck that I can **View Channel**, **Send Messages** and **Embed Links** there."),
            )
            return None
        await self.db.add_news_post(guild.id, row["url"], kind, msg.id, int(now()), spec.topic)
        logger.info("Posted %s %s in %s: %s", kind, spec.label, guild, row["url"])
        return msg

    async def dispatch(self, guild: discord.Guild, spec: FeedSpec = AI_FEED) -> Optional[str]:
        """Post at most one queued article of this feed if the limits allow. Returns its kind, or None."""
        channel = news_channel(guild, spec)
        if channel is None or not news_on(guild, spec):
            return None
        async with self.post_lock:
            stamp = int(now())
            today = day_start()
            if ai.key_set():
                breaking = await self.db.news_candidates(
                    guild.id, stamp - config.NEWS_BREAKING_MAX_AGE_HOURS * 3600, min_score=config.NEWS_BREAKING_SCORE,
                    topic=spec.topic,
                )
                if breaking and await self.db.count_news_posts(guild.id, today, ("breaking",), spec.topic) \
                        < config.NEWS_BREAKING_DAILY_LIMIT:
                    return "breaking" if await self.post(guild, channel, breaking[0], "breaking", spec) else None

            if quiet_hours():
                return None
            if await self.db.count_news_posts(guild.id, today, ("normal", "breaking"), spec.topic) >= config.NEWS_DAILY_LIMIT:
                return None
            if stamp - await self.db.last_news_post(guild.id, "normal", spec.topic) < config.NEWS_MIN_GAP_MINUTES * 60:
                return None
            queue = await self.db.news_candidates(guild.id, stamp - config.NEWS_MAX_AGE_HOURS * 3600, topic=spec.topic)
            if not queue:
                return None
            return "normal" if await self.post(guild, channel, queue[0], "normal", spec) else None

    # ----------------------------------------------------------------- loops
    @tasks.loop(minutes=20)
    async def poll_loop(self):
        for spec in FEEDS.values():
            if not self.guilds(spec):
                continue  # nobody has this feed's channel: don't fetch or spend AI calls
            try:
                await self.poll(spec)
            except Exception as e:
                logger.exception("%s poll failed", spec.label)
                for guild in self.guilds(spec):
                    await self.report(guild, f"poll:{spec.topic}", f"{spec.label} check failed",
                                      f"`{type(e).__name__}: {e}`"[:500] + "\nI'll try again next time.")
        await self.dispatch_all()

    @tasks.loop(minutes=DISPATCH_MINUTES)
    async def dispatch_loop(self):
        await self.dispatch_all()

    async def dispatch_all(self):
        for spec in FEEDS.values():
            for guild in self.guilds(spec):
                try:
                    await self.dispatch(guild, spec)
                except Exception as e:
                    logger.exception("%s dispatch failed in %s", spec.label, guild)
                    await self.report(guild, f"dispatch:{spec.topic}", f"{spec.label} posting failed",
                                      f"`{type(e).__name__}: {e}`"[:500])

    async def post_daily_recap(self, guild: discord.Guild, force: bool = False) -> str:
        """Optional daily AI recap (/news daily on) in the news channel, once per day."""
        channel = news_channel(guild)
        if channel is None:
            return "No news channel is set."
        key = f"{guild.id}:{local_now().date().isoformat()}"
        if not force and await self.db.job_ran("news_daily", key):
            return "Today's recap was already posted."
        embed = await build_recap(self.db, int(now()) - 86400, "day", guild.id)
        if embed is None:
            return "No recap today (AI is off, or there were no high-scored stories)."
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            logger.warning("Could not post the daily AI news recap in %s: %s", guild, e)
            return f"I couldn't post in {channel.mention}."
        if not force:
            await self.db.mark_job("news_daily", key)
        return f"Posted today's recap in {channel.mention}."

    @tasks.loop(time=recap_time())
    async def recap_loop(self):
        for guild in self.guilds():
            if not gc.saved(guild).get("NEWS_DAILY_RECAP", 0):
                continue
            try:
                await self.post_daily_recap(guild)
            except Exception:
                logger.exception("Daily AI news recap failed in %s", guild)

    @poll_loop.before_loop
    @dispatch_loop.before_loop
    @recap_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # -------------------------------------------------------------- commands
    @commands.hybrid_group(name="news", description="Live AI + gaming news feeds (admin)", fallback="status")
    @app_commands.describe(feed="Which feed: ai (default) or gaming")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def news(self, ctx: commands.Context, feed: Literal["ai", "gaming"] = "ai"):
        spec = FEEDS[feed]
        channel = news_channel(ctx.guild, spec)
        stamp, today = int(now()), day_start()
        posted = await self.db.count_news_posts(ctx.guild.id, today, ("normal", "breaking"), spec.topic)
        queued = len(await self.db.news_candidates(ctx.guild.id, stamp - config.NEWS_MAX_AGE_HOURS * 3600, topic=spec.topic))
        lines = [
            f"**Status:** {'on' if news_on(ctx.guild, spec) else 'off'}",
            f"**Channel:** {channel.mention if channel else f'not set (`/setup-link {spec.channel_key}`)'}",
            f"**AI summaries + breaking news:** {'on' if ai.key_set() else 'off (no XAI_API_KEY; feed summaries are used)'}",
            f"**Posted today:** {posted}/{config.NEWS_DAILY_LIMIT} · **Waiting:** {queued}",
            f"**Limits:** 1 post per {config.NEWS_MIN_GAP_MINUTES} min, quiet {config.NEWS_QUIET_START}:00–"
            f"{config.NEWS_QUIET_END}:00 ({config.TIMEZONE}); breaking news skips both",
        ]
        role = news_role(ctx.guild, spec)
        lines.append(f"**Breaking ping:** {role.mention if role else f'none (create a role named **{spec.role_name}**)'}")
        if self.feed_failures:
            lines.append(f"**Feeds failing:** {len(self.feed_failures)}")
        await ctx.send(f"📰 **{spec.label}**\n" + "\n".join(lines), ephemeral=True,
                       allowed_mentions=discord.AllowedMentions.none())

    @news.command(name="on", description="Turn a news feed on (admin)")
    @app_commands.describe(feed="Which feed: ai (default) or gaming")
    @commands.has_permissions(manage_guild=True)
    async def news_on_cmd(self, ctx: commands.Context, feed: Literal["ai", "gaming"] = "ai"):
        spec = FEEDS[feed]
        await gc.set_many(ctx.guild, {spec.enabled_key: 1})
        logger.info("%s turned %s on in %s", ctx.author, spec.label, ctx.guild)
        channel = news_channel(ctx.guild, spec)
        msg = f"📰 {spec.label} is **on**."
        if channel is None:
            msg += f"\n⚠️ No channel yet: run `/setup-link key:{spec.channel_key} channel:#your-channel`."
        else:
            msg += f" New articles will appear in {channel.mention}."
            perms = channel.permissions_for(ctx.guild.me)
            if not (perms.send_messages and perms.embed_links):
                msg += f"\n⚠️ I need **Send Messages** and **Embed Links** in {channel.mention}."
        if not ai.key_set():
            msg += "\nℹ️ No xAI key: summaries come from the feeds, and there are no BREAKING alerts."
        await ctx.send(msg, ephemeral=True)

    @news.command(name="off", description="Turn a news feed off (admin)")
    @app_commands.describe(feed="Which feed: ai (default) or gaming")
    @commands.has_permissions(manage_guild=True)
    async def news_off_cmd(self, ctx: commands.Context, feed: Literal["ai", "gaming"] = "ai"):
        spec = FEEDS[feed]
        await gc.set_many(ctx.guild, {spec.enabled_key: 0})
        logger.info("%s turned %s off in %s", ctx.author, spec.label, ctx.guild)
        hint = "/news on" if spec is AI_FEED else f"/news on feed:{feed}"
        await ctx.send(f"📰 {spec.label} is **off**. Turn it back on with `{hint}`.", ephemeral=True)

    @news.command(name="daily", description="Daily AI-written recap of the top stories on/off (admin)")
    @app_commands.describe(enabled="on: post a recap every day; off: stop; now: post one now")
    @commands.has_permissions(manage_guild=True)
    async def news_daily_cmd(self, ctx: commands.Context, enabled: Literal["on", "off", "now"]):
        if enabled == "now":
            await ctx.defer(ephemeral=True)
            await ctx.send(f"📰 {await self.post_daily_recap(ctx.guild, force=True)}", ephemeral=True)
            return
        await gc.set_many(ctx.guild, {"NEWS_DAILY_RECAP": int(enabled == "on")})
        logger.info("%s turned the daily AI news recap %s in %s", ctx.author, enabled, ctx.guild)
        msg = f"📰 Daily AI recap is **{enabled}**."
        if enabled == "on":
            msg += f" It posts around {config.NEWS_DAILY_RECAP_HOUR}:30 ({config.TIMEZONE}) in the news channel."
            if not ai.key_set():
                msg += "\nℹ️ No xAI key, so there's nothing to post until one is added."
        await ctx.send(msg, ephemeral=True)

    @news.command(name="test", description="Post the latest item from a news feed now (admin)")
    @app_commands.describe(feed="Which feed: ai (default) or gaming")
    @commands.has_permissions(manage_guild=True)
    async def news_test(self, ctx: commands.Context, feed: Literal["ai", "gaming"] = "ai"):
        spec = FEEDS[feed]
        channel = news_channel(ctx.guild, spec)
        if channel is None:
            await ctx.send(f"⚠️ No channel yet: run `/setup-link key:{spec.channel_key} channel:#your-channel`.",
                           ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        row = await self.db.latest_news(spec.topic)
        if row is None:
            try:
                await self.poll(spec)
            except Exception as e:
                logger.exception("%s test poll failed", spec.label)
                await ctx.send(f"⚠️ Couldn't load the feeds: `{e}`", ephemeral=True)
                return
            row = await self.db.latest_news(spec.topic)
        if row is None:
            await ctx.send("⚠️ None of the feeds returned an article. Check the bot logs.", ephemeral=True)
            return
        msg = await self.post(ctx.guild, channel, row, "test", spec)
        if msg is None:
            await ctx.send(f"⚠️ I couldn't post in {channel.mention}. Check my permissions there.", ephemeral=True)
            return
        await ctx.send(f"✅ Posted the latest item in {channel.mention}: {msg.jump_url}", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(News(bot))
