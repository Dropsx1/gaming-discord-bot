"""Self-role menus. Edit PLATFORM_ROLES / GAME_ROLES / INTEREST_ROLES / PING_ROLES in config.py."""

import logging
import time
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import config

logger = logging.getLogger("bot.roles")

RoleRef = Union[str, int]


# Discord rate-limits role creation hard (sometimes ~10+ minutes). When that happens we
# stop trying until the limit resets, instead of failing every click. guild_id -> unix time
_create_blocked_until: dict[int, float] = {}


def creation_wait(guild: discord.Guild) -> int:
    """Seconds until role creation is allowed again in this server (0 = allowed)."""
    return max(0, int(_create_blocked_until.get(guild.id, 0) - time.time()))


async def resolve_role(guild: discord.Guild, ref: RoleRef, create: bool = False) -> Optional[discord.Role]:
    """Find a role by ID or exact name. Create it only when `create` is True (the member picked it)."""
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        return guild.get_role(int(ref))
    role = discord.utils.get(guild.roles, name=ref)
    if role or not create or not config.SELF_ROLES_AUTO_CREATE or creation_wait(guild):
        return role
    try:
        role = await guild.create_role(name=ref, mentionable=True, reason="Self-role auto-created")
        logger.info("Auto-created self-role %r in %s", ref, guild)
        return role
    except discord.RateLimited as e:
        _create_blocked_until[guild.id] = time.time() + e.retry_after
        logger.warning("Discord is rate-limiting role creation in %s for %.0fs", guild, e.retry_after)
    except discord.HTTPException as e:
        logger.warning("Could not create self-role %r: %s", ref, e)
    return None


class RoleSelect(discord.ui.Select):
    def __init__(self, entries: list, placeholder: str, custom_id: str):
        entries = entries[:25]  # Discord limit
        options = [
            discord.SelectOption(label=label, value=str(ref), emoji=emoji)
            for label, ref, emoji in entries
        ]
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._apply(interaction)
        except Exception:
            logger.exception("Self-role menu failed for %s", interaction.user)
            await interaction.followup.send("💥 Something went wrong updating your roles. Please try again.", ephemeral=True)

    async def _apply(self, interaction: discord.Interaction):
        member = interaction.user
        guild = interaction.guild
        selected = set(self.values)

        to_add, to_remove, missing = [], [], []
        for option in self.options:
            picked = option.value in selected
            # Only roles the member picked are ever created; unpicked missing roles are skipped.
            role = await resolve_role(guild, option.value, create=picked)
            if role is None:
                if picked:
                    missing.append(option.label)
                continue
            if picked and role not in member.roles:
                to_add.append(role)
            elif not picked and role in member.roles:
                to_remove.append(role)

        try:
            if to_add:
                await member.add_roles(*to_add, reason="Self-role menu")
            if to_remove:
                await member.remove_roles(*to_remove, reason="Self-role menu")
        except discord.Forbidden:
            logger.warning("Role hierarchy/permission problem assigning self-roles in %s", guild)
            await interaction.followup.send(
                "⚠️ I couldn't update your roles. My role needs to be **above** these roles. Please tell staff.",
                ephemeral=True,
            )
            return

        lines = []
        if to_add:
            lines.append("✅ Added: " + ", ".join(r.mention for r in to_add))
        if to_remove:
            lines.append("➖ Removed: " + ", ".join(r.mention for r in to_remove))
        if missing:
            wait = creation_wait(guild)
            when = f" Try again in about {max(1, round(wait / 60))} min." if wait else " Please tell staff."
            lines.append("⚠️ Couldn't set up yet: " + ", ".join(missing) + "." + when)
        if not lines:
            lines.append("No changes — you're all set.")
        logger.info("Self-roles for %s: +%s -%s", member, [r.name for r in to_add], [r.name for r in to_remove])
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class RoleView(discord.ui.View):
    """Persistent: registered at startup so old menus keep working after restarts."""

    def __init__(self):
        super().__init__(timeout=None)
        if config.PLATFORM_ROLES:
            self.add_item(RoleSelect(config.PLATFORM_ROLES, "🎮 Choose your platform(s)", "selfroles:platform"))
        if config.GAME_ROLES:
            self.add_item(RoleSelect(config.GAME_ROLES, "🕹️ Choose your main game(s)", "selfroles:games"))
        if config.INTEREST_ROLES:
            self.add_item(RoleSelect(config.INTEREST_ROLES, "✨ What are you into?", "selfroles:interests"))
        if config.PING_ROLES:
            self.add_item(RoleSelect(config.PING_ROLES, "🔔 Which pings do you want?", "selfroles:pings"))


class Roles(commands.Cog):
    """Self-assignable platform and game roles."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(RoleView())

    @commands.hybrid_command(name="roles", description="Post the self-role menu (staff)")
    @app_commands.default_permissions(manage_roles=True)
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def roles(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎭 Pick Your Roles",
            description=(
                "Use the menus below. You can pick several in each one.\n\n"
                "🎮 **Platforms** and 🕹️ **Games** help squads find you for LFG.\n"
                "✨ **Interests** show what you're into: creating, AI, tech or game dev.\n"
                "🔔 **Pings** are opt-in notifications for events, challenges, game nights and the daily prompt.\n\n"
                "To remove a role, open its menu and deselect it."
            ),
            color=discord.Color.purple(),
        )
        await ctx.channel.send(embed=embed, view=RoleView())
        if ctx.interaction:
            await ctx.send("✅ Role menu posted.", ephemeral=True)
        logger.info("Role menu posted by %s in #%s", ctx.author, ctx.channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(Roles(bot))
