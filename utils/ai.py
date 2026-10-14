"""xAI (Grok) text generation.

Set in Railway (or .env):
    XAI_API_KEY   your key from https://console.x.ai
    XAI_MODEL     a model name your key can use (the bot logs the available
                  ones at startup if this is missing or wrong)

Optional, for /ask, /imagine and AI auto-moderation (cogs/ai_features.py):
    XAI_FAST_MODEL    cheap text model (default grok-4-1-fast-non-reasoning)
    XAI_IMAGE_MODEL   image model (default grok-imagine-image)
These only need XAI_API_KEY.

Nothing here raises: on a missing key, timeout, HTTP error or bad response the
functions return None, and callers fall back or show a friendly message.
"""

import asyncio
import logging
import os
import base64
import re
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger("bot.ai")

API_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
XAI_MODEL = os.getenv("XAI_MODEL", "").strip()
XAI_FAST_MODEL = os.getenv("XAI_FAST_MODEL", "").strip() or "grok-4-1-fast-non-reasoning"
XAI_IMAGE_MODEL = os.getenv("XAI_IMAGE_MODEL", "").strip() or "grok-imagine-image"
TIMEOUT = aiohttp.ClientTimeout(total=20)
IMAGE_TIMEOUT = aiohttp.ClientTimeout(total=60)
MAX_TOKENS = int(os.getenv("XAI_MAX_TOKENS", "300") or 300)
CHAT_MAX_TOKENS = 400  # /ask replies

VERDICTS = ("ok", "toxic", "nsfw", "spam")
MODERATION_SYSTEM = (
    "You are a content moderator for a friendly, safe-for-work gaming Discord server. "
    "Classify the user's message with exactly one word: ok, toxic, nsfw or spam. "
    "toxic = harassment, slurs, hate, threats or telling someone to self-harm. "
    "nsfw = sexual or graphic content. spam = scams, fake giveaways/free nitro, mass advertising or flooding. "
    "Banter, mild swearing and gaming trash talk are ok. Reply with the single word only."
)

DEFAULT_SYSTEM = (
    "You write for a friendly, safe-for-work gaming Discord server. "
    "Keep replies short, upbeat and inclusive. No NSFW content, slurs, harassment, politics, "
    "or real-money gambling. Never use @everyone or @here. Plain text only: no preamble, "
    "no quotation marks around the answer."
)

_session: Optional[aiohttp.ClientSession] = None
_limit = asyncio.Semaphore(3)  # cap concurrent API calls
_MASS_PING = re.compile(r"@(everyone|here)", re.IGNORECASE)


@dataclass
class Reply:
    text: str
    tokens: int  # prompt + completion tokens, as billed


def enabled() -> bool:
    """/idea and /prompt: need a key and XAI_MODEL."""
    return bool(XAI_API_KEY and XAI_MODEL)


def key_set() -> bool:
    """/ask, /imagine and AI moderation: only need the key."""
    return bool(XAI_API_KEY)


def _headers() -> dict:
    return {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True)
    return _session


async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _clean(text: str) -> str:
    text = text.strip().strip('"“”').strip()
    return _MASS_PING.sub(r"@​\1", text)  # defang mass pings


