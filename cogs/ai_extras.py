"""More AI features: /summarize and /trivia (+ /trivia-leaderboard).

Everything needs XAI_API_KEY; without it each command replies with the friendly "AI is off" message.
Cost controls: per-user cooldowns and daily limits (counted in ai_usage) plus the per-feature daily
cost caps in utils/ai.py.
"""

import logging
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from cogs.ai_features import AI_OFF, Cooldowns, day_start, nsfw_prompt
from utils import ai, summaries
from utils.helpers import is_staff, trim

logger = logging.getLogger("bot.ai_extras")

SUMMARIZE_COOLDOWN = 120
SUMMARIZE_DAILY_LIMIT = config._int("SUMMARIZE_DAILY_LIMIT", 10)
TRIVIA_COOLDOWN = 20          # per channel
TRIVIA_DAILY_LIMIT = config._int("TRIVIA_DAILY_LIMIT", 20)  # questions started per user per day
TRIVIA_SECONDS = 30
TRIVIA_XP = {"easy": 10, "medium": 20, "hard": 35}


LETTERS = "ABCD"


class TriviaView(discord.ui.View):
    """One question, four answer buttons. Everyone may answer once; the first correct answer gets full XP,
    later correct answers half. Reveals the answer when time runs out."""

    def __init__(self, db, guild_id: int, quiz: dict, difficulty: str, seconds: int = TRIVIA_SECONDS):
        super().__init__(timeout=seconds)
        self.db, self.guild_id, self.quiz, self.difficulty = db, guild_id, quiz, difficulty
        self.answers: dict[int, int] = {}
        self.first_correct: Optional[int] = None
        self.message = None
        for i, option in enumerate(quiz["options"]):
            button = discord.ui.Button(label=trim(f"{LETTERS[i]}. {option}", 80), style=discord.ButtonStyle.primary,
                                       custom_id=f"trivia:{id(self)}:{i}")
            button.callback = self._make_callback(i)
            self.add_item(button)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            await self.answer(interaction, index)
        return callback

    def xp_for(self, correct: bool, first: bool) -> int:
        base = TRIVIA_XP.get(self.difficulty, 20)
        return 0 if not correct else (base if first else base // 2)

    async def answer(self, interaction: discord.Interaction, index: int) -> None:
        user = interaction.user
        if user.id in self.answers:
            await interaction.response.send_message("You already answered this one!", ephemeral=True)
            return
        self.answers[user.id] = index
        correct = index == self.quiz["answer"]
        first = correct and self.first_correct is None
        if first:
            self.first_correct = user.id
        xp = self.xp_for(correct, first)
        await self.db.add_trivia_result(self.guild_id, user.id, xp, correct)
        if correct:
            text = f"✅ Correct! **+{xp} XP**" + (" (first!)" if first else "")
        else:
            text = "❌ Not quite. The answer is revealed when time's up."
        await interaction.response.send_message(text, ephemeral=True)

    def reveal_embed(self, embed: Optional[discord.Embed] = None) -> discord.Embed:
        q = self.quiz
        embed = embed or discord.Embed(title="🧠 Trivia")
        right = sum(1 for v in self.answers.values() if v == q["answer"])
        result = f"**Answer: {LETTERS[q['answer']]}. {q['options'][q['answer']]}**"
        if q.get("explanation"):
            result += f"\n{q['explanation']}"
        result += f"\n\n{right}/{len(self.answers)} got it right."
        if self.first_correct:
            result += f" First: <@{self.first_correct}> 🏅"
        embed.add_field(name="⏰ Time's up!", value=trim(result, 1024), inline=False)
        return embed

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                embed = self.message.embeds[0] if getattr(self.message, "embeds", None) else None
                await self.message.edit(embed=self.reveal_embed(embed), view=self,
                                        allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                pass


class AIExtras(commands.Cog):
    """/summarize and /trivia."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.summarize_cd = Cooldowns(SUMMARIZE_COOLDOWN)
        self.trivia_cd = Cooldowns(TRIVIA_COOLDOWN)  # keyed by (guild, channel)

    # ----------------------------------------------------------- /summarize
    async def summarize_block(self, ctx: commands.Context) -> Optional[str]:
        if not ai.key_set():
            return AI_OFF
        perms = ctx.channel.permissions_for(ctx.guild.me)
        if not getattr(perms, "read_message_history", True):
            return "⚠️ I need **Read Message History** here to summarize."
        wait = self.summarize_cd.remaining(ctx.guild.id, ctx.author.id)
        if wait:
            return f"⏳ Try `/summarize` again in {wait:.0f}s."
        if await self.bot.db.count_ai(ctx.guild.id, "summarize", day_start(), ctx.author.id) >= SUMMARIZE_DAILY_LIMIT:
            return f"📅 You've used all {SUMMARIZE_DAILY_LIMIT} summaries for today. They reset at midnight."
        return None

    @commands.hybrid_command(name="summarize", description="AI summary of recent chat: topics, decisions, open questions")
    @app_commands.describe(messages="How many recent messages (5-200, default 50)",
                           public="Post the summary for everyone (staff only; default: only you see it)")
    async def summarize(self, ctx: commands.Context, messages: commands.Range[int, 5, 200] = 50, public: bool = False):
        if problem := await self.summarize_block(ctx):
            await ctx.send(problem, ephemeral=True)
            return
        public = public and is_staff(ctx.author)
        self.summarize_cd.hit(ctx.guild.id, ctx.author.id)
        await ctx.defer(ephemeral=not public)
        summary, used, tokens = await summaries.summarize_channel(ctx.channel, min(int(messages), summaries.MAX_MESSAGES))
        if used:
            await self.bot.db.log_ai(ctx.guild.id, ctx.author.id, "summarize", tokens, ok=summary is not None)
        if summary is None:
            self.summarize_cd.reset(ctx.guild.id, ctx.author.id)
            text = ("🤷 There's nothing to summarize here yet." if not used
                    else "😵 I couldn't summarize that just now. Try again in a minute.")
            await ctx.send(text, ephemeral=True)
            return
        name = getattr(ctx.channel, "name", "this channel")
        await ctx.send(embed=summaries.summary_embed(summary, f"📝 Summary of #{name}", used), ephemeral=not public,
                       allowed_mentions=discord.AllowedMentions.none())

    # --------------------------------------------------------------- /trivia
    async def trivia_block(self, ctx: commands.Context) -> Optional[str]:
        if not ai.key_set():
            return AI_OFF
        wait = self.trivia_cd.remaining(ctx.guild.id, ctx.channel.id)
        if wait:
            return f"⏳ A question just ran here. Next one in {wait:.0f}s."
        if await self.bot.db.count_ai(ctx.guild.id, "trivia", day_start(), ctx.author.id) >= TRIVIA_DAILY_LIMIT:
            return f"📅 You've started all {TRIVIA_DAILY_LIMIT} trivia questions for today. They reset at midnight."
        return None

    @commands.hybrid_command(name="trivia", description="AI trivia question: answer with the buttons to earn XP")
    @app_commands.describe(topic="Any topic, e.g. Zelda, retro consoles, esports (default: video games)",
                           difficulty="easy, medium or hard (more XP)")
    async def trivia(self, ctx: commands.Context, topic: str = "video games",
                     difficulty: Literal["easy", "medium", "hard"] = "medium"):
        if problem := await self.trivia_block(ctx):
            await ctx.send(problem, ephemeral=True)
            return
        topic = trim(topic.strip(), 100) or "video games"
        if nsfw_prompt(topic):
            await ctx.send("🚫 Let's keep trivia safe-for-work. Try another topic!", ephemeral=True)
            return
        self.trivia_cd.hit(ctx.guild.id, ctx.channel.id)
        await ctx.defer()
        result = await ai.trivia_question(topic, difficulty)
        await self.bot.db.log_ai(ctx.guild.id, ctx.author.id, "trivia", result[1] if result else 0, ok=bool(result))
        if result is None:
            self.trivia_cd.reset(ctx.guild.id, ctx.channel.id)
            await ctx.send("😵 I couldn't come up with a question just now. Try again in a minute.", ephemeral=True)
            return
        quiz = result[0]
        view = TriviaView(self.bot.db, ctx.guild.id, quiz, difficulty)
        options = "\n".join(f"**{LETTERS[i]}.** {o}" for i, o in enumerate(quiz["options"]))
        embed = discord.Embed(title=trim(f"🧠 Trivia: {topic}", 256), description=f"{quiz['question']}\n\n{options}",
                              color=discord.Color.gold())
        embed.set_footer(text=f"{difficulty.title()} • {TRIVIA_XP[difficulty]} XP for the first correct answer • "
                              f"{TRIVIA_SECONDS}s • ✨ AI-generated")
        view.message = await ctx.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_command(name="trivia-leaderboard", description="Top trivia players on this server")
    async def trivia_leaderboard(self, ctx: commands.Context):
        rows = await self.bot.db.trivia_leaderboard(ctx.guild.id, 10)
        if not rows:
            await ctx.send("🧠 No trivia answers yet. Start one with `/trivia`!", ephemeral=True)
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = [f"{medals[i] if i < 3 else f'`#{i + 1}`'} <@{r['user_id']}>: **{r['xp']:,} XP** "
                 f"({r['correct']}/{r['answered']} correct)" for i, r in enumerate(rows)]
        mine = await self.bot.db.trivia_score(ctx.guild.id, ctx.author.id)
        embed = discord.Embed(title="🏆 Trivia leaderboard", description="\n".join(lines), color=discord.Color.gold())
        if mine and mine["answered"]:
            embed.set_footer(text=f"You: {mine['xp']:,} XP, {mine['correct']}/{mine['answered']} correct")
        await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(AIExtras(bot))
