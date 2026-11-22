"""Caption grouping: short, perfectly timed word groups for karaoke highlight."""
from __future__ import annotations

import json
import re
from pathlib import Path


def _has_pause(text: str) -> bool:
    return bool(re.search(r"[.!?।]$", text.strip()))


def _ends_sentence(group: dict | list) -> bool:
    if not group:
        return False
    if isinstance(group, dict):
        words = group.get("words", [])
        return bool(words) and _has_pause(str(words[-1].get("text", "")))
    return _has_pause(str(group[-1]))


def group_plain(words: list[str], cfg: dict) -> list[list[str]]:
    """Group plain tokens (no timings) — used before Hindi TTS synthesis."""
    ccfg = cfg["captions"]
    min_w, max_w = int(ccfg["min_words"]), int(ccfg["max_words"])
    groups: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if current and (len(current) >= max_w or _has_pause(current[-1])):
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
        if len(group) == 1 and merged and not _ends_sentence(merged[-1]) and len(merged[-1]) < max_w:
            merged[-1] += group
        else:
            merged.append(group)
    return merged


def group_words(words: list[dict], cfg: dict) -> list[dict]:
    ccfg = cfg["captions"]
    min_w = int(ccfg["min_words"])
    max_w = int(ccfg["max_words"])
    pause = float(ccfg["pause_break"])

    groups: list[dict] = []
    current: list[dict] = []

    def flush():
        nonlocal current
        if not current:
            return
        groups.append({
            "id": f"cap{len(groups)}",
            "words": current,
            "text": " ".join(w["text"] for w in current),
            "start": current[0]["start"],
            "end": current[-1]["end"],
        })
        current = []

    for word in words:
        if current:
            gap = word["start"] - current[-1]["end"]
            if gap >= pause or len(current) >= max_w or _has_pause(current[-1]["text"]):
                flush()
        current.append(word)
        if len(current) >= min_w and (_has_pause(word["text"]) or len(current) >= max_w):
            flush()
    flush()

    # Merge one-word groups into a neighbour (except a deliberate first word).
    merged: list[dict] = []
    for group in groups:
        if len(group["words"]) == 1 and merged:
            prev = merged[-1]
            if not _ends_sentence(prev) and len(prev["words"]) < max_w:
                prev["words"] += group["words"]
                prev["text"] += " " + group["text"]
                prev["end"] = group["end"]
                continue
        merged.append(group)

    for i, group in enumerate(merged):
        group["id"] = f"cap{i}"
    return [g for g in merged if g["words"]]


def write_captions(groups: list[dict], workdir: str | Path) -> Path:
    path = Path(workdir) / "captions.json"
    path.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
