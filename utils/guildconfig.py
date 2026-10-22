"""Per-server channel/role IDs.

/setup saves IDs in the guild_config table. Every lookup goes through get():
  1. the ID saved for this server (guild_config), else
  2. the env var of the same name, but only for the "home" server
     (GUILD_ID, or any server when GUILD_ID is unset).

Values are cached in memory so lookups stay synchronous and fast.
"""

import logging
from typing import Optional, Union

import discord

import config

log = logging.getLogger("bot.guildconfig")

# Keys the rest of the bot reads. Names match the env vars.
CHANNEL_KEYS = [
    "WELCOME_CHANNEL_ID", "START_HERE_CHANNEL_ID", "SELF_ROLES_CHANNEL_ID", "LFG_CHANNEL_ID", "EVENT_CHANNEL_ID",
    "LOG_CHANNEL_ID", "MOD_LOG_CHANNEL_ID", "TICKET_LOG_CHANNEL_ID", "PROMPT_CHANNEL_ID", "DIGEST_CHANNEL_ID",
    "CHALLENGE_CHANNEL_ID", "JOIN_TO_CREATE_CHANNEL_ID", "NEWS_CHANNEL_ID",
    "GAMING_NEWS_CHANNEL_ID",
]
CATEGORY_KEYS = ["TICKET_CATEGORY_ID", "VOICE_CATEGORY_ID"]
ROLE_KEYS = [
    "STAFF_ROLE_ID", "MEMBER_ROLE_ID", "LFG_ROLE_ID", "PROMPT_PING_ROLE_ID", "PROMPT_KING_ROLE_ID",
    "CHALLENGE_PING_ROLE_ID", "REMIX_CHAMPION_ROLE_ID", "NEWS_ROLE_ID",
    "GAMING_NEWS_PING_ROLE_ID",
]
KEYS = CHANNEL_KEYS + CATEGORY_KEYS + ROLE_KEYS

# If a key isn't set, use this one instead (e.g. mod log -> general staff log).
FALLBACKS = {
    "MOD_LOG_CHANNEL_ID": "LOG_CHANNEL_ID", "TICKET_LOG_CHANNEL_ID": "LOG_CHANNEL_ID",
    "ASK_CHANNEL_ID": "BOT_COMMANDS_CHANNEL_ID",
}

_cache: dict[int, dict[str, int]] = {}
_db = None

GuildLike = Union[discord.Guild, discord.abc.Snowflake, int, None]


def _gid(guild: GuildLike) -> Optional[int]:
    if guild is None:
        return None
    return guild if isinstance(guild, int) else guild.id


def is_home(guild: GuildLike) -> bool:
    """Env-var IDs belong to the home server only."""
    gid = _gid(guild)
    return not config.GUILD_ID or gid == config.GUILD_ID


def _env(key: str) -> int:
    return int(getattr(config, key, 0) or 0)


def get(guild: GuildLike, key: str) -> int:
    """Channel/role ID for this server (0 if not configured)."""
    gid = _gid(guild)
    saved = _cache.get(gid, {}) if gid is not None else {}
    if saved.get(key):
        return saved[key]
    fallback = FALLBACKS.get(key)
    if fallback and saved.get(fallback):
        return saved[fallback]
    if gid is not None and not is_home(gid):
        return 0
    return _env(key)


def saved(guild: GuildLike) -> dict[str, int]:
    return dict(_cache.get(_gid(guild), {}))


def configured_guild_ids() -> set[int]:
    return set(_cache)


async def init(db) -> None:
    """Load all saved config. Called once at startup after the DB connects."""
    global _db
    _db = db
    _cache.clear()
    for row in await db.fetchall("SELECT guild_id, key, value FROM guild_config"):
        try:
            _cache.setdefault(row["guild_id"], {})[row["key"]] = int(row["value"])
        except (TypeError, ValueError):
            log.warning("Ignoring bad guild_config value %r for %s", row["value"], row["key"])
    log.info("Loaded saved config for %d server(s)", len(_cache))


async def set_many(guild: GuildLike, values: dict[str, int]) -> None:
    gid = _gid(guild)
    await _db.conn.executemany(
        "INSERT INTO guild_config (guild_id, key, value, updated_at) VALUES (?, ?, ?, strftime('%s','now')) "
        "ON CONFLICT (guild_id, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        [(gid, k, str(v)) for k, v in values.items()],
    )
    await _db.conn.commit()
    _cache.setdefault(gid, {}).update({k: int(v) for k, v in values.items()})


async def delete_keys(guild: GuildLike, keys) -> None:
    gid = _gid(guild)
    keys = list(keys)
    await _db.conn.executemany("DELETE FROM guild_config WHERE guild_id = ? AND key = ?", [(gid, k) for k in keys])
    await _db.conn.commit()
    for k in keys:
        _cache.get(gid, {}).pop(k, None)


def channel(guild: discord.Guild, key: str):
    """Resolve a configured channel, only if it belongs to this server."""
    cid = get(guild, key)
    ch = guild.get_channel(cid) if cid and guild is not None else None
    return ch


def role(guild: discord.Guild, key: str) -> Optional[discord.Role]:
    rid = get(guild, key)
    return guild.get_role(rid) if rid and guild is not None else None
