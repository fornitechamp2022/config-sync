"""Text-to-speech with native word timings.

Primary provider: edge-tts (free Microsoft Edge neural voices), which returns
WordBoundary / SentenceBoundary events directly — no alignment model, no LLM,
no local download. Word offsets are in 100ns ticks and converted to seconds here.

Hindi narration uses a *single continuous* edge-tts call (``synthesize_natural``)
so the voice never gets the "choppy group TTS" sound. Because a single call can
occasionally fail with ``NoAudioReceived`` (seen in GitHub Actions), the stream
is wrapped in a bounded retry + voice-fallback loop. Exhausting every attempt
raises :class:`VoiceSynthesisError` rather than silently writing empty audio.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

import edge_tts
from edge_tts.exceptions import EdgeTTSException

from .utils import run, tokens

log = logging.getLogger(__name__)

TICKS = 10_000_000.0

# Retries are attempted *per voice*. After the initial try, the three retries
# wait 1s, 2s and 4s respectively (exponential backoff). Tests pass backoff=0.
RETRY_BACKOFF = (1.0, 2.0, 4.0)
DEFAULT_HINDI_FALLBACKS = ["hi-IN-SwaraNeural", "en-IN-NeerjaNeural"]

# Documented pacing profiles. ``rate`` is authoritative when pace_profile is
# configured: it is the single knob that moves a 140-word brief into 45-65s.
# The "news" default is deliberately the historical +8% (real sample: 132
# words -> 59.3s), leaving headroom for a full 140 words (~63s). "calm" is
# slower and reserves a longer visual pause between sentences.
PACE_PROFILES = {
    "news": {
        "rate": "+8%",
        "sentence_pause": 0.30,
        "min_word_gap": 0.04,
        "description": "Brisk news-package delivery; 140 words ~= 55-63s.",
    },
    "calm": {
        "rate": "-4%",
        "sentence_pause": 0.45,
        "min_word_gap": 0.06,
        "description": "Slower, more deliberate delivery; 140 words ~= 65-72s.",
    },
}


class VoiceSynthesisError(RuntimeError):
    """Raised when every TTS attempt (all voices, all retries) fails.

    Callers should skip the affected story rather than emitting a silent or
    truncated voice track.
    """


# ---------------------------------------------------------------------------
# Pacing / profile helpers
# ---------------------------------------------------------------------------

def resolve_pace(vcfg: dict) -> dict:
    """Resolve the effective Hindi pace settings from the voice config.

    Precedence:
      * ``pace_profile`` (news|calm) supplies rate and pause settings;
      * legacy ``hindi_rate`` / ``rate`` are used only when pace_profile is
        absent, keeping older configs working;
      * explicit ``rate_override`` always wins (handy for calibration).

    The returned dict also carries the ordered ``voices`` list (primary first)
    and retry parameters used by :func:`synthesize_natural`.
    """
    name = str(vcfg.get("pace_profile") or "").strip().lower()
    if name not in PACE_PROFILES:
        name = "news" if vcfg.get("pace_profile") else "legacy"
    base = PACE_PROFILES.get(name, PACE_PROFILES["news"])
    pace = dict(base)
    pace["name"] = name

    if not vcfg.get("pace_profile"):
        pace["rate"] = vcfg.get("hindi_rate") or vcfg.get("rate") or base["rate"]
    if vcfg.get("rate_override"):
        pace["rate"] = str(vcfg["rate_override"])

    pace["voice"] = str(vcfg.get("hindi_voice", "hi-IN-MadhurNeural"))
    fallbacks = vcfg.get("hindi_voice_fallbacks", DEFAULT_HINDI_FALLBACKS) or []
    pace["voices"] = [pace["voice"]] + [v for v in fallbacks if v != pace["voice"]]

    backoff = tuple(float(x) for x in vcfg.get("retry_backoff", RETRY_BACKOFF))
    pace["backoff"] = backoff or RETRY_BACKOFF
    pace["retries"] = int(vcfg.get("retry_attempts", len(RETRY_BACKOFF)))

    # Like rate, the profile owns the pause settings when a profile is active;
    # config values below are legacy fallbacks for profile-less configs.
    if vcfg.get("pace_profile"):
        pace["sentence_pause"] = float(base["sentence_pause"])
        pace["min_word_gap"] = float(base["min_word_gap"])
    else:
        pace["sentence_pause"] = float(vcfg.get("sentence_pause", base["sentence_pause"]))
        pace["min_word_gap"] = float(vcfg.get("min_word_gap", base["min_word_gap"]))
    return pace


def _syllable_weight(token: str) -> float:
    """Approximate spoken length of a Hindi token by its Devanagari aksharas.

    A consonant carries one beat; a dependent vowel sign (matra) only lengthens
    it slightly; a consonant joined by virama (cluster) is a half-beat; an
    independent vowel is a full beat. Non-Devanagari characters (digits, Hinglish
    fragments) count as half a beat. Raw character counts badly over-weight
    matra-heavy words, so this keeps highlights paced like natural speech.
    """
    text = re.sub(r"[^\w\u0900-\u097F]", "", token or "")
    if not text:
        return 1.0
    weight = 0.0
    prev_virama = False
    for ch in text:
        code = ord(ch)
        if 0x0915 <= code <= 0x0939:            # consonant (क..ह)
            weight += 0.5 if prev_virama else 1.0
            prev_virama = False
        elif code == 0x094D:                    # virama → next consonant is a cluster
            prev_virama = True
        elif 0x093E <= code <= 0x094C or code in (0x0949, 0x094A, 0x094B, 0x094C, 0x0955):
            weight += 0.05                      # matra: lengthens, not a new beat
            prev_virama = False
        elif 0x0904 <= code <= 0x0914:          # independent vowel (अ..औ)
            weight += 1.0
            prev_virama = False
        elif 0x0900 <= code <= 0x097F:          # anusvara, nukta, digits, etc.
            weight += 0.1
            prev_virama = False
        else:                                   # latin / digit / underscore
            weight += 0.5
            prev_virama = False
    return max(1.0, round(weight, 3))


def _native_norm(text: str) -> str:
    """Lowercase, keeping Devanagari + alphanumerics, for sequence alignment."""
    return "".join(c for c in (text or "").lower() if c.isalnum())


# ---------------------------------------------------------------------------
# Low-level edge-tts streaming (single attempt)
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


async def _synthesize(text: str, voice: str, rate: str) -> tuple[bytes, list[dict]]:
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    audio = bytearray()
    raw: list[dict] = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] == "WordBoundary":
            raw.append({
                "text": chunk["text"],
                "start": round(chunk["offset"] / TICKS, 3),
                "end": round((chunk["offset"] + chunk["duration"]) / TICKS, 3),
            })
    return bytes(audio), raw


async def _synthesize_natural(text: str, voice: str, rate: str) -> tuple[bytes, list[dict]]:
    """One continuous TTS call; hi-IN voices emit SentenceBoundary events."""
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="SentenceBoundary")
    audio = bytearray()
    sentences: list[dict] = []
    async for chunk in comm.stream():
        ctype = chunk.get("type")
        if ctype == "audio":
            audio.extend(chunk.get("data", b""))
        elif ctype in ("SentenceBoundary", "WordBoundary"):
            offset = chunk.get("offset", 0)
            duration = chunk.get("duration", 0)
            sentences.append({
                "text": chunk.get("text", ""),
                "start": round(offset / TICKS, 3),
                "end": round((offset + duration) / TICKS, 3),
            })
    return bytes(audio), sentences


# ---------------------------------------------------------------------------
# Resilient streaming: retries + voice fallback
# ---------------------------------------------------------------------------

def _stream_with_retry(text: str, voices: list[str], rate: str, *,
                       backoff=RETRY_BACKOFF, retries: int = 3,
                       sleep=None) -> tuple[bytes, list[dict], str]:
    """Synthesize with bounded retries per voice, then fall back to other voices.

    A failed attempt is any NoAudioReceived / transient network exception or an
    empty audio payload. Only the voice name and the exception class are logged
    — never the script text, which could contain sensitive news content.
    """
    sleep = sleep or time.sleep
    failures: list[str] = []
    for voice in voices:
        for attempt in range(retries + 1):
            try:
                audio, events = asyncio.run(_synthesize_natural(text, voice, rate))
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:  # noqa: BLE001 - edge-tts/network transients
                name = type(exc).__name__
                failures.append(f"{voice}:{name}")
                log.warning("TTS attempt %d/%d with voice %s failed (%s)",
                            attempt + 1, retries + 1, voice, name)
            else:
                if audio:
                    if failures:
                        log.info("TTS recovered with voice %s after %d failure(s)",
                                 voice, len(failures))
                    return audio, events, voice
                failures.append(f"{voice}:EmptyAudio")
                log.warning("TTS voice %s returned empty audio (attempt %d/%d)",
                            voice, attempt + 1, retries + 1)

            if attempt < retries:
                delay = backoff[min(attempt, len(backoff) - 1)] if backoff else 0.0
                if delay:
                    sleep(delay)

    raise VoiceSynthesisError(
        "Hindi TTS failed after trying voices "
        f"{voices} ({retries + 1} attempts each): " + ", ".join(failures)
    )


# ---------------------------------------------------------------------------
# Timing math: sentence alignment + word distribution
# ---------------------------------------------------------------------------

def align_sentence_spans(events: list[dict], script_sentences: list[str], total: float,
                         min_pause: float = 0.0) -> tuple[list[dict], float, list[float]]:
    """Map TTS SentenceBoundary events onto the deterministic script sentences.

    Returns ``(spans, drift, natural_pauses)``. Spans are 1:1 with
    ``script_sentences`` (each ``end`` extends to the next sentence's start,
    i.e. it includes the natural pause). ``drift`` is the maximum absolute
    difference, in seconds, between a TTS sentence start and the position
    predicted by proportional script-token timing. ``natural_pauses`` records
    the measured gap between each spoken sentence end and the next sentence.
    """
    count = len(script_sentences)
    if count == 0:
        return [], 0.0, []
    if not events:
        # No boundaries at all: fall back to proportional token timing so spans
        # stay non-overlapping and words never bunch up at the same instant.
        script_tokens = [tokens(s) for s in script_sentences]
        weights = [len(toks) or 1 for toks in script_tokens]
        total_weight = sum(weights) or 1
        acc = 0
        fallback = []
        for i, sentence in enumerate(script_sentences):
            start = total * acc / total_weight
            acc += weights[i]
            fallback.append({"index": i, "text": sentence,
                             "start": round(start, 3),
                             "end": round(total * acc / total_weight, 3),
                             "words": weights[i]})
        return fallback, 0.0, []

    script_tokens = [tokens(sentence) for sentence in script_sentences]
    event_tokens = [tokens(event.get("text", "")) for event in events]

    # Align by normalized token identity so punctuation/extra filler in the TTS
    # text does not break the mapping.
    flat_script = [(_native_norm(tok), i)
                   for i, toks in enumerate(script_tokens) for tok in toks]
    flat_event = [(_native_norm(tok), j)
                  for j, toks in enumerate(event_tokens) for tok in toks]
    owner: dict[int, set[int]] = {}
    if flat_script and flat_event:
        matcher = SequenceMatcher(None, [t for t, _ in flat_script],
                                  [t for t, _ in flat_event], autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    owner.setdefault(flat_script[i1 + k][1], set()).add(flat_event[j1 + k][1])

    assigned: list[int] = []
    for i in range(count):
        if owner.get(i):
            assigned.append(min(owner[i]))
        else:
            # No token match: fall back to proportional event position.
            assigned.append(min(int(round(i * len(events) / count)), len(events) - 1))

    # Several script sentences can map to a single (merged) TTS event. Spread
    # them across that event's spoken range so spans remain strictly ordered.
    starts = [0.0] * count
    by_event: dict[int, list[int]] = {}
    for i, event_index in enumerate(assigned):
        by_event.setdefault(event_index, []).append(i)
    for event_index, members in by_event.items():
        event = events[event_index]
        weights = [max(1, len(script_tokens[m])) for m in members]
        total_weight = sum(weights)
        acc = 0
        for member, weight in zip(members, weights):
            starts[member] = event["start"] + (event["end"] - event["start"]) * acc / total_weight
            acc += weight

    previous = -1.0
    for i in range(count):
        if starts[i] <= previous:
            starts[i] = round(previous + 0.02, 3)
        previous = starts[i]

    spans: list[dict] = []
    natural_pauses: list[float] = []
    for i, sentence in enumerate(script_sentences):
        start = starts[i]
        if i + 1 < count:
            end = starts[i + 1]                      # includes the natural pause
            last_idx = max(owner[i]) if owner.get(i) else min(i, len(events) - 1)
            natural_pauses.append(round(max(0.0, end - events[last_idx]["end"]), 3))
        else:
            last_event_end = events[-1]["end"]
            end = total if total > last_event_end else last_event_end
            end = max(end, last_event_end, start + 0.05)
        if min_pause and i + 1 < count:
            # Never shorten speech or overlap the next sentence: only reserve a
            # caption gap when the TTS pause already leaves enough room (no audio
            # surgery, no choppiness).
            end = max(end, min(start + min_pause, starts[i + 1]))
        spans.append({
            "index": i,
            "text": sentence,
            "start": round(start, 3),
            "end": round(max(end, start + 0.05), 3),
            "words": len(script_tokens[i]),
        })

    total_tokens = sum(len(toks) for toks in script_tokens) or 1
    cumulative = 0
    drift = 0.0
    for i, toks in enumerate(script_tokens):
        expected = total * cumulative / total_tokens
        drift = max(drift, abs(spans[i]["start"] - expected))
        cumulative += len(toks)
    return spans, round(drift, 3), natural_pauses


def _distribute_words(sentence_tokens: list[str], start: float, end: float,
                      min_gap: float, total: float) -> list[dict]:
    """Place word times across a sentence using syllable-aware weights."""
    if not sentence_tokens:
        return []
    weights = [_syllable_weight(tok) for tok in sentence_tokens]
    total_weight = sum(weights) or 1.0
    span = max(0.05, end - start)
    words: list[dict] = []
    acc = 0.0
    prev_end = start
    for token, weight in zip(sentence_tokens, weights):
        ws = start + span * acc / total_weight
        acc += weight
        we = start + span * acc / total_weight
        ws = max(ws, prev_end)
        we = max(we, ws + 0.05, prev_end + min_gap)
        words.append({
            "text": token,
            "start": round(max(0.0, ws), 3),
            "end": round(max(ws + 0.05, we), 3),
        })
        prev_end = we

    # Clamp to the real audio duration and keep strictly monotonic endpoints;
    # this is what prevents mid-sentence clipping and overlapping highlights.
    for word in words:
        word["start"] = round(min(word["start"], max(0.0, total - 0.05)), 3)
        word["end"] = round(min(max(word["end"], word["start"] + 0.05), total), 3)
    return words


# ---------------------------------------------------------------------------
# Public synthesis entry points
# ---------------------------------------------------------------------------

def synthesize_natural(story: dict, cfg: dict, workdir: str | Path, *,
                       backoff=None, retries=None, sleep=None) -> dict:
    """Natural, single-pass Hindi narration with sentence-anchored timings.

    Retries transient edge-tts failures per voice and walks the configured
    fallback voices. Timing is derived from SentenceBoundary events; word times
    inside each sentence are distributed by Devanagari syllable weight.
    """
    from .translate import romanize

    workdir = Path(workdir)
    audio_dir = workdir / "assets" / "voice"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "voice.mp3"

    pace = resolve_pace(cfg["voice"])
    if backoff is None:
        backoff = pace["backoff"]
    if retries is None:
        retries = pace["retries"]

    audio, events, used_voice = _stream_with_retry(
        story["script"], pace["voices"], pace["rate"],
        backoff=backoff, retries=retries, sleep=sleep,
    )
    audio_path.write_bytes(audio)

    # ffprobe is authoritative in production; tests can patch duration_of.
    total = duration_of(audio_path)
    if not total:
        total = round(max((e["end"] for e in events), default=0.0) + 0.2, 3)

    script_sentences = story.get("sentences") or []
    if not script_sentences:
        script_sentences = [story["script"]] if story.get("script") else []

    spans, drift, natural_pauses = align_sentence_spans(
        events, script_sentences, total,
        min_pause=pace.get("sentence_pause", 0.0),
    )

    words: list[dict] = []
    for span, sentence in zip(spans, script_sentences):
        sentence_tokens = tokens(sentence)
        if not sentence_tokens:
            continue
        # Words stop at the spoken end of the sentence; the remaining gap to
        # the next span start is the natural pause (honoured by caption grouping).
        event_end = span["end"]
        for word in _distribute_words(sentence_tokens, span["start"], event_end,
                                      pace["min_word_gap"], total):
            word["id"] = f"w{len(words)}"
            word["text"] = romanize(word["text"])
            words.append(word)

    # Global safety net: merge/split boundary events must never produce an
    # overlapping or out-of-order highlight at a sentence junction.
    for previous, current in zip(words, words[1:]):
        if current["start"] < previous["end"]:
            current["start"] = round(previous["end"], 3)
        if current["end"] < current["start"] + 0.05:
            current["end"] = round(min(current["start"] + 0.05, total), 3)

    (workdir / "words.json").write_text(
        json.dumps(words, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "audio": "assets/voice/voice.mp3",
        "words": words,
        "duration": total,
        "sentence_spans": spans,
        "max_sentence_drift": drift,
        "natural_pauses": natural_pauses,
        "voice": used_voice,
        "pace_profile": pace["name"],
        "rate": pace["rate"],
    }


async def _audio_only(text: str, voice: str, rate: str) -> bytes:
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="SentenceBoundary")
    audio = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


def synthesize_grouped(story: dict, cfg: dict, workdir: str | Path,
                       groups: list[list[str]]) -> dict:
    """Hindi path: synthesise each caption group separately (legacy fallback).

    Native WordBoundary events are unreliable for hi-IN voices, so each group
    becomes its own audio file. Group boundaries are therefore sample-accurate;
    word highlights inside a group are distributed by syllable-aware weights.
    """
    import tempfile
    from .captions import group_plain

    workdir = Path(workdir)
    audio_dir = workdir / "assets" / "voice"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "voice.mp3"

    vcfg = cfg["voice"]
    pace = resolve_pace(vcfg)
    voice, rate = vcfg["voice"], vcfg.get("rate", "+0%")
    if story.get("language") == "hi":
        voice, rate = pace["voice"], pace["rate"]

    if not groups:
        groups = group_plain(story["tokens"], cfg)

    tmp = Path(tempfile.mkdtemp(prefix="reelgrp_", dir=str(workdir)))
    pieces: list[Path] = []
    for i, group in enumerate(groups):
        text = " ".join(group)
        audio = asyncio.run(_audio_only(text, voice, rate))
        if not audio:
            continue
        path = tmp / f"g{i:03d}.mp3"
        path.write_bytes(audio)
        pieces.append(path)

    if not pieces:
        return {"audio": "", "words": [], "groups": [], "duration": 0.0}

    gap_path = tmp / "gap.mp3"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=24000:cl=mono", "-t", "0.10",
         "-c:a", "libmp3lame", "-q:a", "9", str(gap_path)])

    concat_list = tmp / "list.txt"
    lines = []
    for i, piece in enumerate(pieces):
        lines.append(f"file '{piece.as_posix()}'")
        if i != len(pieces) - 1:
            lines.append(f"file '{gap_path.as_posix()}'")
    concat_list.write_text("\n".join(lines), encoding="utf-8")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(concat_list), "-c", "copy", str(audio_path)])

    gap = 0.10
    words: list[dict] = []
    built_groups: list[dict] = []
    cursor = 0.0
    wi = 0
    for piece, tokens_in_group in zip(pieces, groups):
        dur = duration_of(piece)
        start, end = round(cursor, 3), round(cursor + dur, 3)
        weights = [_syllable_weight(t) for t in tokens_in_group]
        total = sum(weights) or 1
        acc = 0.0
        group_words = []
        for token, weight in zip(tokens_in_group, weights):
            ws = round(start + dur * acc / total, 3)
            acc += weight
            we = round(start + dur * acc / total, 3)
            word = {"id": f"w{wi}", "text": token, "start": ws,
                    "end": max(ws + 0.08, we)}
            words.append(word)
            group_words.append(word)
            wi += 1
        built_groups.append({
            "id": f"cap{len(built_groups)}", "words": group_words,
            "text": " ".join(tokens_in_group), "start": start, "end": end,
        })
        cursor = end + gap

    (workdir / "words.json").write_text(json.dumps(words, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return {
        "audio": "assets/voice/voice.mp3", "words": words, "groups": built_groups,
        "duration": duration_of(audio_path),
    }


def duration_of(path: str | Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    try:
        return round(float(out), 3)
    except ValueError:
        return 0.0


def _align(script: str, boundaries: list[dict]) -> list[dict]:
    """Attach a time to every script token, matching boundaries by position."""
    script_tokens = tokens(script)
    norm_script = [_norm(t) for t in script_tokens]
    norm_bound = [_norm(w["text"]) for w in boundaries]

    mapping: dict[int, int] = {}
    if len(norm_script) == len(norm_bound):
        mapping = {i: i for i in range(len(norm_script))}
    else:
        matcher = SequenceMatcher(None, norm_script, norm_bound, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    mapping[i1 + k] = j1 + k

    words: list[dict] = []
    for i, token in enumerate(script_tokens):
        j = mapping.get(i)
        if j is not None and j < len(boundaries):
            start, end = boundaries[j]["start"], boundaries[j]["end"]
        else:
            prev_t = next((mapping[k] for k in range(i - 1, -1, -1) if k in mapping), None)
            next_t = next((mapping[k] for k in range(i + 1, len(norm_script)) if k in mapping), None)
            if prev_t is not None and next_t is not None and next_t == prev_t + 1:
                start = boundaries[prev_t]["end"]
                end = boundaries[next_t]["start"]
            elif prev_t is not None:
                start = boundaries[prev_t]["end"]
                end = start + 0.12
            elif next_t is not None:
                end = boundaries[next_t]["start"]
                start = max(0.0, end - 0.12)
            else:
                start = round(i * 0.12, 3)
                end = round(start + 0.12, 3)
            if end <= start:
                end = start + 0.08
        words.append({
            "id": f"w{i}",
            "text": token,
            "start": round(max(0.0, start), 3),
            "end": round(max(start + 0.02, end), 3),
        })
    return words


def synthesize(story: dict, cfg: dict, workdir: str | Path) -> dict:
    """Write voice.mp3 and words.json; return their paths plus duration (English)."""
    workdir = Path(workdir)
    audio_dir = workdir / "assets" / "voice"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "voice.mp3"
    words_path = workdir / "words.json"

    vcfg = cfg["voice"]
    if vcfg.get("provider", "edge") == "kokoro":
        text_path = workdir / "narration.txt"
        text_path.write_text(story["script"], encoding="utf-8")
        cli = os.environ.get("HYPERFRAMES_CLI", vcfg.get("kokoro_cli", "hyperframes"))
        run(["node", cli, "tts", str(text_path), "-o", str(audio_path),
             "-v", vcfg["kokoro_voice"]])
        boundaries = []
    else:
        audio, boundaries = asyncio.run(_synthesize(
            story["script"], vcfg["voice"], vcfg.get("rate", "+0%")))
        audio_path.write_bytes(audio)

    words = _align(story["script"], boundaries)
    words_path.write_text(json.dumps(words, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "audio": str(audio_path.relative_to(workdir)).replace("\\", "/"),
        "words_path": str(words_path.relative_to(workdir)).replace("\\", "/"),
        "words": words,
        "duration": duration_of(audio_path),
    }
