"""Join-to-Create temporary voice channels.

Ownership is stored in the channel's permission overwrites (the owner gets
Manage Channels + Move/Mute Members), so it's rebuilt automatically after a
restart -- no database needed. Use a dedicated category for temp channels.
"""

import asyncio
import logging
import time
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils.helpers import trim

logger = logging.getLogger("bot.voice")

CREATE_COOLDOWN = 10  # seconds between temp-channel creations per member


def owner_overwrite() -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(
        view_channel=True, connect=True, speak=True,
        manage_channels=True, move_members=True, mute_members=True,
    )


def find_owner_from_overwrites(channel: discord.VoiceChannel) -> Optional[discord.Member]:
    for target, ow in channel.overwrites.items():
        if isinstance(target, discord.Member) and not target.bot and ow.manage_channels and ow.move_members and ow.mute_members:
            return target
    return None


class Voice(commands.Cog):
    """Temporary voice channels with owner controls."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.temp_channels: dict[int, int] = {}  # channel_id -> owner_id
        self._last_create: dict[int, float] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ helpers
    def get_category(self, guild: discord.Guild) -> Optional[discord.CategoryChannel]:
        if config.VOICE_CATEGORY_ID:
            cat = guild.get_channel(config.VOICE_CATEGORY_ID)
            if isinstance(cat, discord.CategoryChannel):
                return cat
            logger.warning("VOICE_CATEGORY_ID %s is not a category", config.VOICE_CATEGORY_ID)
        jtc = guild.get_channel(config.JOIN_TO_CREATE_CHANNEL_ID)
        return jtc.category if jtc else None

    def owned_channel(self, member: discord.Member) -> Optional[discord.VoiceChannel]:
        for cid, oid in self.temp_channels.items():
            if oid == member.id:
                ch = member.guild.get_channel(cid)
                if isinstance(ch, discord.VoiceChannel):
                    return ch
        return None

    async def delete_if_empty(self, channel: discord.VoiceChannel):
        if channel.id not in self.temp_channels or channel.members:
            return
        self.temp_channels.pop(channel.id, None)
        try:
            await channel.delete(reason="Temporary voice channel empty")
            logger.info("Deleted empty temp VC %s", channel.name)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.warning("Could not delete temp VC %s: %s", channel.name, e)

    # ---------------------------------------------------------------- listeners
    @commands.Cog.listener()
    async def on_ready(self):
        """Rebuild temp-channel ownership after a restart and clean up empty ones."""
        if not config.JOIN_TO_CREATE_CHANNEL_ID:
            return
        for guild in self.bot.guilds:
            category = self.get_category(guild)
            if not category:
                continue
            for channel in category.voice_channels:
                if channel.id == config.JOIN_TO_CREATE_CHANNEL_ID:
                    continue
                owner = find_owner_from_overwrites(channel)
                if owner is None:
                    continue  # not one of ours
                self.temp_channels[channel.id] = owner.id
                await self.delete_if_empty(channel)
        logger.info("Tracking %d temp voice channel(s)", len(self.temp_channels))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel:
            return  # mute/deafen/etc.
        if after.channel and after.channel.id == config.JOIN_TO_CREATE_CHANNEL_ID and not member.bot:
            await self.create_temp_channel(member)
        if isinstance(before.channel, discord.VoiceChannel):
            await self.delete_if_empty(before.channel)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        self.temp_channels.pop(channel.id, None)

    async def create_temp_channel(self, member: discord.Member):
        guild = member.guild
        async with self._lock:
            # Already own a channel? Send them back to it instead of making another.
            existing = self.owned_channel(member)
            if existing:
                try:
                    await member.move_to(existing)
                except discord.HTTPException:
                    pass
                return

            now = time.monotonic()
            if now - self._last_create.get(member.id, 0) < CREATE_COOLDOWN:
                try:
                    await member.move_to(None)  # disconnect from Join-to-Create
                except discord.HTTPException:
                    pass
                return
            self._last_create[member.id] = now

            category = self.get_category(guild)
            if category is None:
                logger.warning("No category for temp VCs (set VOICE_CATEGORY_ID)")
                return

            name = trim(config.VC_NAME_TEMPLATE.format(user=member.display_name), 100)
            overwrites = dict(category.overwrites)  # inherit category permissions
            overwrites[member] = owner_overwrite()
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True, move_members=True)

            try:
                channel = await guild.create_voice_channel(
                    name=name,
                    category=category,
                    overwrites=overwrites,
                    user_limit=max(0, min(config.VC_DEFAULT_LIMIT, 99)),
                    reason=f"Temp VC for {member}",
                )
            except discord.HTTPException as e:
                logger.error("Could not create temp VC for %s: %s", member, e)
                return
            self.temp_channels[channel.id] = member.id

        try:
            await member.move_to(channel, reason="Join to Create")
            logger.info("Created temp VC '%s' for %s", channel.name, member)
        except discord.HTTPException:
            # They left before we could move them.
            await self.delete_if_empty(channel)

    # ----------------------------------------------------------------- commands
    async def _require_owner(self, ctx: commands.Context) -> Optional[discord.VoiceChannel]:
        voice = ctx.author.voice
        channel = voice.channel if voice else None
        if not isinstance(channel, discord.VoiceChannel) or channel.id not in self.temp_channels:
            await ctx.send("🔊 Join your temporary voice channel first.", ephemeral=True)
            return None
        if self.temp_channels[channel.id] != ctx.author.id:
            await ctx.send("🚫 You don't own this channel. If the owner left, use `/vc claim`.", ephemeral=True)
            return None
        return channel

    @commands.hybrid_group(name="vc", fallback="info", description="Control your temporary voice channel")
    async def vc(self, ctx: commands.Context):
        voice = ctx.author.voice
        channel = voice.channel if voice else None
        if not channel or channel.id not in self.temp_channels:
            jtc = f"<#{config.JOIN_TO_CREATE_CHANNEL_ID}>" if config.JOIN_TO_CREATE_CHANNEL_ID else "Join to Create"
            await ctx.send(f"Join {jtc} to get your own voice channel, then use `/vc` to control it.", ephemeral=True)
            return
        owner = ctx.guild.get_member(self.temp_channels[channel.id])
        locked = channel.overwrites_for(ctx.guild.default_role).connect is False
        embed = discord.Embed(title=f"🔊 {channel.name}", color=discord.Color.blurple())
        embed.add_field(name="Owner", value=owner.mention if owner else "Unknown")
        embed.add_field(name="Limit", value=channel.user_limit or "None")
        embed.add_field(name="Status", value="🔒 Locked" if locked else "🔓 Open")
        embed.set_footer(text="/vc rename • limit • lock • unlock • permit • kick • transfer • claim")
        await ctx.send(embed=embed, ephemeral=True)

    @vc.command(name="rename", description="Rename your voice channel")
    @app_commands.describe(name="New channel name")
    async def vc_rename(self, ctx: commands.Context, *, name: str):
        if not (channel := await self._require_owner(ctx)):
            return
        await ctx.defer(ephemeral=True)
        try:
            await channel.edit(name=trim(name, 100), reason=f"Renamed by {ctx.author}")
        except discord.RateLimited:
            await ctx.send("⏳ Discord only allows 2 renames per 10 minutes. Try again later.", ephemeral=True)
            return
        await ctx.send(f"✏️ Renamed to **{trim(name, 100)}**.", ephemeral=True)

    @vc.command(name="limit", description="Set a user limit (0 = unlimited)")
    @app_commands.describe(limit="Max users (0-99)")
    async def vc_limit(self, ctx: commands.Context, limit: app_commands.Range[int, 0, 99]):
        if not (channel := await self._require_owner(ctx)):
            return
        await channel.edit(user_limit=limit)
        await ctx.send(f"👥 User limit set to **{limit or 'unlimited'}**.", ephemeral=True)

    @vc.command(name="lock", description="Lock your channel so no one new can join")
    async def vc_lock(self, ctx: commands.Context):
        if not (channel := await self._require_owner(ctx)):
            return
        ow = channel.overwrites_for(ctx.guild.default_role)
        ow.connect = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=ow)
        # Let people already inside reconnect.
        for m in channel.members:
            if m.id != ctx.author.id:
                await channel.set_permissions(m, connect=True, view_channel=True)
        await ctx.send("🔒 Channel locked. Use `/vc permit` to let someone in.", ephemeral=True)

    @vc.command(name="unlock", description="Unlock your channel")
    async def vc_unlock(self, ctx: commands.Context):
        if not (channel := await self._require_owner(ctx)):
            return
        ow = channel.overwrites_for(ctx.guild.default_role)
        ow.connect = None  # back to category default
        await channel.set_permissions(ctx.guild.default_role, overwrite=None if ow.is_empty() else ow)
        await ctx.send("🔓 Channel unlocked.", ephemeral=True)

    @vc.command(name="permit", description="Allow a member to join even when locked")
    @app_commands.describe(member="Member to allow")
    async def vc_permit(self, ctx: commands.Context, member: discord.Member):
        if not (channel := await self._require_owner(ctx)):
            return
        await channel.set_permissions(member, connect=True, view_channel=True)
        await ctx.send(f"✅ {member.mention} can now join.", ephemeral=True)

    @vc.command(name="kick", description="Remove a member from your channel")
    @app_commands.describe(member="Member to remove")
    async def vc_kick(self, ctx: commands.Context, member: discord.Member):
        if not (channel := await self._require_owner(ctx)):
            return
        if member == ctx.author:
            await ctx.send("You can't kick yourself.", ephemeral=True)
            return
        await channel.set_permissions(member, connect=False)
        if member.voice and member.voice.channel == channel:
            await member.move_to(None, reason=f"Removed from temp VC by {ctx.author}")
        await ctx.send(f"👋 {member.mention} was removed and can't rejoin.", ephemeral=True)

    async def _set_owner(self, channel: discord.VoiceChannel, old_id: Optional[int], new: discord.Member):
        if old_id:
            old = channel.guild.get_member(old_id)
            if old:
                await channel.set_permissions(old, overwrite=None)
        await channel.set_permissions(new, overwrite=owner_overwrite())
        self.temp_channels[channel.id] = new.id

    @vc.command(name="transfer", description="Give ownership to someone in your channel")
    @app_commands.describe(member="New owner (must be in the channel)")
    async def vc_transfer(self, ctx: commands.Context, member: discord.Member):
        if not (channel := await self._require_owner(ctx)):
            return
        if member.bot or member not in channel.members:
            await ctx.send("The new owner must be a member in your channel.", ephemeral=True)
            return
        await self._set_owner(channel, ctx.author.id, member)
        await ctx.send(f"👑 {member.mention} now owns this channel.", ephemeral=True)

    @vc.command(name="claim", description="Claim the channel if the owner left")
    async def vc_claim(self, ctx: commands.Context):
        voice = ctx.author.voice
        channel = voice.channel if voice else None
        if not isinstance(channel, discord.VoiceChannel) or channel.id not in self.temp_channels:
            await ctx.send("🔊 Join a temporary voice channel first.", ephemeral=True)
            return
        owner_id = self.temp_channels[channel.id]
        if owner_id == ctx.author.id:
            await ctx.send("You already own this channel.", ephemeral=True)
            return
        if any(m.id == owner_id for m in channel.members):
            await ctx.send("The owner is still here. Ask them to `/vc transfer`.", ephemeral=True)
            return
        await self._set_owner(channel, owner_id, ctx.author)
        await ctx.send("👑 You now own this channel.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Voice(bot))
