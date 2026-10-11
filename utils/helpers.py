"""Shared helpers: staff checks, log-channel posting, text trimming."""

import logging
from typing import Optional

import discord
from discord.ext import commands

import config

log = logging.getLogger("bot.utils")


class NotStaff(commands.CheckFailure):
    def __init__(self):
        super().__init__("This command is for staff only.")


def is_staff(member: discord.abc.User) -> bool:
    """Staff = has the configured staff role, or Manage Server / Administrator."""
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    return bool(config.STAFF_ROLE_ID) and member.get_role(config.STAFF_ROLE_ID) is not None


def staff_only():
    """Command check: staff role or Manage Server."""

    async def predicate(ctx: commands.Context) -> bool:
        if is_staff(ctx.author):
            return True
        raise NotStaff()

    return commands.check(predicate)


async def send_log(
    guild: discord.Guild,
    channel_id: int,
    embed: discord.Embed,
    file: Optional[discord.File] = None,
) -> None:
    """Post to a log channel, never raising -- logging must not break commands."""
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.abc.Messageable):
        log.warning("Log channel %s not found in %s", channel_id, guild)
        return
    try:
        if file:
            await channel.send(embed=embed, file=file)
        else:
            await channel.send(embed=embed)
    except discord.HTTPException as e:
        log.warning("Could not post to log channel %s: %s", channel_id, e)


def trim(text: Optional[str], limit: int = 1024) -> str:
    """Trim text to fit embed limits."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def autocomplete_from(values: list[str]):
    """Build an app-command autocomplete callback from a static list."""

    async def _autocomplete(interaction: discord.Interaction, current: str):
        current = current.lower()
        matches = [v for v in values if current in v.lower()] if current else values
        return [discord.app_commands.Choice(name=v, value=v) for v in matches[:25]]

    return _autocomplete
