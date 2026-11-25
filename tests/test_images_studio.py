"""Focused regression tests for the edge-to-edge TV-studio still geometry.

These pin the properties that used to fail: the sharp photographs covered only
the upper third of the frame, leaving a large empty blurred band. No network
access is used — every source is a synthetic, deterministic PIL image.
"""
from __future__ import annotations

import random

from PIL import Image, ImageDraw, ImageStat

import pipeline.images as imgs

W, H = 1080, 1920


def _photo(seed: int, w: int = 1600, h: int = 900) -> Image.Image:
    """A distinct, textured synthetic photograph."""
    rnd = random.Random(seed)
    noise = Image.effect_noise((w, h), 42)
    base = (seed * 37) % 90
    channels = tuple(
        Image.eval(noise, lambda v, b=b: min(255, max(0, int(v * 0.55 + b))))
        for b in (base, (base + 60) % 180, (base + 120) % 180)
    )
    img = Image.merge("RGB", channels)
    draw = ImageDraw.Draw(img)
    for _ in range(5):
        cx, cy = rnd.randrange(w), rnd.randrange(h)
        colour = (rnd.randrange(80, 255), rnd.randrange(40, 200), rnd.randrange(40, 200))
        r = rnd.randrange(w // 12, w // 5)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=colour)
    return img


def _covered_rows(boxes: list[tuple[int, int, int, int]]) -> set[int]:
    rows: set[int] = set()
    for x0, y0, x1, y1 in boxes:
        rows.update(range(y0, y1))
    return rows


def _empty_band(boxes: list[tuple[int, int, int, int]],
                min_uncovered: float = 0.10) -> int:
    """Longest run of rows where >=10% of the canvas width is bare backdrop."""
    bad: list[int] = []
    for y in range(H):
        covered = 0
        for x0, y0, x1, y1 in boxes:
            if y0 <= y < y1:
                covered += max(0, min(x1, W) - max(x0, 0))
        if (W - covered) / W >= min_uncovered:
            bad.append(y)
    longest = run = 0
    previous = -2
    for y in bad:
        run = run + 1 if y == previous + 1 else 1
        longest = max(longest, run)
        previous = y
    return longest


# (a) height coverage ---------------------------------------------------------

def test_panels_cover_at_least_70_percent_of_canvas_height():
    for variant in range(12):
        name, boxes = imgs.layout_boxes(W, H, variant)
        coverage = len(_covered_rows(boxes)) / H
        assert coverage >= 0.70, (name, coverage)


# (b) area coverage -----------------------------------------------------------

def test_panels_cover_at_least_80_percent_of_canvas_area():
    for variant in range(12):
        name, boxes = imgs.layout_boxes(W, H, variant)
        covered = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in boxes)
        assert covered / (W * H) >= 0.80, (name, covered)


# (c) no empty band -----------------------------------------------------------

def test_no_plain_background_band_taller_than_12_percent():
    limit = 0.12 * H
    for variant in range(12):
        name, boxes = imgs.layout_boxes(W, H, variant)
        assert _empty_band(boxes) <= limit, (name, _empty_band(boxes))


# (d) anti-upscaling still holds ----------------------------------------------

def test_native_crop_and_panel_tile_keep_no_upscale_contract():
    """Native crops stay within the source, panel_scale <= 1, tiles are exact."""
    panels = ((529, 1920), (537, 957), (1080, 931), (545, 957))
    for size in ((1600, 900), (1200, 800), (900, 600)):
        src = _photo(100 + size[0], *size)
        for pw, ph in panels:
            cw, ch = imgs.native_crop_size(src, pw, ph)
            assert 1 <= cw <= src.width and 1 <= ch <= src.height
            assert imgs.panel_scale(src, pw, ph) <= 1.0
            tile = imgs.panel_tile(src, pw, ph)
            assert tile.size == (pw, ph) and tile.mode == "RGB"


# (e) consecutive variants differ ---------------------------------------------

def test_consecutive_variants_have_distinct_geometry_and_pixels():
    geometries = [tuple(imgs.layout_boxes(W, H, v)[1]) for v in range(len(imgs.LAYOUTS))]
    assert len(set(geometries)) == len(imgs.LAYOUTS)
    for i, geometry in enumerate(geometries):
        assert geometry != geometries[(i + 1) % len(geometries)]

    items = [{"image": _photo(200 + i)} for i in range(4)]
    digests = [imgs.dhash(imgs.bake_collage(items, W, H, v))
               for v in range(2 * len(imgs.LAYOUTS))]
    for i in range(len(digests) - 1):
        assert digests[i] != digests[i + 1], i


# compositional checks --------------------------------------------------------

def test_baked_still_has_photography_at_every_height():
    """No baked band may be a flat, dark blur — the panels carry the frame."""
    items = [{"image": _photo(300 + i)} for i in range(4)]
    step = int(0.08 * H)
    for variant in range(len(imgs.LAYOUTS)):
        still = imgs.bake_collage(items, W, H, variant)
        for y0 in range(0, H, step):
            band = still.crop((0, y0, W, min(H, y0 + step)))
            stat = ImageStat.Stat(band)
            assert sum(stat.mean) / 3 > 40, (variant, y0)
            assert max(stat.stddev) > 10, (variant, y0)


def test_scrim_touches_only_the_bottom_quarter_and_stays_light():
    flat = Image.new("RGB", (W, H), (200, 200, 200))
    out = imgs._scrim(flat, W, H)
    assert out.getpixel((10, int(H * 0.70))) == (200, 200, 200)
    assert out.getpixel((10, int(H * 0.78))) == (200, 200, 200)
    assert sum(out.getpixel((10, H - 1))) / 3 > 100


def test_bake_still_single_source_is_also_full_frame():
    item = {"image": _photo(400)}
    for variant in range(len(imgs.LAYOUTS)):
        still = imgs.bake_still(item, W, H, variant)
        assert still.size == (W, H) and still.mode == "RGB"
        _, boxes = imgs.layout_boxes(W, H, variant)
        rows = _covered_rows(boxes)
        assert len(rows) / H >= 0.70
        top = ImageStat.Stat(still.crop((0, 0, W, 300))).mean
        assert sum(top) / 3 > 40
