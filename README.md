# Bollywood Reel Generator

Automated 9:16 entertainment-news reels for Bigg Boss / Bollywood. **No LLM is
used.** The script is assembled deterministically, sentence by sentence, from a
headline, an RSS lead and whole article paragraphs; a free machine-translation
endpoint renders each sentence in Hindi and a free neural TTS voice supplies the
narration and word timings. Only MT and TTS are called externally.

## Pipeline

```
Bing News RSS + 3 direct feeds
        ↓  topic / recency / source filters
article fetch → quality gate (≥3 paragraphs, ≥600 chars, og:image, headline overlap)
        ↓  URL key + title key + title Jaccard dedupe against state
sentence-first English brief (hook → lead → body → CTA)
        ↓  free MT, one sentence at a time ("।" boundaries preserved)
Hindi narration text + edge-tts SentenceBoundary timings (hi-IN-MadhurNeural)
        ↓  offline Devanagari → Hinglish romanization for caption words
script QA gate → Hinglish captions with per-word karaoke timing
        ↓
image harvest → perceptual-hash dedupe → vertical collage stills (1080×1920)
        ↓
HyperFrames project (pinned CLI 0.9.77) → check → render → ffmpeg size cap
        ↓
Vercel Blob (public MP4 URL, rolling 7-day cleanup)
        ↓  → YouTube Shorts   (OAuth refresh token + resumable upload)
        ↓  → Instagram Reels  (Graph API: container → poll → publish)
append-only private Vercel Blob state records (local JSON fallback)
```

Every publishing step is optional: it runs only when its credentials are present
in the environment, and successful platform IDs are written into the manifest
ledger so a rerun never double-posts.

## Layout

| Path | Purpose |
|---|---|
| `config.yaml` | topics, feeds, gate thresholds, voice, caption and video settings |
| `generate.py` | CLI orchestrator (`--check`, `--run`, `--count`, `--no-render`, `--no-publish`, `--local-state`) |
| `pilot.py` | 14-day audit of the sentence-first script builder + Hindi QA flags |
| `pipeline/sources.py` | RSS discovery (tolerant XML parser) |
| `pipeline/articles.py` | article extraction + no-LLM quality gate |
| `pipeline/story.py` | deterministic sentence-first script assembly + Hindi translation |
| `pipeline/translate.py` | free MT (Google Translate endpoint) + offline Devanagari→Hinglish romanization |
| `pipeline/voice.py` | edge-tts synthesis and word/group timings |
| `pipeline/captions.py` | caption grouping and karaoke timing data |
| `pipeline/qa.py` | script quality flags used by the pilot and production gate |
| `pipeline/images.py` | image download, perceptual-hash dedupe and collage baking |
| `pipeline/project.py` | HyperFrames project writer (motion, graphics, captions, audio) |
| `pipeline/state.py` | append-only state (private Vercel Blob or local JSON) |
| `pipeline/publish.py` | public Blob video, YouTube Shorts and Instagram Reels |
| `assets/fonts/` | bundled Noto Sans Devanagari (OFL) |
| `.github/workflows/reels.yml` | 6-hourly scheduled run (cron + manual dispatch) |
| `tests/test_state_publish.py` | offline unit tests for state, publish and the workflow YAML |

## Run locally

```bash
pip install -r requirements.txt
python generate.py --check                               # discovery + gate + story preview
python pilot.py                                          # 14-day sentence-first script audit
python generate.py --run --count 1 --no-publish --local-state
python -m pytest tests/test_state_publish.py -q          # offline tests, no network
```

`--local-state` forces the local JSON ledger (`state/ledger.json`) even when the
Blob backend is configured. A full run writes the render to `out/`, a per-reel
manifest (`out/*.manifest.json`) and the collage stills under `work/`.

Set `HYPERFRAMES_CLI` to the pinned CLI's `bin/hyperframes.mjs` if `npx` is
unavailable; CI does this from `node_modules/hyperframes` after installing
`hyperframes@0.9.77`.

## Quality gates

- **Article gate:** recency, ≥3 paragraphs, ≥600 characters, an `og:image`,
  headline–body overlap and a minimum image width.
- **Dedupe:** canonical URL key, normalized title key and token-Jaccard title
  similarity (default ≥ 0.6) against every stored run record; images are
  deduplicated by perceptual hash.
- **Script gate / QA flags:** sentence count, punctuation (`।`), repeated or
  truncated sentences, relative time references, vague pronouns, metadata
  leaks, clickbait and word-count bounds. Run `python pilot.py` to see the flag
  deltas on 14 days of real material before rendering.

## State and publishing

State is an append-only audit log: one uniquely named JSON record per run in a
**private** Vercel Blob store under `state/runs/`, merged on load by run id. It
holds article keys, image hashes, platform IDs and public URLs — never image or
video bytes. If credentials or the SDK are missing, it transparently falls back
to `state/ledger.json`. Public MP4s live in a separate Blob store and are pruned
after 7 days.

## GitHub Actions secrets

| Secret | Needed for |
|---|---|
| `BLOB_STATE_TOKEN`, `BLOB_STATE_STORE_ID` | private state store |
| `BLOB_VIDEO_TOKEN`, `BLOB_VIDEO_STORE_ID` | public MP4 hosting |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | YouTube Shorts |
| `IG_LONG_LIVED_TOKEN`, `IG_USER_ID` | Instagram Reels |

No secret is committed to the repo; all are injected as step environment
variables. The workflow needs only `contents: read` and writes no git state.
YouTube uploads default to **private** when `publish.verification_required` is
true — flip it to `public` after review. Instagram needs a Professional account
linked to a Facebook Page.

## Language behaviour

- **Narration:** Hindi (`hi-IN-MadhurNeural`), translated one sentence at a time.
- **Captions:** Hinglish (Roman) transliterated offline from the same Hindi
  tokens, so word timings stay aligned.
- **On-screen graphics and platform copy:** Hindi; the source URL is recorded in the manifest and platform descriptions.

## Rights

Headlines, article text and images belong to their publishers. The generator
keeps only hashes/URLs in state and records the source in the manifest and on
screen. Review usage rights before publishing commercially.
