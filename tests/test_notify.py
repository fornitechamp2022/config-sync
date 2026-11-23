"""Offline unit tests for the OneSignal failure notifier and workflow wiring.

All HTTP traffic is mocked at the stdlib ``urllib.request.urlopen`` layer: no
network calls and no real credentials are used. The notifier itself must be
importable with no third-party packages installed, so nothing here patches or
imports ``requests`` in the module under test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
from pathlib import Path

import pytest
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
    """Minimal stand-in for the object returned by urllib.request.urlopen."""

    def __init__(self, status_code: int, body: bytes = b'{"id": "abc"}'):
        self.status = status_code
        self._body = body
        self.closed = False

    def getcode(self):
        return self.status

    def read(self):
        return self._body

    def close(self):
        self.closed = True


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
    monkeypatch.setenv("GITHUB_WORKFLOW", "YT Bolly")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")


def capture_posts(monkeypatch, responses):
    """Record (request, timeout) for each stdlib urlopen call.

    Real ``urlopen`` raises ``HTTPError`` for non-2xx statuses, so the fake
    reproduces that behaviour from the queued FakeResponse objects. Queued
    exceptions (e.g. URLError) are raised verbatim to simulate transport
    failures.
    """
    calls = []
    queue = list(responses)

    def fake_urlopen(request, timeout=None):
        calls.append((request, timeout))
        result = queue.pop(0)
        if isinstance(result, BaseException):
            raise result
        status = getattr(result, "status", 0)
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url, status, "error", {}, None
            )
        return result

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
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

    request, timeout = calls[0]
    assert request.full_url == "https://api.onesignal.com/notifications"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Key {API_KEY}"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == notify.REQUEST_TIMEOUT

    payload = json.loads(request.data.decode("utf-8"))
    assert payload["app_id"] == APP_ID
    assert payload["target_channel"] == "push"
    assert payload["filters"] == [
        {"field": "session_count", "relation": ">", "value": "-1"}
    ]
    assert "included_segments" not in payload
    assert payload["headings"]["en"]
    assert "failed" in payload["contents"]["en"].lower()
    assert RUN_URL in payload["contents"]["en"]
    assert payload["data"] == {"run_url": RUN_URL}


def test_run_url_is_omitted_when_context_incomplete(monkeypatch):
    set_creds(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(200)])

    assert notify.main([]) == 0
    payload = json.loads(calls[0][0].data.decode("utf-8"))
    assert "data" not in payload
    assert "https://github.com" not in payload["contents"]["en"]


FILTERS = [{"field": "session_count", "relation": ">", "value": "-1"}]


def test_build_payload_targets_all_devices_with_filters():
    env = {
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "owner/repo",
        "GITHUB_RUN_ID": "42",
        "GITHUB_WORKFLOW": "YT Bolly",
    }
    failure = notify.build_payload(APP_ID, env=env)
    assert failure["target_channel"] == "push"
    assert failure["filters"] == FILTERS
    assert "included_segments" not in failure
    assert failure["data"] == {"run_url": RUN_URL}

    test = notify.build_payload(APP_ID, test_mode=True, env=env)
    assert test["target_channel"] == "push"
    assert test["filters"] == FILTERS
    assert "included_segments" not in test
    assert "data" not in test


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
        [urllib.error.URLError("connection refused"), FakeResponse(200)],
    )

    assert notify.main([]) == 0
    assert len(calls) == 2


def test_4xx_is_not_retried(monkeypatch):
    set_creds(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(400), FakeResponse(200)])

    assert notify.main([]) == 0
    assert len(calls) == 1


def test_http_200_with_empty_body_is_accepted(monkeypatch):
    """OneSignal returns 200 with no id when there are no subscribers."""
    set_creds(monkeypatch)
    calls = capture_posts(
        monkeypatch, [FakeResponse(200, body=b'{"id": ""}')]
    )

    assert notify.send_notification(
        notify.build_payload(APP_ID), API_KEY
    ) is True
    assert len(calls) == 1


# --------------------------------------------------------------------------
# --test mode
# --------------------------------------------------------------------------
def test_test_mode_payload_is_labelled_and_has_no_run_url(monkeypatch):
    set_creds(monkeypatch)
    set_github_context(monkeypatch)
    calls = capture_posts(monkeypatch, [FakeResponse(200)])

    assert notify.main(["--test"]) == 0
    payload = json.loads(calls[0][0].data.decode("utf-8"))
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
        [urllib.error.URLError(f"boom {API_KEY} {APP_ID}"), FakeResponse(200)],
    )

    assert notify.main([]) == 0
    assert len(calls) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert API_KEY not in output
    assert APP_ID not in output


def test_unexpected_error_still_exits_zero(monkeypatch, capsys):
    set_creds(monkeypatch)
    calls = capture_posts(
        monkeypatch, [RuntimeError(f"unexpected {API_KEY}")]
    )

    assert notify.main([]) == 0
    assert len(calls) == 1
    captured = capsys.readouterr()
    assert API_KEY not in captured.out + captured.err


# --------------------------------------------------------------------------
# No third-party packages (regression for workflow run 45058391453)
# --------------------------------------------------------------------------
def test_imports_and_runs_without_requests_installed():
    """The failure step must work when pip install failed and requests is absent.

    A sitecustomize hook makes any ``import requests`` raise ImportError in a
    fresh subprocess. The notifier still has to import, run and exit 0.
    """
    with tempfile.TemporaryDirectory() as tmp:
        blocker = Path(tmp) / "sitecustomize.py"
        blocker.write_text(
            "import sys\n"
            "class _BlockThirdParty:\n"
            "    def find_spec(self, fullname, path=None, target=None):\n"
            "        if fullname.split('.')[0] == 'requests':\n"
            "            raise ImportError(\n"
            "                'third-party import blocked: ' + fullname\n"
            "            )\n"
            "        return None\n"
            "sys.meta_path.insert(0, _BlockThirdParty())\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        for key in ENV_KEYS:
            env.pop(key, None)
        env["PYTHONPATH"] = tmp + os.pathsep + str(ROOT)

        result = subprocess.run(
            [sys.executable, str(ROOT / "notify_failure.py"), "--test"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "ImportError" not in result.stderr
    assert API_KEY not in result.stdout + result.stderr


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
    # Non-secret configuration is injected from an Actions variable; no
    # literal site URL (and no credential) belongs in the workflow file.
    assert keepalive["env"]["NETLIFY_SITE_URL"] == "${{ vars.NETLIFY_SITE_URL }}"
    assert '"$NETLIFY_SITE_URL"' in keepalive["run"]
    assert "|| true" in keepalive["run"]
    assert "--fail" in keepalive["run"]

    # No production deploy was introduced (that would cost build credits).
    assert not any("deploy" in run and "netlify" in run.lower() for run in runs)

    # Existing steps and secrets remain intact.
    assert data["name"] == "YT Bolly"
    assert any("hyperframes@0.9.77" in run for run in runs)
    assert any("netlify-cli@24" in run for run in runs)
    generate_env = next(step for step in steps if step.get("name") == "Generate reel")["env"]
    for secret in (
        "NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID", "BLOB_STATE_TOKEN",
        "BLOB_STATE_STORE_ID", "BLOB_VIDEO_TOKEN", "BLOB_VIDEO_STORE_ID",
        "YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN",
        "IG_LONG_LIVED_TOKEN", "IG_USER_ID",
        "BUFFER_API_KEY", "BUFFER_YT_CHANNEL_ID", "BUFFER_IG_CHANNEL_ID",
    ):
        assert generate_env[secret] == "${{ secrets.%s }}" % secret
