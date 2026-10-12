"""/setup-role: point the bot's roles (e.g. the "I accept" Member role) at an existing role, safely.

Run:  python tests/test_setup_role.py      (or: python -m pytest tests/)
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
os.environ.update(DB_PATH=os.path.join(tempfile.mkdtemp(), "setup_role.db"), GUILD_ID="", MEMBER_ROLE_ID="")

import discord  # noqa: E402
from discord.ext import commands  # noqa: E402

from cogs import onboarding  # noqa: E402
from cogs import setup as setup_cog  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402

_ids = itertools.count(90_000)


class Role(discord.Role):
    def __init__(self, guild, name, position, perms=0, managed=False, mentionable=False, role_id=None):
        self.guild, self.name, self.position = guild, name, position
        self.id = role_id or next(_ids)
        self._permissions, self.managed, self.mentionable = perms, managed, mentionable

    __hash__ = object.__hash__


class Guild:
    def __init__(self):
        self.id = next(_ids)
        self.default_role = Role(self, "@everyone", 0, role_id=self.id)
        self.me = types.SimpleNamespace(
            top_role=Role(self, "UPH Bot", 10), guild_permissions=discord.Permissions(manage_roles=True)
        )
        self.roles = [self.default_role, self.me.top_role]

    def add(self, *args, **kw):
        r = Role(self, *args, **kw)
        self.roles.append(r)
        return r

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


class Ctx:
    def __init__(self, guild):
        self.guild, self.author, self.replies = guild, "admin", []

    async def send(self, content=None, ephemeral=False, **kw):
        self.replies.append(content)


async def _run():
    db = Database(os.environ["DB_PATH"])
    await db.connect()
    await gc.init(db)
    try:
        g = Guild()
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cog = setup_cog.Setup(bot)
        run = cog.setup_role.callback

        async def set_role(setting, role):
            ctx = Ctx(g)
            await run(cog, ctx, setting, role)
            return ctx.replies[-1]

        verified = g.add("Verified", 3)
        admin = g.add("Admins", 5, perms=discord.Permissions(administrator=True).value)
        mods = g.add("Mods", 4, perms=discord.Permissions(kick_members=True, ban_members=True).value)
        above = g.add("Owner Crew", 20)
        booster = g.add("Server Booster", 2, managed=True)
        pings = g.add("Game Night Pings", 1, mentionable=False)

        # Refused: unsafe or unusable roles for the "I accept" button. Nothing is saved.
        for role, why in [(admin, "Administrator"), (mods, "Kick Members"), (above, "above my highest role"),
                          (booster, "managed"), (g.default_role, "@everyone")]:
            reply = await set_role("MEMBER_ROLE_ID", role)
            assert reply.startswith("🚫") and why in reply, (role.name, reply)
        assert gc.get(g, "MEMBER_ROLE_ID") == 0

        # Accepted: a plain role below the bot. Saved and persisted.
        reply = await set_role("MEMBER_ROLE_ID", verified)
        assert reply.startswith("✅") and "I accept the rules" in reply, reply
        assert gc.get(g, "MEMBER_ROLE_ID") == verified.id
        row = await db.fetchone("SELECT value FROM guild_config WHERE guild_id = ? AND key = 'MEMBER_ROLE_ID'", (g.id,))
        assert int(row["value"]) == verified.id

        # The "I accept" button now grants that role.
        member = types.SimpleNamespace(roles=[], mention="@new")

        async def add_roles(*r, reason=None):
            member.roles += r

        member.add_roles = add_roles
        sent = []
        resp = types.SimpleNamespace(send_message=lambda content, ephemeral=False: _append(sent, content))
        interaction = types.SimpleNamespace(user=member, guild=g, response=resp)
        await onboarding.RulesView().accept.callback(interaction)
        assert member.roles == [verified] and "unlocked" in sent[-1], (member.roles, sent)

        # Staff may be a moderator role (the bot never hands it out); ping roles warn when not mentionable.
        assert (await set_role("STAFF_ROLE_ID", mods)).startswith("✅")
        reply = await set_role("PROMPT_PING_ROLE_ID", pings)
        assert reply.startswith("✅") and "isn't mentionable" in reply, reply

        # Friendly setting names work for the prefix version (!setup-role member @Role); unknown ones explain.
        assert (await set_role("member", verified)).startswith("✅")
        assert "Unknown setting" in await set_role("favorite_color", verified)
    finally:
        await db.close()


async def _append(lst, value):
    lst.append(value)


def test_setup_role():
    asyncio.run(_run())


if __name__ == "__main__":
    test_setup_role()
    print("tests/test_setup_role.py: all checks passed")
