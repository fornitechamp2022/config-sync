#!/usr/bin/env python3
"""Bollywood Reel Generator — deterministic, no LLM.

Examples:
  python generate.py --check                 # discover + gate + QA flag report
  python generate.py --run --count 1         # full generate + render
  python generate.py --run --count 1 --no-publish
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from pipeline.articles import enrich_and_gate
from pipeline.captions import group_words, write_captions
from pipeline.images import prepare_stills
from pipeline.project import write_project
from pipeline.qa import flags as qa_flags
from pipeline.sources import discover
from pipeline.state import State
from pipeline.story import build_story, to_hindi
from pipeline.utils import load_config, run, slugify, similarity, ensure_dir, tokens
from pipeline.voice import synthesize, synthesize_natural

DEFAULT_CLI = r"C:\Users\MASHU\.agents\skills\hyperframes\scripts\plugin-cli.mjs"

# QA flags that hard-reject a script *before* any TTS, image work or rendering.
# Everything else (vague_pronoun, time_relative, clickbait, repeated_sentence,
# latin_in_hindi) is logged as a warning and never blocks a story on its own.
CRITICAL_FLAGS = frozenset({
    "no_sentence_punctuation",
    "truncated_lead",
    "metadata_leak",
    "too_few_sentences",
    "question_headline",
    "word_count_out_of_range",
})


def cli_command(args: list[str]) -> list[str]:
    cli = os.environ.get("HYPERFRAMES_CLI", DEFAULT_CLI)
    return ["node", cli] + args


def check_project(workdir: Path) -> dict:
    proc = run(cli_command(["check", str(workdir), "--json"]), check=False)
    try:
        return json.loads(proc.stdout)
    except Exception:
        return {"ok": proc.returncode == 0, "raw": (proc.stdout or proc.stderr)[-2000:]}


def render_project(workdir: Path, output: Path, cfg: dict) -> bool:
    cmd = cli_command(["render", str(workdir), "-o", str(output),
                       "--quality", "delivery", "--fps", str(cfg["video"]["fps"]),
                       "--json", "--strict"])
    proc = run(cmd, check=False)
    if proc.returncode != 0:
        print("[render]", (proc.stdout or proc.stderr)[-2500:])
    return proc.returncode == 0


def cap_size(path: Path) -> None:
    """Keep the MP4 under the Vercel 100 MB static limit."""
    size = path.stat().st_size
    if size <= 92 * 1024 * 1024:
        return
    temp = path.with_suffix(".small.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
         "-c:v", "libx264", "-crf", "25", "-maxrate", "10M", "-bufsize", "20M",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temp)])
    if temp.exists() and temp.stat().st_size < size:
        temp.replace(path)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def story_flags(story: dict, cfg: dict) -> tuple[list[str], list[str]]:
    """Compute QA flags once; return (all_flags, critical_flags)."""
    found = qa_flags(story, cfg)
    return found, [f for f in found if f in CRITICAL_FLAGS]


def sentence_spans(story: dict, words: list[dict]) -> list[dict]:
    """Attach word-timing spans (start/end seconds) to each script sentence.

    Token counts of the script sentences are used to slice the flat word list
    produced by either voice path, so English and Hinglish/Hindi both work
    without any caption-token substitution.
    """
    sentences = story.get("sentences", [])
    if not sentences:
        return []
    counts = [len(tokens(s)) for s in sentences]
    total_words = len(words)
    spans: list[dict] = []
    cursor = 0
    for i, (sentence, count) in enumerate(zip(sentences, counts)):
        # The last sentence absorbs any remainder from TTS boundary merging.
        take = count if i < len(sentences) - 1 else max(0, total_words - cursor)
        chunk = words[cursor:cursor + take]
        cursor += take
        if chunk:
            start, end = chunk[0]["start"], chunk[-1]["end"]
        elif words:
            start = end = words[min(cursor, total_words - 1)]["end"]
        else:
            start = end = 0.0
        spans.append({"index": i, "text": sentence, "start": start, "end": end})
    return spans


def voice_provider(cfg: dict, story: dict) -> str:
    """Report the configured TTS provider (the voice id is logged separately)."""
    return str(cfg.get("voice", {}).get("provider", "edge"))


def write_debug_dump(item: dict, cfg: dict, voice: dict,
                     captions: list[dict], out_root: Path, reel_id: str) -> Path:
    """Persist the QA-gated script and all data needed to audit its timing."""
    story = item["story"]
    found = item.get("flags", [])
    dump = {
        "id": reel_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "language": story.get("language", "en"),
        "script": story.get("script", ""),
        "sentence_spans": sentence_spans(story, voice.get("words", [])),
        "caption_groups": captions,
        "flags": found,
        "critical_flags": item.get("critical", []),
        "warnings": [f for f in found if f not in CRITICAL_FLAGS],
        "source_url": story.get("url", ""),
        "voice_provider": voice_provider(cfg, story),
        "voice": (cfg.get("voice", {}).get("hindi_voice")
                  if story.get("language") == "hi" else cfg.get("voice", {}).get("voice")),
        "duration": voice.get("duration", 0.0),
    }
    path = out_root / f"{reel_id}.debug.json"
    path.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def prepare_stories(cfg, state, limit: int) -> list[dict]:
    """Build, QA-gate and dedupe candidate stories.

    Flagged items are kept (annotated with ``flags``/``critical``) so --check
    can report them and --run can record a clean 'rejected' run. Callers must
    never process an item whose ``critical`` list is non-empty.
    """
    candidates = discover(cfg)
    candidates = [c for c in candidates if not state.is_seen(c)]
    articles = enrich_and_gate(candidates, cfg, limit=max(24, limit * 6))
    stories: list[dict] = []
    for article in articles:
        english = build_story(article, cfg)
        if not english:
            continue

        all_flags, critical = story_flags(english, cfg)
        story = english
        if cfg.get("language", {}).get("output", "en") == "hi":
            story = to_hindi(english, cfg)
            hi_flags, hi_critical = story_flags(story, cfg)
            all_flags = _unique(all_flags + hi_flags)
            critical = _unique(critical + hi_critical)

        if any(similarity(story["headline"], s["story"]["headline"]) >= 0.6 for s in stories):
            continue

        warnings = [f for f in all_flags if f not in CRITICAL_FLAGS]
        if warnings:
            print(f"[qa-warn] {story['source']}: {story['headline'][:60]} "
                  f"-> {', '.join(warnings)}")
        if critical:
            print(f"[qa-reject] {story['source']}: {story['headline'][:60]} "
                  f"-> critical: {', '.join(critical)}")

        stories.append({
            "story": story, "article": article, "flags": all_flags,
            "critical": critical,
        })
        if len(stories) >= limit:
            break
    return stories


def report_check(selection: list[dict]) -> None:
    """Print flag counts and per-story reject reasons without rendering."""
    counts = Counter(f for item in selection for f in item["flags"])
    critical_counts = Counter(f for item in selection for f in item["critical"])
    print(f"QA FLAG COUNTS: {dict(counts.most_common())}")
    print(f"CRITICAL REJECT COUNTS: {dict(critical_counts.most_common())}")
    clean = 0
    for i, item in enumerate(selection, 1):
        story, critical = item["story"], item["critical"]
        if critical:
            print(f"  [{i}] REJECT {story['source']}: {story['headline'][:70]}")
            print(f"        reject reason: {', '.join(critical)}")
            if item["flags"]:
                print(f"        all flags: {', '.join(item['flags'])}")
        else:
            clean += 1
            warnings = [f for f in item["flags"] if f not in CRITICAL_FLAGS]
            print(f"  [{i}] PASS   {story['source']}: {story['headline'][:70]}")
            if warnings:
                print(f"        warnings: {', '.join(warnings)}")
    print(f"QA summary: {clean} pass / {len(selection) - clean} reject")


def make_manifest(story: dict, article: dict, cfg: dict) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return {
        "id": f"{stamp}-{slugify(story['headline'], 36)}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "story": {k: story[k] for k in ("headline", "hook", "script", "word_count",
                                        "source", "url", "published")},
        "article": {
            "source": article.get("source", ""),
            "url": article.get("url", ""),
            "og_image": article.get("og_image", ""),
            "overlap": article.get("overlap"),
        },
        "config": {"voice": cfg["voice"]["voice"], "rate": cfg["voice"]["rate"]},
        "rights": "News images and headlines belong to their publishers; source URL recorded in this manifest.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="preview + QA flag report only")
    parser.add_argument("--run", action="store_true", help="generate and render")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--no-publish", action="store_true")
    parser.add_argument("--no-render", action="store_true")
    parser.add_argument("--local-state", action="store_true")
    args = parser.parse_args()

    cfg = load_config(HERE / "config.yaml")
    if args.local_state:
        cfg["publish"]["state_backend"] = "local"
    os.chdir(HERE)

    requested = args.count if args.run else 5
    # For --run gather a pool so a few QA rejects do not starve the requested
    # number of clean reels; for --check keep the preview short.
    pool_limit = max(12, requested * 6) if args.run else requested
    state = State(cfg, HERE)
    state.load()
    selection = prepare_stories(cfg, state, pool_limit)

    print(f"Discovered/gated: {len(selection)} story candidate(s)")
    for item in selection:
        s = item["story"]
        print(f"  - {s['source']}: {s['headline'][:78]} "
              f"[{s['word_count']}w, {len(item['article']['images'])} images]")
    if args.check:
        report_check(selection)
        return 0 if any(not i["critical"] for i in selection) else 2
    if not args.run:
        return 0
    if not selection:
        print("[noop] not enough fresh, complete material — exiting cleanly")
        state.record_run({"outcome": "noop", "reason": "insufficient_material", "candidates": 0})
        return 0

    out_root = ensure_dir(HERE / "out")
    work_root = ensure_dir(HERE / "work")
    published_ids = []
    made = 0
    for item in selection:
        if made >= requested:
            break
        story, article = item["story"], item["article"]

        # Hard QA gate: reject before any TTS, image work or rendering.
        if item["critical"]:
            reason = ", ".join(item["critical"])
            print(f"[reject] {story['headline'][:60]} — critical QA flags: {reason}")
            state.record_run({
                "outcome": "rejected",
                "reason": "qa_critical_flags",
                "critical_flags": item["critical"],
                "flags": item["flags"],
                "articles": {article["url_key"]: {
                    "title": story["headline"],
                    "title_key": article.get("title_key", ""),
                    "source": story["source"],
                    "status": "rejected",
                    "reasons": item["critical"],
                }},
            })
            continue

        manifest = make_manifest(story, article, cfg)
        workdir = work_root / manifest["id"]
        if workdir.exists():
            shutil.rmtree(workdir)
        ensure_dir(workdir)

        # Hindi words from synthesize_natural are already Hinglish — never
        # replace caption tokens with the story's transliterated token list.
        if story.get("language") == "hi":
            voice = synthesize_natural(story, cfg, workdir)
        else:
            voice = synthesize(story, cfg, workdir)
        captions = group_words(voice["words"], cfg)
        write_captions(captions, workdir)
        write_debug_dump(item, cfg, voice, captions, out_root, manifest["id"])

        needed = math.ceil((voice["duration"] + float(cfg["video"]["tail_seconds"]))
                           / float(cfg["video"]["shot_seconds"])) + 1
        shots = prepare_stills(article, cfg, workdir, needed)
        if not shots:
            print(f"[skip] no usable images for {story['headline'][:50]}")
            continue
        write_project(workdir, story, voice, captions, shots, cfg, manifest)

        check = check_project(workdir)
        ok = check.get("ok", check.get("passed", False))
        print(f"[check] {manifest['id']}: {'ok' if ok else 'issues'}")
        if not ok:
            print(json.dumps(check, ensure_ascii=False)[:3000])
            continue

        if not args.no_render:
            video_path = out_root / f"{manifest['id']}.mp4"
            if not render_project(workdir, video_path, cfg):
                continue
            cap_size(video_path)
            manifest["video_path"] = str(video_path.relative_to(HERE)).replace("\\", "/")
        else:
            video_path = None

        if not args.no_publish and video_path:
            from pipeline.publish import publish_all, upload_video
            blob = upload_video(video_path, cfg, manifest)
            if blob:
                manifest["video_url"] = blob["url"]
                published = publish_all(video_path, blob["url"], manifest, cfg)
                manifest["published"] = published
            else:
                print("[publish] no video URL; platform uploads skipped")

        (out_root / f"{manifest['id']}.manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        state.record_run({
            "outcome": "rendered" if video_path else "prepared",
            "articles": {article["url_key"]: {
                "title": story["headline"],
                "title_key": article.get("title_key", ""),
                "source": story["source"], "status": "rendered", "reel": manifest["id"],
            }},
            "reels": [manifest],
        })
        published_ids.append(manifest["id"])
        made += 1

    print(f"Done. {made} reel(s): {published_ids}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
