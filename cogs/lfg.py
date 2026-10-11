import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import autocomplete_from, trim

logger = logging.getLogger("bot.lfg")


class LFG(commands.Cog):
    """Looking-for-group posts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="lfg", description="Post a Looking For Group request")
    @app_commands.describe(
        game="The game you want to play",
        region="Your region (e.g. NA, EU, Asia)",
        platform="Your platform (PC, PlayStation, Xbox, Switch, Mobile, Crossplay)",
        rank="Optional: your rank or skill level",
        notes="Optional: extra notes (mic required, chill games, etc.)",
        ping_role="Ping the LFG role?",
    )
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def lfg(
        self,
        ctx: commands.Context,
        game: str,
        region: str,
        platform: str,
        rank: Optional[str] = None,
        notes: Optional[str] = None,
        ping_role: bool = False,
    ):
        """Prefix usage: !lfg "Valorant" NA PC "Gold 2" "need 2 for ranked" yes"""
        channel = ctx.guild.get_channel(config.LFG_CHANNEL_ID) if config.LFG_CHANNEL_ID else None
        if not isinstance(channel, discord.TextChannel):
            channel = ctx.channel

        author = ctx.author
        embed = discord.Embed(title=f"🎮 LFG: {trim(game, 200)}", color=discord.Color.blue())
        embed.set_author(name=author.display_name, icon_url=author.display_avatar.url)
        embed.add_field(name="Region", value=trim(region, 100), inline=True)
        embed.add_field(name="Platform", value=trim(platform, 100), inline=True)
        if rank:
            embed.add_field(name="Rank / Skill", value=trim(rank, 100), inline=True)
        if author.voice and author.voice.channel:
            embed.add_field(name="Voice", value=author.voice.channel.mention, inline=True)
        if notes:
            embed.add_field(name="Notes", value=trim(notes), inline=False)
        embed.add_field(name="Contact", value=f"DM or reply to {author.mention}", inline=False)
        embed.set_footer(text="React with 🎮 if you're in!")
        embed.timestamp = discord.utils.utcnow()

        content = None
        if ping_role and config.LFG_ROLE_ID:
            role = ctx.guild.get_role(config.LFG_ROLE_ID)
            if role:
                content = role.mention

        try:
            msg = await channel.send(content=content, embed=embed)
            await msg.add_reaction("🎮")
        except discord.Forbidden:
            await ctx.send("⚠️ I can't post in the LFG channel. Check my permissions.", ephemeral=True)
            return

        logger.info("LFG posted by %s for %s in #%s", author, game, channel)
        if channel != ctx.channel or ctx.interaction:
            await ctx.send(f"✅ LFG posted in {channel.mention}!", ephemeral=True)

    lfg.autocomplete("game")(autocomplete_from(config.LFG_GAMES))
    lfg.autocomplete("region")(autocomplete_from(config.LFG_REGIONS))
    lfg.autocomplete("platform")(autocomplete_from(config.LFG_PLATFORMS))


async def setup(bot: commands.Bot):
    await bot.add_cog(LFG(bot))
