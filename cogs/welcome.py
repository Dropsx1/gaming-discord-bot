import asyncio
import logging
import time
from collections import deque
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.ai_features import NSFW_RE
from utils import ai
from utils import guildconfig as gc

logger = logging.getLogger("bot.welcome")

WELCOME_AI = config._bool("WELCOME_AI", True)  # AI-personalized greeting when XAI_API_KEY is set
WELCOME_AI_PER_HOUR = 20   # per server; a join raid falls back to the template (cost guard)
WELCOME_AI_TIMEOUT = 8     # seconds; never hold up the welcome for long


class Welcome(commands.Cog):
    """Greets new members and points them to #start-here."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.recent: dict[int, deque] = {}

    def _ai_allowed(self, guild_id: int, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        q = self.recent.setdefault(guild_id, deque())
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= WELCOME_AI_PER_HOUR:
            return False
        q.append(now)
        return True

    async def ai_greeting(self, member: discord.Member) -> Optional[str]:
        """A personalized one-liner, or None (AI off, busy, slow or failed) so the template is used."""
        if not (WELCOME_AI and ai.key_set()) or not self._ai_allowed(member.guild.id):
            return None
        try:
            reply = await asyncio.wait_for(
                ai.welcome_line(member.guild.name, member.display_name, member.guild.member_count), WELCOME_AI_TIMEOUT
            )
        except asyncio.TimeoutError:
            reply = None
        try:
            await self.bot.db.log_ai(member.guild.id, member.id, "welcome", reply.tokens if reply else 0, ok=bool(reply))
        except Exception:
            logger.exception("Could not log AI usage")
        if reply is None or NSFW_RE.search(reply.text):
            return None
        return reply.text

    def build_embed(self, member: discord.Member, greeting: Optional[str] = None) -> discord.Embed:
        def link(key: str, fallback: str) -> str:
            cid = gc.get(member.guild, key)
            return f"<#{cid}>" if cid else fallback

        start_here = link("START_HERE_CHANNEL_ID", "#start-here")
        roles_ch = link("SELF_ROLES_CHANNEL_ID", "")
        lfg = link("LFG_CHANNEL_ID", "the LFG channel")
        intro = f"Hey {member.mention}! {greeting}" if greeting else f"Hey {member.mention}, glad you made it!"
        embed = discord.Embed(
            title=f"Welcome to {member.guild.name}, {member.display_name}! 🎮",
            description=(
                f"{intro}\n\n"
                f"📌 **Start here:** head to {start_here}, read the rules and click **I accept** to unlock the server.\n"
                f"🎭 **Roles:** pick your platforms, games and interests{' in ' + roles_ch if roles_ch else ''}, plus pings for events and challenges.\n"
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

        # With the rules button (/rulespanel) the member role is earned there instead.
        member_role_id = gc.get(member.guild, "MEMBER_ROLE_ID")
        if config.MEMBER_ROLE_ON_JOIN and member_role_id:
            role = member.guild.get_role(member_role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Auto member role")
                except discord.HTTPException as e:
                    logger.warning("Could not give member role to %s: %s", member, e)
            else:
                logger.warning("MEMBER_ROLE_ID %s not found", member_role_id)

        welcome_id = gc.get(member.guild, "WELCOME_CHANNEL_ID")
        if not welcome_id:
            return
        channel = member.guild.get_channel(welcome_id)
        if not isinstance(channel, discord.abc.Messageable):
            logger.warning("Welcome channel %s not found", welcome_id)
            return
        try:
            greeting = await self.ai_greeting(member)
            await channel.send(content=member.mention, embed=self.build_embed(member, greeting),
                               allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
        except discord.HTTPException as e:
            logger.error("Could not send welcome message: %s", e)

    @commands.hybrid_command(name="welcometest", description="Preview the welcome message (staff)")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def welcometest(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        await ctx.send(embed=self.build_embed(ctx.author, await self.ai_greeting(ctx.author)), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
