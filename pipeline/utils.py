from __future__ import annotations

import hashlib
import html
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlunparse

import yaml

STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "into", "over", "after",
    "about", "what", "why", "how", "are", "was", "has", "have", "its", "his",
    "her", "they", "you", "not", "but", "all", "new", "news", "latest", "bigg",
    "boss", "season", "show", "gets", "got", "says", "say", "will", "can", "its",
}


def load_config(path: str | Path = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def slugify(value: str, max_len: int = 60) -> str:
    value = re.sub(r"[^\w]+", "-", value.lower(), flags=re.UNICODE).strip("-")
    return value[:max_len].strip("-") or "reel"


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("\u2019", "'").replace("\u2018", "'")
    value = value.replace("\u201c", '"').replace("\u201d", '"')
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_url(url: str) -> str:
    try:
        parts = urlparse(url)
        query = parse_qs(parts.query)
        clean_q = {k: v for k, v in query.items() if not k.lower().startswith("utm_")}
        from urllib.parse import urlencode
        return urlunparse((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"),
                           urlencode(clean_q, doseq=True), ""))
    except Exception:
        return url or ""


def normalize_title(title: str) -> str:
    title = clean_text(title).lower()
    title = re.sub(r"\s*[-|]\s*[^-|]+$", "", title)  # strip " - Source"
    tokens = [t for t in re.findall(r"[a-z0-9]+", title) if t not in STOPWORDS]
    return " ".join(tokens)


def title_tokens(title: str) -> set[str]:
    return set(normalize_title(title).split())


def similarity(a: str, b: str) -> float:
    """Token Jaccard similarity of two titles."""
    sa, sb = title_tokens(a), title_tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def jaccard_sets(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def tokens(text: str) -> list[str]:
    return clean_text(text).split()


def sentence_split(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9'\"])", clean_text(text))
    return [p.strip() for p in parts if len(p.strip()) > 12]


def word_count(text: str) -> int:
    return len(tokens(text))


def run(cmd: list[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=capture,
                          encoding="utf-8", errors="replace")


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p
