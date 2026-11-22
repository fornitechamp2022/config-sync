import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.captions import (
    build_caption_timeline,
    group_plain,
    group_words,
    mark_emphasis,
    render_timeline_js,
    split_lines,
    write_captions,
)
from pipeline.translate import romanize
from pipeline.utils import load_config

CFG = load_config(Path(__file__).resolve().parents[1] / "config.yaml")


def word(text, start, end):
    return {"id": f"w{start}", "text": text, "start": start, "end": end}


def test_romanize_keeps_sentence_boundary():
    result = romanize("पहला। दूसरा।")
    assert result.endswith(".") and result.count(".") == 2, result


def test_group_words_breaks_at_sentence_end():
    words = [
        word("big", 0.0, 0.2), word("boss", 0.2, 0.4), word("20.", 0.4, 0.6),
        word("new", 0.7, 0.9), word("story", 0.9, 1.1),
    ]
    groups = group_words(words, CFG)
    assert groups[0]["text"].endswith("."), groups[0]["text"]
    assert all(not g["text"].endswith(".") for g in groups[1:]), groups


def test_single_word_group_does_not_merge_across_sentence():
    words = [
        word("first", 0.0, 0.2), word("sentence.", 0.2, 0.4),
        word("next", 0.5, 0.7), word("one.", 0.7, 0.9),
    ]
    groups = group_words(words, CFG)
    assert groups[0]["words"][-1]["text"].endswith(".")
    assert "next" not in groups[0]["text"]


def test_groups_cover_all_words():
    words = [word(f"w{i}", i * 0.2, i * 0.2 + 0.15) for i in range(12)]
    groups = group_words(words, CFG)
    assert sum(len(g["words"]) for g in groups) == len(words)


# --------------------------------------------------------------------------- #
# Iteration 3 -- acceptance criteria
# --------------------------------------------------------------------------- #
def test_no_group_crosses_sentence_boundary():
    # Three sentences with varying length; a bad grouping would mix e.g.
    # "doosri." with "teensri" in one group.
    words = [
        word("pehli", 0.0, 0.2), word("khabar", 0.2, 0.4), word("hai.", 0.4, 0.6),
        word("doosri", 0.7, 0.9), word("bhi", 0.9, 1.1), word("aayi.", 1.1, 1.3),
        word("teensri", 1.4, 1.6), word("aur", 1.6, 1.8), word("aakhri.", 1.8, 2.0),
    ]
    groups = group_words(words, CFG)
    sentence_texts = ["pehli khabar hai.", "doosri bhi aayi.", "teensri aur aakhri."]
    for group in groups:
        for expected in sentence_texts:
            overlap = set(group["text"].split()) & set(expected.split())
            assert not (overlap and overlap != set(group["text"].split())), (
                f"group crosses sentence boundary: {group['text']}"
            )
    assert sum(len(g["words"]) for g in groups) == len(words)


def test_group_size_bounds_with_single_deliberate_first_word():
    words = [word(f"w{i}", i * 0.2, i * 0.2 + 0.15) for i in range(23)]
    groups = group_words(words, CFG)
    assert len(groups) >= 1
    for i, group in enumerate(groups):
        size = len(group["words"])
        if size == 1:
            assert i == 0, f"unexpected orphan group at index {i}: {group['text']}"
        else:
            assert 2 <= size <= 5, (i, size, group["text"])


def test_all_words_covered_exactly_once():
    words = [
        word("ek", 0.0, 0.2), word("do.", 0.2, 0.4),
        word("teen", 0.5, 0.7), word("chaar", 0.7, 0.9), word("paanch.", 0.9, 1.1),
        word("chhah", 1.2, 1.4), word("saat", 1.4, 1.6), word("aath.", 1.6, 1.8),
    ]
    groups = group_words(words, CFG)
    covered = [w["text"] for g in groups for w in g["words"]]
    original = [w["text"] for w in words]
    assert sorted(covered) == sorted(original)
    assert len(covered) == len(set(id(w) for g in groups for w in g["words"]))
    # No word dict is shared between groups (no aliasing of the caller's data).
    assert all(w is not words[0] for g in groups for w in g["words"])


