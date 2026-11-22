"""News discovery: Bing News RSS + verified direct publisher feeds.

Parsing is tolerant: a handful of publisher feeds ship malformed XML, so we
fall back to a regex item extractor instead of dropping the whole feed.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlparse

import requests

from .utils import canonical_url, clean_text, normalize_title, sha1

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}


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

    raw: dict[str, dict] = {}
    for query in topic.get("bing_queries", []):
        for item in fetch_feed("https://www.bing.com/news/search",
                               {"q": query, "format": "rss", "mkt": "en-IN"}):
            raw.setdefault(item["title"], item)
    for feed in topic.get("direct_feeds", []):
        for item in fetch_feed(feed):
            raw.setdefault(item["title"], item)

    candidates: list[dict] = []
    for item in raw.values():
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
        candidates.append({
            "title": title,
            "url": canonical_url(url),
            "source": item.get("source", "") or urlparse(url).netloc.replace("www.", ""),
            "description": item.get("description", ""),
            "pubdate": item.get("pubdate", ""),
            "published": dt.isoformat() if dt else "",
            "thumbnail": item.get("thumbnail", ""),
            "title_key": sha1(normalize_title(title)),
            "url_key": sha1(canonical_url(url)),
        })
    candidates.sort(key=lambda c: c.get("published", ""), reverse=True)
    return candidates
