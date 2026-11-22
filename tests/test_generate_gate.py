"""Unit tests for generate.py's QA gate, debug dump and state recording.

All external work (discovery, network, TTS, ffmpeg, HyperFrames, publishing) is
mocked; only the orchestration logic in generate.py is under test.
"""
from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_UNSET = object()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import generate as G  # noqa: E402


CFG = {
    "language": {"output": "hi"},
    "gate": {},
    "story": {"min_words": 110, "max_words": 165, "min_sentences": 4,
              "cta": "Follow for more Bollywood updates."},
    "voice": {"provider": "edge", "voice": "en-IN-PrabhatNeural",
              "hindi_voice": "hi-IN-MadhurNeural", "rate": "+8%", "hindi_rate": "+8%"},
    "captions": {"min_words": 2, "max_words": 5, "pause_break": 0.16,
                 "highlight_color": "#FFE600", "text_color": "#FFFFFF"},
    "video": {"fps": 30, "tail_seconds": 1.0, "shot_seconds": 2.2},
    "publish": {"state_backend": "blob"},
}


def fake_story(language: str) -> dict:
    script = "Ek sentence. Doosra sentence."
    return {
        "headline": "Ek badi khabar", "hook": "Ek sentence.",
        "script": script, "sentences": ["Ek sentence.", "Doosra sentence."],
        "word_count": 120, "source": "Test Source",
        "url": "https://example.com/news/ek-badi-khabar",
        "published": "2026-01-01T00:00:00Z", "tokens": script.split(),
        "language": language,
    }


def fake_words() -> list[dict]:
    return [
        {"id": "w0", "text": "Ek", "start": 0.0, "end": 0.3},
        {"id": "w1", "text": "sentence.", "start": 0.3, "end": 0.7},
        {"id": "w2", "text": "Doosra", "start": 0.7, "end": 1.1},
        {"id": "w3", "text": "sentence.", "start": 1.1, "end": 1.6},
    ]


def fake_article() -> dict:
    return {
        "url": "https://example.com/news/ek-badi-khabar",
        "url_key": "https://example.com/news/ek-badi-khabar",
        "title": "Ek badi khabar", "title_key": "ek badi khabar",
        "source": "Test Source", "published": "2026-01-01T00:00:00Z",
        "description": "", "paragraphs": [], "images": ["https://example.com/a.jpg"],
    }


def state_runs(state: MagicMock) -> list[dict]:
    return [call.args[0] for call in state.record_run.call_args_list]


@pytest.fixture
def env(tmp_path: Path):
    """Wire generate.py to in-memory mocks and mutable QA flags."""
    old_cwd = Path.cwd()
    flags_holder: dict[str, list[str]] = {"flags": []}
    article = fake_article()
    en_story, hi_story = fake_story("en"), fake_story("hi")
    state = MagicMock()
    state.is_seen.return_value = False
    ctx: dict = {"tmp": tmp_path, "flags": flags_holder["flags"], "state": state,
                 "article": article, "en_story": en_story, "hi_story": hi_story}

    with ExitStack() as stack:
        def mock(target: str, new=_UNSET, **kwargs):
            if new is _UNSET:
                obj = stack.enter_context(patch(target, **kwargs))
            else:
                obj = stack.enter_context(patch(target, new, **kwargs))
            return obj

        mock("generate.HERE", tmp_path)
        loader = mock("generate.load_config")
        loader.return_value = deepcopy(CFG)
        state_cls = mock("generate.State")
        state_cls.return_value = state
        mock("generate.discover", return_value=[article])
        enrich = mock("generate.enrich_and_gate", return_value=[article])
        mock("generate.build_story", return_value=en_story)
        to_hindi = mock("generate.to_hindi", return_value=hi_story)
        qa = mock("generate.qa_flags",
                  side_effect=lambda story, cfg: list(flags_holder["flags"]))
        synth_natural = mock("generate.synthesize_natural", return_value={
            "audio": "assets/voice/voice.mp3", "words": fake_words(), "duration": 12.5,
        })
        synth = mock("generate.synthesize", return_value={
            "audio": "assets/voice/voice.mp3", "words": fake_words(), "duration": 12.5,
        })
        stills = mock("generate.prepare_stills", return_value=["shot_00.jpg"])
        mock("generate.write_project")
        check = mock("generate.check_project", return_value={"ok": True})
        render = mock("generate.render_project", return_value=True)
        mock("generate.cap_size")

        ctx.update({"enrich": enrich, "to_hindi": to_hindi, "qa": qa,
                    "synth_natural": synth_natural, "synth": synth,
                    "prep_stills": stills, "check": check, "render": render})
        try:
            yield ctx
        finally:
            os.chdir(old_cwd)


