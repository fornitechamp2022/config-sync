# YT Bolly

Internal runbook / future reference. Not an onboarding document. No values are
recorded here — only names, locations and behaviour.

## Identity

| Item | Value |
|---|---|
| Repository | `fornitechamp2022/config-sync` (public, code-only, stealth name) |
| Workflow | `YT Bolly` — `.github/workflows/reels.yml`, cron `0 */6 * * *` + `workflow_dispatch` |
| Public host | `https://bollywood-reel-generator.netlify.app` (already deployed; do not rename or deploy) |
| Public MP4 | `https://bollywood-reel-generator.netlify.app/r/reels/<ascii-id>.mp4` |
| State / media | Netlify Blobs `reel-state` / `reels-media`; no Vercel backend active |
| Publisher | Buffer official GraphQL API; ≤2 posts/day, ≥3 h apart; no double-post |
| Script source | Deterministic assembly + machine translation + neural TTS. No LLM. |

## Credentials — populate in GitHub

Credentials are **encrypted Actions Secrets**, referenced in the workflow only
as `${{ secrets.NAME }}`. Non-secret configuration is an **Actions Variable**,
referenced as `${{ vars.NAME }}`. Never put a value in the workflow, a log, an
issue, this file, or a command line. The workflow uses `contents: read` and the
built-in `GITHUB_TOKEN`; no GitHub PAT is used by automation.

Fill these at **Settings → Secrets and variables → Actions**:

| Name | Kind | Purpose | Status |
|---|---|---|---|
| `NETLIFY_AUTH_TOKEN` | Secret | Blob state + public MP4 upload (long-lived; **never a deploy**) | already set |
| `NETLIFY_SITE_ID` | Secret | Netlify site id, also needed by the video Function | already set |
| `ONESIGNAL_APP_ID` | Secret | Failure-only push alerts | already set |
| `ONESIGNAL_REST_API_KEY` | Secret | Failure-only push alerts (long-lived) | already set |
| `BUFFER_API_KEY` | Secret | Primary publisher, long-lived personal key — no monthly login / app verification | empty — populate |
| `BUFFER_YT_CHANNEL_ID` | Secret | Buffer channel id, YouTube Shorts | empty — populate |
| `BUFFER_IG_CHANNEL_ID` | Secret | Buffer channel id, Instagram Reels | empty — populate |
| `BLOB_STATE_TOKEN` / `BLOB_STATE_STORE_ID` | Secret | Optional legacy Vercel state backend | empty — optional |
| `BLOB_VIDEO_TOKEN` / `BLOB_VIDEO_STORE_ID` | Secret | Optional Vercel public-MP4 fallback | empty — optional |
| `YT_CLIENT_ID` / `YT_CLIENT_SECRET` / `YT_REFRESH_TOKEN` | Secret | Optional direct YouTube fallback | empty — optional |
| `IG_LONG_LIVED_TOKEN` / `IG_USER_ID` | Secret | Optional direct Instagram fallback | empty — optional |
| `NETLIFY_SITE_URL` | Variable | Keep-alive target URL for the site (non-secret) | set |

`GH_PAT` is deliberately absent: it was only for initial repo creation/push.
Absent optional secrets are skipped cleanly; the run still renders and uploads
an artifact.

**Also required on the Netlify site** (not in Actions), for the public `/r/`
streaming Function: site environment variables `NETLIFY_AUTH_TOKEN` and
`NETLIFY_SITE_ID`. Web Push subscription must exist on the site for alerts;
if alerts stop, reopen the site and subscribe again.

## Behaviour that matters later

- Cron can be delayed by a few minutes; acceptable. Delayed runs read the latest feed.
- Failure alerts only (`if: failure()`); no success push. Failure alerting must
  never fail the job (`notify_failure.py` exits 0 on all errors).
- Keep-alive is an unauthenticated GET to `${{ vars.NETLIFY_SITE_URL }}`; not a deploy.
- No Netlify deploy step exists. Each production deploy would cost 15 credits;
  do not add one.
- Posting caps live in `config.yaml`: `publish.provider: buffer`,
  `publish.max_posts_per_day: 2`, `publish.min_hours_between_posts: 3`.
  Direct `youtube`/`instagram` flags stay false while Buffer is active.
- Pacing/voice knobs: `voice.pace_profile` (`news` | `calm`),
  `voice.hindi_voice`, `voice.hindi_voice_fallbacks`, `mix.*`, `captions.*`.
- Neutralise a bad run: dispatch with `--no-publish --local-state`, or roll back
  to the `verified-working` tag.

## Limits (steady state, after resets)

| Service | Limit | Effect at 1–2 reels/day |
|---|---|---|
| Netlify Free | 300 credits/month, no rollover, site pauses at 0; ~20 credits/GB bandwidth | ~42–84 credits/month; not binding. Keep deploys at zero. |
| GitHub Actions | Public repo standard runners free; artifacts 2-day retention | fine |
| Buffer Free | 3 channels, 10 queued posts/channel, 1,000 API requests/channel/30 days | fine |
| Instagram / YouTube via Buffer | Platform/app quota, not your Google Cloud quota | fine at ≤2/day |
| Anti-spam | ≤2 posts/day, ≥3 h spacing, official APIs only, varied stories | risk reduced, not eliminated; no platform guarantees against reach limits |

Attention is only needed if a credential is revoked/expires, a platform changes
policy, OneSignal push subscription is lost, or feeds change shape. No monthly
login is required.

## Retention

- Public MP4s: 7 days (pruned after each upload).
- CI artifacts: 2 days.
- State records: 30 days (pruned after each write). Ledger holds URLs, IDs and
  hashes only — never article bodies or media bytes.

## Local checks

```bash
python -m pytest tests -q
python -m compileall pipeline generate.py pilot.py
python -c "import yaml; yaml.safe_load(open('.github/workflows/reels.yml', encoding='utf-8')); print('YAML OK')"
python generate.py --check
git grep -nE 'ghp_[A-Za-z0-9]|nfp_[A-Za-z0-9]|os_v2_[A-Za-z0-9]|buf_[A-Za-z0-9]' -- '*.md'  # no output
```

## Key files

`generate.py` orchestration · `config.yaml` settings · `pipeline/` stages ·
`netlify/functions/video.mjs` MP4 Range streaming · `notify_failure.py` alerts ·
`.github/workflows/reels.yml` schedule · `QUALITY_REPORT.md` verification record ·
`SECURITY.md` credential inventory · `CONTENT_POLICY.md` rights/retention.
