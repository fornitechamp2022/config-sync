# Security policy

## Reporting a vulnerability or a secrets exposure

**Please do not open a public GitHub/GitLab issue for a security problem.**

- Use GitHub's **private vulnerability reporting** ("Security" tab →
  "Report a vulnerability") for bugs, or contact the maintainers privately.
- If you believe a token, key or password has been committed or exposed,
  report it immediately through that private channel, then **rotate the
  credential first** and tell us which secret and roughly when it leaked.
- Include enough detail to reproduce (file, line, command or workflow step).
  Do not paste live secret values into reports, issues or logs.

You should expect an acknowledgement within a few days. Because this is a
public repository, anything posted in an issue or commit is visible to anyone
— treat every credential as compromised the moment it appears in public.

## Secret handling in this project

**No PAT, token, key or other credential is stored in this repository.** All
values live either as encrypted **GitHub Actions secrets** (injected into jobs
as environment variables only at run time) or as **Netlify site environment
variables** for the public video Function. The workflow declares
`permissions: contents: read`, writes no git state, and never places tokens in
command-line arguments or logs (the Netlify CLI reads `NETLIFY_AUTH_TOKEN` from
the environment, and the Function reads its own site variables). Tests use
dummy placeholder strings only — never real values. A local backup of the
credential values exists outside the repository and is never committed.

**After initial setup the automation needs no GitHub PAT and no Netlify PAT.**
The built-in `GITHUB_TOKEN` with `contents: read` is enough for CI; a GitHub PAT
was only used once to create and push the repository. The Netlify token is a
**long-lived, user-supplied token** used only for private Blob state/media
reads and writes — **no Netlify deploy is performed**. Buffer and OneSignal keys
are long-lived too, so **no monthly login or renewal is required**: attention is
needed only if a credential is revoked/expires or a platform changes policy.
Publishing is rate-limited in code (≤2 posts/day, ≥3h spacing, varied stories,
official APIs only) as risk reduction, but no service can guarantee a platform
will never limit reach.

### Secret inventory (names only — never commit values)

**Netlify — state blobs and public video delivery (no deploys)**

- `NETLIFY_AUTH_TOKEN` — **long-lived, user-supplied** Netlify token. Used
  **twice**, only for private Blob reads/writes — never for a deploy:
  - as an encrypted **GitHub Actions secret**, where the pinned `netlify-cli@24`
    uses it for the private `reel-state` state blobs and for uploading MP4s to
    `reels-media`;
  - as a **Netlify site environment variable**, where
    [`netlify/functions/video.mjs`](netlify/functions/video.mjs) passes it (with
    the site ID) to `@netlify/blobs` so the public `/r/<ascii-key>` URL can
    stream the MP4 with HTTP Range support. Actions secrets are **not** visible
    to the Function, so this site-level variable is required.
- `NETLIFY_SITE_ID` — site identifier (not secret, but kept alongside the token
  in both Actions and the Netlify site environment).
- `NETLIFY_AUTH_TOKEN` is configured in both places. It is long-lived and needs
  no monthly rotation; rotate only if revoked, expired or suspected exposed.

**GitHub automation (no PAT in the automation)**

- `GH_PAT` — GitHub personal access token. **Not used by the automation after
  setup** — it was only for initial repository creation/push. The scheduled
  workflow uses the built-in `GITHUB_TOKEN` with `contents: read`, writes no git
  state, and the Netlify keep-alive is an unauthenticated HTTPS GET. No GitHub
  PAT is needed for any scheduled or manual run.

**Buffer publishing (primary — official API)**

- `BUFFER_API_KEY` — long-lived personal Buffer API key. No monthly login and
  no Meta/Google app verification.
- `BUFFER_YT_CHANNEL_ID` — Buffer channel id for the connected YouTube account.
- `BUFFER_IG_CHANNEL_ID` — Buffer channel id for the connected Instagram
  account.
- All three are encrypted Actions secrets injected into the *Generate reel*
  step. The key is a credential; the channel ids are identifiers. No Buffer
  value is ever committed or printed in logs.

**Failure notifications (OneSignal)**

- `ONESIGNAL_APP_ID` — public app identifier, configured.
- `ONESIGNAL_REST_API_KEY` — long-lived REST key used by `notify_failure.py`;
  configured, Web Push enabled, a device subscribed, and live delivery verified
  (`successful: 1, failed: 0`). Alerts are **failure-only** (`if: failure()`);
  there are no success notifications. Rotate only if revoked/expired or
  suspected exposed.

**Private state, optional Vercel fallback**

