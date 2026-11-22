# Quality report and iteration record

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | No truncated leads, metadata leaks, missing `।` boundaries, or question hooks; every script ≥5 sentences | **PASS** — final 14-day pilot: 0 English flags, 0 critical Hindi flags |
| 2 | QA gate rejects flagged scripts before TTS/render; debug dump per reel | **PASS** — 5 mocked gate tests + `out/<id>.debug.json` |
| 3 | Continuous Hindi narration with sentence pauses; Hinglish captions, yellow highlight, no cross-sentence caption groups | **PASS** — silence detection found 4 sentence pauses; caption-boundary tests pass; `hyperframes check` 0 errors |
| 4 | Dedupe: a second run over the same candidates is a no-op | **PASS** — local/Blob state tests (17) |
| 5 | CI workflow valid, secrets only from env, publish/state tested | **PASS** — YAML parsed; 17 publish/state tests |
| 6 | Full test suite, pilot audit, one rendered reel inspected | **PASS** — 41 tests, `compileall` clean, `out/production-test.mp4` verified (60.3 s, 1080×1920, H.264/AAC) |

## Iterations

1. **Initial build** — RSS gate, deterministic script, edge-tts word timings, collage stills, HyperFrames render.
   - Found: ad creative harvested from Figma; picture track ended early; publisher label looked like a watermark.
   - Fixed: ad-domain blocklist; shot count derived from audio duration; removed source label.
2. **Hinglish/Hindi switch** — translation + Devanagari font + grouped TTS.
   - Found: grouped TTS produced choppy, story-less audio; flattening `।` destroyed sentence pauses.
   - Fixed: single continuous TTS call with `SentenceBoundary` events; sentence-level anchor mapping.
3. **Script audit (14 days)** — 38 scripts analysed. Found 34/38 missing sentence punctuation, 32/38 vague pronouns, 11/38 relative-time references, 9 truncated leads, cross-outlet duplicates, metadata leakage.
4. **Pilot implementation (multi-agent)** — three parallel agents with disjoint file ownership:
   - script quality (`pipeline/story.py`, `pipeline/qa.py`, tests),
   - QA gate and debug dumps (`generate.py`, tests),
   - state/publish/CI hardening (`state.py`, `publish.py`, workflow, README, tests).
   - Independently reviewed all diffs; corrected two stale "source credited on screen" claims.
5. **Integration fixes by reviewer** — ITRANS converted `।` to `|`, so caption groups crossed sentence boundaries; added pause-marker conversion and a merge guard, with tests. Verified sentence pauses in the rendered audio.

## Remaining issues / manual steps

1. **External-service credentials (only remaining setup)**: add the nine GitHub Actions secrets listed in `README.md` — Vercel Blob state + video token/store IDs, YouTube OAuth client/secret/refresh token, Instagram long-lived token + user ID. Until then, publishing steps skip cleanly and the workflow still produces MP4 artifacts.
2. **`latin_in_hindi`** is a non-critical warning on 3/30 pilot scripts — Google Translate leaves brand names such as *JioHotstar* in Latin script. Intentional and harmless.
3. **YouTube uploads default to private** (`publish.verification_required: true`) until reviewed manually.
4. GitHub Actions cron can be delayed under load; the generator always reads the latest RSS feed.

## Verification commands

```bash
python -m pytest tests -q          # 41 passed
python -m compileall pipeline generate.py pilot.py
python pilot.py                    # 14-day script audit
python generate.py --check         # QA gate preview
python generate.py --run --count 1 # full pipeline
```
