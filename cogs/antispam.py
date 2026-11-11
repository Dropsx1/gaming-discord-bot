"""Rule-based anti-spam (no AI, no API cost).

Catches message floods, copy-paste spam (also across channels), mass mentions,
@everyone/@here from non-mods, invites to other servers, scam/phishing links,
link-only posts from brand-new members, and caps / emoji / zalgo walls.

Escalation per member (strikes expire after 10 minutes):
  1st hit -> message deleted + short warning that deletes itself
  2nd hit -> 10 minute timeout, recent spam (last 2 minutes) bulk-deleted
  3rd hit -> 1 hour timeout, recent spam bulk-deleted
Every action is logged to the mod-log channel (if one is set).

Exempt: bots, webhooks, staff (admins, Manage Server, Manage Messages, staff role),
and any channel/role added with /antispam exempt-channel or /antispam exempt-role.

Settings live in guild_config as plain integers (ANTISPAM_*). It is ON by default.
This cog is independent from the AI auto-moderation in cogs.ai_features.
"""

import logging
import re
import time
import unicodedata
from collections import defaultdict, deque
from datetime import timedelta
from typing import Literal, Optional
from urllib.parse import urlsplit

import discord
from discord import app_commands
from discord.ext import commands

from utils import guildconfig as gc
from utils.helpers import is_staff, send_log, trim

logger = logging.getLogger("bot.antispam")

# ------------------------------------------------------------------ settings
# key suffix -> (default, min, max, description). Stored as ANTISPAM_<KEY>.
SETTINGS: dict[str, tuple[int, int, int, str]] = {
    "ENABLED": (1, 0, 1, "anti-spam on/off"),
    "FLOOD_COUNT": (5, 3, 30, "messages within the flood window"),
    "FLOOD_SECONDS": (5, 2, 60, "flood window (seconds)"),
    "DUP_COUNT": (3, 2, 20, "identical messages within the duplicate window"),
    "DUP_SECONDS": (30, 5, 600, "duplicate window (seconds)"),
    "MENTIONS": (5, 2, 50, "user/role mentions in one message"),
    "INVITES": (1, 0, 1, "block invites to other servers"),
    "SCAMS": (1, 0, 1, "block scam/phishing links"),
    "NEW_LINKS_HOURS": (24, 0, 168, "link-only posts blocked for members newer than this (0 = off)"),
    "CAPS_PERCENT": (75, 0, 100, "caps wall: % uppercase (0 = off)"),
    "CAPS_MIN": (20, 5, 500, "caps wall: minimum letters before it counts"),
    "EMOJIS": (15, 0, 200, "emoji wall: emojis in one message (0 = off)"),
    "ZALGO": (1, 0, 1, "block zalgo / combining-character text"),
    "TIMEOUT1_MIN": (10, 1, 1440, "2nd strike timeout (minutes)"),
    "TIMEOUT2_MIN": (60, 1, 40320, "3rd strike timeout (minutes)"),
}
# Friendly names for /antispam set
SETTING_NAMES = {
    "flood_messages": "FLOOD_COUNT", "flood_seconds": "FLOOD_SECONDS",
    "duplicates": "DUP_COUNT", "duplicate_seconds": "DUP_SECONDS",
    "mentions": "MENTIONS", "invites": "INVITES", "scams": "SCAMS",
    "new_member_link_hours": "NEW_LINKS_HOURS", "caps_percent": "CAPS_PERCENT", "caps_min_letters": "CAPS_MIN",
    "emojis": "EMOJIS", "zalgo": "ZALGO", "timeout_2nd_minutes": "TIMEOUT1_MIN", "timeout_3rd_minutes": "TIMEOUT2_MIN",
}
SettingName = Literal[
    "flood_messages", "flood_seconds", "duplicates", "duplicate_seconds", "mentions", "invites", "scams",
    "new_member_link_hours", "caps_percent", "caps_min_letters", "emojis", "zalgo",
    "timeout_2nd_minutes", "timeout_3rd_minutes",
]
EXEMPT_CH = "ANTISPAM_EXEMPT_CH_"
EXEMPT_ROLE = "ANTISPAM_EXEMPT_ROLE_"

