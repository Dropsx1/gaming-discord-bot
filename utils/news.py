"""Live AI news: turning RSS entries into articles (no Discord or network code here).

Every article is identified by its canonical URL (tracking parameters and #fragments
removed), so the same story is stored and posted once even if a feed re-lists it.
"""

import calendar
import html
import json
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING = re.compile(r"^(utm_|mc_|fbclid$|gclid$|ref$|ref_src$|cmpid$|guccounter$)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_IMG = re.compile(r"<img[^>]+src=[\"'](https?://[^\"']+)[\"']", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“‘(])")
_SCORE = re.compile(r"\b(10|[1-9])\b")

SUMMARY_LIMIT = 300


@dataclass
class Article:
    url: str
    title: str
    source: str
    summary: str          # plain text from the feed (replaced by the AI summary when there is one)
    image: Optional[str]
    published_at: int     # Unix time; 0 if the feed gave no date
    feed: str             # the feed URL it came from
    score: Optional[int] = None
    dup_of: Optional[str] = None  # URL of the same story from another outlet (merged: never posted itself)


_STOP = set("""the a an and or of to in on for with at by from as is are was were be been it its this that these those
new now how why what who will can has have had into over after about more than just your you our their his her not
says said say report reports""".split())
_WORD = re.compile(r"[a-z0-9][a-z0-9.+-]*")


def title_tokens(title: str) -> set[str]:
    words = _WORD.findall(plain_text(title).lower().replace("’", "'"))
    return {w.strip(".-+") for w in words if len(w.strip(".-+")) >= 3 and w not in _STOP}


def same_story(a: str, b: str) -> bool:
    """Two headlines about the same story (cross-outlet duplicates)."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return False
    shared = len(ta & tb)
    if shared < 3:
        return False
    return shared / len(ta | tb) >= 0.5 or shared / min(len(ta), len(tb)) >= 0.8


def merge_duplicates(new: list, recent: list) -> int:
    """Mark new articles that repeat a story from a *different* outlet (already stored in `recent` rows with
    url/title/source, or earlier in `new`) with dup_of = the first one's URL. Returns how many were merged."""
    keepers: list[tuple[str, str, str]] = [(r["url"], r["title"], r["source"]) for r in recent]
    merged = 0
    for a in sorted(new, key=lambda x: (x.published_at or 0, x.url)):
        match = next((url for url, title, source in keepers
                      if source.casefold() != a.source.casefold() and same_story(a.title, title)), None)
        if match:
            a.dup_of = match
            merged += 1
        else:
            keepers.append((a.url, a.title, a.source))
    return merged


def canonical_url(url: str) -> str:
    """Lowercase scheme/host, drop tracking params and the #fragment, so one story = one URL."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def plain_text(raw: str) -> str:
    text = html.unescape(_TAG.sub(" ", raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r" ([.,!?;:])", r"\1", text)  # "<b>world</b>." -> "world." not "world ."


def first_sentences(text: str, count: int = 2, limit: int = SUMMARY_LIMIT) -> str:
    """The first one or two sentences, trimmed to `limit` characters."""
    text = plain_text(text)
    if not text:
        return ""
    summary = " ".join(_SENTENCE.split(text)[:count])
    if len(summary) > limit:
        summary = summary[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return summary


def _http(url) -> Optional[str]:
    return url if isinstance(url, str) and url.startswith(("http://", "https://")) else None


def find_image(entry) -> Optional[str]:
    """media:content / media:thumbnail / image enclosure / first <img> in the HTML."""
    for media in (entry.get("media_content") or []) + (entry.get("media_thumbnail") or []):
        url = _http(media.get("url"))
        medium = (media.get("medium") or media.get("type") or "image").lower()
        if url and ("image" in medium or not media.get("type")):
            return url
    for enc in entry.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image/") and _http(enc.get("href") or enc.get("url")):
            return enc.get("href") or enc.get("url")
    blobs = [entry.get("summary") or ""] + [c.get("value") or "" for c in entry.get("content") or []]
    for blob in blobs:
        match = _IMG.search(blob)
        if match:
            return html.unescape(match.group(1))
    return None


def _stamp(entry) -> int:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    try:
        return int(calendar.timegm(t)) if t else 0  # feedparser's struct_time is UTC
    except (TypeError, ValueError, OverflowError):
        return 0


def parse_entries(parsed, feed_url: str, name: Optional[str] = None, limit: int = 30) -> list[Article]:
    """Articles from a feedparser result, newest first (at most `limit`)."""
    source = name or plain_text(parsed.feed.get("title") or "") or urlsplit(feed_url).netloc
    source = source[:60]
    out, seen = [], set()
    for entry in parsed.entries:
        title = plain_text(entry.get("title") or "")
        link = _http(entry.get("link") or "")
        if not title or not link:
            continue
        url = canonical_url(link)
        if url in seen:
            continue
        seen.add(url)
        body = entry.get("summary") or next((c.get("value") for c in entry.get("content") or []), "")
        summary = first_sentences(body)
        if summary.rstrip(".…").casefold() == title.rstrip(".").casefold():
            summary = ""  # some feeds repeat the title as the description
        out.append(Article(
            url=url, title=title[:256], source=source, summary=summary,
            image=find_image(entry), published_at=_stamp(entry), feed=feed_url,
        ))
    out.sort(key=lambda a: a.published_at, reverse=True)
    return out[:limit]


def parse_review(text: str) -> tuple[Optional[str], Optional[int]]:
    """Read the AI's {"summary": ..., "score": n} reply. Tolerates code fences and stray text."""
    if not text:
        return None, None
    summary = score = None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            summary = data.get("summary")
            score = data.get("score")
        except (ValueError, AttributeError):
            pass
    if score is None:
        found = re.search(r"score\D{0,5}(10|[1-9])", text, re.IGNORECASE) or _SCORE.search(text)
        score = found.group(1) if found else None
    try:
        score = max(1, min(10, int(float(score)))) if score is not None else None
    except (TypeError, ValueError):
        score = None
    if isinstance(summary, str):
        summary = first_sentences(summary, count=2, limit=SUMMARY_LIMIT) or None
    else:
        summary = None
    return summary, score
