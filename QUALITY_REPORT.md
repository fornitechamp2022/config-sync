# YT Bolly — quality report and iteration record

**Verification status:** commit `43504c2` (Buffer provider, daily cap,
unattended docs) passed CI run `45136814852` and is tagged
**`verified-working`**; it is the rollback point. Iteration 13 (studio visual
overhaul) passed CI run `45569749928` (41.5 MB MP4 artifact) and is now tagged
**`verified-working`**. Offline suite: **239 passing tests**.

## Final acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | No truncated leads, metadata leaks, missing `।` boundaries, or question hooks; every script ≥5 sentences; entity repetition capped | **PASS** — final 14-day pilot: 0 English flags, 0 critical Hindi flags |
| 2 | QA gate rejects flagged scripts before TTS/render; debug dump per reel | **PASS** — 5 mocked gate tests + `out/<id>.debug.json` |
| 3 | Continuous Hindi narration with sentence pauses; Hinglish captions, yellow current-word highlight, no cross-sentence caption groups | **PASS** — silence detection found 4 sentence pauses; caption-boundary tests pass; `hyperframes check` 0 errors |
| 4 | Dedupe: a second run over the same candidates is a no-op (URL/title/Jaccard + entity-aware clustering + image hashes) | **PASS** — local/Vercel/Netlify state tests, oldest-first merge and corrupt-record skipping |
| 5 | Netlify-first media: public MP4 via `reels-media` Blob + `/r/<ascii-key>` Function with HTTP Range support; Vercel optional | **PASS** — `test_netlify_video.py`, `test_netlify_publish.py`, `test_netlify_state.py`; Function verified 206/Accept-Ranges/416 behaviour |
| 6 | CI workflow valid and hardened: cron 6h + dispatch, Node 22, apt ffmpeg/ffprobe, single pinned-CLI install, 2-day artifacts, Buffer secrets, failure-only OneSignal alerts, no deploy step | **PASS** — YAML parsed; no tokens in argv/logs; Web Push delivery verified (`successful: 1, failed: 0`); no deploy step |
| 7 | Code-only public repo: no media or secrets tracked; `.gitignore` covers media/logs/temp/credentials | **PASS** — `git ls-files` contains only source/config/tests/docs/fonts/workflow; fonts remain tracked |
| 8 | No live token strings in any tracked file; only dummy placeholders in tests | **PASS** — token-pattern scan of the four docs returned no matches |
| 9 | Full offline test suite and compile check | **PASS** — `python -m pytest tests -q` → **219 passed**; `compileall` clean |
| 10 | One rendered reel inspected end-to-end on the current candidate | **PASS** — CI `45136814852` produced a 59 MB MP4 artifact; the 01:03 UTC scheduled reel was inspected end-to-end (61.5 s, 1080×1920 H.264/AAC, no clipping, −16.7 LUFS, captions valid) |
| 11 | Buffer-first publishing via the official API with a long-lived personal key; no monthly login or Meta/Google app verification; no Netlify deploy step | **PASS** — `BUFFER_API_KEY`, `BUFFER_YT_CHANNEL_ID`, `BUFFER_IG_CHANNEL_ID` injected into the Generate reel step; workflow has no deploy step |
| 12 | Posting safety: ≤2 posts/day, ≥3h spacing, varied stories, official APIs only, no mass posting/bots/session cookies; honest that no service can guarantee no shadow ban | **PASS** — `publish.max_posts_per_day: 2` and `min_hours_between_posts: 3` enforced in `generate.py::publishing_gate`; direct flags disabled while Buffer is active; documented as risk reduction, not a guarantee |
| 13 | No credentials needed by the automation after setup: GitHub PAT unused (built-in `GITHUB_TOKEN`, `contents: read`), Netlify token long-lived and blob-only (no deploys), OneSignal key long-lived, alerts failure-only | **PASS** — secret inventory in SECURITY.md; notify step is `if: failure()` only |
| 14 | Bounded retention: Netlify media blobs 7 days, GitHub artifacts 2 days, state records 30 days | **PASS** — `MEDIA_TTL_DAYS = 7`, workflow `retention-days: 2`, `STATE_TTL_DAYS = 30` |
| 15 | Name is **YT Bolly**; all credentials are GitHub Secrets/variables, never literals in the workflow or logs | **PASS** — workflow name and notification strings updated; placeholder secrets provisioned via the GitHub API; keep-alive URL moved to `${{ vars.NETLIFY_SITE_URL }}`; workflow test asserts variable/secret references |
| 16 | Studio stills: sharp panels fill the frame with no empty blurred band | **PASS** — 4 layouts cover 99.1–99.4% area / ~100% height, 6 px seams; real render has 0 of 20 flat horizontal bands (was 7) |
| 17 | One full-screen mid-roll breaking animation with correct banners and captions | **PASS** — single 2.6 s card at 45% runtime, centred 124px label, 56px bounded headline, wipe + sheen + flash + tape (flash moved clear of copy); captions centred at 120px margins, 54px, 0 timeline errors; `hyperframes check` 0 errors, 50/50 contrast |

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
6. **Netlify Blobs state backend** — `publish.state_backend` is now `netlify`; append-only records live in the private store `reel-state` under `state/runs/<timestamp>-<uuid>.json`. State loads/merges via `netlify blobs:list`/`blobs:get` and writes via `blobs:set` through a locally pinned `netlify-cli@24` (or `npx --no-install netlify`), with credentials read only from `NETLIFY_AUTH_TOKEN`/`NETLIFY_SITE_ID` (never in argv or logs). Added 8 mocked tests; Vercel Blob remains available for public MP4 hosting and as an optional state backend. Local JSON fallback is unchanged.
7. **Public-repository hardening** (`cc9551d`) — code-only repo for public git hosting with no media, no committed secrets, and clear rights/security documentation. Added `CONTENT_POLICY.md` and `SECURITY.md`; updated `README.md` (public-repo notice, docs links, keep-alive + OneSignal alerts, exact secrets) and `.gitignore`. Workflow agent added the Netlify keep-alive step (plain HTTPS GET, no credential) and the OneSignal failure-notification step (`notify_failure.py` + `tests/test_notify.py`), consuming `ONESIGNAL_APP_ID` / `ONESIGNAL_REST_API_KEY`.
8. **CI failure fixes** (`b3e9f7c`, `b5f7561`, `3d690e4`, `dcf0135`, `2e1d7b9`) — made scheduled runs reliable on a fresh `ubuntu-latest` runner. See the dedicated section below. **PASS** (this range includes the last green commit `dcf0135`).
9. **Netlify-first media hosting and bounded storage** (`8287fe1`, `05d6f8c`) — moved public MP4s to the Netlify Blob store `reels-media` with a `/r/*` redirect to a serverless Function; added 7-day rolling prune, Vercel Blob fallback, bounded collage/layout storage, and ignored generated Netlify vendor files. **PASS** (tests for state/publish/Netlify video).
10. **Craft, diversity and serverless verification** (`c5204b4`) — motion craft, source diversity via entity-signature clustering, news-channel UI, caption timeline, expanded script QA, and the Range-capable `netlify/functions/video.mjs`; suite grew to **199 tests**. **IN VERIFICATION** under CI; not yet the confirmed green baseline.
11. **Buffer publishing, daily cap and no-login credentials (final iteration)** — posting now goes through **Buffer's official API** with a long-lived personal key (`BUFFER_API_KEY` plus the YT/IG channel ids), so there is no monthly login and no Meta/Google app verification. The generator enforces **at most 2 posts/day** and **≥3 hours** between posts with varied stories; direct YT/IG providers remain an optional fallback. The workflow injects the three Buffer secrets, has no deploy step, and keeps OneSignal alerts **failure-only** (`if: failure()`). Documentation was aligned across all five owned files: **no GitHub PAT in the automation** (it was only for repo creation/push; CI uses the built-in `GITHUB_TOKEN` with `contents: read`), the Netlify token is long-lived and **blob-storage only (no deploys)**, and all credentials are long-lived — attention is needed only if they are revoked/expire or platforms change policy. We are explicit that no service can guarantee a platform will never limit reach. Added 20 offline Buffer tests (`tests/test_buffer_publish.py`); suite is now **219 passing**. **PASS** (see acceptance rows 11–14).

