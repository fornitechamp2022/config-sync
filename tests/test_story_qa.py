"""Focused tests for the deterministic story builder and its QA flags."""
from __future__ import annotations

import re

import pytest

import pipeline.translate as translate_mod
from pipeline.qa import flags
from pipeline.story import _anchors, _resolve_pronouns, build_story, to_hindi


def _cfg() -> dict:
    return {
        "story": {
            "min_words": 60,
            "max_words": 120,
            "min_sentences": 4,
            "cta": "Follow for more Bollywood updates.",
        },
        "topic": {
            "primary": "Bigg Boss 20",
            "keywords": [
                "Bigg Boss", "Salman Khan", "Bollywood", "Kanika", "Qazi",
            ],
        },
    }


BODY = [
    "Salman Khan opened the Weekend Ka Vaar episode with a sharp monologue about discipline.",
    "The eviction round left three contestants shaken before the final announcement.",
    "Salman Khan watched Kanika question the captaincy decision during a heated dinner conversation.",
    "A former contestant praised the production team for the smooth live broadcast.",
    "Salman Khan and the makers teased a surprise guest entry, which sent viewers to social media.",
    "The makers confirmed a fresh task to test the contestants during the weekend.",
    "Salman Khan reviewed the nomination chart before the house gathered for the weekly task.",
    "The captaincy race intensified after the latest round reshuffled the contestants.",
    "Salman Khan met the producers after the finale to discuss the season's biggest twist.",
    "Several celebrities congratulated the finalists on social media after the episode aired.",
]


def _article(
    *,
    title: str = "Salman Khan Bigg Boss 20 eviction",
    description: str = (
        "Salman Khan addressed the Bigg Boss 20 eviction in the latest episode "
        "and shared strong views with the audience."
    ),
    extra_paragraphs: list[str] | None = None,
    paragraphs: list[str] | None = None,
) -> dict:
    if paragraphs is None:
        all_sentences = BODY + list(extra_paragraphs or [])
        # Each paragraph carries at least two headline entities ("Salman
        # Khan" / "eviction") so the topic-drift guard keeps it.
        paragraphs = [
            " ".join(all_sentences[i:i + 2])
            for i in range(0, len(all_sentences), 2)
        ]
    return {
        "title": title,
        "description": description,
        "published": "2026-11-21T10:00:00Z",
        "source": "Test Source",
        "url": "https://example.com/story",
        "paragraphs": paragraphs,
    }


# --- deterministic, offline translation -------------------------------------

_TOKEN_CACHE: dict[str, str] = {}


def _token_for(word: str) -> str:
    key = re.sub(r"[^A-Za-z]", "", word).lower() or word
    if key not in _TOKEN_CACHE:
        digits = "".join(chr(0x0966 + int(d)) for d in str(len(_TOKEN_CACHE)))
        _TOKEN_CACHE[key] = "वाक्य" + digits
    return _TOKEN_CACHE[key]


def _fake_translate(text: str, src: str = "en", tgt: str = "hi") -> str:
    core = re.sub(r"[.!?]+$", "", text)
    return " ".join(_token_for(w) for w in core.split()) + "।"


@pytest.fixture(autouse=True)
def offline_translation(monkeypatch):
    monkeypatch.setattr(translate_mod, "translate", _fake_translate)
    monkeypatch.setattr(translate_mod, "romanize", lambda text: text)


# --- punctuation / truncation ------------------------------------------------

def test_sentence_punctuation_preserved():
    en = build_story(_article(), _cfg())
    assert en is not None
    assert all(s.endswith((".", "!", "?")) for s in en["sentences"])
    assert "..." not in en["script"] and "…" not in en["script"]

    hi = to_hindi(en, _cfg())
    assert hi["sentences"]
    assert all(s.endswith("।") for s in hi["sentences"])
    assert hi["script"].count("।") == len(hi["sentences"])
    assert "..." not in hi["script"]
    assert "no_sentence_punctuation" not in flags(hi, _cfg())


def test_whole_sentences_never_truncated():
    en = build_story(_article(), _cfg())
    assert en is not None
    assert en["selected_body"]
    # Every kept body sentence is a byte-for-byte whole source sentence.
    for sentence in en["selected_body"]:
        assert sentence in BODY
        assert sentence in en["script"]


# --- pronouns ----------------------------------------------------------------

def test_personal_pronouns_resolved_or_dropped():
    article = _article(extra_paragraphs=[
        "Salman Khan hosted the episode and he praised the contestants in the finale.",
        "He thanked the audience after the dramatic eviction round ended.",
        "It was a surprising decision that shocked the entire studio audience.",
    ])
    en = build_story(article, _cfg())
    assert en is not None
    words = set(re.findall(r"[A-Za-z]+", en["script"].lower()))
    assert "he" not in words
    assert "it" not in words
    assert "It was a surprising decision" not in en["script"]
    assert "vague_pronoun" not in flags(en, _cfg())


def test_possessive_pronoun_uses_nearest_named_entity():
    cfg = _cfg()
    anchors = _anchors("Salman Khan Bigg Boss 20 eviction", cfg)
    sentence = (
        "Salman Khan hosted the show, and his comments about the finale "
        "impressed the judges."
    )
    resolved = _resolve_pronouns(sentence, anchors, ["Salman Khan"])
    assert resolved == (
        "Salman Khan hosted the show, and Salman Khan's comments about the "
        "finale impressed the judges."
    )


def test_ambiguous_pronoun_sentence_dropped():
    cfg = _cfg()
    article = _article(
        title="Salman Khan Kanika Bigg Boss 20 eviction",
        extra_paragraphs=[
            "They left the studio together after the heated argument ended.",
        ],
    )
    en = build_story(article, cfg)
    assert en is not None
    assert "They left the studio" not in en["script"]
    assert "vague_pronoun" not in flags(en, cfg)


