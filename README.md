# Bollywood Reel Generator

Automated 9:16 entertainment-news reels for Bigg Boss / Bollywood, published to a
public repository under the stealth name **`config-sync`**. **No LLM is used.**
The script is assembled deterministically, sentence by sentence, from a
headline, an RSS lead and whole article paragraphs; a free machine-translation
endpoint renders each sentence in Hindi and a free neural TTS voice supplies the
narration and word timings. Only MT and TTS are called externally.

> **Public, code-only repository.** No article bodies, images, audio, MP4s or
> secrets are committed — see [CONTENT_POLICY.md](CONTENT_POLICY.md) and
> [SECURITY.md](SECURITY.md).

## Documentation

- [CONTENT_POLICY.md](CONTENT_POLICY.md) — what is/is not stored, transient use
  of third-party news material, attribution, retention and the takedown process.
- [SECURITY.md](SECURITY.md) — how to report vulnerabilities or exposed
  secrets, the full secret inventory (including the Function's site-level
  variables), and rotation guidance.
- [QUALITY_REPORT.md](QUALITY_REPORT.md) — acceptance criteria, iteration
  record (including CI failure fixes), verification status and remaining issues.

## Pipeline

```
Bing News RSS + 3 direct feeds
        ↓  topic / recency / source filters + entity-signature clustering
article fetch → quality gate (≥3 paragraphs, ≥600 chars, og:image, headline overlap)
        ↓  entity-aware script dedupe + URL key / title key / title Jaccard vs state
deterministic English brief (hook → lead → body → CTA)
        ↓  free MT, one sentence at a time ("।" boundaries preserved)
continuous hi-IN TTS (hi-IN-MadhurNeural) with SentenceBoundary timings
        ↓  offline Devanagari → Hinglish romanization for caption words
script QA gate → Hinglish captions with yellow current-word highlight
        ↓
image harvest → perceptual-hash dedupe → vertical collage stills (1080×1920)
        ↓
HyperFrames project (pinned CLI 0.9.77) → check → render → ffmpeg size cap
        ↓
Netlify Blob store `reels-media` (7-day rolling retention)
        ↓  public URL  https://bollywood-reel-generator.netlify.app/r/reels/<ascii-id>.mp4
        ↓  serverless Function streams with HTTP Range support (206 / Accept-Ranges)
        ↓  → YouTube Shorts   (OAuth refresh token + resumable upload)  [optional]
        ↓  → Instagram Reels  (Graph API: container → poll → publish)   [optional]
append-only private Netlify Blob state records (store `reel-state`)
```

Every publishing step is optional: it runs only when its credentials are present
in the environment, and successful platform IDs are written into the manifest
ledger so a rerun never double-posts. **Netlify is the default and active host
for both state and public media. Vercel is an optional fallback, not the primary
platform.** There is no LLM anywhere in the loop.

## Public MP4 hosting (Netlify-first)

The finished MP4 is uploaded with the pinned Netlify CLI to the private Blob
store `reels-media` under the key `reels/<ascii-reel-id>.mp4` (the id is
sanitized to `[A-Za-z0-9_-]`). It is **not** committed to git and **not** stored
in the `public/` folder. A redirect in `netlify.toml` maps:

```
/r/*  →  /.netlify/functions/video?key=:splat          (HTTP 200)
```

The serverless Function [`netlify/functions/video.mjs`](netlify/functions/video.mjs)
reads the object with `@netlify/blobs` and streams it with `Content-Type:
video/mp4`, `Accept-Ranges: bytes`, `206 Partial Content` + `Content-Range` for
range requests, `416` for invalid ranges, and a one-hour public cache header.
This lets social platforms seek and verify the file.

- **Public URL format:**
  `https://bollywood-reel-generator.netlify.app/r/reels/<ascii-reel-id>.mp4`
- **Retention:** blobs older than **7 days** are pruned after each successful
  upload (the just-uploaded object is always protected).
- **Function credentials:** the Function reads `NETLIFY_SITE_ID` and
  `NETLIFY_AUTH_TOKEN` from the **Netlify site's environment variables** (not
  from the Actions environment). These must be set on the Netlify site for
  public video delivery to work; see [SECURITY.md](SECURITY.md).
- **Fallback:** if `BLOB_VIDEO_TOKEN` / `BLOB_VIDEO_STORE_ID` are present, the
  same MP4 can instead be uploaded to a Vercel Blob store (`reels-media`), also
  with a 7-day rolling cleanup. Without them, Netlify is used unconditionally.

## Scheduled runs, keep-alive and failure alerts

The 6-hourly GitHub Actions workflow needs only `contents: read` and writes no
git state. It runs on **Node 22**, installs `ffmpeg`/`ffprobe` via apt, installs
the pinned CLIs (`hyperframes@0.9.77`, `netlify-cli@24`) in a **single**
`npm install` so one package cannot prune the other, and retains CI artifacts
for **2 days**. Two housekeeping behaviours keep the public setup dependable:

