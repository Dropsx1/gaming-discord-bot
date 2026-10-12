"""Ticket system.

Ticket state lives in the channel topic ("owner:<id>" / "claimed:<id>"), so
tickets keep working after a restart without a database. The panel and the
in-ticket control buttons are persistent views.
"""

import asyncio
import io
import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import guildconfig as gc
from utils.helpers import is_staff, send_log, staff_only

logger = logging.getLogger("bot.tickets")

OWNER_RE = re.compile(r"owner:(\d+)")
CLAIMED_RE = re.compile(r"claimed:(\d+)")
CLOSE_DELAY = 5  # seconds


def ticket_owner_id(channel: discord.abc.GuildChannel) -> Optional[int]:
    """Return the ticket owner's ID if this channel is a ticket, else None."""
    if not isinstance(channel, discord.TextChannel) or not channel.topic:
        return None
    category_id = gc.get(channel.guild, "TICKET_CATEGORY_ID")
    if category_id and channel.category_id != category_id:
        return None
    m = OWNER_RE.search(channel.topic)
    return int(m.group(1)) if m else None


def safe_channel_name(member: discord.Member) -> str:
    base = re.sub(r"[^a-z0-9-]", "", member.name.lower().replace(" ", "-"))[:20] or str(member.id)
    return f"ticket-{base}"


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Open Ticket", style=discord.ButtonStyle.green, emoji="🎫", custom_id="ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.create_ticket(interaction)


class TicketControlView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Close", style=discord.ButtonStyle.red, emoji="🔒", custom_id="ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        owner_id = ticket_owner_id(interaction.channel)
        if owner_id is None:
            await interaction.response.send_message("This isn't a ticket channel.", ephemeral=True)
            return
        if not (is_staff(interaction.user) or interaction.user.id == owner_id):
            await interaction.response.send_message("Only staff or the ticket owner can close this.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Close this ticket?", view=CloseConfirmView(self.cog, interaction.user), ephemeral=True
        )

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.blurple, emoji="✋", custom_id="ticket:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.claim(interaction.channel, interaction.user, interaction.response.send_message)


class CloseConfirmView(discord.ui.View):
    """Short-lived ephemeral confirmation (not persistent on purpose)."""

    def __init__(self, cog: "Tickets", user: discord.abc.User):
        super().__init__(timeout=60)
        self.cog = cog
        self.user = user

    async def _go(self, interaction: discord.Interaction, transcript: bool):
        await interaction.response.edit_message(content="🔒 Closing…", view=None)
        await self.cog.close(interaction.channel, interaction.user, transcript=transcript)

    @discord.ui.button(label="Close + transcript", style=discord.ButtonStyle.red, emoji="📄")
    async def with_transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.grey)
    async def without_transcript(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, False)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)


