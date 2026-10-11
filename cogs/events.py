import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import is_staff, trim

logger = logging.getLogger("bot.events")


def can_host_events():
    async def predicate(ctx: commands.Context) -> bool:
        if is_staff(ctx.author) or ctx.author.guild_permissions.manage_events:
            return True
        raise commands.CheckFailure("You need the staff role or **Manage Events** to announce events.")

    return commands.check(predicate)


class Events(commands.Cog):
    """Event announcements."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="event", description="Announce a server event")
    @app_commands.describe(
        title="Event title",
        time="When is it? (e.g. Saturday 8PM EST)",
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
        """Prefix usage: !event "Among Us Night" "Sat 8PM EST" "Bring snacks" @Gamers"""
        channel = ctx.guild.get_channel(config.EVENT_CHANNEL_ID) if config.EVENT_CHANNEL_ID else None
        if not isinstance(channel, discord.TextChannel):
            channel = ctx.channel

        embed = discord.Embed(
            title=f"📅 {trim(title, 240)}",
            description=trim(description, 4000),
            color=discord.Color.gold(),
        )
        embed.set_author(name="Event Announcement")
        embed.add_field(name="🕒 When", value=trim(time, 1024), inline=False)
        embed.add_field(name="RSVP", value="✅ Going • ❔ Maybe • ❌ Can't make it", inline=False)
        embed.set_footer(text=f"Hosted by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)

        content = None
        if ping_role:
            if ping_role.is_default():
                await ctx.send("Pinging @everyone isn't allowed here. Use a role instead.", ephemeral=True)
                return
            content = ping_role.mention

        try:
            msg = await channel.send(content=content, embed=embed)
            for emoji in ("✅", "❔", "❌"):
                await msg.add_reaction(emoji)
        except discord.Forbidden:
            await ctx.send("⚠️ I can't post in the events channel. Check my permissions.", ephemeral=True)
            return

        logger.info("Event '%s' announced by %s in #%s", title, ctx.author, channel)
        if channel != ctx.channel or ctx.interaction:
            await ctx.send(f"✅ Event announced in {channel.mention}!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Events(bot))
