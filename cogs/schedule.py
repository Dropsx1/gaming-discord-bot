"""Scheduled community content (all times in config.TIMEZONE, default America/New_York).

Daily   18:05  Crown yesterday's Prompt King (most-reacted reply, role for 24h)
Daily   18:07  Prompt of the Day (random unused item from data/prompts.json)
Monday  10:13  Weekly digest (newest RSS headlines + last Remix winner + most active channel)
Friday  16:11  Remix Friday kickoff (random unused theme from data/remix_themes.json)
Sunday  17:09  "Voting closes soon" reminder
Sunday  18:00  Close Remix voting, crown winner (role for 7 days)
Every 10 min   Remove expired temporary roles, save activity counts, catch up missed jobs

Every job records that it ran (job_runs table), so restarts never double-post,
and the 10-minute maintenance loop catches up on anything missed while the
bot was offline. /schedule-test runs any job immediately.
"""

import asyncio
import html
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta
from time import mktime
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import content
from utils import guildconfig as gc
from utils.db import now as utc_now
from utils.helpers import trim

logger = logging.getLogger("bot.schedule")

TZ = ZoneInfo(config.TIMEZONE)
MON, FRI, SUN = 0, 4, 6
CATCH_UP_WINDOW = timedelta(hours=6)  # how late a missed job may still run
FIRE = "🔥"
URL_RE = re.compile(r"https?://\S+")
FEED_HEADERS = {"User-Agent": "UniversalPlayHavenBot/1.0 (+Discord community digest)"}
CATEGORY_TITLES = {"gaming": "🎮 Gaming", "ai": "🤖 AI", "tech": "💻 Tech"}

JobName = Literal["prompt", "prompt_king", "digest", "remix_kickoff", "remix_reminder", "remix_close", "cleanup"]


def local_now() -> datetime:
    return datetime.now(TZ)


def at(t) -> "datetime.time":
    return t.replace(tzinfo=TZ)


def today_at(t) -> datetime:
    return datetime.combine(local_now().date(), t, tzinfo=TZ)


def week_key(d: date) -> str:
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def next_deadline(start: datetime) -> datetime:
    """Next Sunday at REMIX_CLOSE_TIME strictly after `start`."""
    days = (SUN - start.weekday()) % 7
    deadline = datetime.combine(start.date() + timedelta(days=days), config.REMIX_CLOSE_TIME, tzinfo=TZ)
    return deadline if deadline > start else deadline + timedelta(days=7)


def is_entry(msg: discord.Message) -> bool:
    """A Remix entry: a human post with an attachment, embed or link."""
    return not msg.author.bot and bool(msg.attachments or msg.embeds or URL_RE.search(msg.content or ""))


def role_ping(guild: discord.Guild, role_id: int, enabled: bool, fallback_name: Optional[str] = None) -> Optional[str]:
    """Mention for the ping role: the configured ID, else the self-role with `fallback_name`."""
    if not enabled:
        return None
    role = guild.get_role(role_id) if role_id else None
    if role is None and fallback_name:
        role = discord.utils.get(guild.roles, name=fallback_name)
    return role.mention if role and not role.is_default() else None


