"""Caption grouping, line layout and GSAP-ready highlight timelines.

The public API stays backwards compatible with the rest of the pipeline::

    group_plain(words, cfg) -> list[list[str]]
    group_words(words, cfg) -> list[dict]  # id, text, words, start, end (+ extras)
    write_captions(groups, workdir) -> Path

New helpers added for iteration 3::

    mark_emphasis(words, cfg) -> list[dict]
    split_lines(words, max_chars) -> list[list[int]]
    build_caption_timeline(groups, cfg) -> dict
    render_timeline_js(timeline) -> str

Design notes
------------
* A word whose text ends in ``. ! ? ।`` terminates a sentence.  A caption group
  is never allowed to contain words from two different sentences.
* Grouping prefers semantic breaks (pauses and conjunctions) and never leaves
  an orphan single-word chunk when it can be merged inside the same sentence.
* Line wrapping is *metadata only*: ``group["lines"]`` holds word indices per
  visual line.  Word spans stay intact so consumers can render one span per
  word while laying words out on two lines.
* Emphasis is metadata only -- it never changes the yellow/white highlight.
* ``build_caption_timeline`` produces a flat, time-sorted list of explicit GSAP
  events plus validation errors, so ``project.py`` can consume a clean data
  structure instead of re-deriving highlight times.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

# A sentence ends on a danda (ITrans '।', romanized to '.') or a terminating mark.
_SENTENCE_END_RE = re.compile(r"[.!?।][\"')\]]*$")
_DIGIT_RE = re.compile(r"\d")
_PUNCT_RE = re.compile(r"^[\s\"'`()\[\]{}.,!?।:;]+|[\s\"'`()\[\]{}.,!?।:;]+$")

# Smallest gap we keep between a word turning yellow and turning white (seconds).
# Mirrors project.py's `ws + 0.05` floor so both renderers agree.
_MIN_HIGHLIGHT_GAP = 0.05

# Default soft wrap width for Hinglish (roman) captions, in characters.
_DEFAULT_MAX_LINE_CHARS = 24

# Hinglish conjunctions / connectives that make good *leading* words of a new
# group.  Overridable via cfg['captions']['conjunction_words'].
_DEFAULT_CONJUNCTIONS = frozenset({
    "aur", "and", "ya", "or", "but", "kyunki", "kyoki", "kyonki", "kyun",
    "kyon", "phir", "fir", "tab", "jab", "jahan", "jo", "ki", "keh",
    "isse", "usse", "to", "toh", "jaise", "agar",
})

# Known named entities worth flagging as emphasized (metadata only).
# Combined with cfg['topic']['keywords'] tokens at runtime.
_DEFAULT_ENTITY_WORDS = frozenset({
    "salman", "khan", "bigg", "boss", "bollywood", "india", "indian",
    "mumbai", "delhi", "katrina", "kaif", "shahrukh", "srk", "aamir",
})

# Stable ordering when several events share the same timestamp: show the group,
# retire the previous word, light the next word, then hide at the very end.
_EVENT_ORDER = {"group_show": 0, "word_off": 1, "word_on": 2, "group_hide": 3}


# --------------------------------------------------------------------------- #
# Small text helpers
# --------------------------------------------------------------------------- #
def _has_pause(text: str) -> bool:
    """True when a token ends a sentence (kept for backwards compatibility)."""
    return bool(re.search(r"[.!?।]$", str(text).strip()))


def _is_sentence_end(text: str) -> bool:
    """Slightly more tolerant sentence-end test (allows trailing quotes)."""
    return bool(_SENTENCE_END_RE.search(str(text).strip()))


def _strip_punct(text: str) -> str:
    return _PUNCT_RE.sub("", str(text)).lower()


def _ends_sentence(group: dict | list) -> bool:
    if not group:
        return False
    if isinstance(group, dict):
        words = group.get("words", [])
        return bool(words) and _has_pause(str(words[-1].get("text", "")))
    return _has_pause(str(group[-1]))


def _conjunction_set(cfg: dict) -> frozenset[str]:
    ccfg = cfg.get("captions", {}) if isinstance(cfg, dict) else {}
    extra = ccfg.get("conjunction_words")
    words = set(_DEFAULT_CONJUNCTIONS)
    if extra:
        words.update(_strip_punct(w) for w in extra)
    return frozenset(words)


def _entity_set(cfg: dict) -> frozenset[str]:
    words = set(_DEFAULT_ENTITY_WORDS)
    topic = cfg.get("topic", {}) if isinstance(cfg, dict) else {}
    for keyword in topic.get("keywords", []) or []:
        words.update(_strip_punct(part) for part in str(keyword).split())
    ccfg = cfg.get("captions", {}) if isinstance(cfg, dict) else {}
    for extra in ccfg.get("emphasis_words", []) or []:
        words.add(_strip_punct(extra))
    return frozenset(w for w in words if w)


def _is_conjunction(word: dict | str, conjunctions: frozenset[str]) -> bool:
    text = word.get("text", "") if isinstance(word, dict) else word
    return _strip_punct(text) in conjunctions


# --------------------------------------------------------------------------- #
# Emphasis + line layout (metadata only; never touches highlight colours)
# --------------------------------------------------------------------------- #
def mark_emphasis(words: list[dict], cfg: dict) -> list[dict]:
    """Return copies of *words* with a boolean ``emphasis`` flag added.

    A word is emphasized when it contains a digit (e.g. ``20.``) or is a known
    entity name from the config.  The flag is informational only -- GSAP still
    colours every word yellow -> white exactly as before.
    """
    entities = _entity_set(cfg)
    return [_enrich_word(w, entities, True) for w in words]


def _enrich_word(word: dict, entities: frozenset[str], enabled: bool) -> dict:
    out = dict(word)  # never mutate caller data
    text = str(out.get("text", ""))
    out["emphasis"] = bool(
        enabled and (_DIGIT_RE.search(text) or _strip_punct(text) in entities)
    )
    return out


def split_lines(words: list[dict], max_chars: int) -> list[list[int]]:
    """Split word indices into visual lines of at most *max_chars* characters.

    Wrapping happens only between words: every word stays a single, intact
    span.  A word longer than *max_chars* simply occupies its own line.
    """
    max_chars = max(1, int(max_chars))
    lines: list[list[int]] = []
    current: list[int] = []
    length = 0
    for i, word in enumerate(words):
        text = str(word.get("text", ""))
        added = len(text) + (1 if current else 0)  # +1 for the joining space
        if current and length + added > max_chars:
            lines.append(current)
            current, length, added = [], 0, len(text)
        current.append(i)
        length += added
    if current:
        lines.append(current)
    return lines


# --------------------------------------------------------------------------- #
# Grouping
# --------------------------------------------------------------------------- #
def _merge_orphans(chunks: list[list[dict]], min_w: int,
                   max_w: int) -> list[list[dict]]:
    """Resolve single-word chunks without ever leaving a 1-word group behind.

    All chunks belong to the same sentence, so merging/rebalancing here never
    crosses a sentence boundary.  When the previous chunk is already full we
    rebalance its trailing words into the orphan (e.g. ``[5 words] + [1]``
    becomes ``[4 words] + [2]``) instead of emitting an unacceptable singleton.
    """
    if len(chunks) <= 1:
        return chunks

    merged: list[list[dict]] = []
    for chunk in chunks:
        if len(chunk) == 1 and merged and len(merged[-1]) < max_w:
            merged[-1] = merged[-1] + chunk
        else:
            merged.append(chunk)

    i = 0
    while i < len(merged):
        if len(merged[i]) != 1:
            i += 1
            continue
        orphan = merged[i]
        # Prefer folding a leading orphan into the following chunk.
        if i + 1 < len(merged) and len(merged[i + 1]) < max_w:
            merged[i + 1] = orphan + merged[i + 1]
            del merged[i]
            continue
        # Otherwise rebalance from the previous chunk (keeps both >= min_w).
        if i > 0 and len(merged[i - 1]) > min_w:
            prev = merged[i - 1]
            take = max(min_w - 1, len(prev) - (max_w - 1))
            take = min(take, len(prev) - min_w)
            if take > 0:
                moved = prev[-take:]
                merged[i - 1] = prev[:-take]
                merged[i] = moved + orphan
        i += 1
    return [chunk for chunk in merged if chunk]


def _group_sentence(words: list[dict], min_w: int, max_w: int, pause: float,
                    conjunctions: frozenset[str]) -> list[list[dict]]:
    """Group the words of a *single* sentence (no boundary crossing possible)."""
    if not words:
        return []
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        if current:
            gap = float(word["start"]) - float(current[-1]["end"])
            should_break = (
                len(current) >= max_w
                or gap >= pause
                or (len(current) >= min_w and _is_conjunction(word, conjunctions))
            )
            if should_break:
                chunks.append(current)
                current = []
        current.append(word)
    if current:
        chunks.append(current)
    return _merge_orphans(chunks, min_w, max_w)


def group_words(words: list[dict], cfg: dict) -> list[dict]:
    """Group timed word tokens into karaoke caption groups.

    Returns dicts with the original keys ``id, text, words, start, end`` plus:

    * ``sentence``  -- zero-based sentence index (groups never mix sentences)
    * ``lines``     -- word indices per visual line (soft <=24 char wrap)
    * ``line_texts``-- the same split rendered as strings
    * each word dict additionally carries ``emphasis`` (metadata only)
    """
    ccfg = cfg.get("captions", {}) if isinstance(cfg, dict) else {}
    min_w = max(1, int(ccfg.get("min_words", 2)))
    max_w = max(min_w, int(ccfg.get("max_words", 5)))
    pause = float(ccfg.get("pause_break", 0.16))
    max_chars = int(ccfg.get("max_line_chars", _DEFAULT_MAX_LINE_CHARS))
    emphasis_on = bool(ccfg.get("emphasis", True))
    conjunctions = _conjunction_set(cfg)
    entities = _entity_set(cfg)

    enriched = [_enrich_word(w, entities, emphasis_on) for w in words]

    # 1. Hard sentence segmentation -- the invariant that no group crosses a
    #    sentence boundary is enforced structurally, not by post-processing.
    sentences: list[list[dict]] = []
    current: list[dict] = []
    for word in enriched:
        current.append(word)
        if _is_sentence_end(str(word.get("text", ""))):
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)

    # 2. Group each sentence independently.
    groups: list[dict] = []
    for sentence_id, sentence in enumerate(sentences):
        for chunk in _group_sentence(sentence, min_w, max_w, pause, conjunctions):
            lines = split_lines(chunk, max_chars)
            groups.append({
                "id": "",  # assigned once the final order is known
                "words": chunk,
                "text": " ".join(str(w.get("text", "")) for w in chunk),
                "start": round(float(chunk[0]["start"]), 3),
                "end": round(float(chunk[-1]["end"]), 3),
                "sentence": sentence_id,
                "lines": lines,
                "line_texts": [
                    " ".join(str(chunk[i].get("text", "")) for i in line)
                    for line in lines
                ],
            })

    for i, group in enumerate(groups):
        group["id"] = f"cap{i}"
    return [g for g in groups if g["words"]]


def group_plain(words: list[str], cfg: dict) -> list[list[str]]:
    """Group plain tokens (no timings) -- used before Hindi TTS synthesis."""
    ccfg = cfg.get("captions", {}) if isinstance(cfg, dict) else {}
    min_w = max(1, int(ccfg.get("min_words", 2)))
    max_w = max(min_w, int(ccfg.get("max_words", 5)))
    conjunctions = _conjunction_set(cfg)

    groups: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if current and (
            len(current) >= max_w
            or _has_pause(current[-1])
            or (len(current) >= min_w and _is_conjunction(word, conjunctions))
        ):
            groups.append(current)
            current = []
        current.append(word)
        if len(current) >= min_w and (_has_pause(word) or len(current) >= max_w):
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    merged: list[list[str]] = []
    for group in groups:
        if (len(group) == 1 and merged and len(merged[-1]) < max_w
                and not _ends_sentence(merged[-1])):
            merged[-1] = merged[-1] + group
        else:
            merged.append(group)
    return merged


# --------------------------------------------------------------------------- #
# GSAP timeline
# --------------------------------------------------------------------------- #
def build_caption_timeline(groups: list[dict], cfg: dict) -> dict:
    """Build an explicit, validated, GSAP-ready caption timeline.

    Returns a dict with:

    * ``groups`` -- deep copies of the input groups, each gaining
      ``word_timings`` (canonical on/off seconds) and ``events`` (this group's
      events).  All original keys -- ``id, text, words, start, end`` -- remain.
    * ``events`` -- one flat list of event dicts sorted by time.  Each event has
      ``kind`` (``group_show | group_hide | word_on | word_off``), ``selector``
      (CSS id selector matching ``project.py``), ``time`` (seconds), ``props``
      (GSAP properties) and ``group_id``/``word_index`` context.
    * ``valid``  -- True when no validation error was found.
    * ``errors`` -- human-readable validation errors (empty when valid).

    Timing is normalized so every word satisfies ``off > on >= 0`` and
    consecutive words never overlap (``off_i <= on_i+1``), which makes the
    yellow -> white hand-off strictly monotonic.
    """
    ccfg = cfg.get("captions", {}) if isinstance(cfg, dict) else {}
    highlight = ccfg.get("highlight_color", "#FFE600")
    text_color = ccfg.get("text_color", "#FFFFFF")
    gap = float(ccfg.get("min_highlight_gap", _MIN_HIGHLIGHT_GAP))
    if gap <= 0:
        gap = _MIN_HIGHLIGHT_GAP

    timeline_groups: list[dict] = []
    flat_events: list[dict] = []
    errors: list[str] = []

    for gi, raw in enumerate(groups):
        group = copy.deepcopy(raw)
        gid = str(group.get("id", f"cap{gi}"))
        words = group.get("words", [])
        try:
            gstart = round(float(group["start"]), 3)
            gend = round(float(group["end"]), 3)
        except (KeyError, TypeError, ValueError):
            errors.append(f"{gid}: missing or invalid start/end")
            continue
        if gend < gstart:
            errors.append(f"{gid}: group end {gend} < start {gstart}")
            gend = gstart
        gstart = max(0.0, gstart)
        gend = max(gstart, gend)

        # Canonical, clamped highlight timings for this group.
        timings: list[dict] = []
        cursor = gstart
        for wi, word in enumerate(words):
            try:
                ws = round(float(word["start"]), 3)
                we = round(float(word["end"]), 3)
            except (KeyError, TypeError, ValueError):
                errors.append(f"{gid}/w{wi}: missing or invalid timing")
                continue
            if we < ws:
                errors.append(f"{gid}/w{wi}: word end {we} < start {ws}")
                we = ws
            ws = max(ws, gstart, cursor)
            we = round(max(we, ws + gap), 3)
            timings.append({"index": wi, "on": ws, "off": we})
            cursor = we

        # Monotonic hand-off: previous word goes white before next goes yellow.
        for i in range(len(timings) - 1):
            if timings[i]["off"] > timings[i + 1]["on"]:
                timings[i + 1]["on"] = timings[i]["off"]
                if timings[i + 1]["off"] <= timings[i + 1]["on"]:
                    timings[i + 1]["off"] = round(timings[i + 1]["on"] + gap, 3)

        group_events: list[dict] = [
            {"kind": "group_show", "selector": f"#{gid}_inner", "time": gstart,
             "props": {"opacity": 1, "visibility": "visible"}, "group_id": gid},
            {"kind": "group_hide", "selector": f"#{gid}_inner", "time": gend,
             "props": {"opacity": 0, "visibility": "hidden"}, "group_id": gid},
        ]
        for timing in timings:
            selector = f"#{gid}_w{timing['index']}"
            group_events.append({
                "kind": "word_on", "selector": selector, "time": timing["on"],
                "props": {"color": highlight}, "group_id": gid,
                "word_index": timing["index"],
            })
            group_events.append({
                "kind": "word_off", "selector": selector, "time": timing["off"],
                "props": {"color": text_color}, "group_id": gid,
                "word_index": timing["index"],
            })

        group["word_timings"] = timings
        group["events"] = group_events
        timeline_groups.append(group)
        flat_events.extend(group_events)

    flat_events.sort(key=lambda e: (e["time"], _EVENT_ORDER.get(e["kind"], 99)))

    # Final validation of the normalized result.
    for event in flat_events:
        if event["time"] < 0:
            errors.append(f"{event['group_id']}: negative event time {event['time']}")
    for group in timeline_groups:
        for timing in group.get("word_timings", []):
            if timing["off"] <= timing["on"]:
                errors.append(
                    f"{group.get('id')}/w{timing['index']}: "
                    f"highlight off {timing['off']} not after on {timing['on']}"
                )

    return {
        "groups": timeline_groups,
        "events": flat_events,
        "valid": not errors,
        "errors": errors,
    }


def render_timeline_js(timeline: dict) -> str:
    """Serialize a timeline's events into ``gsap.set`` JS statements."""
    lines = []
    for event in timeline.get("events", []):
        props = ", ".join(
            f"{json.dumps(k)}:{json.dumps(v)}" for k, v in event["props"].items()
        )
        lines.append(
            f"tl.set({json.dumps(event['selector'])}, {{{props}}}, {event['time']});"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def write_captions(groups: list[dict], workdir: str | Path) -> Path:
    path = Path(workdir) / "captions.json"
    path.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