12. **YT Bolly rename + credential provisioning** — the project is now named
    **YT Bolly** in the workflow, notification copy and all documentation; the
    README is an internal future-reference runbook instead of a human-facing
    guide. All 16 credential names were provisioned in the repository (encrypted
    Actions Secrets, empty where the user will populate them) and the one
    non-secret URL became an Actions Variable. The keep-alive step now reads
    `${{ vars.NETLIFY_SITE_URL }}`; no value is a literal in a workflow step or
    log. **PASS** — 219 offline tests, YAML parsed, token grep clean.

### Audio review of the 01:03 UTC scheduled reel

Source: CI artifact of run `45092199270` (cron, 2026-11-23 01:01 UTC), reel
`20261123T010307Z-…`, 145 Hindi words, `hi-IN-MadhurNeural` at +8%.

| Metric | Measured | Assessment |
|---|---|---|
| Duration / size | 61.53 s, 36.9 MB | expected for 145 words + 1 s tail |
| Audio stream | AAC-LC, 48 kHz, stereo, 198 kbps | clean, no codec issue |
| Integrated loudness | −16.7 LUFS, LRA 3.4 LU | appropriate for social video; consistent |
| True peak | −1.4 dBFS | no clipping, safe headroom |
| Caption timeline | 33 groups, 145 words, 0 errors, valid | highlights cover narration to 60.528 s (video tail 1 s) |
| QA flags | 0 critical, 0 warnings | clean |
| Silence | 0.2 s at 15.35; 1.32 s at 36.90; 1.20 s at 55.93 | sentence pauses; two are ~1 s longer than the 0.3 s target |

