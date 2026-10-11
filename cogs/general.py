import logging

import discord
from discord.ext import commands

import config

logger = logging.getLogger("bot.general")


class General(commands.Cog):
    """Help, ping and admin utilities."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Show what the bot can do")
    async def help(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎮 Bot Commands",
            description=f"Use slash commands (`/`) or the `{config.COMMAND_PREFIX}` prefix.",
            color=discord.Color.blurple(),
        )
        for cog_name, cog in sorted(self.bot.cogs.items()):
            lines = []
            for cmd in cog.walk_commands():
                if cmd.hidden:
                    continue
                if isinstance(cmd, commands.Group) and not getattr(cmd, "fallback", None):
                    continue
                lines.append(f"`/{cmd.qualified_name}` — {cmd.description or cmd.short_doc or ''}")
            if lines:
                embed.add_field(name=cog_name, value="\n".join(lines)[:1024], inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @commands.hybrid_command(name="ping", description="Check if the bot is alive")
    async def ping(self, ctx: commands.Context):
        await ctx.send(f"🏓 Pong! `{self.bot.latency * 1000:.0f}ms`", ephemeral=True)

    @commands.command(name="sync", hidden=True)
    @commands.is_owner()
    async def sync(self, ctx: commands.Context):
        """Owner-only: re-sync slash commands (prefix only, e.g. `!sync`)."""
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.bot.tree.copy_global_to(guild=guild)
            synced = await self.bot.tree.sync(guild=guild)
        else:
            synced = await self.bot.tree.sync()
        logger.info("Manual sync by %s: %d commands", ctx.author, len(synced))
        await ctx.send(f"✅ Synced {len(synced)} slash commands.")


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))