class Tickets(commands.Cog):
    """Private support tickets."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._creating: set[int] = set()
        self._closing: set[int] = set()
        bot.add_view(TicketPanelView(self))
        bot.add_view(TicketControlView(self))

    # ---------------------------------------------------------------- create
    def find_open_ticket(self, guild: discord.Guild, user_id: int) -> Optional[discord.TextChannel]:
        category = gc.channel(guild, "TICKET_CATEGORY_ID")
        if not isinstance(category, discord.CategoryChannel):
            return None
        for ch in category.text_channels:
            if ticket_owner_id(ch) == user_id and ch.id not in self._closing:
                return ch
        return None

    async def open_ticket_for(self, guild: discord.Guild, user: discord.Member) -> str:
        """Create a ticket and return a user-facing status message."""
        category = gc.channel(guild, "TICKET_CATEGORY_ID")
        if not isinstance(category, discord.CategoryChannel):
            logger.warning("TICKET_CATEGORY_ID is missing or not a category in %s", guild)
            return "⚠️ Tickets aren't configured yet. Please ping staff (they can run `/setup`)."
        if existing := self.find_open_ticket(guild, user.id):
            return f"You already have an open ticket: {existing.mention}"
        if user.id in self._creating:
            return "Hang on, your ticket is being created…"

        self._creating.add(user.id)
        try:
            staff_role = gc.role(guild, "STAFF_ROLE_ID")
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                user: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, manage_channels=True,
                    manage_permissions=True, embed_links=True, attach_files=True,
                ),
            }
            if staff_role:
                overwrites[staff_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, attach_files=True
                )

            channel = await guild.create_text_channel(
                name=safe_channel_name(user),
                category=category,
                overwrites=overwrites,
                topic=f"Support ticket for {user} | owner:{user.id}",
                reason=f"Ticket opened by {user}",
            )

            embed = discord.Embed(
                title="🎫 Support Ticket",
                description=(
                    f"Hey {user.mention}, thanks for reaching out!\n"
                    "Tell us what's up and a staff member will be with you shortly.\n\n"
                    "Use the buttons below to **close** or (staff) **claim** this ticket."
                ),
                color=discord.Color.green(),
            )
            ping = f"{user.mention} {staff_role.mention}" if staff_role else user.mention
            await channel.send(content=ping, embed=embed, view=TicketControlView(self))
            logger.info("Ticket created for %s -> #%s", user, channel.name)

            log = discord.Embed(title="🎫 Ticket Opened", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            log.add_field(name="User", value=f"{user.mention} (`{user}`)")
            log.add_field(name="Channel", value=channel.mention)
            await send_log(guild, gc.get(guild, "TICKET_LOG_CHANNEL_ID"), log)
            return f"✅ Ticket created: {channel.mention}"
        except discord.Forbidden:
            logger.error("Missing permissions to create ticket channels")
            return "⚠️ I don't have permission to create ticket channels."
        finally:
            self._creating.discard(user.id)

    async def create_ticket(self, interaction: discord.Interaction):
        """Button / slash entry point."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await self.open_ticket_for(interaction.guild, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)

    # ----------------------------------------------------------------- claim
    async def claim(self, channel, user: discord.Member, respond):
        if ticket_owner_id(channel) is None:
            await respond("This isn't a ticket channel.", ephemeral=True)
            return
        if not is_staff(user):
            await respond("Only staff can claim tickets.", ephemeral=True)
            return
        if m := CLAIMED_RE.search(channel.topic or ""):
            claimer = channel.guild.get_member(int(m.group(1)))
            await respond(f"Already claimed by {claimer.mention if claimer else 'someone'}.", ephemeral=True)
            return

        await respond(embed=discord.Embed(description=f"✋ Ticket claimed by {user.mention}", color=discord.Color.blurple()))
        logger.info("Ticket #%s claimed by %s", channel.name, user)
        try:
            # Topic edits are rate-limited by Discord; do it after responding.
            await channel.edit(topic=f"{channel.topic} | claimed:{user.id}")
        except (discord.HTTPException, discord.RateLimited) as e:
            logger.warning("Could not store claim in topic for #%s: %s", channel.name, e)

    # ----------------------------------------------------------------- close
    async def build_transcript(self, channel: discord.TextChannel) -> discord.File:
        lines = [f"Transcript of #{channel.name} ({channel.id})", f"Topic: {channel.topic}", ""]
        async for msg in channel.history(limit=config.TICKET_TRANSCRIPT_LIMIT, oldest_first=True):
            stamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
            text = msg.clean_content
            for e in msg.embeds:
                text += f" [embed: {e.title or ''} {e.description or ''}]".rstrip()
            for a in msg.attachments:
                text += f" [attachment: {a.url}]"
            lines.append(f"[{stamp}] {msg.author} ({msg.author.id}): {text}")
        data = io.BytesIO("\n".join(lines).encode("utf-8"))
        return discord.File(data, filename=f"transcript-{channel.name}.txt")

    async def close(self, channel: discord.TextChannel, closed_by: discord.abc.User, transcript: bool = True):
        owner_id = ticket_owner_id(channel)
        if owner_id is None or channel.id in self._closing:
            return
        self._closing.add(channel.id)
        try:
            await channel.send(
                embed=discord.Embed(
                    description=f"🔒 Ticket closed by {closed_by.mention}. Deleting in {CLOSE_DELAY} seconds…",
                    color=discord.Color.red(),
                )
            )

            log = discord.Embed(title="🔒 Ticket Closed", color=discord.Color.red(), timestamp=discord.utils.utcnow())
            log.add_field(name="Ticket", value=f"#{channel.name}")
            log.add_field(name="Owner", value=f"<@{owner_id}>")
            log.add_field(name="Closed by", value=closed_by.mention)
            if m := CLAIMED_RE.search(channel.topic or ""):
                log.add_field(name="Claimed by", value=f"<@{m.group(1)}>")

            file = None
            if transcript:
                try:
                    file = await self.build_transcript(channel)
                except discord.HTTPException as e:
                    logger.warning("Transcript failed for #%s: %s", channel.name, e)
            await send_log(channel.guild, gc.get(channel.guild, "TICKET_LOG_CHANNEL_ID"), log, file=file)

            if transcript and config.TICKET_DM_TRANSCRIPT:
                owner = channel.guild.get_member(owner_id)
                if owner:
                    try:
                        await owner.send(
                            f"Your ticket in **{channel.guild.name}** was closed. Here's a copy:",
                            file=await self.build_transcript(channel),
                        )
                    except discord.HTTPException:
                        pass

            await asyncio.sleep(CLOSE_DELAY)
            await channel.delete(reason=f"Ticket closed by {closed_by}")
            logger.info("Ticket #%s closed by %s (transcript=%s)", channel.name, closed_by, transcript)
        except discord.NotFound:
            pass
        except discord.HTTPException as e:
            logger.error("Failed to close ticket #%s: %s", channel.name, e)
        finally:
            self._closing.discard(channel.id)

    # -------------------------------------------------------------- commands
    @commands.hybrid_command(name="ticket", description="Open a private support ticket")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def ticket(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        await ctx.send(await self.open_ticket_for(ctx.guild, ctx.author), ephemeral=True)

    @commands.hybrid_command(name="ticketpanel", description="Post the ticket panel (staff)")
    @staff_only()
    async def ticketpanel(self, ctx: commands.Context):
        embed = discord.Embed(
            title="🎫 Need Help?",
            description=(
                "Click the button below to open a **private ticket** with staff.\n"
                "Use it for reports, questions, appeals, or anything you'd rather not post publicly."
            ),
            color=discord.Color.blue(),
        )
        await ctx.channel.send(embed=embed, view=TicketPanelView(self))
        if ctx.interaction:
            await ctx.send("✅ Ticket panel posted.", ephemeral=True)

    @commands.hybrid_command(name="close", description="Close the current ticket")
    @app_commands.describe(transcript="Save a transcript to the log channel? (default: yes)")
    async def close_cmd(self, ctx: commands.Context, transcript: bool = True):
        owner_id = ticket_owner_id(ctx.channel)
        if owner_id is None:
            await ctx.send("This isn't a ticket channel.", ephemeral=True)
            return
        if not (is_staff(ctx.author) or ctx.author.id == owner_id):
            await ctx.send("Only staff or the ticket owner can close this.", ephemeral=True)
            return
        if ctx.interaction:
            await ctx.send("🔒 Closing…", ephemeral=True)
        await self.close(ctx.channel, ctx.author, transcript=transcript)

    @commands.hybrid_command(name="claim", description="Claim the current ticket (staff)")
    async def claim_cmd(self, ctx: commands.Context):
        await self.claim(ctx.channel, ctx.author, ctx.send)

    @commands.hybrid_command(name="adduser", description="Add a member to the current ticket (staff)")
    @app_commands.describe(member="Member to add")
    @staff_only()
    async def adduser(self, ctx: commands.Context, member: discord.Member):
        if ticket_owner_id(ctx.channel) is None:
            await ctx.send("This isn't a ticket channel.", ephemeral=True)
            return
        await ctx.channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True, attach_files=True,
            reason=f"Added to ticket by {ctx.author}",
        )
        await ctx.send(f"➕ {member.mention} was added to the ticket.")
        logger.info("%s added %s to #%s", ctx.author, member, ctx.channel.name)

    @commands.hybrid_command(name="removeuser", description="Remove a member from the current ticket (staff)")
    @app_commands.describe(member="Member to remove")
    @staff_only()
    async def removeuser(self, ctx: commands.Context, member: discord.Member):
        owner_id = ticket_owner_id(ctx.channel)
        if owner_id is None:
            await ctx.send("This isn't a ticket channel.", ephemeral=True)
            return
        if member.id == owner_id:
            await ctx.send("You can't remove the ticket owner. Close the ticket instead.", ephemeral=True)
            return
        await ctx.channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {ctx.author}")
        await ctx.send(f"➖ {member.mention} was removed from the ticket.")
        logger.info("%s removed %s from #%s", ctx.author, member, ctx.channel.name)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
