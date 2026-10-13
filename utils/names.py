"""Name normalization for matching hand-made channels, categories and roles.

Servers decorate names with emoji and divider symbols, e.g. "📅┃prompt-of-the-day",
"「🎨」self-roles", "・ʀᴜʟᴇs", "👑 Prompt King". normalize_name() reduces all of
these to a plain slug so they match what the bot looks for:

    normalize_name("📅┃prompt-of-the-day")  -> "prompt-of-the-day"
    normalize_name("AI & Create Lab")         -> "ai-create-lab"
    normalize_name("➕ Join to Create")       -> "join-to-create"
"""

import unicodedata

# Unicode "small caps" letters that decorated names often use (ʀᴜʟᴇs -> rules).
_SMALL_CAPS = str.maketrans({
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j",
    "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ǫ": "q", "ʀ": "r", "ꜱ": "s", "ᴛ": "t",
    "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
})


def normalize_name(name: str) -> str:
    """Lowercase, drop emoji/symbols/dividers, and join the remaining words with single dashes."""
    text = unicodedata.normalize("NFKC", name or "").translate(_SMALL_CAPS).casefold()
    out, dash = [], False
    for ch in text:
        # Letters (any script) and plain digits only: dividers like 〢 count as "numbers" in Unicode.
        if unicodedata.category(ch)[0] == "L" or unicodedata.category(ch) == "Nd":
            out.append(ch)
            dash = False
        elif not dash and out:
            out.append("-")  # any run of spaces, dashes, emoji or symbols becomes one dash
            dash = True
    return "".join(out).strip("-")
