# Quality report and iteration record

## Acceptance criteria

| # | Criterion | Result |
|---|---|---|
| 1 | No truncated leads, metadata leaks, missing `।` boundaries, or question hooks; every script ≥5 sentences | **PASS** — final 14-day pilot: 0 English flags, 0 critical Hindi flags |
| 2 | QA gate rejects flagged scripts before TTS/render; debug dump per reel | **PASS** — 5 mocked gate tests + `out/<id>.debug.json` |
| 3 | Continuous Hindi narration with sentence pauses; Hinglish captions, yellow highlight, no cross-sentence caption groups | **PASS** — silence detection found 4 sentence pauses; caption-boundary tests pass; `hyperframes check` 0 errors |
| 4 | Dedupe: a second run over the same candidates is a no-op | **PASS** — local/Vercel/Netlify state tests, including oldest-first merge and corrupt-record skipping |
| 5 | CI workflow valid, secrets only from env, publish/state tested | **PASS** — YAML parsed; 25 publish/state tests (17 existing + 8 Netlify), no token in commands |
| 6 | Full test suite, pilot audit, one rendered reel inspected | **PASS** — 50 tests, `compileall` clean, `out/production-test.mp4` verified (60.3 s, 1080×1920, H.264/AAC) |

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
7. **Public-repository hardening (this iteration)** — prepared the code-only repo for public git hosting (GitHub/GitLab) with no media, no committed secrets, and clear rights/security documentation. Added `CONTENT_POLICY.md` (what is/is not stored, transient third-party use, attribution, takedown process) and `SECURITY.md` (private reporting, secret-name inventory, rotation guidance); updated `README.md` (public-repo notice, docs links, keep-alive + OneSignal alerts, exact remaining secrets) and `.gitignore` (media, logs, temp files and local credential files can never be committed; `assets/fonts/*.woff2` remains tracked). The workflow agent added a **Netlify keep-alive** step (plain HTTPS GET, no credential) and a **OneSignal failure-notification** step (`notify_failure.py` + `tests/test_notify.py`), which consume the new `ONESIGNAL_APP_ID` / `ONESIGNAL_REST_API_KEY` pair.

### Iteration 7 acceptance criteria and status

| # | Criterion | Status |
|---|---|---|
| 1 | Repo is code-only: no article bodies, images, audio, MP4s or secrets tracked; `git ls-files` contains only source/config/tests/docs/fonts/workflow | **PASS** — verified via `git ls-files` (29 currently tracked files), no media/secret values found |
| 2 | `.gitignore` blocks `work/`, `out/`, `state/`, `node_modules/`, `.hyperframes/`, `__pycache__/`, `*.pyc`, `.env`, `*.log`, `pilot_*.txt`, `*.mp4`, `*.png`, `*.wav`, `*.mp3`, but not `assets/fonts/*.woff2` | **PASS** — both tracked OFL fonts remain in `git ls-files` |
| 3 | Workflow uses `contents: read`, writes no git state, injects secrets only via env, never in argv/logs | **PASS** — YAML verified; Netlify creds read from environment |
| 4 | Netlify app stays active without manual login (keep-alive) and failures are alerted (OneSignal) | **PASS** — keep-alive present; Web Push configured and a device subscribed; two live test pushes delivered (`successful: 1, failed: 0`); notifier is stdlib-only so it survives a failed `pip install` |
| 5 | Attribution + takedown + security reporting documented for a public repo | **PASS** — `CONTENT_POLICY.md` and `SECURITY.md` added; README links them |
| 6 | No live token strings in any tracked file; only dummy placeholders in tests | **PASS** — scan found only test fixtures (e.g. `"token"`, `"secret"`), no real credential patterns |

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

1. **Remaining setup**: add the publishing secrets listed in `README.md` — Vercel Blob video token/store ID, YouTube OAuth client/secret/refresh token and Instagram token. Netlify and OneSignal are fully configured and verified.
2. **`latin_in_hindi`** is a non-critical warning on 3/30 pilot scripts — Google Translate leaves brand names such as *JioHotstar* in Latin script. Intentional and harmless.
3. **YouTube uploads default to private** (`publish.verification_required: true`) until reviewed manually.
4. GitHub Actions cron can be delayed under load; the generator always reads the latest RSS feed.

## Verification commands

```bash
python -m pytest tests -q          # 66 passed (after iteration 7 notification tests)
python -m compileall pipeline generate.py
python -m compileall pilot.py
python pilot.py                    # 14-day script audit
python generate.py --check         # QA gate preview
python generate.py --run --count 1 # full pipeline

# Public-repo hardening verification (iteration 7)
git ls-files                       # only source/config/tests/docs/fonts/workflow
git status --short                 # working tree status
```