def test_group_words_does_not_mutate_input():
    words = [word("bigg", 0.0, 0.2), word("boss", 0.2, 0.4), word("20.", 0.4, 0.6)]
    before = [dict(w) for w in words]
    group_words(words, CFG)
    assert words == before


def test_conjunction_prefers_break():
    words = [
        word("salman", 0.0, 0.2), word("ghar", 0.2, 0.4),
        word("aur", 0.4, 0.6), word("khan", 0.6, 0.8), word("aaye.", 0.8, 1.0),
    ]
    groups = group_words(words, CFG)
    # "aur" should lead a new group rather than end the previous one, and no
    # group should exceed the configured maximum.
    assert all(len(g["words"]) <= 5 for g in groups)
    joined = [g["text"].split() for g in groups]
    flat = [t for group in joined for t in group]
    assert flat == [w["text"] for w in words]


def test_orphan_word_is_merged_within_sentence():
    # The pause after "boss." chunk is followed by a single word "hi" which
    # must be folded into a neighbour rather than left alone.
    words = [
        word("bigg", 0.0, 0.2), word("boss", 0.2, 0.4), word("hi.", 0.4, 0.6),
        word("aur", 0.7, 0.9), word("salman", 0.9, 1.1), word("bhi.", 1.1, 1.3),
    ]
    groups = group_words(words, CFG)
    sizes = [len(g["words"]) for g in groups]
    assert all(s >= 2 for s in sizes) or sizes[0] == 1, sizes
    assert sum(sizes) == len(words)


def test_pause_before_last_word_rebalances_instead_of_orphan():
    # Regression: a 5-word chunk followed by a pause and the sentence's final
    # word used to leave a 1-word group.  Rebalancing must yield 4 + 2.
    words = [
        word("scout", 0.0, 0.2), word("willingly", 0.2, 0.4),
        word("took", 0.4, 0.6), word("up", 0.6, 0.8),
        word("the", 0.8, 1.0),                       # gap >= pause here
        word("responsibility.", 1.2, 1.5),
    ]
    groups = group_words(words, CFG)
    sizes = [len(g["words"]) for g in groups]
    assert all(2 <= size <= 5 for size in sizes), sizes
    assert [w["text"] for g in groups for w in g["words"]] == [w["text"] for w in words]
    assert all(not g["text"].endswith(".") or g["words"][-1]["text"].endswith(".")
               for g in groups)


def test_split_lines_caps_length_and_keeps_words_intact():
    words = [
        word("salman", 0.0, 0.2), word("khan", 0.2, 0.4),
        word("bollywood", 0.4, 0.6), word("superstar", 0.6, 0.8),
    ]
    lines = split_lines(words, 24)
    assert len(lines) >= 2
    for line in lines:
        text = " ".join(words[i]["text"] for i in line)
        assert len(text) <= 24, text
    flat = [i for line in lines for i in line]
    assert flat == list(range(len(words)))
    # All word indices present exactly once.


def test_group_carries_line_metadata():
    words = [
        word("yahan", 0.0, 0.2), word("bahut", 0.2, 0.4),
        word("lambe", 0.4, 0.6), word("shabdon", 0.6, 0.8), word("hain.", 0.8, 1.0),
    ]
    cfg = dict(CFG)
    cfg["captions"] = dict(CFG["captions"])
    cfg["captions"]["max_line_chars"] = 10
    groups = group_words(words, cfg)
    for group in groups:
        assert "lines" in group and "line_texts" in group
        for text in group["line_texts"]:
            assert len(text) <= 10, text
        flat = [i for line in group["lines"] for i in line]
        assert flat == list(range(len(group["words"])))


