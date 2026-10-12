"""Built-in content from data/ (see data/README.md).

- prompts.json       Prompt of the Day items: {id, theme, text}
- remix_themes.json  Remix Friday themes:     {id, title, description}
- rss_feeds.json     Digest feeds:            {category: [url, ...]}

Items are picked at random from the ones not used yet. Once every item has
been used, the rotation resets (without repeating the one just posted).
Edit the JSON files to add or change items; keep each `id` unique and never
reuse an old id for different text.
"""

import json
import logging
import random
from pathlib import Path
from typing import Optional

log = logging.getLogger("bot.content")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

PROMPTS = "prompts"
REMIX = "remix"
_FILES = {PROMPTS: ("prompts.json", ("id", "theme", "text")), REMIX: ("remix_themes.json", ("id", "title", "description"))}


def load_items(kind: str) -> list[dict]:
    """Load and validate a content file. Invalid entries are skipped with a warning."""
    filename, required = _FILES[kind]
    path = DATA_DIR / filename
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. If you mounted a Railway volume at /app/data it hides the bundled "
            "content; mount the volume at /data instead."
        )
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    items, seen = [], set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict) or any(not entry.get(k) for k in required):
            log.warning("%s: skipping invalid entry %r", filename, entry)
            continue
        if not isinstance(entry["id"], int) or entry["id"] in seen:
            log.warning("%s: skipping entry with bad or duplicate id %r", filename, entry.get("id"))
            continue
        seen.add(entry["id"])
        items.append(entry)
    if not items:
        raise ValueError(f"{path} has no valid items")
    return items


def load_feeds() -> dict[str, list[str]]:
    with (DATA_DIR / "rss_feeds.json").open(encoding="utf-8") as f:
        raw = json.load(f)
    return {cat: [u for u in urls if isinstance(u, str) and u.startswith("http")] for cat, urls in raw.items()}


async def pick_unused(db, kind: str) -> dict:
    """Pick a random unused item. Call db.mark_used(kind, item['id']) after posting it."""
    items = load_items(kind)
    used = await db.used_ids(kind)
    available = [i for i in items if i["id"] not in used]
    if not available:
        last: Optional[int] = await db.last_used_id(kind)
        await db.reset_usage(kind)
        log.info("All %d %s items used; starting a new rotation", len(items), kind)
        available = [i for i in items if i["id"] != last] or items
    item = random.choice(available)
    log.info("Picked %s #%s (%d unused left before this)", kind, item["id"], len(available))
    return item
