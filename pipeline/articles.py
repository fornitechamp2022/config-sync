"""Article fetch, extraction and the no-LLM quality gate.

Only coherent body prose is allowed into the script pool. Related-story rails,
"also read" blocks, hashtags, photo credits and breadcrumb tails are common in
Indian entertainment pages and would otherwise leak into narration.
"""
from __future__ import annotations

import re
from collections import Counter, OrderedDict
from math import ceil
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .utils import clean_text, sentence_split, title_tokens, canonical_url

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
BAD_IMG = (
    "notification", "addr-bar", "arrow", "lock", "subscribe", "logo", "icon",
    "tracker", "google", "cross", "avatar", "sprite", "placeholder", "ads",
    "figma.site", "figma.com", "doubleclick", "googlesyndication", "adservice",
    "banner", "creative", "newsletter", "related", "comment", "share",
    "whatsapp", "telegram", "email", "promo", "widget", "breadcrumb",
)

BAD_PARA = (
    "photo credit", "image credit", "photo courtesy", "image courtesy",
    "read more", "also read", "also watch", "watch more", "related story",
    "related stories", "related news", "trending now", "top news",
    "must read", "editor's pick", "follow us", "follow @", "subscribe",
    "sign up", "newsletter", "advertisement", "advertorial", "sponsored",
    "click here", "copyright", "all rights reserved", "terms of use",
    "privacy policy", "story continues", "for more updates", "table of contents",
    "press trust of india", "pti ", "ians ", "live updates", "fact check",
)

# Container hints: whole blocks that never hold article prose.
BLOCK_HINTS = (
    "related", "recommend", "trending", "also-read", "read-more",
    "newsletter", "social", "comments", "advert", "promo", "widget",
    "sidebar", "breadcrumb", "share", "tags", "footer", "masthead",
    "modal", "popup", "overlay", "more-stories",
)

# Inline tails that sometimes sit at the end of an otherwise good paragraph.
_TAIL_RE = re.compile(
    r"\s*(?:\||—|\s-\s)\s*(?:read\s+more|also\s+read|also\s+watch|watch\s+more|"
    r"follow\s+us|photo\s+credit|image\s+credit|via\s+@?\w+|"
    r"[A-Z][\w.&' -]{2,40}(?:\s+TV|\s+News|\s+Desk|\s+Bureau))\s*$",
    re.I,
)
_LEAD_RE = re.compile(
    r"^\s*(?:also\s+read|also\s+watch|read\s+more|watch\s+more|related\s+story|"
    r"related\s+stories|trending\s+now|top\s+news|must\s+read|"
    r"(?:photo|image)\s+(?:credit|courtesy))\s*[:\-|]\s*",
    re.I,
)
_LIST_START_RE = re.compile(
    r"^\s*(?:\d+[.)]\s+|[-•*·]\s+|top\s+\d+\b|list\s+of\b|"
    r"here\s+are\s+\d+\b|\d+\s+(?:things|reasons|ways)\b)",
    re.I,
)


def _strip_inline(text: str) -> str:
    """Remove breadcrumbs, hashtags and credit tails from a paragraph."""
    text = clean_text(text)
    text = _LEAD_RE.sub("", text)
    text = _TAIL_RE.sub("", text)
    text = re.sub(r"#[A-Za-z0-9_]+", " ", text)          # hashtags
    text = re.sub(r"\b(?:photo|image)\s+credit\s*[:\-].*$", " ", text, flags=re.I)
    text = re.sub(r"\s+[|]\s*[A-Z][\w &'.-]{2,40}\s*$", " ", text)  # "| Source"
    text = re.sub(r"\s+", " ", text).strip(" \t-–—|")
    return text


def _coherent_paragraph(text: str) -> bool:
    """Reject listicle fragments, captions and one-line blurbs."""
    if len(text) < 55 or len(text.split()) < 11:
        return False
    if _LIST_START_RE.search(text):
        return False
    if text.count("|") >= 2 or text.count("#") >= 1:
        return False
    sentences = [s for s in sentence_split(text) if len(s.split()) >= 6]
    # A paragraph made mostly of stubs ("Kanika. Gullu. Mahhi.") is a list,
    # not narration, even when the concatenated character count looks fine.
    if len(sentences) < 1:
        return False
    fragments = sum(1 for s in sentence_split(text) if len(s.split()) < 6)
    if fragments >= 3 and fragments > len(sentences):
        return False
    return True


def _remove_blocks(soup: BeautifulSoup) -> None:
    """Decompose pages' related/trending/social containers before parsing."""
    doomed = []
    for tag in soup.find_all(True):
        blob = " ".join(filter(None, [
            " ".join(tag.get("class", []) if isinstance(tag.get("class"), list)
                     else [str(tag.get("class") or "")]),
            str(tag.get("id") or ""),
            str(tag.get("role") or ""),
        ])).lower()
        if any(hint in blob for hint in BLOCK_HINTS):
            doomed.append(tag)
    for tag in doomed:
        if tag.parent is not None:
            tag.decompose()


