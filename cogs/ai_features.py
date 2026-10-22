"""AI features on xAI (Grok): /ask, /imagine, AI auto-moderation and /ai-usage.

Everything needs only XAI_API_KEY; without it each feature replies with a
friendly "AI is off" message and the automod listener does nothing.

Cost controls: a cheap text model with a 400-token cap, per-user cooldowns and
daily limits, a server-wide daily image cap, and automod only asks the AI about
messages a keyword/regex pre-filter already found suspicious. Every AI call is
logged in the ai_usage table.
"""

import io
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from utils import ai
from utils import guildconfig as gc
from utils.helpers import is_staff, send_log, staff_only, trim

logger = logging.getLogger("bot.ai_features")

ASK_COOLDOWN = 30          # seconds between /ask per user
ASK_DAILY_LIMIT = config._int("ASK_DAILY_LIMIT", 30)
IMAGINE_COOLDOWN = 120     # seconds between /imagine per user
IMAGINE_DAILY_LIMIT = config._int("IMAGINE_DAILY_LIMIT", 5)
IMAGINE_GUILD_DAILY_CAP = config._int("IMAGINE_GUILD_DAILY_CAP", 50)  # per server; /imagine-cap overrides
AUTOMOD_USER_COOLDOWN = 10  # seconds between AI checks of the same user's messages (cost guard)
AUTOMOD_IMAGE_COOLDOWN = 30  # seconds between AI image checks per user (vision calls cost more)
AUTOMOD_CONTEXT_MESSAGES = 3  # previous messages shown to the AI as context
IMAGE_TYPES = ("image/png", "image/jpeg", "image/jpg")  # what the vision model accepts
IMAGE_MAX_BYTES = 20 * 1024 * 1024
MODES = {0: "delete", 1: "warn"}
ASK_REPLY_LIMIT = 1800
ASK_MEMORY_TURNS = max(0, config._int("ASK_MEMORY_TURNS", 6))    # exchanges remembered per channel/thread
ASK_MEMORY_HOURS = max(1, config._int("ASK_MEMORY_HOURS", 24))   # older messages are forgotten

AI_OFF = "🤖 AI features are switched off on this bot right now. Ask an admin to add an xAI key."

ASK_SYSTEM = (
    "You are the friendly helper bot of a gaming Discord server called {server}. Answer members' questions "
    "about games, tech, creative projects and general topics clearly and briefly (under 1,500 characters; "
    "short paragraphs or a few bullets). Keep everything safe-for-work and kind. If a request is sexual, "
    "NSFW, hateful, or asks for harmful or illegal help, politely refuse in one sentence and suggest "
    "something else. Never use @everyone or @here. Earlier messages in this conversation may come from different "
    "members; each user message starts with their name."
)

# --------------------------------------------------------------- filtering
# Words that make an /imagine prompt (or an automod pre-filter hit) clearly NSFW.
NSFW_WORDS = [
    "nsfw", "nude", "nudes", "naked", "porn", "porno", "pornographic", "hentai", "sex", "sexy", "sexual",
    "xxx", "boobs", "boob", "tits", "titty", "nipple", "nipples", "genital", "genitals", "penis", "vagina",
    "dick", "cock", "pussy", "cum", "orgasm", "erotic", "fetish", "bdsm", "lingerie", "topless", "bottomless",
    "undressed", "onlyfans", "stripper", "blowjob", "handjob", "milf", "rule34", "r34", "lewd", "ecchi",
    "gore", "gory", "dismember", "dismembered", "decapitated", "beheading", "loli", "shota",
]
NSFW_RE = re.compile(r"\b(" + "|".join(map(re.escape, NSFW_WORDS)) + r")\b", re.IGNORECASE)

# Automod pre-filter. Each level includes the ones below it.
_SLURS = r"\b(n[i1!]gg(?:a|er|uh)s?|f[a@]gg?(?:ot)?s?|tr[a@]nn(?:y|ies)|r[e3]t[a@]rds?|k[i1]kes?|ch[i1]nks?|sp[i1]cs?)\b"
_THREATS = r"\b(kys|kill (?:your|ur) ?self|neck yourself|go die|i(?:'ll| will) (?:kill|find) you)\b"
_SCAMS = r"(free\s*nitro|nitro\s*(?:gift|giveaway)|steam\s*gift|claim (?:your|ur) (?:prize|reward)|@everyone.*https?://)"
_INVITES = r"(discord(?:app)?\.(?:gg|com/invite)/\w+)"
_INSULTS = r"\b(fuck (?:you|u|off)|stfu|idiot|moron|loser|trash player|piece of shit|bitch|whore|slut|cunt)\b"
_PROFANITY = r"\b(fuck\w*|shit\w*|asshole|bastard|dumbass|wtf)\b"

_LOW = [("slur", re.compile(_SLURS, re.I)), ("threat", re.compile(_THREATS, re.I)),
        ("scam", re.compile(_SCAMS, re.I)), ("nsfw", NSFW_RE)]
