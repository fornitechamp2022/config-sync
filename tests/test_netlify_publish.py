import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline import publish


class Proc:
    def __init__(self, code=0, out="", err=""):
        self.returncode = code
        self.stdout = out
        self.stderr = err


def _env(monkeypatch, *, creds=True):
    monkeypatch.delenv("BLOB_VIDEO_TOKEN", raising=False)
    monkeypatch.delenv("BLOB_VIDEO_STORE_ID", raising=False)
    if creds:
        monkeypatch.setenv("NETLIFY_AUTH_TOKEN", "test-token")
        monkeypatch.setenv("NETLIFY_SITE_ID", "test-site")
    else:
        monkeypatch.delenv("NETLIFY_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("NETLIFY_SITE_ID", raising=False)


def test_netlify_upload_returns_public_edge_url(tmp_path, monkeypatch):
    _env(monkeypatch)
    mp4 = tmp_path / "reel.mp4"
    mp4.write_bytes(b"fake mp4 bytes")
    monkeypatch.setattr(publish, "_netlify_cli", lambda: ["netlify"])
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return Proc(0, '{"ok":true}')

    monkeypatch.setattr(publish, "_netlify_run", fake_run)
    monkeypatch.setattr(publish, "_verify_public_url", lambda url: True)
    monkeypatch.setattr(publish, "_prune_netlify_media", lambda **kwargs: None)

    result = publish.upload_video(mp4, {}, {"id": "reel-123"})
    assert result["url"] == "https://bollywood-reel-generator.netlify.app/r/reels/reel-123.mp4"
    assert result["provider"] == "netlify"
    assert calls[0][:3] == ["blobs:set", "reels-media", "reels/reel-123.mp4"]


def test_netlify_upload_skips_without_credentials(tmp_path, monkeypatch):
    _env(monkeypatch, creds=False)
    mp4 = tmp_path / "reel.mp4"
    mp4.write_bytes(b"x")
    monkeypatch.setattr(publish, "_netlify_cli", lambda: ["netlify"])
    assert publish.upload_video(mp4, {}, {"id": "x"}) is None


def test_netlify_upload_failure_returns_none(tmp_path, monkeypatch):
    _env(monkeypatch)
    mp4 = tmp_path / "reel.mp4"
    mp4.write_bytes(b"x")
    monkeypatch.setattr(publish, "_netlify_cli", lambda: ["netlify"])
    monkeypatch.setattr(publish, "_netlify_run", lambda args, **kw: Proc(1, err="boom"))
    assert publish.upload_video(mp4, {}, {"id": "x"}) is None


def test_unverified_url_deletes_blob(tmp_path, monkeypatch):
    _env(monkeypatch)
    mp4 = tmp_path / "reel.mp4"
    mp4.write_bytes(b"x")
    monkeypatch.setattr(publish, "_netlify_cli", lambda: ["netlify"])
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return Proc(0)

    monkeypatch.setattr(publish, "_netlify_run", fake_run)
    monkeypatch.setattr(publish, "_verify_public_url", lambda url: False)
    result = publish.upload_video(mp4, {}, {"id": "x"})
    assert result is None
    assert any(c[:2] == ["blobs:delete", "reels-media"] for c in calls)


def test_prune_keeps_unknown_age_and_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(publish, "_netlify_cli", lambda: ["netlify"])
    listing = '{"blobs":[{"key":"reels/new.mp4","uploadedAt":"2099-01-01T00:00:00Z"},{"key":"reels/unknown.mp4"}]}'
    deleted = []

    def fake_run(args, **kw):
        if args[1] == "blobs:list":
            return Proc(0, listing)
        if args[1] == "blobs:delete":
            deleted.append(args)
        return Proc(0)

    monkeypatch.setattr(publish, "_netlify_run", fake_run)
    publish._prune_netlify_media(just_uploaded="reels/current.mp4")
    assert deleted == []
