"""Event announcements with Going / Maybe RSVP buttons.

RSVPs live in the database, so buttons keep working after restarts. A loop
pings everyone marked Going EVENT_REMINDER_MINUTES before the start, then
closes RSVPs once the event begins.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import guildconfig as gc
from utils.db import now
from utils.helpers import is_staff, trim
from utils.timeparse import TZ, parse_when

logger = logging.getLogger("bot.events")

STATUSES = {"going": ("✅", "Going"), "maybe": ("❔", "Maybe")}
REFRESH_DELAY = 3  # seconds; batches embed edits when lots of people RSVP at once
MAX_NAMES = 20
TIME_HELP = "Try `sat 8pm`, `tomorrow 7:30pm`, `10/31 8pm`, `2026-10-31 20:00` or `in 2h` (Eastern time)."


def can_host_events():
    async def predicate(ctx: commands.Context) -> bool:
        if is_staff(ctx.author) or ctx.author.guild_permissions.manage_events:
            return True
        raise commands.CheckFailure("You need the staff role or **Manage Events** to announce events.")

    return commands.check(predicate)


class RSVPView(discord.ui.View):
    """Persistent: one registered instance handles every event's buttons."""

    def __init__(self, cog: "Events", going: int = 0, maybe: int = 0, closed: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        for status, count in (("going", going), ("maybe", maybe)):
            emoji, label = STATUSES[status]
            button = discord.ui.Button(
                label=f"{label} ({count})",
                emoji=emoji,
                style=discord.ButtonStyle.green if status == "going" else discord.ButtonStyle.secondary,
                custom_id=f"event:{status}",
                disabled=closed,
            )
            button.callback = self._make_callback(status)
            self.add_item(button)

    def _make_callback(self, status: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.handle_rsvp(interaction, status)

        return callback


class Events(commands.Cog):
    """Event announcements with RSVPs and reminders."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._refreshing: dict[int, asyncio.Task] = {}
        bot.add_view(RSVPView(self))

    async def cog_unload(self):
        self.event_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.event_loop.is_running():
            self.event_loop.start()

    # ------------------------------------------------------------ data
    async def get(self, message_id: int):
        return await self.db.fetchone("SELECT * FROM events WHERE message_id = ?", (message_id,))

    async def rsvps(self, message_id: int) -> dict[str, list[int]]:
        rows = await self.db.fetchall(
            "SELECT user_id, status FROM event_rsvps WHERE message_id = ? ORDER BY updated_at, rowid", (message_id,)
        )
        out = {"going": [], "maybe": []}
        for r in rows:
            out[r["status"]].append(r["user_id"])
        return out

    @staticmethod
    def name_list(user_ids: list[int]) -> str:
        if not user_ids:
            return "Nobody yet"
        shown = " ".join(f"<@{u}>" for u in user_ids[:MAX_NAMES])
        extra = len(user_ids) - MAX_NAMES
        return trim(shown + (f" +{extra} more" if extra > 0 else ""), 1024)

    def build_embed(self, row, rsvps: dict[str, list[int]]) -> discord.Embed:
        closed = bool(row["closed"])
        ts = row["starts_at"]
        embed = discord.Embed(
            title=f"📅 {row['title']}" + (" (started)" if closed else ""),
            description=row["description"],
            color=discord.Color.dark_grey() if closed else discord.Color.gold(),
        )
        embed.set_author(name="Event Announcement")
        embed.add_field(name="🕒 When", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
        embed.add_field(name="Host", value=f"<@{row['host_id']}>", inline=False)
        for status, (emoji, label) in STATUSES.items():
            people = rsvps[status]
            embed.add_field(name=f"{emoji} {label} ({len(people)})", value=self.name_list(people), inline=False)
        if closed:
            embed.set_footer(text="RSVPs are closed. Have fun!")
        else:
            embed.set_footer(
                text=f"RSVP below. Going? You'll get a ping {config.EVENT_REMINDER_MINUTES} min before it starts."
            )
        return embed

    async def refresh(self, message_id: int) -> None:
        row = await self.get(message_id)
        channel = self.bot.get_channel(row["channel_id"]) if row else None
        if channel is None:
            return
        rsvps = await self.rsvps(message_id)
        view = RSVPView(self, len(rsvps["going"]), len(rsvps["maybe"]), closed=bool(row["closed"]))
        try:
            await channel.get_partial_message(message_id).edit(embed=self.build_embed(row, rsvps), view=view)
        except discord.NotFound:
            logger.warning("Event message %s was deleted", message_id)
        except discord.HTTPException as e:
            logger.warning("Could not update event %s: %s", message_id, e)

    def schedule_refresh(self, message_id: int) -> None:
        if message_id in self._refreshing:
            return

        async def later():
            try:
                await asyncio.sleep(REFRESH_DELAY)
            finally:
                self._refreshing.pop(message_id, None)
            await self.refresh(message_id)

        self._refreshing[message_id] = asyncio.create_task(later())

    # ------------------------------------------------------------ buttons
    async def handle_rsvp(self, interaction: discord.Interaction, status: str):
        message_id = interaction.message.id
        row = await self.get(message_id)
        if row is None:
            await interaction.response.send_message("This event isn't in my records anymore.", ephemeral=True)
            return
        if row["closed"] or row["starts_at"] <= now():
            await interaction.response.send_message("This event has already started, so RSVPs are closed.", ephemeral=True)
            return
        user_id = interaction.user.id
        current = await self.db.fetchone(
            "SELECT status FROM event_rsvps WHERE message_id = ? AND user_id = ?", (message_id, user_id)
        )
        emoji, label = STATUSES[status]
        if current and current["status"] == status:
            await self.db.execute("DELETE FROM event_rsvps WHERE message_id = ? AND user_id = ?", (message_id, user_id))
            msg = f"Removed your RSVP for **{row['title']}**."
        else:
            await self.db.execute(
                "INSERT INTO event_rsvps (message_id, user_id, status, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (message_id, user_id) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                (message_id, user_id, status, now()),
            )
            msg = f"{emoji} You're marked **{label}** for **{row['title']}**."
            if status == "going":
                msg += f" I'll ping you {config.EVENT_REMINDER_MINUTES} minutes before it starts."
            msg += " Click again to remove your RSVP."
        await interaction.response.send_message(msg, ephemeral=True)
        self.schedule_refresh(message_id)

    # ------------------------------------------------------------ loop
    async def send_reminder(self, row) -> int:
        """Ping everyone Going. Returns how many people were pinged."""
        channel = self.bot.get_channel(row["channel_id"])
        going = (await self.rsvps(row["message_id"]))["going"]
        if channel is None or not going:
            return 0
        header = f"⏰ **{row['title']}** starts <t:{row['starts_at']}:R>! See you there:"
        reference = channel.get_partial_message(row["message_id"])
        mentions = [f"<@{u}>" for u in going]
        chunk, first = header, True
        for mention in mentions:  # split so each message stays under Discord's 2000-char limit
            if len(chunk) + len(mention) + 1 > 1900:
                await channel.send(chunk, reference=reference if first else None, mention_author=False)
                chunk, first = "", False
            chunk += " " + mention
        await channel.send(chunk.strip(), reference=reference if first else None, mention_author=False)
        return len(going)

    @tasks.loop(seconds=60)
    async def event_loop(self):
        try:
            t = now()
            due = await self.db.fetchall(
                "SELECT * FROM events WHERE reminded = 0 AND closed = 0 AND starts_at - ? <= ?",
                (config.EVENT_REMINDER_MINUTES * 60, t),
            )
            for row in due:
                # Mark first so a failure can't cause repeated pings.
                await self.db.execute("UPDATE events SET reminded = 1 WHERE message_id = ?", (row["message_id"],))
                if row["starts_at"] > t - 300:  # skip reminders for events that started over 5 min ago
                    pinged = await self.send_reminder(row)
                    logger.info("Event reminder for %s: pinged %d", row["title"], pinged)
            started = await self.db.fetchall("SELECT message_id FROM events WHERE closed = 0 AND starts_at <= ?", (t,))
            for row in started:
                await self.db.execute("UPDATE events SET closed = 1 WHERE message_id = ?", (row["message_id"],))
                await self.refresh(row["message_id"])
        except Exception:
            logger.exception("Event loop failed")

    @event_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ command
    @commands.hybrid_command(name="event", description="Announce a server event with RSVP buttons")
    @app_commands.describe(
        title="Event title",
        time="When? e.g. sat 8pm, tomorrow 7:30pm, 10/31 8pm, in 2h (Eastern time)",
        description="What's the event about?",
        ping_role="Optional role to ping",
    )
    @can_host_events()
    async def event(
        self,
        ctx: commands.Context,
        title: str,
        time: str,
        description: str,
        ping_role: Optional[discord.Role] = None,
    ):
        """Prefix usage: !event "Among Us Night" "sat 8pm" "Bring snacks" @Events"""
        when = parse_when(time)
        if when is None:
            await ctx.send(f"❓ I couldn't understand `{trim(time, 50)}`. {TIME_HELP}", ephemeral=True)
            return
        current = datetime.now(TZ)
        if when <= current + timedelta(minutes=1):
            await ctx.send(f"❓ That time is in the past ({when:%a %b %-d, %-I:%M %p %Z}). {TIME_HELP}", ephemeral=True)
            return
        if when > current + timedelta(days=366):
            await ctx.send("❓ Events can be scheduled up to a year ahead.", ephemeral=True)
            return
        if ping_role is not None and ping_role.is_default():
            await ctx.send("Pinging @everyone isn't allowed here. Use a role instead.", ephemeral=True)
            return

        channel = gc.channel(ctx.guild, "EVENT_CHANNEL_ID")
        if not isinstance(channel, discord.TextChannel):
            channel = ctx.channel

        starts_at = int(when.timestamp())
        # Too close for a reminder? Mark it done so nobody gets an instant ping.
        reminded = int(starts_at - now() <= config.EVENT_REMINDER_MINUTES * 60)
        draft = {
            "title": trim(title, 200), "description": trim(description, 3000), "starts_at": starts_at,
            "host_id": ctx.author.id, "closed": 0,
        }
        empty = {"going": [], "maybe": []}
        try:
            message = await channel.send(
                content=ping_role.mention if ping_role else None,
                embed=self.build_embed(draft, empty),
                view=RSVPView(self),
            )
        except discord.Forbidden:
            await ctx.send("⚠️ I can't post in the events channel. Check my permissions.", ephemeral=True)
            return
        await self.db.execute(
            "INSERT INTO events (message_id, guild_id, channel_id, title, description, starts_at, host_id, reminded, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message.id, ctx.guild.id, channel.id, draft["title"], draft["description"], starts_at, ctx.author.id,
             reminded, now()),
        )
        logger.info("Event '%s' at %s announced by %s in #%s", title, when.isoformat(), ctx.author, channel)
        if channel != ctx.channel or ctx.interaction:
            await ctx.send(f"✅ Event announced in {channel.mention} for <t:{starts_at}:F>!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
