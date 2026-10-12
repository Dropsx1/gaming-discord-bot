"""Tests for /setup and per-server config, using fake Discord objects (no token needed).

Run:  python tests/test_setup.py      (or: python -m pytest tests/)
"""

import asyncio
import itertools
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOME_GUILD = 111
os.environ.update(
    DB_PATH=os.path.join(tempfile.mkdtemp(), "test.db"),
    GUILD_ID=str(HOME_GUILD),
    WELCOME_CHANNEL_ID="999",  # env ID: must only apply to the home server
)

import discord  # noqa: E402

from cogs import setup as setup_cog  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402

_ids = itertools.count(10_000)


# ------------------------------------------------------------------ fakes
def _view(overwrites: dict, target, guild) -> bool:
    """Tiny permission resolver: target's own overwrite, then @everyone, default allow."""
    for key in (target, guild.default_role):
        ow = overwrites.get(key)
        if ow is not None and ow.view_channel is not None:
            return ow.view_channel
    return True


class FakeRole(discord.Role):
    def __init__(self, guild, name, position, role_id=None):
        self.guild, self.name, self.position = guild, name, position
        self.id = role_id or next(_ids)
        self.kwargs = {}

    def __repr__(self):
        return f"<Role {self.name}>"

    __hash__ = object.__hash__


class _ChannelMixin:
    def _init(self, guild, name, category=None, overwrites=None):
        self.guild, self.name, self.id = guild, name, next(_ids)
        self.category_id = category.id if category is not None else None
        self._ow = dict(overwrites or (category.overwrites if category is not None else {}))

    @property
    def overwrites(self):
        return dict(self._ow)

    @property
    def mention(self):
        return f"<#{self.id}>"

    def permissions_for(self, target):
        return discord.Permissions(view_channel=_view(self._ow, target, self.guild))

    __hash__ = object.__hash__


class FakeCategory(_ChannelMixin, discord.CategoryChannel):
    def __init__(self, guild, name, overwrites=None):
        self._init(guild, name, overwrites=overwrites)


class FakeText(_ChannelMixin, discord.TextChannel):
    def __init__(self, guild, name, category=None, overwrites=None):
        self._init(guild, name, category, overwrites)


class FakeVoice(_ChannelMixin, discord.VoiceChannel):
    def __init__(self, guild, name, category=None):
        self._init(guild, name, category)


class FakeMe:
    def __init__(self, guild, perms):
        self.id, self.guild = next(_ids), guild
        self.top_role = FakeRole(guild, "Bot", position=10)
        self.guild_permissions = perms


class FakeGuild:
    def __init__(self, guild_id=None, perms=None):
        self.id = guild_id or next(_ids)
        self.name = f"guild-{self.id}"
        self.default_role = FakeRole(self, "@everyone", 0, role_id=self.id)
        self._roles = [self.default_role]
        self._channels = []
        self.me = FakeMe(self, perms or discord.Permissions(manage_roles=True, manage_channels=True))
        self._roles.append(self.me.top_role)
        self.calls = 0  # Discord API "create" calls made

    @property
    def roles(self):
        return list(self._roles)

    @property
    def channels(self):
        return list(self._channels)

    @property
    def categories(self):
        return [c for c in self._channels if isinstance(c, discord.CategoryChannel)]

    def get_role(self, rid):
        return next((r for r in self._roles if r.id == rid), None)

    def get_channel(self, cid):
        return next((c for c in self._channels if c.id == cid), None)

    async def create_role(self, name, colour=None, mentionable=False, reason=None):
        self.calls += 1
        role = FakeRole(self, name, position=1)  # Discord puts new roles at the bottom
        role.kwargs = {"mentionable": mentionable}
        self._roles.append(role)
        return role

    async def create_category(self, name, overwrites=None, reason=None):
        self.calls += 1
        cat = FakeCategory(self, name, overwrites)
        self._channels.append(cat)
        return cat

    async def create_text_channel(self, name, category=None, reason=None, overwrites=None):
        self.calls += 1
        ch = FakeText(self, name, category, overwrites)
        self._channels.append(ch)
        return ch

    async def create_voice_channel(self, name, category=None, reason=None):
        self.calls += 1
        ch = FakeVoice(self, name, category)
        self._channels.append(ch)
        return ch


EXPECTED_ITEMS = len(setup_cog.ROLES) + len(setup_cog.LAYOUT) + sum(len(c.channels) for c in setup_cog.LAYOUT)


