"""Multi-server behavior: per-server scheduling, job keys and slash-command sync. No token needed.

Run:  python tests/test_multiserver.py      (or: python -m pytest tests/)
"""

import asyncio
import itertools
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(DB_PATH=os.path.join(tempfile.mkdtemp(), "multi.db"), GUILD_ID="", SCHEDULE_ENABLED="false")
for key in ("PROMPT_CHANNEL_ID", "DIGEST_CHANNEL_ID", "CHALLENGE_CHANNEL_ID"):
    os.environ.pop(key, None)

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from cogs import schedule as sch  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402

_ids = itertools.count(50_000)


class Msg:
    def __init__(self, channel, content=None, embed=None):
        self.id, self.channel, self.content, self.embed = next(_ids), channel, content, embed

    async def create_thread(self, name, auto_archive_duration):
        return types.SimpleNamespace(id=next(_ids), name=name)


class Chan:
    def __init__(self, guild):
        self.id, self.guild, self.sent = next(_ids), guild, []
        self.mention = f"<#{self.id}>"

    async def send(self, content=None, embed=None, **kw):
        m = Msg(self, content, embed)
        self.sent.append(m)
        return m


class Guild:
    def __init__(self, name):
        self.id, self.name, self._channels, self.roles = next(_ids), name, {}, []

    def add(self):
        ch = Chan(self)
        self._channels[ch.id] = ch
        return ch

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return None

    def __str__(self):
        return self.name


async def _run_all():
    db = Database(os.environ["DB_PATH"])
    await db.connect()
    await gc.init(db)
    try:
        a, b, c = Guild("Universal Play Haven"), Guild("Artifi"), Guild("Unconfigured")
        await gc.set_many(a, {"PROMPT_CHANNEL_ID": a.add().id})
        await gc.set_many(b, {"PROMPT_CHANNEL_ID": b.add().id})
        # A channel ID saved for one server must never resolve in another.
        await gc.set_many(c, {"PROMPT_CHANNEL_ID": gc.get(a, "PROMPT_CHANNEL_ID")})

        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.db = db
        bot._connection._guilds = {g.id: g for g in (a, b, c)}
        cog = sch.Schedule(bot)

        async def resolve(cid):  # stored thread/channel IDs -> fake objects (threads aren't tracked here)
            return next((g.get_channel(cid) for g in (a, b, c) if g.get_channel(cid)), None)

        cog.resolve = resolve
        await cog.cog_load()

        assert [g.name for g in cog.guilds_with("PROMPT_CHANNEL_ID")] == ["Universal Play Haven", "Artifi"]

        await cog.prompt_loop.coro(cog)  # the 6:07 PM job
        pa, pb = gc.channel(a, "PROMPT_CHANNEL_ID"), gc.channel(b, "PROMPT_CHANNEL_ID")
        assert len(pa.sent) == 1 and len(pb.sent) == 1, (len(pa.sent), len(pb.sent))
        assert pa.sent[0].embed.title == "💬 Prompt of the Day"

        await cog.prompt_loop.coro(cog)  # same day again: no double posts anywhere
        assert len(pa.sent) == 1 and len(pb.sent) == 1

        keys = sorted(r["run_key"].split(":")[0] for r in await db.fetchall("SELECT run_key FROM job_runs"))
        assert keys == sorted([str(a.id), str(b.id)]), keys  # one run per server, keyed by server

        rows = await db.fetchall("SELECT guild_id FROM prompt_log")
        assert sorted(r[0] for r in rows) == sorted([a.id, b.id])

        # Judging is per server: each server's prompt is judged on its own.
        await db.execute("UPDATE prompt_log SET posted_at = posted_at - 86400")
        assert "thread" in (await cog.judge_prompt(a)).lower()  # fake thread can't be fetched -> no crash
        left = await db.fetchall("SELECT guild_id FROM prompt_log WHERE judged = 0")
        assert [r[0] for r in left] == [b.id], left

        # Old single-server job keys get scoped to the home server on startup.
        await db.execute("INSERT INTO job_runs (job, run_key, ran_at) VALUES ('digest', '2026-W42', 0)")
        sch.config.GUILD_ID = a.id
        try:
            await cog.cog_load()
        finally:
            sch.config.GUILD_ID = 0
        assert await db.job_ran("digest", f"{a.id}:2026-W42")
    finally:
        await db.close()


async def _sync_test():
    import main

    bot = main.GamingBot()
    bot._synced_guilds = set()
    synced = []

    async def fake_sync(guild=None):
        synced.append(guild.id)
        return [1, 2, 3]

    bot.tree.sync = fake_sync
    bot._connection._guilds = {1: types.SimpleNamespace(id=1, name="A"), 2: types.SimpleNamespace(id=2, name="B")}
    bot._connection.user = types.SimpleNamespace(id=9, __str__=lambda self: "Bot")
    main.config.GUILD_ID = 0
    await bot.on_ready()
    await bot.on_ready()  # reconnect: no re-sync
    assert sorted(synced) == [1, 2], synced
    await bot.on_guild_join(types.SimpleNamespace(id=3, name="Artifi"))
    assert sorted(synced) == [1, 2, 3], synced


def test_multiserver():
    asyncio.run(_run_all())


def test_sync_every_server():
    asyncio.run(_sync_test())


if __name__ == "__main__":
    test_multiserver()
    test_sync_every_server()
    print("tests/test_multiserver.py: all checks passed")