async def _chat(model: str, prompt: str, system: str, max_tokens: int, temperature: float) -> Optional[Reply]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        async with _limit:
            session = await _get_session()
            async with session.post(f"{API_BASE}/chat/completions", json=payload, headers=_headers()) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    log.warning("xAI request failed: HTTP %s %s", resp.status, body)
                    return None
                data = await resp.json()
        text = data["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            log.warning("xAI returned an empty reply")
            return None
        tokens = int((data.get("usage") or {}).get("total_tokens") or 0)
        return Reply(_clean(text), tokens)
    except asyncio.TimeoutError:
        log.warning("xAI request timed out after %ss", TIMEOUT.total)
    except (aiohttp.ClientError, KeyError, IndexError, TypeError, ValueError) as e:
        log.warning("xAI request failed: %s: %s", type(e).__name__, e)
    except Exception:
        log.exception("Unexpected error calling xAI")
    return None


async def generate(prompt: str, system: Optional[str] = None) -> Optional[str]:
    """Return the model's reply to `prompt`, or None on any failure."""
    if not enabled():
        return None
    reply = await _chat(XAI_MODEL, prompt, system or DEFAULT_SYSTEM, MAX_TOKENS, 0.9)
    return reply.text if reply else None


async def chat(prompt: str, system: str, max_tokens: int = CHAT_MAX_TOKENS) -> Optional[Reply]:
    """Cheap-model reply for /ask."""
    if not key_set():
        return None
    return await _chat(XAI_FAST_MODEL, prompt, system, max_tokens, 0.7)


async def classify(text: str) -> Optional[Reply]:
    """Moderation verdict: Reply.text is one of VERDICTS, or None if the call failed."""
    if not key_set():
        return None
    reply = await _chat(XAI_FAST_MODEL, text[:1500], MODERATION_SYSTEM, 5, 0.0)
    if reply is None:
        return None
    word = re.sub(r"[^a-z]", " ", reply.text.lower()).split()
    verdict = next((w for w in word if w in VERDICTS), None)
    if verdict is None:
        log.warning("Unexpected moderation verdict %r", reply.text[:50])
        return None
    return Reply(verdict, reply.tokens)


async def image(prompt: str) -> Optional[bytes]:
    """Generate one image; returns PNG/JPEG bytes, or None on any failure."""
    if not key_set():
        return None
    payload = {"model": XAI_IMAGE_MODEL, "prompt": prompt, "n": 1, "response_format": "b64_json"}
    try:
        async with _limit:
            session = await _get_session()
            async with session.post(
                f"{API_BASE}/images/generations", json=payload, headers=_headers(), timeout=IMAGE_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    log.warning("xAI image request failed: HTTP %s %s", resp.status, body)
                    return None
                data = await resp.json()
            item = data["data"][0]
            if item.get("b64_json"):
                return base64.b64decode(item["b64_json"])
            if item.get("url"):  # some models only return a (temporary) URL
                async with session.get(item["url"], timeout=IMAGE_TIMEOUT) as img:
                    if img.status == 200:
                        return await img.read()
            log.warning("xAI image response had no image data")
    except asyncio.TimeoutError:
        log.warning("xAI image request timed out after %ss", IMAGE_TIMEOUT.total)
    except (aiohttp.ClientError, KeyError, IndexError, TypeError, ValueError) as e:
        log.warning("xAI image request failed: %s: %s", type(e).__name__, e)
    except Exception:
        log.exception("Unexpected error calling xAI images")
    return None


async def check_setup() -> None:
    """Log whether AI is configured, and which models the key can use."""
    if not XAI_API_KEY:
        log.info("AI disabled (XAI_API_KEY not set): /idea and /prompt use built-in lists; /ask, /imagine and AI automod are off")
        return
    try:
        session = await _get_session()
        async with session.get(f"{API_BASE}/models", headers=_headers()) as resp:
            if resp.status == 401:
                log.error("XAI_API_KEY was rejected by xAI (HTTP 401). Check the key.")
                return
            if resp.status != 200:
                log.warning("Could not list xAI models: HTTP %s", resp.status)
                return
            data = await resp.json()
        models = sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))
    except Exception as e:
        log.warning("Could not reach xAI to check setup: %s", e)
        return

    if models and XAI_FAST_MODEL not in models:
        log.warning("XAI_FAST_MODEL=%r (for /ask and auto-moderation) is not in your key's model list. Listed: %s",
                    XAI_FAST_MODEL, ", ".join(models))
    if not XAI_MODEL:
        log.warning("/idea and /prompt use built-in lists: set XAI_MODEL to one of: %s", ", ".join(models) or "(none listed)")
    elif models and XAI_MODEL not in models:
        log.warning("XAI_MODEL=%r is not in the model list for your key (fine if it is an alias). Listed: %s", XAI_MODEL, ", ".join(models))
    else:
        log.info("AI enabled with model %s", XAI_MODEL)
