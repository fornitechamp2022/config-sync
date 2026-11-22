"""Offline unit tests for the OneSignal failure notifier and workflow wiring.

All HTTP traffic is mocked: no network calls and no real credentials are used.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import notify_failure as notify  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "reels.yml"

APP_ID = "12345678-1234-4234-8234-1234567890ab"
API_KEY = "super-secret-rest-key-abcdef"
RUN_URL = "https://github.com/owner/repo/actions/runs/42"

ENV_KEYS = (
    "ONESIGNAL_APP_ID",
    "ONESIGNAL_REST_API_KEY",
    "GITHUB_REPOSITORY",
    "GITHUB_RUN_ID",
    "GITHUB_WORKFLOW",
    "GITHUB_SERVER_URL",
)


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Never let a developer's real credentials leak into a test."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Keep retry tests instant while still exercising the retry path.
    monkeypatch.setattr(notify.time, "sleep", lambda _seconds: None)


def set_creds(monkeypatch, app_id=APP_ID, api_key=API_KEY):
    if app_id is not None:
        monkeypatch.setenv("ONESIGNAL_APP_ID", app_id)
    if api_key is not None:
        monkeypatch.setenv("ONESIGNAL_REST_API_KEY", api_key)


def set_github_context(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_WORKFLOW", "Bollywood Reels")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")


def capture_posts(monkeypatch, responses):
    """Return a list that records (url, kwargs) for each call to requests.post."""
    calls = []
    queue = list(responses)

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(notify.requests, "post", fake_post)
    return calls


# --------------------------------------------------------------------------
# Endpoint / headers / body
# --------------------------------------------------------------------------
def test_failure_post_uses_correct_endpoint_headers_and_body(monkeypatch):
    set_creds(monkeypatch)
    set_github_context(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(200)])

    assert notify.main([]) == 0
    assert len(calls) == 1

    url, kwargs = calls[0]
    assert url == "https://api.onesignal.com/notifications"
    assert kwargs["headers"] == {"Authorization": f"Key {API_KEY}"}
    assert kwargs["timeout"] == notify.REQUEST_TIMEOUT

    payload = kwargs["json"]
    assert payload["app_id"] == APP_ID
    assert payload["target_channel"] == "push"
    assert payload["included_segments"] == ["Subscribed Users"]
    assert payload["headings"]["en"]
    assert "failed" in payload["contents"]["en"].lower()
    assert RUN_URL in payload["contents"]["en"]
    assert payload["data"] == {"run_url": RUN_URL}


def test_run_url_is_omitted_when_context_incomplete(monkeypatch):
    set_creds(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(200)])

    assert notify.main([]) == 0
    payload = calls[0][1]["json"]
    assert "data" not in payload
    assert "https://github.com" not in payload["contents"]["en"]


# --------------------------------------------------------------------------
# Missing / invalid credentials
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "app_id, api_key",
    [
        (None, API_KEY),       # missing App ID
        ("", API_KEY),         # blank App ID
        ("YOUR_APP_ID", API_KEY),  # placeholder App ID, not a UUID
        (APP_ID, None),        # missing API key
        (APP_ID, ""),          # blank API key
        (None, None),          # nothing configured
    ],
)
def test_missing_or_invalid_credentials_skip_post(monkeypatch, app_id, api_key):
    set_creds(monkeypatch, app_id, api_key)
    calls = capture_posts(monkeypatch, [])

    assert notify.main([]) == 0
    assert calls == []


# --------------------------------------------------------------------------
# Retry behaviour
# --------------------------------------------------------------------------
def test_retries_once_on_5xx(monkeypatch):
    set_creds(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(500), FakeResponse(200)])

    assert notify.main([]) == 0
    assert len(calls) == 2


def test_gives_up_after_two_failed_5xx_attempts(monkeypatch):
    set_creds(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(503), FakeResponse(500)])

    assert notify.main([]) == 0
    assert len(calls) == 2


def test_retries_once_on_network_error(monkeypatch):
    set_creds(monkeypatch)
    calls = capture_posts(
        monkeypatch,
        [requests.ConnectionError("connection refused"), FakeResponse(200)],
    )

    assert notify.main([]) == 0
    assert len(calls) == 2


def test_4xx_is_not_retried(monkeypatch):
    set_creds(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(400), FakeResponse(200)])

    assert notify.main([]) == 0
    assert len(calls) == 1


# --------------------------------------------------------------------------
# --test mode
# --------------------------------------------------------------------------
def test_test_mode_payload_is_labelled_and_has_no_run_url(monkeypatch):
    set_creds(monkeypatch)
    set_github_context(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(200)])

    assert notify.main(["--test"]) == 0
    payload = calls[0][1]["json"]
    assert "TEST" in payload["headings"]["en"]
    assert "test" in payload["contents"]["en"].lower()
    assert "data" not in payload
    assert RUN_URL not in payload["contents"]["en"]
    assert "github.com" not in payload["contents"]["en"]


# --------------------------------------------------------------------------
# Secret hygiene
# --------------------------------------------------------------------------
def test_secrets_never_appear_in_output(monkeypatch, capsys):
    set_creds(monkeypatch)
    # Even an exception message that embeds the credentials must not leak.
    calls = capture_posts(
        monkeypatch,
        [requests.ConnectionError(f"boom {API_KEY} {APP_ID}"), FakeResponse(200)],
    )

    assert notify.main([]) == 0
    assert len(calls) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert API_KEY not in output
    assert APP_ID not in output


def test_unexpected_error_still_exits_zero(monkeypatch, capsys):
    set_creds(monkeypatch)

    def exploding_post(url, **kwargs):
        raise RuntimeError(f"unexpected {API_KEY}")

    monkeypatch.setattr(notify.requests, "post", exploding_post)
    assert notify.main([]) == 0
    captured = capsys.readouterr()
    assert API_KEY not in captured.out + captured.err


# --------------------------------------------------------------------------
# Workflow wiring
# --------------------------------------------------------------------------
def test_workflow_wiring():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = data["jobs"]["generate"]["steps"]
    runs = [step.get("run", "") for step in steps]

    notify_step = next(step for step in steps if step.get("name") == "Notify on failure")
    assert notify_step["if"] == "failure()"
    assert "python notify_failure.py" in notify_step["run"]
    assert notify_step["env"]["ONESIGNAL_APP_ID"] == "${{ secrets.ONESIGNAL_APP_ID }}"
    assert (
        notify_step["env"]["ONESIGNAL_REST_API_KEY"]
        == "${{ secrets.ONESIGNAL_REST_API_KEY }}"
    )

    keepalive = next(step for step in steps if "Keep Netlify" in step.get("name", ""))
    assert "https://bollywood-reel-generator.netlify.app" in keepalive["run"]
    assert "|| true" in keepalive["run"]
    assert "--fail" in keepalive["run"]

    # No production deploy was introduced (that would cost build credits).
    assert not any("deploy" in run and "netlify" in run.lower() for run in runs)

    # Existing steps and secrets remain intact.
    assert any("hyperframes@0.9.77" in run for run in runs)
    assert any("netlify-cli@24" in run for run in runs)
    generate_env = next(step for step in steps if step.get("name") == "Generate reel")["env"]
    for secret in (
        "NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID", "BLOB_STATE_TOKEN",
        "BLOB_STATE_STORE_ID", "BLOB_VIDEO_TOKEN", "BLOB_VIDEO_STORE_ID",
        "YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN",
        "IG_LONG_LIVED_TOKEN", "IG_USER_ID",
    ):
        assert generate_env[secret] == "${{ secrets.%s }}" % secret
