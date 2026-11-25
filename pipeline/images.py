"""Source-image validation, dedupe and premium vertical collage baking.

Every still is exactly ``1080x1920`` (or whatever the configured canvas is):
sharp, full-resolution news panels laid out as a TV-studio wall that runs edge
to edge with only hairline gutters. There is no empty blurred band: the panels
themselves carry the frame from y=0 to y=canvas_h, and the blurred source is
only visible in the 6 px seams. Panels are cropped at *native* resolution and
only ever resized downwards, so the collage never shows upscaling blur. A
deterministic set of four layout templates (hero-left, hero-right, triptych,
grid) cycles with the shot index so consecutive shots never share a framing,
and identical Pillow colour grading makes the panels read as one package.
"""
from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageStat

from .utils import ensure_dir

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 Chrome/124 Safari/537.36"}

JPEG_QUALITY = 94                 # saved stills are always >= 92
MIN_SOURCE_HEIGHT = 600           # below this a source is a thumb, not a lead image

# Ad / creative / analytics hosts and URL markers. These are checked before a
# single byte is downloaded, so ad creatives and brand banners never enter a
# reel. Config-level ``skip_image_hosts`` is applied on top of this list.
AD_HOST_BLOCKLIST: tuple[str, ...] = (
    "doubleclick", "googlesyndication", "googleadservices", "adservice.google",
    "googletagmanager", "google-analytics", "adnxs", "adsrvr", "rubiconproject",
    "pubmatic", "criteo", "amazon-adsystem", "taboola", "outbrain",
    "scorecardresearch", "quantserve", "moatads", "adcolony", "chartbeat",
    "bat.bing", "figma.site", "figma.com", "canva.com",
)
AD_URL_MARKERS: tuple[str, ...] = (
    "/ads/", "/ad/", "adcreative", "ad_creative", "/creatives/", "creative-asset",
    "banner", "advert", "sponsor", "tracker", "tracking", "pixel", "beacon",
    "/logo", "logo-", "logo_", "icon-", "favicon", "sprite", "placeholder",
    "1x1.", "fit-in", "utm_",
)
# Patterns that mark a URL as a small thumbnail even before we fetch it.
THUMB_URL_MARKERS: tuple[str, ...] = (
    "/thumb/", "/thumbnail", "thumbnail_", "thumb_", "/small/", "/mini/",
    "100x100", "120x90", "150x150", "300x", "320x", "400x", "480x",
)

LAYOUTS: tuple[str, ...] = ("hero-left", "hero-right", "triptych", "grid")
ACCENTS: tuple[tuple[int, int, int], ...] = (
    (255, 230, 0),   # channel yellow on the hero panel
    (225, 29, 46),   # news red
    (245, 245, 240), # off-white keyline
    (225, 29, 46),
)

# Studio-wall geometry. Panels are full-bleed with only slim seams, so no
# contiguous plain-background band can appear anywhere in the frame. The
# broadcaster graphics in pipeline/project.py (breaking band, lower third,
# ticker, captions) sit on their own translucent panels and stay legible over
# the photography; only the very bottom gets a light caption scrim.
_GUTTER_FRAC = 0.006          # ~6 px at 1080 wide — hairline studio seams
_HERO_FRAC = 0.49             # just under half so every panel is >= 25% of area
_HERO_ROW_FRAC = 0.485        # horizontal hero band for the triptych variant
_SCRIM_TOP = 0.78             # gentle darkening of the final ~22% only
_SCRIM_ALPHA = 118            # light enough to keep the lower panels readable


def _gutter(canvas_w: int) -> int:
    """Width of a studio seam, scaled to the canvas but never zero."""
    return max(6, int(canvas_w * _GUTTER_FRAC))


def is_allowed_source(url: str, skip_hosts: tuple[str, ...] = ()) -> bool:
    """True when a URL is a plausible editorial image (not an ad/creative)."""
    low = (url or "").lower()
    if not low.startswith(("http://", "https://")):
        return False
    if any(host in low for host in AD_HOST_BLOCKLIST):
        return False
    if any(host in low for host in skip_hosts):
        return False
    return not any(marker in low for marker in AD_URL_MARKERS)