_MED = _LOW + [("invite link", re.compile(_INVITES, re.I)), ("insult", re.compile(_INSULTS, re.I))]
_HIGH = _MED + [("profanity", re.compile(_PROFANITY, re.I))]
PREFILTER: dict[int, list[tuple[str, re.Pattern]]] = {1: _LOW, 2: _MED, 3: _HIGH}
SENSITIVITY_NAMES = {1: "low", 2: "med", 3: "high"}
SENSITIVITY_VALUES = {v: k for k, v in SENSITIVITY_NAMES.items()}
_REPEAT = re.compile(r"(.)\1{14,}")


def spam_reason(content: str, mentions: int, level: int) -> Optional[str]:
    if mentions >= (8 if level == 1 else 5 if level == 2 else 4):
        return "mass mentions"
    if level >= 2 and _REPEAT.search(content):
        return "repeated characters"
    letters = [c for c in content if c.isalpha()]
    if level >= 3 and len(letters) >= 30 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
        return "all caps"
    return None


def prefilter(content: str, mentions: int, level: int) -> Optional[str]:
    """Cheap first pass. Returns why the message looks suspicious, or None if it looks fine."""
    for reason, pattern in PREFILTER.get(level, _MED):
        if pattern.search(content):
            return reason
    return spam_reason(content, mentions, level)


def checkable_images(message) -> list:
    """PNG/JPEG attachments the vision model can check."""
    out = []
    for att in getattr(message, "attachments", None) or []:
        ctype = (getattr(att, "content_type", "") or "").split(";")[0].lower()
        name = (getattr(att, "filename", "") or "").lower()
        if (ctype in IMAGE_TYPES or name.endswith((".png", ".jpg", ".jpeg"))) and \
                (getattr(att, "size", 0) or 0) <= IMAGE_MAX_BYTES:
            out.append(att)
    return out


def nsfw_prompt(prompt: str) -> bool:
    return bool(NSFW_RE.search(prompt))


# ------------------------------------------------------------------ limits
class Cooldowns:
    """In-memory per-user cooldowns (reset on restart; daily limits live in the DB)."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        self.last: dict[tuple[int, int], float] = {}

    def remaining(self, guild_id: int, user_id: int, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self.last.get((guild_id, user_id), -1e9) + self.seconds - now)

    def hit(self, guild_id: int, user_id: int, now: Optional[float] = None) -> None:
        self.last[(guild_id, user_id)] = time.monotonic() if now is None else now

    def reset(self, guild_id: int, user_id: int) -> None:
        self.last.pop((guild_id, user_id), None)


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def day_start(at: Optional[datetime] = None) -> int:
    """Unix time of local midnight today (TIMEZONE); daily limits reset then."""
    local = at.astimezone(_tz()) if at else datetime.now(_tz())
    return int(local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def month_start(at: Optional[datetime] = None) -> int:
    local = at.astimezone(_tz()) if at else datetime.now(_tz())
    return int(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def setting(guild, key: str, default: int) -> int:
    """A plain (non-ID) per-server setting saved in guild_config."""
    value = gc.saved(guild).get(key)
    return default if value is None else value


def _named(guild: discord.Guild, name: str):
    return discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and c.name == name, guild.text_channels)


def ask_channel(guild: discord.Guild):
    """Where /ask works: ASK_CHANNEL_ID (/setup-link), else #bot-commands."""
    return gc.channel(guild, "ASK_CHANNEL_ID") or _named(guild, "bot-commands")


def imagine_channels(guild: discord.Guild) -> list:
    found = [gc.channel(guild, "AI_ART_CHANNEL_ID") or _named(guild, "ai-art"),
             gc.channel(guild, "BOT_COMMANDS_CHANNEL_ID") or _named(guild, "bot-commands")]
    return [c for c in found if c is not None]


def _in(channel, allowed) -> bool:
    """True if `channel` is one of `allowed` or a thread inside one."""
    ids = {c.id for c in allowed}
    return channel.id in ids or getattr(channel, "parent_id", None) in ids