# ------------------------------------------------------------------ tests
async def _run_all():
    db = Database(os.environ["DB_PATH"])
    await db.connect()
    await gc.init(db)
    try:
        # 1. Dry run on an empty server: reports everything, changes nothing.
        g = FakeGuild()
        rep = await setup_cog.SetupRunner(g, dry_run=True).run()
        assert len(rep.created) == EXPECTED_ITEMS == 27, (len(rep.created), EXPECTED_ITEMS)
        assert g.calls == 0 and not rep.failed and gc.saved(g) == {}

        # 2. Real run: creates everything, saves every ID.
        rep = await setup_cog.SetupRunner(g, dry_run=False).run()
        assert len(rep.created) == 27 and not rep.reused and not rep.failed and not rep.warnings, rep.__dict__
        saved = gc.saved(g)
        assert len(saved) == 27, len(saved)
        rows = await db.fetchall("SELECT key FROM guild_config WHERE guild_id = ?", (g.id,))
        assert len(rows) == 27  # persisted, not just cached

        # Lookups resolve to the created items.
        assert gc.channel(g, "PROMPT_CHANNEL_ID").name == "prompt-of-the-day"
        assert gc.channel(g, "DIGEST_CHANNEL_ID").name == "tech-digest"
        assert gc.channel(g, "START_HERE_CHANNEL_ID").name == "rules"
        assert gc.channel(g, "JOIN_TO_CREATE_CHANNEL_ID").name == "➕ Join to Create"
        assert isinstance(gc.channel(g, "TICKET_CATEGORY_ID"), discord.CategoryChannel)
        assert gc.role(g, "STAFF_ROLE_ID").name == "Staff"
        assert gc.get(g, "MOD_LOG_CHANNEL_ID") == saved["LOG_CHANNEL_ID"]  # falls back to the staff log
        assert gc.role(g, "LFG_ROLE_ID").kwargs["mentionable"] is True

        # New roles sit below the bot.
        for key in ("STAFF_ROLE_ID", "MEMBER_ROLE_ID", "PROMPT_KING_ROLE_ID", "REMIX_CHAMPION_ROLE_ID", "LFG_ROLE_ID"):
            assert gc.role(g, key) < g.me.top_role

        # Private categories: hidden from @everyone, visible to Staff and the bot.
        staff = gc.role(g, "STAFF_ROLE_ID")
        for key in ("TICKET_CATEGORY_ID", "STAFF_CATEGORY_ID"):
            cat = gc.channel(g, key)
            assert not cat.permissions_for(g.default_role).view_channel
            assert cat.permissions_for(staff).view_channel and cat.permissions_for(g.me).view_channel
        modlog = gc.channel(g, "LOG_CHANNEL_ID")
        assert not modlog.permissions_for(g.default_role).view_channel  # inherits the Staff category

        # Read-only and thread-only channels.
        rules_ow = gc.channel(g, "START_HERE_CHANNEL_ID").overwrites[g.default_role]
        assert rules_ow.send_messages is False
        prompt_ow = gc.channel(g, "PROMPT_CHANNEL_ID").overwrites
        assert prompt_ow[g.default_role].send_messages is False
        assert prompt_ow[g.default_role].send_messages_in_threads is True
        assert prompt_ow[g.me].create_public_threads is True
        assert g.default_role not in gc.channel(g, "GENERAL_CHAT_CHANNEL_ID").overwrites  # normal chat untouched

        # 3. Running again reuses everything and creates nothing.
        calls = g.calls
        rep = await setup_cog.SetupRunner(g, dry_run=False).run()
        assert not rep.created and len(rep.reused) == 27 and g.calls == calls, rep.__dict__
        assert len(g.channels) == 22 and len(g.roles) == 7  # 6 categories + 16 channels; 5 roles + bot + @everyone

        # 4. Existing items with matching names are reused (case-insensitive); problems are flagged.
        g2 = FakeGuild()
        high_staff = FakeRole(g2, "STAFF", position=20)  # above the bot
        g2._roles.append(high_staff)
        open_tickets = FakeCategory(g2, "tickets")  # visible to @everyone
        g2._channels.append(open_tickets)
        other = FakeCategory(g2, "Old Stuff")
        g2._channels.append(other)
        old_general = FakeText(g2, "general-chat", other)
        g2._channels.append(old_general)
        rep = await setup_cog.SetupRunner(g2, dry_run=False).run()
        assert gc.get(g2, "STAFF_ROLE_ID") == high_staff.id
        assert gc.get(g2, "TICKET_CATEGORY_ID") == open_tickets.id
        assert gc.get(g2, "GENERAL_CHAT_CHANNEL_ID") == old_general.id  # reused, not moved or duplicated
        assert len(rep.created) == 24 and len(rep.reused) == 3, (len(rep.created), len(rep.reused))
        joined = " ".join(rep.warnings)
        assert "above my top role" in joined and "visible to @everyone" in joined, rep.warnings
        assert sum(1 for c in g2.channels if c.name == "general-chat") == 1

        # 5. Missing permissions: nothing is created, the reason is reported.
        g3 = FakeGuild(perms=discord.Permissions(manage_channels=True))
        rep = await setup_cog.SetupRunner(g3, dry_run=False).run()
        assert g3.calls == 0 and rep.failed and "Manage Roles" in rep.failed[0]

        # 6. Env-var IDs apply to the home server only; saved IDs win over env.
        home, other_guild = FakeGuild(HOME_GUILD), FakeGuild()
        assert gc.get(home, "WELCOME_CHANNEL_ID") == 999
        assert gc.get(other_guild, "WELCOME_CHANNEL_ID") == 0
        await gc.set_many(home, {"WELCOME_CHANNEL_ID": 12345})
        assert gc.get(home, "WELCOME_CHANNEL_ID") == 12345

        # 7. Saved config survives a restart (reload from the database).
        await gc.init(db)
        assert gc.saved(g) == saved and gc.get(home, "WELCOME_CHANNEL_ID") == 12345

        # 8. The summary embed fits Discord's limits.
        embed = setup_cog.build_summary(g2, rep, dry_run=False)
        assert len(embed) <= 6000 and len(embed.fields) <= 25
        assert all(len(f.value) <= 1024 for f in embed.fields)
    finally:
        await db.close()


def test_setup():
    asyncio.run(_run_all())


if __name__ == "__main__":
    test_setup()
    print("tests/test_setup.py: all checks passed")
