"""Offline unit tests for the Netlify Blobs state backend.

The Netlify CLI and ``shutil.which`` are mocked; no network calls and no real
credentials are used. Every recorded argv is inspected so a token can never
leak into a command string that might be logged.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import state as state_mod  # noqa: E402
from pipeline.state import (  # noqa: E402
    NETLIFY_STATE_STORE,
    RECORD_PREFIX,
    State,
)

TOKEN = "NETLIFY_SUPER_SECRET_TOKEN_9f3a7c"
SITE = "8a5d7b87-540f-41dd-adf9-1be70de414fe"


@pytest.fixture(autouse=True)
def clean_netlify_env(monkeypatch):
    """Do not let a developer's real Netlify credentials enter a test."""
    monkeypatch.delenv("NETLIFY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("NETLIFY_SITE_ID", raising=False)


def make_cfg(backend: str = "netlify") -> dict:
    return {"publish": {"state_backend": backend, "netlify_state_store": NETLIFY_STATE_STORE}}


class _Completed:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, args, returncode=0, stdout=""):
        self.args = list(args)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def install_fake_cli(monkeypatch, store: dict[str, str], calls: list[list[str]],
                     *, netlify=True, npx=True):
    """Install an in-memory Netlify CLI; record every argv it receives."""

    def which(name):
        if name == "netlify":
            return "/usr/bin/netlify" if netlify else None
        if name == "npx":
            return "/usr/bin/npx" if npx else None
        return None

    def run(args, **kwargs):
        argv = [str(a) for a in args]
        calls.append(argv)
        sub = next((a for a in argv if a.startswith("blobs:")), None)
        pos = argv.index(sub) if sub else -1
        if sub == "blobs:list":
            return _Completed(argv, stdout=json.dumps(
                [{"key": key, "storeName": NETLIFY_STATE_STORE} for key in store]
            ))
        if sub == "blobs:get":
            key = argv[pos + 2]
            out = argv[argv.index("--output") + 1]
            if key not in store:
                return _Completed(argv, returncode=1)
            Path(out).write_text(store[key], encoding="utf-8")
            return _Completed(argv)
        if sub == "blobs:set":
            key = argv[pos + 2]
            src = argv[argv.index("--input") + 1]
            store[key] = Path(src).read_text(encoding="utf-8")
            return _Completed(argv)
        return _Completed(argv, returncode=2)

    monkeypatch.setattr(state_mod.shutil, "which", which)
    monkeypatch.setattr(state_mod.subprocess, "run", run)
    return run


@pytest.fixture
def netlify_creds(monkeypatch):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("NETLIFY_SITE_ID", SITE)


# --------------------------------------------------------------------------
# load()
# --------------------------------------------------------------------------
def test_netlify_load_merges_records_and_skips_corrupt(
    netlify_creds, monkeypatch, tmp_path
):
    first = {
        "at": "2026-01-01T00:00:00+00:00", "runs": [{"id": "r1"}],
        "articles": {"u1": {"title": "T1", "title_key": "k1"}},
        "images": {}, "reels": [],
    }
    second = {
        "at": "2026-01-02T00:00:00+00:00", "runs": [{"id": "r2"}],
        "articles": {"u2": {"title": "T2", "title_key": "k2"}},
        "images": {"hash-2": {}}, "reels": [{"id": "reel-2"}],
    }
    store = {
        f"{RECORD_PREFIX}20260101T000000Z-aaa.json": json.dumps(first),
        f"{RECORD_PREFIX}20260102T000000Z-bbb.json": json.dumps(second),
        f"{RECORD_PREFIX}20260103T000000Z-ccc.json": "not json",  # corrupt
        "unrelated/keep.json": json.dumps({"articles": {"bad": {}}}),
    }
    calls: list[list[str]] = []
    install_fake_cli(monkeypatch, store, calls)

    state = State(make_cfg(), tmp_path)
    data = state.load()

    assert set(data["articles"]) == {"u1", "u2"}  # corrupt + unrelated skipped
    assert {r["id"] for r in data["runs"]} == {"r1", "r2"}
    assert len(data["reels"]) == 1
    assert state.seen_image("hash-2")
    assert not (tmp_path / "state" / "ledger.json").exists()

    listing = [c for c in calls if "blobs:list" in c]
    assert listing and "--prefix" in listing[0] and RECORD_PREFIX in listing[0]
    assert "--json" in listing[0]
    assert NETLIFY_STATE_STORE in listing[0]
    assert any("blobs:get" in c for c in calls)


