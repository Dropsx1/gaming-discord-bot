"""/setup: configure a server automatically.

Creates (or reuses, matching by saved ID first and then by name) the roles,
categories and channels the bot uses, and saves every ID to guild_config so
no env vars or ID copying are needed. Safe to run again: existing items are
reused and never modified; only items it creates get permissions set.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from utils import guildconfig as gc
from utils.helpers import trim

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

# Prompt of the Day and Remix Friday also work in forum channels.
FORUM_OK = {"PROMPT_CHANNEL_ID", "CHALLENGE_CHANNEL_ID"}


def _norm(name: str) -> str:
    return name.casefold().strip()


class Report:
    def __init__(self):
        self.created: list[str] = []
        self.reused: list[str] = []
        self.warnings: list[str] = []
        self.failed: list[str] = []
        self.ids: dict[str, int] = {}


class SetupRunner:
    """One /setup run for one server."""

    def __init__(self, guild: discord.Guild, dry_run: bool):
        self.guild = guild
        self.dry_run = dry_run
        self.me = guild.me
        self.report = Report()
        self.reason = "Bot /setup"

    # --------------------------------------------------------------- lookup
    def _saved(self, key: str) -> int:
        return gc.get(self.guild, key)

    def find_role(self, spec: RoleSpec) -> Optional[discord.Role]:
        role = self.guild.get_role(self._saved(spec.key)) if self._saved(spec.key) else None
        if role is None:
            role = discord.utils.find(lambda r: _norm(r.name) == _norm(spec.name), self.guild.roles)
        return role if role is not None and not role.is_default() else None

    def find_category(self, spec: CategorySpec) -> Optional[discord.CategoryChannel]:
        ch = self.guild.get_channel(self._saved(spec.key)) if self._saved(spec.key) else None
        if isinstance(ch, discord.CategoryChannel):
            return ch
        return discord.utils.find(lambda c: _norm(c.name) == _norm(spec.name), self.guild.categories)

    def _type_ok(self, ch, spec: ChannelSpec) -> bool:
        if spec.voice:
            return isinstance(ch, discord.VoiceChannel)
        if spec.key in FORUM_OK and isinstance(ch, discord.ForumChannel):
            return True
        return isinstance(ch, discord.TextChannel)

    def find_channel(self, spec: ChannelSpec, category: Optional[discord.CategoryChannel]):
        ch = self.guild.get_channel(self._saved(spec.key)) if self._saved(spec.key) else None
        if ch is not None and self._type_ok(ch, spec):
            return ch
        matches = [c for c in self.guild.channels if self._type_ok(c, spec) and _norm(c.name) == _norm(spec.name)]
        # Prefer one already in the target category.
        matches.sort(key=lambda c: 0 if category is not None and c.category_id == category.id else 1)
        return matches[0] if matches else None

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
        role = self.find_role(spec)
        if role is not None:
            self.report.reused.append(f"@{role.name}")
            self.report.ids[spec.key] = role.id
            if role >= self.me.top_role:
                self.report.warnings.append(
                    f"@{role.name} is above my top role, so I can't give it out. Drag my role above it."
                )
            return role
        if self.dry_run:
            self.report.created.append(f"@{spec.name}")
            return None
        try:
            role = await self.guild.create_role(
                name=spec.name, colour=spec.color, mentionable=spec.mentionable, reason=self.reason
            )  # new roles start at the bottom, i.e. below the bot's top role
        except (discord.HTTPException, discord.RateLimited) as e:
            self.report.failed.append(f"@{spec.name} ({e.__class__.__name__})")
            return None
        self.report.created.append(f"@{role.name}")
        self.report.ids[spec.key] = role.id
        return role

    async def ensure_category(self, spec: CategorySpec, staff: Optional[discord.Role]):
        cat = self.find_category(spec)
        if cat is not None:
            self.report.reused.append(f"📁 {cat.name}")
            self.report.ids[spec.key] = cat.id
            if spec.private:
                if cat.permissions_for(self.guild.default_role).view_channel:
                    self.report.warnings.append(f"📁 {cat.name} is visible to @everyone. Hide it in its permissions.")
                if not cat.permissions_for(self.me).view_channel:
                    self.report.warnings.append(f"📁 {cat.name} is hidden from me. Allow my role to View Channel.")
            return cat
        if self.dry_run:
            self.report.created.append(f"📁 {spec.name}")
            return None
        try:
            cat = await self.guild.create_category(
                spec.name,
                overwrites=self.private_overwrites(staff) if spec.private else {},
                reason=self.reason,
            )
        except (discord.HTTPException, discord.RateLimited) as e:
            self.report.failed.append(f"📁 {spec.name} ({e.__class__.__name__})")
            return None
        self.report.created.append(f"📁 {cat.name}")
        self.report.ids[spec.key] = cat.id
        return cat

    async def ensure_channel(self, spec: ChannelSpec, category, cat_spec: CategorySpec):
        label = f"🔊 {spec.name}" if spec.voice else f"#{spec.name}"
        ch = self.find_channel(spec, category)
        if ch is not None:
            self.report.reused.append(f"🔊 {ch.name}" if spec.voice else ch.mention)
            self.report.ids[spec.key] = ch.id
            return ch
        if self.dry_run:
            self.report.created.append(f"{label} (in {cat_spec.name})")
            return None
        if category is None:
            self.report.failed.append(f"{label} (its category {cat_spec.name} is missing)")
            return None
        try:
            if spec.voice:
                ch = await self.guild.create_voice_channel(spec.name, category=category, reason=self.reason)
            else:
                ow = self.channel_overwrites(spec, category)
                kwargs = {"overwrites": ow} if ow is not None else {}
                ch = await self.guild.create_text_channel(spec.name, category=category, reason=self.reason, **kwargs)
        except (discord.HTTPException, discord.RateLimited) as e:
            self.report.failed.append(f"{label} ({e.__class__.__name__})")
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
    embed = discord.Embed(title=title, color=discord.Color.orange() if report.failed else discord.Color.green())
    sections = [
        ("🆕 Would create" if dry_run else "🆕 Created", report.created),
        ("♻️ Would reuse" if dry_run else "♻️ Reused", report.reused),
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


class Setup(commands.Cog):
    """Automatic server setup."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.locks: dict[int, asyncio.Lock] = {}

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
                "/setup in %s: %d created, %d reused, %d warnings, %d failed",
                ctx.guild, len(report.created), len(report.reused), len(report.warnings), len(report.failed),
            )
            await ctx.send(embed=build_summary(ctx.guild, report, dry_run), ephemeral=True)

    @commands.hybrid_command(name="setup-show", description="Show this server's saved bot configuration (admin)")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def setup_show(self, ctx: commands.Context):
        saved = gc.saved(ctx.guild)
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
            msg += (
                f"\nThe **I accept the rules** button now gives {role.mention}, starting with the next click. "
                "Members who already clicked it can click again to get the role."
                "\nThis doesn't hide any channels. To make it a gate, deny **View Channels** to @everyone "
                f"on your main categories and allow it for {role.mention}."
            )
        elif key in PING_KEYS and not role.mentionable and not ctx.guild.me.guild_permissions.mention_everyone:
            msg += f"\n⚠️ {role.mention} isn't mentionable, so pings won't notify. Turn on **Allow anyone to @mention**."
        await ctx.send(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Setup(bot))
