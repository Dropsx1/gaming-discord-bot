import logging
import sys

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import ai, guildconfig
from utils.db import Database

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],  # Railway captures stdout
)
logger = logging.getLogger("bot")

COGS = [
    "cogs.general",
    "cogs.setup",
    "cogs.welcome",
    "cogs.onboarding",
    "cogs.lfg",
    "cogs.events",
    "cogs.roles",
    "cogs.moderation",
    "cogs.fun",
    "cogs.voice",
    "cogs.tickets",
    "cogs.schedule",
    "cogs.giveaways",
    "cogs.ai_features",
    "cogs.ai_extras",
    "cogs.news",
]

intents = discord.Intents.default()
intents.members = True          # privileged: welcome + member lookups
intents.message_content = True  # privileged: "!" prefix commands
intents.voice_states = True
intents.guilds = True


class GamingBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned_or(config.COMMAND_PREFIX),
            intents=intents,
            help_command=None,  # replaced by the hybrid /help in cogs.general
            case_insensitive=True,
            activity=discord.Activity(type=discord.ActivityType.playing, name="with the community"),
            # Never let user input trigger @everyone/@here.
            allowed_mentions=discord.AllowedMentions(everyone=False, users=True, roles=True),
            # Raise instead of silently waiting minutes on a rate limit (e.g. channel renames).
            max_ratelimit_timeout=30.0,
        )
        self.db = Database(config.DB_PATH)

    async def setup_hook(self):
        self.add_check(self._guild_only)
        await self.db.connect()  # before cogs, so they can use self.bot.db
        await guildconfig.init(self.db)  # per-server IDs saved by /setup
        await ai.set_store(self.db)  # per-feature daily AI cost caps survive restarts
        self._synced_guilds: set[int] = set()

        for cog in COGS:
            try:
                await self.load_extension(cog)
                logger.info("Loaded cog: %s", cog)
            except Exception:
                logger.exception("Failed to load cog %s", cog)

        if not config.SYNC_COMMANDS:
            logger.info("SYNC_COMMANDS is off; skipping slash command sync")
        elif config.GUILD_ID:
            await self.sync_guild(config.GUILD_ID)
        # Without GUILD_ID, commands are synced to every server in on_ready / on_guild_join.

    async def sync_guild(self, guild_id: int) -> int:
        """Instantly sync slash commands to one server. Returns how many were synced."""
        guild = discord.Object(id=guild_id)
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        except discord.HTTPException:
            logger.exception("Slash command sync failed for guild %s", guild_id)
            return 0
        self._synced_guilds.add(guild_id)
        logger.info("Synced %d slash commands to guild %s", len(synced), guild_id)
        return len(synced)

    async def on_guild_join(self, guild: discord.Guild):
        logger.info("Joined new server: %s (%s). Run /setup there to configure it.", guild.name, guild.id)
        if config.SYNC_COMMANDS and not config.GUILD_ID:
            await self.sync_guild(guild.id)

    async def close(self):
        await super().close()
        await self.db.close()

    @staticmethod
    async def _guild_only(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        return True

    async def on_ready(self):
        logger.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        logger.info("Connected to %d guild(s): %s", len(self.guilds), ", ".join(g.name for g in self.guilds))
        if config.SYNC_COMMANDS and not config.GUILD_ID:
            for guild in self.guilds:  # on_ready can repeat after reconnects; sync each server once
                if guild.id not in self._synced_guilds:
                    await self.sync_guild(guild.id)
        if config.GUILD_ID and not self.get_guild(config.GUILD_ID):
            logger.warning("GUILD_ID %s is set but the bot is not in that server", config.GUILD_ID)

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        # Unwrap hybrid/app-command wrappers to get the real error.
        if isinstance(error, commands.HybridCommandError):
            error = error.original
        if isinstance(error, (commands.CommandInvokeError, app_commands.CommandInvokeError)):
            error = error.original

        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, (commands.MissingPermissions, app_commands.MissingPermissions)):
            perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            msg = f"🚫 You need **{perms}** to use this."
        elif isinstance(error, (commands.BotMissingPermissions, app_commands.BotMissingPermissions)):
            perms = ", ".join(p.replace("_", " ").title() for p in error.missing_permissions)
            msg = f"⚠️ I'm missing **{perms}** for that. Ask an admin to check my role."
        elif isinstance(error, commands.NoPrivateMessage):
            msg = "This command only works inside the server."
        elif isinstance(error, commands.CommandOnCooldown):
            msg = f"⏳ Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, commands.UserInputError):
            usage = f"{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}" if ctx.command else ""
            msg = f"❓ {error}" + (f"\nUsage: `{usage}`" if usage else "")
        elif isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
            msg = f"🚫 {error}" if str(error) else "🚫 You can't use this command."
        elif isinstance(error, discord.Forbidden):
            msg = "⚠️ I don't have permission to do that. Check my role position and channel permissions."
        elif isinstance(error, discord.RateLimited):
            msg = f"⏳ Discord is rate-limiting that action. Try again in about {error.retry_after:.0f}s."
        else:
            logger.error(
                "Unhandled error in command %s (by %s)",
                ctx.command.qualified_name if ctx.command else "?",
                ctx.author,
                exc_info=error,
            )
            msg = "💥 Something went wrong. Please try again or contact staff."

        try:
            await ctx.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass


bot = GamingBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Errors from pure slash commands (hybrid ones go through on_command_error)."""
    if isinstance(error, app_commands.MissingPermissions):
        msg = "🚫 You don't have permission to use this."
    elif isinstance(error, app_commands.BotMissingPermissions):
        msg = "⚠️ I don't have the required permissions."
    elif isinstance(error, app_commands.CheckFailure):
        msg = f"🚫 {error}" if str(error) else "🚫 You can't use this command."
    else:
        logger.error("App command error in %s", interaction.command and interaction.command.name, exc_info=error)
        msg = "💥 Something went wrong. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        logger.critical("DISCORD_TOKEN is not set. Add it to your environment variables.")
        raise SystemExit(1)
    bot.run(config.DISCORD_TOKEN, log_handler=None)  # use our logging config
