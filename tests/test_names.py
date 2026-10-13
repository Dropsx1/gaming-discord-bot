"""Hand-made channels/roles: name normalization, /setup reuse (even when rate-limited), /setup-link.

Run:  python tests/test_names.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # reuse test_setup's fakes
import test_setup as ts  # noqa: E402
from test_setup_ratelimit import LimitedGuild  # noqa: E402

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from cogs import setup as setup_cog  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402
from utils.names import normalize_name  # noqa: E402

DB_FILE = os.path.join(tempfile.mkdtemp(), "names_match.db")


def test_normalize_name():
    cases = {
        "📅┃prompt-of-the-day": "prompt-of-the-day",
        "#📖〢「rules": "rules",                 # Universal Play Haven's style
        "「🎨」self-roles": "self-roles",
        "💬・general・chat": "general-chat",
        "・ʀᴜʟᴇs": "rules",                      # small-caps letters
        "𝐠𝐞𝐧𝐞𝐫𝐚𝐥-𝐜𝐡𝐚𝐭": "general-chat",        # math-bold letters
        "AI & Create Lab": "ai-create-lab",
        "🤖 AI | CREATE LAB": "ai-create-lab",
        "──── Staff ────": "staff",
        "➕ Join to Create": "join-to-create",
        "👑 Prompt King": "prompt-king",
        "general--chat": "general-chat",
        "tech_digest": "tech-digest",
        "2v2-lfg": "2v2-lfg",
        "日本語-chat": "日本語-chat",
        "🔊": "",
    }
    for raw, want in cases.items():
        assert normalize_name(raw) == want, (raw, normalize_name(raw), want)


def _add_text(g, name, category=None):
    ch = ts.FakeText(g, name, category)
    g._channels.append(ch)
    return ch


async def _run():
    gc.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    try:
        # A server where someone built part of the layout by hand, with decorated names,
        # and Discord is rate-limiting creation from the very first call.
        g = LimitedGuild(budget=0)
        info = ts.FakeCategory(g, "📌〢INFO")  # their own category name
        g._channels.append(info)
        lab = ts.FakeCategory(g, "🤖 AI & CREATE LAB")
        g._channels.append(lab)
        rules = _add_text(g, "#📖〢「rules", info)
        prompt = _add_text(g, "📅┃prompt-of-the-day", info)  # in a *different* category than /setup's
        digest = _add_text(g, "📰・tech・digest", lab)
        staff = ts.FakeRole(g, "🛡️ Staff", position=3)
        member = ts.FakeRole(g, "Member ✅", position=2)
        g._roles += [staff, member]
        voice = ts.FakeVoice(g, "➕ Join to Create")
        g._channels.append(voice)

        rep = await setup_cog.SetupRunner(g, dry_run=False).run()
        saved = gc.saved(g)
        assert saved["START_HERE_CHANNEL_ID"] == rules.id
        assert saved["PROMPT_CHANNEL_ID"] == prompt.id      # matched in another category
        assert saved["DIGEST_CHANNEL_ID"] == digest.id
        assert saved["LAB_CATEGORY_ID"] == lab.id
        assert saved["STAFF_ROLE_ID"] == staff.id and saved["MEMBER_ROLE_ID"] == member.id
        assert saved["JOIN_TO_CREATE_CHANNEL_ID"] == voice.id
        assert len(rep.reused) == 7, rep.reused
        # Rate limit never blocks reuse: only truly missing items are waiting, each listed by name.
        assert len(rep.waiting) == 27 - 7 and not rep.failed, (rep.waiting, rep.failed)
        assert not any("rules" in w or "prompt-of-the-day" in w or w == "@Staff" for w in rep.waiting), rep.waiting
        assert "#welcome (in Server)" in rep.waiting and "@Prompt King" in rep.waiting, rep.waiting
        assert g.attempts == 1  # one create attempt hit the limit, then it stopped

        embed = setup_cog.build_summary(g, rep, dry_run=False)
        waiting_text = "\n".join(f.value for f in embed.fields if f.name.startswith("⏳"))
        for item in rep.waiting:
            assert item in waiting_text, item  # every waiting item is listed by name

        # Once the limit lifts, only the missing items get created, and nothing is duplicated.
        g.budget = 100
        rep2 = await setup_cog.SetupRunner(g, dry_run=False).run()
        assert not rep2.waiting and not rep2.failed and len(rep2.created) == 20, (rep2.created, rep2.failed)
        assert sum(1 for c in g.channels if normalize_name(c.name) == "rules") == 1
        assert sum(1 for c in g.channels if normalize_name(c.name) == "prompt-of-the-day") == 1
        assert sum(1 for r in g.roles if normalize_name(r.name) == "staff") == 1

        # One existing item never serves two settings, even if names collide.
        g2 = ts.FakeGuild()
        only = _add_text(g2, "rules")
        rep3 = await setup_cog.SetupRunner(g2, dry_run=True).run()
        assert rep3.reused.count(only.mention) == 1

        # /setup-link: point any setting at an existing channel/role.
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cog = setup_cog.Setup(bot)
        link = cog.setup_link.callback
        g3 = ts.FakeGuild()
        fancy_prompt = _add_text(g3, "✨┃daily-prompt")
        tickets = ts.FakeCategory(g3, "🎫 Support")
        g3._channels.append(tickets)
        verified = ts.FakeRole(g3, "Verified", position=2)
        mods = ts.FakeRole(g3, "Mods", position=4)
        mods._permissions = discord.Permissions(ban_members=True).value
        mods.managed = False
        verified._permissions, verified.managed = 0, False
        g3._roles += [verified, mods]

        class Ctx:
            def __init__(self):
                self.guild, self.author, self.replies = g3, "admin", []

            async def send(self, content=None, ephemeral=False, **kw):
                self.replies.append(content)

        async def run_link(key, channel=None, role=None):
            ctx = Ctx()
            await link(cog, ctx, key, channel, role)
            return ctx.replies[-1]

        assert (await run_link("PROMPT_CHANNEL_ID", channel=fancy_prompt)).startswith("✅")
        assert gc.get(g3, "PROMPT_CHANNEL_ID") == fancy_prompt.id
        assert (await run_link("tickets category", channel=tickets)).startswith("✅")
        assert gc.get(g3, "TICKET_CATEGORY_ID") == tickets.id
        reply = await run_link("member_role", role=verified)
        assert reply.startswith("✅") and "I accept the rules" in reply, reply
        assert gc.get(g3, "MEMBER_ROLE_ID") == verified.id
        # Wrong kind / unsafe / unknown are refused and nothing is saved.
        assert "needs a category" in await run_link("TICKET_CATEGORY_ID", channel=fancy_prompt)
        assert "needs a role" in await run_link("member_role", channel=fancy_prompt)
        assert "moderator permissions" in await run_link("member_role", role=mods)
        assert gc.get(g3, "MEMBER_ROLE_ID") == verified.id
        assert "don't know the setting" in await run_link("favorite_snack", channel=fancy_prompt)

        # Autocomplete finds settings by plain words.
        choices = await cog._setup_link_key_autocomplete(None, "member")
        assert any(c.value == "MEMBER_ROLE_ID" for c in choices), choices
    finally:
        await db.close()


def test_setup_matches_hand_made_items():
    asyncio.run(_run())


if __name__ == "__main__":
    test_normalize_name()
    test_setup_matches_hand_made_items()
    print("tests/test_names.py: all checks passed")