def fetch_article(url: str, cfg: dict, candidate: dict | None = None) -> dict | None:
    try:
        resp = requests.get(url, headers=UA, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.content, "html.parser")
    except Exception:
        return None

    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    _remove_blocks(soup)

    paragraphs: list[str] = []
    for node in soup.find_all(["p", "li"]):
        text = _strip_inline(node.get_text(" "))
        if not text or any(b in text.lower() for b in BAD_PARA):
            continue
        if not _coherent_paragraph(text):
            continue
        if text not in paragraphs:
            paragraphs.append(text)

    og = None
    for attr in (("property", "og:image"), ("name", "twitter:image"),
                 ("name", "twitter:image:src"), ("property", "og:image:url")):
        node = soup.find("meta", attrs={attr[0]: attr[1]})
        if node and node.get("content"):
            og = urljoin(url, node["content"])
            break

    images: list[str] = []
    if og:
        images.append(og)
    for img in soup.find_all("img"):
        src = (img.get("data-original") or img.get("data-src") or img.get("src") or "").strip()
        if not src or "${" in src:
            continue
        low = src.lower()
        if any(b in low for b in BAD_IMG) or "fit-in" in low:
            continue
        if not re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", low):
            continue
        full = urljoin(url, src.replace("&amp;", "&"))
        # Times of India thumbnails: ask for full resolution
        if "toiimg.com/thumb/" in full and re.search(r"[?&]width=\d+", full):
            full = re.sub(r"width=\d+", "width=1280", full)
            full = re.sub(r"height=\d+", "height=720", full)
        if full not in images:
            images.append(full)

    body = " ".join(paragraphs)
    title = (candidate or {}).get("title", "")
    t_tokens = title_tokens(title)
    b_tokens = title_tokens(body[:4000])
    overlap = (len(t_tokens & b_tokens) / len(t_tokens)) if t_tokens else 0.0
    gate = cfg["gate"]
    accepted = (
        len(paragraphs) >= int(gate["min_paragraphs"])
        and len(body) >= int(gate["min_chars"])
        and bool(og)
        and (not title or overlap >= float(gate["title_overlap_ratio"]))
    )

    return {
        "title": title,
        "url": canonical_url(url),
        "url_key": (candidate or {}).get("url_key", canonical_url(url)),
        "title_key": (candidate or {}).get("title_key", ""),
        "source": (candidate or {}).get("source", ""),
        "description": _strip_inline((candidate or {}).get("description", "")),
        "published": (candidate or {}).get("published", ""),
        "paragraphs": paragraphs,
        "body": body,
        "images": images[:20],
        "og_image": og,
        "overlap": round(overlap, 2),
        "accepted": accepted,
    }


def article_quality(article: dict, cfg: dict) -> float:
    """Post-fetch score: preferred outlet, real article length, image count."""
    preferred = {s.lower() for s in cfg["topic"].get("preferred_sources", [])}
    source = (article.get("source") or "").lower()
    bonus = 3.0 if any(p in source or source in p for p in preferred) else 0.0
    length = min(len(article.get("body", "")) / 500.0, 2.0)
    images = min(len(article.get("images", [])) / 3.0, 1.5)
    return round(bonus + length + images, 3)


def _source_name(article: dict) -> str:
    return (article.get("source") or "").strip() or "unknown"


def _diverse_selection(ordered: list[dict], limit: int) -> list[dict]:
    """Quality-first selection with a per-outlet ceiling.

    A plain top-N by ``article_quality`` lets preferred outlets (which get a
    scoring bonus) crowd out every other publication. We still take the best
    article available at each step, but never more than ``cap`` from one outlet
    while other accepted articles remain. A second pass fills any empty slots
    so a small candidate pool is not wasted.
    """
    if limit <= 0 or not ordered:
        return []
    cap = max(2, ceil(limit / 6))  # 12 slots -> 2 per outlet -> room for 6+
    selected: list[dict] = []
    used: Counter = Counter()
    for article in ordered:
        if used[_source_name(article)] < cap:
            selected.append(article)
            used[_source_name(article)] += 1
    if len(selected) < limit:
        chosen = {id(a) for a in selected}
        for article in ordered:
            if len(selected) >= limit:
                break
            if id(article) not in chosen:
                selected.append(article)
                chosen.add(id(article))
    return selected[:limit]


def _interleave_by_source(selected: list[dict]) -> list[dict]:
    """Round-robin the chosen articles so any prefix spans many outlets.

    Downstream consumers build scripts in list order, so an interleaved list
    keeps even a short sample (8-12 scripts) source-diverse instead of starting
    with six stories from the same publication. Within each outlet the
    quality-first order is preserved.
    """
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for article in selected:
        groups.setdefault(_source_name(article), []).append(article)
    out: list[dict] = []
    while any(groups.values()):
        for source in list(groups):
            if groups[source]:
                out.append(groups[source].pop(0))
    return out


def enrich_and_gate(candidates: list[dict], cfg: dict, limit: int = 24) -> list[dict]:
    ordered = sorted(candidates,
                     key=lambda c: (c.get("quality", 0.0), c.get("published", "")),
                     reverse=True)
    # Fetch a modest pool, then keep the genuinely best articles rather than
    # letting an early, mediocre candidate occupy a slot.
    fetch_cap = min(len(ordered), max(limit * 2, 40))
    accepted: list[dict] = []
    for cand in ordered[:fetch_cap]:
        article = fetch_article(cand["url"], cfg, cand)
        if article and article["accepted"]:
            if article.get("quality") is None:
                article["quality"] = article_quality(article, cfg)
            accepted.append(article)
    accepted.sort(key=lambda a: (a.get("quality", 0.0), a.get("published", "")),
                  reverse=True)
    return _interleave_by_source(_diverse_selection(accepted, limit))
