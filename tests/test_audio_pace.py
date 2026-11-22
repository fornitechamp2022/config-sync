"""Audio pacing + resilience tests (no network, no LLM, no ffmpeg).

edge_tts.Communicate is replaced by a scripted fake stream so timing math,
sentence alignment, retries and voice fallback are all deterministic.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from edge_tts.exceptions import NoAudioReceived

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline import voice as V  # noqa: E402
from pipeline.utils import load_config  # noqa: E402

CFG = load_config(ROOT / "config.yaml")
AUDIO = b"fake-mp3-payload"


# ---------------------------------------------------------------------------
# Fake edge-tts stream
# ---------------------------------------------------------------------------

class FakeCommunicate:
    """Scripted stand-in for edge_tts.Communicate.

    ``outcomes`` is consumed one per constructed client. An element may be an
    Exception (raised from stream()), the string "empty" (audio chunk with no
    payload), or "ok" (replay ``chunks``). Voice name is recorded per call.
    """

    outcomes: list = []
    chunks: list[dict] = []
    calls: list[str] = []
    duration: float = 58.0

    def __init__(self, text, voice, rate=None, boundary=None):
        self.voice = voice
        self.rate = rate
        self.boundary = boundary

    async def stream(self):
        FakeCommunicate.calls.append(self.voice)
        outcome = FakeCommunicate.outcomes.pop(0) if FakeCommunicate.outcomes else "ok"
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == "empty":
            yield {"type": "audio", "data": b""}
            return
        for chunk in FakeCommunicate.chunks:
            yield chunk


@pytest.fixture(autouse=True)
def fake_tts(monkeypatch):
    FakeCommunicate.outcomes = []
    FakeCommunicate.chunks = []
    FakeCommunicate.calls = []
    FakeCommunicate.duration = 58.0
    monkeypatch.setattr(V.edge_tts, "Communicate", FakeCommunicate)
    monkeypatch.setattr(V, "duration_of", lambda path: FakeCommunicate.duration)
    monkeypatch.setattr(V.time, "sleep", lambda seconds: None)
    return FakeCommunicate


# ---------------------------------------------------------------------------
# Fixtures/helpers for realistic Hindi script timing
# ---------------------------------------------------------------------------

def make_story(words: int = 140, sentences: int = 7) -> dict:
    per = words // sentences
    sents = [" ".join(["खबर"] * per) + "।" for _ in range(sentences)]
    script = " ".join(sents)
    return {
        "script": script,
        "sentences": sents,
        "tokens": script.split(),
        "language": "hi",
    }


def make_events(story: dict, total: float, gap: float = 0.25) -> list[dict]:
    """SentenceBoundary-style events proportional to script token counts."""
    counts = [len(s.split()) for s in story["sentences"]]
    total_tokens = sum(counts)
    speakable = total - gap * (len(counts) - 1)
    events = []
    cursor = 0.0
    for sentence, count in zip(story["sentences"], counts):
        dur = speakable * count / total_tokens
        events.append({
            "text": sentence,
            "start": round(cursor, 3),
            "end": round(cursor + dur, 3),
        })
        cursor += dur + gap
    return events


def chunks_for(events: list[dict]) -> list[dict]:
    chunks = [{"type": "audio", "data": AUDIO}]
    for event in events:
        chunks.append({
            "type": "SentenceBoundary",
            "text": event["text"],
            "offset": int(event["start"] * V.TICKS),
            "duration": int((event["end"] - event["start"]) * V.TICKS),
        })
    return chunks


def synthesize(story, tmp_path, fake, **kwargs):
    fake.chunks = chunks_for(make_events(story, fake.duration))
    return V.synthesize_natural(story, CFG, tmp_path,
                                backoff=(0, 0, 0), retries=3, **kwargs)


# ---------------------------------------------------------------------------
# Pacing / timing math
# ---------------------------------------------------------------------------

def test_syllable_weight_handles_matras_and_clusters():
    assert V._syllable_weight("क") == 1.0                 # plain consonant
    assert V._syllable_weight("की") > V._syllable_weight("क")   # matra lengthens
    assert V._syllable_weight("क्ष") < 2.0                # virama cluster is half
    assert V._syllable_weight("खबर") > V._syllable_weight("क")


def test_words_are_monotonic_and_never_clip(tmp_path, fake_tts):
    story = make_story(140, 7)
    result = synthesize(story, tmp_path, fake_tts)

    words = result["words"]
    assert len(words) == 140
    assert all(0.0 <= w["start"] < w["end"] <= fake_tts.duration for w in words)
    for first, second in zip(words, words[1:]):
        assert first["end"] <= second["start"] + 0.0011   # no overlap
        assert second["start"] >= first["start"]          # monotonic starts


def test_sentence_spans_align_and_include_pause(tmp_path, fake_tts):
    story = make_story(140, 7)
    result = synthesize(story, tmp_path, fake_tts)

    spans = result["sentence_spans"]
    assert len(spans) == len(story["sentences"])
    assert spans[0]["start"] == pytest.approx(0.0, abs=0.01)
    assert spans[-1]["end"] <= fake_tts.duration + 1e-6
    for first, second in zip(spans, spans[1:]):
        # Each sentence end extends to the next sentence start (natural pause).
        assert first["end"] == pytest.approx(second["start"], abs=1e-6)
    assert len(result["natural_pauses"]) == 6
    assert all(pause >= 0.20 for pause in result["natural_pauses"])

    # Drift vs proportional script-token timing stays small.
    assert 0.0 <= result["max_sentence_drift"] < 1.0


def test_narration_duration_in_target_window(tmp_path, fake_tts):
    result = synthesize(make_story(140, 7), tmp_path, fake_tts)
    assert 45.0 <= result["duration"] <= 65.0


def test_merged_boundary_events_keep_spans_and_words_monotonic(tmp_path, fake_tts):
    """TTS may emit fewer SentenceBoundary events than script sentences."""
    story = make_story(20, 4)
    fake_tts.duration = 11.5
    fake_tts.chunks = chunks_for([
        {"text": story["script"][:60], "start": 0.0, "end": 5.0},
        {"text": story["script"][60:], "start": 5.4, "end": 11.0},
    ])

    result = V.synthesize_natural(story, CFG, tmp_path, backoff=(0,), retries=0)

    spans = result["sentence_spans"]
    assert len(spans) == 4
    assert all(a["end"] <= b["start"] + 0.0011 for a, b in zip(spans, spans[1:]))
    assert all(a["end"] <= b["start"] + 0.0011
               for a, b in zip(result["words"], result["words"][1:]))


def test_no_boundary_events_falls_back_gracefully(tmp_path, fake_tts):
    story = make_story(20, 4)
    fake_tts.chunks = [{"type": "audio", "data": AUDIO}]  # no boundaries at all

    result = V.synthesize_natural(story, CFG, tmp_path, backoff=(0,), retries=0)

    assert len(result["words"]) == 20
    assert len(result["sentence_spans"]) == 4
    assert all(a["end"] <= b["start"] + 0.0011
               for a, b in zip(result["words"], result["words"][1:]))


def test_voice_result_exposes_sentence_spans_for_debug_dump(tmp_path, fake_tts):
    result = synthesize(make_story(20, 4), tmp_path, fake_tts)
    for key in ("sentence_spans", "max_sentence_drift", "voice", "rate", "pace_profile"):
        assert key in result
    assert [span["index"] for span in result["sentence_spans"]] == [0, 1, 2, 3]


# ---------------------------------------------------------------------------
# Pace profile configuration
# ---------------------------------------------------------------------------

def test_pace_profile_config_applies_rate_and_pauses():
    assert CFG["voice"]["pace_profile"] == "news"
    news = V.resolve_pace(CFG["voice"])
    calm = V.resolve_pace({**CFG["voice"], "pace_profile": "calm"})

    assert news["rate"] == "+8%"
    assert calm["rate"] == "-4%"
    assert news["sentence_pause"] != calm["sentence_pause"]
    assert news["voices"][0] == "hi-IN-MadhurNeural"
    assert "hi-IN-SwaraNeural" in news["voices"]


def test_legacy_hindi_rate_still_works_without_profile():
    pace = V.resolve_pace({"hindi_rate": "+3%", "hindi_voice": "hi-IN-MadhurNeural"})
    assert pace["rate"] == "+3%"


# ---------------------------------------------------------------------------
# Retry + fallback resilience (NoAudioReceived was crashing GitHub Actions)
# ---------------------------------------------------------------------------

def test_success_after_one_no_audio_received(tmp_path, fake_tts):
    story = make_story(20, 4)
    good = chunks_for(make_events(story, 12.0))
    fake_tts.duration = 12.0
    fake_tts.chunks = good
    fake_tts.outcomes = [NoAudioReceived("No audio was received."), "ok"]

    result = V.synthesize_natural(story, CFG, tmp_path, backoff=(0, 0, 0), retries=3)

    assert len(fake_tts.calls) == 2
    assert result["voice"] == "hi-IN-MadhurNeural"
    assert (tmp_path / "assets" / "voice" / "voice.mp3").read_bytes() == AUDIO


def test_success_after_switching_voice(tmp_path, fake_tts):
    story = make_story(20, 4)
    fake_tts.duration = 12.0
    fake_tts.chunks = chunks_for(make_events(story, 12.0))
    # retries=0: primary and first fallback fail, second fallback succeeds.
    fake_tts.outcomes = [
        NoAudioReceived(), NoAudioReceived(), "ok",
    ]

    result = V.synthesize_natural(story, CFG, tmp_path, backoff=(), retries=0)

    assert fake_tts.calls == ["hi-IN-MadhurNeural", "hi-IN-SwaraNeural",
                              "en-IN-NeerjaNeural"]
    assert result["voice"] == "en-IN-NeerjaNeural"


def test_all_attempts_fail_raises_voice_synthesis_error(tmp_path, fake_tts):
    story = make_story(20, 4)
    # 3 voices x 2 attempts each (retries=1).
    fake_tts.outcomes = [NoAudioReceived() for _ in range(6)]

    with pytest.raises(V.VoiceSynthesisError):
        V.synthesize_natural(story, CFG, tmp_path, backoff=(0,), retries=1)

    assert len(fake_tts.calls) == 6
    assert not (tmp_path / "assets" / "voice" / "voice.mp3").exists()


def test_empty_audio_is_never_accepted(tmp_path, fake_tts):
    story = make_story(20, 4)
    fake_tts.outcomes = ["empty" for _ in range(6)]

    with pytest.raises(V.VoiceSynthesisError):
        V.synthesize_natural(story, CFG, tmp_path, backoff=(0,), retries=1)

    assert len(fake_tts.calls) == 6
    assert not (tmp_path / "assets" / "voice" / "voice.mp3").exists()
