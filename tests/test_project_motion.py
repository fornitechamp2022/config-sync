"""Motion-craft contract for the generated news reel.

The generator must feel like a studio news package rather than a slideshow:
varied deterministic camera moves, a deliberate beat on a subset of cuts, and
subtle arrivals/outro on the graphics -- all as absolute-time GSAP tweens on
*child* wrappers, never on a timed ``.clip`` host.

Everything is generated locally with patched audio helpers, so the tests are
fast, offline and fully deterministic.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline.project as P  # noqa: E402

URL = "https://secret.example.com/private/news?id=12345"
CFG = {
    "language": {"output": "hi"},
    "video": {"width": 1080, "height": 1920, "fps": 30,
              "shot_seconds": 2.2, "tail_seconds": 1.0},
    "captions": {"min_words": 2, "max_words": 5, "pause_break": 0.16,
                 "highlight_color": "#FFE600", "text_color": "#FFFFFF"},
    "mix": {"bgm_volume": 0.10, "whoosh_volume": 0.16},
}
STORY = {
    "headline": "बिग बॉस 20 का नया एपिसोड आ गया और सलमान ने की बात",
    "hook": "बिग बॉस 20 का नया एपिसोड आ गया।",
    "script": "बिग बॉस 20 का नया एपिसोड आ गया। सलमान ने सबसे कहा कि यह एपिसोड खास होगा।",
    "sentences": ["बिग बॉस 20 का नया एपिसोड आ गया।",
                  "सलमान ने सबसे कहा कि यह एपिसोड खास होगा।"],
    "language": "hi", "source": "Secret Source", "url": URL,
}
VOICE = {"audio": "assets/voice/voice.mp3", "duration": 60.0}
SHOT_COUNT = 21  # enough shots for several every-third-cut dips
SHOTS = [{"path": f"assets/stills/shot_{i:02d}.jpg"} for i in range(SHOT_COUNT)]
CAPTIONS = [
    {"id": "cap0", "text": "बिग बॉस 20", "start": 0.2, "end": 1.4, "words": [
        {"id": "w0", "text": "बिग", "start": 0.2, "end": 0.6},
        {"id": "w1", "text": "बॉस", "start": 0.6, "end": 1.0},
        {"id": "w2", "text": "20", "start": 1.0, "end": 1.4},
    ]},
]


def _generate(workdir, monkeypatch):
    """Write one project with local audio stubs; return its HTML strings."""
    monkeypatch.setattr(P, "_fetch_bgm", lambda dest, duration: {
        "path": "assets/audio/bgm.mp3", "title": "Test bed",
        "creator": "Test", "license": "CC", "url": URL,
    })
    monkeypatch.setattr(P, "_prepare_audio",
                        lambda workdir, sfx_name="whoosh-short.mp3":
                        "assets/audio/whoosh-short.mp3")
    manifest = {"story": {"url": URL, "source": "Secret Source"}}
    result = P.write_project(workdir, dict(STORY), dict(VOICE),
                             [dict(g) for g in CAPTIONS], list(SHOTS),
                             CFG, manifest)
    return {
        "index": (Path(workdir) / "index.html").read_text(encoding="utf-8"),
        "caps": (Path(workdir) / "compositions" / "captions.html").read_text(encoding="utf-8"),
        "result": result,
    }


@pytest.fixture
def project(tmp_path, monkeypatch):
    return _generate(tmp_path, monkeypatch)


def _tween_lines(html: str) -> list[str]:
    return [ln for ln in html.splitlines()
            if re.search(r"\btl\.(?:fromTo|to|from|set)\(", ln)]


def _tween_targets(html: str) -> list[str]:
    return re.findall(r'\btl\.(?:fromTo|to|from|set)\(\s*["\']#([A-Za-z0-9_]+)["\']', html)


def _clip_ids(html: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"[^>]*class="[^"]*\bclip\b', html))


# --------------------------------------------------------------------------- #
# 1. Varied, deterministic camera moves
# --------------------------------------------------------------------------- #
def test_three_distinct_camera_moves_by_shot_index(project):
    moves = re.findall(r'class="shot-motion" data-camera="([a-z]+)"', project["index"])
    assert len(moves) >= 6, moves
    assert set(moves) >= {"push", "pan", "punch"}, set(moves)
    # No two consecutive shots use the identical move.
    assert all(a != b for a, b in zip(moves, moves[1:])), moves
    # The cycle is deterministic, not random.
    assert moves[:6] == ["push", "pan", "punch", "push", "pan", "punch"]


def test_camera_tweens_encode_distinct_geometry_and_eases(project):
    js = project["index"].splitlines()

    def line_for(shot_id):
        return next(ln for ln in js if f'tl.fromTo("#{shot_id}"' in ln)

    push = line_for("shotmotion0")
    pan = line_for("shotmotion1")
    punch = line_for("shotmotion2")
    # push = slow scale up; pan = horizontal x sweep; punch = quick scale snap
    assert 'scale:1.085' in push and 'power2.inOut' in push
    assert 'x:-30' in pan and 'x:30' in pan and 'sine.inOut' in pan
    assert 'scale:1.1' in punch or 'scale:1.10' in punch
    assert 'power3.out' in punch


def test_hero_panel_has_its_own_quick_punch_in(project):
    html = project["index"]
    assert 'id="heropunch" class="hero-punch"' in html
    assert 'tl.fromTo("#heropunch", {scale:1}' in html
    assert 'scale:1.06,duration:0.38' in html
    # Nested punch lives inside an image wrapper, never on the .clip host.
    assert 'id="heropunch"' not in _clip_ids(html)


def test_generation_is_deterministic(tmp_path, monkeypatch):
    a = _generate(tmp_path / "a", monkeypatch)["index"]
    b = _generate(tmp_path / "b", monkeypatch)["index"]
    assert a == b, "the same inputs produced different motion HTML"


# --------------------------------------------------------------------------- #
# 2. Deliberate transition beat on a subset of cuts
# --------------------------------------------------------------------------- #
def test_dip_overlays_only_on_every_third_cut(project):
    html = project["index"]
    dips = re.findall(r'<div id="dip(\d+)" class="clip dip-overlay"', html)
    expected = [str(i) for i in range(3, SHOT_COUNT, 3)]
    assert dips == expected, (dips, expected)
    # A subset of cuts, not every cut.
    assert len(dips) < SHOT_COUNT - 1

    for i in expected:
        assert f'id="dip{i}inner" class="dip-inner"' in html
        # The opacity tween targets the child, never the .clip host.
        assert f'tl.fromTo("#dip{i}inner", {{opacity:0}}' in html
        assert f'tl.to("#dip{i}inner", {{opacity:0' in html
        assert f'"#dip{i}"' not in html.split("<script>")[1]


def test_dip_child_starts_hidden_in_css(project):
    css = re.search(r"\.dip-inner\{([^}]+)\}", project["index"]).group(1)
    assert "background:#000" in css
    assert "opacity:0" in css


# --------------------------------------------------------------------------- #
# 3. Graphics arrivals, one pulse, clean outro
# --------------------------------------------------------------------------- #
def test_lower_third_pulse_is_a_single_child_beat(project):
    html = project["index"]
    assert 'id="lowerpulse" class="lower-pulse"' in html
    pulses = re.findall(r'tl\.fromTo\("#lowerpulse"', html)
    assert len(pulses) == 1
    assert 'opacity:0.9' in html
    assert 'tl.to("#lowerpulse"' in html
    assert '"#lowerpulse"' not in "".join(_clip_ids(html))


def test_clean_outro_fades_the_whole_graphics_package(project):
    js = project["index"]
    for target in ("lowerpanel", "tickerinner", "breakinginner", "bugpanel"):
        assert re.search(rf'tl\.to\("#{target}", \{{opacity:0', js), target
    outro = round(project["result"]["duration"] - 1.1, 2)
    assert js.count(f"}}, {outro});") >= 4


# --------------------------------------------------------------------------- #
# 4. Absolute-time tweens on children only; no forbidden properties on clips
# --------------------------------------------------------------------------- #
def test_every_tween_uses_an_absolute_time(project):
    for html in (project["index"], project["caps"]):
        lines = _tween_lines(html)
        assert lines, "no tweens generated"
        for line in lines:
            assert re.search(r",\s*-?\d+(?:\.\d+)?\);\s*$", line), line
        assert "Date.now" not in html and "Math.random" not in html
        assert "repeat:-1" not in html and "repeat: -1" not in html


def test_no_visibility_display_autoalpha_tween_on_clips(project):
    clip_ids = _clip_ids(project["index"]) | _clip_ids(project["caps"])
    for html in (project["index"], project["caps"]):
        for line in _tween_lines(html):
            if not re.search(r"\b(display|visibility|autoAlpha)\b", line):
                continue
            targets = set(_tween_targets(line))
            assert not (targets & clip_ids), line
            # Index composition never hides a clip via these props at all.
            if html is project["index"]:
                assert False, f"forbidden property in main timeline: {line}"


def test_no_tween_targets_a_clip_host(project):
    clip_ids = _clip_ids(project["index"]) | _clip_ids(project["caps"])
    targets = _tween_targets(project["index"]) + _tween_targets(project["caps"])
    assert targets
    assert not (set(targets) & clip_ids), sorted(set(targets) & clip_ids)


# --------------------------------------------------------------------------- #
# 5. Safe zones, captions and assets unchanged
# --------------------------------------------------------------------------- #
def test_text_safe_zones_and_captions_untouched(project):
    caps = project["caps"]
    group_css = re.search(r"\.cap-group\{([^}]+)\}", caps).group(1)
    assert "left:150px" in group_css
    assert "right:150px" in group_css
    assert "bottom:740px" in group_css
    assert 'data-composition-id="captions"' in project["index"]
    assert 'data-track-kind="captions"' in project["index"]
    assert URL not in project["index"] and URL not in caps


def test_no_external_image_assets_added(project):
    srcs = re.findall(r'<img[^>]+src="([^"]+)"', project["index"])
    assert srcs, "no stills referenced"
    for src in srcs:
        assert src.startswith("assets/"), src
        assert "http" not in src
