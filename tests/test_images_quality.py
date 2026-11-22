"""Offline quality tests for source validation and collage still baking.

All source pictures are synthetic: no network and no third-party assets are
needed to prove dimensions, panel count, dedupe, ad rejection and layout variety.
"""
from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw

import pipeline.images as imgs


def _photo(seed: int, w: int = 1600, h: int = 900) -> Image.Image:
    """A distinct, JPEG-compressible 'photograph' with texture and a subject."""
    rnd = random.Random(seed)
    noise = Image.effect_noise((w, h), 42)
    base = (seed * 37) % 90
    channels = tuple(Image.eval(noise, lambda v, b=b: min(255, max(0, int(v * 0.55 + b))))
                     for b in (base, (base + 60) % 180, (base + 120) % 180))
    img = Image.merge("RGB", channels)
    draw = ImageDraw.Draw(img)
    for _ in range(5):
        cx, cy = rnd.randrange(w), rnd.randrange(h)
        colour = (rnd.randrange(80, 255), rnd.randrange(40, 200), rnd.randrange(40, 200))
        r = rnd.randrange(w // 12, w // 5)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour)
    return img


def _jpeg(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=94)
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


def _monkeypatch_get(monkeypatch, mapping: dict[str, bytes], calls: list[str]) -> None:
    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(mapping.get(url, b""))
    monkeypatch.setattr(imgs.requests, "get", fake_get)


def _quality92_tables() -> dict[int, list[int]]:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (100, 90, 80)).save(buf, "JPEG", quality=92)
    return Image.open(buf).quantization


# --- source validation ---------------------------------------------------------

def test_ad_hosts_and_creative_url_markers_are_rejected(monkeypatch):
    good = "https://news.example.com/lead.jpg"
    calls: list[str] = []
    _monkeypatch_get(monkeypatch, {good: _jpeg(_photo(1))}, calls)
    bad = [
        "https://doubleclick.net/ads/leaderboard.jpg",
        "https://static.googlesyndication.com/pagead/creative.jpg",
        "https://cdn.example.com/banner-top.jpg",
        "https://cdn.example.com/fit-in/600/400/lead.jpg",
        "https://cdn.example.com/logo-header.jpg",
        "https://tracker.example.com/pixel.gif",
    ]
    out = imgs.download_images(bad + [good], limit=8)
    assert calls == [good], "ad/creative hosts must be skipped before download"
    assert [item["url"] for item in out] == [good]


def test_skip_hosts_and_small_images_are_rejected(monkeypatch):
    urls = {
        "https://small.example.com/thumb.jpg": _jpeg(_photo(2, 800, 700)),
        "https://short.example.com/short.jpg": _jpeg(_photo(3, 1200, 500)),
        "https://blocked.example.com/lead.jpg": _jpeg(_photo(4)),
        "https://good.example.com/lead.jpg": _jpeg(_photo(5)),
    }
    calls: list[str] = []
    _monkeypatch_get(monkeypatch, urls, calls)
    out = imgs.download_images(list(urls), limit=8,
                               skip_hosts=("blocked.example.com",))
    assert [item["url"] for item in out] == ["https://good.example.com/lead.jpg"]
    assert "https://blocked.example.com/lead.jpg" not in calls


def test_perceptual_dedupe_drops_identical_and_near_duplicate_images(monkeypatch):
    a, b = _photo(10), _photo(11)
    near = a.resize((1500, 844), Image.Resampling.LANCZOS)
    urls = {
        "https://news.example.com/a.jpg": _jpeg(a),
        "https://cdn.example.com/a-copy.jpg": _jpeg(a),
        "https://news.example.com/a-resized.jpg": _jpeg(near),
        "https://news.example.com/b.jpg": _jpeg(b),
    }
    calls: list[str] = []
    _monkeypatch_get(monkeypatch, urls, calls)
    out = imgs.download_images(list(urls), limit=8)
    assert [item["url"] for item in out] == [
        "https://news.example.com/a.jpg", "https://news.example.com/b.jpg"]
    assert imgs.dhash_distance(out[0]["hash"], out[1]["hash"]) > 4


def test_toi_thumbnail_is_upgraded_to_full_width(monkeypatch):
    url = "https://static.toiimg.com/thumb/msid-42,imgsize-900?width=300&height=200"
    upgraded = imgs.upgrade_source_url(url)
    assert "width=1280" in upgraded and "height=720" in upgraded
    calls: list[str] = []
    _monkeypatch_get(monkeypatch, {upgraded: _jpeg(_photo(12))}, calls)
    out = imgs.download_images([url], limit=4)
    assert calls == [upgraded]
    assert out and out[0]["width"] == 1600


