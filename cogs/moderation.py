import logging
import re
from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import send_log, trim

logger = logging.getLogger("bot.moderation")

MAX_TIMEOUT = timedelta(days=28)  # Discord limit
_DURATION_RE = re.compile(r"(\d+)\s*([smhdw]?)", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks", "": "minutes"}


def parse_duration(text: str) -> Optional[timedelta]:
    """'30' -> 30 min, '10m', '2h', '1d', '1h30m'. Returns None if invalid."""
    text = text.strip().lower().replace(" ", "")
    if not text or _DURATION_RE.sub("", text):
        return None
    total = timedelta()
    for amount, unit in _DURATION_RE.findall(text):
        total += timedelta(**{_UNITS[unit]: int(amount)})
    return total if total.total_seconds() > 0 else None


def human(td: timedelta) -> str:
    secs = int(td.total_seconds())
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if secs >= size:
            parts.append(f"{secs // size}{name}")
            secs %= size
    return " ".join(parts) or "0s"


def hierarchy_error(ctx: commands.Context, member: discord.Member) -> Optional[str]:
    """Return a reason the action isn't allowed, or None if it's fine."""
    guild = ctx.guild
    if member == ctx.author:
        return "You can't do that to yourself."
    if member == guild.me:
        return "Nice try. 😄"
    if member == guild.owner:
        return "You can't moderate the server owner."
    if ctx.author != guild.owner and member.top_role >= ctx.author.top_role:
        return "That member's top role is equal to or higher than yours."
    if member.top_role >= guild.me.top_role:
        return "That member's top role is higher than mine, so I can't act on them."
    return None


class Moderation(commands.Cog):
    """Warn, timeout and kick with staff logging."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def mod_embed(self, title: str, color: discord.Color, ctx: commands.Context, member, reason: str) -> discord.Embed:
        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=f"{member.mention} (`{member}` • {member.id})", inline=False)
        embed.add_field(name="Moderator", value=f"{ctx.author.mention} (`{ctx.author}`)", inline=False)
        embed.add_field(name="Reason", value=trim(reason), inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        return embed

    async def dm(self, member: discord.Member, text: str) -> bool:
        try:
            await member.send(text)
            return True
        except discord.HTTPException:
            return False

    @commands.hybrid_command(name="warn", description="Warn a member")
    @app_commands.describe(member="Member to warn", reason="Reason for the warning")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if member == ctx.author or member.bot:
            await ctx.send("You can't warn that member.", ephemeral=True)
            return
        dm_ok = await self.dm(member, f"⚠️ You received a warning in **{ctx.guild.name}**.\n**Reason:** {reason}")
        embed = self.mod_embed("⚠️ Warning Issued", discord.Color.orange(), ctx, member, reason)
        embed.set_footer(text="DM delivered" if dm_ok else "DM failed (closed DMs)")
        await send_log(ctx.guild, config.MOD_LOG_CHANNEL_ID, embed)
        logger.info("%s warned %s: %s", ctx.author, member, reason)
        await ctx.send(f"⚠️ {member.mention} has been warned.", ephemeral=True)

    @commands.hybrid_command(name="timeout", description="Timeout (mute) a member")
    @app_commands.describe(member="Member to timeout", duration="e.g. 30 (minutes), 10m, 2h, 1d", reason="Reason")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(
        self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = "No reason provided"
    ):
        if err := hierarchy_error(ctx, member):
            await ctx.send(f"🚫 {err}", ephemeral=True)
            return
        delta = parse_duration(duration)
        if delta is None or delta > MAX_TIMEOUT:
            await ctx.send("❓ Invalid duration. Use e.g. `30`, `10m`, `2h`, `1d` (max 28 days).", ephemeral=True)
            return

        await member.timeout(delta, reason=f"{ctx.author}: {reason}")
        await self.dm(member, f"🔇 You were timed out in **{ctx.guild.name}** for {human(delta)}.\n**Reason:** {reason}")

        embed = self.mod_embed("🔇 Member Timed Out", discord.Color.dark_orange(), ctx, member, reason)
        embed.add_field(name="Duration", value=human(delta), inline=True)
        await send_log(ctx.guild, config.MOD_LOG_CHANNEL_ID, embed)
        logger.info("%s timed out %s for %s: %s", ctx.author, member, human(delta), reason)
        await ctx.send(f"🔇 {member.mention} has been timed out for **{human(delta)}**.", ephemeral=True)

    @commands.hybrid_command(name="untimeout", description="Remove a member's timeout")
    @app_commands.describe(member="Member to un-timeout", reason="Reason")
    @app_commands.default_permissions(moderate_members=True)
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if not member.is_timed_out():
            await ctx.send("That member isn't timed out.", ephemeral=True)
            return
        await member.timeout(None, reason=f"{ctx.author}: {reason}")
        embed = self.mod_embed("🔊 Timeout Removed", discord.Color.green(), ctx, member, reason)
        await send_log(ctx.guild, config.MOD_LOG_CHANNEL_ID, embed)
        logger.info("%s removed timeout for %s", ctx.author, member)
        await ctx.send(f"🔊 {member.mention}'s timeout was removed.", ephemeral=True)

    @commands.hybrid_command(name="kick", description="Kick a member")
    @app_commands.describe(member="Member to kick", reason="Reason for the kick")
    @app_commands.default_permissions(kick_members=True)
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        if err := hierarchy_error(ctx, member):
            await ctx.send(f"🚫 {err}", ephemeral=True)
            return
        # DM first -- after the kick we may no longer share a server.
        await self.dm(member, f"👢 You were kicked from **{ctx.guild.name}**.\n**Reason:** {reason}")
        await member.kick(reason=f"{ctx.author}: {reason}")

        embed = self.mod_embed("👢 Member Kicked", discord.Color.red(), ctx, member, reason)
        await send_log(ctx.guild, config.MOD_LOG_CHANNEL_ID, embed)
        logger.info("%s kicked %s: %s", ctx.author, member, reason)
        await ctx.send(f"👢 **{member}** has been kicked.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