STRIKE_WINDOW = 600      # strikes expire after 10 minutes
STRIKE_GRACE = 5         # seconds: further hits right after a strike delete but don't add strikes
PURGE_WINDOW = 120       # bulk-delete the offender's messages from the last 2 minutes on timeout
HISTORY_KEEP = 150       # seconds of per-user message history kept in memory
WARN_DELETE_AFTER = 8    # seconds the in-channel warning stays up


def setting(guild, key: str) -> int:
    default, lo, hi, _ = SETTINGS[key]
    value = gc.saved(guild).get("ANTISPAM_" + key)
    if value is None:
        return default
    return max(lo, min(hi, value))


# ------------------------------------------------------------------ detection
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>]+", re.I)
BARE_DOMAIN_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:gg|com|net|org|io|xyz|ru|ly|me|link|site|online|shop|"
                            r"store|gift|click|top|info|pro|app|co|tk|ml|ga|cf|gq|us|cc|su|icu|fun|live|life)"
                            r")(?:/[^\s<>]*)?", re.I)
INVITE_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:discord(?:app)?\.com/invite|discord\.gg|dsc\.gg|"
                       r"discord\.me|discord\.io|invite\.gg)/([a-z0-9-]+)", re.I)
SCAM_TEXT_RE = re.compile(
    r"(free\s*-?\s*nitro|nitro\s*(?:for\s*free|gift|giveaway|drop)|discord\s*nitro\s*(?:free|gift)|"
    r"steam\s*(?:gift|card|giveaway|wallet)|free\s*(?:steam|skins?|robux|v-?bucks|cs2?\s*skins)|"
    r"air\s*-?\s*drop|claim\s+(?:your|ur|the)\s+(?:prize|reward|gift|nitro)|"
    r"(?:i'?m|im)\s+leaving\s+cs|first\s+\d+\s+(?:people|users)|gift\s+for\s+you|who\s+is\s+first\?|"
    r"(?:\$|usd)\s?\d+\s*(?:gift|giveaway)|crypto\s*giveaway|double\s+your\s+(?:btc|eth|crypto))",
    re.I,
)
# Real domains for brands scammers imitate. Lookalikes of these are treated as phishing.
OFFICIAL = {
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.media", "discord.gift",
    "discord.new", "discordstatus.com", "discord.dev", "discord.co",
    "steamcommunity.com", "steampowered.com", "steamstatic.com", "steamdeck.com", "steamgames.com",
    "steamusercontent.com", "steamchina.com",
}
BRANDS = ("discord", "steamcommunity", "steampowered")
BAIT_WORDS = re.compile(r"(gift|nitro|free|promo|claim|airdrop|reward|giveaway|login|verify|auth|trade|skins?)", re.I)
KNOWN_BAD = re.compile(r"(dlscord|disc0rd|d1scord|discorcl|dicsord|discrod|disocrd|discord-?nitro|"
                       r"discord-?gift|discordgift|nitro-?gift|steamcommunlty|steamcomrnunity|stearncommunity|"
                       r"steamcommunity-|steancommunity|stemcommunity|steampowered-|"
                       r"grabify|iplogger|2no\.co|blasze)", re.I)
_LEET = str.maketrans({"0": "o", "1": "i", "l": "i", "3": "e", "4": "a", "5": "s", "!": "i", "|": "i", "$": "s",
                       "@": "a"})
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w{1,32}:\d{15,25}>")
EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F\U0001F900-\U0001F9FF\U0001FA00-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
)