def run_cli(monkeypatch, *args: str) -> int:
    monkeypatch.setattr(sys, "argv", ["generate.py", *args])
    return G.main()


def test_flagged_story_is_skipped_without_tts_or_render(env, monkeypatch):
    """Critical flags reject a story before synthesize/check/render are called."""
    env["flags"].extend(["truncated_lead", "question_headline"])

    rc = run_cli(monkeypatch, "--run", "--count", "1", "--no-render",
                 "--no-publish", "--local-state")

    assert rc == 0
    env["synth_natural"].assert_not_called()
    env["synth"].assert_not_called()
    env["prep_stills"].assert_not_called()
    env["check"].assert_not_called()
    env["render"].assert_not_called()
    assert not list((env["tmp"] / "out").glob("*.debug.json"))

    runs = state_runs(env["state"])
    assert len(runs) == 1
    assert runs[0]["outcome"] == "rejected"
    assert runs[0]["reason"] == "qa_critical_flags"
    assert "truncated_lead" in runs[0]["critical_flags"]
    assert env["article"]["url_key"] in runs[0]["articles"]


def test_debug_dump_written_for_passing_story(env, monkeypatch):
    """A clean story synthesizes Hindi, writes the dump, then records state."""
    rc = run_cli(monkeypatch, "--run", "--count", "1", "--no-render",
                 "--no-publish", "--local-state")

    assert rc == 0
    env["to_hindi"].assert_called_once()
    env["synth_natural"].assert_called_once()
    env["synth"].assert_not_called()

    dumps = sorted((env["tmp"] / "out").glob("*.debug.json"))
    assert len(dumps) == 1
    data = json.loads(dumps[0].read_text(encoding="utf-8"))

    assert data["script"] == env["hi_story"]["script"]
    assert data["language"] == "hi"
    assert data["flags"] == []
    assert data["critical_flags"] == []
    assert data["source_url"] == env["hi_story"]["url"]
    assert data["voice_provider"] == "edge"
    assert data["duration"] == 12.5
    assert data["caption_groups"]
    # Caption words come straight from synthesize_natural (already Hinglish).
    assert "Ek" in data["caption_groups"][0]["text"]
    # Sentence spans are derived from word timings.
    assert len(data["sentence_spans"]) == 2
    assert data["sentence_spans"][0]["start"] == 0.0
    assert data["sentence_spans"][-1]["end"] == 1.6

    assert [r["outcome"] for r in state_runs(env["state"])] == ["prepared"]


def test_noop_path_records_state(env, monkeypatch):
    """No fresh material exits cleanly but still records a 'noop' run."""
    env["enrich"].return_value = []

    rc = run_cli(monkeypatch, "--run", "--count", "1", "--local-state")

    assert rc == 0
    env["synth_natural"].assert_not_called()
    env["render"].assert_not_called()
    runs = state_runs(env["state"])
    assert len(runs) == 1
    assert runs[0]["outcome"] == "noop"
    assert runs[0]["reason"] == "insufficient_material"


def test_check_mode_prints_flag_counts_and_reject_reasons(env, monkeypatch, capsys):
    """--check reports flag counts and per-story reject reasons, and renders nothing."""
    env["flags"].extend(["truncated_lead", "vague_pronoun"])

    rc = run_cli(monkeypatch, "--check")

    out = capsys.readouterr().out
    assert "QA FLAG COUNTS" in out
    assert "CRITICAL REJECT COUNTS" in out
    assert "truncated_lead" in out
    assert "vague_pronoun" in out
    assert "REJECT" in out
    assert "reject reason: truncated_lead" in out
    assert rc == 2  # no clean story in the preview

    env["synth_natural"].assert_not_called()
    env["synth"].assert_not_called()
    env["render"].assert_not_called()
    env["state"].record_run.assert_not_called()


def test_warning_only_flags_do_not_block_and_are_logged(env, monkeypatch, capsys):
    """vague_pronoun is a warning: logged, but TTS and state still proceed."""
    env["flags"].append("vague_pronoun")

    rc = run_cli(monkeypatch, "--run", "--count", "1", "--no-render",
                 "--no-publish", "--local-state")

    assert rc == 0
    assert "[qa-warn]" in capsys.readouterr().out
    env["synth_natural"].assert_called_once()
    runs = state_runs(env["state"])
    assert len(runs) == 1
    assert runs[0]["outcome"] == "prepared"
    dump = json.loads(next((env["tmp"] / "out").glob("*.debug.json")).read_text(encoding="utf-8"))
    assert "vague_pronoun" in dump["flags"]
    assert "vague_pronoun" in dump["warnings"]
