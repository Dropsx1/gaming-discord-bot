"""Chat summaries for /summarize and ticket close (AI on the smart model)."""

import logging
from typing import Optional

import discord

from utils import ai
from utils.helpers import trim

log = logging.getLogger("bot.summaries")

MAX_MESSAGES = 200


async def collect_lines(channel, limit: int, before=None) -> list[str]:
    """The last `limit` (<= 200) non-bot messages with text, as "name: text", oldest first."""
    limit = max(1, min(MAX_MESSAGES, limit))
    lines: list[str] = []
    scanned = 0
    async for msg in channel.history(limit=limit * 3, before=before):  # newest first; skip bots/empties
        scanned += 1
        if msg.author.bot or getattr(msg, "webhook_id", None):
            continue
        text = (getattr(msg, "clean_content", None) or msg.content or "").strip()
        if getattr(msg, "attachments", None):
            text += " [attachment]"
        if not text:
            continue
        name = getattr(msg.author, "display_name", None) or str(msg.author)
        lines.append(f"{name}: {text.replace(chr(10), ' ')}")
        if len(lines) >= limit:
            break
    return lines[::-1]


def summary_embed(summary: dict, title: str, count: int) -> discord.Embed:
    embed = discord.Embed(title=trim(title, 256), color=discord.Color.teal())
    for key, name in (("topics", "🗂️ Topics"), ("decisions", "✅ Decisions"), ("open_questions", "❓ Open questions")):
        items = summary.get(key) or []
        value = "\n".join(f"• {i}" for i in items) or "None"
        embed.add_field(name=name, value=trim(value, 1024), inline=False)
    embed.set_footer(text=f"✨ AI summary of the last {count} message(s) • can be wrong")
    return embed


async def summarize_channel(channel, limit: int, feature: str = "summarize") -> tuple[Optional[dict], int, int]:
    """Returns (summary or None, messages used, tokens)."""
    lines = await collect_lines(channel, limit)
    if not lines:
        return None, 0, 0
    result = await ai.summarize(lines, feature=feature)
    if result is None:
        return None, len(lines), 0
    summary, tokens = result
    return summary, len(lines), tokens