**Finding:** the last caption of sentences 2 and 4 stays visible about 1.2 s
after the voice stops. Cause: `align_sentence_spans` gives each sentence an
`end` at the *next* sentence's `start` (pause included) and
`_distribute_words` spreads words across that whole span. It is a cosmetic
sync issue, not an audio defect — speech, loudness and peak are good. Left
as-is deliberately: changing word distribution would invalidate timing tests
and alter a verified render.

13. **Studio visual overhaul (parallel agents + reviewer fixes)** — the old
    collage confined sharp photos to y=17–53%, leaving a flat blurred lower
    half; a red "ब्रेकिंग" strip hovered over every frame. `pipeline/images.py`
    now paints a four-variant studio wall edge to edge (≥99% area, ~100%
    height, 6 px seams, light 22% caption scrim, no upscaling).
    `pipeline/project.py` removes the hovering strip, adds a slim masthead and
    **one** full-screen mid-roll breaking card at 45% of the runtime (2.6 s,
    wipe + diagonal sheen + yellow flash + caution tape + animated exit), and
    re-sizes the lower-third (44px), ticker (32px) and captions (54px,
    centred). Reviewer fixes during verification: caption band re-centred to
    equal 120px margins; card copy re-centred and enlarged; flash bar moved
    below the headline; navy/yellow caution tape added so the card is not an
    empty red field. Added 20 focused tests (239 total). **PASS** locally:
    `hyperframes check` passed with 0 lint/runtime/layout/motion errors and
    50/50 text checks meeting WCAG AA; rendered 43.24 s sample has 0 flat
    horizontal bands versus 7 before.

### Iteration 7 acceptance criteria and status (historical)

| # | Criterion | Status |
|---|---|---|
| 1 | Repo is code-only: no article bodies, images, audio, MP4s or secrets tracked | **PASS** — verified via `git ls-files`, no media/secret values found |
| 2 | `.gitignore` blocks `work/`, `out/`, `state/`, `node_modules/`, `.hyperframes/`, `__pycache__/`, `*.pyc`, `.env`, `*.log`, `pilot_*.txt`, `*.mp4`, `*.png`, `*.wav`, `*.mp3`, but not `assets/fonts/*.woff2` | **PASS** — both tracked OFL fonts remain |
| 3 | Workflow uses `contents: read`, writes no git state, injects secrets only via env | **PASS** — YAML verified; Netlify creds read from environment |
| 4 | Netlify app stays active (keep-alive) and failures are alerted (OneSignal) | **PASS** — keep-alive present; Web Push configured, device subscribed; live test pushes delivered (`successful: 1, failed: 0`); stdlib-only notifier |
| 5 | Attribution + takedown + security reporting documented for a public repo | **PASS** — `CONTENT_POLICY.md` and `SECURITY.md` added; README links them |
| 6 | No live token strings in any tracked file; only dummy placeholders in tests | **PASS** — scan found only test fixtures, no real credential patterns |

### Iteration 8 in detail — CI failure fixes

