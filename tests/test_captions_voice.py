import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.captions import group_words
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
