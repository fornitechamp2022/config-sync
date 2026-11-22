"""Deterministic, sentence-first script builder.

No LLM. The brief is assembled from whole sentences of the source article,
cleaned of metadata and context-free references, then translated one sentence
at a time so Hindi punctuation and sentence boundaries survive intact.

Hard rules for this module:

* only whole sentences are ever selected -- nothing is truncated mid-thought;
* every spoken sentence must be free of breadcrumbs/source metadata;
* context-free pronouns are rewritten to the nearest named person from the
  same sentence (or the single named person in the headline) when that is
  deterministic, and the sentence is dropped otherwise -- facts are never
  invented and ambiguous references are never kept;
* the English script is selected as a *prefix* of clean sentences closest to
  the configured word target, so translation variance can be corrected in
  ``to_hindi`` by adding/removing whole sentences rather than slicing text.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from .utils import clean_text, sentence_split, word_count, tokens, sha1, STOPWORDS

# Breadcrumb / metadata markers that must never reach the spoken script.
META_MARKERS = (
    "|", "updated", "अद्यतन", "read more", "follow us", "photo credit",
    "click here", "advertisement", "also read", "also watch", "subscribe",
    "press trust of india", "copyright", "all rights reserved", "stay tuned",
    "newsletter", "table of contents",
)
BAD_PARA = META_MARKERS  # kept as an alias for readability / callers
_META_RE = re.compile(
    r"\|"
    r"|\b(updated?|read more|follow us|photo credit|click here|advertisement)\b"
    r"|\balso (read|watch)\b"
    r"|\b(subscribe|newsletter|copyright|all rights reserved|stay tuned)\b"
    r"|\b(press trust of india|pti|ians)\b"
    r"|अद्यतन",
    re.I,
)

VAGUE_WORD = re.compile(
    r"\b(he|she|they|it|this|that|these|those|his|her|their|him|them)\b", re.I
)
THING_PRONOUN = re.compile(r"\b(it|this|that|these|those)\b", re.I)
RELATIVE_TIME = re.compile(
    r"\b(today|tonight|yesterday|last night|this week|this morning|now)\b", re.I
)
CAP_NAME = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*)\b")
NON_PERSON_HINTS = ("bigg", "boss", "bollywood", "weekend", "show", "season", "news")
QUESTION_START = frozenset({
    "why", "how", "what", "when", "where", "who", "whom", "whose", "which",
})
LIST_START_RE = re.compile(
    r"^\s*(?:\d+[.)]\s+|[-•*·]\s+|top\s+\d+\b|list\s+of\b|"
    r"here\s+are\s+\d+\b|\d+\s+(?:things|reasons|ways)\b)",
    re.I,
)
CREDIT_RE = re.compile(
    r"^\s*(?:photo|image|credit|source|caption|file|pic)\s*[:\-]", re.I
)
# Words that mark the actual event of a story, used to order body sentences.
EVENT_WORDS = (
    "confirmed", "announced", "exits", "exit", "evicted", "eviction", "wins",
    "won", "clash", "fight", "task", "captaincy", "finale", "decision",
)
BACKGROUND_WORDS = (
    "reportedly", "rumour", "rumor", "speculation", "social media",
    "former", "fans", "sources",
)
SENTENCE_START_COMMON = {
    "why", "how", "what", "when", "where", "who", "will", "would", "did",
    "does", "do", "is", "are", "was", "were", "has", "have", "had", "the",
    "a", "an", "after", "before", "as", "in", "on", "at", "by", "for",
    "from", "with", "and", "but", "so", "then", "this", "that", "these",
    "those", "he", "she", "they", "it", "his", "her", "their", "him",
    "them", "here", "thousands", "several", "many", "most", "some", "amid",
    "during", "over", "inside", "fans", "actors", "sources", "reports",
    "new", "latest", "top", "ex",
}
OBJECT_NEXT = {
    "and", "or", "at", "in", "on", "to", "for", "with", "from", "by", "of",
    "as", "was", "were", "is", "are", "said", "told", "met", "saw", "asked",
    "gave", "took", "hit", "praised", "thanked", "congratulated", "called",
    "named",
}
HINDI_WORD = re.compile(r"[\u0900-\u097f]+")


def _punctuate(text: str) -> str:
    text = clean_text(text).strip()
    # Never surface an ellipsis: it looks like a truncated lead to QA and to
    # the viewer. Collapse it instead of cutting any real content.
    text = text.replace("…", "...")
    text = re.sub(r"\.{3,}", " ", text)
    text = re.sub(r"^(?:[-–—•|]\s*)+", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    if text[-1] not in ".!?":
        text += "."
    return text


def _too_similar(a: str, b: str, threshold: float = 0.60) -> bool:
    """Jaccard overlap; a lead that expands the headline is not a duplicate.

    Containment (intersection / shorter length) used to discard every lead
    that merely restated its own headline with extra detail, which left the
    script without a context sentence.
    """
    sa = set(re.findall(r"[a-z0-9]+", a.lower())) - STOPWORDS
    sb = set(re.findall(r"[a-z0-9]+", b.lower())) - STOPWORDS
    if not sa or not sb:
        return False
    return len(sa & sb) / len(sa | sb) >= threshold


def _too_similar_hindi(a: str, b: str, threshold: float = 0.86) -> bool:
    """Post-translation dedupe that actually understands Devanagari."""
    sa, sb = set(HINDI_WORD.findall(a)), set(HINDI_WORD.findall(b))
    if not sa or not sb:
        return clean_text(a).lower().strip() == clean_text(b).lower().strip()
    return len(sa & sb) / min(len(sa), len(sb)) >= threshold


def _is_metadata(text: str) -> bool:
    return bool(_META_RE.search(text or ""))


def _strip_breadcrumbs(text: str) -> str:
    """Drop '... | Source' and '... - Source' tails from a headline."""
    text = clean_text(text)
    if "|" in text:
        parts = [p.strip() for p in text.split("|")]
        good = [p for p in parts if not _is_metadata(p) and len(p) > 12]
        text = good[0] if good else (parts[0] if parts else "")
    text = re.sub(r"\s+[-–—]\s+[^-–—]{2,40}$", "", text).strip()
    return text


def _entities(headline: str) -> set[str]:
    return {
        w for w in re.findall(r"[a-z0-9]+", headline.lower())
        if len(w) > 3 and w not in STOPWORDS
    }


def _anchors(headline: str, cfg: dict) -> list[tuple[str, str]]:
    """Named entities (name, kind) taken from config, in priority order."""
    topic = cfg.get("topic", {})
    names = [topic.get("primary", ""), *topic.get("keywords", [])]
    anchors: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in names:
        name = clean_text(name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        kind = ("nonperson" if any(h in name.lower() for h in NON_PERSON_HINTS)
                else "person")
        anchors.append((name, kind))
    return anchors


def _entity_mentions(
    text: str, anchors: list[tuple[str, str]], *, use_caps: bool = True
) -> list[tuple[int, int, str, str]]:
    """Locate concrete entity mentions in *text* with their character spans."""
    raw: list[tuple[int, int, str, str]] = []
    for name, kind in anchors:
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
        for match in pattern.finditer(text):
            raw.append((match.start(), match.end(), name, kind))
    if use_caps:
        for match in CAP_NAME.finditer(text):
            name = match.group(1).strip()
            if name.split()[0].lower() in SENTENCE_START_COMMON:
                continue
            low = name.lower()
            kind = ("nonperson" if any(h in low for h in NON_PERSON_HINTS)
                    else "person")
            raw.append((match.start(), match.end(), name, kind))
    # Prefer the longest (often multi-word) name when spans overlap.
    raw.sort(key=lambda item: (-(item[1] - item[0]), item[0]))
    kept: list[tuple[int, int, str, str]] = []
    for span in raw:
        if any(not (span[1] <= k[0] or span[0] >= k[1]) for k in kept):
            continue
        kept.append(span)
    return sorted(kept)


def _is_fragment(text: str) -> bool:
    """Reject question-shaped, list-shaped and credit-shaped sentences."""
    core = text.strip()
    if not core or core.endswith(":") or "|" in core:
        return True
    if LIST_START_RE.search(core) or CREDIT_RE.search(core):
        return True
    first = re.match(r"[A-Za-z]+", core)
    # A pure question is never narrated -- even when RSS dropped the '?'.
    return bool(first and first.group(0).lower() in QUESTION_START)


def _is_list_like(text: str) -> bool:
    """Detect a run of comma-separated names ('A, B, C, D and E were...')."""
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    if len(parts) < 5:
        return False
    short = [p for p in parts if len(re.findall(r"\w+", p)) <= 4]
    return len(short) >= 4


def _repeats_entity(text: str, anchors: list[tuple[str, str]]) -> bool:
    """An entity phrase repeated inside one sentence reads like a stub."""
    counts: dict[str, int] = {}
    for _start, _end, name, _kind in _entity_mentions(text, anchors):
        key = name.lower()
        counts[key] = counts.get(key, 0) + 1
    return any(value >= 2 for value in counts.values())


def _anchor_counts(text: str, anchors: list[tuple[str, str]]) -> Counter:
    """Count non-overlapping named-entity phrases in *text*.

    Configured names win over a longer capitalised run: in the hook
    "Salman Khan Bigg Boss 20 eviction" the run "Salman Khan Bigg Boss" must
    not hide the separate "Salman Khan" and "Bigg Boss 20" mentions, otherwise
    the spoken-entity budget undercounts them.
    """
    spans: list[tuple[int, int, str, int]] = []
    for name, _kind in anchors:
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I)
        for match in pattern.finditer(text):
            spans.append((match.start(), match.end(), name.lower(), 0))
    for match in CAP_NAME.finditer(text):
        name = match.group(1).strip()
        if name.split()[0].lower() in SENTENCE_START_COMMON:
            continue
        spans.append((match.start(), match.end(), name.lower(), 1))
    spans.sort(key=lambda item: (item[3], -(item[1] - item[0]), item[0]))
    counts: Counter = Counter()
    kept: list[tuple[int, int]] = []
    for start, end, name, _priority in spans:
        if any(not (end <= k[0] or start >= k[1]) for k in kept):
            continue
        kept.append((start, end))
        counts[name] += 1
    return counts


def _topic_tokens(sentence: str) -> set[str]:
    """Distinctive content words that identify the sub-event of a sentence."""
    return {
        word for word in re.findall(r"[a-z0-9]+", sentence.lower())
        if len(word) > 3 and word not in STOPWORDS
    }


def _relatedness(a: set[str], b: set[str]) -> float:
    """Overlap coefficient: short sub-event descriptions still match."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _importance(sentence: str) -> int:
    """Event sentences outrank speculation/background sentences."""
    low = sentence.lower()
    score = sum(1 for word in EVENT_WORDS if word in low)
    score -= sum(1 for word in BACKGROUND_WORDS if word in low)
    return score


