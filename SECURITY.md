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
secrets are configured as encrypted **GitHub Actions secrets** and injected
into jobs as environment variables only at run time. The workflow declares
`permissions: contents: read`, writes no git state, and never places tokens in
command-line arguments or logs (the Netlify CLI reads its credentials from the
environment). Tests use dummy placeholder strings only — never real values.

### Secret names used (names only — never commit values)

State (private):

- `NETLIFY_AUTH_TOKEN`
- `NETLIFY_SITE_ID`
- `BLOB_STATE_TOKEN`
- `BLOB_STATE_STORE_ID`

Public MP4 hosting (Vercel Blob):

- `BLOB_VIDEO_TOKEN`
- `BLOB_VIDEO_STORE_ID`

Publishing:

- `YT_CLIENT_ID`
- `YT_CLIENT_SECRET`
- `YT_REFRESH_TOKEN`
- `IG_LONG_LIVED_TOKEN`
- `IG_USER_ID`

Failure notifications (OneSignal — step present in `.github/workflows/reels.yml`;
both secrets configured, Web Push enabled, device subscribed, live delivery
verified):

- `ONESIGNAL_APP_ID` (configured and verified)
- `ONESIGNAL_REST_API_KEY` (the REST API key used by the notification step)

Automation PAT (informational — **no PAT is currently used or required**): the
workflow uses only the built-in `GITHUB_TOKEN` with `contents: read`, and the
Netlify keep-alive is an unauthenticated HTTPS GET. If a PAT is ever added for
external triggers, name it clearly (e.g. `GH_PAT` / `KEEPALIVE_PAT`) and rotate
it as described below.

## Rotation guidance

Rotate a credential by generating a **new** value at the provider, replacing the
corresponding encrypted Actions secret in repository settings, and revoking the
old value. Prefer a short overlap window and re-run the workflow to confirm.

- **GitHub PAT (personal access token):** revoke/delete the old token under
  GitHub settings → Developer settings → Personal access tokens, create a new
  one with the minimum scopes needed (the keep-alive/automation trigger does
  not need repo write access for this project), then update the Actions secret.
  If a PAT was ever committed, revoke it immediately — git history is public.
- **Netlify PAT (`NETLIFY_AUTH_TOKEN`, plus `NETLIFY_SITE_ID`):** create a new
  personal access token in Netlify user settings, update the Actions secret,
  then revoke the old token. The site ID is an identifier, not a secret, but
  keep it consistent with the token's account.
- **OneSignal key (`ONESIGNAL_REST_API_KEY`, with `ONESIGNAL_APP_ID`):** regenerate
  the REST API key in the OneSignal dashboard (App Settings → Keys & IDs),
  update the Actions secret, and revoke the previous key. The App ID is public
  metadata; the REST key is the credential that must be protected.
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
