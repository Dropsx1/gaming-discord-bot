"""/summarize (+ ticket summary on close) and /trivia (+ leaderboard). AI is faked; no token or key needed.

Run:  python tests/test_ai_extras.py      (or: python -m pytest tests/)
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
DB_FILE = os.path.join(tempfile.mkdtemp(), "extras.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

from aifakes import BotAuthor, Ctx, Guild, Interaction, Member, Msg, patched  # noqa: E402
from cogs import ai_extras as ext  # noqa: E402
from cogs import tickets as tickets_cog  # noqa: E402
from utils import ai, summaries  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402

QUIZ = {"question": "Which company made Zelda?", "options": ["Sega", "Nintendo", "Sony", "Atari"], "answer": 1,
        "explanation": "Nintendo published it in 1986."}


async def _summarize(db, bot):
    cog = ext.AIExtras(bot)
    got = []

    async def fake_summarize(lines, feature="summarize"):
        got.append((list(lines), feature))
        return {"topics": ["raid night"], "decisions": ["Friday 8pm"], "open_questions": []}, 77

    g = Guild()
    ch = g.add("general")
    alice, bob, botty = Member(g, name="Alice"), Member(g, name="Bob"), BotAuthor()
    for i in range(250):
        author = botty if i % 5 == 0 else (alice if i % 2 else bob)
        ch.history_msgs.append(Msg(g, ch, author, f"msg {i}"))
    ch.history_msgs.append(Msg(g, ch, alice, ""))  # empty: skipped

    lines = await summaries.collect_lines(ch, 500)  # capped at 200
    assert len(lines) == 200 and all("SomeBot" not in l for l in lines)
    assert lines[-1] == "Alice: msg 249" and lines[0].endswith("msg ") is False

    with patched(ai, summarize=fake_summarize, XAI_API_KEY="k"):
        ctx = Ctx(g, ch, alice)
        await cog.summarize.callback(cog, ctx, messages=30)
        embed = ctx.kw[-1]["embed"]
        assert ctx.kw[-1]["ephemeral"] is True  # private by default
        assert [f.name for f in embed.fields] == ["🗂️ Topics", "✅ Decisions", "❓ Open questions"]
        assert "Friday 8pm" in embed.fields[1].value and embed.fields[2].value == "None"
        assert len(got[-1][0]) == 30 and got[-1][1] == "summarize"
        await cog.summarize.callback(cog, ctx, messages=30)
        assert ctx.replies[-1].startswith("⏳")
        assert await db.count_ai(g.id, "summarize", ext.day_start(), alice.id) == 1

        staff = Member(g, staff=True)
        sctx = Ctx(g, ch, staff)
        await cog.summarize.callback(cog, sctx, messages=10, public=True)
        assert sctx.kw[-1]["ephemeral"] is False

        empty = g.add("empty")
        ectx = Ctx(g, empty, bob)
        await cog.summarize.callback(cog, ectx, messages=10)
        assert "nothing to summarize" in ectx.replies[-1] and cog.summarize_cd.remaining(g.id, bob.id) == 0

    with patched(ai, XAI_API_KEY=""):
        octx = Ctx(g, ch, bob)
        await cog.summarize.callback(cog, octx, messages=10)
        assert octx.replies[-1] == ext.AI_OFF

    # Ticket: staff close with AI summary -> summary embed in the ticket log.
    tlog = g.add("ticket-log", public=False)
    await gc.set_many(g, {"TICKET_LOG_CHANNEL_ID": tlog.id})
    ticket = g.add("ticket-alice")
    ticket.topic = f"Support ticket | owner:{alice.id}"
    ticket.history_msgs = [Msg(g, ticket, alice, "my game crashes"), Msg(g, ticket, staff, "try verifying files")]
    deleted = []

    async def delete(reason=None):
        deleted.append(reason)
    ticket.delete = delete
    tcog = tickets_cog.Tickets(types.SimpleNamespace(add_view=lambda v: None, db=db))
    with patched(tickets_cog, CLOSE_DELAY=0), patched(ai, summarize=fake_summarize, XAI_API_KEY="k"):
        await tcog.close(ticket, staff, transcript=False, summarize=True)
        assert deleted and got[-1][1] == "ticket-summary"
        assert got[-1][0] == ["Alice: my game crashes", "user%d: try verifying files" % staff.id]
        titles = [m.embed.title for m in tlog.sent]
        assert "🔒 Ticket Closed" in titles and any(t.startswith("📝 Ticket summary") for t in titles)
        # The owner (not staff) can't request one.
        ticket2 = g.add("ticket-2")
        ticket2.topic = f"owner:{alice.id}"
        ticket2.history_msgs = list(ticket.history_msgs)
        ticket2.delete = delete
        n = len(got)
        await tcog.close(ticket2, alice, transcript=False, summarize=True)
        assert len(got) == n
        assert not any(getattr(c, "label", "").endswith("AI summary") for c in tickets_cog.CloseConfirmView(tcog, alice).children)
        assert any(getattr(c, "label", "").endswith("AI summary") for c in tickets_cog.CloseConfirmView(tcog, staff).children)


async def _trivia(db, bot):
    cog = ext.AIExtras(bot)
    asked = []

    async def fake_trivia(topic, difficulty):
        asked.append((topic, difficulty))
        return dict(QUIZ), 50

    g = Guild()
    ch = g.add("games")
    alice, bob, carol = Member(g, name="Alice"), Member(g, name="Bob"), Member(g, name="Carol")
    with patched(ai, trivia_question=fake_trivia, XAI_API_KEY="k"):
        ctx = Ctx(g, ch, alice)
        await cog.trivia.callback(cog, ctx, topic="Zelda", difficulty="hard")
        view = ctx.kw[-1]["view"]
        assert asked == [("Zelda", "hard")] and isinstance(view, ext.TriviaView) and len(view.children) == 4
        assert "Nintendo" in ctx.kw[-1]["embed"].description

        i1, i2, i3 = Interaction(g, ch, bob), Interaction(g, ch, carol), Interaction(g, ch, alice)
        await view.answer(i1, 1)
        await view.answer(i2, 1)
        await view.answer(i3, 0)
        again = Interaction(g, ch, bob)
        await view.answer(again, 2)
        assert "+35 XP" in i1.response.messages[0][0] and "first" in i1.response.messages[0][0]
        assert "+17 XP" in i2.response.messages[0][0] and i3.response.messages[0][0].startswith("❌")
        assert "already" in again.response.messages[0][0]
        reveal = view.reveal_embed()
        assert "B. Nintendo" in reveal.fields[0].value and "2/3" in reveal.fields[0].value

        # channel cooldown, NSFW topic, failure
        await cog.trivia.callback(cog, Ctx(g, ch, bob), topic="Mario", difficulty="easy")
        assert len(asked) == 1
        nctx = Ctx(g, g.add("other"), bob)
        await cog.trivia.callback(cog, nctx, topic="hentai games", difficulty="easy")
        assert nctx.replies[-1].startswith("🚫") and len(asked) == 1

        # leaderboard
        lctx = Ctx(g, ch, alice)
        await cog.trivia_leaderboard.callback(cog, lctx)
        desc = lctx.kw[-1]["embed"].description.splitlines()
        assert desc[0].startswith("🥇") and f"<@{bob.id}>" in desc[0] and "35 XP" in desc[0]
        assert f"<@{carol.id}>" in desc[1] and "0 XP" in desc[2] and "0/1" in desc[2]
        assert "You: 0 XP, 0/1" in lctx.kw[-1]["embed"].footer.text

    async def broken(*a):
        return None

    with patched(ai, trivia_question=broken, XAI_API_KEY="k"):
        fctx = Ctx(g, g.add("third"), carol)
        await cog.trivia.callback(cog, fctx)
        assert fctx.replies[-1].startswith("😵")


async def _run():
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    bot = types.SimpleNamespace(db=db)
    try:
        await _summarize(db, bot)
        await _trivia(db, bot)
    finally:
        await db.close()


def test_extras():
    asyncio.run(_run())


if __name__ == "__main__":
    test_extras()
    print("tests/test_ai_extras.py: all checks passed")
