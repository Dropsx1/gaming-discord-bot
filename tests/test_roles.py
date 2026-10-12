"""Self-role menu: only picked roles are created, and Discord's role-creation rate limit is handled.

Regression test for a live bug: one pick tried to create every missing role in the menu,
hit Discord's rate limit (RateLimited is not an HTTPException) and the click silently failed.

Run:  python tests/test_roles.py      (or: python -m pytest tests/)
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
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "roles.db"))

import discord  # noqa: E402

from cogs import roles  # noqa: E402

_ids = itertools.count(70_000)


class Role:
    def __init__(self, name):
        self.id, self.name, self.mention = next(_ids), name, f"@{name}"


class Guild:
    def __init__(self, allowed_creates):
        self.id = next(_ids)
        self.roles = []
        self.allowed_creates = allowed_creates  # Discord rate-limits after this many
        self.create_calls = []

    async def create_role(self, name, mentionable=False, reason=None):
        self.create_calls.append(name)
        if len(self.create_calls) > self.allowed_creates:
            raise discord.RateLimited(691.2)
        role = Role(name)
        self.roles.append(role)
        return role

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)


class Member:
    def __init__(self):
        self.roles = []

    async def add_roles(self, *r, reason=None):
        self.roles += r

    async def remove_roles(self, *r, reason=None):
        self.roles = [x for x in self.roles if x not in r]

    def __str__(self):
        return "member"


class Interaction:
    def __init__(self, guild, member):
        self.guild, self.user, self.sent = guild, member, []
        self.response = types.SimpleNamespace(defer=self._defer)
        self.followup = types.SimpleNamespace(send=self._send)

    async def _defer(self, **kw):
        pass

    async def _send(self, content, ephemeral=False):
        self.sent.append(content)


def make_select(picked):
    sel = roles.RoleSelect(
        [("Valorant", "Valorant", None), ("Minecraft", "Minecraft", None), ("Apex Legends", "Apex", None),
         ("CS2", "CS2", None)],
        "games", "test:games",
    )
    type(sel).values = property(lambda self: picked)  # what the member selected
    return sel


async def _run():
    # 1. Picking one game creates only that role, not every missing role in the menu.
    g, m = Guild(allowed_creates=10), Member()
    it = Interaction(g, m)
    await make_select(["Valorant"]).callback(it)
    assert g.create_calls == ["Valorant"], g.create_calls
    assert [r.name for r in m.roles] == ["Valorant"] and "Added" in it.sent[-1], it.sent

    # 2. Rate limit mid-way: the member still gets a clear reply, and gets what could be created.
    g, m = Guild(allowed_creates=1), Member()
    it = Interaction(g, m)
    await make_select(["Valorant", "Minecraft", "CS2"]).callback(it)
    assert len(it.sent) == 1, it.sent  # never a silent failure
    msg = it.sent[-1]
    assert "Valorant" in msg and "Couldn't set up yet: Minecraft, CS2" in msg and "Try again in about 12 min" in msg, msg
    assert g.create_calls == ["Valorant", "Minecraft"], g.create_calls  # stopped after the first rate limit

    # 3. While limited, other clicks don't call Discord again but still get answered.
    it2 = Interaction(g, Member())
    await make_select(["Apex"]).callback(it2)
    assert g.create_calls == ["Valorant", "Minecraft"] and "Try again in about" in it2.sent[-1], (g.create_calls, it2.sent)

    # 4. Removing works without creating anything.
    it3 = Interaction(g, m)
    await make_select([]).callback(it3)
    assert m.roles == [] and "Removed" in it3.sent[-1], it3.sent


def test_roles():
    asyncio.run(_run())


if __name__ == "__main__":
    test_roles()
    print("tests/test_roles.py: all checks passed")
