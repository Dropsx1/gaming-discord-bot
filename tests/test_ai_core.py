"""utils/ai.py groundwork: per-feature models, cost caps, JSON mode, multi-message chat, retries.

No network or Gemini key needed (the HTTP call is faked).
Run:  python tests/test_ai_core.py      (or: python -m pytest tests/)
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
DB_FILE = os.path.join(tempfile.mkdtemp(), "core.db")
os.environ.update(DB_PATH=DB_FILE, GUILD_ID="")

from utils import ai  # noqa: E402
from aifakes import patched  # noqa: E402
from utils.db import Database  # noqa: E402


def test_models_and_prices():
    # Defaults (overridable with GEMINI_*_MODEL env vars).
    assert ai.GEMINI_FAST_MODEL == (os.getenv("GEMINI_FAST_MODEL") or "gemini-3.5-flash-lite")
    assert ai.GEMINI_SMART_MODEL == (os.getenv("GEMINI_SMART_MODEL") or "gemini-3.8-flash")
    assert ai.GEMINI_VISION_MODEL == (os.getenv("GEMINI_VISION_MODEL") or ai.GEMINI_SMART_MODEL)
    assert ai.GEMINI_IMAGE_MODEL == (os.getenv("GEMINI_IMAGE_MODEL") or "gemini-3-pro-image")
    assert ai.GEMINI_MODEL == (os.getenv("GEMINI_MODEL") or ai.GEMINI_FAST_MODEL)
    assert ai.GEMINI_API_BASE.startswith("https://generativelanguage.googleapis.com/v1beta/openai")
    with patched(ai, GEMINI_API_KEY="k", XAI_API_KEY="x"):  # Gemini wins when both keys are set
        assert ai.using_gemini() and ai.provider() == "Gemini" and ai._api_base() == ai.GEMINI_API_BASE
        assert ai.model_for("ask") == ai.GEMINI_SMART_MODEL and ai.model_for("summarize") == ai.GEMINI_SMART_MODEL
        for feature in ("automod", "news", "imagine-check", "trivia", "welcome", "idea"):
            assert ai.model_for(feature) == ai.GEMINI_FAST_MODEL, feature
        assert ai.model_for("automod-image") == ai.GEMINI_VISION_MODEL
        assert ai.model_for("imagine") == ai.GEMINI_IMAGE_MODEL
        assert ai.enabled() and ai.key_set()  # /idea works with just the key (default model)
        assert ai._headers()["Authorization"] == "Bearer k"
    with patched(ai, GEMINI_API_KEY="", XAI_API_KEY="x", XAI_MODEL=""):  # legacy xAI rollback path
        assert not ai.using_gemini() and ai.provider() == "xAI" and ai._api_base() == ai.API_BASE
        assert ai.model_for("ask") == ai.XAI_SMART_MODEL and ai.model_for("imagine") == ai.XAI_IMAGE_MODEL
        assert ai.key_set() and not ai.enabled()  # xAI still needs XAI_MODEL for /idea
    with patched(ai, GEMINI_API_KEY="", XAI_API_KEY=""):
        assert not ai.key_set() and not ai.enabled()
    assert abs(ai.token_cost("gemini-3.8-flash", 1_000_000, 1_000_000) - 4.5) < 1e-9
    assert abs(ai.token_cost("gemini-3.5-flash-lite", 1_000_000, 1_000_000) - 2.8) < 1e-9
    assert abs(ai.token_cost("grok-4.7", 1_000_000, 1_000_000) - 8.0) < 1e-9
    assert ai.token_cost("unknown-model", 1_000_000, 1_000_000) == 19.0  # priced high so caps stay safe
    assert ai.image_cost("gemini-3-pro-image") == 0.134
    assert ai.image_cost("gemini-3.1-flash-lite-image") == 0.0336
    assert ai.image_cost("gemini-nano-banana-2.1") == 0.0336
    assert ai.image_cost("unknown-image-model") == 0.134
    assert ai.image_cost("grok-imagine-image") == 0.02
    assert ai.DEFAULT_CAPS["imagine"] == 2.0 and ai.DEFAULT_CAPS["ask"] == 2.0  # caps unchanged


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
    real_post, real_keys = ai._post_chat, (ai.GEMINI_API_KEY, ai.XAI_API_KEY)
    real_inline = ai._inline_image
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
        ai.GEMINI_API_KEY = ai.XAI_API_KEY = ""
        assert await ai.complete([{"role": "user", "content": "hi"}], feature="ask") is None and not sent

        ai.GEMINI_API_KEY = "test-key"
        ai.XAI_API_KEY = "must-not-be-used"
        await ai.set_store(db)
        # multi-message + smart model + spend recorded; no xAI-only reasoning_effort sent to Gemini
        replies.append(ok("Hello @everyone"))
        r = await ai.converse([{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                               {"role": "user", "content": "q2"}], "sys", feature="ask")
        assert r.text == "Hello @\u200beveryone" and r.tokens == 150
        p = sent[-1]
        assert p["model"] == ai.GEMINI_SMART_MODEL and [m["role"] for m in p["messages"]] == ["system", "user", "assistant", "user"]
        assert "reasoning_effort" not in p and p["max_tokens"] == ai.CHAT_MAX_TOKENS
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
        assert sent[-1]["model"] == ai.GEMINI_FAST_MODEL

        # vision: Gemini gets the image inlined as a data: URL (falls back to the plain URL if download fails)
        async def fake_inline(url):
            return "data:image/png;base64,iVBORw=="
        ai._inline_image = fake_inline
        replies.append(ok("nsfw"))
        r = await ai.classify_image("https://cdn.example/x.png")
        part = sent[-1]["messages"][1]["content"][0]
        assert r.text == "nsfw" and part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png")
        assert sent[-1]["model"] == ai.GEMINI_VISION_MODEL

        async def no_inline(url):
            return None
        ai._inline_image = no_inline
        replies.append(ok("ok"))
        await ai.classify_image("https://cdn.example/y.png")
        assert sent[-1]["messages"][1]["content"][0]["image_url"]["url"] == "https://cdn.example/y.png"

        # legacy xAI rollback (no GEMINI_API_KEY): grok smart model gets reasoning_effort
        ai.GEMINI_API_KEY = ""
        replies.append(ok("hi"))
        await ai.converse([{"role": "user", "content": "q"}], "sys", feature="ask")
        assert sent[-1]["model"] == ai.XAI_SMART_MODEL
        assert ("reasoning_effort" in sent[-1]) == bool(ai.XAI_SMART_REASONING)
        ai.GEMINI_API_KEY = "test-key"

        # daily cap: once a feature is over, no call is made
        ai.CAPS["trivia"] = 0.000001
        count = len(sent)
        assert await ai.trivia_question("zelda", "easy") is None and len(sent) == count
        ai.CAPS["trivia"] = ai.DEFAULT_CAPS["trivia"]

        # failures return None
        replies.append((500, "boom"))
        assert await ai.complete([{"role": "user", "content": "x"}], feature="ask") is None
    finally:
        ai._post_chat, (ai.GEMINI_API_KEY, ai.XAI_API_KEY) = real_post, real_keys
        ai._inline_image = real_inline
        ai.spend.store = None
        await db.close()


def test_complete():
    asyncio.run(_run())


def test_check_setup_gemini_logs():
    """check_setup lists Gemini models, warns on 401 for GEMINI_API_KEY, and never logs the key."""
    import logging

    class Resp:
        def __init__(self, status, data=None):
            self.status, self.data = status, data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self.data

    responses = []
    urls = []

    class Session:
        closed = False

        def get(self, url, headers=None, **kw):
            urls.append(url)
            return Resp(*responses.pop(0))

    async def get_session():
        return Session()

    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Grab()
    ai.log.addHandler(handler)
    old_level = ai.log.level
    ai.log.setLevel(logging.INFO)
    secret = "sk-test-SECRET-123"
    try:
        with patched(ai, _get_session=get_session, GEMINI_API_KEY=secret):
            responses.append((401,))
            asyncio.run(ai.check_setup())
            ids = [{"id": f"models/{m}"} for m in (ai.GEMINI_FAST_MODEL, ai.GEMINI_SMART_MODEL, ai.GEMINI_IMAGE_MODEL)]
            responses.append((200, {"data": ids}))
            asyncio.run(ai.check_setup())
    finally:
        ai.log.removeHandler(handler)
        ai.log.setLevel(old_level)
    assert urls[0] == f"{ai.GEMINI_API_BASE}/models"
    assert any("GEMINI_API_KEY was rejected by Gemini" in r for r in records)
    assert any(r.startswith(f"AI enabled with model {ai.GEMINI_MODEL} (Gemini") for r in records)
    assert not any("is not in your key's model list" in r for r in records)
    assert not any(secret in r for r in records)


if __name__ == "__main__":
    test_models_and_prices()
    test_caps_parsing()
    test_parse_json_and_trivia()
    test_complete()
    test_check_setup_gemini_logs()
    print("tests/test_ai_core.py: all checks passed")
