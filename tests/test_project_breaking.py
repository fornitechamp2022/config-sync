"""Contract for the single full-screen mid-roll breaking-news card.

The old always-on red strip is gone.  This module proves the replacement is a
real studio-style beat: one full-frame clip at 35-60% of the runtime, above the
captions, animated only on child wrappers, with bounded copy and a layout that
never collides with the other banners.
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
SHOTS = [{"path": f"assets/stills/shot_{i:02d}.jpg"} for i in range(28)]
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

# Reserved vertical room for two caption lines (54px * 1.2 * 2 + padding + slack).
CAP_BAND_RESERVE = 180


@pytest.fixture
def project(tmp_path, monkeypatch):
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
    return {"index": index, "caps": caps, "manifest": manifest, "result": result}


def _attr(tag: str, name: str) -> str:
    match = re.search(rf'{name}="([^"]+)"', tag)
    assert match, f"{name} missing in {tag[:80]}"
    return match.group(1)


def _css(html: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\{([^}]+)\}", html)
    assert match, f"CSS rule {selector} missing"
    return match.group(1)


def _px(css: str, prop: str) -> float:
    match = re.search(rf"{re.escape(prop)}\s*:\s*(-?\d+(?:\.\d+)?)px", css)
    assert match, f"{prop} missing in {css[:80]}"
    return float(match.group(1))


def _z_index(tag: str) -> int:
    match = re.search(r"z-index:(\d+)", _attr(tag, "style"))
    assert match, tag
    return int(match.group(1))


def _card_tag(html: str) -> str:
    cards = re.findall(r'<div id="breakingcard"[^>]*>', html)
    assert len(cards) == 1, cards
    return cards[0]


def _tween_lines(html: str) -> list[str]:
    return [ln for ln in html.splitlines()
            if re.search(r"\btl\.(?:fromTo|to|from|set)\(", ln)]


def _clip_ids(html: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"[^>]*class="[^"]*\bclip\b', html))


# --------------------------------------------------------------------------- #
# 1. Exactly one card, full frame, mid-roll, above captions
# --------------------------------------------------------------------------- #
def test_exactly_one_fullscreen_midroll_card(project):
    html = project["index"]
    assert html.count('id="breakingcard"') == 1
    tag = _card_tag(html)
    assert 'class="clip breaking-card"' in tag
    # A full-frame card deliberately covers the canvas for its two seconds.
    assert "data-layout-allow-occlusion" in tag

    card_css = _css(html, ".breaking-card")
    assert "position:absolute" in card_css
    assert "inset:0" in card_css

    total = project["result"]["duration"]
    start = float(_attr(tag, "data-start"))
    duration = float(_attr(tag, "data-duration"))
    assert 0.35 * total <= start <= 0.60 * total, (start, total)
    assert 2.0 <= duration <= 3.0, duration
    assert start + duration <= total


def test_card_z_index_is_above_the_captions_subcomposition(project):
    html = project["index"]
    card_z = _z_index(_card_tag(html))
    captions_tag = re.search(r'<div id="captions"[^>]*>', html).group(0)
    assert card_z > _z_index(captions_tag), (card_z, captions_tag)
    assert card_z >= 90


# --------------------------------------------------------------------------- #
# 2. Motion targets children only; no static overlay
# --------------------------------------------------------------------------- #
def test_card_tween_targets_are_never_clip_hosts(project):
    html = project["index"]
    clip_ids = _clip_ids(html) | _clip_ids(project["caps"])
    card_lines = [ln for ln in _tween_lines(html) if "#breaking" in ln]
    assert card_lines, "card has no animation"
    for line in card_lines:
        targets = set(re.findall(r'["\']#([A-Za-z0-9_]+)["\']', line))
        assert targets, line
        assert not (targets & clip_ids), f"animated a .clip host: {line}"
        assert "breakingcard" not in targets, line
        assert "display" not in line and "autoAlpha" not in line, line


def test_card_entrance_exit_flash_and_pulse_are_animated(project):
    html = project["index"]
    tag = _card_tag(html)
    start = float(_attr(tag, "data-start"))
    length = float(_attr(tag, "data-duration"))

    for target in ("breakingfield", "breakingdiag", "breakingflash",
                   "breakinginner", "breakingrule", "breakingpulse"):
        assert re.search(rf'tl\.(?:fromTo|to)\("#{target}"', html), target
    assert 'clipPath:"inset(0 100% 0 0)"' in html       # wipe in
    assert 'clipPath:"inset(0 0% 0 0)"' in html         # fully open
    assert 'clipPath:"inset(0 0 0 100%)"' in html       # wipe out
    assert "{scaleX:0,opacity:1}" in html               # yellow flash bar
    assert "{opacity:0.9,duration:0.1" in html          # quick pulse

    # Every card tween is scheduled at an absolute time inside its window.
    times = []
    for line in _tween_lines(html):
        if "#breaking" not in line:
            continue
        match = re.search(r",\s*(-?\d+(?:\.\d+)?)\);\s*$", line)
        assert match, line
        times.append(float(match.group(1)))
    assert times
    assert all(start <= t <= start + length for t in times), (start, length, times)


# --------------------------------------------------------------------------- #
# 3. Studio design, bounded copy, typography
# --------------------------------------------------------------------------- #
def test_card_copy_is_bounded_and_studio_styled(project):
    html = project["index"]
    label = re.search(r'id="breakinglabel"[^>]*>([^<]+)<', html).group(1)
    assert label in ("ब्रेकिंग", "BREAKING"), label
    label_css = _css(html, ".breaking-label")
    assert int(re.search(r"font-size:(\d+)px", label_css).group(1)) >= 90

    headline = re.search(r'id="breakingheadline"[^>]*>([^<]+)<', html).group(1)
    assert headline
    assert len(headline) <= 50, (len(headline), headline)
    assert "linear-gradient" in _css(html, ".breaking-field")
    assert "var(--news-yellow)" in _css(html, ".breaking-rule")
    assert "border:2px" in _css(html, ".breaking-frame")
    assert "text-shadow" in label_css
    assert "text-shadow" in _css(html, ".breaking-headline")


def test_every_font_size_is_at_least_28_px(project):
    for html in (project["index"], project["caps"]):
        sizes = [int(v) for v in re.findall(r"font-size:(\d+)px", html)]
        assert sizes and min(sizes) >= 28, sizes


def test_banner_hierarchy_sizes_are_confident(project):
    html = project["index"]
    lower = int(re.search(r"font-size:(\d+)px",
                          _css(html, ".lower-inner")).group(1))
    ticker = int(re.search(r"font-size:(\d+)px",
                           _css(html, ".ticker-text")).group(1))
    kicker = int(re.search(r"font-size:(\d+)px",
                           _css(html, ".masthead-kicker")).group(1))
    assert 40 <= lower <= 48, lower
    assert 30 <= ticker <= 34, ticker
    assert 28 <= kicker <= 34, kicker


def test_banners_respect_sixty_pixel_side_safe_margins(project):
    html = project["index"]
    for selector in (".masthead-band", ".lower-panel", ".ticker-inner",
                     ".breaking-inner"):
        css = _css(html, selector)
        assert _px(css, "left") >= 60, selector
        assert _px(css, "right") >= 60, selector
    cap_css = _css(project["caps"], ".cap-group")
    assert _px(cap_css, "left") >= 60
    assert _px(cap_css, "right") >= 60


def test_breaking_card_fills_lower_field_and_flash_clears_the_copy(project):
    """The card must not become an empty red field or strike through its text."""
    html = project["index"]
    for element_id in ("breakingstripes", "breakingtape", "breakingflash", "breakingpulse"):
        assert f'id="{element_id}"' in html

    inner = _css(html, ".breaking-inner")
    # Copy is centred in the 1080px frame, not biased to one side.
    assert _px(inner, "left") == _px(inner, "right")
    assert int(re.search(r"font-size:(\d+)px", _css(html, ".breaking-label")).group(1)) >= 110

    # The flash/pulse sit well below the headline block.
    flash = _css(html, ".breaking-flash")
    assert _px(flash, "top") >= _px(inner, "top") + 450

    tape = _css(html, ".breaking-tape")
    stripes = _css(html, ".breaking-stripes")
    assert _px(tape, "bottom") == 0 and _px(tape, "height") >= 80
    assert _px(stripes, "bottom") >= 80

    tweens = "\n".join(_tween_lines(html))
    assert '#breakingstripes"' in tweens
    assert '#breakingtape"' in tweens


# --------------------------------------------------------------------------- #
# 4. Old strip removed; captions and their highlight untouched
# --------------------------------------------------------------------------- #
def test_old_always_on_breakingbar_is_gone(project):
    assert "breakingbar" not in project["index"]
    assert "breakingbar" not in project["caps"]


def test_captions_still_valid_with_yellow_current_word(project):
    caps = project["caps"]
    assert 'data-track-kind="captions"' in project["index"]
    assert 'data-composition-id="captions"' in project["index"]
    assert 'tl.set("#cap0_w0", {"color":"#FFE600"}' in caps
    assert 'tl.set("#cap0_w0", {"color":"#FFFFFF"}' in caps
    assert 'class="cap-word"' in caps
    cap_css = _css(caps, ".cap-line")
    assert int(re.search(r"font-size:(\d+)px", cap_css).group(1)) >= 52
    video = project["manifest"]["video"]
    assert video["caption_timeline_valid"] is True
    assert video["caption_timeline_errors"] == []


def test_no_banner_caption_rectangle_overlap(project):
    html, caps = project["index"], project["caps"]
    frame_height = 1920.0

    mast = _css(html, ".masthead-band")
    bug = _css(html, ".bug-panel")
    lower = _css(html, ".lower-panel")
    ticker = _css(html, ".ticker-band")
    cap = _css(caps, ".cap-group")

    mast_top = _px(mast, "top")
    bug_top = _px(bug, "top")
    lower_top = _px(lower, "top")
    ticker_top = _px(ticker, "top")
    cap_bottom = frame_height - _px(cap, "bottom")

    rectangles = {
        "masthead": (mast_top, mast_top + _px(mast, "height")),
        "channelbug": (bug_top, bug_top + _px(bug, "height")),
        "lowerthird": (lower_top, lower_top + _px(lower, "min-height")),
        "captions": (cap_bottom - CAP_BAND_RESERVE, cap_bottom),
        "ticker": (ticker_top, ticker_top + _px(ticker, "height")),
    }
    ordered = sorted(rectangles.items(), key=lambda item: item[1][0])
    for (first, (_, first_end)), (second, (second_start, _)) in zip(ordered, ordered[1:]):
        assert first_end <= second_start, f"{first} overlaps {second}"
