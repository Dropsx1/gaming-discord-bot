"""{server} in rules / prompts / Remix themes becomes the name of the server it's posted in.

Run:  python tests/test_server_name.py      (or: python -m pytest tests/)
"""

import asyncio
import itertools
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_FILE = os.path.join(tempfile.mkdtemp(), "names.db")  # own DB, so tests never share state
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="", SCHEDULE_ENABLED="false")

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from cogs import onboarding  # noqa: E402
from cogs import schedule as sch  # noqa: E402
from utils import content  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402

_ids = itertools.count(120_000)


class Msg:
    def __init__(self, embed):
        self.id, self.embed = next(_ids), embed

    async def create_thread(self, name, auto_archive_duration):
        return types.SimpleNamespace(id=next(_ids), name=name)


class Chan:
    def __init__(self, guild):
        self.id, self.guild, self.sent, self.mention = next(_ids), guild, [], "#chan"

    async def send(self, content=None, embed=None, **kw):
        self.sent.append(Msg(embed))
        return self.sent[-1]


class Guild:
    def __init__(self, name):
        self.id, self.name, self.roles, self._ch = next(_ids), name, [], {}

    def add(self):
        ch = Chan(self)
        self._ch[ch.id] = ch
        return ch

    def get_channel(self, cid):
        return self._ch.get(cid)

    def get_role(self, rid):
        return None


async def _run():
    # Shipped content uses the placeholder, not a hard-coded server name.
    for path in ("data/rules.md", "data/remix_themes.json", "data/prompts.json"):
        assert "Universal Play Haven" not in (ROOT / path).read_text(encoding="utf-8"), path
    themes = json.loads((ROOT / "data/remix_themes.json").read_text(encoding="utf-8"))
    mascot = next(t for t in themes if "{server}" in t["description"])

    # Rules panel title.
    title, body = onboarding.load_rules("Nexus Arcade")
    assert title == "📜 Nexus Arcade Rules" and "{server}" not in title + body, title
    assert onboarding.load_rules("Artifi")[0] == "📜 Artifi Rules"

    # Rendering leaves other braces and non-text fields alone.
    item = content.render_item({"id": 4, "description": "Mascot for {server} {not_a_field}"}, "Nexus Arcade")
    assert item == {"id": 4, "description": "Mascot for Nexus Arcade {not_a_field}"}, item
    assert content.render_item(mascot, "Nexus Arcade")["description"] == "Invent a mascot for Nexus Arcade."

    # Scheduled posts fill in the name of the server they're posted in.
    sch.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    try:
        nexus, other = Guild("Nexus Arcade"), Guild("Artifi")
        for g in (nexus, other):
            await gc.set_many(g, {"PROMPT_CHANNEL_ID": g.add().id, "CHALLENGE_CHANNEL_ID": g.add().id})
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.db = db
        cog = sch.Schedule(bot)

        real_pick = content.pick_unused

        async def fake_pick(db_, kind):
            if kind == content.PROMPTS:
                return {"id": 1, "theme": "gaming", "text": "What's your favorite memory from {server}?"}
            return mascot

        sch.content.pick_unused = fake_pick
        try:
            for g in (nexus, other):
                await cog.post_prompt(g, force=True, ping=False)
                await cog.kickoff_remix(g, force=True, ping=False)
        finally:
            sch.content.pick_unused = real_pick

        for g in (nexus, other):
            prompt_ch = gc.channel(g, "PROMPT_CHANNEL_ID")
            remix_ch = gc.channel(g, "CHALLENGE_CHANNEL_ID")
            assert prompt_ch.sent[-1].embed.description == f"What's your favorite memory from {g.name}?"
            assert remix_ch.sent[-1].embed.description == f"Invent a mascot for {g.name}."
        logged = [r["text"] for r in await db.fetchall("SELECT text FROM prompt_log ORDER BY id")]
        assert logged == ["What's your favorite memory from Nexus Arcade?", "What's your favorite memory from Artifi?"]
    finally:
        await db.close()


def test_server_name():
    asyncio.run(_run())


if __name__ == "__main__":
    test_server_name()
    print("tests/test_server_name.py: all checks passed")
