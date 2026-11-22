"""News discovery: Bing News RSS + verified direct publisher feeds.

Parsing is tolerant: a handful of publisher feeds ship malformed XML, so we
fall back to a regex item extractor instead of dropping the whole feed.

Discovery also does two things the raw feeds cannot be trusted to do:

* it clusters near-identical headlines by their *entity signature* (named
  people / shows plus distinctive content words), so twenty outlets covering
  the same eviction do not crowd out unrelated stories; and
* it ranks candidates by a light source-quality score (preferred outlet,
  snippet length as an article-length proxy, thumbnail presence) and keeps the
  best representative of each cluster.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import requests

from .utils import canonical_url, clean_text, normalize_title, sha1, STOPWORDS

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}

# Words that say "this is a news item" but say nothing about *which* event.
# They are removed before clustering so "Bigg Boss 20 episode 78 highlights"
# does not, on its own, make every headline look like the same story.
GENERIC_EVENT_WORDS = frozenset({
    "bigg", "boss", "ep", "eps", "episode", "episodes", "highlights",
    "highlight", "news", "video", "videos", "today", "tonight", "weekend",
    "ka", "vaar", "wkv", "show", "season", "update", "updates", "latest",
    "new", "exclusive", "full", "watch", "live", "read", "more", "here",
    "what", "who", "why", "when", "where", "will", "would", "can", "could",
    "did", "does", "do", "is", "are", "was", "were", "has", "have", "had",
    "the", "and", "for", "from", "with", "into", "over", "after", "before",
    "its", "his", "her", "their", "this", "that", "these", "those", "get",
    "gets", "got", "says", "say", "said", "takes", "take", "via", "out",
})

# Capitalised words that are ordinary headline vocabulary, not people/shows.
NON_NAME_WORDS = frozenset({
    "exits", "exit", "house", "following", "breaks", "heart", "again",
    "reacts", "reaction", "questions", "questioned", "clashes", "slams",
    "teases", "apologised", "apologizes", "feels", "reveals", "reports",
    "expected", "due", "health", "issues", "fans", "abuse", "actor",
    "singer", "team", "challenges", "understanding", "contestants",
    "captaincy", "task", "elimination", "eviction", "evicted", "finale",
    "winner", "lowest", "votes", "strict", "punishments", "pushes",
    "reportedly", "likely", "mid", "week", "top", "stars", "celebrities",
    "gameplay", "extension", "surprises", "cast", "crew", "scores", "rank",
    "ranks", "earnings", "charge", "charges", "afternoon", "morning",
    "backlash", "remarks", "inside", "after", "before", "during", "amid",
    "here's", "know", "stop", "ahead", "takes", "look", "first", "now",
})

_NAMED_RE = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)\b")
_SUFFIX_RE = re.compile(r"(?:ing|ed|es|s)$")


def _child_text(item, *names: str) -> str:
    for child in item:
        if child.tag.split("}")[-1] in names:
            return child.text or ""
    return ""


def _parse_xml(payload: str) -> list[dict]:
    try:
        root = ET.fromstring(payload)
        items = []
        for node in root.findall(".//item"):
            items.append({
                "title": clean_text(node.findtext("title", "")),
                "link": node.findtext("link", ""),
                "description": clean_text(node.findtext("description", "")),
                "pubdate": node.findtext("pubDate", ""),
                "source": clean_text(_child_text(node, "Source", "source")),
                "thumbnail": _child_text(node, "Image", "image", "thumbnail").strip(),
            })
        return items
    except ET.ParseError:
        pass
    # Tolerant fallback
    out = []
    for block in re.findall(r"<item[^>]*>(.*?)</item>", payload, re.I | re.S):
        def grab(tag: str) -> str:
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.I | re.S)
            return clean_text(m.group(1)) if m else ""
        out.append({
            "title": grab("title"),
            "link": html.unescape(grab("link")),
            "description": grab("description"),
            "pubdate": grab("pubDate"),
            "source": grab("News:Source") or grab("Source"),
            "thumbnail": grab("News:Image") or grab("media:thumbnail"),
        })
    return out


def _publisher_url(link: str) -> str:
    parsed = urlparse(link)
    if "bing.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        return qs.get("url", [link])[0]
    return link


def _parse_date(value: str) -> datetime | None:
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception:
        return None


def _stem(word: str) -> str:
    """Cheap suffix stripping so 'exits'/'exit' and 'slams'/'slam' agree."""
    return _SUFFIX_RE.sub("", word) if len(word) > 4 else word


def _event_signature(title: str, cfg: dict) -> set[str]:
    """Entity signature of a headline.

    Named people/shows (from config plus capitalised runs) and distinctive
    content words are kept; generic show/breadcrumb words are dropped. The
    result is compared with an overlap coefficient, not plain Jaccard: a short
    headline about the same two people should still match a long one.
    """
    text = re.sub(r"\bscout\s+op\b", "scoutop", title or "", flags=re.I)
    entities: set[str] = set()

    # Configured anchors are authoritative even when followed by a common word
    # ("Love Gill Reportedly Evicted...").
    topic = cfg.get("topic", {})
    anchors = [topic.get("primary", ""), *topic.get("keywords", [])]
    for anchor in anchors:
        anchor = clean_text(anchor or "").strip()
        if not anchor:
            continue
        if re.search(r"(?<!\w)" + re.escape(anchor) + r"(?!\w)", text, re.I):
            for word in re.findall(r"[a-z0-9]+", anchor.lower()):
                if word not in GENERIC_EVENT_WORDS and len(word) > 2:
                    entities.add(_stem(word))

    # Capitalised runs: keep only the leading run before ordinary headline
    # vocabulary, so "Karan Wahi Reacts" yields karan/wahi but not "reacts".
    for match in _NAMED_RE.finditer(text):
        for word in re.findall(r"[a-z0-9]+", match.group(1).lower()):
            if word in GENERIC_EVENT_WORDS or word in NON_NAME_WORDS or len(word) <= 2:
                break
            entities.add(_stem(word))

    # Other distinctive content words, useful when outlets do not repeat the
    # full name ("Manish Exits..." vs "Manish Goel Exits..."). NON_NAME_WORDS
    # is applied only to the capitalised-name runs above; here it would strip
    # the very words ("exit", "health", "gameplay") that tell events apart.
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if (len(word) > 3 and word not in GENERIC_EVENT_WORDS
                and word not in STOPWORDS):
            entities.add(_stem(word))
    return entities


def _same_event(a: set[str], b: set[str]) -> bool:
    """True when two entity signatures describe the same news event.

    Requires at least three shared entity tokens (so a bare 'Salman Khan' does
    not merge unrelated stories), or two shared tokens with very high overlap
    (short headline variations of one another).
    """
    if not a or not b:
        return False
    shared = a & b
    if len(shared) < 2:
        return False
    overlap = len(shared) / min(len(a), len(b))
    return len(shared) >= 3 or overlap >= 0.8


def source_quality(candidate: dict, cfg: dict) -> float:
    """Light discovery-time score: outlet trust, snippet length, image."""
    preferred = {s.lower() for s in cfg["topic"].get("preferred_sources", [])}
    source = (candidate.get("source") or "").lower()
    bonus = 3.0 if any(p in source or source in p for p in preferred) else 0.0
    length = min(len(candidate.get("description", "")) / 120.0, 2.0)
    image = 1.0 if candidate.get("thumbnail") else 0.0
    return round(bonus + length + image, 3)


def fetch_feed(url: str, params: dict | None = None) -> list[dict]:
    try:
        resp = requests.get(url, params=params, headers=UA, timeout=25)
        if resp.status_code != 200:
            return []
        payload = resp.content.decode("utf-8", "replace")
        return _parse_xml(payload)
    except Exception:
        return []


def discover(cfg: dict) -> list[dict]:
    topic = cfg["topic"]
    lookback = int(cfg["gate"]["lookback_days"])
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback)
    skip = tuple(p.lower() for p in topic.get("skip_url_patterns", []))
    keywords = [k.lower() for k in topic.get("keywords", [])]

    raw: list[dict] = []
    for query in topic.get("bing_queries", []):
        raw.extend(fetch_feed("https://www.bing.com/news/search",
                              {"q": query, "format": "rss", "mkt": "en-IN"}))
    for feed in topic.get("direct_feeds", []):
        raw.extend(fetch_feed(feed))

    # Collapse exact URL / title duplicates first, preferring the richer item.
    unique: dict[str, dict] = {}
    for item in raw:
        title = item.get("title", "")
        url = _publisher_url(item.get("link", ""))
        if not title or not url:
            continue
        if any(p in url.lower() for p in skip):
            continue
        low = title.lower()
        if keywords and not any(k in low for k in keywords):
            continue
        dt = _parse_date(item.get("pubdate", ""))
        if dt and dt < cutoff:
            continue
        canonical = canonical_url(url)
        candidate = {
            "title": title,
            "url": canonical,
            "source": item.get("source", "") or urlparse(url).netloc.replace("www.", ""),
            "description": item.get("description", ""),
            "pubdate": item.get("pubdate", ""),
            "published": dt.isoformat() if dt else "",
            "thumbnail": item.get("thumbnail", ""),
            "title_key": sha1(normalize_title(title)),
            "url_key": sha1(canonical),
        }
        candidate["quality"] = source_quality(candidate, cfg)
        key = candidate["url_key"]
        prior = unique.get(key)
        if prior is None or candidate["quality"] > prior["quality"]:
            unique[key] = candidate

    # Best sources first; recency breaks quality ties.
    ordered = sorted(unique.values(),
                     key=lambda c: (c["quality"], c.get("published", "")),
                     reverse=True)

    # Entity-based clustering. Union-find gives full transitive closure: if
    # outlet A and B cover one event and B and C cover it too, all three collapse
    # to one representative. Candidates were sorted best-first, so the lowest
    # index of a group is always its highest-quality outlet.
    signatures = [_event_signature(c["title"], cfg) for c in ordered]
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[max(a, b)] = min(a, b)

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            if _same_event(signatures[i], signatures[j]):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(len(ordered)):
        groups.setdefault(find(i), []).append(i)

    kept: list[dict] = []
    for root in sorted(groups):
        candidate = ordered[root]
        candidate["event_key"] = sha1(" ".join(sorted(signatures[root])))
        candidate["cluster_size"] = len(groups[root])
        kept.append(candidate)
    return kept