def test_source_priority_and_og_image_are_respected(monkeypatch):
    og = "https://paper.example.com/og/lead.jpg"
    thumb = "https://paper.example.com/thumb_300/lead.jpg"
    assert imgs.source_priority(og) < imgs.source_priority(thumb)

    captured: list[list[str]] = []
    monkeypatch.setattr(imgs, "download_images",
                        lambda urls, *a, **k: captured.append(urls) or [])
    cfg = {"video": {"width": 1080, "height": 1920, "min_shots": 1,
                     "max_shots": 4, "skip_image_hosts": ()},
           "gate": {"min_width": 900}}
    assert imgs.prepare_stills({"og_image": og, "images": [thumb, og]}, cfg, "/tmp/x") == []
    assert captured[0][0] == og
    assert captured[0].count(og) == 1


# --- panel geometry ------------------------------------------------------------

def test_layout_templates_cycle_without_repeating_and_keep_panels_large():
    seen = []
    for idx in range(9):
        name, boxes = imgs.layout_boxes(1080, 1920, idx)
        assert name == imgs.LAYOUTS[idx % 4]
        assert len(boxes) >= 3
        areas = [(x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes]
        assert min(areas) / sum(areas) >= 0.25 - 1e-9, (name, areas)
        stage = max(b[2] for b in boxes) - min(b[0] for b in boxes)
        stage *= max(b[3] for b in boxes) - min(b[1] for b in boxes)
        assert min(areas) / stage >= 0.25 - 1e-9, (name, areas, stage)
        for x0, y0, x1, y1 in boxes:
            assert 0 <= x0 < x1 <= 1080 and 0 <= y0 < y1 <= 1920
            assert y0 >= 0.15 * 1920 and y1 <= 0.55 * 1920  # graphics/caption space
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * \
                          max(0, min(a[3], b[3]) - max(a[1], b[1]))
                assert overlap == 0, (name, a, b)
        seen.append(tuple(boxes))
    assert len(set(seen[:4])) == 4


def test_panels_are_cropped_natively_and_only_downscaled():
    for size in ((1600, 900), (1200, 800), (900, 600)):
        src = _photo(20 + size[0], *size)
        cw, ch = imgs.native_crop_size(src, 518, 691)
        assert cw <= size[0] and ch <= size[1]
        assert imgs.panel_scale(src, 518, 691) <= 1.0
        tile = imgs.panel_tile(src, 518, 691)
        assert tile.size == (518, 691) and tile.mode == "RGB"


# --- baked stills --------------------------------------------------------------

def test_baked_stills_are_1080x1920_rgb_high_quality_with_accent_borders():
    items = [{"image": _photo(30 + i)} for i in range(4)]
    ref = _quality92_tables()
    for idx in range(4):
        still = imgs.bake_collage(items, 1080, 1920, idx)
        assert still.size == (1080, 1920) and still.mode == "RGB"
        buf = io.BytesIO()
        still.save(buf, "JPEG", quality=imgs.JPEG_QUALITY, subsampling=0)
        buf.seek(0)
        saved = Image.open(buf)
        assert saved.size == (1080, 1920) and saved.mode == "RGB"
        assert all(saved.quantization[k][0] <= ref[k][0] for k in (0, 1))
        _, boxes = imgs.layout_boxes(1080, 1920, idx)
        x0, y0, _, _ = boxes[0]
        assert still.getpixel((x0 + 1, y0 + 1)) == imgs.ACCENTS[0]
        # The top band is deliberately quiet negative space for graphics.
        top = still.crop((0, 0, 1080, 300)).resize((1, 1)).getpixel((0, 0))
        assert sum(top) / 3 < 110


def test_bake_collage_changes_framing_on_consecutive_shots():
    items = [{"image": _photo(40 + i)} for i in range(4)]
    digests = [imgs.dhash(imgs.bake_collage(items, 1080, 1920, i)) for i in range(4)]
    assert len(set(digests)) == 4


def test_prepare_stills_produces_varied_valid_files(tmp_path, monkeypatch):
    urls = [f"https://news.example.com/photo-{i}.jpg" for i in range(4)]
    mapping = {url: _jpeg(_photo(50 + i)) for i, url in enumerate(urls)}
    calls: list[str] = []
    _monkeypatch_get(monkeypatch, mapping, calls)
    cfg = {"video": {"width": 1080, "height": 1920, "min_shots": 5,
                     "max_shots": 8, "skip_image_hosts": ()},
           "gate": {"min_width": 900}}
    article = {"og_image": urls[0], "images": urls}
    shots = imgs.prepare_stills(article, cfg, tmp_path, target_shots=5)

    assert len(shots) == 5
    assert [shot["layout"] for shot in shots[:4]] == list(imgs.LAYOUTS)
    ref = _quality92_tables()
    for shot in shots:
        path = tmp_path / shot["path"]
        assert path.exists()
        with Image.open(path) as saved:
            assert saved.size == (1080, 1920) and saved.mode == "RGB"
            assert saved.format == "JPEG"
            assert all(saved.quantization[k][0] <= ref[k][0] for k in (0, 1))
        assert len(shot["source_urls"]) >= 3
    assert shots[0]["source_url"] == urls[0]
