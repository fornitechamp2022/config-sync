"""Offline tests for Buffer publishing, the daily cap and publish routing.

No network calls: ``requests.post``, ``time.sleep`` and all external generate.py
helpers are mocked. Credentials are injected only through the environment and
are cleaned up after every test.
"""
from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import publish  # noqa: E402

_UNSET = object()
ENV_KEYS = (
    "BUFFER_API_KEY", "BUFFER_YT_CHANNEL_ID", "BUFFER_IG_CHANNEL_ID",
    "YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN",
    "IG_LONG_LIVED_TOKEN", "IG_USER_ID",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Never let a developer's real credentials leak into a test."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class _Resp:
    def __init__(self, status_code=200, payload=None, *, headers=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._payload


def _success(post_id="post-1"):
    return _Resp(200, {"data": {"createPost": {
        "post": {"id": post_id, "text": "caption", "dueAt": "2026-01-01T00:00:00Z"},
    }}})


def _story_manifest():
    return {
        "id": "reel-1",
        "story": {
            "headline": "Big headline",
            "script": "Some script text.",
            "source": "Test Source",
            "url": "https://news.example/story",
        },
    }


# ---------------------------------------------------------------------------
# Buffer request: endpoint, auth header, query, channel ids
# ---------------------------------------------------------------------------
def test_buffer_post_uses_endpoint_header_query_and_channel_ids(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "secret-key")
    monkeypatch.setenv("BUFFER_YT_CHANNEL_ID", "yt-channel")
    monkeypatch.setenv("BUFFER_IG_CHANNEL_ID", "ig-channel")
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _success(f"post-{len(calls)}")

    monkeypatch.setattr(publish.requests, "post", fake_post)
    manifest = _story_manifest()

    result = publish.buffer_post("https://media.example/reel.mp4", manifest, {}, "youtube")

    assert result == {
        "post_id": "post-1",
        "platform": "youtube",
        "posted_at": result["posted_at"],
    }
    assert datetime.fromisoformat(result["posted_at"]).tzinfo is not None

    url, kwargs = calls[0]
    assert url == "https://api.buffer.com"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key"
    query = kwargs["json"]["query"]
    assert "mutation CreatePost" in query
    assert "schedulingType: automatic" in query
    assert "mode: addToQueue" in query
    assert "thumbnailOffset: 2000" in query
    assert "PostActionSuccess" in query and "MutationError" in query
    assert '"yt-channel"' in query
    assert '"https://media.example/reel.mp4"' in query
    for expected in ("Big headline", "Some script text.", "Test Source",
                     "https://news.example/story", "#Bollywood"):
        assert expected in query
    assert "secret-key" not in query  # the key is only in the header

    ig_result = publish.buffer_post("https://media.example/reel.mp4", manifest, {}, "instagram")
    assert ig_result["platform"] == "instagram"
    ig_query = calls[1][1]["json"]["query"]
    assert '"ig-channel"' in ig_query
    assert '"yt-channel"' not in ig_query


def test_buffer_post_rejects_unknown_platform(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("BUFFER_YT_CHANNEL_ID", "yt")
    post = MagicMock()
    monkeypatch.setattr(publish.requests, "post", post)
    assert publish.buffer_post("https://v", {}, {}, "tiktok") is None
    post.assert_not_called()


# ---------------------------------------------------------------------------
# Errors: GraphQL mutation error and top-level errors -> None
# ---------------------------------------------------------------------------
def test_buffer_mutation_error_returns_none(monkeypatch, capsys):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("BUFFER_YT_CHANNEL_ID", "yt")
    monkeypatch.setattr(publish.requests, "post", lambda url, **kw: _Resp(
        200, {"data": {"createPost": {"message": "channel not found"}}}))

    assert publish.buffer_post("https://v", {}, {}, "youtube") is None
    assert "channel not found" in capsys.readouterr().out


def test_buffer_graphql_errors_return_none(monkeypatch, capsys):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setattr(publish.requests, "post", lambda url, **kw: _Resp(
        200, {"errors": [{"message": "bad token"}]}))

    assert publish.buffer_request("{ channels { id } }") is None
    assert "bad token" in capsys.readouterr().out


def test_buffer_http_and_transport_errors_return_none(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setattr(publish.requests, "post", lambda url, **kw: _Resp(500, text="boom"))
    assert publish.buffer_request("{}") is None

    def explode(url, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(publish.requests, "post", explode)
    assert publish.buffer_request("{}") is None


# ---------------------------------------------------------------------------
# 429 retry behaviour
# ---------------------------------------------------------------------------
def test_buffer_request_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: sleeps.append(seconds))
    responses = iter([_Resp(429, headers={"Retry-After": "2"}), _success("retried")])
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return next(responses)

    monkeypatch.setattr(publish.requests, "post", fake_post)

    data = publish.buffer_request("{ channels { id } }")
    assert data["createPost"]["post"]["id"] == "retried"
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_buffer_request_bounds_retries_and_wait(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    sleeps = []
    monkeypatch.setattr(publish.time, "sleep", lambda seconds: sleeps.append(seconds))
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _Resp(429, headers={"Retry-After": "9999"})

    monkeypatch.setattr(publish.requests, "post", fake_post)

    assert publish.buffer_request("{}") is None
    assert len(calls) == publish.BUFFER_MAX_RETRIES + 1
    assert len(sleeps) == publish.BUFFER_MAX_RETRIES
    assert all(0 <= wait <= 60 for wait in sleeps)


# ---------------------------------------------------------------------------
# Missing credentials skip without any request
# ---------------------------------------------------------------------------
def test_missing_credentials_skip_buffer(monkeypatch, capsys):
    post = MagicMock()
    monkeypatch.setattr(publish.requests, "post", post)

    assert publish.buffer_request("{}") is None
    assert publish.buffer_post("https://v", {}, {}, "youtube") is None
    post.assert_not_called()

    # A key alone is not enough: a destination channel is also required.
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    assert publish.buffer_post("https://v", {}, {}, "youtube") is None
    post.assert_not_called()
    assert "skipping" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# No double posting via the manifest ledger
# ---------------------------------------------------------------------------
def test_buffer_post_guard_prevents_double_post(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("BUFFER_YT_CHANNEL_ID", "yt")
    post = MagicMock()
    monkeypatch.setattr(publish.requests, "post", post)

    existing = {"post_id": "already", "platform": "youtube",
                "posted_at": datetime.now(timezone.utc).isoformat()}
    manifest = {"published": {"youtube": existing}}

    assert publish.buffer_post("https://v", manifest, {}, "youtube") == existing
    post.assert_not_called()


def test_publish_all_buffer_guard_prevents_second_round(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("BUFFER_YT_CHANNEL_ID", "yt")
    post = MagicMock()
    monkeypatch.setattr(publish.requests, "post", post)
    cfg = {"publish": {"provider": "buffer", "youtube": False, "instagram": False}}
    manifest = _story_manifest()
    manifest["published"] = {"youtube": {
        "post_id": "done", "platform": "youtube",
        "posted_at": datetime.now(timezone.utc).isoformat(),
    }}

    result = publish.publish_all("video.mp4", "https://v.mp4", manifest, cfg)
    assert result["youtube"]["post_id"] == "done"
    assert "instagram" not in result
    post.assert_not_called()


# ---------------------------------------------------------------------------
# Daily cap helper
# ---------------------------------------------------------------------------
def _reel(reel_id, *posts):
    now = datetime.now(timezone.utc)
    return {
        "id": reel_id,
        "published": {
            platform: {"post_id": f"{reel_id}-{platform}",
                       "posted_at": (now - timedelta(hours=hours)).isoformat()}
            for platform, hours in posts
        },
    }


def test_posts_in_last_24h_counts_only_recent_reels():
    data = {"reels": [
        _reel("r1", ("youtube", 1)),
        _reel("r2", ("youtube", 2), ("instagram", 3)),  # one reel, two posts
        _reel("old", ("youtube", 25)),
        _reel("future", ("youtube", -2)),                # clock skew: ignored
        {"id": "no-pub"},
        {"id": "no-time", "published": {"youtube": {"post_id": "x"}}},
    ]}

    assert publish.posts_in_last_24h(data) == 2
    assert publish.posts_in_last_24h(data["reels"]) == 2
    assert publish.posts_in_last_24h({}) == 0
    assert publish.posts_in_last_24h(None) == 0


def test_last_post_at_ignores_old_and_unparseable_entries():
    data = {"reels": [_reel("old", ("youtube", 48)),
                      _reel("recent", ("instagram", 1)),
                      {"id": "x", "published": {"youtube": {"post_id": "p"}}}]}
    last = publish.last_post_at(data)
    assert last is not None
    assert datetime.now(timezone.utc) - last < timedelta(hours=2)
    assert publish.last_post_at({"reels": []}) is None


def test_posted_at_survives_state_round_trip(tmp_path):
    """The cap must still see Buffer timestamps on a later run."""
    from pipeline.state import State

    cfg = {"publish": {"state_backend": "local"}}
    state = State(cfg, tmp_path)
    state.load()
    now = datetime.now(timezone.utc).isoformat()
    state.record_run({"outcome": "rendered", "reels": [{
        "id": "reel-1", "published": {
            "youtube": {"post_id": "p1", "platform": "youtube", "posted_at": now},
            "instagram": {"post_id": "p2", "platform": "instagram", "posted_at": now},
        },
    }]})

    reloaded = State(cfg, tmp_path)
    reloaded.load()
    assert reloaded.data["reels"][0]["published"]["youtube"]["posted_at"] == now
    assert publish.posts_in_last_24h(reloaded.data) == 1
    assert publish.last_post_at(reloaded.data) is not None


# ---------------------------------------------------------------------------
# Routing: Buffer vs direct
# ---------------------------------------------------------------------------
def test_publish_all_uses_buffer_when_credentials_present(monkeypatch):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("BUFFER_YT_CHANNEL_ID", "yt")
    monkeypatch.setenv("BUFFER_IG_CHANNEL_ID", "ig")
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return _success(f"post-{len(calls)}")

    monkeypatch.setattr(publish.requests, "post", fake_post)
    direct = MagicMock()
    monkeypatch.setattr(publish, "youtube_upload", direct)
    monkeypatch.setattr(publish, "instagram_upload", direct)

    # Direct flags are false while Buffer is active: no double posting.
    cfg = {"publish": {"provider": "buffer", "youtube": False, "instagram": False}}
    manifest = _story_manifest()
    result = publish.publish_all("video.mp4", "https://v.mp4", manifest, cfg)

    assert set(result) == {"youtube", "instagram"}
    assert result["youtube"]["post_id"] == "post-1"
    assert result["instagram"]["post_id"] == "post-2"
    assert len(calls) == 2
    direct.assert_not_called()
    assert manifest["published"]["youtube"]["posted_at"]
    assert manifest["published"]["instagram"]["posted_at"]


def test_publish_all_falls_back_to_direct_only_when_flags_true(monkeypatch, tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"fake mp4")
    yt = MagicMock(return_value={"video_id": "v1"})
    ig = MagicMock(return_value={"media_id": "m1"})
    monkeypatch.setattr(publish, "youtube_upload", yt)
    monkeypatch.setattr(publish, "instagram_upload", ig)
    post = MagicMock()
    monkeypatch.setattr(publish.requests, "post", post)

    # Buffer is the configured provider but its credentials are absent.
    cfg = {"publish": {"provider": "buffer", "youtube": True, "instagram": True}}
    result = publish.publish_all(video, "https://v.mp4", {}, cfg)
    assert result["youtube"]["video_id"] == "v1"
    assert result["instagram"]["media_id"] == "m1"
    yt.assert_called_once()
    ig.assert_called_once()
    post.assert_not_called()

    # Both flags off -> neither direct nor Buffer runs.
    yt.reset_mock()
    ig.reset_mock()
    cfg["publish"]["youtube"] = False
    cfg["publish"]["instagram"] = False
    assert publish.publish_all(video, "https://v.mp4", {}, cfg) == {}
    yt.assert_not_called()
    ig.assert_not_called()


def test_direct_provider_never_calls_buffer(monkeypatch, tmp_path):
    monkeypatch.setenv("BUFFER_API_KEY", "k")
    monkeypatch.setenv("BUFFER_YT_CHANNEL_ID", "yt")
    post = MagicMock()
    monkeypatch.setattr(publish.requests, "post", post)
    yt = MagicMock(return_value={"video_id": "v1"})
    ig = MagicMock(return_value=None)
    monkeypatch.setattr(publish, "youtube_upload", yt)
    monkeypatch.setattr(publish, "instagram_upload", ig)

    cfg = {"publish": {"provider": "direct", "youtube": True, "instagram": True}}
    result = publish.publish_all(tmp_path / "v.mp4", "https://v.mp4", {}, cfg)
    assert result["youtube"]["video_id"] == "v1"
    yt.assert_called_once()
    post.assert_not_called()


# ---------------------------------------------------------------------------
# generate.py gate
# ---------------------------------------------------------------------------
def test_publishing_gate_enforces_cap_and_spacing():
    import generate as G

    cfg = {"publish": {"max_posts_per_day": 2, "min_hours_between_posts": 3}}

    def state(hours_ago, count=1):
        now = datetime.now(timezone.utc)
        return {"reels": [
            {"id": f"r{i}", "published": {"youtube": {
                "post_id": f"p{i}",
                "posted_at": (now - timedelta(hours=hours_ago + i)).isoformat(),
            }}}
            for i in range(count)
        ]}

    assert G.publishing_gate({"reels": []}, cfg)[:2] == (True, None)

    allowed, reason, _count = G.publishing_gate(state(1), cfg)
    assert (allowed, reason) == (False, "min_hours")

    allowed, reason, _count = G.publishing_gate(state(5), cfg)
    assert (allowed, reason) == (True, None)

    # Cap is checked first and counts distinct reels, not platform entries.
    allowed, reason, count = G.publishing_gate(state(1, count=2), cfg)
    assert (allowed, reason, count) == (False, "daily_cap", 2)

    # Bad config values fall back to documented defaults.
    bad = {"publish": {"max_posts_per_day": "x", "min_hours_between_posts": None}}
    allowed, reason, _count = G.publishing_gate(state(1), bad)
    assert (allowed, reason) == (False, "min_hours")
    allowed, reason, _count = G.publishing_gate(state(5, count=2), bad)
    assert (allowed, reason) == (False, "daily_cap")


GEN_CFG = {
    "language": {"output": "en"},
    "voice": {"provider": "edge", "voice": "v", "hindi_voice": "h", "rate": "+8%"},
    "captions": {"min_words": 2, "max_words": 5, "pause_break": 0.16,
                 "highlight_color": "#FFFFFF", "text_color": "#FFFFFF"},
    "video": {"fps": 30, "tail_seconds": 1.0, "shot_seconds": 2.2},
    "publish": {
        "state_backend": "local", "provider": "buffer",
        "max_posts_per_day": 2, "min_hours_between_posts": 3,
        "youtube": False, "instagram": False,
    },
}


@pytest.fixture
def gen_env(tmp_path):
    """Run generate.py with all external work mocked; state is a MagicMock."""
    import generate as G

    old_cwd = Path.cwd()
    state = MagicMock()
    state.data = {"reels": []}
    state.is_seen.return_value = False
    story = {
        "headline": "Ek badi khabar", "hook": "Ek sentence.",
        "script": "Ek sentence. Doosra sentence.",
        "sentences": ["Ek sentence.", "Doosra sentence."],
        "word_count": 120, "source": "Test Source",
        "url": "https://example.com/news/ek-badi-khabar",
        "published": "2026-01-01T00:00:00Z", "language": "en",
    }
    article = {
        "url": story["url"], "url_key": story["url"], "title": story["headline"],
        "title_key": "ek badi khabar", "source": "Test Source",
        "published": story["published"], "description": "", "paragraphs": [],
        "images": ["https://example.com/a.jpg"],
    }
    words = [
        {"id": "w0", "text": "Ek", "start": 0.0, "end": 0.3},
        {"id": "w1", "text": "sentence.", "start": 0.3, "end": 0.7},
    ]
    ctx: dict = {"state": state, "story": story, "article": article}

    with ExitStack() as stack:
        def mock(target, new=_UNSET, **kwargs):
            if new is _UNSET:
                obj = stack.enter_context(patch(target, **kwargs))
            else:
                obj = stack.enter_context(patch(target, new, **kwargs))
            return obj

        mock("generate.HERE", tmp_path)
        mock("generate.load_config", return_value=deepcopy(GEN_CFG))
        mock("generate.State").return_value = state
        mock("generate.discover", return_value=[article])
        mock("generate.enrich_and_gate", return_value=[article])
        mock("generate.build_story", return_value=story)
        mock("generate.qa_flags", return_value=[])
        mock("generate.synthesize", return_value={
            "words": words, "duration": 5.0,
        })
        mock("generate.prepare_stills", return_value=["shot_00.jpg"])
        mock("generate.write_project")
        mock("generate.check_project", return_value={"ok": True})
        mock("generate.render_project", return_value=True)
        mock("generate.cap_size")
        upload = mock("pipeline.publish.upload_video",
                      return_value={"url": "https://media.example/reel.mp4"})
        publish_all = mock("pipeline.publish.publish_all", return_value={
            "youtube": {"post_id": "p1", "platform": "youtube",
                        "posted_at": datetime.now(timezone.utc).isoformat()},
        })
        ctx.update({"upload": upload, "publish_all": publish_all})
        try:
            yield ctx
        finally:
            os.chdir(old_cwd)


def _run_generate(monkeypatch):
    import generate as G
    monkeypatch.setattr(sys, "argv", [
        "generate.py", "--run", "--count", "1", "--local-state",
    ])
    return G.main()


def test_generate_skips_social_post_when_daily_cap_reached(gen_env, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    gen_env["state"].data = {"reels": [
        {"id": "a", "published": {"youtube": {"post_id": "1", "posted_at": now}}},
        {"id": "b", "published": {"youtube": {"post_id": "2", "posted_at": now}}},
    ]}

    assert _run_generate(monkeypatch) == 0  # the job still succeeds
    gen_env["publish_all"].assert_not_called()
    gen_env["upload"].assert_called_once()  # render + upload still happened

    runs = [call.args[0] for call in gen_env["state"].record_run.call_args_list]
    assert runs[-1]["outcome"] == "skipped_rate_limit"
    reel = runs[-1]["reels"][0]
    assert reel["publish_skipped"]["reason"] == "daily_cap"
    assert reel["video_url"] == "https://media.example/reel.mp4"


def test_generate_skips_social_post_when_too_soon(gen_env, monkeypatch):
    recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    gen_env["state"].data = {"reels": [
        {"id": "a", "published": {"youtube": {"post_id": "1", "posted_at": recent}}},
    ]}

    assert _run_generate(monkeypatch) == 0
    gen_env["publish_all"].assert_not_called()
    runs = [call.args[0] for call in gen_env["state"].record_run.call_args_list]
    assert runs[-1]["outcome"] == "skipped_rate_limit"
    assert runs[-1]["reels"][0]["publish_skipped"]["reason"] == "min_hours"


def test_generate_publishes_and_records_posted_at(gen_env, monkeypatch):
    gen_env["state"].data = {"reels": []}

    assert _run_generate(monkeypatch) == 0
    gen_env["publish_all"].assert_called_once()
    runs = [call.args[0] for call in gen_env["state"].record_run.call_args_list]
    assert runs[-1]["outcome"] == "rendered"
    reel = runs[-1]["reels"][0]
    assert reel["published"]["youtube"]["posted_at"]  # cap works on later runs
