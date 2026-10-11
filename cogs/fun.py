import logging
import random
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import ai
from utils.helpers import trim

logger = logging.getLogger("bot.fun")

GAME_SUGGESTIONS = sorted(set(config.LFG_GAMES))

CONVERSATION_STARTERS = [
    "What's the most clutch play you've ever pulled off?",
    "Which game have you sunk the most hours into?",
    "PC, console, or mobile — what's your main and why?",
    "What's your all-time favorite game soundtrack?",
    "What's a game you think is criminally underrated?",
    "Team games or solo grinding — what's your vibe?",
    "Anyone grinding ranked right now? What rank are you chasing?",
    "What's your go-to warm-up before a session?",
    "Best gaming memory with people from this server?",
    "If you could add one feature to your main game, what would it be?",
    "What game would you love to see a sequel or remake of?",
    "Controller or mouse & keyboard — and for which games?",
]

CHALLENGES = [
    "Play 3 matches using only your least-used weapon or character.",
    "Swap roles: if you usually play DPS, go support for the night.",
    "Win a round without using your ultimate / special ability.",
    "Queue up with someone new from the server.",
    "Do a no-comms round and see how well your team reads each other.",
    "Set a personal goal for the next hour and report back.",
    "Try a game you've never played before tonight.",
    "Host a mini 1v1 tournament with friends.",
    "Speedrun the first level/mission of a game you love.",
    "Play a match where you only follow your teammates' calls.",
]

PROMPTS = [
    "Design your dream loadout for your main game and explain each pick.",
    "Pitch a crossover between two games. What's the plot?",
    "Rank your top 5 maps of all time — go!",
    "Describe your playstyle in three words. Let the chat guess your main.",
    "Which game character would you want on your squad in real life?",
    "Create a house rule that would make your favorite game more chaotic (but fun).",
    "What's one tip you wish you knew when you started your main game?",
    "Build a 5-stack from characters across different games. Who wins?",
]


class Fun(commands.Cog):
    """Suggestions, challenges and prompts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="suggest", description="Random game suggestion or conversation starter")
    @app_commands.describe(kind="What kind of suggestion?")
    @app_commands.rename(kind="type")
    @commands.cooldown(3, 30, commands.BucketType.user)
    async def suggest(self, ctx: commands.Context, kind: Literal["game", "convo"] = "game"):
        if kind == "convo":
            embed = discord.Embed(
                title="💬 Conversation Starter",
                description=random.choice(CONVERSATION_STARTERS),
                color=discord.Color.teal(),
            )
        else:
            embed = discord.Embed(
                title="🎮 Game Suggestion",
                description=f"How about some **{random.choice(GAME_SUGGESTIONS)}**? Use `/lfg` to find a squad!",
                color=discord.Color.green(),
            )
        await ctx.send(embed=embed)

    async def _ai_or_fallback(self, ctx: commands.Context, kind: str, theme: Optional[str], fallback: list, title: str):
        text = None
        try:
            if ctx.interaction:
                await ctx.defer()  # AI calls can take a few seconds
            text = await ai.generate(kind, theme)
        except Exception:
            logger.exception("AI generation failed; using fallback")
        source = "AI" if text else "built-in"
        if not text:
            text = random.choice(fallback)
            if theme:
                text = f"**[{trim(theme, 50)}]** {text}"
        embed = discord.Embed(title=title, description=trim(text, 4000), color=discord.Color.blurple())
        if source == "built-in":
            embed.set_footer(text="Tip: staff can plug in Grok/xAI for fresh AI ideas (see utils/ai.py)")
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="idea", description="Get a random game challenge")
    @app_commands.describe(theme="Optional theme (e.g. Valorant, chill, competitive)")
    @commands.cooldown(3, 30, commands.BucketType.user)
    async def idea(self, ctx: commands.Context, *, theme: Optional[str] = None):
        await self._ai_or_fallback(ctx, "challenge", theme, CHALLENGES, "💡 Game Challenge")

    @commands.hybrid_command(name="prompt", description="Get a discussion or creative gaming prompt")
    @app_commands.describe(theme="Optional theme (e.g. Minecraft, retro, horror)")
    @commands.cooldown(3, 30, commands.BucketType.user)
    async def prompt(self, ctx: commands.Context, *, theme: Optional[str] = None):
        await self._ai_or_fallback(ctx, "prompt", theme, PROMPTS, "🧠 Gaming Prompt")


async def setup(bot: commands.Bot):
    await bot.add_cog(Fun(bot))
