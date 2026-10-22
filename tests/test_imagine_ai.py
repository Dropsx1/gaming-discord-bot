"""/imagine upgrades: style presets, aspect ratio, AI prompt enhancement (after the safety checks),
Variation / Remix buttons, limits unchanged. AI is faked; no token or key needed.

Run:  python tests/test_imagine_ai.py      (or: python -m pytest tests/)
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
DB_FILE = os.path.join(tempfile.mkdtemp(), "imagine.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

from aifakes import Ctx, Guild, Interaction, Member, patched  # noqa: E402
from cogs import ai_features as aif  # noqa: E402
from utils import ai  # noqa: E402
from utils import guildconfig as gc  # noqa: E402
from utils.db import Database  # noqa: E402


async def _payloads():
    """Only documented grok-imagine-image parameters are sent (model, prompt, n, response_format, aspect_ratio)."""
    sent = []

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"data": [{"b64_json": "iVBORw=="}]}

    class Session:
        closed = False

        def post(self, url, json=None, **kw):
            sent.append((url, json))
            return Resp()

    async def get_session():
        return Session()

    with patched(ai, _get_session=get_session, XAI_API_KEY="k"):
        assert await ai.image("a cat", "16:9")
        assert await ai.image("a cat", "auto")
        assert await ai.image("a cat", "7:3")  # not a supported ratio: omitted
    assert sent[0][0].endswith("/images/generations")
    assert sent[0][1] == {"model": ai.XAI_IMAGE_MODEL, "prompt": "a cat", "n": 1, "response_format": "b64_json",
                          "aspect_ratio": "16:9"}
    assert "aspect_ratio" not in sent[1][1] and "aspect_ratio" not in sent[2][1]
    assert set(sent[0][1]) <= {"model", "prompt", "n", "response_format", "aspect_ratio"}


async def _run():
    gc.config.GUILD_ID = 0
    db = Database(DB_FILE)
    await db.connect()
    await gc.init(db)
    bot = types.SimpleNamespace(db=db)
    cog = aif.AIFeatures(bot)
    calls = []

    async def classify(text, context=None, feature="automod"):
        calls.append(("classify", text))
        return ai.Reply("nsfw" if "sneaky" in text else "ok", 3)

    async def enhance(prompt, style_hint=""):
        calls.append(("enhance", prompt, style_hint))
        if "trap" in prompt:
            return ai.Reply("a nude figure in a trap", 9)  # enhancement must never sneak NSFW in
        return ai.Reply(f"DETAILED: {prompt} ({style_hint or 'no style'})", 9)

    async def image(prompt, aspect_ratio=None):
        calls.append(("image", prompt, aspect_ratio))
        return b"\x89PNG fake"

    try:
        await _payloads()
        with patched(ai, classify=classify, enhance_prompt=enhance, image=image, XAI_API_KEY="k"):
            g = Guild()
            art = g.add("ai-art")
            user = Member(g, name="Artist")

            # 1. Safety first: the NSFW keyword filter runs before any AI call (incl. enhancement).
            ctx = Ctx(g, art, user)
            await cog.imagine.callback(cog, ctx, prompt="naked elf", style="anime")
            assert ctx.replies[-1] == aif.BLOCKED and not calls
            await cog.imagine.callback(cog, ctx, prompt="a sneaky thing")
            assert ctx.replies[-1] == aif.BLOCKED and [c[0] for c in calls] == ["classify"]  # no enhance/image

            # 2. Short prompt + style + aspect: checked, enhanced with the style, then generated.
            calls.clear()
            cog.imagine_cd.reset(g.id, user.id)
            ctx = Ctx(g, art, user)
            await cog.imagine.callback(cog, ctx, prompt="a fox knight", style="pixel art", aspect_ratio="16:9")
            assert calls[0] == ("classify", "Image generation prompt: a fox knight")
            assert calls[1] == ("enhance", "a fox knight", aif.STYLES["pixel art"])
            assert calls[2] == ("image", f"DETAILED: a fox knight ({aif.STYLES['pixel art']})", "16:9")
            embed, view = ctx.kw[-1]["embed"], ctx.kw[-1]["view"]
            assert embed.description == "a fox knight" and embed.fields[0].name == "✨ Enhanced prompt"
            assert "Pixel art" in embed.footer.text and "16:9" in embed.footer.text
            assert isinstance(view, aif.ImagineView) and [c.label for c in view.children] == ["Variation", "Remix"]
            assert await db.count_ai(g.id, "imagine-enhance", 0) == 1

            # 3. Enhancement off, or a long prompt: used as written (+ style words).
            calls.clear()
            cog.imagine_cd.reset(g.id, user.id)
            await cog.imagine.callback(cog, Ctx(g, art, user), prompt="a castle", style="cinematic", enhance=False)
            assert [c[0] for c in calls] == ["classify", "image"] and calls[-1][1] == f"a castle, {aif.STYLES['cinematic']}"
            calls.clear()
            cog.imagine_cd.reset(g.id, user.id)
            long_prompt = " ".join(["word"] * 30)
            await cog.imagine.callback(cog, Ctx(g, art, user), prompt=long_prompt)
            assert [c[0] for c in calls] == ["classify", "image"] and calls[-1][1] == long_prompt and calls[-1][2] == "auto"

            # 4. An NSFW enhancement is thrown away: the member's checked prompt is used instead.
            calls.clear()
            cog.imagine_cd.reset(g.id, user.id)
            await cog.imagine.callback(cog, Ctx(g, art, user), prompt="a treasure trap")
            assert calls[-1] == ("image", "a treasure trap", "auto")

            # 5. Variation: same final prompt, no new checks, but the cooldown/limits still apply.
            calls.clear()
            inter = Interaction(g, art, user)
            await view.variation.callback(inter)
            assert inter.response.messages[0][0].startswith("⏳") and not calls  # still on cooldown
            cog.imagine_cd.reset(g.id, user.id)
            inter = Interaction(g, art, user)
            await view.variation.callback(inter)
            assert calls == [("image", f"DETAILED: a fox knight ({aif.STYLES['pixel art']})", "16:9")]
            assert inter.followup.messages[-1][1]["embed"].fields[0].name == "✨ Enhanced prompt"

            await db.execute("DELETE FROM ai_usage WHERE feature = 'imagine'")  # fresh daily budget for the rest
            # 6. Remix: a modal with the prompt; the edited prompt goes through every check again.
            inter = Interaction(g, art, user)
            await view.remix.callback(inter)
            modal = inter.response.modal
            assert isinstance(modal, aif.RemixModal) and modal.prompt.default == "a fox knight"
            calls.clear()
            cog.imagine_cd.reset(g.id, user.id)
            modal.prompt._value = "naked fox knight"
            sub = Interaction(g, art, user)
            await modal.on_submit(sub)
            assert sub.response.messages[-1][0] == aif.BLOCKED and not calls
            modal.prompt._value = "a fox wizard"
            sub = Interaction(g, art, user)
            await modal.on_submit(sub)
            assert [c[0] for c in calls] == ["classify", "enhance", "image"] and calls[-1][2] == "16:9"

            # 7. Daily limit still enforced (Variation counts as an image).
            cog.imagine_cd.reset(g.id, user.id)
            for _ in range(aif.IMAGINE_DAILY_LIMIT):
                await db.log_ai(g.id, user.id, "imagine", images=1)
            inter = Interaction(g, art, user)
            await view.variation.callback(inter)
            assert inter.response.messages[-1][0].startswith("📅")
    finally:
        await db.close()


def test_imagine_upgrades():
    asyncio.run(_run())


if __name__ == "__main__":
    test_imagine_upgrades()
    print("tests/test_imagine_ai.py: all checks passed")