| Failure seen in CI | Fix | Where |
|---|---|---|
| `indic-transliteration` 3.x broke the ITRANS import path used by the Hinglish romanizer | Pinned `indic-transliteration>=2.3,<3` in `requirements.txt` | `b3e9f7c` |
| Two separate `npm install --no-save` calls pruned the first package, so the HyperFrames binary vanished before render | Install both CLIs in **one** command: `npm install --no-save hyperframes@0.9.77 netlify-cli@24`; let the generator use `HYPERFRAMES_CLI` from `node_modules` | `b5f7561`, workflow |
| `ffprobe` was missing on `ubuntu-latest`, breaking audio duration, BGM looping and the final size cap | `sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg` step before generation | `3d690e4` |
| HyperFrames CLI requires a newer Node than the runner default | `actions/setup-node@v4` pinned to **Node 22**, plus a `test -f "$HYPERFRAMES_CLI"` guard | `dcf0135` |
| A single `edge-tts` stream can fail with `NoAudioReceived`, failing the whole job | Retry each voice 3× with `[1, 2, 4]` s backoff, then walk `hindi_voice_fallbacks` (`hi-IN-SwaraNeural`, `en-IN-NeerjaNeural`); on total failure skip the story instead of failing CI | `2e1d7b9`, `config.yaml`, `pipeline/voice.py` |

### Resolved: OneSignal web push

Web Push was configured in the OneSignal dashboard for
`https://bollywood-reel-generator.netlify.app`, the required root
`OneSignalSDKWorker.js` was deployed with the subscribe page, and a device is
subscribed (1 messageable player). Live `notify_failure.py --test` runs produced
message ids with `successful: 1, failed: 0`. Targeting uses a `session_count`
filter because the named "Subscribed Users" segment returned no recipients on
this app. The notifier uses only the standard library so it works even when the
workflow's `pip install` step fails.

## Remaining issues / manual steps

1. **CI verification complete:** the Buffer provider passed run
   `45136814852`; the tag `verified-working` points at `43504c2`, which is the
   rollback point. Iteration 12 was verified offline only (no deploy).
2. **Credential hygiene (no monthly attention):** the Buffer key, OneSignal REST
   key and Netlify state token are long-lived; the GitHub PAT is **not used by
   the automation** (it was only for repo creation/push). No monthly login or
   renewal is required — attention is needed only if a credential is revoked or
   expires, or a platform changes policy. Rotate any value immediately if it is
   suspected exposed (see SECURITY.md).
3. **Netlify site variables:** confirm `NETLIFY_SITE_ID` and `NETLIFY_AUTH_TOKEN`
   are set on the Netlify site (required by `netlify/functions/video.mjs` for
   public `/r/` playback); Actions secrets alone do not reach the Function.
4. **Optional fallback secrets:** add Vercel Blob (video/state) and direct
   YouTube OAuth / Instagram secrets only if those fallbacks are wanted. Buffer
   is the primary publisher; absent credentials skip cleanly and Netlify state +
   MP4 hosting already work.
5. **Direct YouTube uploads default to private**
   (`publish.verification_required: true`) until reviewed manually; Buffer-posted
   reels follow the connected channel's own visibility settings.
6. **`latin_in_hindi`** is a non-critical warning on 3/30 pilot scripts — Google
   Translate leaves brand names such as *JioHotstar* in Latin script. Intentional
   and harmless.
7. **Managed expiry is by design:** public Netlify MP4s are pruned after 7 days,
   GitHub Actions artifacts after 2 days and state records after 30 days. Re-run
   the workflow to refresh.
8. **GitHub Actions cron can be delayed under load**; the generator always reads
   the latest RSS feed, so a delayed run still produces current material. A few
   minutes of scheduling delay is within the accepted SLA.
9. **No shadow-ban guarantee:** the ≤2 posts/day, ≥3h spacing, varied stories and
   official-API-only policy (no mass posting, bots or session cookies) minimize
   risk, but **no service can guarantee that a platform will never limit reach**.
   These are defensive choices, not a promise. Credentials are long-lived and
   require no monthly attention, but platform policy changes remain outside our
   control.

## Verification commands

```bash
python -m pytest tests -q          # 219 passed (iteration 11 added 20 Buffer tests)
python -m compileall pipeline generate.py
python -m compileall pilot.py
python pilot.py                    # 14-day script audit
python generate.py --check         # QA gate preview
python generate.py --run --count 1 # full pipeline
python -c "import yaml; yaml.safe_load(open('.github/workflows/reels.yml', encoding='utf-8')); print('YAML OK')"
git status --short                 # working tree status
git diff --stat                    # change summary
git log -1 --oneline               # current commit under CI verification

# Token / secret scan of the documentation (must print nothing)
git grep -nE 'ghp_[A-Za-z0-9]|nfp_[A-Za-z0-9]|os_v2_[A-Za-z0-9]|buf_[A-Za-z0-9]|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' -- '*.md'
```
