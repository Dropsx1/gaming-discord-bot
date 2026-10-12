"""Central configuration.

Everything server-specific comes from environment variables (Railway "Variables"
tab, or a local .env file). The lists at the bottom of this file -- self-roles,
games, regions -- are meant to be edited directly.
"""

import logging
import os
from datetime import time

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("bot.config")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int = 0) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("%s=%r is not a valid number/ID, ignoring it", name, raw)
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# --- Core -------------------------------------------------------------------
DISCORD_TOKEN = _str("DISCORD_TOKEN")
COMMAND_PREFIX = _str("COMMAND_PREFIX", "!") or "!"
GUILD_ID = _int("GUILD_ID")  # set for instant slash-command sync to one server
SYNC_COMMANDS = _bool("SYNC_COMMANDS", True)
LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()

# --- Storage ----------------------------------------------------------------
# On Railway, mount a volume at /data and set DB_PATH=/data/bot.db
DB_PATH = _str("DB_PATH", "./data/bot.db") or "./data/bot.db"

# --- Channels ---------------------------------------------------------------
WELCOME_CHANNEL_ID = _int("WELCOME_CHANNEL_ID")
START_HERE_CHANNEL_ID = _int("START_HERE_CHANNEL_ID")
LFG_CHANNEL_ID = _int("LFG_CHANNEL_ID")
EVENT_CHANNEL_ID = _int("EVENT_CHANNEL_ID")
LOG_CHANNEL_ID = _int("LOG_CHANNEL_ID")
MOD_LOG_CHANNEL_ID = _int("MOD_LOG_CHANNEL_ID") or LOG_CHANNEL_ID
TICKET_LOG_CHANNEL_ID = _int("TICKET_LOG_CHANNEL_ID") or LOG_CHANNEL_ID

# --- Roles ------------------------------------------------------------------
STAFF_ROLE_ID = _int("STAFF_ROLE_ID")
LFG_ROLE_ID = _int("LFG_ROLE_ID")
MEMBER_ROLE_ID = _int("MEMBER_ROLE_ID")  # optional auto-role on join

# --- Tickets ----------------------------------------------------------------
TICKET_CATEGORY_ID = _int("TICKET_CATEGORY_ID")
TICKET_TRANSCRIPT_LIMIT = _int("TICKET_TRANSCRIPT_LIMIT", 500)
TICKET_DM_TRANSCRIPT = _bool("TICKET_DM_TRANSCRIPT", False)

# --- Auto voice -------------------------------------------------------------
JOIN_TO_CREATE_CHANNEL_ID = _int("JOIN_TO_CREATE_CHANNEL_ID")
VOICE_CATEGORY_ID = _int("VOICE_CATEGORY_ID")  # falls back to the Join-to-Create channel's category
VC_NAME_TEMPLATE = _str("VC_NAME_TEMPLATE", "🎮 {user}'s Room")  # {user} = display name
VC_DEFAULT_LIMIT = _int("VC_DEFAULT_LIMIT", 0)  # 0 = unlimited

# --- Scheduled content --------------------------------------------------------
SCHEDULE_ENABLED = _bool("SCHEDULE_ENABLED", True)
TIMEZONE = _str("TIMEZONE", "America/New_York") or "America/New_York"
PROMPT_CHANNEL_ID = _int("PROMPT_CHANNEL_ID")        # text or forum channel
PROMPT_PING_ROLE_ID = _int("PROMPT_PING_ROLE_ID")
PROMPT_KING_ROLE_ID = _int("PROMPT_KING_ROLE_ID")    # given for 24h to the top reply
DIGEST_CHANNEL_ID = _int("DIGEST_CHANNEL_ID")
CHALLENGE_CHANNEL_ID = _int("CHALLENGE_CHANNEL_ID")  # text or forum channel
CHALLENGE_PING_ROLE_ID = _int("CHALLENGE_PING_ROLE_ID")
REMIX_CHAMPION_ROLE_ID = _int("REMIX_CHAMPION_ROLE_ID")  # given for 7 days to the winner

# Schedule (local time in TIMEZONE). Edit here to move a job.
PROMPT_KING_TIME = time(18, 5)     # daily: crown yesterday's top reply
PROMPT_TIME = time(18, 7)          # daily: post Prompt of the Day
DIGEST_TIME = time(10, 13)         # Mondays: weekly digest
REMIX_KICKOFF_TIME = time(16, 11)  # Fridays: Remix Friday kickoff
REMIX_REMINDER_TIME = time(17, 9)  # Sundays: "voting closes soon" reminder
REMIX_CLOSE_TIME = time(18, 0)     # Sundays: voting deadline + winner
DIGEST_HEADLINES_PER_FEED = 3      # newest N per feed (drops to 2 if a section is too long)

# --- Self-roles ---------------------------------------------------------------
# Auto-create a self-role if it doesn't exist yet (needs Manage Roles).
SELF_ROLES_AUTO_CREATE = _bool("SELF_ROLES_AUTO_CREATE", True)

# ============================================================================
# EDIT ME: self-role menus
# Each entry is (label shown in the menu, role name OR role ID, emoji or None).
# Using a role ID (int) is the most robust; a name is matched exactly.
# Max 25 entries per menu (Discord limit).
# ============================================================================
PLATFORM_ROLES = [
    ("PC", "PC", "🖥️"),
    ("PlayStation", "PlayStation", "🎮"),
    ("Xbox", "Xbox", "🟩"),
    ("Nintendo Switch", "Switch", "🍄"),
    ("Mobile", "Mobile", "📱"),
]

GAME_ROLES = [
    ("Valorant", "Valorant", None),
    ("League of Legends", "League", None),
    ("Fortnite", "Fortnite", None),
    ("Minecraft", "Minecraft", None),
    ("Apex Legends", "Apex", None),
    ("Rocket League", "Rocket League", None),
    ("Overwatch 2", "Overwatch", None),
    ("Counter-Strike 2", "CS2", None),
]

# Autocomplete suggestions for /lfg (users can still type anything).
LFG_GAMES = [label for label, _, _ in GAME_ROLES] + [
    "Call of Duty", "Destiny 2", "Sea of Thieves", "Among Us", "Helldivers 2",
    "Marvel Rivals", "Elden Ring", "GTA Online", "Roblox", "Deadlock",
]
LFG_REGIONS = ["NA", "NA East", "NA West", "EU", "EU West", "EU East", "LATAM", "BR", "Asia", "OCE", "ME", "Africa"]
LFG_PLATFORMS = ["PC", "PlayStation", "Xbox", "Switch", "Mobile", "Crossplay"]
