"""Text-to-speech with native word timings.

Primary provider: edge-tts (free Microsoft Edge neural voices), which returns
WordBoundary events directly — no alignment model, no LLM, no local download.
Word offsets are in 100ns ticks and converted to seconds here.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path

import edge_tts

from .utils import run, tokens


TICKS = 10_000_000.0


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


def _align(script: str, boundaries: list[dict]) -> list[dict]:
    """Attach a time to every script token, matching boundaries by position.

    SequenceMatcher handles punctuation/extra tokens; unmatched tokens are
    interpolated between their neighbours so every caption word has a time.
    """
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


async def _audio_only(text: str, voice: str, rate: str) -> bytes:
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="SentenceBoundary")
    audio = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
    return bytes(audio)


def synthesize_grouped(story: dict, cfg: dict, workdir: str | Path,
                       groups: list[list[str]]) -> dict:
    """Hindi path: synthesise each caption group separately.

    Native WordBoundary events are unreliable for hi-IN voices, so each group
    becomes its own audio file. Group boundaries are therefore sample-accurate;
    word highlights inside a group are distributed by character length.
    """
    import math
    import tempfile
    from .captions import group_plain

    workdir = Path(workdir)
    audio_dir = workdir / "assets" / "voice"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "voice.mp3"

    vcfg = cfg["voice"]
    voice, rate = vcfg["voice"], vcfg.get("rate", "+0%")
    if story.get("language") == "hi":
        voice = vcfg.get("hindi_voice", "hi-IN-MadhurNeural")
        rate = vcfg.get("hindi_rate", rate)

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
    for piece, tokens in zip(pieces, groups):
        dur = duration_of(piece)
        start, end = round(cursor, 3), round(cursor + dur, 3)
        weights = [max(1, len(re.sub(r"[^0-9A-Za-z\u0900-\u097F]", "", t))) for t in tokens]
        total = sum(weights) or 1
        acc = 0.0
        group_words = []
        for token, weight in zip(tokens, weights):
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
            "text": " ".join(tokens), "start": start, "end": end,
        })
        cursor = end + gap

    (workdir / "words.json").write_text(json.dumps(words, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return {
        "audio": "assets/voice/voice.mp3", "words": words, "groups": built_groups,
        "duration": duration_of(audio_path),
    }


async def _synthesize_natural(text: str, voice: str, rate: str) -> tuple[bytes, list[dict]]:
    """One continuous TTS call; Hindi voices emit SentenceBoundary events."""
    comm = edge_tts.Communicate(text, voice, rate=rate, boundary="SentenceBoundary")
    audio = bytearray()
    sentences: list[dict] = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] in ("SentenceBoundary", "WordBoundary"):
            sentences.append({
                "text": chunk["text"],
                "start": round(chunk["offset"] / TICKS, 3),
            })
    return bytes(audio), sentences


def synthesize_natural(story: dict, cfg: dict, workdir: str | Path) -> dict:
    """Natural, single-pass narration with sentence-anchored caption timings."""
    from .translate import romanize

    workdir = Path(workdir)
    audio_dir = workdir / "assets" / "voice"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "voice.mp3"

    vcfg = cfg["voice"]
    voice = vcfg.get("hindi_voice", "hi-IN-MadhurNeural")
    rate = vcfg.get("hindi_rate", "+10%")
    audio, sentences = asyncio.run(_synthesize_natural(story["script"], voice, rate))
    audio_path.write_bytes(audio)
    total = duration_of(audio_path)

    words: list[dict] = []
    for i, sentence in enumerate(sentences):
        start = sentence["start"]
        end = sentences[i + 1]["start"] if i + 1 < len(sentences) else total
        sentence_tokens = tokens(sentence["text"])
        if not sentence_tokens:
            continue
        weights = [max(1, len(_norm(t))) for t in sentence_tokens]
        span = max(0.05, end - start)
        acc = 0
        for token, weight in zip(sentence_tokens, weights):
            ws = round(start + span * acc / sum(weights), 3)
            acc += weight
            we = round(start + span * acc / sum(weights), 3)
            words.append({
                "id": f"w{len(words)}",
                "text": romanize(token),
                "start": ws,
                "end": max(ws + 0.05, we),
            })

    (workdir / "words.json").write_text(json.dumps(words, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
    return {"audio": "assets/voice/voice.mp3", "words": words, "duration": total}


def duration_of(path: str | Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(path)]).stdout.strip()
    try:
        return round(float(out), 3)
    except ValueError:
        return 0.0


def synthesize(story: dict, cfg: dict, workdir: str | Path) -> dict:
    """Write voice.mp3 and words.json; return their paths plus duration."""
    workdir = Path(workdir)
    audio_dir = workdir / "assets" / "voice"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / "voice.mp3"
    words_path = workdir / "words.json"

    vcfg = cfg["voice"]
    if vcfg.get("provider", "edge") == "kokoro":
        # Offline fallback. Kokoro has no native word timings; use the bundled
        # sentence-anchored CLI route and the same positional alignment is not
        # possible here, so edge-tts remains the default for caption accuracy.
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