def upgrade_source_url(url: str) -> str:
    """Ask known thumbnail endpoints for their full-size variant.

    Times of India article images commonly arrive as ``toiimg.com/thumb/...``
    with ``width=300``; the same path serves a 1280px image when asked.
    """
    try:
        parts = urlparse(url)
    except Exception:
        return url
    host = (parts.netloc or "").lower()
    if "toiimg.com" not in host and "timesofindia" not in host:
        return url
    if "/thumb/" not in parts.path and not any(
            k.lower() in {"width", "w", "height", "h"} for k, _ in parse_qsl(parts.query)):
        return url
    pairs = []
    has_w = has_h = False
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        k = key.lower()
        if k in {"width", "w"}:
            value, has_w = "1280", True
        elif k in {"height", "h"}:
            value, has_h = "720", True
        pairs.append((key, value))
    if not has_w:
        pairs.append(("width", "1280"))
    if not has_h:
        pairs.append(("height", "720"))
    return urlunparse(parts._replace(query=urlencode(pairs)))


def source_priority(url: str) -> int:
    """Lower sorts first: og:image/full-size paths before tiny thumbnails."""
    low = url.lower()
    thumb = any(marker in low for marker in THUMB_URL_MARKERS)
    full = any(marker in low for marker in ("/full/", "/original", "large", "1280", "1920"))
    return (1 if thumb else 0) + (0 if full else 1)


def dhash(image: Image.Image) -> str:
    """64-bit difference hash of an image, independent of size and format."""
    g = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = g.tobytes()
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0"
                   for r in range(8) for c in range(8))
    return hex(int(bits, 2))[2:].zfill(16)


def dhash_distance(a: str, b: str) -> int:
    """Hamming distance between two difference hashes (0 == identical)."""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return 64


def download_images(urls: list[str], min_width: int = 900, limit: int = 16,
                    skip_hosts: tuple[str, ...] = (),
                    min_height: int = MIN_SOURCE_HEIGHT) -> list[dict]:
    """Fetch, validate and perceptually dedupe candidate source images.

    Rejects ad hosts/markers, ``fit-in`` and other thumbnail paths, and any
    decoded image smaller than ``min_width`` x ``min_height``. Near-duplicates
    (Hamming distance <= 4 in perceptual-hash space) are dropped as well.
    """
    seen: list[str] = []
    out: list[dict] = []
    unique = list(dict.fromkeys(u for u in urls if u))
    # The caller's first URL is og:image: always try it before the rest.
    candidates = unique[:1] + sorted(unique[1:], key=source_priority)
    for original_url in candidates:
        if len(out) >= limit:
            break
        original_url = original_url.replace("&amp;", "&")
        url = upgrade_source_url(original_url)
        if not is_allowed_source(url, skip_hosts):
            continue
        # Thumbnail URL markers survive upgrade only for TOI: its /thumb/ path
        # is the full-resolution endpoint once width=1280 is supplied.
        upgraded = url != original_url
        if not upgraded and any(marker in url.lower() for marker in THUMB_URL_MARKERS):
            continue
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            if resp.status_code != 200 or len(resp.content) < 12000:
                continue
            image = Image.open(io.BytesIO(resp.content))
            image.load()
            if image.width < max(min_width, 600) or image.height < max(min_height, 600):
                continue
            digest = dhash(image)
            if any(dhash_distance(digest, old) <= 4 for old in seen):
                continue
            seen.append(digest)
            out.append({"url": url, "image": image.convert("RGB"), "hash": digest,
                        "width": image.width, "height": image.height})
        except Exception:
            continue
    return out


def _cover(image: Image.Image, w: int, h: int) -> Image.Image:
    """Fill a box by cropping (centred) from the source; used for blur fill."""
    scale = max(w / image.width, h / image.height)
    size = (max(w, int(image.width * scale)), max(h, int(image.height * scale)))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _grade(image: Image.Image) -> Image.Image:
    """One subtle, identical grade for every panel and the blurred fill."""
    graded = ImageEnhance.Color(image).enhance(0.90)
    graded = ImageEnhance.Contrast(graded).enhance(1.06)
    graded = ImageEnhance.Brightness(graded).enhance(1.02)
    return Image.blend(graded, Image.new("RGB", graded.size, (10, 15, 30)), 0.035)


def native_crop_size(image: Image.Image, w: int, h: int) -> tuple[int, int]:
    """Native-resolution crop dimensions used to fill a ``w x h`` panel."""
    aspect = w / h
    if image.width / image.height > aspect:
        return max(1, min(image.width, int(round(image.height * aspect)))), image.height
    return image.width, max(1, min(image.height, int(round(image.width / aspect))))


