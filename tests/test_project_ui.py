"""Structural checks for the generated news-channel graphics package.

All file writes are local (tmp_path); audio/network helpers are patched so the
tests never download anything.  The point is to assert the *contract* of the
generated HTML: required elements, safe-zone geometry, palette, and that every
GSAP tween targets a child wrapper rather than a ``.clip`` host.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline.project as P  # noqa: E402
from pipeline.captions import build_caption_timeline  # noqa: E402

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

VOICE = {"audio": "assets/voice/voice.mp3", "duration": 12.5}

CAPTIONS = [
    {"id": "cap0", "text": "बिग बॉस 20", "start": 0.2, "end": 1.4, "words": [
        {"id": "w0", "text": "बिग", "start": 0.2, "end": 0.6},
        {"id": "w1", "text": "बॉस", "start": 0.6, "end": 1.0},
        {"id": "w2", "text": "20", "start": 1.0, "end": 1.4},
    ]},
    {"id": "cap1", "text": "सलमान ने कहा", "start": 2.0, "end": 3.2, "words": [
        {"id": "w3", "text": "सलमान", "start": 2.0, "end": 2.5},
        {"id": "w4", "text": "ने", "start": 2.5, "end": 2.8},
        {"id": "w5", "text": "कहा", "start": 2.8, "end": 3.2},
    ]},
]

SHOTS = [{"path": f"assets/stills/shot_{i:02d}.jpg"} for i in range(7)]


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Generate a project with deterministic local audio placeholders."""
    monkeypatch.setattr(P, "_fetch_bgm", lambda dest, duration: {
        "path": "assets/audio/bgm.mp3", "title": "Test bed",
        "creator": "Test", "license": "CC", "url": URL,
    })
    monkeypatch.setattr(P, "_prepare_audio",
                        lambda workdir, sfx_name="whoosh-short.mp3":
                        "assets/audio/whoosh-short.mp3")
    manifest = {"story": {"url": URL, "source": "Secret Source"}}
    result = P.write_project(tmp_path, dict(STORY), dict(VOICE),
                             [dict(g) for g in CAPTIONS], list(SHOTS),
                             CFG, manifest)
    index = (tmp_path / "index.html").read_text(encoding="utf-8")
    caps = (tmp_path / "compositions" / "captions.html").read_text(encoding="utf-8")
    return {"dir": tmp_path, "index": index, "caps": caps,
            "manifest": manifest, "result": result}


