import logging

import discord
from discord import app_commands
from discord.ext import commands

import config

logger = logging.getLogger("bot.welcome")


class Welcome(commands.Cog):
    """Greets new members and points them to #start-here."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def build_embed(self, member: discord.Member) -> discord.Embed:
        start_here = f"<#{config.START_HERE_CHANNEL_ID}>" if config.START_HERE_CHANNEL_ID else "#start-here"
        lfg = f"<#{config.LFG_CHANNEL_ID}>" if config.LFG_CHANNEL_ID else "the LFG channel"
        embed = discord.Embed(
            title=f"Welcome to {member.guild.name}, {member.display_name}! 🎮",
            description=(
                f"Hey {member.mention}, glad you made it!\n\n"
                f"📌 **Start here:** head to {start_here} to read the rules and grab your roles.\n"
                f"🎭 **Roles:** pick your platform and main games so squads can find you.\n"
                f"🤝 **LFG:** use `/lfg` in {lfg} to find teammates, or hop into a voice channel.\n\n"
                f"Have fun and keep it chill. GG!"
            ),
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Member #{member.guild.member_count} • SFW gaming community")
        return embed

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        logger.info("Member joined: %s (%s) in %s", member, member.id, member.guild)

        if config.MEMBER_ROLE_ID:
            role = member.guild.get_role(config.MEMBER_ROLE_ID)
            if role:
                try:
                    await member.add_roles(role, reason="Auto member role")
                except discord.HTTPException as e:
                    logger.warning("Could not give member role to %s: %s", member, e)
            else:
                logger.warning("MEMBER_ROLE_ID %s not found", config.MEMBER_ROLE_ID)

        if not config.WELCOME_CHANNEL_ID:
            return
        channel = member.guild.get_channel(config.WELCOME_CHANNEL_ID)
        if not isinstance(channel, discord.abc.Messageable):
            logger.warning("Welcome channel %s not found", config.WELCOME_CHANNEL_ID)
            return
        try:
            await channel.send(content=member.mention, embed=self.build_embed(member))
        except discord.HTTPException as e:
            logger.error("Could not send welcome message: %s", e)

    @commands.hybrid_command(name="welcometest", description="Preview the welcome message (staff)")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def welcometest(self, ctx: commands.Context):
        await ctx.send(embed=self.build_embed(ctx.author), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