def native_crop(image: Image.Image, w: int, h: int) -> Image.Image:
    """Crop the source at native size, favouring the upper third for faces."""
    cw, ch = native_crop_size(image, w, h)
    left = (image.width - cw) // 2
    top = int((image.height - ch) * 0.35)
    return image.crop((left, top, left + cw, top + ch))


def panel_scale(image: Image.Image, w: int, h: int) -> float:
    """Resize factor applied to a native crop; always <= 1.0 (never upscale)."""
    cw, ch = native_crop_size(image, w, h)
    return min(1.0, w / cw, h / ch)


def panel_tile(image: Image.Image, w: int, h: int) -> Image.Image:
    """Return an exactly ``w x h`` panel built only from downscaled pixels."""
    crop = native_crop(image, w, h)
    scale = min(1.0, w / crop.width, h / crop.height)
    if scale >= 0.999:
        return crop.resize((w, h), Image.Resampling.LANCZOS)
    fit_w = max(1, int(round(crop.width * scale)))
    fit_h = max(1, int(round(crop.height * scale)))
    fit = crop.resize((fit_w, fit_h), Image.Resampling.LANCZOS)
    mean = ImageStat.Stat(crop).mean
    backing = Image.new("RGB", (w, h), tuple(int(v * 0.38) for v in mean))
    backing.paste(fit, ((w - fit_w) // 2, (h - fit_h) // 2))
    return backing


def layout_boxes(canvas_w: int, canvas_h: int,
                 variant: int = 0) -> tuple[str, list[tuple[int, int, int, int]]]:
    """Deterministic studio-wall rectangles for one shot (3 or 4 panels).

    The panels fill the canvas edge to edge; only ``_gutter`` px seams show the
    blurred backing. Four templates cycle by shot index:

    * ``hero-left`` / ``hero-right`` — a full-height hero column on one side,
      two stacked secondary panels on the other (mirrored between variants).
    * ``triptych`` — a full-width hero band across the top with two secondary
      panels filling the lower part.
    * ``grid`` — four equal cells edge to edge (each exactly 25% of the area).

    Every panel is at least 25% of the total panel area, stays inside the
    canvas and never overlaps another panel.
    """
    name = LAYOUTS[int(variant) % len(LAYOUTS)]
    g = _gutter(canvas_w)
    w, h = canvas_w, canvas_h

    if name in ("hero-left", "hero-right"):
        hero_w = int(w * _HERO_FRAC)
        side_w = w - hero_w - g
        split = (h - g) // 2
        lower = split + g
        if name == "hero-left":
            return name, [
                (0, 0, hero_w, h),
                (hero_w + g, 0, w, split),
                (hero_w + g, lower, w, h),
            ]
        return name, [
            (w - hero_w, 0, w, h),
            (0, 0, side_w, split),
            (0, lower, side_w, h),
        ]

    if name == "triptych":
        hero_h = int(h * _HERO_ROW_FRAC)
        lower = hero_h + g
        pw = (w - g) // 2
        return name, [
            (0, 0, w, hero_h),
            (0, lower, pw, h),
            (pw + g, lower, w, h),
        ]

    # grid: four equal cells, edge to edge with a single central cross seam.
    pw, ph = (w - g) // 2, (h - g) // 2
    return name, [
        (0, 0, pw, ph),
        (pw + g, 0, w, ph),
        (0, ph + g, pw, h),
        (pw + g, ph + g, w, h),
    ]


def _scrim(image: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """Light gradient over the final ~22% so captions stay readable.

    The upper 78% of the frame is left completely untouched — the studio
    panels must stay bright and detailed there. The ramp peaks at a modest
    alpha, so the lower panels are still recognisable photographs rather than
    a flat dark band.
    """
    layer = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    start = int(canvas_h * _SCRIM_TOP)
    for y in range(start, canvas_h):
        alpha = int(_SCRIM_ALPHA * ((y - start) / max(1, canvas_h - start)) ** 1.6)
        draw.line([(0, y), (canvas_w, y)], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB")


def _studio_backdrop(src: Image.Image, canvas_w: int, canvas_h: int) -> Image.Image:
    """Dark blurred fill, now visible only inside the hairline panel seams."""
    return _grade(ImageEnhance.Brightness(
        _cover(src, canvas_w, canvas_h).filter(ImageFilter.GaussianBlur(46))).enhance(0.42))


def _keyline(bg: Image.Image, box: tuple[int, int, int, int],
             color: tuple[int, int, int]) -> None:
    """Thin, consistent studio accent keyline around one panel."""
    x0, y0, x1, y1 = box
    ImageDraw.Draw(bg).rectangle([x0, y0, x1 - 1, y1 - 1], outline=color, width=2)


def bake_still(item: dict, canvas_w: int, canvas_h: int, variant: int = 0) -> Image.Image:
    """Single-source fallback: one photo studio-tiled edge to edge.

    The old contain-fit left a large empty blurred band. Reusing
    ``layout_boxes`` keeps the same full-frame studio wall even when only one
    source is available; panels still come from native crops via ``panel_tile``.
    """
    src = item["image"]
    bg = _studio_backdrop(src, canvas_w, canvas_h)
    _, boxes = layout_boxes(canvas_w, canvas_h, variant)
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        bg.paste(_grade(panel_tile(src, x1 - x0, y1 - y0)), (x0, y0))
        _keyline(bg, (x0, y0, x1, y1), ACCENTS[i % len(ACCENTS)])
    return _scrim(bg, canvas_w, canvas_h)


def bake_collage(items: list[dict], canvas_w: int, canvas_h: int,
                 variant: int = 0) -> Image.Image:
    """Bake a multi-panel studio still; ``variant`` is the shot index (not a modulo).

    Sources rotate with the shot index as well, so neither the framing nor the
    lead picture repeats on consecutive shots. Panels fill the frame; the
    blurred base is visible only in the slim gutters between them.
    """
    if not items:
        raise ValueError("bake_collage needs at least one source image")
    _, boxes = layout_boxes(canvas_w, canvas_h, variant)
    base = items[variant % len(items)]["image"]
    bg = _studio_backdrop(base, canvas_w, canvas_h)

    for i, (x0, y0, x1, y1) in enumerate(boxes):
        src = items[(variant + i) % len(items)]["image"]
        tile = _grade(panel_tile(src, x1 - x0, y1 - y0))
        bg.paste(tile, (x0, y0))
        _keyline(bg, (x0, y0, x1, y1), ACCENTS[i % len(ACCENTS)])
    return _scrim(bg, canvas_w, canvas_h)


def prepare_stills(article: dict, cfg: dict, workdir: str | Path,
                   target_shots: int = 0) -> list[dict]:
    """Download once, then bake `target_shots` varied collage stills."""
    vcfg = cfg["video"]
    w, h = int(vcfg["width"]), int(vcfg["height"])
    min_w = int(cfg["gate"]["min_width"])
    max_shots = int(vcfg["max_shots"])
    target = min(max(target_shots, int(vcfg["min_shots"])), max_shots)
    skip = tuple(vcfg.get("skip_image_hosts", ()))

    # og:image is the editorial lead picture: always try it first, once.
    og = article.get("og_image") or ""
    sources = [og] + [u for u in (article.get("images") or []) if not og or u != og]
    downloaded = download_images(sources, min_w, limit=12, skip_hosts=skip)
    if not downloaded:
        return []

    shots_dir = ensure_dir(Path(workdir) / "assets" / "stills")
    shots: list[dict] = []
    for idx in range(target):
        start = idx % len(downloaded)
        rotation = downloaded[start:] + downloaded[:start]
        _, boxes = layout_boxes(w, h, idx)
        used = [rotation[i % len(rotation)] for i in range(len(boxes))]
        still = bake_collage(rotation, w, h, idx)
        name = f"shot_{idx:02d}.jpg"
        still.save(shots_dir / name, "JPEG", quality=JPEG_QUALITY, subsampling=0)
        urls: list[str] = []
        for item in used:
            if item["url"] not in urls:
                urls.append(item["url"])
        shots.append({
            "path": f"assets/stills/{name}",
            "source_url": used[0]["url"],
            "source_urls": urls,
            "variant": idx % len(LAYOUTS),
            "layout": LAYOUTS[idx % len(LAYOUTS)],
            "original": "+".join(f"{x['width']}x{x['height']}" for x in used),
        })
    return shots