- `BLOB_STATE_TOKEN`, `BLOB_STATE_STORE_ID` — legacy Vercel Blob state backend
  (optional; Netlify Blobs is active).
- `BLOB_VIDEO_TOKEN`, `BLOB_VIDEO_STORE_ID` — optional Vercel Blob public MP4
  fallback (Netlify `reels-media` is active).

**Optional direct-publishing fallback**

- `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` — direct YouTube Shorts
  fallback (Buffer is the primary publisher).
- `IG_LONG_LIVED_TOKEN`, `IG_USER_ID` — direct Instagram Reels fallback.

All of the optional values above are absent by default; the corresponding steps
skip cleanly, Buffer remains the primary publisher, and the workflow still
produces MP4 artifacts.

## Rotation guidance

All long-lived credentials (Buffer, OneSignal, Netlify, GitHub) require **no
monthly rotation**. Rotate one only when it is revoked, expires, or is suspected
exposed: generate a new value at the provider, replace the encrypted Actions
secret (and any Netlify site variable), and revoke the old value. Prefer a short
overlap window and re-run the workflow to confirm. The GitHub PAT is no longer
used by the automation, so it does not need rotation for CI to keep working.

- **GitHub PAT (`GH_PAT`):** no longer used by the automation; keep it only for
  repo administration/backups. If it was ever exposed or committed, revoke it
  immediately (git history is public) and re-issue only if you still need it.
  CI itself uses the built-in `GITHUB_TOKEN` with `contents: read`.
- **Netlify token (`NETLIFY_AUTH_TOKEN`, with `NETLIFY_SITE_ID`):** this is a
  long-lived, user-supplied token used only for Blob state/media — **not for
  deploys**. If it must be rotated, create a new token in Netlify user settings,
  replace **both** copies — the encrypted Actions secret *and* the Netlify site
  environment variable used by the video Function — then revoke the old token.
  Verify the Function still serves `/r/reels/<id>.mp4` with a `206` Range
  response. The site ID is an identifier, not a secret, but keep it consistent
  with the token's account.
- **Buffer (`BUFFER_API_KEY`, with `BUFFER_YT_CHANNEL_ID` /
  `BUFFER_IG_CHANNEL_ID`):** the key is long-lived; rotate it in the Buffer
  dashboard if it is revoked or suspected exposed, then update the Actions
  secret. The channel ids are identifiers and change only if you reconnect a
  social account. No monthly login or app verification is involved.
- **OneSignal key (`ONESIGNAL_REST_API_KEY`, with `ONESIGNAL_APP_ID`):** regenerate
  the REST API key in the OneSignal dashboard (App Settings → Keys & IDs),
  update the Actions secret, revoke the previous key, and re-run
  `notify_failure.py --test`. The App ID is public metadata; the REST key is the
  credential that must be protected.
- **Vercel (`BLOB_VIDEO_TOKEN`, `BLOB_STATE_TOKEN`, plus store IDs):** create a
  new Blob store token with least privilege (read/write scoped to the relevant
  store), update the Actions secrets, then revoke the old token. Re-run the
  pipeline to confirm upload/state still work; store IDs are identifiers.
- **YouTube (`YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN`):** rotate
  the OAuth client secret in Google Cloud Console and re-issue the refresh
  token through the configured OAuth consent flow, then update all three
  secrets together. A rotated client secret with an old refresh token will fail
  — replace them as a set.
- **Instagram (`IG_LONG_LIVED_TOKEN`, `IG_USER_ID`):** regenerate the long-lived
  Graph API token in Meta's developer tools (short-lived → long-lived exchange),
  update the Actions secret, and verify the token's expiry. The user ID is an
  identifier. Instagram requires a Professional account linked to a Facebook
  Page.

After any rotation, remove the exposed value from git history if it was ever
committed (e.g. with `git filter-repo`/BFG) and force-push, then ask the host to
purge cached views. Rotating the live credential is always the first priority.

## Keeping the Netlify app active

The scheduled workflow includes a keep-alive step that makes a plain HTTPS GET
to the Netlify site so it is not paused for inactivity. It requires **no
credential at all**, uses no build minutes, and does not write to git. Failure
alerting is handled by `notify_failure.py` (OneSignal), which exits 0 on all
errors, retries once on transport/5xx failures, and never prints credential
values.

## Verifying no secrets are committed

```bash
git grep -nE 'ghp_[A-Za-z0-9]|nfp_[A-Za-z0-9]|os_v2_[A-Za-z0-9]|buf_[A-Za-z0-9]|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' -- '*.md'
# expected: no output / non-zero exit status
```
