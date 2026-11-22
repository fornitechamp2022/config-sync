"""Retention / storage-cleanup tests for iteration 2.

Covers all five acceptance criteria with temp dirs and in-memory fakes:
work-dir removal (and --keep-work), out/ MP4 cap, state pruning (local +
remote), public media TTL, and the guarantee that no media bytes ever reach
state. No network calls and no real credentials are used.
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import generate as G  # noqa: E402
from pipeline import publish, state as state_mod  # noqa: E402
from pipeline.state import (  # noqa: E402
    LAST_PRUNE_KEY, PRUNED_AT_KEY, RECORD_PREFIX, STATE_TTL_DAYS, State,
)
from tests.test_generate_gate import (  # noqa: E402
    CFG, fake_article, fake_story, fake_words,
)
from tests.test_netlify_state import (  # noqa: E402
    SITE, TOKEN, _Completed, make_cfg as netlify_cfg,
)
from tests.test_state_publish import (  # noqa: E402
    _Creds, _FakeBlob, _Item, _Opts, _Resp, _Session,
)


def now_iso(days: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def stamp(days: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")


# ==========================================================================
# 1. work/ cleanup
# ==========================================================================
def test_cleanup_work_deletes_dir_and_never_raises(tmp_path, monkeypatch, capsys):
    target = tmp_path / "reel-1" / "assets"
    target.mkdir(parents=True)
    (target / "shot.jpg").write_bytes(b"media bytes")

    G.cleanup_work(target.parent)
    assert not target.parent.exists()
    # Missing path is a no-op.
    G.cleanup_work(target.parent)
    # A permissions/race failure is logged, never raised.
    monkeypatch.setattr(G.shutil, "rmtree", lambda *_: (_ for _ in ()).throw(OSError("locked")))
    G.cleanup_work(tmp_path)
    assert "could not remove work dir" in capsys.readouterr().out


def test_cleanup_work_root_sweeps_only_stale_dirs(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    stale = root / "stale"
    recent = root / "recent"
    skipped = root / "keep-me"
    for path in (stale, recent, skipped):
        path.mkdir()
        (path / "captions.json").write_text("{}", encoding="utf-8")
    old = time.time() - 10 * 86400
    os.utime(stale, (old, old))
    os.utime(skipped, (old, old))

    G.cleanup_work_root(root, max_age_days=2, skip={"keep-me"})

    assert not stale.exists()
    assert recent.exists()
    assert skipped.exists()


# ==========================================================================
# 2. out/ MP4 + sidecar cap
# ==========================================================================
def test_cleanup_out_keeps_newest_ten_groups_and_deletes_older_sidecars(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for index in range(13):
        reel = out / f"reel-{index:02d}"
        # Deliberately different mtimes so the ranking is unambiguous.
        moment = time.time() - (20 - index) * 60
        for suffix in G.OUT_SUFFIXES:
            path = out / f"{reel.name}{suffix}"
            path.write_bytes(b"data")
            os.utime(path, (moment, moment))
    (out / "notes.txt").write_text("keep", encoding="utf-8")

    G.cleanup_out(out, keep=10)

    remaining = sorted(p.name for p in out.glob("*.mp4"))
    assert len(remaining) == 10
    assert remaining[-1] == "reel-12.mp4"
    assert remaining[0] == "reel-03.mp4"
    for index in range(10):
        assert (out / f"reel-{index + 3:02d}.manifest.json").exists()
        assert (out / f"reel-{index + 3:02d}.debug.json").exists()
    for index in range(3):
        for suffix in G.OUT_SUFFIXES:
            assert not (out / f"reel-{index:02d}{suffix}").exists()
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_out_orphan_sidecars_count_toward_the_cap(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    for index in range(12):
        moment = time.time() - (20 - index) * 60
        path = out / f"orphan-{index:02d}.debug.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (moment, moment))

    G.cleanup_out(out, keep=10)

    assert len(list(out.glob("*.debug.json"))) == 10


def test_cleanup_out_is_best_effort(tmp_path, monkeypatch, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "reel.mp4").write_bytes(b"x")
    monkeypatch.setattr(G, "_mtime", lambda _path: 1 / 0)

    G.cleanup_out(out)  # must not raise
    assert (out / "reel.mp4").exists()
    assert "retention skipped" in capsys.readouterr().out


# ==========================================================================
# 1+2. orchestration: cleanup after record_run, --keep-work, failure safety
# ==========================================================================
_SENTINEL = object()


@contextmanager
def main_env(tmp_path: Path):
    """Wire generate.main() to in-memory mocks with a real render path."""
    article = fake_article()
    en_story, hi_story = fake_story("en"), fake_story("hi")
    state = MagicMock()
    state.is_seen.return_value = False
    old_cwd = Path.cwd()
    ctx = {"tmp": tmp_path, "state": state, "article": article}
    with ExitStack() as stack:
        def mock(target, new=_SENTINEL, **kwargs):
            if new is _SENTINEL:
                return stack.enter_context(patch(target, **kwargs))
            return stack.enter_context(patch(target, new, **kwargs))

        mock("generate.HERE", tmp_path)
        mock("generate.load_config", return_value=deepcopy(CFG))
        mock("generate.State", return_value=state)
        mock("generate.discover", return_value=[article])
        mock("generate.enrich_and_gate", return_value=[article])
        mock("generate.build_story", return_value=en_story)
        mock("generate.to_hindi", return_value=hi_story)
        mock("generate.qa_flags", return_value=[])
        mock("generate.synthesize_natural", return_value={
            "audio": "assets/voice/voice.mp3", "words": fake_words(), "duration": 12.5,
        })
        mock("generate.prepare_stills", return_value=["shot_00.jpg"])
        mock("generate.write_project")
        mock("generate.check_project", return_value={"ok": True})
        mock("generate.render_project", return_value=True)
        mock("generate.cap_size")
        try:
            yield ctx
        finally:
            os.chdir(old_cwd)


def _run_main(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["generate.py", *args])
    return G.main()


def _manifest_id(tmp_path: Path) -> str:
    manifests = list((tmp_path / "out").glob("*.manifest.json"))
    assert len(manifests) == 1
    return manifests[0].name[: -len(".manifest.json")]


@pytest.mark.parametrize("extra,workdir_survives", [
    ((), False),
    (("--keep-work",), True),
])
def test_main_deletes_work_after_recorded_render_unless_kept(
    tmp_path, monkeypatch, extra, workdir_survives
):
    with main_env(tmp_path) as ctx:
        observed = {}
        original_cleanup = G.cleanup_work

        def spy(path):
            observed["existed"] = Path(path).is_dir()
            original_cleanup(path)

        monkeypatch.setattr(G, "cleanup_work", spy)
        rc = _run_main(monkeypatch, "--run", "--count", "1", "--no-publish",
                       "--local-state", *extra)

        assert rc == 0
        reel_id = _manifest_id(tmp_path)
        workdir = tmp_path / "work" / reel_id
        if workdir_survives:
            assert observed == {}  # --keep-work: cleanup was never invoked
        else:
            assert observed["existed"] is True  # it existed when cleanup ran
        assert workdir.exists() is workdir_survives
        # Cleanup happens only after state recorded the successful run.
        assert ctx["state"].record_run.call_count == 1
        assert (tmp_path / "out" / f"{reel_id}.mp4").name.endswith(".mp4")
        # Opting out of work cleanup does NOT disable out/ retention.
        assert (tmp_path / "out" / f"{reel_id}.manifest.json").exists()


def test_cleanup_failures_never_fail_the_job(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("cleanup exploded")

    with main_env(tmp_path):
        monkeypatch.setattr(G, "cleanup_work", boom)
        monkeypatch.setattr(G, "cleanup_out", boom)
        rc = _run_main(monkeypatch, "--run", "--count", "1", "--no-publish", "--local-state")

    assert rc == 0
    assert _manifest_id(tmp_path)


# ==========================================================================
# 3. state pruning
# ==========================================================================
def _seed_runs(state: State, *, fresh: int, stale: int) -> None:
    """Build a ledger where each run carries article/image/reel metadata."""
    for index in range(fresh):
        at = now_iso(1 + index * 0.01)
        state.data["runs"].append({
            "id": f"fresh-{index}", "at": at,
            "articles": {f"new-u{index}": {"title": f"New {index}", "at": at}},
            "images": {f"new-h{index}": {"at": at}},
            "reels": [{"id": f"new-r{index}", "at": at}],
        })
        state.data["articles"][f"new-u{index}"] = {"title": f"New {index}", "at": at}
        state.data["images"][f"new-h{index}"] = {"at": at}
        state.data["reels"].append({"id": f"new-r{index}", "at": at})
    for index in range(stale):
        at = now_iso(40 + index)
        state.data["runs"].append({
            "id": f"old-{index}", "at": at,
            "articles": {f"old-u{index}": {"title": f"Old {index}", "at": at}},
            "images": {f"old-h{index}": {"at": at}},
            "reels": [{"id": f"old-r{index}", "at": at}],
        })
        state.data["articles"][f"old-u{index}"] = {"title": f"Old {index}", "at": at}
        state.data["images"][f"old-h{index}"] = {"at": at}
        state.data["reels"].append({"id": f"old-r{index}", "at": at})


def test_prune_removes_stale_entries_and_records_marker(tmp_path):
    state = State({"publish": {"state_backend": "local"}}, tmp_path)
    _seed_runs(state, fresh=6, stale=8)
    state.data["articles"]["orphan-old"] = {"title": "Orphan", "at": now_iso(90)}

    summary = state.prune(max_age_days=STATE_TTL_DAYS)

    assert {run["id"] for run in state.data["runs"]} == {f"fresh-{i}" for i in range(6)}
    assert all(key.startswith("new-") for key in state.data["articles"])
    assert "orphan-old" not in state.data["articles"]
    assert all(key.startswith("new-") for key in state.data["images"])
    assert all(reel["id"].startswith("new-") for reel in state.data["reels"])
    assert state.data[PRUNED_AT_KEY]
    assert state.data[LAST_PRUNE_KEY] == summary
    assert summary["max_age_days"] == 30
    assert summary["removed"]["runs"] == 8
    assert summary["removed"]["articles"] == 9
    assert summary["removed"]["images"] == 8
    assert summary["removed"]["reels"] == 8


def test_prune_always_pins_newest_runs(tmp_path):
    state = State({"publish": {"state_backend": "local"}}, tmp_path)
    _seed_runs(state, fresh=0, stale=8)

    state.prune(max_age_days=30)

    ids = [run["id"] for run in state.data["runs"]]
    assert len(ids) == 5  # KEEP_RECENT_RUNS
    # old-0 has the newest timestamp, old-7 the oldest.
    assert ids == [f"old-{i}" for i in range(5)]


def test_prune_runs_on_load_and_before_write(tmp_path):
    cfg = {"publish": {"state_backend": "local"}}
    seeder = State(cfg, tmp_path)
    _seed_runs(seeder, fresh=6, stale=8)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "ledger.json").write_text(
        json.dumps(seeder.data), encoding="utf-8")

    loaded = State(cfg, tmp_path)
    data = loaded.load()
    assert len(data["runs"]) == 6
    assert data[PRUNED_AT_KEY]

    loaded.record_run({"outcome": "rendered", "articles": {"u": {"title": "T"}}})
    saved = json.loads((tmp_path / "state" / "ledger.json").read_text(encoding="utf-8"))
    assert any(run.get("outcome") == "rendered" for run in saved["runs"])
    assert len(saved["runs"]) == 7
    assert PRUNED_AT_KEY in saved and LAST_PRUNE_KEY in saved
    assert not any(run["id"].startswith("old-") for run in saved["runs"])


def test_prune_runs_before_write_even_without_prior_load(tmp_path):
    state = State({"publish": {"state_backend": "local"}}, tmp_path)
    _seed_runs(state, fresh=6, stale=8)  # populated directly, no load() call

    state.record_run({"outcome": "rendered"})

    saved = json.loads((tmp_path / "state" / "ledger.json").read_text(encoding="utf-8"))
    assert len(saved["runs"]) == 7  # 6 fresh + the just-recorded run
    assert not any(run["id"].startswith("old-") for run in saved["runs"])
    assert saved[PRUNED_AT_KEY]


def test_no_media_bytes_ever_reach_state(tmp_path):
    state = State({"publish": {"state_backend": "local"}}, tmp_path)
    state.record_run({
        "outcome": "rendered",
        "articles": {"u": {"title": "T", "thumbnail": b"\x00\x01RAW"}},
        "images": {"h": {"raw": b"\xff\xd8\xffIMAGE"}},
        "reels": [{"id": "r", "audio": bytearray(b"SOUND"), "frame": memoryview(b"PNG")}],
    })
    saved_text = (tmp_path / "state" / "ledger.json").read_text(encoding="utf-8")
    saved = json.loads(saved_text)

    assert "RAW" not in saved_text and "IMAGE" not in saved_text and "SOUND" not in saved_text
    assert "thumbnail" not in saved["articles"]["u"]
    assert saved["images"]["h"] == {}
    assert "audio" not in saved["reels"][0] and "frame" not in saved["reels"][0]


# ==========================================================================
# 4. public media TTL (pagination, skip, missing times, failure paths)
# ==========================================================================
@pytest.fixture(autouse=True)
def clean_media_env(monkeypatch):
    for key in ("BLOB_VIDEO_TOKEN", "BLOB_VIDEO_STORE_ID",
                "NETLIFY_AUTH_TOKEN", "NETLIFY_SITE_ID"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_media_blob(monkeypatch):
    blob = _FakeBlob()
    vercel = sys.modules.get("vercel") or type(sys)("vercel")
    api = type(sys)("vercel.api")
    blob_pkg = type(sys)("vercel.blob")
    sync = type(sys)("vercel.blob.sync")
    api.session = _Session
    sync.BlobCredentials = _Creds
    sync.SyncBlobServiceOptions = _Opts
    sync.put_blob = blob.put_blob
    sync.get_blob = blob.get_blob
    sync.query_blobs = blob.query_blobs
    sync.delete_blob = blob.delete_blob
    blob_pkg.sync = sync
    for name, module in (("vercel", vercel), ("vercel.api", api),
                         ("vercel.blob", blob_pkg), ("vercel.blob.sync", sync)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(publish.requests, "head", lambda url, **kw: _Resp(200))
    return blob


def _rebind_media_methods(monkeypatch, blob) -> None:
    """SDK functions live in sys.modules; rebind them after swapping fakes."""
    sync = sys.modules["vercel.blob.sync"]
    for name in ("put_blob", "get_blob", "query_blobs", "delete_blob"):
        monkeypatch.setattr(sync, name, getattr(blob, name))


def _video(tmp_path: Path) -> Path:
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"12345")
    return video


class _Page:
    def __init__(self, items, cursor=None):
        self.items = items
        self.cursor = cursor


class _PagedBlob(_FakeBlob):
    def __init__(self):
        super().__init__()
        self.cursors: list = []

    def query_blobs(self, *, prefix=None, cursor=None):
        self.cursors.append(cursor)
        # Snapshot once: deletes happen while pages are consumed.
        if not hasattr(self, "_paths"):
            self._paths = sorted(
                p for p in self.objects if not prefix or p.startswith(prefix))
        paths = self._paths
        if cursor is None:
            return _Page([_Item(p, self.uploaded_at.get(p)) for p in paths[:3]], cursor="p2")
        return [_Item(p, self.uploaded_at.get(p)) for p in paths[3:]]


def test_upload_video_follows_pagination_and_deletes_every_expired_blob(
    fake_media_blob, monkeypatch, tmp_path
):
    paged = _PagedBlob()
    _rebind_media_methods(monkeypatch, paged)
    old = datetime.now(timezone.utc) - timedelta(days=9)
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    for name, when in (("reels/old-a.mp4", old), ("reels/fresh.mp4", fresh),
                       ("reels/old-b.mp4", old), ("reels/old-c.mp4", old)):
        paged.objects[name] = b"x"
        paged.uploaded_at[name] = when
    monkeypatch.setattr(publish, "_vercel_options", lambda *_: object())

    result = publish.upload_video(_video(tmp_path), {}, {"id": "new"})

    assert result["bytes"] == 5
    assert paged.cursors == [None, "p2"]  # the second page was actually requested
    assert set(paged.deleted) == {"reels/old-a.mp4", "reels/old-b.mp4", "reels/old-c.mp4"}
    assert "reels/fresh.mp4" not in paged.deleted
    assert "reels/new.mp4" not in paged.deleted


def test_upload_video_skips_just_uploaded_object_with_different_metadata(
    fake_media_blob, monkeypatch, tmp_path
):
    old = datetime.now(timezone.utc) - timedelta(days=30)
    new_path = "reels/sdk-chosen-name.mp4"
    monkeypatch.setattr(publish, "_vercel_options", lambda *_: object())
    original_put = fake_media_blob.put_blob

    def put(pathname, body, **kwargs):
        result = original_put(pathname, body, **kwargs)
        result.pathname = new_path
        return result

    fake_media_blob.put_blob = put
    _rebind_media_methods(monkeypatch, fake_media_blob)
    for name in ("reels/new.mp4", new_path, "reels/older.mp4"):
        fake_media_blob.objects[name] = b"x"
        fake_media_blob.uploaded_at[name] = old

    result = publish.upload_video(_video(tmp_path), {}, {"id": "new"})

    assert result["pathname"] == new_path
    assert "reels/new.mp4" not in fake_media_blob.deleted
    assert new_path not in fake_media_blob.deleted
    assert "reels/older.mp4" in fake_media_blob.deleted


def test_upload_video_tolerates_missing_and_odd_timestamps(
    fake_media_blob, monkeypatch, tmp_path
):
    old = datetime.now(timezone.utc) - timedelta(days=9)
    fake_media_blob.objects.update({
        "reels/no-time.mp4": b"x",
        "reels/iso.mp4": b"x",
        "reels/epoch.mp4": b"x",
        "reels/garbage.mp4": b"x",
    })
    fake_media_blob.uploaded_at.update({
        "reels/iso.mp4": old.isoformat(),
        "reels/epoch.mp4": old.timestamp(),
        "reels/garbage.mp4": "not-a-date",
    })
    monkeypatch.setattr(publish, "_vercel_options", lambda *_: object())

    assert publish.upload_video(_video(tmp_path), {}, {"id": "new"})

    assert set(fake_media_blob.deleted) == {"reels/iso.mp4", "reels/epoch.mp4"}
    assert "reels/no-time.mp4" not in fake_media_blob.deleted
    assert "reels/garbage.mp4" not in fake_media_blob.deleted


def test_upload_video_removes_unverifiable_upload(
    fake_media_blob, monkeypatch, tmp_path
):
    monkeypatch.setattr(publish, "_vercel_options", lambda *_: object())
    monkeypatch.setattr(publish.requests, "head", lambda url, **kw: _Resp(403))

    assert publish.upload_video(_video(tmp_path), {}, {"id": "new"}) is None
    assert "reels/new.mp4" in fake_media_blob.deleted
    assert "reels/new.mp4" not in fake_media_blob.objects


def test_upload_video_survives_listing_and_delete_failures(
    fake_media_blob, monkeypatch, tmp_path
):
    monkeypatch.setattr(publish, "_vercel_options", lambda *_: object())
    fake_media_blob.objects["reels/old.mp4"] = b"x"
    fake_media_blob.uploaded_at["reels/old.mp4"] = datetime.now(timezone.utc) - timedelta(days=9)

    def broken_listing(**_kwargs):
        raise RuntimeError("listing down")

    fake_media_blob.query_blobs = broken_listing
    _rebind_media_methods(monkeypatch, fake_media_blob)
    assert publish.upload_video(_video(tmp_path), {}, {"id": "new"})

    def broken_delete(_key, **_kwargs):
        raise RuntimeError("delete down")

    fake_media_blob.query_blobs = _FakeBlob.query_blobs.__get__(fake_media_blob)
    fake_media_blob.delete_blob = broken_delete
    _rebind_media_methods(monkeypatch, fake_media_blob)
    assert publish.upload_video(_video(tmp_path), {}, {"id": "new-2"})


def test_verify_public_url_rejects_non_https():
    assert publish._verify_public_url("") is False
    assert publish._verify_public_url("http://blob.example/reel.mp4") is False


# ==========================================================================
# 5. remote state pruning (best effort, never the current run)
# ==========================================================================
def _remote_records(fresh: int, old: int) -> dict[str, dict]:
    """Timestamped immutable record keys with matching metadata."""
    records: dict[str, dict] = {}
    for index in range(fresh):
        at, key_stamp = now_iso(1 + index * 0.01), stamp(1 + index * 0.01)
        key = f"{RECORD_PREFIX}{key_stamp}-fresh{index:02d}.json"
        records[key] = {"at": at, "runs": [{"id": f"fresh{index:02d}", "at": at}]}
    for index in range(old):
        at, key_stamp = now_iso(40 + index), stamp(40 + index)
        key = f"{RECORD_PREFIX}{key_stamp}-old{index:02d}.json"
        records[key] = {"at": at, "runs": [{"id": f"old{index:02d}", "at": at}]}
    return records


def _netlify_cli_with_delete(monkeypatch, store: dict[str, str], calls: list[list[str]]):
    def which(name):
        if name == "netlify":
            return "/usr/bin/netlify"
        return None

    def run(args, **_kwargs):
        argv = [str(part) for part in args]
        calls.append(argv)
        sub = next((part for part in argv if part.startswith("blobs:")), None)
        pos = argv.index(sub) if sub else -1
        if sub == "blobs:list":
            return _Completed(argv, stdout=json.dumps([{"key": key} for key in store]))
        if sub == "blobs:get":
            key = argv[pos + 2]
            if key not in store:
                return _Completed(argv, returncode=1)
            Path(argv[argv.index("--output") + 1]).write_text(store[key], encoding="utf-8")
            return _Completed(argv)
        if sub == "blobs:delete":
            store.pop(argv[pos + 2], None)
            return _Completed(argv)
        return _Completed(argv, returncode=2)

    monkeypatch.setattr(state_mod.shutil, "which", which)
    monkeypatch.setattr(state_mod.subprocess, "run", run)


def test_netlify_remote_prune_deletes_old_keys_but_keeps_newest(monkeypatch, tmp_path):
    monkeypatch.setenv("NETLIFY_AUTH_TOKEN", TOKEN)
    monkeypatch.setenv("NETLIFY_SITE_ID", SITE)
    records = _remote_records(fresh=1, old=10)
    store = {key: json.dumps(value) for key, value in records.items()}
    calls: list[list[str]] = []
    _netlify_cli_with_delete(monkeypatch, store, calls)

    state = State(netlify_cfg(), tmp_path)
    state.load()

    deleted = [argv[argv.index("blobs:delete") + 2]
               for argv in calls if "blobs:delete" in argv]
    assert len(deleted) == 6  # 10 old - 4 protected by keep_recent=5
    assert all("-old" in key for key in deleted)
    assert any(key.startswith(RECORD_PREFIX) and "fresh" in key for key in store)
    assert not any("-old09" in key or "-old08" in key or "-old07" in key or "-old06" in key
                   for key in store)  # the four oldest-run keys were pruned
    for argv in calls:
        assert TOKEN not in " ".join(argv)
        assert "--auth" not in argv


def test_blob_remote_prune_deletes_old_keys_but_keeps_newest(
    fake_media_blob, monkeypatch, tmp_path
):
    monkeypatch.setenv("BLOB_STATE_TOKEN", "token")
    monkeypatch.setenv("BLOB_STATE_STORE_ID", "store")
    records = _remote_records(fresh=1, old=10)
    for key, value in records.items():
        fake_media_blob.objects[key] = json.dumps(value).encode("utf-8")

    state = State({"publish": {"state_backend": "blob"}}, tmp_path)
    state.load()

    state_deletes = [key for key in fake_media_blob.deleted if key.startswith(RECORD_PREFIX)]
    assert len(state_deletes) == 6
    assert all("-old" in key for key in state_deletes)
    assert any("fresh" in key for key in fake_media_blob.objects
               if key.startswith(RECORD_PREFIX))