class Schedule(commands.Cog):
    """Prompt of the Day, Remix Friday and the weekly digest."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.locks: dict[str, asyncio.Lock] = {}
        self.activity: Counter = Counter()  # (guild_id, channel_id, day) -> messages
        self.remix_threads: set[int] = set()
        self.loops = [
            self.king_loop, self.prompt_loop, self.digest_loop, self.kickoff_loop,
            self.reminder_loop, self.close_loop, self.maintenance_loop,
        ]

    async def cog_load(self):
        rows = await self.db.fetchall("SELECT thread_id FROM challenges WHERE closed = 0 AND thread_id IS NOT NULL")
        self.remix_threads = {r[0] for r in rows}
        if config.GUILD_ID:
            # Job keys used to be global ("2026-10-12"); they're now per server ("<guild>:2026-10-12").
            await self.db.execute(
                "UPDATE job_runs SET run_key = ? || ':' || run_key WHERE run_key NOT LIKE '%:%'", (str(config.GUILD_ID),)
            )
        if not config.SCHEDULE_ENABLED:
            logger.info("SCHEDULE_ENABLED is off; scheduled posts are paused (/schedule-test still works)")

    @commands.Cog.listener()
    async def on_ready(self):
        # Start once connected (on_ready can fire again after reconnects).
        if not config.SCHEDULE_ENABLED or self.maintenance_loop.is_running():
            return
        for loop in self.loops:
            loop.start()
        logger.info("Scheduler started (timezone %s)", config.TIMEZONE)

    async def cog_unload(self):
        for loop in self.loops:
            loop.cancel()
        await self.flush_activity()

    def lock(self, name: str) -> asyncio.Lock:
        return self.locks.setdefault(name, asyncio.Lock())

    def guilds_with(self, key: str) -> list[discord.Guild]:
        """Servers where the channel for `key` is configured and exists."""
        return [g for g in self.bot.guilds if gc.channel(g, key) is not None]

    # ------------------------------------------------------------ helpers
    async def resolve(self, channel_id: Optional[int]):
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return None
        return channel

    async def publish(self, channel, *, thread_name: str, content: Optional[str], embed: discord.Embed,
                      archive_minutes: int = 1440):
        """Post to a text channel (+ start a thread) or create a forum post. Returns (message, thread)."""
        thread_name = trim(thread_name, 100)
        if isinstance(channel, discord.ForumChannel):
            created = await channel.create_thread(
                name=thread_name, content=content, embed=embed, auto_archive_duration=archive_minutes
            )
            return created.message, created.thread
        message = await channel.send(content=content, embed=embed)
        try:
            thread = await message.create_thread(name=thread_name, auto_archive_duration=archive_minutes)
        except discord.HTTPException as e:
            logger.error("Posted in #%s but couldn't create a thread (needs Create Public Threads): %s", channel, e)
            thread = None
        return message, thread

    async def announce(self, channel, thread, **kwargs):
        """Send to the main channel when it's a text channel, otherwise into the thread."""
        target = channel if isinstance(channel, discord.TextChannel) else thread
        if target is not None:
            await target.send(**kwargs)

    async def grant_temp_role(self, member: discord.Member, role_id: int, duration: timedelta, reason: str) -> bool:
        role = member.guild.get_role(role_id) if role_id else None
        if role is None:
            return False
        try:
            await member.add_roles(role, reason=reason)
        except discord.HTTPException as e:
            logger.warning("Could not give %s to %s: %s", role.name, member, e)
            return False
        await self.db.add_temp_role(member.guild.id, member.id, role.id, utc_now() + int(duration.total_seconds()))
        logger.info("Gave %s to %s for %s", role.name, member, duration)
        return True

    # ===================================================== Prompt of the Day
    async def post_prompt(self, guild: discord.Guild, force: bool = False, ping: bool = True) -> str:
        async with self.lock(f"prompt:{guild.id}"):
            key = f"{guild.id}:{local_now().date().isoformat()}"
            if not force and await self.db.job_ran("prompt", key):
                return "Today's prompt was already posted."
            channel = gc.channel(guild, "PROMPT_CHANNEL_ID")
            if channel is None:
                return "No Prompt of the Day channel is configured here (run /setup)."
            if not force:
                await self.db.mark_job("prompt", key)  # before posting: a failure must not retry every 10 min

            item = content.render_item(await content.pick_unused(self.db, content.PROMPTS), guild.name)
            day = local_now().strftime("%b %-d")
            embed = discord.Embed(title="💬 Prompt of the Day", description=item["text"], color=discord.Color.teal())
            embed.add_field(
                name="How it works",
                value="Reply in the thread below! The most-reacted reply by tomorrow's prompt becomes **👑 Prompt King** for 24 hours.",
                inline=False,
            )
            embed.set_footer(text=f"{item['theme'].title()} • {day}")
            message, thread = await self.publish(
                channel, thread_name=f"Prompt of the Day – {day}",
                content=role_ping(guild, gc.get(guild, "PROMPT_PING_ROLE_ID"), ping, config.PING_ROLE_NAMES.get("prompt")), embed=embed,
            )
            await self.db.execute(
                "INSERT INTO prompt_log (text, channel_id, message_id, thread_id, posted_at, guild_id, item_id, theme) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (item["text"], channel.id, message.id, thread.id if thread else None, utc_now(), channel.guild.id, item["id"], item["theme"]),
            )
            await self.db.mark_used(content.PROMPTS, item["id"])
            logger.info("Posted Prompt of the Day #%s in #%s (%s)", item["id"], channel, guild)
            return f"Posted prompt #{item['id']} in {channel.mention}."

    async def judge_prompt(self, guild: discord.Guild, force: bool = False) -> str:
        async with self.lock(f"prompt_king:{guild.id}"):
            today_start = int(datetime.combine(local_now().date(), datetime.min.time(), tzinfo=TZ).timestamp())
            cutoff = utc_now() + 1 if force else today_start  # normally only prompts from previous days
            row = await self.db.fetchone(
                "SELECT * FROM prompt_log WHERE guild_id = ? AND judged = 0 AND posted_at < ? ORDER BY posted_at DESC LIMIT 1",
                (guild.id, cutoff),
            )
            if row is None:
                return "No prompt waiting to be judged."
            # Older unjudged prompts (bot was offline for days) are skipped, not crowned.
            await self.db.execute(
                "UPDATE prompt_log SET judged = 1 WHERE guild_id = ? AND judged = 0 AND posted_at <= ?",
                (guild.id, row["posted_at"]),
            )

            thread = await self.resolve(row["thread_id"])
            channel = await self.resolve(row["channel_id"])
            if thread is None:
                return "The prompt's thread is missing; nobody crowned."

            best, best_score = None, 0
            async for msg in thread.history(limit=1000, oldest_first=True):
                if msg.author.bot or msg.id == row["message_id"]:
                    continue
                score = sum(r.count - (1 if r.me else 0) for r in msg.reactions)
                if score > best_score:  # ties go to the earliest reply
                    best, best_score = msg, score
            if best is None:
                logger.info("Prompt #%s had no reacted replies", row["item_id"])
                return "No replies with reactions; nobody crowned."

            member = thread.guild.get_member(best.author.id)
            got_role = bool(member) and await self.grant_temp_role(
                member, gc.get(guild, "PROMPT_KING_ROLE_ID"), timedelta(hours=24), "Prompt King"
            )
            embed = discord.Embed(
                title="👑 Yesterday's Prompt King",
                description=f"{best.author.mention} won with **{best_score}** reaction(s)!\n> {trim(best.content or '(attachment)', 300)}",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Prompt", value=trim(row["text"], 1024), inline=False)
            embed.add_field(name="Reply", value=f"[Jump to it]({best.jump_url})", inline=False)
            if got_role:
                embed.set_footer(text="Prompt King role active for 24 hours")
            await self.announce(channel, thread, content=best.author.mention, embed=embed)
            logger.info("Prompt King: %s (%d reactions)", best.author, best_score)
            return f"Crowned {best.author} with {best_score} reaction(s)."

    # ========================================================== Remix Friday
    async def kickoff_remix(self, guild: discord.Guild, force: bool = False, ping: bool = True) -> str:
        async with self.lock("remix"):
            key = f"{guild.id}:{week_key(local_now().date())}"
            if not force and await self.db.job_ran("remix_kickoff", key):
                return "This week's Remix Friday already started."
            open_row = await self.db.fetchone("SELECT * FROM challenges WHERE guild_id = ? AND closed = 0 LIMIT 1", (guild.id,))
            if open_row:
                return f"A Remix is still open (\"{open_row['theme']}\"). Close it first with the remix_close job."
            channel = gc.channel(guild, "CHALLENGE_CHANNEL_ID")
            if channel is None:
                return "No Remix Friday channel is configured here (run /setup)."
            if not force:
                await self.db.mark_job("remix_kickoff", key)

            item = content.render_item(await content.pick_unused(self.db, content.REMIX), guild.name)
            opens = local_now()
            closes = next_deadline(opens)
            ts = int(closes.timestamp())
            embed = discord.Embed(
                title=f"🎨 Remix Friday: {item['title']}",
                description=item["description"],
                color=discord.Color.magenta(),
            )
            embed.add_field(
                name="Rules",
                value=(
                    "• Post your entry in this thread: an image, video or link.\n"
                    "• One entry per person. Your first post with an attachment or link counts.\n"
                    "• AI-made, hand-drawn and digital entries are all welcome. Keep it SFW and credit sources.\n"
                    f"• Vote with {FIRE} on your favorites. Self-votes don't count."
                ),
                inline=False,
            )
            embed.add_field(name="Voting closes", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
            embed.set_footer(text="Winner gets 🏆 Remix Champion for 7 days")
            message, thread = await self.publish(
                channel, thread_name=f"Remix Friday – {item['title']}",
                content=role_ping(guild, gc.get(guild, "CHALLENGE_PING_ROLE_ID"), ping, config.PING_ROLE_NAMES.get("challenge")), embed=embed,
                archive_minutes=10080,
            )
            if thread is None:
                # Entries and voting happen in the thread, so without one there's no contest.
                await self.db.mark_used(content.REMIX, item["id"])
                return "Posted the theme but couldn't create the entry thread. Give me Create Public Threads and rerun."
            await self.db.execute(
                "INSERT INTO challenges (guild_id, theme, channel_id, thread_id, message_id, opens_at, closes_at, item_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (channel.guild.id, item["title"], channel.id, thread.id, message.id, int(opens.timestamp()), ts, item["id"]),
            )
            self.remix_threads.add(thread.id)
            await self.db.mark_used(content.REMIX, item["id"])
            logger.info("Remix Friday #%s started in #%s, closes %s", item["id"], channel, closes)
            return f"Started Remix \"{item['title']}\" in {channel.mention}, closing <t:{ts}:F>."

    async def remind_remix(self, force: bool = False, guild: Optional[discord.Guild] = None) -> str:
        async with self.lock("remix"):
            rows = await self.db.fetchall(
                "SELECT * FROM challenges WHERE closed = 0 AND reminded = 0 AND closes_at > ?", (utc_now(),)
            )
            if guild is not None:
                rows = [r for r in rows if r["guild_id"] == guild.id]
            sent = 0
            for row in rows:
                if not force and row["closes_at"] - utc_now() > 3600:
                    continue
                thread = await self.resolve(row["thread_id"])
                if thread is not None:
                    await thread.send(f"⏰ Voting closes <t:{row['closes_at']}:R>! Get your {FIRE} votes in.")
                    sent += 1
                await self.db.execute("UPDATE challenges SET reminded = 1 WHERE id = ?", (row["id"],))
            return f"Sent {sent} reminder(s)." if sent else "No open Remix needs a reminder."

    async def close_remix(self, force: bool = False, guild: Optional[discord.Guild] = None) -> str:
        async with self.lock("remix"):
            sql = "SELECT * FROM challenges WHERE closed = 0" + ("" if force else " AND closes_at <= ?")
            rows = await self.db.fetchall(sql, () if force else (utc_now(),))
            if guild is not None:
                rows = [r for r in rows if r["guild_id"] == guild.id]
            if not rows:
                return "No Remix is due to close."
            results = []
            for row in rows:
                results.append(await self._close_one(row))
            return " ".join(results)

    async def _close_one(self, row) -> str:
        await self.db.execute("UPDATE challenges SET closed = 1 WHERE id = ?", (row["id"],))
        self.remix_threads.discard(row["thread_id"])
        thread = await self.resolve(row["thread_id"])
        channel = await self.resolve(row["channel_id"])
        if thread is None:
            return f"\"{row['theme']}\": thread is gone, no winner."

        closes = datetime.fromtimestamp(row["closes_at"], TZ)
        entrants, best, best_votes = set(), None, 0
        async for msg in thread.history(limit=2000, oldest_first=True, before=closes + timedelta(seconds=1)):
            if not is_entry(msg) or msg.author.id in entrants:
                continue
            entrants.add(msg.author.id)  # first entry per person only
            votes = 0
            for reaction in msg.reactions:
                if str(reaction.emoji) == FIRE:
                    async for user in reaction.users():
                        if not user.bot and user.id != msg.author.id:
                            votes += 1
            if votes > best_votes:  # ties go to the earliest entry
                best, best_votes = msg, votes

        if best is None:
            await thread.send(f"🏁 Voting is closed! No entries got a {FIRE} this time. See you next Remix Friday!")
            result = f"\"{row['theme']}\": closed with {len(entrants)} entries, no votes."
        else:
            await self.db.execute("UPDATE challenges SET winner_id = ? WHERE id = ?", (best.author.id, row["id"]))
            member = thread.guild.get_member(best.author.id)
            got_role = bool(member) and await self.grant_temp_role(
                member, gc.get(thread.guild, "REMIX_CHAMPION_ROLE_ID"), timedelta(days=7), "Remix Champion"
            )
            embed = discord.Embed(
                title=f"🏆 Remix Champion: {row['theme']}",
                description=f"Congrats {best.author.mention}! Your entry got **{best_votes}** {FIRE} out of {len(entrants)} entries.",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Winning entry", value=f"[Jump to it]({best.jump_url})", inline=False)
            if best.attachments and best.attachments[0].content_type and best.attachments[0].content_type.startswith("image/"):
                embed.set_image(url=best.attachments[0].url)
            if got_role:
                embed.set_footer(text="Remix Champion role active for 7 days")
            await thread.send(content=best.author.mention, embed=embed)
            if isinstance(channel, discord.TextChannel):
                await channel.send(embed=embed)
            result = f"\"{row['theme']}\": {best.author} won with {best_votes} votes."
        try:
            await thread.edit(locked=True)
        except discord.HTTPException as e:
            logger.warning("Could not lock Remix thread %s: %s", thread.id, e)
        logger.info("Remix closed: %s", result)
        return result

    # ============================================================ Digest
    async def fetch_feed(self, session: aiohttp.ClientSession, url: str) -> tuple[str, list[tuple[str, str]]]:
        """Return (feed title, [(headline, link), ...] newest first). Raises on failure."""
        async with session.get(url, headers=FEED_HEADERS) as resp:
            resp.raise_for_status()
            data = bytearray()
            async for chunk in resp.content.iter_chunked(65536):  # read fully, capped at 5 MB
                data += chunk
                if len(data) > 5_000_000:
                    raise ValueError("feed larger than 5 MB")
            data = bytes(data)
        parsed = await asyncio.to_thread(feedparser.parse, data)
        title = trim(html.unescape(parsed.feed.get("title") or url.split("/")[2]), 60)

        def stamp(e):
            t = e.get("published_parsed") or e.get("updated_parsed")
            return mktime(t) if t else 0

        entries = sorted(parsed.entries, key=stamp, reverse=True)
        items = []
        for e in entries:
            headline = re.sub(r"\s+", " ", html.unescape(e.get("title") or "")).strip()
            link = e.get("link") or ""
            if headline and link.startswith("http"):
                items.append((headline, link))
            if len(items) >= config.DIGEST_HEADLINES_PER_FEED:
                break
        return title, items

    @staticmethod
    def format_headlines(items: list[tuple[str, str]]) -> str:
        for count in (len(items), 2, 1):  # 3 headlines, falling back to 2 if the field is too long
            lines = [f"• [{trim(h, 90).replace('[', '(').replace(']', ')')}]({link})" for h, link in items[:count]]
            value = "\n".join(lines)
            if len(value) <= 1024:
                return value
        return trim(value, 1024)

    async def build_digest_sections(self) -> tuple[list[discord.Embed], int]:
        """Fetch every feed once. Returns (one embed per category, number of failed feeds)."""
        feeds = content.load_feeds()
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            tasks_ = {
                (cat, url): asyncio.create_task(self.fetch_feed(session, url))
                for cat, urls in feeds.items() for url in urls
            }
            await asyncio.gather(*tasks_.values(), return_exceptions=True)

        embeds, failed = [], 0
        for cat, urls in feeds.items():
            embed = discord.Embed(title=CATEGORY_TITLES.get(cat, cat.title()), color=discord.Color.blurple())
            for url in urls:
                task = tasks_[(cat, url)]
                if task.exception() or not task.result()[1]:
                    failed += 1
                    logger.warning("Digest feed failed: %s (%s)", url, task.exception() or "no items")
                    continue
                title, items = task.result()
                embed.add_field(name=title, value=self.format_headlines(items), inline=False)
            if embed.fields:
                embeds.append(embed)
        return embeds, failed

    async def post_digest(self, guild: discord.Guild, force: bool = False, sections=None) -> str:
        """Post the digest in one server. Pass `sections` to reuse feeds already fetched for other servers."""
        async with self.lock(f"digest:{guild.id}"):
            key = f"{guild.id}:{week_key(local_now().date())}"
            if not force and await self.db.job_ran("digest", key):
                return "This week's digest was already posted."
            channel = gc.channel(guild, "DIGEST_CHANNEL_ID")
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                return "No digest text channel is configured here (run /setup)."
            if not force:
                await self.db.mark_job("digest", key)

            embeds, failed = sections if sections is not None else await self.build_digest_sections()
            summary = discord.Embed(
                title=f"📰 Weekly Digest: week of {local_now().strftime('%B %-d, %Y')}",
                description="The newest headlines from around gaming, AI and tech. Have a great week!",
                color=discord.Color.dark_teal(),
            )
            summary.add_field(name="🏆 Last week's Remix winner", value=await self.last_remix_winner(guild), inline=False)
            summary.add_field(name="💬 Most active channel", value=await self.most_active_channel(guild), inline=False)
            if failed:
                summary.set_footer(text=f"{failed} feed(s) couldn't be loaded this week")

            await channel.send(embed=summary)
            recap = await self.weekly_recap(guild)
            if recap is not None:
                await channel.send(embed=recap, allowed_mentions=discord.AllowedMentions.none())
            for embed in embeds:  # one message per category keeps under Discord's 6000-char limit
                await channel.send(embed=embed)
            logger.info("Posted weekly digest in %s (%d sections, %d feeds failed)", guild, len(embeds), failed)
            return f"Posted the digest in {channel.mention} ({failed} feed(s) failed)."

    async def weekly_recap(self, guild: discord.Guild) -> Optional[discord.Embed]:
        """AI-written recap of the week's top-scored news (one AI call per week, shared by every server)."""
        from cogs.news import build_recap  # local import: news is optional

        week = week_key(local_now().date())
        cache = getattr(self, "_recap_cache", None)
        if cache is None or cache[0] != week:
            try:
                embed = await build_recap(self.db, utc_now() - 7 * 86400, "week", guild.id)
            except Exception:
                logger.exception("Could not build the weekly AI recap")
                embed = None
            if embed is None:
                return None  # don't cache a miss: the next server (or next week's run) may succeed
            self._recap_cache = cache = (week, embed)
        return cache[1]

    async def last_remix_winner(self, guild: discord.Guild) -> str:
        row = await self.db.fetchone(
            "SELECT * FROM challenges WHERE guild_id = ? AND closed = 1 AND closes_at >= ? "
            "ORDER BY closes_at DESC LIMIT 1",
            (guild.id, utc_now() - 8 * 86400),
        )
        if row is None:
            return "No Remix Friday last week."
        if not row["winner_id"]:
            return f"**{row['theme']}**: no votes this time."
        return f"<@{row['winner_id']}> with **{row['theme']}**"

    async def most_active_channel(self, guild: discord.Guild) -> str:
        await self.flush_activity()
        since = (local_now().date() - timedelta(days=7)).isoformat()
        rows = await self.db.fetchall(
            "SELECT channel_id, SUM(count) AS n FROM channel_activity WHERE guild_id = ? AND day >= ? "
            "GROUP BY channel_id ORDER BY n DESC LIMIT 10",
            (guild.id, since),
        )
        for row in rows:
            ch = guild.get_channel(row["channel_id"])
            # Only name channels everyone can see.
            if ch is not None and ch.permissions_for(guild.default_role).view_channel:
                return f"{ch.mention} with **{row['n']:,}** messages"
        return "Not enough data yet."

    # ======================================================= Activity + roles
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        channel = message.channel
        parent_id = channel.parent_id if isinstance(channel, discord.Thread) else channel.id
        self.activity[(message.guild.id, parent_id, local_now().date().isoformat())] += 1
        if channel.id in self.remix_threads and is_entry(message):
            try:
                await message.add_reaction(FIRE)  # makes voting one tap
            except discord.HTTPException:
                pass

    async def flush_activity(self) -> None:
        if not self.activity:
            return
        batch, self.activity = self.activity, Counter()
        await self.db.conn.executemany(
            "INSERT INTO channel_activity (guild_id, channel_id, day, count) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (guild_id, channel_id, day) DO UPDATE SET count = count + excluded.count",
            [(g, c, d, n) for (g, c, d), n in batch.items()],
        )
        cutoff = (local_now().date() - timedelta(days=30)).isoformat()
        await self.db.conn.execute("DELETE FROM channel_activity WHERE day < ?", (cutoff,))
        await self.db.conn.commit()

    async def remove_expired_roles(self) -> int:
        removed = 0
        for row in await self.db.due_temp_roles():
            guild = self.bot.get_guild(row["guild_id"])
            member = guild.get_member(row["user_id"]) if guild else None
            role = guild.get_role(row["role_id"]) if guild else None
            if member and role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Temporary role expired")
                    removed += 1
                except discord.Forbidden:
                    logger.warning("No permission to remove %s from %s", role, member)
                except discord.HTTPException as e:
                    logger.warning("Could not remove %s from %s, will retry: %s", role, member, e)
                    continue
            await self.db.delete_temp_role(row["guild_id"], row["user_id"], row["role_id"])
        if removed:
            logger.info("Removed %d expired temporary role(s)", removed)
        return removed

    async def catch_up(self) -> None:
        """Run jobs that were missed because the bot was offline at their time, in every server."""
        now = local_now()

        def due(t, weekday=None) -> bool:
            start = today_at(t)
            return (weekday is None or now.weekday() == weekday) and start <= now < start + CATCH_UP_WINDOW

        for guild in self.bot.guilds:
            try:
                if due(config.PROMPT_KING_TIME):
                    await self.judge_prompt(guild)  # no-op unless yesterday's prompt is unjudged
                if due(config.PROMPT_TIME) and gc.channel(guild, "PROMPT_CHANNEL_ID"):
                    await self.post_prompt(guild)
                if due(config.DIGEST_TIME, MON) and gc.channel(guild, "DIGEST_CHANNEL_ID"):
                    await self.post_digest(guild)
                if due(config.REMIX_KICKOFF_TIME, FRI) and gc.channel(guild, "CHALLENGE_CHANNEL_ID"):
                    await self.kickoff_remix(guild)
            except Exception:
                logger.exception("Catch-up failed in %s", guild)
        await self.remind_remix()  # anything closing within the hour, any server
        await self.close_remix()   # anything past its deadline, any server

    # ================================================================ loops
    async def _run(self, name: str, job, guilds: Optional[list] = None, weekday: Optional[int] = None):
        """Run `job(guild)` for each server (or `job()` once when guilds is None). One failure never stops the rest."""
        if weekday is not None and local_now().weekday() != weekday:
            return
        for guild in (guilds if guilds is not None else [None]):
            try:
                result = await (job(guild) if guild is not None else job())
                logger.info("Job %s%s: %s", name, f" [{guild}]" if guild else "", result)
            except Exception:
                logger.exception("Scheduled job %s failed%s", name, f" in {guild}" if guild else "")

    @tasks.loop(time=at(config.PROMPT_KING_TIME))
    async def king_loop(self):
        await self._run("prompt_king", self.judge_prompt, guilds=list(self.bot.guilds))

    @tasks.loop(time=at(config.PROMPT_TIME))
    async def prompt_loop(self):
        await self._run("prompt", self.post_prompt, guilds=self.guilds_with("PROMPT_CHANNEL_ID"))

    @tasks.loop(time=at(config.DIGEST_TIME))
    async def digest_loop(self):
        if local_now().weekday() != MON:
            return
        guilds = self.guilds_with("DIGEST_CHANNEL_ID")
        if not guilds:
            return
        try:
            sections = await self.build_digest_sections()  # fetch feeds once for all servers
        except Exception:
            logger.exception("Could not build the digest")
            return
        await self._run("digest", lambda g: self.post_digest(g, sections=sections), guilds=guilds)

    @tasks.loop(time=at(config.REMIX_KICKOFF_TIME))
    async def kickoff_loop(self):
        await self._run("remix_kickoff", self.kickoff_remix, guilds=self.guilds_with("CHALLENGE_CHANNEL_ID"), weekday=FRI)

    @tasks.loop(time=at(config.REMIX_REMINDER_TIME))
    async def reminder_loop(self):
        await self._run("remix_reminder", self.remind_remix, weekday=SUN)

    @tasks.loop(time=at(config.REMIX_CLOSE_TIME))
    async def close_loop(self):
        await self._run("remix_close", self.close_remix, weekday=SUN)

    @tasks.loop(minutes=10)
    async def maintenance_loop(self):
        for name, job in (("flush_activity", self.flush_activity), ("cleanup", self.remove_expired_roles),
                          ("catch_up", self.catch_up)):
            try:
                await job()
            except Exception:
                logger.exception("Maintenance step %s failed", name)

    @king_loop.before_loop
    @prompt_loop.before_loop
    @digest_loop.before_loop
    @kickoff_loop.before_loop
    @reminder_loop.before_loop
    @close_loop.before_loop
    @maintenance_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ============================================================== command
    @commands.hybrid_command(name="schedule-test", description="Run a scheduled job right now (admin)")
    @app_commands.describe(job="Which job to run", ping="Ping the role like a real post? (default: no)")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def schedule_test(self, ctx: commands.Context, job: JobName, ping: bool = False):
        await ctx.defer(ephemeral=True)
        runners = {
            "prompt": lambda: self.post_prompt(ctx.guild, force=True, ping=ping),
            "prompt_king": lambda: self.judge_prompt(ctx.guild, force=True),
            "digest": lambda: self.post_digest(ctx.guild, force=True),
            "remix_kickoff": lambda: self.kickoff_remix(ctx.guild, force=True, ping=ping),
            "remix_reminder": lambda: self.remind_remix(force=True, guild=ctx.guild),
            "remix_close": lambda: self.close_remix(force=True, guild=ctx.guild),
            "cleanup": lambda: self._cleanup_report(),
        }
        logger.info("%s ran /schedule-test %s", ctx.author, job)
        try:
            result = await runners[job]()
        except discord.Forbidden:
            result = "I'm missing permissions in that channel (Send Messages, Create Public Threads, Embed Links, Manage Roles)."
        except Exception as e:
            logger.exception("schedule-test %s failed", job)
            result = f"Failed: {type(e).__name__}: {e}"
        await ctx.send(f"🧪 **{job}**: {result}\n-# Test runs don't replace the scheduled post.", ephemeral=True)

    async def _cleanup_report(self) -> str:
        await self.flush_activity()
        return f"Removed {await self.remove_expired_roles()} expired role(s)."


async def setup(bot: commands.Bot):
    await bot.add_cog(Schedule(bot))