- **Netlify keep-alive:** a workflow step makes a plain HTTPS GET to the Netlify
  site so the app is never paused for inactivity (no manual login required). It
  needs no credential at all and uses no build minutes.
- **OneSignal failure notifications:** a workflow step sends a push alert on
  failed runs so issues are noticed without watching CI. Web Push is configured
  on `https://bollywood-reel-generator.netlify.app`, a device is subscribed, and
  live test pushes were delivered (`successful: 1, failed: 0`). Targeting uses a
  `session_count` filter (the named "Subscribed Users" segment returned no
  recipients). The notifier uses only the Python standard library so it still
  works when `pip install` fails.

## Layout

| Path | Purpose |
|---|---|
| `config.yaml` | topics, feeds, gate thresholds, voice, caption and video settings |
| `generate.py` | CLI orchestrator (`--check`, `--run`, `--count`, `--no-render`, `--no-publish`, `--local-state`, `--keep-work`) |
| `pilot.py` | 14-day audit of the sentence-first script builder + Hindi QA flags |
| `pipeline/sources.py` | RSS discovery (tolerant XML parser) + entity-signature story clustering |
| `pipeline/articles.py` | article extraction + no-LLM quality gate |
| `pipeline/story.py` | deterministic sentence-first script assembly + entity dedupe + Hindi translation |
| `pipeline/translate.py` | free MT (Google Translate endpoint) + offline Devanagari→Hinglish romanization |
| `pipeline/voice.py` | edge-tts synthesis, SentenceBoundary timings, retry + voice fallback |
| `pipeline/captions.py` | caption grouping, emphasis and karaoke timing data |
| `pipeline/qa.py` | script quality flags used by the pilot and production gate |
| `pipeline/images.py` | image download, perceptual-hash dedupe and collage baking |
| `pipeline/project.py` | HyperFrames project writer (motion, news-channel UI, captions, audio) |
| `pipeline/state.py` | append-only state (Netlify Blobs or Vercel Blob, local JSON fallback) |
| `pipeline/publish.py` | Netlify public MP4 upload + Function URL, YouTube Shorts, Instagram Reels |
| `netlify.toml` | `/r/*` redirect to the video Function; publish root `public/` |
| `netlify/functions/video.mjs` | serverless MP4 streamer with Range support |
| `public/` | Netlify static root: subscribe page + OneSignal service worker |
| `notify_failure.py` | stdlib-only OneSignal failure notifier |
| `assets/fonts/` | bundled Noto Sans Devanagari (OFL) |
| `.github/workflows/reels.yml` | 6-hourly run (cron + manual dispatch), keep-alive, 2-day artifacts, OneSignal alerts |
| `tests/` | offline unit tests for state, publishing, video Function, QA, motion and the workflow YAML |
| `QUALITY_REPORT.md` | acceptance criteria, iteration record and remaining issues |

## Run locally

```bash
pip install -r requirements.txt
python generate.py --check                               # discovery + gate + story preview
python pilot.py                                          # 14-day sentence-first script audit
python generate.py --run --count 1 --no-publish --local-state
python -m pytest tests -q                                # 199 passed, all offline (no network)
python -m compileall pipeline generate.py pilot.py
```

`--local-state` forces the local JSON ledger (`state/ledger.json`) even when a
remote backend (`netlify` or `blob`) is configured. A full run writes the render
to `out/`, a per-reel manifest (`out/*.manifest.json`), a QA dump
(`out/<id>.debug.json`) and the collage stills under `work/`. Work directories
are deleted after rendering in CI.

Set `HYPERFRAMES_CLI` to the pinned CLI's `bin/hyperframes.mjs` if `npx` is
unavailable; CI does this from `node_modules/hyperframes` after installing
`hyperframes@0.9.77` with Node 22.

## Quality gates

- **Article gate:** recency, ≥3 paragraphs, ≥600 characters, an `og:image`,
  headline–body overlap and a minimum image width. Near-identical headlines are
  clustered by named-entity signature before selection.
- **Dedupe:** canonical URL key, normalized title key and token-Jaccard title
  similarity (default ≥ 0.6) against every stored run record; entity repetition
  is capped in the brief; images are deduplicated by perceptual hash.
- **Script gate / QA flags:** sentence count, punctuation (`।`), repeated or
  truncated sentences, relative time references, vague pronouns, low entity
  density, metadata leaks, clickbait and word-count bounds. Run `python pilot.py`
  to see the flag deltas on 14 days of real material before rendering.

## State and publishing

State is an append-only audit log: one uniquely named JSON record per run,
merged oldest-first on load. It holds article keys, image hashes, platform IDs
and public URLs — never image or video bytes. The active backend is **Netlify
Blobs** (`publish.state_backend: netlify`):

