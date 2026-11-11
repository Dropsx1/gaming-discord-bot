"""Rule-based anti-spam: detection rules, escalation (delete -> 10m -> 1h), exemptions, /antispam settings.

No token needed (Discord objects are faked).
Run:  python tests/test_antispam.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
import types
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
DB_FILE = os.path.join(tempfile.mkdtemp(), "antispam.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

import discord  # noqa: E402

from aifakes import Ctx, Guild, Member, Msg  # noqa: E402
from cogs import antispam as asp  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402


class SpamMember(Member):
    def __init__(self, guild, joined_hours=24 * 30, staff=False, manage_messages=False):
        super().__init__(guild, staff=staff)
        self.joined_at = discord.utils.utcnow() - timedelta(hours=joined_hours)
        self.timeouts = []
        self._mm = manage_messages
        pass

    guild_permissions = property(lambda self: discord.Permissions(manage_guild=self._staff,
                                                                  manage_messages=self._mm))
    top_role = property(lambda self: 1)
    roles = property(lambda self: [])

    async def timeout(self, delta, reason=None):
        self.timeouts.append(delta)


def check_rules():
    g = 0  # unconfigured guild id -> defaults
    assert asp.is_phishing_domain("dlscord-gift.com")
    assert asp.is_phishing_domain("disc0rd.gg")
    assert asp.is_phishing_domain("steamcommunlty.ru")
    assert asp.is_phishing_domain("discord-nitro-claim.xyz")
    assert not asp.is_phishing_domain("discord.com")
    assert not asp.is_phishing_domain("cdn.discordapp.com")
    assert not asp.is_phishing_domain("steamcommunity.com")
    assert not asp.is_phishing_domain("discordpy.readthedocs.io")
    assert not asp.is_phishing_domain("youtube.com")
    assert asp.scam_reason("FREE NITRO for everyone https://example.xyz/claim")
    assert asp.scam_reason("steam gift 50$ -> https://bit.ly/abc")
    assert asp.scam_reason("check dlscord-app.com/gift now")
    assert not asp.scam_reason("free nitro would be nice lol")  # no link
    assert not asp.scam_reason("look at this clip https://youtube.com/watch?v=1")
    assert asp.invite_codes("join discord.gg/abc123 now") == ["abc123"]
    assert asp.invite_codes("https://discord.com/invite/XyZ") == ["XyZ"]
    assert asp.is_link_only("https://example.com/x")
    assert asp.is_link_only("<@123> https://a.co/b")
    assert not asp.is_link_only("this is my clip https://youtube.com/x")
    assert asp.caps_wall("THIS IS SO LOUD AND ANNOYING EVERYONE LOOK", 75, 20)
    assert not asp.caps_wall("GG", 75, 20)
    assert not asp.caps_wall("Normal sentence with NASA and GPU words in it ok", 75, 20)
    assert asp.emoji_count("😀" * 16) == 16
    assert asp.zalgo("h̷̢̛̖̗̘̙̜̝̞̟̠̤e̷̡̢̛̖̗̘̙̜̝̞̟̠̤l̷̢̛̖̗̘̙̜̝̞̟̠̤p")
    assert not asp.zalgo("café naïve résumé")
    r = asp.content_reason(g, "hi", 6, False, None)
    assert r and "mentions" in r
    assert asp.content_reason(g, "hey all", 0, True, None)
    assert asp.content_reason(g, "https://x.io/a", 0, False, 3600) and \
        "new member" in asp.content_reason(g, "https://x.io/a", 0, False, 3600)
    assert asp.content_reason(g, "https://x.io/a", 0, False, 3 * 86400) is None
    assert asp.content_reason(g, "normal chat about games", 2, False, 3600) is None


async def _run():
    gc.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    guild = Guild()
    guild.me = types.SimpleNamespace(guild_permissions=discord.Permissions(manage_messages=True,
                                                                           moderate_members=True), top_role=5)
    log = guild.add("mod-log", public=False)
    general = guild.add("general")
    other = guild.add("memes")
    await gc.set_many(guild, {"MOD_LOG_CHANNEL_ID": log.id})

    bot = types.SimpleNamespace(db=db, guilds=[guild])

    async def fetch_invite(code, with_counts=False):
        return types.SimpleNamespace(guild=types.SimpleNamespace(id=guild.id if code == "home" else 1))
    bot.fetch_invite = fetch_invite
    cog = asp.AntiSpam(bot)

    def msg(author, text, ch=general):
        m = Msg(guild, ch, author, text)
        m.mention_everyone = False
        return m

    # normal chat is untouched
    alice = SpamMember(guild)
    for t in ["hey", "what's up", "anyone playing tonight?"]:
        m = msg(alice, t)
        await cog.on_message(m)
        assert not m.deleted

    # duplicate spam across channels: 3rd copy -> strike 1, all copies deleted, warning, mod log
    bob = SpamMember(guild)
    copies = [msg(bob, "buy my stuff", ch) for ch in (general, other, general)]
    for m in copies:
        await cog.on_message(m)
    assert all(m.deleted for m in copies), "duplicates should be deleted"
    assert not bob.timeouts
    assert any("anti-spam" in (s.content or "") for s in general.sent), "warning expected"
    assert log.sent and "Anti-spam" in log.sent[-1].embed.title

    # 2nd strike (after the grace period) -> 10 minute timeout
    key = (guild.id, bob.id)
    cog.tracker.last_strike[key] -= 10
    m = msg(bob, "@everyone look")
    await cog.on_message(m)
    assert m.deleted and bob.timeouts == [timedelta(minutes=10)], bob.timeouts
    cog.tracker.last_strike[key] -= 10
    m = msg(bob, "join discord.gg/raidserver")
    await cog.on_message(m)
    assert m.deleted and bob.timeouts[-1] == timedelta(hours=1), bob.timeouts

    # flood: 5 messages in 5s from one user
    carl = SpamMember(guild)
    flood = [msg(carl, f"spam {i}") for i in range(5)]
    for m in flood:
        await cog.on_message(m)
    assert all(m.deleted for m in flood) and not carl.timeouts

    # own-server invite is fine; foreign one isn't
    dana = SpamMember(guild)
    m = msg(dana, "invite your friends: discord.gg/home")
    await cog.on_message(m)
    assert not m.deleted
    m = msg(dana, "come to discord.gg/other")
    await cog.on_message(m)
    assert m.deleted

    # new member posting only a link
    newbie = SpamMember(guild, joined_hours=1)
    m = msg(newbie, "https://sketchy.example/x")
    await cog.on_message(m)
    assert m.deleted

    # exemptions: staff, Manage Messages, exempt channel/role, disabled
    for who in (SpamMember(guild, staff=True), SpamMember(guild, manage_messages=True)):
        m = msg(who, "@everyone https://discord.gg/other")
        await cog.on_message(m)
        assert not m.deleted
    await gc.set_many(guild, {f"{asp.EXEMPT_CH}{other.id}": 1})
    m = msg(SpamMember(guild), "@everyone", other)
    await cog.on_message(m)
    assert not m.deleted

    admin = SpamMember(guild, staff=True)
    ctx = Ctx(guild, general, admin)
    await cog.antispam.callback(cog, ctx)
    assert "Anti-spam is on" in ctx.replies[-1]
    await cog.antispam_set.callback(cog, ctx, "mentions", 8)
    assert asp.setting(guild, "MENTIONS") == 8
    await cog.antispam_set.callback(cog, ctx, "mentions", 999)
    assert asp.setting(guild, "MENTIONS") == 8 and "between" in ctx.replies[-1]
    await cog.antispam_off.callback(cog, ctx)
    m = msg(SpamMember(guild), "@everyone free stuff")
    await cog.on_message(m)
    assert not m.deleted
    await cog.antispam_on.callback(cog, ctx)
    await cog.antispam_reset.callback(cog, ctx)
    assert asp.setting(guild, "MENTIONS") == 5

    await db.close()


def test_rules():
    check_rules()


def test_antispam_flow():
    asyncio.run(_run())


if __name__ == "__main__":
    check_rules()
    asyncio.run(_run())
    print("antispam tests passed")
