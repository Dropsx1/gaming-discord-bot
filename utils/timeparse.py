"""Parse event times typed by staff, in the server's timezone (config.TIMEZONE).

Accepted examples:
    8pm | 20:00 | tonight 9pm | tomorrow 7:30pm | sat 8pm | next friday 6pm
    10/31 8pm | 10/31/2026 20:00 | 2026-10-31 20:00 | in 2h | in 1d3h
    a Discord timestamp like <t:1792360800:F>, or a raw Unix timestamp
"""

import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import config
from utils.helpers import parse_duration

TZ = ZoneInfo(config.TIMEZONE)
WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b|\b(noon|midnight)\b")
_NOISE_RE = re.compile(r"\b(at|on|the|est|edt|et|eastern)\b|[,@]")


def _parse_clock(text: str) -> tuple[Optional[time], str]:
    """Find a clock time in text. Returns (time or None, text with the time removed)."""
    m = _TIME_RE.search(text)
    if not m:
        return None, text
    rest = (text[: m.start()] + " " + text[m.end():]).strip()
    if m.group(6):
        return (time(12) if m.group(6) == "noon" else time(0)), rest
    if m.group(3):
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if not 1 <= hour <= 12:
            return None, text
        hour = hour % 12 + (12 if m.group(3) == "pm" else 0)
    else:
        hour, minute = int(m.group(4)), int(m.group(5))
    if hour > 23 or minute > 59:
        return None, text
    return time(hour, minute), rest


def _parse_day(text: str, today: date) -> Optional[date]:
    text = text.strip()
    if text in ("", "today", "tonight"):
        return today
    if text in ("tomorrow", "tmrw", "tmr"):
        return today + timedelta(days=1)
    words = text.split()
    if words and words[-1] in WEEKDAYS and len(words) <= 2 and (len(words) == 1 or words[0] in ("next", "this")):
        ahead = (WEEKDAYS[words[-1]] - today.weekday()) % 7
        if words[0] == "next" and ahead == 0:
            ahead = 7
        return today + timedelta(days=ahead)
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", text)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        d = date(year, int(m.group(1)), int(m.group(2)))
        if not m.group(3) and d < today:
            d = date(year + 1, d.month, d.day)  # "1/5" in December means next January
        return d
    return None


def parse_when(text: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Return an aware datetime, or None if the text isn't understood."""
    now = now or datetime.now(TZ)
    raw = text.strip()
    m = re.fullmatch(r"<t:(\d{9,11})(?::[a-zA-Z])?>|(\d{9,11})", raw)
    if m:
        return datetime.fromtimestamp(int(m.group(1) or m.group(2)), TZ)

    text = raw.lower()
    if text.startswith("in "):
        delta = parse_duration(text[3:])
        return now + delta if delta else None

    clock, rest = _parse_clock(text)
    rest = re.sub(r"\s+", " ", _NOISE_RE.sub(" ", rest)).strip()
    try:
        day = _parse_day(rest, now.date())
    except ValueError:  # e.g. 2/30
        return None
    if day is None:
        return None
    if clock is None:
        return None  # a day without a time is too vague for a reminder
    when = datetime.combine(day, clock, tzinfo=TZ)
    if when <= now and rest in ("", "today", "tonight"):
        when += timedelta(days=1)  # "8pm" after 8pm means tomorrow
    elif when <= now and rest in WEEKDAYS:
        when += timedelta(days=7)  # "sat 8pm" on Saturday at 9pm means next Saturday
    return when