def test_netlify_load_accepts_wrapped_json_list(netlify_creds, monkeypatch, tmp_path):
    record = {"at": "2026-02-01T00:00:00+00:00", "articles": {"u": {"title": "T"}}}
    store = {f"{RECORD_PREFIX}one.json": json.dumps(record)}
    calls: list[list[str]] = []
    install_fake_cli(monkeypatch, store, calls)

    def run(args, **kwargs):
        argv = [str(a) for a in args]
        calls.append(argv)
        if "blobs:list" in argv:
            return _Completed(argv, stdout=json.dumps({"blobs": [{"key": k} for k in store]}))
        if "blobs:get" in argv:
            out = argv[argv.index("--output") + 1]
            Path(out).write_text(store[argv[argv.index("blobs:get") + 2]], encoding="utf-8")
            return _Completed(argv)
        return _Completed(argv, returncode=2)

    monkeypatch.setattr(state_mod.subprocess, "run", run)

    state = State(make_cfg(), tmp_path)
    assert "u" in state.load()["articles"]


# --------------------------------------------------------------------------
# record_run()
# --------------------------------------------------------------------------
def test_netlify_record_run_is_unique_immutable_and_metadata_only(
    netlify_creds, monkeypatch, tmp_path
):
    store: dict[str, str] = {}
    calls: list[list[str]] = []
    install_fake_cli(monkeypatch, store, calls)

    state = State(make_cfg(), tmp_path)
    state.record_run({
        "articles": {"u": {"title": "T"}},
        "images": {"h": {"thumbnail": b"\x00\x01\x02"}},
        "reels": [],
    })
    state.record_run({"articles": {"u2": {"title": "T2"}}})

    assert len(store) == 2
    keys = sorted(store)
    assert keys[0] != keys[1]
    for key in keys:
        assert key.startswith(RECORD_PREFIX) and key.endswith(".json")
        assert re.match(r"state/runs/\d{8}T\d{6}Z-[0-9a-f]{12}\.json$", key)

    set_calls = [c for c in calls if "blobs:set" in c]
    assert len(set_calls) == 2
    assert all("--force" in c and "--input" in c for c in set_calls)
    assert all(NETLIFY_STATE_STORE in c for c in set_calls)

    first_key = set_calls[0][set_calls[0].index("blobs:set") + 2]
    payload = json.loads(store[first_key])
    assert payload["images"]["h"] == {}  # binary payload stripped
    assert payload["version"] == 1
    assert payload["runs"][0]["id"] and payload["runs"][0]["at"]
    assert not (tmp_path / "state" / "ledger.json").exists()  # no local write


def test_netlify_record_run_falls_back_to_npx(netlify_creds, monkeypatch, tmp_path):
    store: dict[str, str] = {}
    calls: list[list[str]] = []
    install_fake_cli(monkeypatch, store, calls, netlify=False, npx=True)

    State(make_cfg(), tmp_path).record_run({"articles": {"u": {"title": "T"}}})

    assert len(calls) == 1
    assert calls[0][:3] == ["/usr/bin/npx", "--no-install", "netlify"]
    assert len(store) == 1


# --------------------------------------------------------------------------
# fallback + credential hygiene
# --------------------------------------------------------------------------
@pytest.mark.parametrize("token,site,netlify,npx", [
    (TOKEN, None, True, True),     # site id missing
    (None, SITE, True, True),      # auth token missing
    (TOKEN, SITE, False, False),   # credentials present, no CLI at all
])
def test_netlify_missing_creds_or_cli_falls_back_to_local(
    monkeypatch, tmp_path, token, site, netlify, npx
):
    store: dict[str, str] = {}
    calls: list[list[str]] = []
    install_fake_cli(monkeypatch, store, calls, netlify=netlify, npx=npx)
    if token:
        monkeypatch.setenv("NETLIFY_AUTH_TOKEN", token)
    if site:
        monkeypatch.setenv("NETLIFY_SITE_ID", site)

    state = State(make_cfg(), tmp_path)
    state.record_run({"outcome": "noop", "articles": {"u": {"title": "T"}}})

    ledger = tmp_path / "state" / "ledger.json"
    assert ledger.is_file()
    assert "u" in json.loads(ledger.read_text(encoding="utf-8"))["articles"]
    assert store == {}
    assert calls == []


def test_netlify_commands_never_contain_token_or_auth_flag(
    netlify_creds, monkeypatch, tmp_path
):
    store: dict[str, str] = {}
    calls: list[list[str]] = []
    install_fake_cli(monkeypatch, store, calls)

    state = State(make_cfg(), tmp_path)
    state.load()
    state.record_run({"articles": {"u": {"title": "T"}}})

    assert calls
    for argv in calls:
        assert TOKEN not in " ".join(argv)
        assert "--auth" not in argv
