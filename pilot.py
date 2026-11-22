#!/usr/bin/env python3
"""Pilot: audit 14 days of stories with the new sentence-first script builder.

Prints flag counts and full sample Hindi scripts. Does not render anything.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from pipeline.articles import enrich_and_gate
from pipeline.qa import flags
from pipeline.sources import discover
from pipeline.story import build_story, to_hindi
from pipeline.utils import load_config

cfg = load_config(HERE / "config.yaml")
cfg["gate"]["lookback_days"] = 14

candidates = discover(cfg)
articles = enrich_and_gate(candidates, cfg, limit=40)
english, hindi = [], []
for article in articles:
    en = build_story(article, cfg)
    if not en:
        continue
    hi = to_hindi(en, cfg)
    english.append((en, flags(en, cfg)))
    hindi.append((hi, flags(hi, cfg)))

print(f"14-day candidates: {len(candidates)} | accepted: {len(articles)} | "
      f"scripts: {len(english)}")

def report(label, rows):
    counts = Counter(f for _, fs in rows for f in fs)
    print(f"\n{label} FLAGS: {dict(counts.most_common())}")
    return counts

before = Counter({
    "no_sentence_punctuation": 34, "too_few_sentences": 38, "vague_pronoun": 32,
    "time_relative": 11, "truncated_lead": 9, "question_headline": 7,
})
after_en = report("ENGLISH", english)
after_hi = report("HINDI", hindi)

print("\nHINDI FLAG DELTA (old audit -> pilot):")
for key in sorted(set(before) | set(after_hi)):
    print(f"  {key:26} {before.get(key, 0):3} -> {after_hi.get(key, 0):3}")

print("\n=== FULL SAMPLE HINDI SCRIPTS ===")
for story, fs in hindi[:4]:
    print(f"\nSOURCE: {story['source']} | {story['url']}")
    print(f"FLAGS: {', '.join(fs) or 'none'}")
    print(f"SCRIPT ({story['word_count']}w, {story['script'].count('।')} sentence breaks):")
    print(story["script"])
    print("CAPTION WORDS:", " ".join(story["caption_tokens"][:16]))

report_path = HERE / "out" / "pilot-report.json"
report_path.parent.mkdir(exist_ok=True)
report_path.write_text(json.dumps({
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "candidates": len(candidates), "accepted": len(articles),
    "english_flags": dict(after_en), "hindi_flags": dict(after_hi),
    "samples": [{"source": s["source"], "url": s["url"], "flags": fs,
                 "script": s["script"]} for s, fs in hindi[:8]],
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nReport: {report_path}")