def _domain(url: str) -> str:
    if not re.match(r"https?://", url, re.I):
        url = "http://" + url
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _registrable(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def domains(content: str) -> list[str]:
    found = [_domain(u) for u in URL_RE.findall(content)]
    found += [m.group(1).lower() for m in BARE_DOMAIN_RE.finditer(content)]
    return [d for d in dict.fromkeys(found) if d]


def is_phishing_domain(host: str) -> bool:
    if not host:
        return False
    reg = _registrable(host)
    if reg in OFFICIAL or host in OFFICIAL:
        return False
    if KNOWN_BAD.search(host):
        return True
    squashed = host.replace("-", "").replace(".", "")
    normal = squashed.replace("rn", "m").translate(_LEET)
    for brand in BRANDS:
        if brand in normal and brand not in squashed:
            return True  # lookalike, e.g. disc0rd / steamcornmunity
        if brand in squashed and BAIT_WORDS.search(squashed.replace(brand, " ")):
            return True  # brand + bait, e.g. discord-gift-claim.xyz
    return False


def invite_codes(content: str) -> list[str]:
    return [m.group(1) for m in INVITE_RE.finditer(content)]


def is_link_only(content: str) -> bool:
    text = content.strip()
    if not text or not (URL_RE.search(text) or INVITE_RE.search(text)):
        return False
    rest = INVITE_RE.sub(" ", URL_RE.sub(" ", text))
    rest = re.sub(r"<@[!&]?\d+>|@everyone|@here", " ", rest)
    return len(re.sub(r"[\W_]+", "", rest)) <= 3


def scam_reason(content: str) -> Optional[str]:
    hosts = domains(content)
    for host in hosts:
        if is_phishing_domain(host):
            return f"phishing link ({host})"
    has_link = bool(hosts) or bool(INVITE_RE.search(content))
    if has_link and SCAM_TEXT_RE.search(content):
        return "scam message with link"
    if has_link and re.search(r"@everyone|@here", content):
        return "@everyone/@here with link"
    return None


def caps_wall(content: str, percent: int, min_letters: int) -> bool:
    if not percent:
        return False
    text = URL_RE.sub(" ", CUSTOM_EMOJI_RE.sub(" ", re.sub(r"<[@#][!&]?\d+>", " ", content)))
    letters = [c for c in text if c.isalpha()]
    if len(letters) < min_letters:
        return False
    return sum(c.isupper() for c in letters) * 100 >= percent * len(letters)


def emoji_count(content: str) -> int:
    return len(CUSTOM_EMOJI_RE.findall(content)) + len(EMOJI_RE.findall(content))


def zalgo(content: str) -> bool:
    marks = sum(1 for c in content if unicodedata.category(c) == "Mn")
    if marks < 8:
        return False
    base = sum(1 for c in content if not c.isspace() and unicodedata.category(c) != "Mn") or 1
    return marks / base >= 0.5 or bool(re.search(r"[\u0300-\u036f\u0483-\u0489\u1dc0-\u1dff\u20d0-\u20ff]{4,}",
                                                 content))


def normalize(content: str) -> str:
    return re.sub(r"\s+", " ", content.strip().lower())


def content_reason(guild, content: str, mentions: int, everyone: bool, joined_age_s: Optional[float]) -> Optional[str]:
    """Rules that look at one message (no history). Invites are handled separately (needs a lookup)."""
    if everyone:
        return "@everyone/@here ping"
    limit = setting(guild, "MENTIONS")
    if mentions >= limit:
        return f"mass mentions ({mentions})"
    if setting(guild, "SCAMS"):
        reason = scam_reason(content)
        if reason:
            return reason
    hours = setting(guild, "NEW_LINKS_HOURS")
    if hours and joined_age_s is not None and joined_age_s < hours * 3600 and is_link_only(content):
        return f"link-only post from a new member (joined <{hours}h ago)"
    if setting(guild, "ZALGO") and zalgo(content):
        return "zalgo text"
    emojis = setting(guild, "EMOJIS")
    if emojis and emoji_count(content) >= emojis:
        return "emoji wall"
    if caps_wall(content, setting(guild, "CAPS_PERCENT"), setting(guild, "CAPS_MIN")):
        return "caps wall"
    return None


# ------------------------------------------------------------------ state
class Tracker:
    """Per-member recent messages and strikes (in memory; resets on restart)."""

    def __init__(self):
        self.recent: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=60))
        self.strikes: dict[tuple[int, int], list[float]] = defaultdict(list)
        self.last_strike: dict[tuple[int, int], float] = {}
        self.calls = 0

    def add(self, key, now: float, message, norm: str) -> deque:
        q = self.recent[key]
        while q and now - q[0][0] > HISTORY_KEEP:
            q.popleft()
        q.append((now, message, norm))
        self.calls += 1
        if self.calls % 500 == 0:
            self.prune(now)
        return q

    def prune(self, now: float) -> None:
        for key in [k for k, q in self.recent.items() if not q or now - q[-1][0] > HISTORY_KEEP]:
            del self.recent[key]
        for key in [k for k, s in self.strikes.items() if not s or now - s[-1] > STRIKE_WINDOW]:
            self.strikes.pop(key, None)
            self.last_strike.pop(key, None)

    def strike(self, key, now: float) -> tuple[int, bool]:
        """Record a strike. Returns (strike count in window, counted). Hits inside the grace period don't count."""
        s = [t for t in self.strikes[key] if now - t <= STRIKE_WINDOW]
        if key in self.last_strike and now - self.last_strike[key] < STRIKE_GRACE and s:
            self.strikes[key] = s
            return len(s), False
        s.append(now)
        self.strikes[key] = s
        self.last_strike[key] = now
        return len(s), True


