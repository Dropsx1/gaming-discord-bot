"""Smarter automod: previous-message context, image checks (vision), warn-only mode, /automod stats from Undo.

No token or xAI key needed (AI is faked).
Run:  python tests/test_automod_ai.py      (or: python -m pytest tests/)
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
DB_FILE = os.path.join(tempfile.mkdtemp(), "automod.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

from aifakes import Attachment, Ctx, Guild, Interaction, Member, Msg, patched  # noqa: E402
from cogs import ai_features as aif  # noqa: E402
from utils import ai  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402


async def _run():
    gc.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    bot = types.SimpleNamespace(db=db)
    cog = aif.AIFeatures(bot)
    text_calls, image_calls = [], []
    verdicts = {}

    async def classify(text, context=None, feature="automod"):
        text_calls.append((text, context))
        return ai.Reply(verdicts.get(text, "ok"), 5)

    async def classify_image(url, caption=""):
        image_calls.append(url)
        return ai.Reply("nsfw" if "bad" in url else "ok", 50)

    try:
        with patched(ai, classify=classify, classify_image=classify_image, XAI_API_KEY="k"):
            g = Guild()
            general, modlog = g.add("general"), g.add("mod-log", public=False)
            await gc.set_many(g, {"AUTOMOD_ENABLED": 1, "MOD_LOG_CHANNEL_ID": modlog.id})
            user, friend = Member(g, name="Sam"), Member(g, name="Alex")

            # 1. Context: the previous messages go to the AI with the flagged one.
            general.history_msgs = [Msg(g, general, friend, "1v1 me noob"), Msg(g, general, user, "ok bet")]
            msg = Msg(g, general, user, "you idiot, I'm winning this")
            general.history_msgs.append(msg)
            await cog.on_message(msg)
            assert text_calls[-1] == ("you idiot, I'm winning this", ["Alex: 1v1 me noob", "Sam: ok bet"])
            assert not msg.deleted  # AI said ok (banter in context)

            # 2. Warn-only mode: flagged but not deleted; DM warns; log says so; Undo = false positive.
            await gc.set_many(g, {"AUTOMOD_MODE": 1})
            spam = "FREE NITRO here"
            verdicts[spam] = "spam"
            cog.automod_cd.reset(g.id, user.id)
            m2 = Msg(g, general, user, spam)
            await cog.on_message(m2)
            assert not m2.deleted and "Heads up" in user.sent[-1]
            log = modlog.sent[-1].embed
            assert "(warn only)" in log.title and "warn-only" in log.footer.text
            flag = await db.get_flag(1)
            assert flag["mode"] == "warn" and flag["kind"] == "text"
            staff = Member(g, staff=True)
            inter = Interaction(g, general, staff, client=bot)
            undo = aif._FlagButton("undo", 1)
            await undo.callback(inter)
            assert "false positive" in inter.response.messages[0][0] and not general.sent  # nothing re-posted

            # 3. Delete mode (default) still deletes.
            await gc.set_many(g, {"AUTOMOD_MODE": 0})
            cog.automod_cd.reset(g.id, user.id)
            m3 = Msg(g, general, user, spam)
            await cog.on_message(m3)
            assert m3.deleted and (await db.get_flag(2))["mode"] == "delete"

            # 4. Images: off by default; on -> vision check; PNG/JPEG only; cooldown; image-only messages too.
            pic = Msg(g, general, user, "", attachments=[Attachment("https://cdn.example/bad.png")])
            await cog.on_message(pic)
            assert image_calls == [] and not pic.deleted
            await gc.set_many(g, {"AUTOMOD_IMAGES": 1})
            await cog.on_message(pic)
            assert image_calls == ["https://cdn.example/bad.png"] and pic.deleted
            flag3 = await db.get_flag(3)
            assert flag3["kind"] == "image" and flag3["reason"] == "image" and "bad.png" in flag3["content"]
            again = Msg(g, general, user, "", attachments=[Attachment("https://cdn.example/bad2.png")])
            await cog.on_message(again)
            assert len(image_calls) == 1  # per-user image cooldown
            other = Member(g)
            gif = Msg(g, general, other, "", attachments=[Attachment("https://cdn.example/x.gif", "image/gif")])
            await cog.on_message(gif)
            assert len(image_calls) == 1  # not a format the vision model takes
            fine = Msg(g, general, other, "look at my base",
                       attachments=[Attachment("https://cdn.example/base.jpg", "image/jpeg")])
            await cog.on_message(fine)
            assert image_calls[-1].endswith("base.jpg") and not fine.deleted
            assert await db.count_ai(g.id, "automod-image", 0) == 2

            # 5. /automod stats from Undo clicks.
            await db.mark_flag_undone(3)
            ctx = Ctx(g, general, staff)
            await aif.AIFeatures.automod_stats.callback(cog, ctx, days=30)
            embed = ctx.kw[-1]["embed"]
            assert "**3** flag(s), **2** undone" in embed.description and "67%" in embed.description
            reasons = embed.fields[0].value
            assert "**scam**: 2 flag(s), 1 undone" in reasons and "**image (image)**: 1 flag(s), 1 undone" in reasons
            assert "**spam**" in embed.fields[1].value

            # mode / images commands save settings
            ctx = Ctx(g, general, staff)
            await aif.AIFeatures.automod_mode.callback(cog, ctx, mode="warn")
            assert gc.saved(g)["AUTOMOD_MODE"] == 1 and "warn" in ctx.replies[-1]
            await aif.AIFeatures.automod_images.callback(cog, ctx, enabled="off")
            assert gc.saved(g)["AUTOMOD_IMAGES"] == 0
            await aif.AIFeatures.automod.callback(cog, ctx)
            assert "mode **warn**" in ctx.replies[-1] and "image checks **off**" in ctx.replies[-1]
    finally:
        await db.close()


def test_automod_upgrades():
    asyncio.run(_run())


if __name__ == "__main__":
    test_automod_upgrades()
    print("tests/test_automod_ai.py: all checks passed")
