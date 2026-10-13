"""/setup under Discord's creation rate limit (seen live on a brand-new server).

Before: after one rate limit every remaining item was attempted and listed as "Failed".
Now: it stops after the first rate limit, marks the rest as waiting, saves what was made,
finishes automatically once the limit resets, and tells the admin.

Run:  python tests/test_setup_ratelimit.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # for test_setup's fakes
import test_setup as ts  # noqa: E402  (sets up sys.path / env for the bot modules)

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from cogs import setup as setup_cog  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402

DB_FILE = os.path.join(tempfile.mkdtemp(), "ratelimit.db")


class LimitedGuild(ts.FakeGuild):
    """Allows `budget` creations, then Discord says: try again in 600 seconds."""

    def __init__(self, budget):
        super().__init__()
        self.budget, self.attempts = budget, 0

    def _gate(self):
        self.attempts += 1
        if self.budget <= 0:
            raise discord.RateLimited(600.0)
        self.budget -= 1

    async def create_role(self, *a, **kw):
        self._gate()
        return await super().create_role(*a, **kw)

    async def create_category(self, *a, **kw):
        self._gate()
        return await super().create_category(*a, **kw)

    async def create_text_channel(self, *a, **kw):
        self._gate()
        return await super().create_text_channel(*a, **kw)

    async def create_voice_channel(self, *a, **kw):
        self._gate()
        return await super().create_voice_channel(*a, **kw)


class Admin:
    def __init__(self, dm_open=True):
        self.dm_open, self.dms, self.mention = dm_open, [], "@admin"
        self.id = next(ts._ids)  # real users always have an ID (used to find them again after a restart)

    async def send(self, content=None, embed=None):
        if not self.dm_open:
            raise discord.HTTPException(types.SimpleNamespace(status=403, reason="Forbidden"), "DMs closed")
        self.dms.append((content, embed))


class Channel:
    def __init__(self):
        self.sent, self.id = [], next(ts._ids)

    async def send(self, content=None, embed=None):
        self.sent.append((content, embed))


async def _run():
    gc.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    try:
        # 1. Live scenario: one role gets made, then Discord rate-limits everything.
        g = LimitedGuild(budget=1)
        rep = await setup_cog.SetupRunner(g, dry_run=False).run()
        assert rep.created == ["@Staff"], rep.created
        assert not rep.failed, rep.failed  # nothing is "failed": it's all just waiting
        assert len(rep.waiting) == 26, len(rep.waiting)
        assert g.attempts == 2, g.attempts  # stopped after the first rate limit (no hammering)
        assert 590 < rep.retry_at - time.time() <= 600
        assert set(gc.saved(g)) == {"STAFF_ROLE_ID"}  # progress is saved

        embed = setup_cog.build_summary(g, rep, dry_run=False)
        assert embed.title.startswith("⏳") and "<t:" in embed.description and "automatically" in embed.description
        names = [f.name for f in embed.fields]
        assert any(n.startswith("⏳ Waiting for Discord (26)") for n in names), names
        assert not any(n.startswith("❌") for n in names), names
        assert len(embed) <= 6000 and all(len(f.value) <= 1024 for f in embed.fields)

        # 2. The limit resets: the automatic follow-up finishes the job and DMs the admin.
        g.budget = 100
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.get_guild = lambda gid: g if gid == g.id else None
        cog = setup_cog.Setup(bot)
        setup_cog.RESUME_PADDING = 0  # don't actually wait in tests
        admin, channel = Admin(), Channel()
        cog.schedule_resume(g, admin, channel, retry_at=time.time())
        cog.schedule_resume(g, admin, channel, retry_at=time.time())  # second call doesn't double-schedule
        await asyncio.wait_for(cog.resume_tasks[g.id], timeout=10)
        assert len(gc.saved(g)) == 27, len(gc.saved(g))
        assert sum(1 for r in g.roles if r.name == "Staff") == 1  # Staff reused, not duplicated
        assert len(g.channels) == 22
        assert len(admin.dms) == 1 and "finished" in admin.dms[0][0], admin.dms
        assert admin.dms[0][1].title == "🛠️ Server setup complete"

        # 3. Admin has DMs closed: the result is posted in the channel where /setup was run.
        g2 = LimitedGuild(budget=0)
        await setup_cog.SetupRunner(g2, dry_run=False).run()
        g2.budget = 100
        bot.get_guild = lambda gid: g2 if gid == g2.id else None
        closed, channel2 = Admin(dm_open=False), Channel()
        cog.schedule_resume(g2, closed, channel2, retry_at=time.time())
        await asyncio.wait_for(cog.resume_tasks[g2.id], timeout=10)
        assert len(channel2.sent) == 1 and "@admin" in channel2.sent[0][0] and "finished" in channel2.sent[0][0]
        assert len(gc.saved(g2)) == 27

        # 4. Still limited after every retry: it gives up politely and asks for a manual rerun.
        g3 = LimitedGuild(budget=0)
        await setup_cog.SetupRunner(g3, dry_run=False).run()
        bot.get_guild = lambda gid: g3 if gid == g3.id else None
        old_rounds = setup_cog.RESUME_MAX_ROUNDS
        setup_cog.RESUME_MAX_ROUNDS = 2
        try:
            admin3 = Admin()
            # Pretend each retry's limit has already passed so the test doesn't sleep 10 minutes.
            orig_run = setup_cog.SetupRunner.run

            async def run_and_expire(self):
                result = await orig_run(self)
                result.retry_at = time.time()
                return result

            setup_cog.SetupRunner.run = run_and_expire
            cog.schedule_resume(g3, admin3, Channel(), retry_at=time.time())
            await asyncio.wait_for(cog.resume_tasks[g3.id], timeout=10)
        finally:
            setup_cog.RESUME_MAX_ROUNDS = old_rounds
            setup_cog.SetupRunner.run = orig_run
        assert len(admin3.dms) == 1 and "run `/setup` again" in admin3.dms[0][0], admin3.dms

        # 5. Seen live: Discord asked for a 3-hour wait. The bot keeps waiting (no instant give-up DM),
        #    saves the pending finish, and picks it up again after a restart.
        g4 = LimitedGuild(budget=0)
        rep4 = await setup_cog.SetupRunner(g4, dry_run=False).run()
        three_hours = time.time() + 3 * 3600
        bot.get_guild = lambda gid: g4 if gid == g4.id else None
        admin4, chan4 = Admin(), Channel()
        cog.schedule_resume(g4, admin4, chan4, retry_at=three_hours)
        await asyncio.sleep(0.05)
        assert not cog.resume_tasks[g4.id].done() and admin4.dms == [], admin4.dms  # still waiting, no DM
        assert gc.saved(g4)["_RESUME_AT"] == int(three_hours)
        embed4 = setup_cog.build_summary(g4, rep4, dry_run=False)
        assert "automatically" in embed4.description and "restart" in embed4.description

        cog.resume_tasks[g4.id].cancel()  # the bot restarts (e.g. a Railway redeploy)...
        await gc.set_many(g4, {"_RESUME_AT": int(time.time())})  # ...and by the time it's back the limit has passed
        g4.budget = 100
        bot2 = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot2.get_guild = lambda gid: g4 if gid == g4.id else None
        bot2._connection._guilds = {g4.id: g4}
        bot2.get_user = lambda uid: admin4 if uid == admin4.id else None
        cog2 = setup_cog.Setup(bot2)
        await cog2.on_ready()
        await asyncio.wait_for(cog2.resume_tasks[g4.id], timeout=10)
        assert len(admin4.dms) == 1 and "finished" in admin4.dms[0][0], admin4.dms
        assert len(gc.saved(g4)) == 27 and "_RESUME_AT" not in gc.saved(g4)  # done, pending marker cleared
    finally:
        setup_cog.RESUME_PADDING = 5
        await db.close()


def test_setup_ratelimit():
    asyncio.run(_run())


if __name__ == "__main__":
    test_setup_ratelimit()
    print("tests/test_setup_ratelimit.py: all checks passed")
