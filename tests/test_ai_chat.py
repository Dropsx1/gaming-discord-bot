"""Chat that remembers: /ask memory per channel/thread, expiry, /ask reset, continue in thread, @mention chat.

No token or xAI key needed (AI is faked).
Run:  python tests/test_ai_chat.py      (or: python -m pytest tests/)
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
DB_FILE = os.path.join(tempfile.mkdtemp(), "chat.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

from aifakes import BotAuthor, Ctx, Guild, Interaction, Member, Msg, Sent, patched  # noqa: E402
from cogs import ai_features as aif  # noqa: E402
from utils import ai  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402


async def _run():
    aif.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    me = BotAuthor()
    commands_known = {"help"}
    bot = types.SimpleNamespace(db=db, user=me, get_command=lambda name: name if name in commands_known else None)
    cog = aif.AIFeatures(bot)
    seen = []

    async def fake_converse(history, system, feature="ask", max_tokens=400):
        seen.append([dict(m) for m in history])
        return ai.Reply(f"answer {len(seen)}", 10)

    try:
        await _body(db, cog, me, seen, fake_converse)
    finally:
        await db.close()


async def _body(db, cog, me, seen, fake_converse):
    with patched(ai, converse=fake_converse, XAI_API_KEY="k"):
        g = Guild()
        bc = g.add("bot-commands")
        await gc.set_many(g, {"ASK_CHANNEL_ID": bc.id})
        alice, bob = Member(g, name="Alice"), Member(g, name="Bob")

        # 1. Memory: the second question sees the first exchange (even from another member).
        await cog.ask.callback(cog, Ctx(g, bc, alice), question="best class in Elden Ring?")
        ctx = Ctx(g, bc, bob)
        await cog.ask.callback(cog, ctx, question="and the best weapon for it?")
        assert [m["role"] for m in seen[1]] == ["user", "assistant", "user"], seen[1]
        assert seen[1][0]["content"] == "Alice: best class in Elden Ring?" and seen[1][1]["content"] == "answer 1"
        assert seen[1][2]["content"] == "Bob: and the best weapon for it?"
        assert isinstance(ctx.kw[-1].get("view"), aif.ContinueView)  # continue-in-thread button

        # 2. Only the last N exchanges are sent.
        old_turns = aif.ASK_MEMORY_TURNS
        aif.ASK_MEMORY_TURNS = 1
        cog.ask_cd.reset(g.id, alice.id)
        await cog.ask.callback(cog, Ctx(g, bc, alice), question="third")
        assert len(seen[-1]) == 3 and seen[-1][0]["content"].startswith("Bob:"), seen[-1]
        aif.ASK_MEMORY_TURNS = old_turns

        # 3. Expiry: old messages are ignored and pruned.
        await db.execute("UPDATE ai_memory SET created_at = created_at - ?", (aif.ASK_MEMORY_HOURS * 3600 + 5,))
        assert await cog.memory(g.id, bc.id) == []
        assert await db.prune_memory(int(__import__("time").time()) - aif.ASK_MEMORY_HOURS * 3600) == 6

        # 4. /ask reset forgets the channel.
        cog.ask_cd.reset(g.id, alice.id)
        await cog.ask.callback(cog, Ctx(g, bc, alice), question="remember me")
        assert len(await cog.memory(g.id, bc.id)) == 2
        rctx = Ctx(g, bc, alice)
        await cog.ask_reset.callback(cog, rctx)
        assert rctx.replies[-1].startswith("🧹") and await cog.memory(g.id, bc.id) == []

        # 5. Continue in thread: creates a thread and copies the conversation into it.
        cog.ask_cd.reset(g.id, alice.id)
        await cog.ask.callback(cog, Ctx(g, bc, alice), question="plan a raid night")
        reply_msg = Sent("x")
        reply_msg.channel = bc
        inter = Interaction(g, bc, alice, message=reply_msg)
        await cog.continue_in_thread(inter, "plan a raid night")
        thread = reply_msg.threads[0]
        assert "Continue in" in inter.response.messages[0][0]
        assert [m["content"] for m in await cog.memory(g.id, thread.id)] == ["Alice: plan a raid night", f"answer {len(seen)}"]
        assert thread.sent and "remember" in thread.sent[0].content

        # 6. @mention chat: same memory, cooldown and daily limit as /ask.
        cog.ask_cd.reset(g.id, bob.id)
        n = len(seen)
        msg = Msg(g, thread, bob, f"<@{me.id}> what time works?", mentions=[me])
        await cog.mention_chat(msg)
        assert len(seen) == n + 1 and seen[-1][0]["content"] == "Alice: plan a raid night"
        assert msg.replies == [f"answer {n + 1}"]
        again = Msg(g, thread, bob, f"<@{me.id}> again?", mentions=[me])
        await cog.mention_chat(again)
        assert again.replies[0].startswith("⏳") and len(seen) == n + 1  # cooldown shared with /ask
        cog.ask_cd.reset(g.id, bob.id)
        for _ in range(aif.ASK_DAILY_LIMIT):
            await db.log_ai(g.id, bob.id, "ask", 1)
        limited = Msg(g, thread, bob, f"<@{me.id}> one more", mentions=[me])
        await cog.mention_chat(limited)
        assert limited.replies[0].startswith("📅") and len(seen) == n + 1

        # Not for the AI: no mention, bots, prefix commands ("@bot help"), wrong channel (silent).
        assert cog.mention_question(Msg(g, bc, alice, "hello")) is None
        assert cog.mention_question(Msg(g, bc, me, f"<@{me.id}> hi", mentions=[me])) is None
        assert cog.mention_question(Msg(g, bc, alice, f"<@{me.id}> help", mentions=[me])) is None
        assert cog.mention_question(Msg(g, bc, alice, f"<@{me.id}> help me pick a game", mentions=[me])) is None
        assert cog.mention_question(Msg(g, bc, alice, f"hey <@{me.id}> help me pick", mentions=[me])) == "hey help me pick"
        other = g.add("general")
        quiet = Msg(g, other, alice, f"<@{me.id}> hi", mentions=[me])
        cog.ask_cd.reset(g.id, alice.id)
        await cog.mention_chat(quiet)
        assert quiet.replies == [] and len(seen) == n + 1

        # AI failure: friendly message, cooldown refunded, nothing remembered.
        async def broken(*a, **kw):
            return None

        with patched(ai, converse=broken):
            fail = Msg(g, bc, alice, f"<@{me.id}> hi", mentions=[me])
            before = len(await cog.memory(g.id, bc.id))
            await cog.mention_chat(fail)
            assert fail.replies[0].startswith("😵") and cog.ask_cd.remaining(g.id, alice.id) == 0
            assert len(await cog.memory(g.id, bc.id)) == before


def test_chat_memory():
    asyncio.run(_run())


if __name__ == "__main__":
    test_chat_memory()
    print("tests/test_ai_chat.py: all checks passed")
