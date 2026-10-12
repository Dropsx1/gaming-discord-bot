"""Shared helpers: staff checks, log-channel posting, text trimming."""

import logging
import re
from datetime import timedelta
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


_DURATION_RE = re.compile(r"(\d+)\s*([smhdw]?)", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks", "": "minutes"}


def parse_duration(text: str) -> Optional[timedelta]:
    """'30' -> 30 min, '10m', '2h', '1d', '1h30m'. Returns None if invalid."""
    text = text.strip().lower().replace(" ", "")
    if not text or _DURATION_RE.sub("", text):
        return None
    total = timedelta()
    try:
        for amount, unit in _DURATION_RE.findall(text):
            total += timedelta(**{_UNITS[unit]: int(amount)})
    except OverflowError:
        return None
    return total if total.total_seconds() > 0 else None


def human(td: timedelta) -> str:
    secs = int(td.total_seconds())
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if secs >= size:
            parts.append(f"{secs // size}{name}")
            secs %= size
    return " ".join(parts) or "0s"