def _clip_ids(html: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"[^>]*class="[^"]*\bclip\b', html))


def _tween_targets(html: str) -> list[str]:
    """Ids referenced as GSAP tween/set targets (first JS argument)."""
    return re.findall(r'\btl\.(?:fromTo|to|from|set)\(\s*["\']#([A-Za-z0-9_]+)["\']', html)


# --------------------------------------------------------------------------- #
# 1. Required elements + attributes
# --------------------------------------------------------------------------- #
def test_news_ui_elements_exist(project):
    html = project["index"]
    expected = [
        "hookcard", "hookinner", "hookrule",
        "masthead", "mastheadinner", "mastheadbrand", "mastheadlive", "mastheadkicker",
        "channelbug", "bugpanel", "bugmark", "buglive",
        "lowerthird", "lowerpanel", "lowerlabel", "lowerinner",
        "ticker", "tickerinner", "tickerlabel", "tickertext",
        "progress", "progressbar",
        "breakingcard", "breakingfield", "breakingframe", "breakingdiag",
        "breakinginner", "breakinglabel", "breakingrule", "breakingheadline",
        "breakingkicker", "breakingflash", "breakingpulse",
        "captions",
    ]
    for element_id in expected:
        assert f'id="{element_id}"' in html, f"missing #{element_id}"
    # The hovering always-on red strip is gone for good.
    assert "breakingbar" not in html

    duration = project["result"]["duration"]
    for host in ("masthead", "channelbug", "lowerthird", "ticker", "progress"):
        block = re.search(rf'<div id="{host}"[^>]*>', html).group(0)
        assert 'class="clip' in block
        assert 'data-start="0"' in block
        assert f'data-duration="{duration}"' in block

    # Caption host stays a single composition host with the captions kind.
    host = re.search(r'<div id="captions"[^>]*>', html).group(0)
    assert 'data-composition-id="captions"' in host
    assert 'data-composition-src="compositions/captions.html"' in host
    assert 'data-track-kind="captions"' in host


def test_ticker_phrases_are_derived_from_script(project):
    ticker = re.search(r'id="tickertext"[^>]*>([^<]+)</span>', project["index"]).group(1)
    assert "•" in ticker
    for phrase in (p.strip() for p in ticker.split("•")):
        assert phrase, "empty ticker phrase"
        assert URL not in phrase


# --------------------------------------------------------------------------- #
# 2. Palette, typography, safe zone
# --------------------------------------------------------------------------- #
def test_palette_and_minimum_text_size(project):
    html = project["index"]
    for variable in ("--news-red:#E11D2E", "--news-yellow:#FFE600", "--news-navy:#0A0F1E"):
        assert variable in html

    # Any font-size declared in the generated CSS must be >= 28 px (vertical spec).
    sizes = [int(v) for v in re.findall(r"font-size:(\d+)px", html)]
    assert sizes and min(sizes) >= 28, sizes


def test_caption_band_keeps_safe_zone_geometry(project):
    caps = project["caps"]
    group_css = re.search(r"\.cap-group\{([^}]+)\}", caps).group(1)
    # Captions are centred in the 1080px frame (equal 120px side margins),
    # not biased towards the left like the lower-third banner.
    assert "left:120px" in group_css
    assert "right:120px" in group_css
    assert "max-width:840px" in group_css
    assert "bottom:760px" in group_css
    assert ".cap-line" in caps
    line_css = re.search(r"\.cap-line\{([^}]+)\}", caps).group(1)
    cap_size = int(re.search(r"font-size:(\d+)px", line_css).group(1))
    assert cap_size >= 52, cap_size
    assert min(int(v) for v in re.findall(r"font-size:(\d+)px", caps)) >= 28


# --------------------------------------------------------------------------- #
# 3. Privacy
# --------------------------------------------------------------------------- #
def test_source_url_stays_out_of_rendered_html(project):
    assert URL not in project["index"]
    assert URL not in project["caps"]
    # It remains available in the manifest for attribution/audit.
    assert project["manifest"]["story"]["url"] == URL


# --------------------------------------------------------------------------- #
# 4. Animation targets children, never .clip hosts
# --------------------------------------------------------------------------- #
def test_animations_never_target_clip_elements(project):
    clip_ids = _clip_ids(project["index"]) | _clip_ids(project["caps"])
    targets = _tween_targets(project["index"]) + _tween_targets(project["caps"])
    assert targets, "no tweens generated"
    animated_clips = sorted(set(targets) & clip_ids)
    assert not animated_clips, f"GSAP targets .clip hosts: {animated_clips}"


def test_no_display_autoalpha_tweens_and_visibility_only_on_children(project):
    for html in (project["index"], project["caps"]):
        for line in html.splitlines():
            if not re.search(r"\btl\.(?:fromTo|to|from|set)\(", line):
                continue
            assert "display" not in line
            assert "autoAlpha" not in line
            if "visibility" in line:
                # Visibility flips are allowed only on an inner child wrapper.
                assert re.search(r'["\']#[A-Za-z0-9_]+_inner["\']', line), line


# --------------------------------------------------------------------------- #
# 5. Deterministic intro / outro + shared caption timeline
# --------------------------------------------------------------------------- #
def test_intro_and_outro_use_absolute_times(project):
    js = project["index"]
    for target, time in (("mastheadinner", "0.08"), ("hookrule", "0.22"),
                         ("bugpanel", "0.34"), ("lowerpanel", "0.46"),
                         ("tickerinner", "0.56")):
        assert re.search(rf'tl\.fromTo\("#{target}"', js), f"missing #{target} tween"
        assert f", {time});" in js
    outro_at = round(project["result"]["duration"] - 1.1, 2)
    assert f'tl.to("#lowerpanel", {{' in js
    assert f"}}, {outro_at});" in js


def test_caption_timeline_helper_is_the_single_source_of_truth(project):
    caps = project["caps"]
    timeline = build_caption_timeline([dict(g) for g in CAPTIONS], CFG)
    assert timeline["valid"], timeline["errors"]
    for event in timeline["events"][:3]:
        # render_timeline_js must have serialized every helper event verbatim.
        assert f'tl.set("{event["selector"]}", {{' in caps
    assert 'tl.set("#cap0_w0", {"color":"#FFE600"}' in caps
    assert 'tl.set("#cap0_w0", {"color":"#FFFFFF"}' in caps
    assert project["manifest"]["video"]["caption_timeline_valid"] is True
    assert project["manifest"]["video"]["caption_timeline_errors"] == []


# --------------------------------------------------------------------------- #
# 6. Config-driven audio mix with backwards-compatible fallbacks
# --------------------------------------------------------------------------- #
def test_audio_mix_reads_config(project):
    html = project["index"]
    bgm = re.search(r'<audio id="bgm"[^>]*>', html).group(0)
    cut = re.search(r'<audio id="cut1"[^>]*>', html).group(0)
    assert 'data-volume="0.1"' in bgm
    assert 'data-volume="0.16"' in cut


def test_audio_mix_fallbacks_for_older_configs(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_fetch_bgm", lambda dest, duration: {"path": "assets/audio/bgm.mp3"})
    monkeypatch.setattr(P, "_prepare_audio", lambda workdir, sfx_name="whoosh-short.mp3":
                        "assets/audio/whoosh-short.mp3")
    older = {**CFG, "mix": {}}
    P.write_project(tmp_path, dict(STORY), dict(VOICE), [dict(g) for g in CAPTIONS],
                    list(SHOTS), older, {"story": {"url": URL}})
    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert 'data-volume="0.12"' in re.search(r'<audio id="bgm"[^>]*>', html).group(0)
    assert 'data-volume="0.22"' in re.search(r'<audio id="cut1"[^>]*>', html).group(0)
