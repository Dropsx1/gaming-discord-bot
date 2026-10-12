"""Rules panel with an "I accept" button that grants MEMBER_ROLE_ID.

Edit the rules in data/rules.md (the first "# " line becomes the title), then
run /rulespanel in your #start-here or #rules channel. The button is a
persistent view, so it keeps working after restarts.
"""

import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import trim

logger = logging.getLogger("bot.onboarding")

RULES_FILE = Path(__file__).resolve().parent.parent / "data" / "rules.md"


def load_rules() -> tuple[str, str]:
    """Return (title, body) from data/rules.md."""
    text = RULES_FILE.read_text(encoding="utf-8").strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).strip()
    return "📜 Server Rules", text


class RulesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I accept the rules", style=discord.ButtonStyle.green, emoji="✅", custom_id="rules:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        role = interaction.guild.get_role(config.MEMBER_ROLE_ID) if config.MEMBER_ROLE_ID else None
        if role is None:
            logger.warning("Rules accepted by %s but MEMBER_ROLE_ID is not set or not found", member)
            await interaction.response.send_message(
                "Thanks for accepting! (Staff: set `MEMBER_ROLE_ID` so this button can unlock the server.)",
                ephemeral=True,
            )
            return
        if role in member.roles:
            await interaction.response.send_message("You've already accepted the rules. You're all set! 🎮", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Accepted the rules")
        except discord.Forbidden:
            logger.error("Can't give %s: the bot's role must be above it and have Manage Roles", role)
            await interaction.response.send_message(
                "⚠️ I couldn't give you access because of a permissions problem. Please tell staff.", ephemeral=True
            )
            return
        logger.info("%s accepted the rules", member)
        roles_hint = (
            f" Next, grab your roles in <#{config.START_HERE_CHANNEL_ID}>." if config.START_HERE_CHANNEL_ID else ""
        )
        await interaction.response.send_message(f"✅ Welcome in, {member.mention}! The server is unlocked.{roles_hint}",
                                                ephemeral=True)


class Onboarding(commands.Cog):
    """Rules acceptance."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.add_view(RulesView())

    @commands.hybrid_command(name="rulespanel", description="Post the rules with an 'I accept' button (staff)")
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    async def rulespanel(self, ctx: commands.Context):
        try:
            title, body = load_rules()
        except OSError as e:
            await ctx.send(f"⚠️ Couldn't read data/rules.md: {e}", ephemeral=True)
            return
        embed = discord.Embed(title=trim(title, 256), description=trim(body, 4096), color=discord.Color.green())
        if not config.MEMBER_ROLE_ID:
            await ctx.send("⚠️ Heads up: `MEMBER_ROLE_ID` isn't set, so the button won't grant a role yet.", ephemeral=True)
        await ctx.channel.send(embed=embed, view=RulesView())
        if ctx.interaction:
            await ctx.send("✅ Rules panel posted.", ephemeral=True)
        logger.info("Rules panel posted by %s in #%s", ctx.author, ctx.channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(Onboarding(bot))
