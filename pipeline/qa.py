"""Script quality flags used by the pilot and by the production gate.

Flags describe the *spoken script*, not the source metadata. In particular a
question mark in a headline that was never spoken is not a defect. New flags
(``list_like``, ``low_entity_density``) are warnings, like the older non-critical
flags; existing flag names are kept so downstream gates stay compatible.
"""
from __future__ import annotations

import re

from .utils import STOPWORDS

RELATIVE = re.compile(r"\b(today|tonight|yesterday|last night|this week|now|aaj|kal|raat)\b", re.I)
VAGUE = re.compile(r"\b(he|she|they|it|this|that|these|those|his|her|their|him|them)\b", re.I)
CLICKBAIT = re.compile(r"here.s why|details inside|what we know|shocking|you won.t believe|top \d", re.I)
META = re.compile(
    r"\||\bupdated\b|अद्यतन|\bread more\b|\bphoto credit\b|\balso read\b|"
    r"\balso watch\b|\bfollow us\b",
    re.I,
)

# Brand names are legitimately left in Latin script by the translator. Only
# other Latin runs count as an untranslated-text warning.
LATIN_OK = frozenset({
    "JioHotstar", "Hotstar", "Colors", "Instagram", "YouTube", "Facebook",
    "WhatsApp", "Twitter", "Netflix", "Amazon", "Google", "ScoutOP",
    "Dailymotion", "Prime", "CBFC", "OTT", "VFX", "Bollywood",
})
COMMON_SENTENCE_START = {
    "the", "a", "an", "after", "before", "as", "in", "on", "at", "by", "for",
    "from", "with", "and", "but", "so", "then", "this", "that", "these",
    "those", "he", "she", "they", "it", "his", "her", "their", "him", "them",
    "here", "thousands", "several", "many", "most", "some", "amid", "during",
    "over", "inside", "fans", "actors", "sources", "reports", "new", "latest",
    "top", "ex", "why", "how", "what", "when", "where", "who", "will",
}
HINDI_FUNCTION_WORDS = {
    "यह", "वह", "ये", "वे", "का", "की", "के", "में", "से", "और", "है", "हैं",
    "था", "थी", "जब", "तब", "तो", "भी", "ही", "नहीं", "उन", "उसे", "उस",
    "अपनी", "अपने", "सभी", "कुछ", "रहा", "रही", "गया", "गई", "लिए",
    "बाद", "पहले", "दौरान", "इस", "उसकी", "उनकी", "जो", "कि", "तथा",
}
_HINDI_WORD = re.compile(r"[\u0900-\u097f]{3,}")
_CAP_NAME = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)\b")


def _list_like(sentence: str) -> bool:
    """A run of four+ short comma-separated names is a list, not prose."""
    parts = [p.strip() for p in re.split(r"[,;]", sentence) if p.strip()]
    if len(parts) < 5:
        return False
    short = [p for p in parts
             if len(re.findall(r"\w+|[\u0900-\u097f]+", p)) <= 4]
    return len(short) >= 4


def _entity_density(story: dict) -> float:
    """Distinct named entities per sentence (0 = no concrete referents)."""
    text = story.get("script", "")
    sentences = [s for s in story.get("sentences", []) if s.strip()]
    if not text or not sentences:
        return 1.0  # nothing to judge
    if story.get("language") == "en":
        names = {
            name.lower() for name in _CAP_NAME.findall(text)
            if name.split()[0].lower() not in COMMON_SENTENCE_START
            and name.split()[0].lower() not in STOPWORDS
        }
        # Single-word headline caps such as 'Kanika' are entities too.
        names.update(
            word.lower() for word in re.findall(r"\b[A-Z][a-z]{3,}\b", text)
            if word.lower() not in COMMON_SENTENCE_START
            and word.lower() not in STOPWORDS
        )
    else:
        names = {w for w in _HINDI_WORD.findall(text) if w not in HINDI_FUNCTION_WORDS}
    return len(names) / len(sentences)


def flags(story: dict, cfg: dict) -> list[str]:
    text = story.get("script", "")
    sentences = story.get("sentences", [])
    hook = story.get("hook", "")
    out: list[str] = []

    if "..." in text or "…" in text:
        out.append("truncated_lead")
    if RELATIVE.search(text):
        out.append("time_relative")
    if META.search(text):
        out.append("metadata_leak")
    if CLICKBAIT.search(text):
        out.append("clickbait")
    # Only a spoken question is a flag; a '?' in headline metadata is not.
    if "?" in hook or "?" in text:
        out.append("question_headline")
    if len(sentences) < 5:
        out.append("too_few_sentences")

    norm = [re.sub(r"[^a-z0-9\u0900-\u097f]+", "", s.lower()) for s in sentences]
    norm = [n for n in norm if n]
    if len(set(norm)) < len(norm):
        out.append("repeated_sentence")

    if any(_list_like(s) for s in sentences):
        out.append("list_like")

    if story.get("language") == "en":
        if VAGUE.search(text):
            out.append("vague_pronoun")
    else:
        if text.count("।") < max(1, len(sentences) - 1):
            out.append("no_sentence_punctuation")
        latin = {w for w in re.findall(r"[A-Za-z]{5,}", text) if w not in LATIN_OK}
        if latin:
            out.append("latin_in_hindi")

    if len(sentences) >= 4 and _entity_density(story) < 0.5:
        out.append("low_entity_density")

    wc = story.get("word_count", 0)
    if wc and not (int(cfg["story"]["min_words"]) - 15 <= wc <= int(cfg["story"]["max_words"]) + 20):
        out.append("word_count_out_of_range")
    return out
