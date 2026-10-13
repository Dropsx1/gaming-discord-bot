"""/setup: configure a server automatically.

Creates (or reuses, matching by saved ID first and then by name) the roles,
categories and channels the bot uses, and saves every ID to guild_config so
no env vars or ID copying are needed. Safe to run again: existing items are
reused and never modified; only items it creates get permissions set.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import guildconfig as gc
from utils.helpers import trim
from utils.names import normalize_name

logger = logging.getLogger("bot.setup")


@dataclass
class RoleSpec:
    name: str
    key: str
    color: discord.Color = field(default_factory=discord.Color.default)
    mentionable: bool = False


@dataclass
class ChannelSpec:
    name: str
    key: str
    voice: bool = False
    readonly: bool = False      # @everyone can read but not post (bot posts)
    thread_only: bool = False   # @everyone replies in threads only (prompt / challenge)


@dataclass
class CategorySpec:
    name: str
    key: str
    channels: list[ChannelSpec]
    private: bool = False       # hidden from @everyone; visible to Staff + bot


ROLES = [
    RoleSpec("Staff", "STAFF_ROLE_ID", discord.Color.blue()),
    RoleSpec("Member", "MEMBER_ROLE_ID"),
    RoleSpec("Prompt King", "PROMPT_KING_ROLE_ID", discord.Color.gold()),
    RoleSpec("Remix Champion", "REMIX_CHAMPION_ROLE_ID", discord.Color.magenta()),
    RoleSpec("LFG", "LFG_ROLE_ID", discord.Color.green(), mentionable=True),
]

LAYOUT = [
    CategorySpec("Server", "SERVER_CATEGORY_ID", [
        ChannelSpec("welcome", "WELCOME_CHANNEL_ID", readonly=True),
        ChannelSpec("rules", "START_HERE_CHANNEL_ID", readonly=True),
        ChannelSpec("announcements", "EVENT_CHANNEL_ID", readonly=True),
        ChannelSpec("self-roles", "SELF_ROLES_CHANNEL_ID", readonly=True),
    ]),
    CategorySpec("Community", "COMMUNITY_CATEGORY_ID", [
        ChannelSpec("general-chat", "GENERAL_CHAT_CHANNEL_ID"),
        ChannelSpec("looking-for-group", "LFG_CHANNEL_ID"),
        ChannelSpec("giveaways", "GIVEAWAYS_CHANNEL_ID"),
        ChannelSpec("clips-and-media", "CLIPS_CHANNEL_ID"),
        ChannelSpec("bot-commands", "BOT_COMMANDS_CHANNEL_ID"),
    ]),
    CategorySpec("AI & Create Lab", "LAB_CATEGORY_ID", [
        ChannelSpec("prompt-of-the-day", "PROMPT_CHANNEL_ID", thread_only=True),
        ChannelSpec("tech-digest", "DIGEST_CHANNEL_ID", readonly=True),
        ChannelSpec("weekly-challenge", "CHALLENGE_CHANNEL_ID", thread_only=True),
        ChannelSpec("ai-art", "AI_ART_CHANNEL_ID"),
        ChannelSpec("tech-talk", "TECH_TALK_CHANNEL_ID"),
    ]),
    CategorySpec("Staff", "STAFF_CATEGORY_ID", [ChannelSpec("mod-log", "LOG_CHANNEL_ID")], private=True),
    CategorySpec("Tickets", "TICKET_CATEGORY_ID", [], private=True),
    CategorySpec("Voice", "VOICE_CATEGORY_ID", [ChannelSpec("➕ Join to Create", "JOIN_TO_CREATE_CHANNEL_ID", voice=True)]),
]

# Auto-resume after a rate limit: extra seconds to wait, longest wait we'll do, and attempts.
RESUME_PADDING = 5
RESUME_MAX_WAIT = 6 * 3600  # Discord has been seen asking for 3+ hours on new servers
# A pending automatic finish is saved here (internal keys), so it survives bot restarts/redeploys.
RESUME_KEYS = ("_RESUME_AT", "_RESUME_USER", "_RESUME_CHANNEL")
RESUME_MAX_ROUNDS = 5

# Prompt of the Day and Remix Friday also work in forum channels.
FORUM_OK = {"PROMPT_CHANNEL_ID", "CHALLENGE_CHANNEL_ID"}


def _norm(name: str) -> str:
    return normalize_name(name)


def _reason(error: discord.HTTPException) -> str:
    """Short, readable reason for a failed Discord call."""
    text = (getattr(error, "text", "") or "").strip()
    if getattr(error, "status", None) == 403:
        return "I'm missing permission for this" + (f" ({text})" if text else "")
    return f"{error.__class__.__name__}" + (f": {text}" if text else "")


class Report:
    def __init__(self):
        self.created: list[str] = []
        self.reused: list[str] = []
        self.warnings: list[str] = []
        self.failed: list[str] = []
        self.waiting: list[str] = []   # not created yet because Discord is rate-limiting us
        self.retry_at: float = 0.0     # unix time when Discord allows creating again
        self.ids: dict[str, int] = {}


class SetupRunner:
    """One /setup run for one server."""

    def __init__(self, guild: discord.Guild, dry_run: bool):
        self.guild = guild
        self.dry_run = dry_run
        self.me = guild.me
        self.report = Report()
        self.reason = "Bot /setup"
        self.waiting_keys: set[str] = set()  # roles/categories postponed by a rate limit
        self.matched: dict[str, object] = {}  # key -> existing role/category/channel found by match_existing()
        self._claimed: set[int] = set()       # IDs already matched, so one item never serves two keys

    # ---------------------------------------------------------- rate limits
    def _limited(self) -> bool:
        return self.report.retry_at > time.time()

    def _postpone(self, key: str, label: str, error: Optional[discord.RateLimited] = None) -> None:
        """Discord is rate-limiting creation: stop trying for now and mark the item as waiting."""
        if error is not None:
            self.report.retry_at = max(self.report.retry_at, time.time() + error.retry_after)
            logger.warning("Rate-limited during /setup in %s for %.0fs", self.guild, error.retry_after)
        self.waiting_keys.add(key)
        self.report.waiting.append(label)

    # --------------------------------------------------------------- lookup
    def _saved(self, key: str) -> int:
        return gc.get(self.guild, key)

    def _best(self, candidates: list, spec_name: str, category=None):
        """Pick among same-name matches: exact name first, then already in the target category, then top-most."""
        candidates = [c for c in candidates if c.id not in self._claimed]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (
            0 if c.name.casefold() == spec_name.casefold() else 1,
            0 if category is not None and getattr(c, "category_id", None) == category.id else 1,
            getattr(c, "position", 0),
        ))
        return candidates[0]

    def find_role(self, spec: RoleSpec) -> Optional[discord.Role]:
        role = self.guild.get_role(self._saved(spec.key)) if self._saved(spec.key) else None
        if role is not None and not role.is_default() and role.id not in self._claimed:
            return role
        target = _norm(spec.name)
        matches = [r for r in self.guild.roles
                   if not r.is_default() and not getattr(r, "managed", False) and _norm(r.name) == target]
        return self._best(matches, spec.name)

    def find_category(self, spec: CategorySpec) -> Optional[discord.CategoryChannel]:
        ch = self.guild.get_channel(self._saved(spec.key)) if self._saved(spec.key) else None
        if isinstance(ch, discord.CategoryChannel) and ch.id not in self._claimed:
            return ch
        target = _norm(spec.name)
        return self._best([c for c in self.guild.categories if _norm(c.name) == target], spec.name)

    def _type_ok(self, ch, spec: ChannelSpec) -> bool:
        if spec.voice:
            return isinstance(ch, discord.VoiceChannel)
        if spec.key in FORUM_OK and isinstance(ch, discord.ForumChannel):
            return True
        return isinstance(ch, discord.TextChannel)

    def find_channel(self, spec: ChannelSpec, category: Optional[discord.CategoryChannel]):
        """Saved ID first, then a same-name channel anywhere in the server (any category)."""
        ch = self.guild.get_channel(self._saved(spec.key)) if self._saved(spec.key) else None
        if ch is not None and self._type_ok(ch, spec) and ch.id not in self._claimed:
            return ch
        target = _norm(spec.name)
        matches = [c for c in self.guild.channels if self._type_ok(c, spec) and _norm(c.name) == target]
        return self._best(matches, spec.name, category)

    def _reuse(self, key: str, item, label: str) -> None:
        self.matched[key] = item
        self._claimed.add(item.id)
        self.report.reused.append(label)
        self.report.ids[key] = item.id

    def match_existing(self) -> None:
        """Find everything that already exists, before any create call (so rate limits never block reuse)."""
        for spec in ROLES:
            role = self.find_role(spec)
            if role is not None:
                self._reuse(spec.key, role, f"@{role.name}")
                if role >= self.me.top_role:
                    self.report.warnings.append(
                        f"@{role.name} is above my top role, so I can't give it out. Drag my role above it."
                    )
        staff = self.matched.get("STAFF_ROLE_ID")
        for spec in LAYOUT:
            cat = self.find_category(spec)
            if cat is None:
                continue
            self._reuse(spec.key, cat, f"📁 {cat.name}")
            if spec.private:
                if cat.permissions_for(self.guild.default_role).view_channel:
                    self.report.warnings.append(f"📁 {cat.name} is visible to @everyone. Hide it in its permissions.")
                if not cat.permissions_for(self.me).view_channel:
                    self.report.warnings.append(f"📁 {cat.name} is hidden from me. Allow my role to View Channel.")
                if staff is not None and not cat.permissions_for(staff).view_channel:
                    self.report.warnings.append(f"📁 {cat.name} is hidden from @{staff.name}. Allow it View Channel.")
        for cat_spec in LAYOUT:
            for spec in cat_spec.channels:
                ch = self.find_channel(spec, self.matched.get(cat_spec.key))
                if ch is not None:
                    self._reuse(spec.key, ch, f"🔊 {ch.name}" if spec.voice else ch.mention)

    # ----------------------------------------------------------- overwrites
    def private_overwrites(self, staff: Optional[discord.Role]) -> dict:
        ow = {
            self.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            self.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, manage_channels=True,
                manage_permissions=True, embed_links=True, attach_files=True,
            ),
        }
        if staff is not None:
            ow[staff] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        return ow

    def channel_overwrites(self, spec: ChannelSpec, category: Optional[discord.CategoryChannel]) -> Optional[dict]:
        if not (spec.readonly or spec.thread_only):
            return None  # inherit the category's permissions
        ow = dict(category.overwrites) if category is not None else {}
        everyone = ow.get(self.guild.default_role, discord.PermissionOverwrite())
        everyone.send_messages = False
        if spec.thread_only:
            everyone.send_messages_in_threads = True
        ow[self.guild.default_role] = everyone
        bot_ow = ow.get(self.me, discord.PermissionOverwrite())
        bot_ow.update(view_channel=True, send_messages=True, embed_links=True, create_public_threads=True,
                      send_messages_in_threads=True, manage_threads=True)
        ow[self.me] = bot_ow
        return ow

    # -------------------------------------------------------------- steps
    async def ensure_role(self, spec: RoleSpec) -> Optional[discord.Role]:
        if spec.key in self.matched:
            return self.matched[spec.key]
        if self.dry_run:
            self.report.created.append(f"@{spec.name}")
            return None
        if self._limited():
            self._postpone(spec.key, f"@{spec.name}")
            return None
        try:
            role = await self.guild.create_role(
                name=spec.name, colour=spec.color, mentionable=spec.mentionable, reason=self.reason
            )  # new roles start at the bottom, i.e. below the bot's top role
        except discord.RateLimited as e:
            self._postpone(spec.key, f"@{spec.name}", e)
            return None
        except discord.HTTPException as e:
            self.report.failed.append(f"@{spec.name}: {_reason(e)}")
            return None
        self.report.created.append(f"@{role.name}")
        self.report.ids[spec.key] = role.id
        return role

    async def ensure_category(self, spec: CategorySpec, staff: Optional[discord.Role]):
        if spec.key in self.matched:
            return self.matched[spec.key]
        if self.dry_run:
            self.report.created.append(f"📁 {spec.name}")
            return None
        # A hidden category must include Staff; if the Staff role is still waiting, wait with it.
        if self._limited() or (spec.private and staff is None and "STAFF_ROLE_ID" in self.waiting_keys):
            self._postpone(spec.key, f"📁 {spec.name}")
            return None
        try:
            cat = await self.guild.create_category(
                spec.name,
                overwrites=self.private_overwrites(staff) if spec.private else {},
                reason=self.reason,
            )
        except discord.RateLimited as e:
            self._postpone(spec.key, f"📁 {spec.name}", e)
            return None
        except discord.HTTPException as e:
            self.report.failed.append(f"📁 {spec.name}: {_reason(e)}")
            return None
        self.report.created.append(f"📁 {cat.name}")
        self.report.ids[spec.key] = cat.id
        return cat

    async def ensure_channel(self, spec: ChannelSpec, category, cat_spec: CategorySpec):
        label = f"🔊 {spec.name}" if spec.voice else f"#{spec.name}"
        if spec.key in self.matched:
            return self.matched[spec.key]
        if self.dry_run:
            self.report.created.append(f"{label} (in {cat_spec.name})")
            return None
        if category is None:
            if cat_spec.key in self.waiting_keys:
                self._postpone(spec.key, f"{label} (in {cat_spec.name})")  # created once its category exists
            else:
                self.report.failed.append(f"{label}: its category {cat_spec.name} couldn't be created")
            return None
        label = f"{label} (in {cat_spec.name})"
        if self._limited():
            self._postpone(spec.key, label)
            return None
        try:
            if spec.voice:
                ch = await self.guild.create_voice_channel(spec.name, category=category, reason=self.reason)
            else:
                ow = self.channel_overwrites(spec, category)
                kwargs = {"overwrites": ow} if ow is not None else {}
                ch = await self.guild.create_text_channel(spec.name, category=category, reason=self.reason, **kwargs)
        except discord.RateLimited as e:
            self._postpone(spec.key, label, e)
            return None
        except discord.HTTPException as e:
            self.report.failed.append(f"{label}: {_reason(e)}")
            return None
        self.report.created.append(f"🔊 {ch.name}" if spec.voice else ch.mention)
        self.report.ids[spec.key] = ch.id
        return ch

    async def run(self) -> Report:
        perms = self.me.guild_permissions
        missing = [p for p in ("manage_roles", "manage_channels") if not getattr(perms, p)]
        if missing:
            self.report.failed.append(
                "I'm missing " + ", ".join(m.replace("_", " ").title() for m in missing)
                + ". Re-invite me with the README link or give my role those permissions."
            )
            if not self.dry_run:
                return self.report

        self.match_existing()  # all reuse first; only truly missing items reach the create calls below
        roles = {}
        for spec in ROLES:
            roles[spec.key] = await self.ensure_role(spec)
        staff = roles.get("STAFF_ROLE_ID")
        for cat_spec in LAYOUT:
            category = await self.ensure_category(cat_spec, staff)
            for ch_spec in cat_spec.channels:
                await self.ensure_channel(ch_spec, category, cat_spec)

        if not self.dry_run and self.report.ids:
            await gc.set_many(self.guild, self.report.ids)
        return self.report


def _chunks(items: list[str], limit: int = 1024) -> list[str]:
    out, cur = [], ""
    for item in items:
        line = f"• {item}"
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def build_summary(guild: discord.Guild, report: Report, dry_run: bool) -> discord.Embed:
    title = "🔍 /setup dry run: nothing was changed" if dry_run else "🛠️ Server setup complete"
    if report.failed and not dry_run:
        title = "🛠️ Server setup finished with problems"
    if report.waiting and not dry_run:
        title = "⏳ Server setup partly done: finishing automatically"
    color = discord.Color.green()
    if report.waiting:
        color = discord.Color.blue()
    if report.failed:
        color = discord.Color.orange()
    embed = discord.Embed(title=title, color=color)
    if report.waiting and not dry_run:
        ts = int(report.retry_at) if report.retry_at else int(time.time()) + 600
        if ts - time.time() > RESUME_MAX_WAIT:
            then = f"Please run `/setup` again <t:{ts}:R>; that's longer than I wait automatically."
        else:
            then = (f"I'll create the rest automatically <t:{ts}:R> and DM you when it's done "
                    "(even if I restart in the meantime). You can also run `/setup` again after that time.")
        embed.description = (
            "Discord limits how fast bots can create channels and roles, and that limit was hit. "
            f"Everything done so far is saved. {then} Or create the missing items yourself: "
            "`/setup` recognizes them by name."
        )
    sections = [
        ("🆕 Would create" if dry_run else "🆕 Created", report.created),
        ("♻️ Would reuse" if dry_run else "♻️ Reused", report.reused),
        ("⏳ Waiting for Discord", report.waiting),
        ("⚠️ Check these", report.warnings),
        ("❌ Failed", report.failed),
    ]
    for name, items in sections:
        for i, chunk in enumerate(_chunks(items)):
            embed.add_field(name=f"{name} ({len(items)})" if i == 0 else f"{name} (cont.)", value=chunk, inline=False)

    if not dry_run:
        def link(key: str, fallback: str) -> str:
            cid = gc.get(guild, key)
            return f"<#{cid}>" if cid else fallback

        steps = [
            f"Run `/rulespanel` in {link('START_HERE_CHANNEL_ID', '#rules')}, `/roles` in "
            f"{link('SELF_ROLES_CHANNEL_ID', '#self-roles')} and `/ticketpanel` in "
            f"{link('BOT_COMMANDS_CHANNEL_ID', '#bot-commands')}.",
            "Give the **Staff** role to your mods. Give it Moderate/Kick Members too if they should use `/timeout` and `/kick`.",
            "Scheduled posts (Prompt of the Day, Remix Friday, digest) start automatically. Try `/schedule-test`.",
        ]
        if report.waiting:
            steps = ["Wait for the rest to be created (see above). Then post the panels and give out Staff."] + steps[1:]
        if report.failed:
            steps.insert(0, "Some items failed. Fix the issue above, then run `/setup` again. It only adds what's missing.")
        embed.add_field(name="Next steps", value=trim("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), 1024),
                        inline=False)
        embed.set_footer(text="Saved for this server. See it any time with /setup-show.")
    else:
        embed.set_footer(text="Run /setup without dry_run to apply.")
    return embed


# Roles admins can point at an existing role with /setup-role. (label, key)
ROLE_SETTINGS = [
    ("Member (I accept button)", "MEMBER_ROLE_ID"),
    ("Staff", "STAFF_ROLE_ID"),
    ("Prompt King", "PROMPT_KING_ROLE_ID"),
    ("Remix Champion", "REMIX_CHAMPION_ROLE_ID"),
    ("LFG pings", "LFG_ROLE_ID"),
    ("Prompt of the Day pings", "PROMPT_PING_ROLE_ID"),
    ("Remix Friday pings", "CHALLENGE_PING_ROLE_ID"),
]
SETTING_ALIASES = {
    "member": "MEMBER_ROLE_ID", "staff": "STAFF_ROLE_ID", "prompt king": "PROMPT_KING_ROLE_ID",
    "remix champion": "REMIX_CHAMPION_ROLE_ID", "lfg": "LFG_ROLE_ID", "prompt pings": "PROMPT_PING_ROLE_ID",
    "challenge pings": "CHALLENGE_PING_ROLE_ID", "challenges": "CHALLENGE_PING_ROLE_ID",
}
# Roles the bot hands to members (by button or as a prize): must be assignable and harmless.
GRANTED_KEYS = {"MEMBER_ROLE_ID", "PROMPT_KING_ROLE_ID", "REMIX_CHAMPION_ROLE_ID"}
PING_KEYS = {"LFG_ROLE_ID", "PROMPT_PING_ROLE_ID", "CHALLENGE_PING_ROLE_ID"}
# Never hand these out automatically.
DANGEROUS_PERMS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_webhooks", "manage_messages",
    "manage_threads", "manage_nicknames", "manage_expressions", "manage_events", "ban_members", "kick_members",
    "moderate_members", "mention_everyone", "view_audit_log", "move_members", "mute_members", "deafen_members",
)


def role_problem(guild: discord.Guild, key: str, role: discord.Role):
    """Why this role can't be used for `key`, or None if it's fine."""
    if role.is_default():
        return "that's @everyone."
    if role.managed:
        return "it's managed by a bot or integration (like a booster or bot role)."
    if key in GRANTED_KEYS:
        if role >= guild.me.top_role:
            return "it's above my highest role, so I can't give it out. Drag my role above it in Server Settings → Roles."
        risky = [p.replace("_", " ").title() for p in DANGEROUS_PERMS if getattr(role.permissions, p, False)]
        if risky:
            return (
                "I'd be handing it to anyone who qualifies, and it has moderator permissions ("
                + ", ".join(risky[:4]) + ("…" if len(risky) > 4 else "") + "). Use a role without them."
            )
    return None


def _link_catalog() -> dict[str, tuple[str, str]]:
    """Every setting /setup-link can point at an existing item: key -> (label, kind)."""
    catalog: dict[str, tuple[str, str]] = {}
    for r in ROLES:
        catalog[r.key] = (f"{r.name} role", "role")
    catalog["MEMBER_ROLE_ID"] = ("Member role (given by the I accept button)", "role")
    for c in LAYOUT:
        catalog[c.key] = (f"{c.name} category", "category")
        for ch in c.channels:
            if ch.voice:
                catalog[ch.key] = (f"{ch.name} voice channel", "voice")
            else:
                catalog[ch.key] = (f"#{ch.name} channel", "text_or_forum" if ch.key in FORUM_OK else "text")
    catalog["MOD_LOG_CHANNEL_ID"] = ("Mod log channel (defaults to #mod-log)", "text")
    catalog["TICKET_LOG_CHANNEL_ID"] = ("Ticket log channel (defaults to #mod-log)", "text")
    catalog["PROMPT_PING_ROLE_ID"] = ("Prompt of the Day ping role", "role")
    catalog["CHALLENGE_PING_ROLE_ID"] = ("Remix Friday ping role", "role")
    return catalog


LINK_KEYS = _link_catalog()
KIND_NAMES = {
    "role": "a role", "category": "a category", "voice": "a voice channel", "text": "a text channel",
    "text_or_forum": "a text or forum channel",
}


def resolve_link_key(text: str) -> Optional[str]:
    """Accept MEMBER_ROLE_ID, member_role, "member role", "prompt channel", "rules", ..."""
    raw = (text or "").strip()
    if raw in LINK_KEYS:
        return raw
    upper = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").upper()
    for candidate in (upper, upper + "_ID", upper + "_CHANNEL_ID", upper + "_ROLE_ID", upper + "_CATEGORY_ID"):
        if candidate in LINK_KEYS:
            return candidate
    alias = SETTING_ALIASES.get(raw.lower())
    if alias:
        return alias
    wanted = normalize_name(raw)
    for key, (label, _) in LINK_KEYS.items():
        short = label.split(" (")[0]  # "Member role (given by ...)" -> "Member role"
        if wanted in (normalize_name(label), normalize_name(short), normalize_name(short.rsplit(" ", 1)[0])):
            return key
    return None


def link_problem(guild: discord.Guild, key: str, channel, role) -> tuple[object, Optional[str]]:
    """Validate a /setup-link target. Returns (target, problem)."""
    label, kind = LINK_KEYS[key]
    if kind == "role":
        if role is None:
            return None, f"**{label}** needs {KIND_NAMES[kind]}. Fill in the `role` option."
        return role, role_problem(guild, key, role)
    if channel is None:
        return None, f"**{label}** needs {KIND_NAMES[kind]}. Fill in the `channel` option."
    ok = {
        "category": isinstance(channel, discord.CategoryChannel),
        "voice": isinstance(channel, discord.VoiceChannel),
        "text": isinstance(channel, discord.TextChannel),
        "text_or_forum": isinstance(channel, (discord.TextChannel, discord.ForumChannel)),
    }[kind]
    if not ok:
        return None, f"**{label}** needs {KIND_NAMES[kind]}, but {channel.mention} isn't one."
    return channel, None


def member_role_note(role: discord.Role) -> str:
    return (
        f"\nThe **I accept the rules** button now gives {role.mention}, starting with the next click. "
        "Members who already clicked it can click again to get the role."
        "\nThis doesn't hide any channels. To make it a gate, deny **View Channels** to @everyone "
        f"on your main categories and allow it for {role.mention}."
    )


class Setup(commands.Cog):
    """Automatic server setup."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}
        self.resume_tasks: dict[int, asyncio.Task] = {}

    async def cog_unload(self):
        for task in self.resume_tasks.values():
            task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        """Pick up automatic finishes that were pending when the bot restarted."""
        for guild in self.bot.guilds:
            saved = gc.saved(guild)
            if "_RESUME_AT" not in saved:
                continue
            channel = guild.get_channel(saved.get("_RESUME_CHANNEL", 0))
            logger.info("Restoring pending /setup finish in %s", guild)
            self.schedule_resume(guild, saved.get("_RESUME_USER"), channel, float(saved["_RESUME_AT"]))

    def schedule_resume(self, guild: discord.Guild, user, channel, retry_at: float) -> None:
        """Finish a rate-limited /setup once Discord allows it, then tell the admin.

        `user` may be a user/member or a user ID (when restored after a restart).
        """
        task = self.resume_tasks.get(guild.id)
        if task is not None and not task.done():
            return  # already scheduled
        self.resume_tasks[guild.id] = asyncio.create_task(self._resume(guild.id, user, channel, retry_at))

    async def _save_pending(self, guild_id: int, user, channel, retry_at: float) -> None:
        user_id = user if isinstance(user, int) else getattr(user, "id", 0)
        await gc.set_many(guild_id, {
            "_RESUME_AT": int(retry_at), "_RESUME_USER": int(user_id or 0),
            "_RESUME_CHANNEL": int(getattr(channel, "id", 0) or 0),
        })

    async def _resolve_user(self, user):
        if not isinstance(user, int):
            return user
        if not user:
            return None
        found = self.bot.get_user(user)
        if found is None:
            try:
                found = await self.bot.fetch_user(user)
            except discord.HTTPException:
                return None
        return found

    async def _resume(self, guild_id: int, user, channel, retry_at: float) -> None:
        report = None
        try:
            await self._save_pending(guild_id, user, channel, retry_at)
        except Exception:
            logger.exception("Could not save pending /setup finish for %s", guild_id)
        for _ in range(RESUME_MAX_ROUNDS):
            delay = max(RESUME_PADDING, retry_at - time.time() + RESUME_PADDING)
            if delay > RESUME_MAX_WAIT:
                break
            await asyncio.sleep(delay)
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return  # bot left the server
            async with self.locks.setdefault(guild_id, asyncio.Lock()):
                report = await SetupRunner(guild, dry_run=False).run()
            logger.info("Resumed /setup in %s: %d created, %d still waiting", guild, len(report.created),
                        len(report.waiting))
            if not report.waiting:
                break
            retry_at = report.retry_at
            await self._save_pending(guild_id, user, channel, retry_at)
        try:
            await gc.delete_keys(guild_id, RESUME_KEYS)
        except Exception:
            logger.exception("Could not clear pending /setup finish for %s", guild_id)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        user = await self._resolve_user(user)
        if report is None or report.waiting:
            when = f"<t:{int(retry_at)}:R>" if retry_at > time.time() else "now"
            text = (f"⏳ Discord is still limiting how fast I can create things in **{guild.name}**. "
                    f"Please run `/setup` again {when}. It picks up where it left off. You can also create the "
                    "missing items yourself; `/setup` will recognize them by name.")
            embed = build_summary(guild, report, dry_run=False) if report else None
        else:
            text = f"✅ `/setup` finished in **{guild.name}**."
            embed = build_summary(guild, report, dry_run=False)
        try:
            await user.send(content=text, embed=embed)
        except (discord.HTTPException, AttributeError):
            if channel is not None:
                try:
                    await channel.send(content=f"{getattr(user, 'mention', '')} {text}".strip(), embed=embed)
                except discord.HTTPException:
                    logger.warning("Could not report resumed /setup result in %s", guild)

    @commands.hybrid_command(name="setup", description="Create/reuse all channels and roles the bot needs (admin)")
    @app_commands.describe(dry_run="Only report what would be created or reused, without changing anything")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_server(self, ctx: commands.Context, dry_run: bool = False):
        lock = self.locks.setdefault(ctx.guild.id, asyncio.Lock())
        if lock.locked():
            await ctx.send("⏳ `/setup` is already running here.", ephemeral=True)
            return
        async with lock:
            await ctx.defer(ephemeral=True)
            logger.info("%s ran /setup (dry_run=%s) in %s", ctx.author, dry_run, ctx.guild)
            report = await SetupRunner(ctx.guild, dry_run).run()
            logger.info(
                "/setup in %s: %d created, %d reused, %d waiting, %d warnings, %d failed",
                ctx.guild, len(report.created), len(report.reused), len(report.waiting), len(report.warnings),
                len(report.failed),
            )
            await ctx.send(embed=build_summary(ctx.guild, report, dry_run), ephemeral=True)
            if dry_run:
                return
            if report.waiting:
                self.schedule_resume(ctx.guild, ctx.author, ctx.channel, report.retry_at)
            else:
                pending = self.resume_tasks.pop(ctx.guild.id, None)
                if pending is not None and not pending.done():
                    pending.cancel()  # finished by hand; no follow-up needed
                if "_RESUME_AT" in gc.saved(ctx.guild):
                    await gc.delete_keys(ctx.guild, RESUME_KEYS)

    @commands.hybrid_command(name="setup-show", description="Show this server's saved bot configuration (admin)")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_show(self, ctx: commands.Context):
        saved = {k: v for k, v in gc.saved(ctx.guild).items() if not k.startswith("_")}  # hide internal keys
        embed = discord.Embed(title=f"⚙️ Saved config for {ctx.guild.name}", color=discord.Color.blurple())

        def fmt(key: str, value: int) -> str:
            if key.endswith("_ROLE_ID"):
                return f"<@&{value}>"
            return f"<#{value}>"

        lines = [f"`{k}` → {fmt(k, v)}" for k, v in sorted(saved.items())]
        if not lines:
            embed.description = "Nothing saved yet. Run `/setup` (or `/setup dry_run:True` to preview)."
        for i, chunk in enumerate(_chunks(lines)):
            embed.add_field(name="Saved by /setup" if i == 0 else "Saved (cont.)", value=chunk, inline=False)

        # Keys the bot uses that aren't saved: from env vars (home server only) or unset.
        env_lines, unset = [], []
        for key in gc.KEYS:
            if key in saved:
                continue
            value = gc.get(ctx.guild, key)
            if value:
                env_lines.append(f"`{key}` → {fmt(key, value)}")
            else:
                unset.append(f"`{key}`")
        for i, chunk in enumerate(_chunks(env_lines)):
            embed.add_field(name="From env vars" if i == 0 else "From env vars (cont.)", value=chunk, inline=False)
        if unset:
            embed.add_field(name="Not set (optional)", value=trim(", ".join(unset), 1024), inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ /setup-role
    @commands.hybrid_command(name="setup-role", description="Use an existing role for one of the bot's roles (admin)")
    @app_commands.describe(setting="Which bot role to set", role="The existing role to use")
    @app_commands.choices(setting=[app_commands.Choice(name=label, value=key) for label, key in ROLE_SETTINGS])
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_role(self, ctx: commands.Context, setting: str, role: discord.Role):
        """Prefix usage: !setup-role MEMBER_ROLE_ID @Verified"""
        key = SETTING_ALIASES.get(setting.strip().lower(), setting.strip().upper())
        label = dict((k, l) for l, k in ROLE_SETTINGS).get(key)
        if label is None:
            options = ", ".join(f"`{k}`" for _, k in ROLE_SETTINGS)
            await ctx.send(f"❓ Unknown setting. Use one of: {options}", ephemeral=True)
            return
        problem = role_problem(ctx.guild, key, role)
        if problem:
            await ctx.send(f"🚫 Can't use {role.mention} for **{label}**: {problem}", ephemeral=True)
            return

        await gc.set_many(ctx.guild, {key: role.id})
        logger.info("%s set %s to %s (%s) in %s", ctx.author, key, role.name, role.id, ctx.guild)
        msg = f"✅ **{label}** is now {role.mention}."
        if key == "MEMBER_ROLE_ID":
            msg += member_role_note(role)
        elif key in PING_KEYS and not role.mentionable and not ctx.guild.me.guild_permissions.mention_everyone:
            msg += f"\n⚠️ {role.mention} isn't mentionable, so pings won't notify. Turn on **Allow anyone to @mention**."
        await ctx.send(msg, ephemeral=True)

    # ------------------------------------------------------------ /setup-link
    @commands.hybrid_command(name="setup-link", description="Point a bot setting at an existing channel or role (admin)")
    @app_commands.describe(
        key="Which setting, e.g. Prompt of the Day channel, Member role, Tickets category",
        channel="The existing channel or category to use",
        role="The existing role to use",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_link(
        self,
        ctx: commands.Context,
        key: str,
        channel: Optional[discord.abc.GuildChannel] = None,
        role: Optional[discord.Role] = None,
    ):
        """Prefix usage: !setup-link PROMPT_CHANNEL_ID #prompt-of-the-day   or   !setup-link member_role @Verified"""
        resolved = resolve_link_key(key)
        if resolved is None:
            await ctx.send(
                f"❓ I don't know the setting `{trim(key, 50)}`. Pick one from the list, e.g. "
                "`PROMPT_CHANNEL_ID`, `MEMBER_ROLE_ID`, `TICKET_CATEGORY_ID`.",
                ephemeral=True,
            )
            return
        target, problem = link_problem(ctx.guild, resolved, channel, role)
        label = LINK_KEYS[resolved][0]
        if problem:
            await ctx.send(f"🚫 {problem}", ephemeral=True)
            return

        await gc.set_many(ctx.guild, {resolved: target.id})
        logger.info("%s linked %s to %s (%s) in %s", ctx.author, resolved, target.name, target.id, ctx.guild)
        shown = target.mention if not isinstance(target, discord.CategoryChannel) else f"📁 {target.name}"
        msg = f"✅ **{label}** (`{resolved}`) is now {shown}. Saved for this server."
        if resolved == "MEMBER_ROLE_ID":
            msg += member_role_note(target)
        elif resolved in PING_KEYS and not target.mentionable and not ctx.guild.me.guild_permissions.mention_everyone:
            msg += f"\n⚠️ {target.mention} isn't mentionable, so pings won't notify. Turn on **Allow anyone to @mention**."
        await ctx.send(msg, ephemeral=True)

    @setup_link.autocomplete("key")
    async def _setup_link_key_autocomplete(self, interaction: discord.Interaction, current: str):
        wanted = normalize_name(current)
        choices = [
            app_commands.Choice(name=trim(f"{label} · {key}", 100), value=key)
            for key, (label, _) in LINK_KEYS.items()
            if not wanted or wanted in normalize_name(f"{label} {key}")
        ]
        return choices[:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(Setup(bot))
