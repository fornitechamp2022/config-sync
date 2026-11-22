"""Script quality flags used by the pilot and by the production gate.

Flags describe the *spoken script*, not the source metadata. In particular a
question mark in a headline that was never spoken is not a defect.
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

    if story.get("language") == "en":
        if VAGUE.search(text):
            out.append("vague_pronoun")
    else:
        if text.count("।") < max(1, len(sentences) - 1):
            out.append("no_sentence_punctuation")
        if re.search(r"[A-Za-z]{5,}", text):
            out.append("latin_in_hindi")

    wc = story.get("word_count", 0)
    if wc and not (int(cfg["story"]["min_words"]) - 15 <= wc <= int(cfg["story"]["max_words"]) + 20):
        out.append("word_count_out_of_range")
    return out
