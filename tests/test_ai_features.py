"""AI features: limits, channel rules, filters, usage log, automod. No token or xAI key needed (AI is faked).

Run:  python tests/test_ai_features.py      (or: python -m pytest tests/)
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
DB_FILE = os.path.join(tempfile.mkdtemp(), "ai.db")  # own DB, so tests never share state
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

import discord  # noqa: E402

from cogs import ai_features as aif  # noqa: E402
from cogs import setup as setup_cog  # noqa: E402
from utils import ai  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402

_ids = itertools.count(70_000)


class FakeText(discord.TextChannel):  # passes isinstance(..., TextChannel)
    __hash__ = object.__hash__
    mention = property(lambda self: f"<#{self.id}>")

    def __init__(self, guild, name, public=True, category_id=None):
        self.id, self.guild, self.name, self.public, self.category_id = next(_ids), guild, name, public, category_id
        self.sent = []

    def permissions_for(self, who):
        return types.SimpleNamespace(view_channel=self.public)

    async def send(self, content=None, **kw):
        self.sent.append((content, kw))


class Guild:
    def __init__(self):
        self.id, self.name = next(_ids), "Test Server"
        self.default_role = object()
        self._channels = {}
        self.me = types.SimpleNamespace(guild_permissions=discord.Permissions(manage_messages=True))

    def add(self, name, **kw):
        ch = FakeText(self, name, **kw)
        self._channels[ch.id] = ch
        return ch

    @property
    def text_channels(self):
        return list(self._channels.values())

    def get_channel(self, cid):
        return self._channels.get(cid)

    def get_role(self, rid):
        return None


class Member(discord.Member):
    def __init__(self, guild, staff=False):
        self.guild, self._staff = guild, staff
        self._user = types.SimpleNamespace(id=next(_ids), bot=False)
        self.sent = []

    id = property(lambda self: self._user.id)
    bot = property(lambda self: False)
    guild_permissions = property(lambda self: discord.Permissions(manage_guild=self._staff))
    mention = property(lambda self: f"<@{self.id}>")
    display_name = property(lambda self: f"user{self.id}")
    display_avatar = property(lambda self: types.SimpleNamespace(url="https://cdn.example/a.png"))

    def get_role(self, rid):
        return None

    async def send(self, text):
        self.sent.append(text)


class Msg:
    def __init__(self, guild, channel, author, content):
        self.guild, self.channel, self.author, self.content = guild, channel, author, content
        self.webhook_id, self.raw_mentions, self.raw_role_mentions = None, [], []
        self.deleted = False

    async def delete(self):
        self.deleted = True


class Ctx:
    def __init__(self, guild, channel, author):
        self.guild, self.channel, self.author, self.replies = guild, channel, author, []

    async def send(self, content=None, **kw):
        self.replies.append(content)

    async def defer(self, **kw):
        pass


def test_prefilter_levels():
    assert aif.prefilter("gg wp everyone, nice match", 0, 3) is None
    assert aif.prefilter("FREE NITRO click here", 0, 1) == "scam"
    assert aif.prefilter("kys", 0, 1) == "threat"
    assert aif.prefilter("you're such an idiot", 0, 1) is None  # low ignores insults
    assert aif.prefilter("you're such an idiot", 0, 2) == "insult"
    assert aif.prefilter("join discord.gg/abc123", 0, 2) == "invite link"
    assert aif.prefilter("this game is shit lol", 0, 2) is None
    assert aif.prefilter("this game is shit lol", 0, 3) == "profanity"
    assert aif.prefilter("hi", 5, 2) == "mass mentions"
    assert aif.prefilter("hi", 5, 1) is None
    assert aif.prefilter("a" * 20, 0, 2) == "repeated characters"
    assert aif.prefilter("I CANNOT BELIEVE WE LOST THAT ROUND AGAIN TODAY", 0, 3) == "all caps"


def test_nsfw_prompt():
    assert aif.nsfw_prompt("a NUDE knight")
    assert aif.nsfw_prompt("hentai style")
    assert not aif.nsfw_prompt("a knight in shiny armor at sunset")
    assert not aif.nsfw_prompt("Sussex countryside")  # whole words only


def test_cooldowns():
    cd = aif.Cooldowns(30)
    assert cd.remaining(1, 2, now=100) == 0
    cd.hit(1, 2, now=100)
    assert cd.remaining(1, 2, now=110) == 20
    assert cd.remaining(1, 3, now=110) == 0  # per user
    assert cd.remaining(9, 2, now=110) == 0  # per server
    assert cd.remaining(1, 2, now=131) == 0
    cd.reset(1, 2)
    assert cd.remaining(1, 2, now=101) == 0


def test_link_key():
    assert setup_cog.resolve_link_key("ASK_CHANNEL_ID") == "ASK_CHANNEL_ID"
    assert setup_cog.resolve_link_key("ask channel") == "ASK_CHANNEL_ID"
    assert setup_cog.LINK_KEYS["ASK_CHANNEL_ID"][1] == "text"


async def _run_all():
    aif.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    bot = types.SimpleNamespace(db=db)
    cog = aif.AIFeatures(bot)
    calls = []

    async def fake_chat(prompt, system, max_tokens=400):
        calls.append(("chat", prompt))
        return ai.Reply("Try the Ranger build!", 42)

    verdicts = {}

    async def fake_classify(text):
        calls.append(("classify", text))
        return ai.Reply(verdicts.get(text, "ok"), 7)

    async def fake_image(prompt):
        calls.append(("image", prompt))
        return b"\x89PNG fake"

    ai.chat, ai.classify, ai.image = fake_chat, fake_classify, fake_image
    try:
        g = Guild()
        bc, art, general = g.add("bot-commands"), g.add("ai-art"), g.add("general-chat")
        modlog = g.add("mod-log", public=False)
        user, staff = Member(g), Member(g, staff=True)

        # --- AI off: friendly message, no calls
        ai.XAI_API_KEY = ""
        ctx = Ctx(g, bc, user)
        await cog.ask.callback(cog, ctx, question="hi")
        await cog.imagine.callback(cog, ctx, prompt="a cat")
        assert ctx.replies == [aif.AI_OFF, aif.AI_OFF] and not calls

        ai.XAI_API_KEY = "test-key"
        # --- /ask: #bot-commands by default (found by name), linkable elsewhere
        ctx = Ctx(g, general, user)
        await cog.ask.callback(cog, ctx, question="best build?")
        assert "works in" in ctx.replies[0] and bc.mention in ctx.replies[0] and not calls
        await gc.set_many(g, {"BOT_COMMANDS_CHANNEL_ID": bc.id})
        await gc.set_many(g, {"ASK_CHANNEL_ID": general.id})  # /setup-link
        await cog.ask.callback(cog, ctx, question="best build?")
        assert calls == [("chat", "best build?")], calls
        await cog.ask.callback(cog, ctx, question="again?")  # cooldown
        assert ctx.replies[-1].startswith("⏳") and len(calls) == 1
        assert await db.count_ai(g.id, "ask", aif.day_start(), user.id) == 1

        # daily limit (cooldown cleared to isolate it)
        for _ in range(aif.ASK_DAILY_LIMIT - 1):
            await db.log_ai(g.id, user.id, "ask", 10)
        cog.ask_cd.reset(g.id, user.id)
        await cog.ask.callback(cog, ctx, question="one more")
        assert ctx.replies[-1].startswith("📅"), ctx.replies[-1]

        # --- /imagine: channels, keyword block, AI block, success, caps
        calls.clear()
        ctx = Ctx(g, general, user)
        await cog.imagine.callback(cog, ctx, prompt="a dragon")
        assert "works in" in ctx.replies[-1] and not calls
        ctx = Ctx(g, art, user)
        await cog.imagine.callback(cog, ctx, prompt="naked elf")
        assert ctx.replies[-1].startswith("🚫") and not calls  # keyword block, no AI spend
        verdicts["Image generation prompt: a sneaky thing"] = "nsfw"
        await cog.imagine.callback(cog, ctx, prompt="a sneaky thing")
        assert ctx.replies[-1].startswith("🚫") and [c[0] for c in calls] == ["classify"]
        cog.imagine_cd.reset(g.id, user.id)
        await cog.imagine.callback(cog, ctx, prompt="a dragon over a castle")
        assert calls[-1] == ("image", "a dragon over a castle")
        assert await db.count_ai(g.id, "imagine", aif.day_start()) == 1
        await cog.imagine.callback(cog, ctx, prompt="another")
        assert ctx.replies[-1].startswith("⏳")
        # server-wide cap
        await gc.set_many(g, {"IMAGINE_DAILY_CAP": 1})
        cog.imagine_cd.reset(g.id, staff.id)
        sctx = Ctx(g, bc, staff)
        await cog.imagine.callback(cog, sctx, prompt="a robot")
        assert "server has used all 1" in sctx.replies[-1], sctx.replies

        # --- automod
        calls.clear()
        spam = "FREE NITRO here"
        verdicts[spam] = "spam"
        msg = Msg(g, general, user, spam)
        assert cog.automod_skip(msg)  # off by default
        await gc.set_many(g, {"AUTOMOD_ENABLED": 1, "LOG_CHANNEL_ID": modlog.id})
        assert not cog.automod_skip(msg)
        assert cog.automod_skip(Msg(g, general, staff, spam))  # staff skipped
        assert cog.automod_skip(Msg(g, modlog, user, spam))    # private / mod-log skipped

        await cog.on_message(Msg(g, general, user, "gg everyone"))
        assert not calls  # pre-filter: clean messages never reach the AI
        await cog.on_message(msg)
        assert calls == [("classify", spam)] and msg.deleted
        assert user.sent and "removed" in user.sent[0]
        content, kw = modlog.sent[-1]
        embed = kw["embed"]
        assert "spam" in [f.value for f in embed.fields] and kw["view"] is not None
        flag = await db.get_flag(1)
        assert flag["content"] == spam and flag["verdict"] == "spam"
        assert await db.mark_flag_undone(1) and not await db.mark_flag_undone(1)

        # suspicious but AI says ok -> left alone
        cog.automod_cd.reset(g.id, user.id)
        ok_msg = Msg(g, general, user, "you idiot, nice clutch")
        await cog.on_message(ok_msg)
        assert not ok_msg.deleted

        # --- usage summary
        rows = {r["feature"]: r for r in await db.ai_usage_summary(g.id, aif.month_start())}
        assert rows["ask"]["calls"] == aif.ASK_DAILY_LIMIT and rows["imagine"]["images"] == 1
        assert rows["automod"]["tokens"] == 14 and "imagine-check" in rows
    finally:
        await db.close()


def test_ai_features():
    asyncio.run(_run_all())


if __name__ == "__main__":
    test_prefilter_levels()
    test_nsfw_prompt()
    test_cooldowns()
    test_link_key()
    test_ai_features()
    print("tests/test_ai_features.py: all checks passed")