def test_emphasis_metadata_does_not_change_color_behavior():
    words = [
        word("bigg", 0.0, 0.2), word("boss", 0.2, 0.4), word("20.", 0.4, 0.6),
        word("salman", 0.7, 0.9), word("khan", 0.9, 1.1), word("aaye.", 1.1, 1.3),
    ]
    groups = group_words(words, CFG)
    emphasis = {w["text"]: w["emphasis"] for g in groups for w in g["words"]}
    assert emphasis["20."] is True          # digit
    assert emphasis["salman"] is True       # known entity
    assert emphasis["khan"] is True         # known entity
    assert emphasis["aaye."] is False       # ordinary word

    timeline = build_caption_timeline(groups, CFG)
    on_colors = {e["props"]["color"] for e in timeline["events"] if e["kind"] == "word_on"}
    off_colors = {e["props"]["color"] for e in timeline["events"] if e["kind"] == "word_off"}
    assert on_colors == {CFG["captions"]["highlight_color"]}
    assert off_colors == {CFG["captions"]["text_color"]}

    # mark_emphasis standalone also flags digits/entities without mutating input.
    marked = mark_emphasis(words, CFG)
    assert marked[2]["emphasis"] is True and "emphasis" not in words[2]


def test_build_caption_timeline_events_and_monotonicity():
    words = [
        word("ek", 0.0, 0.2), word("do.", 0.2, 0.4),
        word("teen", 0.5, 0.7), word("chaar", 0.7, 0.9), word("paanch.", 0.9, 1.1),
    ]
    groups = group_words(words, CFG)
    timeline = build_caption_timeline(groups, CFG)
    assert timeline["valid"] is True, timeline["errors"]
    assert timeline["errors"] == []

    kinds = {e["kind"] for e in timeline["events"]}
    assert {"group_show", "group_hide", "word_on", "word_off"} <= kinds

    # Flat events sorted by time.
    times = [e["time"] for e in timeline["events"]]
    assert times == sorted(times)

    # Preserved keys plus new ones on every group.
    for group in timeline["groups"]:
        assert {"id", "text", "words", "start", "end"} <= set(group)
        assert "word_timings" in group and "events" in group

    # Per-word on < off and non-overlapping hand-off.
    for group in timeline["groups"]:
        timings = group["word_timings"]
        for t in timings:
            assert t["off"] > t["on"] >= 0
        for a, b in zip(timings, timings[1:]):
            assert a["off"] <= b["on"]


def test_build_caption_timeline_normalizes_bad_timing():
    # Two groups: one has an inverted word (end < start); another has an
    # overlapping pair.  The builder must report errors yet still emit
    # monotonic events.
    groups = [
        {
            "id": "cap0", "text": "a b", "start": 0.0, "end": 0.5,
            "words": [word("a", 0.0, 0.3), word("b", 0.1, 0.2)],
        },
        {
            "id": "cap1", "text": "c d", "start": 1.0, "end": 0.5,  # inverted group
            "words": [word("c", 1.0, 0.9)],
        },
    ]
    timeline = build_caption_timeline(groups, CFG)
    assert timeline["valid"] is False
    assert any("end" in e and "start" in e for e in timeline["errors"])

    times = [e["time"] for e in timeline["events"]]
    assert times == sorted(times)
    for group in timeline["groups"]:
        for t in group.get("word_timings", []):
            assert t["off"] > t["on"]


def test_render_timeline_js_uses_configured_colors():
    groups = group_words([word("ek", 0.0, 0.2), word("do.", 0.2, 0.4)], CFG)
    timeline = build_caption_timeline(groups, CFG)
    js = render_timeline_js(timeline)
    assert "tl.set(" in js
    assert CFG["captions"]["highlight_color"] in js
    assert CFG["captions"]["text_color"] in js
    assert "#cap0_w0" in js


def test_group_plain_still_returns_list_of_lists():
    groups = group_plain(["ek", "do.", "teen", "chaar", "paanch."], CFG)
    assert isinstance(groups, list)
    assert all(isinstance(g, list) for g in groups)
    assert [t for g in groups for t in g] == ["ek", "do.", "teen", "chaar", "paanch."]


def test_write_captions_roundtrip(tmp_path):
    words = [word("ek", 0.0, 0.2), word("do.", 0.2, 0.4)]
    groups = group_words(words, CFG)
    path = write_captions(groups, tmp_path)
    assert path.exists()
    assert path.name == "captions.json"
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == groups