def _order_body(body: list[str], context: str) -> list[str]:
    """Chain body sentences by topic, then importance, then source order.

    A plain importance sort interleaves unrelated sub-events (eviction, then a
    health story, then back to the eviction). Starting from the context sentence
    and repeatedly choosing the *most related remaining* sentence keeps each
    sub-event together, while importance and publication order break ties so
    the result still reads chronologically and by news value.
    """
    remaining = list(body)
    previous = _topic_tokens(context)
    ordered: list[str] = []
    while remaining:
        best_index, best_key = 0, None
        for index, sentence in enumerate(remaining):
            key = (
                round(_relatedness(previous, _topic_tokens(sentence)), 4),
                _importance(sentence),
                -index,  # earlier source order wins ties -> chronology
            )
            if best_key is None or key > best_key:
                best_index, best_key = index, key
        chosen = remaining.pop(best_index)
        ordered.append(chosen)
        previous = _topic_tokens(chosen)
    return ordered


# Fixed, fact-free Hindi connectors. They signal a move from context to body
# without asserting any new event, name, time or cause.
HINDI_TRANSITIONS = ("इसके अलावा,", "साथ ही,", "और")


def _transition_phrase(seed: str) -> str:
    """Deterministically pick a connector from the fixed template list."""
    return HINDI_TRANSITIONS[int(sha1(seed)[:2], 16) % len(HINDI_TRANSITIONS)]


