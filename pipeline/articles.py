"""Article fetch, extraction and the no-LLM quality gate."""
from __future__ import annotations

import re
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .utils import clean_text, title_tokens, canonical_url

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
}
BAD_IMG = (
    "notification", "addr-bar", "arrow", "lock", "subscribe", "logo", "icon",
    "tracker", "google", "cross", "avatar", "sprite", "placeholder", "ads",
    "figma.site", "figma.com", "doubleclick", "googlesyndication", "adservice",
    "banner", "creative", "newsletter",
)
BAD_PARA = (
    "photo credit", "read more", "also read", "also watch", "follow us",
    "subscribe", "advertisement", "click here", "copyright", "all rights reserved",
)


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

    paragraphs = []
    for p in soup.find_all("p"):
        text = clean_text(p.get_text(" "))
        if len(text) < 45:
            continue
        if any(b in text.lower() for b in BAD_PARA):
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
        "description": clean_text((candidate or {}).get("description", "")),
        "published": (candidate or {}).get("published", ""),
        "paragraphs": paragraphs,
        "body": body,
        "images": images[:20],
        "og_image": og,
        "overlap": round(overlap, 2),
        "accepted": accepted,
    }


def enrich_and_gate(candidates: list[dict], cfg: dict, limit: int = 24) -> list[dict]:
    preferred = {s.lower() for s in cfg["topic"].get("preferred_sources", [])}
    ordered = sorted(candidates, key=lambda c: c.get("published", ""), reverse=True)
    ordered.sort(key=lambda c: c.get("source", "").lower() not in preferred)
    results = []
    for cand in ordered[:limit]:
        article = fetch_article(cand["url"], cfg, cand)
        if article and article["accepted"]:
            results.append(article)
    return results
