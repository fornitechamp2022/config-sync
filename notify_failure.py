"""Best-effort OneSignal push notification for a failed GitHub Actions run.

Contract:
  * Always exit 0. A broken notification must never mask the original failure.
  * Never print credential values (REST API key or App ID).
  * Retry once on transport errors or HTTP 5xx responses.
  * Skip quietly when credentials are missing/invalid (the App ID is not wired
    up yet) so the workflow still fails for the *real* reason.

This module deliberately uses ONLY the Python standard library. It runs in a
workflow ``if: failure()`` step, which must still work when
``pip install -r requirements.txt`` has failed -- including when third-party
packages such as ``requests`` were never installed at all.

The App ID is public by design but is treated as sensitive here too: nothing
that is printed contains either credential verbatim.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

ONE_SIGNAL_URL = "https://api.onesignal.com/notifications"
REQUEST_TIMEOUT = 10  # seconds
RETRY_DELAY = 1.0  # seconds between the two attempts
MAX_ATTEMPTS = 2


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _log(message: str, secrets: tuple[str, ...] = ()) -> None:
    """Print a message with known secret values scrubbed out."""
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    print(message, file=sys.stderr)


def _valid_app_id(value: str) -> bool:
    """OneSignal App IDs are UUIDs; placeholders like TODO are not valid."""
    if not value:
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def build_run_url(env: dict | None = None) -> str | None:
    """Return the GitHub Actions run URL, or None if context is incomplete."""
    env = os.environ if env is None else env
    server = (env.get("GITHUB_SERVER_URL") or "").rstrip("/")
    repo = env.get("GITHUB_REPOSITORY") or ""
    run_id = env.get("GITHUB_RUN_ID") or ""
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return None


def build_payload(
    app_id: str, test_mode: bool = False, env: dict | None = None
) -> dict:
    """Build the OneSignal request body for either a failure or a test push."""
    env = os.environ if env is None else env
    run_url = None if test_mode else build_run_url(env)

    if test_mode:
        heading = "YT Bolly - TEST (credential check)"
        contents = (
            "This is a test push from the YT Bolly generator. "
            "No action needed."
        )
    else:
        workflow = env.get("GITHUB_WORKFLOW") or "YT Bolly"
        repo = env.get("GITHUB_REPOSITORY") or ""
        subject = f"{workflow} failed" + (f" in {repo}" if repo else "")
        contents = (
            f"{subject}. View run: {run_url}"
            if run_url
            else f"{subject}. Check GitHub Actions for details."
        )
        heading = "YT Bolly: run failed"

    payload = {
        "app_id": app_id,
        "target_channel": "push",
        # "Subscribed Users" is broken on this app (OneSignal replies
        # "All included players are not subscribed" even for subscribed
        # players). Targeting a session count > -1 reaches all devices.
        "filters": [{"field": "session_count", "relation": ">", "value": "-1"}],
        "headings": {"en": heading},
        "contents": {"en": contents},
    }
    if run_url:
        payload["data"] = {"run_url": run_url}
    return payload


def _response_status(response) -> int:
    """Best-effort extraction of an HTTP status from a urlopen response."""
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        status = getcode() if callable(getcode) else None
    try:
        return int(status) if status is not None else 0
    except (TypeError, ValueError):
        return 0


def send_notification(payload: dict, api_key: str, secrets: tuple[str, ...] = ()) -> bool:
    """POST the notification, retrying once on network errors or HTTP 5xx.

    Uses only ``urllib.request`` / ``urllib.error`` / ``json`` so this works
    even when no third-party packages are installed. Returns True on success
    and False otherwise; never raises.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Key {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = None
        try:
            request = urllib.request.Request(
                ONE_SIGNAL_URL,
                data=body,
                headers=headers,
                method="POST",
            )
            response = urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT)
            status = _response_status(response)
        except urllib.error.HTTPError as exc:
            # urlopen raises HTTPError for non-2xx replies. Its text can echo
            # the URL/headers, so only the numeric status is logged below.
            status = getattr(exc, "code", 0) or 0
        except (urllib.error.URLError, OSError) as exc:
            # URLError covers connection/DNS failures; OSError covers timeouts
            # and socket/ssl errors (URLError is itself an OSError subclass).
            # Log only the exception *type*: its text could echo the request.
            _log(
                f"OneSignal: network error on attempt {attempt} "
                f"({type(exc).__name__}).",
                secrets,
            )
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_DELAY)
                continue
            _log("OneSignal: gave up after two attempts.", secrets)
            return False
        finally:
            if response is not None:
                close = getattr(response, "close", None)
                if callable(close):
                    close()

        # Only the status code is inspected: OneSignal answers HTTP 200 with an
        # empty body/id when there are no subscribers, which is still accepted.
        if 200 <= status < 300:
            _log(f"OneSignal: notification accepted (HTTP {status}).", secrets)
            return True
        if 500 <= status < 600 and attempt < MAX_ATTEMPTS:
            _log(f"OneSignal: HTTP {status}; retrying once.", secrets)
            time.sleep(RETRY_DELAY)
            continue
        _log(f"OneSignal: notification rejected (HTTP {status}); not retrying.", secrets)
        return False
    return False


def _run(argv: list[str] | None) -> int:
    parser = argparse.ArgumentParser(
        description="Send a OneSignal push about a failed YT Bolly run."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="send a clearly labelled test push (no run URL) to verify credentials",
    )
    args = parser.parse_args(argv)

    app_id = (os.environ.get("ONESIGNAL_APP_ID") or "").strip()
    api_key = (os.environ.get("ONESIGNAL_REST_API_KEY") or "").strip()
    secrets = (api_key, app_id)

    if not _valid_app_id(app_id):
        _log("OneSignal: App ID missing or invalid; skipping notification.", secrets)
        return 0
    if not api_key:
        _log("OneSignal: REST API key missing; skipping notification.", secrets)
        return 0

    send_notification(build_payload(app_id, test_mode=args.test), api_key, secrets)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point. Never lets a notification failure affect the exit code."""
    try:
        return _run(argv)
    except Exception as exc:  # defensive: notification is strictly best effort
        _log(f"OneSignal: unexpected error ({type(exc).__name__}); skipped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
