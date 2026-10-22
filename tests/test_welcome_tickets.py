"""AI welcome greeting (with template fallback), /setup member-role fix, ticket category auto-create.

No token or xAI key needed (AI and Discord are faked).
Run:  python tests/test_welcome_tickets.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
DB_FILE = os.path.join(tempfile.mkdtemp(), "welcome.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

import discord  # noqa: E402

import test_setup as ts  # noqa: E402  (reuse its fake guild/roles)
from aifakes import Guild, Member, patched  # noqa: E402
from cogs import setup as setup_cog  # noqa: E402
from cogs import tickets as tickets_cog  # noqa: E402
from cogs import welcome as welcome_cog  # noqa: E402
from utils import ai  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402


async def _welcome(db):
    cog = welcome_cog.Welcome(types.SimpleNamespace(db=db))
    g = Guild("Pixel Haven")
    welcome = g.add("welcome")
    await gc.set_many(g, {"WELCOME_CHANNEL_ID": welcome.id})
    m = Member(g, name="Zelda Fan")
    prompts = []

    async def fake_line(server, name, count=None):
        prompts.append((server, name, count))
        return ai.Reply("Welcome, Zelda Fan, grab your Master Sword! 🗡️", 20)

    with patched(ai, welcome_line=fake_line, XAI_API_KEY="k"):
        await cog.on_member_join(m)
        post = welcome.sent[-1]
        assert prompts == [("Pixel Haven", "Zelda Fan", 42)]
        assert post.embed.description.startswith(f"Hey {m.mention}! Welcome, Zelda Fan, grab your Master Sword!")
        assert "Start here" in post.embed.description  # the template's guide stays
        assert post.kw["allowed_mentions"].everyone is False
        assert await db.count_ai(g.id, "welcome", 0) == 1

        # rate cap: a join raid falls back to the template without AI calls
        cog.recent[g.id].extend([cog.recent[g.id][0]] * welcome_cog.WELCOME_AI_PER_HOUR)
        await cog.on_member_join(Member(g))
        assert len(prompts) == 1 and "glad you made it" in welcome.sent[-1].embed.description

    async def broken(*a):
        return None

    async def slow(*a):
        await asyncio.sleep(5)

    async def naughty(*a):
        return ai.Reply("hey sexy", 5)

    for fake in (broken, slow, naughty):
        cog.recent.clear()
        with patched(ai, welcome_line=fake, XAI_API_KEY="k"), patched(welcome_cog, WELCOME_AI_TIMEOUT=0.05):
            await cog.on_member_join(Member(g))
            assert "glad you made it" in welcome.sent[-1].embed.description, fake
    with patched(ai, XAI_API_KEY=""):
        await cog.on_member_join(Member(g))
        assert "glad you made it" in welcome.sent[-1].embed.description


async def _member_role():
    gc.config.GUILD_ID, gc.config.MEMBER_ROLE_ID = 0, 0
    g = ts.FakeGuild()
    # An existing "Member" role above the bot can't be given by the I accept button.
    high = ts.FakeRole(g, "Member", position=50)
    high.managed, high._permissions = False, 0
    g._roles.append(high)
    rep = await setup_cog.SetupRunner(g, dry_run=False).run()
    saved = gc.get(g, "MEMBER_ROLE_ID")
    assert saved and saved != high.id, rep.warnings
    assert g.get_role(saved) < g.me.top_role and "@Member" in rep.created
    assert any("Not using @Member" in w for w in rep.warnings)
    # A second /setup reuses the created role.
    rep2 = await setup_cog.SetupRunner(g, dry_run=False).run()
    assert gc.get(g, "MEMBER_ROLE_ID") == saved and "@Member" not in rep2.created

    # A usable role is reused as before.
    g2 = ts.FakeGuild()
    ok = ts.FakeRole(g2, "Member", position=2)
    ok.managed, ok._permissions = False, 0
    g2._roles.append(ok)
    await setup_cog.SetupRunner(g2, dry_run=False).run()
    assert gc.get(g2, "MEMBER_ROLE_ID") == ok.id


async def _ticket_category(db):
    gc.config.GUILD_ID, gc.config.TICKET_CATEGORY_ID = 0, 0
    g = ts.FakeGuild()
    staff = ts.FakeRole(g, "Staff", position=3)
    g._roles.append(staff)
    await gc.set_many(g, {"STAFF_ROLE_ID": staff.id})
    cog = tickets_cog.Tickets(types.SimpleNamespace(add_view=lambda v: None, db=db))
    cat = await cog.ensure_category(g)
    assert isinstance(cat, discord.CategoryChannel) and cat.name == "Tickets"
    assert gc.get(g, "TICKET_CATEGORY_ID") == cat.id
    assert not cat.permissions_for(g.default_role).view_channel and cat.permissions_for(staff).view_channel
    calls = g.calls
    assert await cog.ensure_category(g) is cat and g.calls == calls  # saved, not created again

    # An existing "Tickets" category is reused instead of making a second one.
    g2 = ts.FakeGuild()
    existing = await g2.create_category("Tickets")
    assert await cog.ensure_category(g2) is existing and g2.calls == 1

    # No permission: a friendly message, nothing saved.
    g3 = ts.FakeGuild()

    async def refuse(*a, **kw):
        raise discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    g3.create_category = refuse
    assert await cog.ensure_category(g3) is None and not gc.get(g3, "TICKET_CATEGORY_ID")


async def _run():
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    try:
        await _welcome(db)
        await _member_role()
        await _ticket_category(db)
    finally:
        await db.close()


def test_welcome_and_setup_fixes():
    asyncio.run(_run())


if __name__ == "__main__":
    test_welcome_and_setup_fixes()
    print("tests/test_welcome_tickets.py: all checks passed")