# ---------------------------------------------------------- automod buttons
class _FlagButton(discord.ui.DynamicItem[discord.ui.Button], template=r"automod:(?P<action>undo|timeout):(?P<flag>\d+)"):
    """Persistent mod-log buttons (survive restarts): the flag ID is in the custom_id."""

    def __init__(self, action: str, flag_id: int):
        label, style, emoji = {
            "undo": ("Undo", discord.ButtonStyle.secondary, "↩️"),
            "timeout": ("Timeout 10m", discord.ButtonStyle.danger, "🔇"),
        }[action]
        super().__init__(discord.ui.Button(
            label=label, style=style, emoji=emoji, custom_id=f"automod:{action}:{flag_id}"
        ))
        self.action, self.flag_id = action, flag_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(match["action"], int(match["flag"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_staff(interaction.user):
            await interaction.response.send_message("🚫 Staff only.", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        flag = await db.get_flag(self.flag_id)
        if flag is None or flag["guild_id"] != interaction.guild_id:
            await interaction.response.send_message("That flag no longer exists.", ephemeral=True)
            return
        guild = interaction.guild
        if self.action == "undo":
            await self._undo(interaction, db, flag, guild)
        else:
            await self._timeout(interaction, flag, guild)

    async def _undo(self, interaction, db, flag, guild):
        if not await db.mark_flag_undone(flag["id"]):
            await interaction.response.send_message("Already undone.", ephemeral=True)
            return
        channel = guild.get_channel_or_thread(flag["channel_id"])
        restored = False
        warn_only = (flag["mode"] if "mode" in flag.keys() else "delete") == "warn"
        if warn_only:
            channel = None  # the message was never deleted: just record the false positive
        if channel is not None:
            embed = discord.Embed(description=trim(flag["content"], 4000), color=discord.Color.light_grey())
            member = guild.get_member(flag["user_id"])
            if member:
                embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
            embed.set_footer(text="Restored by staff: removed by mistake by auto-moderation")
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                restored = True
            except discord.HTTPException:
                pass
        logger.info("%s undid automod flag %s in %s", interaction.user, flag["id"], guild)
        await interaction.response.send_message(
            "↩️ Undone: " + ("the message was re-posted in its channel." if restored
                             else "marked as a false positive." if warn_only
                             else "marked as a false positive (I couldn't re-post it)."),
            ephemeral=True,
        )
        if interaction.message and interaction.message.embeds:
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.light_grey()
            embed.add_field(name="Undone", value=f"by {interaction.user.mention}", inline=False)
            try:
                await interaction.message.edit(embed=embed)
            except discord.HTTPException:
                pass

    async def _timeout(self, interaction, flag, guild):
        member = guild.get_member(flag["user_id"])
        if member is None:
            await interaction.response.send_message("That member isn't in the server anymore.", ephemeral=True)
            return
        if not guild.me.guild_permissions.moderate_members or member.top_role >= guild.me.top_role:
            await interaction.response.send_message("⚠️ I can't time out that member (permissions/role order).",
                                                    ephemeral=True)
            return
        if is_staff(member) or member == guild.owner:
            await interaction.response.send_message("🚫 That member is staff.", ephemeral=True)
            return
        try:
            await member.timeout(timedelta(minutes=10), reason=f"{interaction.user}: AI automod flag #{flag['id']}")
        except discord.HTTPException:
            await interaction.response.send_message("⚠️ Discord refused the timeout.", ephemeral=True)
            return
        logger.info("%s timed out %s for 10m from automod flag %s", interaction.user, member, flag["id"])
        await interaction.response.send_message(f"🔇 {member.mention} is timed out for 10 minutes.", ephemeral=True)
        embed = discord.Embed(title="🔇 Member Timed Out", color=discord.Color.dark_orange(),
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=f"{member.mention} (`{member}` • {member.id})", inline=False)
        embed.add_field(name="Moderator", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=False)
        embed.add_field(name="Reason", value=f"AI automod flag #{flag['id']}", inline=False)
        embed.add_field(name="Duration", value="10m", inline=True)
        await send_log(guild, gc.get(guild, "MOD_LOG_CHANNEL_ID"), embed)


def flag_view(flag_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(_FlagButton("undo", flag_id))
    view.add_item(_FlagButton("timeout", flag_id))
    return view


class ContinueView(discord.ui.View):
    """"Continue in thread" under an /ask answer (not persistent: works for 30 minutes)."""

    def __init__(self, cog: "AIFeatures", question: str):
        super().__init__(timeout=1800)
        self.cog, self.question = cog, question

    @discord.ui.button(label="Continue in thread", style=discord.ButtonStyle.secondary, emoji="🧵")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.continue_in_thread(interaction, self.question)


# ------------------------------------------------------------------------ cog
class AIFeatures(commands.Cog):
    """/ask, /imagine, AI auto-moderation and usage tracking."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ask_cd = Cooldowns(ASK_COOLDOWN)
        self.imagine_cd = Cooldowns(IMAGINE_COOLDOWN)
        self.automod_cd = Cooldowns(AUTOMOD_USER_COOLDOWN)
        self.image_cd = Cooldowns(AUTOMOD_IMAGE_COOLDOWN)

    async def cog_load(self):
        self.bot.add_dynamic_items(_FlagButton)
        self.prune_loop.start()

    async def cog_unload(self):
        self.bot.remove_dynamic_items(_FlagButton)
        self.prune_loop.cancel()

    @tasks.loop(hours=1)
    async def prune_loop(self):
        try:
            removed = await self.bot.db.prune_memory(int(time.time()) - ASK_MEMORY_HOURS * 3600)
            if removed:
                logger.info("Forgot %d expired /ask memory message(s)", removed)
        except Exception:
            logger.exception("Could not prune /ask memory")

    # ---------------------------------------------------------------- /ask
    async def ask_block(self, ctx_or_guild, channel=None, author=None) -> Optional[str]:
        """Why this /ask (or @mention chat) can't run now, or None. Pass a Context, or (guild, channel, author)."""
        if channel is None:
            guild, channel, author = ctx_or_guild.guild, ctx_or_guild.channel, ctx_or_guild.author
        else:
            guild = ctx_or_guild
        if not ai.key_set():
            return AI_OFF
        allowed = ask_channel(guild)
        if allowed is None:
            return "🤖 `/ask` has no channel yet. An admin can set one with `/setup-link key:ASK_CHANNEL_ID`."
        if not _in(channel, [allowed]) and not is_staff(author):
            return f"🤖 `/ask` works in {allowed.mention}."
        wait = self.ask_cd.remaining(guild.id, author.id)
        if wait:
            return f"⏳ Slow down! Try `/ask` again in {wait:.0f}s."
        used = await self.bot.db.count_ai(guild.id, "ask", day_start(), author.id)
        if used >= ASK_DAILY_LIMIT:
            return f"📅 You've used all {ASK_DAILY_LIMIT} `/ask` questions for today. They reset at midnight."
        return None

    async def memory(self, guild_id: int, scope_id: int) -> list[dict]:
        if not ASK_MEMORY_TURNS:
            return []
        rows = await self.bot.db.get_memory(guild_id, scope_id, int(time.time()) - ASK_MEMORY_HOURS * 3600,
                                            ASK_MEMORY_TURNS * 2)
        while rows and rows[0]["role"] != "user":
            rows = rows[1:]  # always start with a question
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def chat_reply(self, guild, channel, author, question: str) -> Optional[ai.Reply]:
        """Answer with the channel/thread's recent conversation as context, and remember the exchange."""
        question = trim(question, 1000)
        history = await self.memory(guild.id, channel.id)
        user_turn = f"{author.display_name}: {question}"
        reply = await ai.converse(history + [{"role": "user", "content": user_turn}],
                                  ASK_SYSTEM.format(server=guild.name), feature="ask")
        await self.bot.db.log_ai(guild.id, author.id, "ask", reply.tokens if reply else 0, ok=bool(reply))
        if reply is not None and ASK_MEMORY_TURNS:
            await self.bot.db.add_memory(guild.id, channel.id, author.id, "user", user_turn)
            await self.bot.db.add_memory(guild.id, channel.id, getattr(getattr(self.bot, "user", None), "id", 0),
                                         "assistant", trim(reply.text, ASK_REPLY_LIMIT))
        return reply

    def ask_embed(self, author, question: str, text: str) -> discord.Embed:
        embed = discord.Embed(description=trim(text, ASK_REPLY_LIMIT), color=discord.Color.blurple())
        embed.set_author(name=trim(f"{author.display_name} asked: {question}", 256), icon_url=author.display_avatar.url)
        embed.set_footer(text="✨ AI-generated • can be wrong • /ask reset to start over")
        return embed

    @commands.hybrid_group(name="ask", description="Ask the bot anything (gaming, tech, creative)", fallback="chat")
    @app_commands.describe(question="Your question")
    async def ask(self, ctx: commands.Context, *, question: str):
        if problem := await self.ask_block(ctx):
            await ctx.send(problem, ephemeral=True)
            return
        self.ask_cd.hit(ctx.guild.id, ctx.author.id)
        await ctx.defer()
        question = trim(question, 1000)
        reply = await self.chat_reply(ctx.guild, ctx.channel, ctx.author, question)
        if reply is None:
            self.ask_cd.reset(ctx.guild.id, ctx.author.id)  # our fault, not theirs
            await ctx.send("😵 I couldn't reach the AI just now. Try again in a minute.", ephemeral=True)
            return
        view = ContinueView(self, question) if isinstance(ctx.channel, discord.TextChannel) else None
        kwargs = {"view": view} if view else {}
        await ctx.send(embed=self.ask_embed(ctx.author, question, reply.text),
                       allowed_mentions=discord.AllowedMentions.none(), **kwargs)

    @ask.command(name="reset", description="Make the bot forget this channel's /ask conversation")
    async def ask_reset(self, ctx: commands.Context):
        if isinstance(ctx.channel, discord.TextChannel) and not is_staff(ctx.author) and ask_channel(ctx.guild) != ctx.channel:
            await ctx.send("🤖 Nothing to reset here.", ephemeral=True)
            return
        removed = await self.bot.db.clear_memory(ctx.guild.id, ctx.channel.id)
        logger.info("%s reset /ask memory in %s (%d messages)", ctx.author, ctx.channel, removed)
        await ctx.send("🧹 Done: I've forgotten our conversation here. Fresh start!", ephemeral=True)

    async def continue_in_thread(self, interaction: discord.Interaction, question: str) -> None:
        message = interaction.message
        if message is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("I can only start a thread from a channel.", ephemeral=True)
            return
        try:
            thread = await message.create_thread(name=trim(question, 90) or "AI chat", auto_archive_duration=1440)
        except discord.HTTPException as e:
            logger.warning("Could not create /ask thread in %s: %s", interaction.channel, e)
            await interaction.response.send_message("⚠️ I couldn't create a thread here (I need **Create Public "
                                                    "Threads**), or one already exists.", ephemeral=True)
            return
        # Carry the conversation over so the thread starts with context.
        for turn in (await self.memory(interaction.guild_id, interaction.channel.id))[-ASK_MEMORY_TURNS * 2:]:
            await self.bot.db.add_memory(interaction.guild_id, thread.id, interaction.user.id, turn["role"], turn["content"])
        await interaction.response.send_message(f"🧵 Continue in {thread.mention}!", ephemeral=True)
        try:
            await thread.send(f"Keep going here, {interaction.user.mention}: mention me or use `/ask` and I'll remember "
                              "this conversation. `/ask reset` clears it.",
                              allowed_mentions=discord.AllowedMentions(users=[interaction.user]))
        except discord.HTTPException:
            pass

    # --------------------------------------------------------- @mention chat
    def mention_question(self, message: discord.Message) -> Optional[str]:
        """The question in a message that @mentions the bot, or None (not for us / it's a prefix command)."""
        me = getattr(self.bot, "user", None)
        if message.guild is None or message.author.bot or message.webhook_id or me is None:
            return None
        if not any(u.id == me.id for u in message.mentions):
            return None
        text = re.sub(r"\s+", " ", re.sub(rf"<@!?{me.id}>", "", message.content)).strip()
        first = text.split(maxsplit=1)[0].lower() if text else ""
        if first and self.bot.get_command(first.lstrip(config.COMMAND_PREFIX)) is not None and \
                re.match(rf"^\s*<@!?{me.id}>", message.content):
            return None  # "@bot help" is a prefix command, handled by discord.py
        return text or None

    @commands.Cog.listener("on_message")
    async def mention_chat(self, message: discord.Message):
        question = self.mention_question(message)
        if question is None:
            return
        guild, channel, author = message.guild, message.channel, message.author
        if not ai.key_set():
            return
        problem = await self.ask_block(guild, channel, author)
        if problem is not None:
            if problem.startswith(("⏳", "📅")):  # tell them; channel problems stay silent (no spam)
                try:
                    await message.reply(problem, delete_after=15, mention_author=False)
                except discord.HTTPException:
                    pass
            return
        self.ask_cd.hit(guild.id, author.id)
        async with channel.typing():
            reply = await self.chat_reply(guild, channel, author, question)
        if reply is None:
            self.ask_cd.reset(guild.id, author.id)
            text = "😵 I couldn't reach the AI just now. Try again in a minute."
        else:
            text = trim(reply.text, 1990)
        try:
            await message.reply(text, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            logger.warning("Could not reply to a mention in %s: %s", channel, e)

    # ------------------------------------------------------------- /imagine
    async def imagine_block(self, ctx: commands.Context) -> Optional[str]:
        if not ai.key_set():
            return AI_OFF
        allowed = imagine_channels(ctx.guild)
        if not _in(ctx.channel, allowed):
            where = " or ".join(c.mention for c in allowed) or "#ai-art or #bot-commands (run `/setup`)"
            return f"🎨 `/imagine` works in {where}."
        wait = self.imagine_cd.remaining(ctx.guild.id, ctx.author.id)
        if wait:
            return f"⏳ Easy, artist! Try `/imagine` again in {wait:.0f}s."
        since = day_start()
        if await self.bot.db.count_ai(ctx.guild.id, "imagine", since, ctx.author.id) >= IMAGINE_DAILY_LIMIT:
            return f"📅 You've made all {IMAGINE_DAILY_LIMIT} images for today. They reset at midnight."
        cap = setting(ctx.guild, "IMAGINE_DAILY_CAP", IMAGINE_GUILD_DAILY_CAP)
        if await self.bot.db.count_ai(ctx.guild.id, "imagine", since) >= cap:
            return f"📅 The server has used all {cap} images for today. Back tomorrow!"
        return None

    @commands.hybrid_command(name="imagine", description="Generate an image with AI")
    @app_commands.describe(prompt="Describe the image (keep it SFW)")
    async def imagine(self, ctx: commands.Context, *, prompt: str):
        if problem := await self.imagine_block(ctx):
            await ctx.send(problem, ephemeral=True)
            return
        prompt = trim(prompt, 500)
        if nsfw_prompt(prompt):
            await ctx.send("🚫 That prompt isn't allowed here. Keep it safe-for-work, please!", ephemeral=True)
            return
        self.imagine_cd.hit(ctx.guild.id, ctx.author.id)
        await ctx.defer()

        check = await ai.classify(f"Image generation prompt: {prompt}")
        await self.bot.db.log_ai(ctx.guild.id, ctx.author.id, "imagine-check", check.tokens if check else 0,
                                 ok=bool(check))
        if check is None:
            self.imagine_cd.reset(ctx.guild.id, ctx.author.id)
            await ctx.send("😵 I couldn't check that prompt just now. Try again in a minute.", ephemeral=True)
            return
        if check.text != "ok":
            logger.info("Blocked /imagine prompt from %s in %s (%s): %r", ctx.author, ctx.guild, check.text, prompt)
            await ctx.send("🚫 That prompt isn't allowed here. Keep it safe-for-work, please!", ephemeral=True)
            return

        data = await ai.image(prompt)
        await self.bot.db.log_ai(ctx.guild.id, ctx.author.id, "imagine", images=1 if data else 0, ok=bool(data))
        if data is None:
            self.imagine_cd.reset(ctx.guild.id, ctx.author.id)
            await ctx.send("😵 Image generation failed. Try again in a minute.", ephemeral=True)
            return
        ext = "jpg" if data[:3] == b"\xff\xd8\xff" else "png"
        file = discord.File(io.BytesIO(data), filename=f"imagine.{ext}")
        embed = discord.Embed(title="🎨 /imagine", description=trim(prompt, 1000), color=discord.Color.purple())
        embed.set_image(url=f"attachment://imagine.{ext}")
        embed.set_footer(text=f"Requested by {ctx.author.display_name} • ✨ AI-generated",
                         icon_url=ctx.author.display_avatar.url)
        await ctx.send(embed=embed, file=file)

    # ------------------------------------------------------------ automod
    def automod_skip(self, message: discord.Message) -> bool:
        guild = message.guild
        if guild is None or message.author.bot or message.webhook_id:
            return True
        if not message.content and not checkable_images(message):
            return True
        if not setting(guild, "AUTOMOD_ENABLED", 0) or not ai.key_set():
            return True
        if not isinstance(message.author, discord.Member) or is_staff(message.author):
            return True
        channel = message.channel
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        if not isinstance(base, discord.TextChannel):
            return True
        if isinstance(channel, discord.Thread) and channel.is_private():
            return True
        if not base.permissions_for(guild.default_role).view_channel:
            return True  # private/staff channels (mod-log, tickets, ...)
        skip_ids = {gc.get(guild, "MOD_LOG_CHANNEL_ID"), gc.get(guild, "LOG_CHANNEL_ID"),
                    gc.get(guild, "TICKET_LOG_CHANNEL_ID")}
        if base.id in skip_ids or (base.category_id and base.category_id == gc.get(guild, "TICKET_CATEGORY_ID")):
            return True
        return False

    async def context_for(self, message: discord.Message) -> list[str]:
        """The few messages before this one ("name: text"), so the AI sees banter vs. harassment."""
        lines = []
        try:
            async for prev in message.channel.history(limit=AUTOMOD_CONTEXT_MESSAGES, before=message):
                text = (prev.content or "").strip()
                if text:
                    lines.append(f"{getattr(prev.author, 'display_name', 'someone')}: {text[:300]}")
        except (discord.HTTPException, AttributeError):
            return []
        return lines[::-1]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if self.automod_skip(message):
            return
        guild = message.guild
        if message.content:
            flagged = await self.check_text(message)
            if flagged:
                return
        images = checkable_images(message)
        if images and setting(guild, "AUTOMOD_IMAGES", 0):
            await self.check_images(message, images)

    async def check_text(self, message: discord.Message) -> bool:
        guild = message.guild
        level = setting(guild, "AUTOMOD_SENSITIVITY", 2)
        mentions = len(message.raw_mentions) + len(message.raw_role_mentions)
        reason = prefilter(message.content, mentions, level)
        if reason is None:
            return False
        if self.automod_cd.remaining(guild.id, message.author.id):
            return False  # cost guard: at most one AI check per user per few seconds
        self.automod_cd.hit(guild.id, message.author.id)

        context = await self.context_for(message)
        check = await ai.classify(message.content, context=context)
        await self.bot.db.log_ai(guild.id, message.author.id, "automod", check.tokens if check else 0, ok=bool(check))
        if check is None or check.text == "ok":
            return False
        await self.act_on_flag(message, reason, check.text)
        return True

    async def check_images(self, message: discord.Message, images: list) -> bool:
        guild = message.guild
        if self.image_cd.remaining(guild.id, message.author.id):
            return False
        self.image_cd.hit(guild.id, message.author.id)
        for att in images[:2]:  # cost guard: at most two images per message
            check = await ai.classify_image(att.url, message.content or "")
            await self.bot.db.log_ai(guild.id, message.author.id, "automod-image", check.tokens if check else 0,
                                     ok=bool(check))
            if check is not None and check.text != "ok":
                await self.act_on_flag(message, "image", check.text, kind="image")
                return True
        return False

    async def act_on_flag(self, message: discord.Message, reason: str, verdict: str, kind: str = "text") -> None:
        guild, author = message.guild, message.author
        mode = MODES.get(setting(guild, "AUTOMOD_MODE", 0), "delete")
        deleted = False
        if mode == "delete":
            try:
                await message.delete()
                deleted = True
            except discord.NotFound:
                deleted = True  # already gone
            except discord.HTTPException:
                deleted = False
        content = message.content or ""
        if kind == "image":
            content = (content + "\n" + "\n".join(a.url for a in checkable_images(message))).strip()
        flag_id = await self.bot.db.add_flag(guild.id, message.channel.id, author.id, content, reason, verdict,
                                             mode=mode, kind=kind)
        logger.info("Automod flag #%s in %s: %s (%s / %s, %s)", flag_id, guild, author, reason, verdict, mode)

        try:
            if mode == "warn":
                await author.send(
                    f"⚠️ Heads up: your message in **{guild.name}** was flagged by auto-moderation ({verdict}). "
                    "Please keep chat friendly and safe-for-work. Staff have been notified."
                )
            else:
                await author.send(
                    f"⚠️ Your message in **{guild.name}** was removed by auto-moderation ({verdict}). "
                    "Please keep chat friendly and safe-for-work. If this was a mistake, staff can restore it."
                )
            dm = "DM delivered"
        except discord.HTTPException:
            dm = "DM failed (closed DMs)"

        title = "🛡️ AI Auto-moderation" + (" (warn only)" if mode == "warn" else "")
        embed = discord.Embed(title=title, color=discord.Color.orange() if mode == "warn" else discord.Color.red(),
                              timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=f"{author.mention} (`{author}` • {author.id})", inline=False)
        embed.add_field(name="Channel", value=message.channel.mention, inline=True)
        embed.add_field(name="Pre-filter", value=reason, inline=True)
        embed.add_field(name="AI verdict", value=verdict, inline=True)
        embed.add_field(name="Message", value=trim(content or "(no text)", 1024), inline=False)
        if mode == "warn" and getattr(message, "jump_url", None):
            embed.add_field(name="Jump", value=message.jump_url, inline=False)
        embed.set_thumbnail(url=author.display_avatar.url)
        state = ("not deleted (warn-only mode)" if mode == "warn" else
                 "deleted" if deleted else "could NOT delete (missing Manage Messages)")
        embed.set_footer(text=f"Flag #{flag_id} • {state} • {dm}")
        channel = gc.channel(guild, "MOD_LOG_CHANNEL_ID")
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(embed=embed, view=flag_view(flag_id))
            except discord.HTTPException as e:
                logger.warning("Could not post automod flag to mod log in %s: %s", guild, e)

    @commands.hybrid_group(name="automod", description="AI auto-moderation settings (admin)", fallback="status")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def automod(self, ctx: commands.Context):
        on = bool(setting(ctx.guild, "AUTOMOD_ENABLED", 0))
        level = SENSITIVITY_NAMES[setting(ctx.guild, "AUTOMOD_SENSITIVITY", 2)]
        mode = MODES.get(setting(ctx.guild, "AUTOMOD_MODE", 0), "delete")
        images = "on" if setting(ctx.guild, "AUTOMOD_IMAGES", 0) else "off"
        note = "" if ai.key_set() else "\n⚠️ No xAI key is set, so it can't run until one is added."
        await ctx.send(f"🛡️ AI auto-moderation is **{'on' if on else 'off'}**, sensitivity **{level}**, "
                       f"mode **{mode}**, image checks **{images}**.{note}", ephemeral=True)

    @automod.command(name="on", description="Turn AI auto-moderation on (admin)")
    @commands.has_permissions(administrator=True)
    async def automod_on(self, ctx: commands.Context):
        await gc.set_many(ctx.guild, {"AUTOMOD_ENABLED": 1})
        logger.info("%s turned automod on in %s", ctx.author, ctx.guild)
        msg = "🛡️ AI auto-moderation is **on**."
        if not ai.key_set():
            msg += "\n⚠️ No xAI key is set, so it won't do anything until one is added."
        if gc.channel(ctx.guild, "MOD_LOG_CHANNEL_ID") is None:
            msg += "\n⚠️ No mod-log channel is set, so flags won't be logged. Run `/setup` or `/setup-link`."
        if not ctx.guild.me.guild_permissions.manage_messages:
            msg += "\n⚠️ I need **Manage Messages** to delete flagged messages."
        await ctx.send(msg, ephemeral=True)

    @automod.command(name="off", description="Turn AI auto-moderation off (admin)")
    @commands.has_permissions(administrator=True)
    async def automod_off(self, ctx: commands.Context):
        await gc.set_many(ctx.guild, {"AUTOMOD_ENABLED": 0})
        logger.info("%s turned automod off in %s", ctx.author, ctx.guild)
        await ctx.send("🛡️ AI auto-moderation is **off**.", ephemeral=True)

    @automod.command(name="sensitivity", description="How strict the pre-filter is (admin)")
    @app_commands.describe(level="low: slurs/threats/scams/NSFW · med: + insults, invites · high: + any swearing")
    @commands.has_permissions(administrator=True)
    async def automod_sensitivity(self, ctx: commands.Context, level: Literal["low", "med", "high"]):
        await gc.set_many(ctx.guild, {"AUTOMOD_SENSITIVITY": SENSITIVITY_VALUES[level]})
        logger.info("%s set automod sensitivity to %s in %s", ctx.author, level, ctx.guild)
        await ctx.send(f"🛡️ Auto-moderation sensitivity is now **{level}**.", ephemeral=True)

    @automod.command(name="mode", description="delete flagged messages, or warn only (admin)")
    @app_commands.describe(mode="delete: remove + DM + log · warn: keep the message, DM a warning + log")
    @commands.has_permissions(administrator=True)
    async def automod_mode(self, ctx: commands.Context, mode: Literal["delete", "warn"]):
        await gc.set_many(ctx.guild, {"AUTOMOD_MODE": 1 if mode == "warn" else 0})
        logger.info("%s set automod mode to %s in %s", ctx.author, mode, ctx.guild)
        text = ("flagged messages stay up; the author gets a warning DM and staff get the flag in the mod log."
                if mode == "warn" else "flagged messages are deleted, the author gets a DM and staff can Undo.")
        await ctx.send(f"🛡️ Auto-moderation mode is now **{mode}**: {text}", ephemeral=True)

    @automod.command(name="images", description="Check posted images with the AI vision model (admin)")
    @commands.has_permissions(administrator=True)
    async def automod_images(self, ctx: commands.Context, enabled: Literal["on", "off"]):
        await gc.set_many(ctx.guild, {"AUTOMOD_IMAGES": int(enabled == "on")})
        logger.info("%s turned automod image checks %s in %s", ctx.author, enabled, ctx.guild)
        msg = f"🖼️ AI image checks are **{enabled}**."
        if enabled == "on":
            msg += (f" PNG/JPEG uploads are checked with `{ai.XAI_VISION_MODEL}` (max 2 per message, one check per "
                    f"member every {AUTOMOD_IMAGE_COOLDOWN}s). Automod itself must be on too.")
        await ctx.send(msg, ephemeral=True)

    @automod.command(name="stats", description="Automod flags and how many staff undid (false positives) (admin)")
    @app_commands.describe(days="How many days back (default 30)")
    @commands.has_permissions(administrator=True)
    async def automod_stats(self, ctx: commands.Context, days: commands.Range[int, 1, 365] = 30):
        rows = await self.bot.db.automod_stats(ctx.guild.id, int(time.time()) - int(days) * 86400)
        embed = discord.Embed(title=f"🛡️ Automod stats: last {days} day(s)", color=discord.Color.blurple())
        total = sum(r["flags"] for r in rows)
        undone = sum(r["undone"] or 0 for r in rows)
        if not total:
            embed.description = "No flags yet."
        else:
            rate = undone / total * 100
            embed.description = (f"**{total}** flag(s), **{undone}** undone by staff (**{rate:.0f}%** false positives)."
                                 + ("\nTip: lots of Undo clicks? Try `/automod sensitivity low` or `/automod mode warn`."
                                    if total >= 5 and rate >= 30 else ""))
            by_reason: dict[str, list[int]] = {}
            by_verdict: dict[str, list[int]] = {}
            for r in rows:
                for bucket, key in ((by_reason, f"{r['reason']}" + (" (image)" if r["kind"] == "image" else "")),
                                    (by_verdict, r["verdict"])):
                    entry = bucket.setdefault(key, [0, 0])
                    entry[0] += r["flags"]
                    entry[1] += r["undone"] or 0

            def fmt(bucket):
                return "\n".join(f"**{k}**: {n} flag(s), {u} undone ({u / n * 100:.0f}%)"
                                 for k, (n, u) in sorted(bucket.items(), key=lambda kv: -kv[1][0]))
            embed.add_field(name="By pre-filter reason", value=trim(fmt(by_reason), 1024), inline=False)
            embed.add_field(name="By AI verdict", value=trim(fmt(by_verdict), 1024), inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------- admin: usage
    @commands.hybrid_command(name="imagine-cap", description="Set the server-wide daily /imagine limit (admin)")
    @app_commands.describe(images="Max images per day for the whole server")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def imagine_cap(self, ctx: commands.Context, images: commands.Range[int, 1, 1000]):
        await gc.set_many(ctx.guild, {"IMAGINE_DAILY_CAP": images})
        await ctx.send(f"🎨 The server can now make **{images}** images per day.", ephemeral=True)

    @commands.hybrid_command(name="ai-usage", description="AI usage today and this month (admin)")
    @app_commands.default_permissions(administrator=True)
    @commands.has_permissions(administrator=True)
    async def ai_usage(self, ctx: commands.Context):
        embed = discord.Embed(title="🤖 AI usage", color=discord.Color.blurple())
        if not ai.key_set():
            embed.description = "⚠️ AI is off (no xAI key)."
        for title, since in (("Today", day_start()), ("This month", month_start())):
            rows = await self.bot.db.ai_usage_summary(ctx.guild.id, since)
            lines = [
                f"**{r['feature']}**: {r['calls']} call(s)"
                + (f" ({r['calls'] - r['ok']} failed)" if r["calls"] != r["ok"] else "")
                + (f", {r['tokens']:,} tokens" if r["tokens"] else "")
                + (f", {r['images']} image(s)" if r["images"] else "")
                + f", {r['users']} user(s)"
                for r in rows
            ]
            embed.add_field(name=title, value=trim("\n".join(lines) or "No AI calls yet.", 1024), inline=False)
        cap = setting(ctx.guild, "IMAGINE_DAILY_CAP", IMAGINE_GUILD_DAILY_CAP)
        on = "on" if setting(ctx.guild, "AUTOMOD_ENABLED", 0) else "off"
        embed.add_field(
            name="Limits",
            value=(f"/ask: {ASK_DAILY_LIMIT}/day per user, 1 per {ASK_COOLDOWN}s\n"
                   f"/imagine: {IMAGINE_DAILY_LIMIT}/day per user, 1 per {IMAGINE_COOLDOWN // 60} min, "
                   f"{cap}/day for the server\nAutomod: {on}"),
            inline=False,
        )
        embed.set_footer(text=f"Models: {ai.XAI_FAST_MODEL} (text), {ai.XAI_IMAGE_MODEL} (images) • days reset at "
                              f"midnight {config.TIMEZONE}")
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIFeatures(bot))
