"""Offline unit tests for state, publishing and the workflow YAML.

No network calls: Vercel Blob, requests and time.sleep are all mocked.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import publish  # noqa: E402
from pipeline.state import State  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "reels.yml"

ENV_KEYS = (
    "BLOB_STATE_TOKEN", "BLOB_STATE_STORE_ID",
    "BLOB_VIDEO_TOKEN", "BLOB_VIDEO_STORE_ID",
    "YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN",
    "IG_LONG_LIVED_TOKEN", "IG_USER_ID",
)


# --------------------------------------------------------------------------
# fixtures / fakes
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Never let a developer's real credentials leak into a test."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def make_cfg(backend: str = "local") -> dict:
    return {
        "publish": {
            "state_backend": backend,
            "youtube": True,
            "instagram": True,
            "verification_required": True,
        }
    }


class _Creds:
    def __init__(self, token=None, store_id=None):
        self.token = token
        self.store_id = store_id


class _Opts:
    def __init__(self, credentials_factory=None):
        self.credentials_factory = credentials_factory


class _Session:
    def __init__(self, service_options=None):
        self.service_options = service_options

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _Item:
    def __init__(self, pathname, uploaded_at=None):
        self.pathname = pathname
        self.uploaded_at = uploaded_at


class _FakeBlob:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploaded_at: dict[str, datetime] = {}
        self.puts: list[tuple] = []
        self.deleted: list[str] = []
        self.access_reads: list[tuple] = []

    def query_blobs(self, *, prefix=None, **kwargs):
        return [
            _Item(path, self.uploaded_at.get(path))
            for path in sorted(self.objects)
            if not prefix or path.startswith(prefix)
        ]

    def get_blob(self, url_or_pathname, *, access):
        self.access_reads.append((url_or_pathname, access))
        return types.SimpleNamespace(body=self.objects[url_or_pathname])

    def put_blob(self, pathname, body, *, access, **kwargs):
        self.puts.append((pathname, access, kwargs))
        self.objects[pathname] = body
        return types.SimpleNamespace(
            url=f"https://blob.example/{pathname}", pathname=pathname
        )

    def delete_blob(self, url_or_pathname, **kwargs):
        self.deleted.append(url_or_pathname)
        self.objects.pop(url_or_pathname, None)


@pytest.fixture
def fake_vercel(monkeypatch):
    """Install a minimal in-memory Vercel Blob SDK into sys.modules."""
    blob = _FakeBlob()
    vercel = types.ModuleType("vercel")
    api = types.ModuleType("vercel.api")
    blob_pkg = types.ModuleType("vercel.blob")
    sync = types.ModuleType("vercel.blob.sync")

    api.session = _Session
    sync.BlobCredentials = _Creds
    sync.SyncBlobServiceOptions = _Opts
    sync.put_blob = blob.put_blob
    sync.get_blob = blob.get_blob
    sync.query_blobs = blob.query_blobs
    sync.delete_blob = blob.delete_blob
    blob_pkg.sync = sync

    for name, module in (
        ("vercel", vercel),
        ("vercel.api", api),
        ("vercel.blob", blob_pkg),
        ("vercel.blob.sync", sync),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return blob


class _Resp:
    def __init__(self, status_code=200, *, text="", headers=None, payload=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return self._payload


# --------------------------------------------------------------------------
# State: dedupe
# --------------------------------------------------------------------------
def test_local_dedupe_by_url_title_and_jaccard(tmp_path):
    state = State(make_cfg("local"), tmp_path)
    state.data["articles"] = {
        "url-1": {"title": "Salman Khan hosts Bigg Boss 20 finale", "title_key": "key-1"},
        "url-2": {"title": "Bollywood actor signs a new film", "title_key": "key-2"},
    }

    # Same canonical URL key.
    assert state.is_seen({"url_key": "url-1", "title_key": "x", "title": "Unrelated"})
    # Same normalized title key, different URL.
    assert state.is_seen({"url_key": "x", "title_key": "key-1", "title": "Unrelated"})
    # Similar title (Jaccard well above 0.6), new keys.
    assert state.is_seen({
        "url_key": "x", "title_key": "x",
        "title": "Salman Khan hosts Bigg Boss 20 grand finale",
    })
    # Genuinely different story.
    assert not state.is_seen({
        "url_key": "x", "title_key": "x", "title": "Cricket team wins the trophy",
    })
    # Empty candidate must never be treated as seen.
    assert not state.is_seen({})


def test_seen_image(tmp_path):
    state = State(make_cfg("local"), tmp_path)
    state.data["images"] = {"abcd1234": {"url": "https://img.example/a.jpg"}}
    assert state.seen_image("abcd1234")
    assert not state.seen_image("deadbeef")
    assert not state.seen_image("")


def test_jaccard_threshold_is_configurable(tmp_path):
    cfg = make_cfg("local")
    cfg["publish"]["state_jaccard_threshold"] = 0.95
    state = State(cfg, tmp_path)
    state.data["articles"] = {"u": {"title": "Salman Khan hosts Bigg Boss finale"}}
    assert not state.is_seen({
        "url_key": "new", "title_key": "new",
        "title": "Salman Khan hosts Bigg Boss grand finale",
    })


# --------------------------------------------------------------------------
# State: writing / merging
# --------------------------------------------------------------------------
def test_record_run_local_writes_and_merges(tmp_path):
    state = State(make_cfg("local"), tmp_path)
    state.load()
    state.record_run({
        "outcome": "rendered",
        "articles": {"u1": {"title": "First", "title_key": "k1"}},
        "images": {"h1": {"url": "https://img.example/1.jpg"}},
        "reels": [{"id": "reel-1"}],
    })

    assert state.data["articles"]["u1"]["title"] == "First"
    assert state.seen_image("h1")
    assert len(state.data["runs"]) == 1
    assert state.data["runs"][0]["id"]
    assert state.data["runs"][0]["at"]

    ledger = tmp_path / "state" / "ledger.json"
    assert ledger.is_file()
    saved = json.loads(ledger.read_text(encoding="utf-8"))
    assert len(saved["runs"]) == 1
    assert "u1" in saved["articles"]

    # A fresh instance sees the data on load, and a second run merges it.
    reloaded = State(make_cfg("local"), tmp_path)
    reloaded.load()
    assert "u1" in reloaded.data["articles"]
    reloaded.record_run({"outcome": "noop", "articles": {"u2": {"title": "Second"}}})
    assert set(reloaded.data["articles"]) == {"u1", "u2"}

    # Reusing a run id must not duplicate the run record.
    reloaded.record_run({"id": "fixed", "articles": {"u3": {"title": "Third"}}})
    reloaded.record_run({"id": "fixed", "articles": {"u4": {"title": "Fourth"}}})
    assert len([r for r in reloaded.data["runs"] if r["id"] == "fixed"]) == 1


def test_missing_blob_credentials_falls_back_to_local(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOB_STATE_TOKEN", "token")  # store id deliberately absent
    state = State(make_cfg("blob"), tmp_path)
    state.record_run({"outcome": "noop", "articles": {"u": {"title": "T"}}})
    assert (tmp_path / "state" / "ledger.json").is_file()


def test_blob_load_merges_all_run_records(fake_vercel, tmp_path, monkeypatch):
    monkeypatch.setenv("BLOB_STATE_TOKEN", "token")
    monkeypatch.setenv("BLOB_STATE_STORE_ID", "store")
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
    fake_vercel.objects["state/runs/one.json"] = json.dumps(first).encode()
    fake_vercel.objects["state/runs/two.json"] = json.dumps(second).encode()
    fake_vercel.objects["state/runs/broken.json"] = b"not json"

    state = State(make_cfg("blob"), tmp_path)
    data = state.load()

    assert set(data["articles"]) == {"u1", "u2"}
    assert {r["id"] for r in data["runs"]} == {"r1", "r2"}
    assert len(data["reels"]) == 1
    assert state.seen_image("hash-2")
    assert fake_vercel.access_reads
    assert all(access == "private" for _, access in fake_vercel.access_reads)


def test_blob_record_run_is_unique_immutable_and_metadata_only(
    fake_vercel, tmp_path, monkeypatch
):
    monkeypatch.setenv("BLOB_STATE_TOKEN", "token")
    monkeypatch.setenv("BLOB_STATE_STORE_ID", "store")
    state = State(make_cfg("blob"), tmp_path)
    state.load()

    state.record_run({
        "articles": {"u": {"title": "T"}},
        "images": {"h": {"thumbnail": b"\x00\x01\x02"}},
        "reels": [],
    })
    state.record_run({"articles": {"u2": {"title": "T2"}}})

    assert len(fake_vercel.puts) == 2
    paths = [entry[0] for entry in fake_vercel.puts]
    assert len(set(paths)) == 2
    assert all(p.startswith("state/runs/") and p.endswith(".json") for p in paths)
    assert all(entry[1] == "private" for entry in fake_vercel.puts)
    assert all(entry[2].get("allow_overwrite") is False for entry in fake_vercel.puts)

    saved = json.loads(fake_vercel.objects[paths[0]])
    assert saved["images"]["h"] == {}  # binary payload stripped, never committed
    assert not (tmp_path / "state" / "ledger.json").exists()  # no local write


# --------------------------------------------------------------------------
# Publish: optional / guarded
# --------------------------------------------------------------------------
def test_publish_steps_skip_when_env_absent(monkeypatch, tmp_path):
    for name in ("BLOB_VIDEO_TOKEN", "BLOB_VIDEO_STORE_ID", "NETLIFY_AUTH_TOKEN",
                 "NETLIFY_SITE_ID", "YT_CLIENT_ID", "YT_CLIENT_SECRET",
                 "YT_REFRESH_TOKEN", "IG_LONG_LIVED_TOKEN", "IG_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"fake mp4")
    manifest = {"id": "reel-1", "story": {"headline": "H", "script": "S"}}

    assert publish.upload_video(video, make_cfg(), manifest) is None
    assert publish.youtube_upload(video, "https://pub", manifest, make_cfg()) is None
    assert publish.instagram_upload("https://pub", manifest, make_cfg()) is None
    assert publish.publish_all(video, "https://pub", manifest, make_cfg()) == {
        "youtube": None, "instagram": None,
    }


def test_publish_all_respects_platform_flags(tmp_path):
    cfg = make_cfg()
    cfg["publish"]["youtube"] = False
    cfg["publish"]["instagram"] = False
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"fake mp4")
    assert publish.publish_all(video, "https://pub", {"id": "r"}, cfg) == {}


def test_ledger_prevents_double_post(tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"fake mp4")
    manifest = {
        "id": "reel-1",
        "story": {},
        "published": {
            "youtube": {"video_id": "existing", "url": "https://youtu.be/existing"},
            "instagram": {"media_id": "m1", "url": "https://ig/p/1"},
        },
    }
    result = publish.publish_all(video, "https://pub", manifest, make_cfg())
    assert result["youtube"]["video_id"] == "existing"
    assert result["instagram"]["media_id"] == "m1"


# --------------------------------------------------------------------------
# Publish: Vercel Blob video
# --------------------------------------------------------------------------
def test_upload_video_public_url_and_rolling_cleanup(
    fake_vercel, tmp_path, monkeypatch
):
    monkeypatch.setenv("BLOB_VIDEO_TOKEN", "token")
    monkeypatch.setenv("BLOB_VIDEO_STORE_ID", "store")
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"12345")

    now = datetime.now(timezone.utc)
    fake_vercel.objects["reels/old.mp4"] = b"old"
    fake_vercel.uploaded_at["reels/old.mp4"] = now - timedelta(days=9)
    fake_vercel.objects["reels/fresh.mp4"] = b"fresh"
    fake_vercel.uploaded_at["reels/fresh.mp4"] = now - timedelta(days=1)

    monkeypatch.setattr(
        publish.requests, "head",
        lambda url, **kw: _Resp(200),
    )
    result = publish.upload_video(video, make_cfg(), {"id": "new"})

    assert result["bytes"] == 5
    assert result["url"].startswith("https://")
    assert fake_vercel.puts[0][0] == "reels/new.mp4"
    assert fake_vercel.puts[0][1] == "public"
    assert "reels/old.mp4" in fake_vercel.deleted
    assert "reels/fresh.mp4" not in fake_vercel.deleted
    assert "reels/new.mp4" not in fake_vercel.deleted


def test_upload_video_rejects_unreachable_public_url(
    fake_vercel, tmp_path, monkeypatch
):
    monkeypatch.setenv("BLOB_VIDEO_TOKEN", "token")
    monkeypatch.setenv("BLOB_VIDEO_STORE_ID", "store")
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"12345")
    monkeypatch.setattr(
        publish.requests, "head",
        lambda url, **kw: _Resp(403, text="forbidden"),
    )
    assert publish.upload_video(video, make_cfg(), {"id": "new"}) is None


# --------------------------------------------------------------------------
# Publish: YouTube / Instagram
# --------------------------------------------------------------------------
def test_youtube_token_refresh_and_resumable_upload(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_CLIENT_ID", "cid")
    monkeypatch.setenv("YT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("YT_REFRESH_TOKEN", "refresh")
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"12345")
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("post", url))
        if url == publish.TOKEN_URL:
            return _Resp(200, payload={"access_token": "access"})
        return _Resp(200, headers={"Location": "https://upload.example/session"})

    def fake_put(url, **kwargs):
        calls.append(("put", url))
        return _Resp(200, payload={"id": "vid123"})

    monkeypatch.setattr(publish.requests, "post", fake_post)
    monkeypatch.setattr(publish.requests, "put", fake_put)
    manifest = {"story": {"headline": "H", "script": "S", "source": "Src", "url": "https://src"}}

    result = publish.youtube_upload(video, "https://pub", manifest, make_cfg())
    assert result == {"video_id": "vid123", "url": "https://youtu.be/vid123"}
    assert ("put", "https://upload.example/session") in calls


def test_youtube_upload_handles_token_and_init_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("YT_CLIENT_ID", "cid")
    monkeypatch.setenv("YT_CLIENT_SECRET", "secret")
    monkeypatch.setenv("YT_REFRESH_TOKEN", "refresh")
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"12345")
    manifest = {"story": {"headline": "H"}}

    monkeypatch.setattr(
        publish.requests, "post",
        lambda url, **kw: _Resp(401, text="bad token"),
    )
    assert publish.youtube_upload(video, "https://pub", manifest, make_cfg()) is None

    monkeypatch.setattr(
        publish.requests, "post",
        lambda url, **kw: (_Resp(200, payload={"access_token": "a"})
                           if url == publish.TOKEN_URL else _Resp(500, text="boom")),
    )
    assert publish.youtube_upload(video, "https://pub", manifest, make_cfg()) is None


def test_instagram_container_poll_and_publish(monkeypatch):
    monkeypatch.setenv("IG_LONG_LIVED_TOKEN", "ig-token")
    monkeypatch.setenv("IG_USER_ID", "ig-user")
    monkeypatch.setattr(publish.time, "sleep", lambda *_: None)

    def fake_post(url, **kwargs):
        if url.endswith("/media"):
            return _Resp(200, payload={"id": "container-1"})
        if url.endswith("/media_publish"):
            return _Resp(200, payload={"id": "media-1"})
        return _Resp(500)

    def fake_get(url, **kwargs):
        if url.endswith("/container-1"):
            return _Resp(200, payload={"status_code": "FINISHED"})
        if url.endswith("/media-1"):
            return _Resp(200, payload={"permalink": "https://ig/p/1"})
        return _Resp(500)

    monkeypatch.setattr(publish.requests, "post", fake_post)
    monkeypatch.setattr(publish.requests, "get", fake_get)
    manifest = {"story": {"headline": "H", "source": "S", "url": "https://s"}}

    result = publish.instagram_upload("https://pub", manifest, make_cfg())
    assert result == {"media_id": "media-1", "url": "https://ig/p/1"}


def test_instagram_unfinished_container_is_not_published(monkeypatch):
    monkeypatch.setenv("IG_LONG_LIVED_TOKEN", "ig-token")
    monkeypatch.setenv("IG_USER_ID", "ig-user")
    monkeypatch.setattr(publish.time, "sleep", lambda *_: None)

    def fake_post(url, **kwargs):
        return _Resp(200, payload={"id": "container-1"})

    def fake_get(url, **kwargs):
        return _Resp(200, payload={"status_code": "IN_PROGRESS"})

    monkeypatch.setattr(publish.requests, "post", fake_post)
    monkeypatch.setattr(publish.requests, "get", fake_get)
    manifest = {"story": {"headline": "H"}}
    assert publish.instagram_upload("https://pub", manifest, make_cfg()) is None


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------
def test_workflow_yaml_parses_and_has_required_keys():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)

    # YAML 1.1 parses the key `on` as boolean True.
    triggers = data.get("on", data.get(True))
    assert triggers is not None
    assert any(job.get("cron") == "0 */6 * * *" for job in triggers["schedule"])
    assert "workflow_dispatch" in triggers

    assert data["concurrency"]["cancel-in-progress"] is False
    assert data["permissions"]["contents"] == "read"

    job = data["jobs"]["generate"]
    steps = job["steps"]
    runs = [step.get("run", "") for step in steps]

    assert any("hyperframes@0.9.77" in run for run in runs)
    assert any("HYPERFRAMES_CLI" in run for run in runs)
    assert any("node_modules/hyperframes/bin/hyperframes.mjs" in run for run in runs)
    assert any(
        step.get("uses", "").startswith("actions/upload-artifact@") for step in steps
    )
    assert any(step.get("if") == "always()" for step in steps)

    generate_step = next(step for step in steps if step.get("name") == "Generate reel")
    env = generate_step["env"]
    for secret in ENV_KEYS:
        assert env[secret] == "${{ secrets.%s }}" % secret
