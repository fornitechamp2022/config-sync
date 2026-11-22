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

### Secret inventory (names only — never commit values)

**Netlify — state, uploads and public video delivery**

- `NETLIFY_AUTH_TOKEN` — Netlify personal access token. Used **twice**:
  - as an encrypted **GitHub Actions secret**, where the pinned `netlify-cli@24`
    uses it for the private `reel-state` ledger and for uploading MP4s to
    `reels-media`;
  - as a **Netlify site environment variable**, where
    [`netlify/functions/video.mjs`](netlify/functions/video.mjs) passes it (with
    the site ID) to `@netlify/blobs` so the public `/r/<ascii-key>` URL can
    stream the MP4 with HTTP Range support. Actions secrets are **not** visible
    to the deployed Function, so this site-level variable is required.
- `NETLIFY_SITE_ID` — site identifier (not secret, but kept alongside the token
  in both Actions and the Netlify site environment).
- `NETLIFY_AUTH_TOKEN` is currently configured in both places. **Rotate it** —
  see below.

**GitHub automation**

- `GH_PAT` — GitHub personal access token, stored as an encrypted Actions
  secret for optional external triggers/backups. The scheduled workflow itself
  needs no PAT: it uses the built-in `GITHUB_TOKEN` with `contents: read` and
  the Netlify keep-alive is an unauthenticated HTTPS GET. **Rotate it** — it was
  shared in chat.

**Failure notifications (OneSignal)**

- `ONESIGNAL_APP_ID` — public app identifier, configured.
- `ONESIGNAL_REST_API_KEY` — REST key used by `notify_failure.py`; configured,
  Web Push enabled, a device subscribed, and live delivery verified
  (`successful: 1, failed: 0`). **Rotate it** — it was shared in chat.

**Private state, optional Vercel fallback**

- `BLOB_STATE_TOKEN`, `BLOB_STATE_STORE_ID` — legacy Vercel Blob state backend
  (optional; Netlify Blobs is active).
- `BLOB_VIDEO_TOKEN`, `BLOB_VIDEO_STORE_ID` — optional Vercel Blob public MP4
  fallback (Netlify `reels-media` is active).

**Optional publishing**

- `YT_CLIENT_ID`, `YT_CLIENT_SECRET`, `YT_REFRESH_TOKEN` — YouTube Shorts.
- `IG_LONG_LIVED_TOKEN`, `IG_USER_ID` — Instagram Reels.

All of the optional values above are absent by default; the corresponding steps
skip cleanly and the workflow still produces MP4 artifacts.

## Rotation guidance

Rotate a credential by generating a **new** value at the provider, replacing the
corresponding encrypted Actions secret (and any Netlify site variable), and
revoking the old value. Prefer a short overlap window and re-run the workflow to
confirm. **The GitHub PAT, Netlify PAT and OneSignal REST key were shared in
chat and should be rotated first.**

- **GitHub PAT (`GH_PAT`):** revoke/delete the old token under GitHub settings →
  Developer settings → Personal access tokens, create a new one with the
  minimum scopes needed (this project does not need repo write access), then
  update the Actions secret. If a PAT was ever committed, revoke it immediately
  — git history is public.
- **Netlify PAT (`NETLIFY_AUTH_TOKEN`, with `NETLIFY_SITE_ID`):** create a new
  personal access token in Netlify user settings, replace **both** copies — the
  encrypted Actions secret *and* the Netlify site environment variable used by
  the video Function — then revoke the old token. Verify the Function still
  serves `/r/reels/<id>.mp4` with a `206` Range response. The site ID is an
  identifier, not a secret, but keep it consistent with the token's account.
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
git grep -nE 'ghp_[A-Za-z0-9]|nfp_[A-Za-z0-9]|os_v2_[A-Za-z0-9]|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' -- '*.md'
# expected: no output / non-zero exit status
```