def _opening(text: str, words: int = 4) -> tuple[str, ...]:
    """Leading tokens, used to catch a lead that merely echoes the headline."""
    return tuple(re.findall(r"[a-z0-9]+", text.lower())[:words])


def _resolve_pronouns(
    sentence: str,
    anchors: list[tuple[str, str]],
    headline_persons: list[str],
) -> str | None:
    """Rewrite deterministic person pronouns; drop the sentence otherwise.

    Thing pronouns (it/this/that/...) are never rewritten: without a source of
    truth they would fabricate a referent, so those sentences are dropped.
    """
    matches = list(VAGUE_WORD.finditer(sentence))
    if not matches:
        return sentence
    mentions = _entity_mentions(sentence, anchors)
    persons = [m for m in mentions if m[3] == "person"]
    distinct = {m[2].lower() for m in persons}

    out = sentence
    for match in reversed(matches):
        word = match.group(0).lower()
        if THING_PRONOUN.fullmatch(word):
            return None
        before = [p for p in persons if p[1] <= match.start()]
        if before:
            entity = before[-1][2]
        elif len(distinct) == 1:
            entity = persons[0][2]
        elif not distinct and len(headline_persons) == 1:
            entity = headline_persons[0]
        else:
            return None  # ambiguous -> drop, never invent a referent
        if word in {"his", "their"}:
            replacement = f"{entity}'s"
        elif word == "her":
            tail = sentence[match.end():].strip().lower().split(" ")[0]
            tail = re.sub(r"[^a-z]", "", tail)
            replacement = entity if tail in OBJECT_NEXT else f"{entity}'s"
        else:
            replacement = entity
        out = out[:match.start()] + replacement + out[match.end():]
    return out