- State store: `reel-state` (override with `publish.netlify_state_store`).
- Keys: `state/runs/<timestamp>-<uuid>.json`; new runs append, never overwrite.
- Access is via the locally installed Netlify CLI (`netlify-cli@24` in CI, or
  `npx --no-install netlify`), using `netlify blobs:list`, `blobs:get` and
  `blobs:set`. The CLI reads `NETLIFY_AUTH_TOKEN` and `NETLIFY_SITE_ID` from the
  environment — tokens are never put in command strings or logs.
- If the CLI or credentials are missing, state transparently falls back to
  `state/ledger.json`. The Vercel Blob backend remains available by setting
  `publish.state_backend: blob` (requires `BLOB_STATE_TOKEN` and
  `BLOB_STATE_STORE_ID`).

Public MP4 hosting uses the separate **Netlify Blob** store `reels-media` and
the `/r/<ascii-key>` Function described above. Vercel Blob (`BLOB_VIDEO_TOKEN`
/ `BLOB_VIDEO_STORE_ID`) is an optional fallback only.

## GitHub Actions secrets

All values are encrypted Actions secrets (plus the two Netlify **site**
environment variables needed by the video Function). Names only — no values are
ever printed or committed. A local backup of the credential values exists
outside the repository; it is never in git.

| Secret / variable | Needed for | Status |
|---|---|---|
| `NETLIFY_AUTH_TOKEN`, `NETLIFY_SITE_ID` | Netlify Blob state (`reel-state`) **and** public MP4 uploads (`reels-media`) via the Actions step | configured — **rotate** |
| `NETLIFY_AUTH_TOKEN`, `NETLIFY_SITE_ID` (set on the **Netlify site**, not in Actions) | read `reels-media` from `netlify/functions/video.mjs` for public `/r/` playback | configure/verify on the site |
| `GH_PAT` (GitHub PAT) | optional external automation/backups; the scheduled workflow itself uses only `contents: read` and the built-in `GITHUB_TOKEN` | configured — **rotate** |
| `ONESIGNAL_APP_ID` | OneSignal failure notifications (public app identifier) | configured and verified |
| `ONESIGNAL_REST_API_KEY` | OneSignal failure notifications (REST key used by the alert step) | configured and verified — **rotate** |
| `BLOB_VIDEO_TOKEN`, `BLOB_VIDEO_STORE_ID` | optional Vercel Blob public MP4 fallback | add manually if wanted |
| `BLOB_STATE_TOKEN`, `BLOB_STATE_STORE_ID` | optional legacy Vercel Blob state backend | add manually if wanted |
| `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` | YouTube Shorts | add manually if wanted |
| `IG_LONG_LIVED_TOKEN`, `IG_USER_ID` | Instagram Reels | add manually if wanted |

> **Rotate the GitHub PAT, Netlify PAT and OneSignal REST key:** they were
> shared in chat and must be treated as exposed. Generate new values, replace
> the encrypted secrets (and the Netlify site variable), then revoke the old
> ones. See [SECURITY.md](SECURITY.md). Publishing secrets that are absent are
> skipped cleanly — the workflow still produces MP4 artifacts.

No secret is committed to the repo; all are injected as step environment
variables. The workflow needs only `contents: read`, never puts tokens in argv
or logs, and writes no git state. YouTube uploads default to **private** when
`publish.verification_required` is true — flip it to `public` after review.
Instagram needs a Professional account linked to a Facebook Page.

## Verification status

- **Current commit `c5204b4`** ("iter6/7: motion craft, source diversity,
  Netlify serverless video hosting") is under CI verification.
- **Last green commit `dcf0135`** is tagged **`verified-working`** and is the
  rollback point if verification fails.
- Offline suite: **199 passing tests**.

```bash
python -m pytest tests -q          # 199 passed
python -m compileall pipeline generate.py pilot.py
python pilot.py                    # 14-day script audit
python generate.py --check         # QA gate preview
git status --short                 # working tree status
git diff --stat                    # change summary
git grep -nE 'ghp_[A-Za-z0-9]|nfp_[A-Za-z0-9]|os_v2_[A-Za-z0-9]|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' -- '*.md'  # must print nothing
```

## Language behaviour

- **Narration:** Hindi (`hi-IN-MadhurNeural` with fallback voices and retries),
  translated one sentence at a time.
- **Captions:** Hinglish (Roman) transliterated offline from the same Hindi
  tokens, so word timings stay aligned; the current word is highlighted in
  yellow (`#FFE600`).
- **On-screen graphics and platform copy:** Hindi; the source URL is recorded in
  the manifest and the platform descriptions.

## Rights

Headlines, article text and images belong to their publishers. The generator
uses them transiently for transformational news-commentary reels, keeps only
hashes/URLs/IDs in state, and records the source URL in the manifest and the
platform description. No scraped media is committed. Public MP4s expire after
7 days and CI artifacts after 2 days. See [CONTENT_POLICY.md](CONTENT_POLICY.md)
for attribution, retention and the takedown/contact process. Review usage rights
before publishing commercially.
