"""AI hook for /idea and /prompt.

Right now this returns None, so the Fun cog falls back to its built-in lists.

# ============================================================================
# TO ADD GROK / xAI LATER
# ----------------------------------------------------------------------------
# 1. Get an API key at https://console.x.ai and set XAI_API_KEY in Railway.
# 2. Optionally set XAI_MODEL (check xAI's docs for current model names).
# 3. Replace the body of `generate()` with something like this (aiohttp is
#    already installed as a dependency of discord.py):
#
#    import aiohttp
#
#    async with aiohttp.ClientSession() as session:
#        async with session.post(
#            "https://api.x.ai/v1/chat/completions",
#            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
#            json={
#                "model": XAI_MODEL,
#                "messages": [
#                    {"role": "system", "content": SYSTEM_PROMPT},
#                    {"role": "user", "content": user_prompt},
#                ],
#                "max_tokens": 200,
#            },
#            timeout=aiohttp.ClientTimeout(total=20),
#        ) as resp:
#            resp.raise_for_status()
#            data = await resp.json()
#            return data["choices"][0]["message"]["content"].strip()
#
# 4. Keep the try/except in the caller: on any failure the bot falls back to
#    the static lists, so the command never breaks.
# ============================================================================
"""

import logging
import os
from typing import Optional

log = logging.getLogger("bot.ai")

XAI_API_KEY = os.getenv("XAI_API_KEY", "").strip()
XAI_MODEL = os.getenv("XAI_MODEL", "").strip()

SYSTEM_PROMPT = (
    "You are a friendly assistant in a safe-for-work gaming Discord server. "
    "Reply with one short, fun, SFW idea (1-2 sentences). No slurs, no NSFW, no toxicity."
)


async def generate(kind: str, theme: Optional[str] = None) -> Optional[str]:
    """Return AI-generated text, or None to use the built-in fallback.

    kind: "challenge" (for /idea) or "prompt" (for /prompt).
    """
    if not XAI_API_KEY:
        return None

    user_prompt = (
        f"Give me a {kind} for gamers"
        + (f" themed around: {theme}" if theme else "")
        + "."
    )
    # --- Plug the xAI call in here (see module docstring). -----------------
    log.debug("AI requested (%s) but not implemented yet: %s", kind, user_prompt)
    return None