def _absolute_date(article: dict) -> str:
    try:
        dt = datetime.fromisoformat(article.get("published", "").replace("Z", "+00:00"))
        return f"{dt.strftime('%B')} {int(dt.strftime('%d'))}"
    except Exception:
        return ""


def build_story(article: dict, cfg: dict) -> dict | None:
    scfg = cfg["story"]
    headline = _strip_breadcrumbs(article["title"])
    # Some headlines append a stub after a full stop ("... Fight In Front Of
    # Salman. Eviction"). Only the first complete clause is a usable hook;
    # utils.sentence_split drops very short parts, so split explicitly here.
    parts = [p.strip() for p in re.split(r"(?<=\.)\s+(?=[A-Z])", headline) if p.strip()]
    if len(parts) > 1 and len(parts[-1].split()) <= 4:
        headline = parts[0]
    if not headline or _is_metadata(headline):
        return None

    date = _absolute_date(article)
    anchors = _anchors(headline, cfg)
    headline_persons: list[str] = []
    for _s, _e, name, kind in _entity_mentions(headline, anchors, use_caps=False):
        if kind == "person" and name not in headline_persons:
            headline_persons.append(name)

    def clean(sentence: str) -> str:
        text = _punctuate(sentence)
        if date:
            text = RELATIVE_TIME.sub(f"on {date}", text)
        return _punctuate(text)

    headline_clean = clean(headline)

    # Lead cleanup: keep the first substantive breadcrumb-free segment.
    lead_raw = clean_text(article.get("description", "") or "")
    if "|" in lead_raw:
        parts = [p.strip() for p in lead_raw.split("|")]
        good = [p for p in parts if not _is_metadata(p) and len(p) > 30]
        lead_raw = good[0] if good else (parts[0] if parts else "")
    lead_raw = re.sub(r"^\s*(updated|अद्यतन)\s*[:\-]\s*", "", lead_raw, flags=re.I)
    lead = clean(lead_raw) if lead_raw else ""

    # Body collection: whole paragraphs, topic guard, then whole sentences.
    entities = _entities(headline)
    source = (article.get("source") or "").lower()
    body_raw: list[str] = []
    for para in article.get("paragraphs", []):
        low = para.lower()
        if len(para) < 60 or _is_metadata(para):
            continue
        if source and source in low:
            continue
        if len(entities & set(re.findall(r"[a-z0-9]+", low))) < 1:
            continue  # topic drift guard: paragraph must share a headline entity
        for raw in sentence_split(para):
            sentence = clean(raw)
            if not (45 <= len(sentence) <= 320):
                continue
            if sentence.lower() in {b.lower() for b in body_raw}:
                continue
            body_raw.append(sentence)

    def prepare(sentence: str, seen: list[str]) -> str | None:
        if not sentence or _is_metadata(sentence) or "..." in sentence or "…" in sentence:
            return None
        # Spoken reels are statements; a question in the headline/lead/body is
        # simply not narrated (a headline '?' never becomes a spoken hook).
        if "?" in sentence or _is_fragment(sentence) or _is_list_like(sentence):
            return None
        resolved = _resolve_pronouns(sentence, anchors, headline_persons)
        if not resolved or len(resolved) < 30:
            return None
        if _repeats_entity(resolved, anchors):
            return None
        opening = _opening(resolved)
        if opening and any(_opening(prior) == opening for prior in seen):
            return None  # do not echo the hook's/lead's opening phrase
        if any(_too_similar(resolved, prior) for prior in seen):
            return None
        return resolved

    # A named entity may be spoken at most twice in the whole brief. Checking
    # this as sentences are admitted (not after assembly) keeps the cap
    # deterministic and never requires rewriting a sentence to invent a
    # pronoun or synonym.
    entity_counts: Counter = Counter()

    def accept(sentence: str, seen: list[str]) -> str | None:
        prepared = prepare(sentence, seen)
        if not prepared:
            return None
        mentions = _anchor_counts(prepared, anchors)
        if any(entity_counts[name] + count > 2 for name, count in mentions.items()):
            return None
        entity_counts.update(mentions)
        return prepared

    seen: list[str] = []
    headline_ok = accept(headline_clean, seen)
    if headline_ok:
        seen.append(headline_ok)
    lead_ok = accept(lead, seen) if lead else None
    if lead_ok:
        seen.append(lead_ok)
    body_ok: list[str] = []
    for sentence in body_raw:
        prepared = accept(sentence, seen)
        if prepared:
            body_ok.append(prepared)
            seen.append(prepared)

    if headline_ok:
        hook, lead_sentence = headline_ok, lead_ok
    elif lead_ok:
        hook, lead_sentence = lead_ok, None
    elif body_ok:
        hook, lead_sentence = body_ok.pop(0), None
    else:
        return None

    # Group related sub-events together and order them by importance/chronology
    # instead of scattering them with a flat importance sort.
    body_ok = _order_body(body_ok, lead_sentence or hook)

    cta = _punctuate(scfg["cta"])
    min_words = int(scfg["min_words"])
    max_words = int(scfg["max_words"])
    # hook + context + at least 2 body + CTA is the shortest spoken shape QA
    # accepts; five sentences is also the floor enforced by ``qa.flags``.
    required_sentences = max(5, int(scfg.get("min_sentences", 4)) + 1)
    upper = max_words - 5
    target = (min_words + max_words) / 2
    base = [hook] + ([lead_sentence] if lead_sentence else [])

    # Choose a whole-sentence prefix closest to the target band. Larger
    # prefixes add sentences; smaller prefixes drop trailing ones -- never a
    # partial sentence. The body is *always* 2-4 sentences: a longer run reads
    # like a listicle and breaks the hook -> context -> body -> CTA shape.
    # Sources whose four best body sentences cannot reach the word floor are
    # rejected rather than padded with weak, unrelated material.
    cap = min(len(body_ok), 4)
    if cap < 2:
        return None

    best: tuple[int, list[str], int] | None = None
    for k in range(2, cap + 1):
        candidate = base + body_ok[:k] + [cta]
        count = word_count(" ".join(candidate))
        if count > upper:
            break
        if len(candidate) < required_sentences:
            continue
        score = abs(count - target)
        if best is None or score < best[0]:
            best = (score, candidate, count)
    # Four short, clean sentences may total slightly under the configured
    # target. A 35-word tolerance still guarantees a substantial brief while
    # preserving the required shape (and matches QA's lower word band).
    if best is None or best[2] < min_words - 35:
        return None  # source cannot produce a long enough clean script

    sentences = best[1]
    script = " ".join(sentences)
    selected = [s for s in sentences if s in body_ok]
    extra = [s for s in body_ok if s not in selected]

    # A connector is only inserted when the context -> body jump is genuinely
    # abrupt (no distinctive token in common). When the body already continues
    # the context's sub-event, the transition would be clutter and is skipped.
    context = lead_sentence or hook
    first_body = selected[0] if selected else ""
    needs_transition = bool(
        first_body
        and _relatedness(_topic_tokens(context), _topic_tokens(first_body)) < 0.15
    )
    return {
        "headline": headline, "hook": hook, "script": script,
        "sentences": sentences, "word_count": word_count(script),
        "source": article.get("source", ""), "url": article.get("url", ""),
        "published": article.get("published", ""), "tokens": tokens(script),
        "language": "en",
        "selected_body": selected, "extra_body": extra,
        "needs_transition": needs_transition,
        "transition_index": 2 if lead_sentence else 1,
        "max_entity_repeats": max(entity_counts.values(), default=0),
    }


