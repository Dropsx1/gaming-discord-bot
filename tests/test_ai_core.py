"""utils/ai.py groundwork: per-feature models, cost caps, JSON mode, multi-message chat, retries.

No network or xAI key needed (the HTTP call is faked).
Run:  python tests/test_ai_core.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_FILE = os.path.join(tempfile.mkdtemp(), "core.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

from utils import ai  # noqa: E402
from utils.db import Database  # noqa: E402


def test_models_and_prices():
    assert ai.XAI_SMART_MODEL == (os.getenv("XAI_SMART_MODEL") or "grok-4.7")
    assert ai.model_for("ask") == ai.XAI_SMART_MODEL and ai.model_for("summarize") == ai.XAI_SMART_MODEL
    for feature in ("automod", "news", "imagine-check", "trivia"):
        assert ai.model_for(feature) == ai.XAI_FAST_MODEL, feature
    assert ai.model_for("automod-image") == ai.XAI_VISION_MODEL
    assert ai.model_for("imagine") == ai.XAI_IMAGE_MODEL
    assert abs(ai.token_cost("grok-4.7", 1_000_000, 1_000_000) - 8.0) < 1e-9
    assert ai.token_cost("unknown-model", 1_000_000, 0) == 2.0  # priced like the most expensive
    assert ai.image_cost("grok-imagine-image") == 0.02


def test_caps_parsing():
    caps = ai._parse_caps("ask=3, imagine=0.5,bad,trivia=x")
    assert caps["ask"] == 3.0 and caps["imagine"] == 0.5 and caps["trivia"] == ai.DEFAULT_CAPS["trivia"]


def test_parse_json_and_trivia():
    assert ai.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai.parse_json("nope") is None
    good = '{"question": "Q?", "options": ["a", "b", "c", "d"], "answer": 2, "explanation": "E."}'
    assert ai.parse_trivia(good)["answer"] == 2
    assert ai.parse_trivia('{"question": "Q", "options": ["a", "a", "b", "c"], "answer": 0}') is None
    assert ai.parse_trivia('{"question": "Q", "options": ["a", "b", "c", "d"], "answer": 7}') is None
    assert ai.parse_trivia('{"question": "Q", "options": ["a", "b"], "answer": 0}') is None


async def _run():
    db = Database(DB_FILE)
    await db.connect()
    real_post, real_key = ai._post_chat, ai.XAI_API_KEY
    sent = []
    replies = []

    async def fake_post(payload, timeout):
        sent.append(dict(payload))
        return replies.pop(0)

    def ok(text, p=100, c=50):
        return 200, {"choices": [{"message": {"content": text}}],
                     "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}}

    ai._post_chat = fake_post
    try:
        ai.XAI_API_KEY = ""
        assert await ai.complete([{"role": "user", "content": "hi"}], feature="ask") is None and not sent

        ai.XAI_API_KEY = "test-key"
        await ai.set_store(db)
        # multi-message + smart model + reasoning effort + spend recorded
        replies.append(ok("Hello @everyone"))
        r = await ai.converse([{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                               {"role": "user", "content": "q2"}], "sys", feature="ask")
        assert r.text == "Hello @\u200beveryone" and r.tokens == 150
        p = sent[-1]
        assert p["model"] == ai.XAI_SMART_MODEL and [m["role"] for m in p["messages"]] == ["system", "user", "assistant", "user"]
        assert ai.spend.spent("ask") > 0
        assert (await db.ai_spend_today(ai.spend.day))["ask"] == ai.spend.spent("ask")  # persisted

        # JSON mode; 400 on optional fields -> one retry without them
        replies[:] = [(400, "unknown field response_format"), ok('{"x": 1}')]
        r = await ai.complete([{"role": "user", "content": "j"}], feature="trivia", json_mode=True)
        assert ai.parse_json(r.text) == {"x": 1}
        assert sent[-2]["response_format"] == {"type": "json_object"} and "response_format" not in sent[-1]

        # classify with context
        replies.append(ok("spam"))
        r = await ai.classify("FREE NITRO", context=["a: hi", "b: yo"])
        assert r.text == "spam" and "Earlier messages" in sent[-1]["messages"][1]["content"]
        assert sent[-1]["model"] == ai.XAI_FAST_MODEL

        # vision
        replies.append(ok("nsfw"))
        r = await ai.classify_image("https://cdn.example/x.png")
        assert r.text == "nsfw" and sent[-1]["messages"][1]["content"][0]["type"] == "image_url"

        # daily cap: once a feature is over, no call is made
        ai.CAPS["trivia"] = 0.000001
        count = len(sent)
        assert await ai.trivia_question("zelda", "easy") is None and len(sent) == count
        ai.CAPS["trivia"] = ai.DEFAULT_CAPS["trivia"]

        # failures return None
        replies.append((500, "boom"))
        assert await ai.complete([{"role": "user", "content": "x"}], feature="ask") is None
    finally:
        ai._post_chat, ai.XAI_API_KEY = real_post, real_key
        ai.spend.store = None
        await db.close()


def test_complete():
    asyncio.run(_run())


if __name__ == "__main__":
    test_models_and_prices()
    test_caps_parsing()
    test_parse_json_and_trivia()
    test_complete()
    print("tests/test_ai_core.py: all checks passed")