def flood_hits(q: deque, now: float, count: int, seconds: int) -> list:
    window = [m for t, m, _ in q if now - t <= seconds]
    return window if len(window) >= count else []


def dup_hits(q: deque, now: float, norm: str, count: int, seconds: int) -> list:
    if not norm or len(norm) < 2:
        return []
    window = [m for t, m, n in q if n == norm and now - t <= seconds]
    return window if len(window) >= count else []


# ------------------------------------------------------------------ cog
class AntiSpam(commands.Cog):
    """Rule-based anti-spam: deletes spam and times out repeat offenders."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tracker = Tracker()
        self.invite_cache: dict[str, tuple[Optional[int], float]] = {}
        self.deleted: deque = deque(maxlen=2000)  # ids of messages already removed

    # ---- exemptions
    def exempt(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or message.webhook_id or getattr(message.author, "bot", False):
            return True
        if not setting(guild, "ENABLED"):
            return True
        author = message.author
        if not isinstance(author, discord.Member):
            return True
        perms = author.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_messages or is_staff(author):
            return True
        saved = gc.saved(guild)
        channel = message.channel
        ids = {getattr(channel, "id", 0), getattr(channel, "parent_id", 0) or 0,
               getattr(channel, "category_id", 0) or 0}
        if any(saved.get(f"{EXEMPT_CH}{i}") for i in ids if i):
            return True
        if any(saved.get(f"{EXEMPT_ROLE}{r.id}") for r in getattr(author, "roles", [])):
            return True
        return False

    async def foreign_invite(self, guild: discord.Guild, codes: list[str]) -> Optional[str]:
        """First invite code that points to another server (or can't be resolved)."""
        vanity = (getattr(guild, "vanity_url_code", None) or "").lower()
        now = time.monotonic()
        for code in codes:
            if vanity and code.lower() == vanity:
                continue
            cached = self.invite_cache.get(code)
            if cached and now - cached[1] < 3600:
                gid = cached[0]
            else:
                gid = None
                try:
                    inv = await self.bot.fetch_invite(code, with_counts=False)
                    gid = inv.guild.id if inv.guild else None
                except (discord.HTTPException, AttributeError):
                    gid = None
                self.invite_cache[code] = (gid, now)
            if gid != guild.id:
                return code
        return None

    # ---- listeners
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if self.exempt(message):
            return
        await self.check(message, new=True)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content == after.content or self.exempt(after):
            return
        await self.check(after, new=False)

    async def check(self, message: discord.Message, new: bool) -> Optional[str]:
        guild, author = message.guild, message.author
        content = message.content or ""
        key = (guild.id, author.id)
        now = time.monotonic()
        extra: list = []
        reason = None

        if new:
            norm = normalize(content) or ("att:" + ",".join(sorted(f"{a.filename}:{a.size}" for a in message.attachments))
                                          if getattr(message, "attachments", None) else "")
            q = self.tracker.add(key, now, message, norm)
            flood = flood_hits(q, now, setting(guild, "FLOOD_COUNT"), setting(guild, "FLOOD_SECONDS"))
            dups = dup_hits(q, now, norm, setting(guild, "DUP_COUNT"), setting(guild, "DUP_SECONDS"))
            if dups:
                reason, extra = f"duplicate messages ({len(dups)}x)", dups
            elif flood:
                reason, extra = f"message flood ({len(flood)} in {setting(guild, 'FLOOD_SECONDS')}s)", flood

        if reason is None:
            mentions = len(set(getattr(message, "raw_mentions", []) or [])) + \
                len(set(getattr(message, "raw_role_mentions", []) or []))
            everyone = bool(getattr(message, "mention_everyone", False)) or bool(
                re.search(r"@(everyone|here)\b", content))
            joined = getattr(author, "joined_at", None)
            age = (discord.utils.utcnow() - joined).total_seconds() if joined else None
            reason = content_reason(guild, content, mentions, everyone, age)

        if reason is None and setting(guild, "INVITES"):
            codes = invite_codes(content)
            if codes:
                code = await self.foreign_invite(guild, codes)
                if code:
                    reason = f"invite to another server ({code})"

        if reason is None:
            return None
        await self.punish(message, reason, extra)
        return reason

    # ---- actions
    async def _delete(self, msgs) -> int:
        """Delete messages, bulk per channel when possible. Returns how many were removed."""
        by_channel: dict[int, tuple] = {}
        for m in msgs:
            if m.id in self.deleted:
                continue
            ch = m.channel
            by_channel.setdefault(ch.id, (ch, []))[1].append(m)
        count = 0
        for ch, items in by_channel.values():
            if len(items) > 1 and hasattr(ch, "delete_messages"):
                try:
                    for i in range(0, len(items), 100):
                        await ch.delete_messages(items[i:i + 100], reason="Anti-spam")
                    self.deleted.extend(m.id for m in items)
                    count += len(items)
                    continue
                except (discord.HTTPException, AttributeError, TypeError):
                    pass  # fall back to one by one (e.g. messages >14 days, or missing perms)
            for m in items:
                try:
                    await m.delete()
                    self.deleted.append(m.id)
                    count += 1
                except discord.NotFound:
                    self.deleted.append(m.id)
                except (discord.HTTPException, AttributeError):
                    pass
        return count

    async def punish(self, message: discord.Message, reason: str, extra: list) -> None:
        guild, author, channel = message.guild, message.author, message.channel
        key = (guild.id, author.id)
        now = time.monotonic()
        strikes, counted = self.tracker.strike(key, now)

        targets = [message] + [m for m in extra if m.id != message.id]
        action = "deleted"
        timeout: Optional[timedelta] = None
        if counted and strikes >= 2:
            minutes = setting(guild, "TIMEOUT2_MIN" if strikes >= 3 else "TIMEOUT1_MIN")
            timeout = timedelta(minutes=minutes)
            q = self.tracker.recent.get(key, ())
            targets += [m for t, m, _ in q if now - t <= PURGE_WINDOW and m.id not in {x.id for x in targets}]

        deleted = await self._delete(targets)
        if message.id not in self.deleted:
            action = "could NOT delete (missing Manage Messages?)"

        timeout_note = ""
        if timeout is not None:
            me = guild.me
            can = getattr(me.guild_permissions, "moderate_members", False)
            top_ok = True
            try:
                top_ok = author.top_role < me.top_role
            except (AttributeError, TypeError):
                pass
            if not can:
                timeout_note = "timeout FAILED: I need Moderate Members"
            elif not top_ok:
                timeout_note = "timeout FAILED: their top role is above mine"
            else:
                try:
                    await author.timeout(timeout, reason=f"Anti-spam: {reason} (strike {strikes})")
                    timeout_note = f"timed out {_human(timeout)}"
                except discord.HTTPException as e:
                    timeout_note = f"timeout FAILED ({e.__class__.__name__})"

        if counted:
            if timeout is None:
                text = f"⚠️ {author.mention}, your message was removed by anti-spam ({reason}). Repeat and you'll be timed out."
            elif timeout_note.startswith("timed out"):
                text = f"🔇 {author.mention} was {timeout_note} for spam ({reason})."
            else:
                text = None
            if text:
                try:
                    await channel.send(text, delete_after=WARN_DELETE_AFTER,
                                       allowed_mentions=discord.AllowedMentions(users=[author], everyone=False,
                                                                                roles=False))
                except (discord.HTTPException, AttributeError, TypeError):
                    pass

        logger.info("Anti-spam in %s: %s (%s) strike %d%s: %s; %s %s", guild, author, author.id, strikes,
                    "" if counted else " (grace)", reason, action, timeout_note)
        if not counted:
            return  # don't flood the mod log during one burst
        embed = discord.Embed(
            title="🧹 Anti-spam" + (" • Timeout" if timeout else ""),
            color=discord.Color.red() if timeout else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Member", value=f"{author.mention} (`{author}` • {author.id})", inline=False)
        embed.add_field(name="Channel", value=getattr(channel, "mention", str(channel)), inline=True)
        embed.add_field(name="Rule", value=trim(reason, 1024), inline=True)
        embed.add_field(name="Strike", value=f"{strikes} (in 10 min)", inline=True)
        embed.add_field(name="Action", value=f"{action}, {deleted} message(s)" +
                        (f"; {timeout_note}" if timeout_note else ""), inline=False)
        embed.add_field(name="Message", value=trim(message.content or "(no text)", 1024), inline=False)
        await send_log(guild, gc.get(guild, "MOD_LOG_CHANNEL_ID"), embed)

    # ---- admin commands
    @commands.hybrid_group(name="antispam", description="Rule-based anti-spam (admin)", fallback="status")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def antispam(self, ctx: commands.Context):
        g = ctx.guild
        on = "on ✅" if setting(g, "ENABLED") else "off ⛔"
        onoff = lambda k: "on" if setting(g, k) else "off"  # noqa: E731
        caps = f"{setting(g, 'CAPS_PERCENT')}% of {setting(g, 'CAPS_MIN')}+ letters" if setting(g, "CAPS_PERCENT") else "off"
        new_links = f"members newer than {setting(g, 'NEW_LINKS_HOURS')}h" if setting(g, "NEW_LINKS_HOURS") else "off"
        lines = [
            f"**Anti-spam is {on}**",
            f"• Flood: {setting(g, 'FLOOD_COUNT')} messages in {setting(g, 'FLOOD_SECONDS')}s",
            f"• Duplicates: same message {setting(g, 'DUP_COUNT')}x in {setting(g, 'DUP_SECONDS')}s (any channel)",
            f"• Mass mentions: {setting(g, 'MENTIONS')}+ user/role mentions; any @everyone/@here",
            f"• Other-server invites: {onoff('INVITES')} · Scam/phishing links: {onoff('SCAMS')}",
            f"• Link-only posts blocked for: {new_links}",
            f"• Caps wall: {caps} · Emoji wall: {setting(g, 'EMOJIS') or 'off'} · Zalgo: {onoff('ZALGO')}",
            f"• Strikes (10 min): 1st delete + warning · 2nd {setting(g, 'TIMEOUT1_MIN')}m timeout · "
            f"3rd+ {setting(g, 'TIMEOUT2_MIN')}m timeout (recent spam bulk-deleted)",
            "• Exempt: bots, admins, Manage Server/Messages, staff role",
        ]
        saved = gc.saved(g)
        chans = [f"<#{k[len(EXEMPT_CH):]}>" for k, v in saved.items() if k.startswith(EXEMPT_CH) and v]
        roles = [f"<@&{k[len(EXEMPT_ROLE):]}>" for k, v in saved.items() if k.startswith(EXEMPT_ROLE) and v]
        if chans:
            lines.append("• Exempt channels: " + ", ".join(chans))
        if roles:
            lines.append("• Exempt roles: " + ", ".join(roles))
        lines += self.perm_warnings(g)
        await ctx.send(trim("\n".join(lines), 1900), ephemeral=True,
                       allowed_mentions=discord.AllowedMentions.none())

    @staticmethod
    def perm_warnings(guild: discord.Guild) -> list[str]:
        out = []
        me = guild.me
        perms = getattr(me, "guild_permissions", None)
        if perms is not None:
            if not perms.manage_messages:
                out.append("⚠️ I need **Manage Messages** to delete spam.")
            if not getattr(perms, "moderate_members", False):
                out.append("⚠️ I need **Moderate Members** to time out spammers.")
        if gc.channel(guild, "MOD_LOG_CHANNEL_ID") is None:
            out.append("ℹ️ No mod-log channel set, so actions aren't logged. Run `/setup` or `/setup-link`.")
        return out

    @antispam.command(name="on", description="Turn anti-spam on (admin)")
    @commands.has_permissions(administrator=True)
    async def antispam_on(self, ctx: commands.Context):
        await gc.set_many(ctx.guild, {"ANTISPAM_ENABLED": 1})
        logger.info("%s turned anti-spam on in %s", ctx.author, ctx.guild)
        await ctx.send("\n".join(["🧹 Anti-spam is **on**."] + self.perm_warnings(ctx.guild)), ephemeral=True)

    @antispam.command(name="off", description="Turn anti-spam off (admin)")
    @commands.has_permissions(administrator=True)
    async def antispam_off(self, ctx: commands.Context):
        await gc.set_many(ctx.guild, {"ANTISPAM_ENABLED": 0})
        logger.info("%s turned anti-spam off in %s", ctx.author, ctx.guild)
        await ctx.send("🧹 Anti-spam is **off**.", ephemeral=True)

    @antispam.command(name="set", description="Change an anti-spam threshold (admin)")
    @app_commands.describe(setting_name="Which threshold", value="New value (0 turns caps/emoji/new-member/on-off rules off)")
    @commands.has_permissions(administrator=True)
    async def antispam_set(self, ctx: commands.Context, setting_name: SettingName, value: int):
        key = SETTING_NAMES[setting_name]
        _, lo, hi, desc = SETTINGS[key]
        if key in ("CAPS_PERCENT", "EMOJIS", "NEW_LINKS_HOURS") and value == 0:
            lo = 0
        if not lo <= value <= hi:
            await ctx.send(f"❓ `{setting_name}` must be between {lo} and {hi}.", ephemeral=True)
            return
        await gc.set_many(ctx.guild, {"ANTISPAM_" + key: value})
        logger.info("%s set anti-spam %s=%s in %s", ctx.author, key, value, ctx.guild)
        await ctx.send(f"🧹 `{setting_name}` ({desc}) is now **{value}**.", ephemeral=True)

    @antispam.command(name="reset", description="Restore default thresholds (admin)")
    @commands.has_permissions(administrator=True)
    async def antispam_reset(self, ctx: commands.Context):
        keys = ["ANTISPAM_" + k for k in SETTINGS if k != "ENABLED"]
        await gc.delete_keys(ctx.guild, [k for k in keys if k in gc.saved(ctx.guild)])
        await ctx.send("🧹 Anti-spam thresholds are back to the defaults.", ephemeral=True)

    @antispam.command(name="exempt-channel", description="Toggle a channel (or category) anti-spam ignores (admin)")
    @app_commands.describe(channel="Channel or category to exempt / un-exempt")
    @commands.has_permissions(administrator=True)
    async def antispam_exempt_channel(self, ctx: commands.Context,
                                      channel: discord.abc.GuildChannel):
        key = f"{EXEMPT_CH}{channel.id}"
        if gc.saved(ctx.guild).get(key):
            await gc.delete_keys(ctx.guild, [key])
            await ctx.send(f"🧹 {channel.mention} is checked by anti-spam again.", ephemeral=True)
        else:
            await gc.set_many(ctx.guild, {key: 1})
            await ctx.send(f"🧹 Anti-spam now ignores {channel.mention}.", ephemeral=True)

    @antispam.command(name="exempt-role", description="Toggle a role anti-spam ignores (admin)")
    @app_commands.describe(role="Role to exempt / un-exempt")
    @commands.has_permissions(administrator=True)
    async def antispam_exempt_role(self, ctx: commands.Context, role: discord.Role):
        key = f"{EXEMPT_ROLE}{role.id}"
        if gc.saved(ctx.guild).get(key):
            await gc.delete_keys(ctx.guild, [key])
            await ctx.send(f"🧹 {role.mention} is checked by anti-spam again.", ephemeral=True,
                           allowed_mentions=discord.AllowedMentions.none())
        else:
            await gc.set_many(ctx.guild, {key: 1})
            await ctx.send(f"🧹 Anti-spam now ignores members with {role.mention}.", ephemeral=True,
                           allowed_mentions=discord.AllowedMentions.none())

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            me = guild.me
            if me is None:
                continue
            p = me.guild_permissions
            missing = [n for n, ok in (("Manage Messages", p.manage_messages),
                                       ("Moderate Members", p.moderate_members)) if not ok]
            pos = me.top_role.position
            above = sum(1 for r in guild.roles if r.position > pos and not r.managed)
            logger.info("Anti-spam %s in %s: missing perms: %s; bot top role '%s' pos %d/%d (%d non-bot roles above)",
                        "on" if setting(guild, "ENABLED") else "off", guild.name,
                        ", ".join(missing) or "none", me.top_role.name, pos, len(guild.roles) - 1, above)


def _human(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes % 60 == 0 and minutes >= 60:
        h = minutes // 60
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{minutes} minute" + ("s" if minutes != 1 else "")


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiSpam(bot))
