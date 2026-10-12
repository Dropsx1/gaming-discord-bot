"""xAI (Grok) text generation.

Set in Railway (or .env):
    XAI_API_KEY   your key from https://console.x.ai
    XAI_MODEL     a model name your key can use (the bot logs the available
                  ones at startup if this is missing or wrong)

`generate()` never raises: on a missing key, timeout, HTTP error or bad
response it returns None, and callers fall back to their built-in lists.
"""

import asyncio
import logging
import os
import re
from typing import Optional

import aiohttp

log = logging.getLogger("bot.ai")

API_BASE = os.getenv("XAI_API_BASE", "https://api.x.ai/v1").rstrip("/")
XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
XAI_MODEL = os.getenv("XAI_MODEL", "").strip()
TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_TOKENS = int(os.getenv("XAI_MAX_TOKENS", "300") or 300)

DEFAULT_SYSTEM = (
    "You write for a friendly, safe-for-work gaming Discord server. "
    "Keep replies short, upbeat and inclusive. No NSFW content, slurs, harassment, politics, "
    "or real-money gambling. Never use @everyone or @here. Plain text only: no preamble, "
    "no quotation marks around the answer."
)

_session: Optional[aiohttp.ClientSession] = None
_limit = asyncio.Semaphore(3)  # cap concurrent API calls
_MASS_PING = re.compile(r"@(everyone|here)", re.IGNORECASE)


def enabled() -> bool:
    return bool(XAI_API_KEY and XAI_MODEL)


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


async def generate(prompt: str, system: Optional[str] = None) -> Optional[str]:
    """Return the model's reply to `prompt`, or None on any failure."""
    if not enabled():
        return None
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": system or DEFAULT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.9,
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
        return _clean(text)
    except asyncio.TimeoutError:
        log.warning("xAI request timed out after %ss", TIMEOUT.total)
    except (aiohttp.ClientError, KeyError, IndexError, TypeError, ValueError) as e:
        log.warning("xAI request failed: %s: %s", type(e).__name__, e)
    except Exception:
        log.exception("Unexpected error calling xAI")
    return None


async def check_setup() -> None:
    """Log whether AI is configured, and which models the key can use."""
    if not XAI_API_KEY:
        log.info("AI disabled (XAI_API_KEY not set); /idea and /prompt use built-in lists")
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

    if not XAI_MODEL:
        log.warning("AI disabled: set XAI_MODEL to one of: %s", ", ".join(models) or "(none listed)")
    elif models and XAI_MODEL not in models:
        log.warning("XAI_MODEL=%r is not in the model list for your key (fine if it is an alias). Listed: %s", XAI_MODEL, ", ".join(models))
    else:
        log.info("AI enabled with model %s", XAI_MODEL)