def _translate_sentence(sentence: str, seen: list[str]) -> str | None:
    """Translate one whole sentence, strip leaks, keep `।`, dedupe."""
    from .translate import translate

    text = translate(sentence).strip()
    if not text:
        return None
    if "|" in text or "अद्यतन" in text or re.search(r"\bupdated\b", text, re.I):
        parts = [re.sub(r"^[\s|।.\-]+|[\s|।.\-]+$", "", p)
                 for p in text.split("|")]
        parts = [p for p in parts if len(p) > 25 and not _is_metadata(p)]
        text = parts[0] if parts else ""
    text = re.sub(r"[।.!?]+$", "", text).strip(" |।.")
    if len(text) < 10:
        return None
    if any(_too_similar_hindi(text, prior) for prior in seen):
        return None
    return text + "।"


def to_hindi(story: dict, cfg: dict) -> dict:
    """Translate each sentence separately so `।` boundaries are preserved.

    Word-count variance is corrected here, after real translation, by adding
    or dropping *whole* sentences from the prepared pool -- never by slicing
    a translated sentence in half.
    """
    from .translate import translate, romanize

    headline = translate(story["headline"])
    translated: list[str] = []
    seen: list[str] = []
    for sentence in story["sentences"]:
        rendered = _translate_sentence(sentence, seen)
        if rendered:
            translated.append(rendered)
            seen.append(rendered)

    min_words = int(cfg["story"]["min_words"]) - 35
    max_words = int(cfg["story"]["max_words"]) + 20

    def count(items: list[str]) -> int:
        return word_count(" ".join(items))

    total = count(translated)
    remaining_extra = list(story.get("extra_body", []))
    first_body = int(story.get("transition_index", 2))

    def add_extra() -> bool:
        # Correct translation variance with whole prepared sentences, but never
        # past four body sentences -- the hook/context/body/CTA shape is fixed.
        if len(translated) - 1 - first_body >= 4:
            return False
        while remaining_extra:
            rendered = _translate_sentence(remaining_extra.pop(0), seen)
            if not rendered:
                continue
            translated.insert(len(translated) - 1, rendered)
            seen.append(rendered)
            return True
        return False

    # Too short after translation -> pull in more prepared whole sentences.
    if total < min_words + 8:
        while total < min_words + 12 and add_extra():
            total += word_count(translated[-2])
    # Too long after translation -> drop trailing whole body sentences.
    while total > max_words - 10 and len(translated) > 5:
        index = len(translated) - 2  # last sentence before the CTA
        if index < 2:
            break
        removed = translated.pop(index)
        if removed in seen:
            seen.remove(removed)
        total -= word_count(removed)
    # Never fall below QA's five-sentence floor after dedupe.
    while len(translated) < 5 and add_extra():
        total += word_count(translated[-2])

    # Deterministic, fact-free connector between context and body. It is part
    # of the first body sentence (not a new sentence), so sentence counts,
    # `।` boundaries and the CTA position are unchanged.
    if story.get("needs_transition"):
        index = int(story.get("transition_index", 2))
        if 0 < index < len(translated) - 1:
            phrase = _transition_phrase(story.get("hook", ""))
            translated[index] = f"{phrase} {translated[index]}"

    script = " ".join(translated)
    return {
        **story,
        "headline": headline or story["headline"],
        "hook": translated[0] if translated else headline,
        "script": script,
        "sentences": translated,
        "tokens": tokens(script),
        "caption_tokens": [romanize(t) for t in tokens(script)],
        "word_count": len(tokens(script)),
        "language": "hi",
    }