# --- metadata / short sources ------------------------------------------------

def test_metadata_and_breadcrumbs_removed():
    article = _article(
        title="Salman Khan Bigg Boss 20 eviction | Test Source",
        description=(
            "Updated: Salman Khan discussed the eviction in a new interview "
            "| Test Source | Read more."
        ),
        extra_paragraphs=[
            "Also read: Salman Khan and the Bigg Boss 20 eviction story continues here.",
        ],
    )
    en = build_story(article, _cfg())
    assert en is not None
    lowered = en["script"].lower()
    for marker in ("|", "updated", "also read", "read more", "test source"):
        assert marker not in lowered
    assert "metadata_leak" not in flags(en, _cfg())

    hi = to_hindi(en, _cfg())
    assert "metadata_leak" not in flags(hi, _cfg())


def test_short_source_rejected():
    article = _article(
        description="",
        paragraphs=[
            "Salman Khan addressed the Bigg Boss 20 eviction during the latest episode.",
            "Kanika praised the contestants after the dramatic finale ceremony.",
        ],
    )
    assert build_story(article, _cfg()) is None


def test_relative_time_replaced_with_absolute_date():
    article = _article(description=(
        "Salman Khan praised the contestants today for discipline and teamwork "
        "during a live interview with the audience."
    ))
    en = build_story(article, _cfg())
    assert en is not None
    assert "today" not in en["script"].lower()
    assert "November 21" in en["script"]
    assert "time_relative" not in flags(en, _cfg())


def test_hindi_translation_strips_leaked_breadcrumb(monkeypatch):
    en = build_story(_article(), _cfg())
    assert en is not None

    def leaky_translate(text: str, src: str = "en", tgt: str = "hi") -> str:
        return "अद्यतन | " + _fake_translate(text, src, tgt)

    monkeypatch.setattr(translate_mod, "translate", leaky_translate)
    hi = to_hindi(en, _cfg())
    assert "अद्यतन" not in hi["script"]
    assert "|" not in hi["script"]
    assert "metadata_leak" not in flags(hi, _cfg())


# --- QA flag accuracy --------------------------------------------------------

def test_question_headline_flag_only_when_spoken():
    article = _article(
        title="Why did Salman Khan exit Bigg Boss 20?",
        description=(
            "Salman Khan confirmed the exit from the show after the finale "
            "and thanked the audience for their support."
        ),
    )
    en = build_story(article, _cfg())
    assert en is not None
    assert en["headline"].endswith("?")
    assert "?" not in en["script"]
    assert "question_headline" not in flags(en, _cfg())


def test_question_sentences_are_not_narrated():
    article = _article(
        title="Why did Salman Khan exit Bigg Boss 20?",
        description=(
            "Did Salman Khan really leave the show during the finale? "
            "He discussed the decision with the audience after the episode."
        ),
    )
    en = build_story(article, _cfg())
    assert en is not None
    assert "?" not in en["script"]
    assert "question_headline" not in flags(en, _cfg())


def test_question_headline_flag_when_spoken():
    story = {
        "script": "Is Salman Khan leaving the show?",
        "hook": "Is Salman Khan leaving the show?",
        "sentences": ["Is Salman Khan leaving the show?"],
        "word_count": 7,
        "language": "en",
    }
    assert "question_headline" in flags(story, _cfg())


def test_flags_reflect_actual_script_content():
    story = {
        "script": "A | B ...",
        "hook": "A",
        "sentences": ["A", "B"],
        "word_count": 2,
        "language": "en",
    }
    found = flags(story, _cfg())
    assert "metadata_leak" in found
    assert "truncated_lead" in found
    assert "too_few_sentences" in found
    assert "word_count_out_of_range" in found
    assert "question_headline" not in found


# --- word-count band / translation variance ---------------------------------

def test_word_count_band_english_and_hindi():
    en = build_story(_article(), _cfg())
    assert en is not None
    hi = to_hindi(en, _cfg())
    assert "word_count_out_of_range" not in flags(en, _cfg())
    assert "word_count_out_of_range" not in flags(hi, _cfg())
    assert 45 <= en["word_count"] <= 140
    assert 45 <= hi["word_count"] <= 140


def test_hindi_adds_whole_sentences_when_translation_short(monkeypatch):
    en = build_story(_article(), _cfg())
    assert en is not None
    assert en["extra_body"], "fixture should leave unused clean sentences"

    def short_translate(text: str, src: str = "en", tgt: str = "hi") -> str:
        words = re.sub(r"[.!?]+$", "", text).split()
        return " ".join(_token_for(w) for w in words[::2]) + "।"

    monkeypatch.setattr(translate_mod, "translate", short_translate)
    hi = to_hindi(en, _cfg())
    assert len(hi["sentences"]) > len(en["sentences"])
    assert all(s.endswith("।") for s in hi["sentences"])


def test_hindi_drops_whole_sentences_when_translation_long(monkeypatch):
    en = build_story(_article(), _cfg())
    assert en is not None

    def long_translate(text: str, src: str = "en", tgt: str = "hi") -> str:
        words = re.sub(r"[.!?]+$", "", text).split()
        doubled = words + words
        return " ".join(_token_for(w) for w in doubled) + "।"

    monkeypatch.setattr(translate_mod, "translate", long_translate)
    hi = to_hindi(en, _cfg())
    assert len(hi["sentences"]) < len(en["sentences"])
    assert all(s.endswith("।") for s in hi["sentences"])
    assert "word_count_out_of_range" not in flags(hi, _cfg())
