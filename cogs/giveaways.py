"""Giveaways with a persistent Enter button.

Everything lives in the database (giveaways + giveaway_entries), so buttons,
entries and timers all survive restarts. A loop every 15 seconds ends any
giveaway whose time is up, including ones that ended while the bot was offline.
"""

import asyncio
import logging
import random
import re
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.db import now
from utils.helpers import human, is_staff, parse_duration, staff_only, trim

logger = logging.getLogger("bot.giveaways")

MIN_DURATION = timedelta(minutes=1)
MAX_DURATION = timedelta(days=30)
MAX_WINNERS = 20
REFRESH_DELAY = 3  # seconds; batches entry-count edits so a rush of clicks doesn't hit rate limits
ENTER_ID = "giveaway:enter"
_rng = random.SystemRandom()


def parse_message_id(text: str) -> Optional[int]:
    """Accept a message ID or a message link."""
    m = re.search(r"(\d{15,25})\s*/?\s*$", text.strip())
    return int(m.group(1)) if m else None


def winner_list(row) -> list[int]:
    return [int(x) for x in (row["winner_ids"] or "").split(",") if x]


class EnterView(discord.ui.View):
    """Persistent: one registered instance handles every giveaway's button."""

    def __init__(self, cog: "Giveaways", entries: int = 0, ended: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(
            label="Giveaway ended" if ended else f"Enter ({entries})",
            emoji="🎉",
            style=discord.ButtonStyle.secondary if ended else discord.ButtonStyle.green,
            custom_id=ENTER_ID,
            disabled=ended,
        )
        button.callback = self.enter
        self.add_item(button)

    async def enter(self, interaction: discord.Interaction):
        await self.cog.handle_enter(interaction)


class LeaveView(discord.ui.View):
    def __init__(self, cog: "Giveaways", message_id: int):
        super().__init__(timeout=120)
        self.cog, self.message_id = cog, message_id

    @discord.ui.button(label="Leave giveaway", style=discord.ButtonStyle.red)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        row = await self.cog.get(self.message_id)
        if row is None or row["ended"]:
            await interaction.response.edit_message(content="This giveaway has already ended.", view=None)
            return
        await self.cog.bot.db.execute(
            "DELETE FROM giveaway_entries WHERE message_id = ? AND user_id = ?", (self.message_id, interaction.user.id)
        )
        await interaction.response.edit_message(content="👋 You've left the giveaway.", view=None)
        self.cog.schedule_refresh(self.message_id)


class Giveaways(commands.Cog):
    """Button giveaways with random winners."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.locks: dict[int, asyncio.Lock] = {}
        self._refreshing: dict[int, asyncio.Task] = {}
        bot.add_view(EnterView(self))

    async def cog_unload(self):
        self.end_loop.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.end_loop.is_running():
            self.end_loop.start()

    # ------------------------------------------------------------- data
    async def get(self, message_id: int):
        return await self.db.fetchone("SELECT * FROM giveaways WHERE message_id = ?", (message_id,))

    async def entry_count(self, message_id: int) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) FROM giveaway_entries WHERE message_id = ?", (message_id,))
        return row[0]

    def build_embed(self, row, entries: int) -> discord.Embed:
        ended = bool(row["ended"])
        embed = discord.Embed(
            title="🎉 GIVEAWAY ENDED" if ended else "🎉 GIVEAWAY",
            description=f"## {row['prize']}",
            color=discord.Color.dark_grey() if ended else discord.Color.fuchsia(),
        )
        ts = row["ends_at"]
        embed.add_field(name="Ended" if ended else "Ends", value=f"<t:{ts}:R> (<t:{ts}:f>)", inline=True)
        embed.add_field(name="Winners", value=str(row["winners"]), inline=True)
        embed.add_field(name="Entries", value=f"{entries:,}", inline=True)
        if row["host_id"]:
            embed.add_field(name="Hosted by", value=f"<@{row['host_id']}>", inline=True)
        if row["required_role_id"]:
            embed.add_field(name="Requirement", value=f"<@&{row['required_role_id']}>", inline=True)
        if ended:
            winners = winner_list(row)
            embed.add_field(
                name="🏆 Winner(s)",
                value=trim(" ".join(f"<@{w}>" for w in winners), 1024) if winners else "No valid entries.",
                inline=False,
            )
        else:
            embed.set_footer(text="Click the button to enter. One entry per person.")
        return embed

    async def refresh(self, message_id: int) -> None:
        """Re-render the giveaway message (entry count, ended state)."""
        row = await self.get(message_id)
        if row is None:
            return
        channel = self.bot.get_channel(row["channel_id"])
        if channel is None:
            return
        entries = await self.entry_count(message_id)
        try:
            await channel.get_partial_message(message_id).edit(
                embed=self.build_embed(row, entries), view=EnterView(self, entries, ended=bool(row["ended"]))
            )
        except discord.NotFound:
            logger.warning("Giveaway message %s was deleted", message_id)
        except discord.HTTPException as e:
            logger.warning("Could not update giveaway %s: %s", message_id, e)

    def schedule_refresh(self, message_id: int) -> None:
        if message_id in self._refreshing:
            return  # an update is already queued and will show the latest count

        async def later():
            try:
                await asyncio.sleep(REFRESH_DELAY)
            finally:
                self._refreshing.pop(message_id, None)
            await self.refresh(message_id)

        self._refreshing[message_id] = asyncio.create_task(later())

    # ------------------------------------------------------------ button
    async def handle_enter(self, interaction: discord.Interaction):
        message_id = interaction.message.id
        row = await self.get(message_id)
        if row is None:
            await interaction.response.send_message("This giveaway isn't in my records anymore.", ephemeral=True)
            return
        if row["ended"] or row["ends_at"] <= now():
            await interaction.response.send_message("This giveaway has ended. Better luck next time!", ephemeral=True)
            return
        member = interaction.user
        if row["required_role_id"] and not (isinstance(member, discord.Member) and member.get_role(row["required_role_id"])):
            await interaction.response.send_message(
                f"You need the <@&{row['required_role_id']}> role to enter this giveaway.", ephemeral=True
            )
            return

        added = await self.db.execute_count(
            "INSERT OR IGNORE INTO giveaway_entries (message_id, user_id, entered_at) VALUES (?, ?, ?)",
            (message_id, member.id, now()),
        )
        if not added:
            await interaction.response.send_message(
                "You're already entered! 🎉", view=LeaveView(self, message_id), ephemeral=True
            )
            return
        await interaction.response.send_message(f"🎉 You're in for **{row['prize']}**. Good luck!", ephemeral=True)
        self.schedule_refresh(message_id)

    # ------------------------------------------------------------ ending
    def lock(self, message_id: int) -> asyncio.Lock:
        return self.locks.setdefault(message_id, asyncio.Lock())

    async def draw(self, row, count: int, exclude: set[int]) -> list[int]:
        """Pick up to `count` random winners from entrants who still qualify."""
        rows = await self.db.fetchall("SELECT user_id FROM giveaway_entries WHERE message_id = ?", (row["message_id"],))
        guild = self.bot.get_guild(row["guild_id"])
        pool = []
        for r in rows:
            uid = r[0]
            if uid in exclude:
                continue
            member = guild.get_member(uid) if guild else None
            if member is None or member.bot:
                continue  # left the server
            if row["required_role_id"] and not member.get_role(row["required_role_id"]):
                continue
            pool.append(uid)
        return _rng.sample(pool, min(count, len(pool)))

    async def announce(self, row, winners: list[int], reroll: bool = False) -> None:
        channel = self.bot.get_channel(row["channel_id"])
        if channel is None:
            return
        link = f"https://discord.com/channels/{row['guild_id']}/{row['channel_id']}/{row['message_id']}"
        if winners:
            mentions = " ".join(f"<@{w}>" for w in winners)
            verb = "New winner" if reroll else "Congratulations"
            text = f"🎉 {verb} {mentions}! You won **{row['prize']}**!"
            if row["host_id"]:
                text += f" Contact <@{row['host_id']}> to claim it."
        else:
            text = f"😢 No valid entries for **{row['prize']}**, so there's no winner."
        reference = channel.get_partial_message(row["message_id"])
        try:
            await channel.send(text, reference=reference, mention_author=False)
        except discord.HTTPException:
            await channel.send(f"{text}\n{link}")

        guild = self.bot.get_guild(row["guild_id"])
        for uid in winners:
            member = guild.get_member(uid) if guild else None
            if member is None:
                continue
            try:
                await member.send(
                    f"🎉 You won **{row['prize']}** in **{guild.name}**! "
                    + (f"Message <@{row['host_id']}> to claim it. " if row["host_id"] else "")
                    + link
                )
            except discord.HTTPException:
                logger.info("Couldn't DM giveaway winner %s (DMs closed)", member)

    async def end_giveaway(self, message_id: int) -> str:
        async with self.lock(message_id):
            row = await self.get(message_id)
            if row is None:
                return "No giveaway with that message ID."
            if row["ended"]:
                return "That giveaway already ended."
            winners = await self.draw(row, row["winners"], exclude=set())
            ended_at = min(row["ends_at"], now())  # if ended early, show the real end time
            await self.db.execute(
                "UPDATE giveaways SET ended = 1, ends_at = ?, winner_ids = ? WHERE message_id = ?",
                (ended_at, ",".join(map(str, winners)), message_id),
            )
            row = await self.get(message_id)
            await self.refresh(message_id)
            await self.announce(row, winners)
            logger.info("Giveaway %s (%s) ended; winners: %s", message_id, row["prize"], winners)
            return f"Ended **{row['prize']}** with {len(winners)} winner(s)."

    async def reroll_giveaway(self, message_id: int, count: int) -> str:
        async with self.lock(message_id):
            row = await self.get(message_id)
            if row is None:
                return "No giveaway with that message ID."
            if not row["ended"]:
                return "That giveaway is still running. End it first."
            previous = winner_list(row)
            winners = await self.draw(row, count, exclude=set(previous))
            if not winners:
                return "Nobody else is eligible to win."
            await self.db.execute(
                "UPDATE giveaways SET winner_ids = ? WHERE message_id = ?",
                (",".join(map(str, previous + winners)), message_id),
            )
            row = await self.get(message_id)
            await self.refresh(message_id)
            await self.announce(row, winners, reroll=True)
            logger.info("Giveaway %s rerolled; new winners: %s", message_id, winners)
            return f"Rerolled **{row['prize']}**: {' '.join(f'<@{w}>' for w in winners)}"

    @tasks.loop(seconds=15)
    async def end_loop(self):
        try:
            due = await self.db.fetchall("SELECT message_id FROM giveaways WHERE ended = 0 AND ends_at <= ?", (now(),))
            for r in due:
                await self.end_giveaway(r[0])
        except Exception:
            logger.exception("Giveaway end loop failed")

    @end_loop.before_loop
    async def _wait_ready(self):
        await self.bot.wait_until_ready()

    # ----------------------------------------------------------- commands
    @commands.hybrid_group(name="giveaway", description="Run giveaways (staff)")
    @staff_only()
    async def giveaway(self, ctx: commands.Context):
        await ctx.send("Use `/giveaway start`, `/giveaway end` or `/giveaway reroll`.", ephemeral=True)

    @giveaway.command(name="start", description="Start a giveaway in this channel")
    @app_commands.describe(
        prize="What are you giving away?",
        duration="How long it runs, e.g. 30m, 2h, 1d, 1d12h (max 30d)",
        winners="Number of winners (1-20)",
        required_role="Only members with this role can enter",
    )
    @staff_only()
    @commands.bot_has_permissions(send_messages=True, embed_links=True)
    async def giveaway_start(
        self,
        ctx: commands.Context,
        prize: str,
        duration: str,
        winners: commands.Range[int, 1, MAX_WINNERS] = 1,
        required_role: Optional[discord.Role] = None,
    ):
        """Prefix usage: !giveaway start "Steam gift card" 2d 1 @Members"""
        delta = parse_duration(duration)
        if delta is None or not (MIN_DURATION <= delta <= MAX_DURATION):
            await ctx.send("❓ Duration must be between 1 minute and 30 days, e.g. `30m`, `2h`, `1d`.", ephemeral=True)
            return
        if required_role is not None and required_role.is_default():
            required_role = None
        prize = trim(prize, 200)
        ends_at = now() + int(delta.total_seconds())
        draft = {
            "message_id": 0, "prize": prize, "winners": winners, "ends_at": ends_at, "ended": 0,
            "host_id": ctx.author.id, "required_role_id": required_role.id if required_role else None, "winner_ids": "",
        }
        message = await ctx.channel.send(embed=self.build_embed(draft, 0), view=EnterView(self))
        await self.db.execute(
            "INSERT INTO giveaways (message_id, guild_id, channel_id, prize, winners, ends_at, ended, host_id, "
            "required_role_id, winner_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, '', ?)",
            (message.id, ctx.guild.id, ctx.channel.id, prize, winners, ends_at, ctx.author.id,
             draft["required_role_id"], now()),
        )
        logger.info("%s started giveaway %s: %s (%s, %d winners)", ctx.author, message.id, prize, human(delta), winners)
        await ctx.send(f"✅ Giveaway started! It ends <t:{ends_at}:R>. Message ID: `{message.id}`", ephemeral=True)

    @giveaway.command(name="end", description="End a giveaway now and draw winners")
    @app_commands.describe(message="The giveaway's message ID or link")
    @staff_only()
    async def giveaway_end(self, ctx: commands.Context, message: str):
        message_id = parse_message_id(message)
        if message_id is None:
            await ctx.send("❓ Give me the giveaway's message ID or link.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        await ctx.send(await self.end_giveaway(message_id), ephemeral=True)

    @giveaway.command(name="reroll", description="Draw new winner(s) for an ended giveaway")
    @app_commands.describe(message="The giveaway's message ID or link", winners="How many new winners (default 1)")
    @staff_only()
    async def giveaway_reroll(self, ctx: commands.Context, message: str, winners: commands.Range[int, 1, MAX_WINNERS] = 1):
        message_id = parse_message_id(message)
        if message_id is None:
            await ctx.send("❓ Give me the giveaway's message ID or link.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        await ctx.send(await self.reroll_giveaway(message_id, winners), ephemeral=True)

    async def _autocomplete(self, interaction: discord.Interaction, current: str, ended: bool):
        if not is_staff(interaction.user):
            return []
        rows = await self.db.fetchall(
            "SELECT message_id, prize FROM giveaways WHERE guild_id = ? AND ended = ? ORDER BY ends_at DESC LIMIT 25",
            (interaction.guild_id, int(ended)),
        )
        current = current.lower()
        return [
            app_commands.Choice(name=trim(f"{r['prize']} ({r['message_id']})", 100), value=str(r["message_id"]))
            for r in rows if current in r["prize"].lower() or current in str(r["message_id"])
        ]

    @giveaway_end.autocomplete("message")
    async def _end_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ended=False)

    @giveaway_reroll.autocomplete("message")
    async def _reroll_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._autocomplete(interaction, current, ended=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Giveaways(bot))
