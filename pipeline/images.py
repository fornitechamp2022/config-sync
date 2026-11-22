"""Full-resolution image harvesting, dedupe and vertical still baking.

Every still is a 1080x1920 image built from the ORIGINAL source: the whole
picture is kept sharp in the upper-middle over a blurred, darkened fill, so
multi-panel news graphics (split screens, triptychs) never lose their story.
"""
from __future__ import annotations

import io
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from .utils import ensure_dir

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def dhash(image: Image.Image) -> str:
    g = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(g.getdata())
    bits = "".join("1" if px[r * 9 + c] > px[r * 9 + c + 1] else "0"
                   for r in range(8) for c in range(8))
    return hex(int(bits, 2))[2:].zfill(16)


def download_images(urls: list[str], min_width: int = 900, limit: int = 16,
                    skip_hosts: tuple[str, ...] = ()) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for url in urls:
        if len(out) >= limit:
            break
        if any(s in url.lower() for s in skip_hosts):
            continue
        try:
            resp = requests.get(url, headers=UA, timeout=20)
            if resp.status_code != 200 or len(resp.content) < 12000:
                continue
            image = Image.open(io.BytesIO(resp.content))
            image.load()
            if image.width < min_width or image.height < 400:
                continue
            digest = dhash(image)
            if digest in seen:
                continue
            seen.add(digest)
            out.append({"url": url, "image": image.convert("RGB"), "hash": digest,
                        "width": image.width, "height": image.height})
        except Exception:
            continue
    return out


def _cover(image: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / image.width, h / image.height)
    size = (max(w, int(image.width * scale)), max(h, int(image.height * scale)))
    resized = image.resize(size, Image.Resampling.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def bake_still(item: dict, canvas_w: int, canvas_h: int, variant: int = 0) -> Image.Image:
    src = item["image"]
    # Blurred, darkened full-frame fill
    bg = _cover(src, canvas_w, canvas_h).filter(ImageFilter.GaussianBlur(46))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)

    # Sharp foreground, whole image preserved
    box_w, box_h = canvas_w, int(canvas_h * 0.40)
    work = src
    if variant == 1:      # left subject
        crop_w, crop_h = int(src.width * 0.76), int(src.height * 0.88)
        work = src.crop((0, int((src.height - crop_h) * 0.15),
                         crop_w, int((src.height - crop_h) * 0.15) + crop_h))
    elif variant == 2:    # right subject
        crop_w, crop_h = int(src.width * 0.76), int(src.height * 0.88)
        left = src.width - crop_w
        work = src.crop((left, int((src.height - crop_h) * 0.15),
                         src.width, int((src.height - crop_h) * 0.15) + crop_h))
    elif variant == 3:    # tight centre punch-in
        crop_w, crop_h = int(src.width * 0.58), int(src.height * 0.72)
        work = src.crop(((src.width - crop_w) // 2, (src.height - crop_h) // 2,
                         (src.width + crop_w) // 2, (src.height + crop_h) // 2))
    scale = min(box_w / work.width, box_h / work.height)
    fg = work.resize((int(work.width * scale), int(work.height * scale)), Image.Resampling.LANCZOS)

    top_y = int(canvas_h * 0.17) + (box_h - fg.height) // 2
    bg.paste(fg, ((canvas_w - fg.width) // 2, top_y))

    # Subtle bottom scrim for caption legibility (news-channel look)
    scrim = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scrim)
    start = int(canvas_h * 0.54)
    for y in range(start, canvas_h):
        alpha = int(200 * ((y - start) / (canvas_h - start)) ** 1.4)
        draw.line([(0, y), (canvas_w, y)], fill=(0, 0, 0, alpha))
    bg = Image.alpha_composite(bg.convert("RGBA"), scrim).convert("RGB")
    return bg


def bake_collage(items: list[dict], canvas_w: int, canvas_h: int,
                 variant: int = 0) -> Image.Image:
    """News-channel collage: one hero panel plus supporting panels."""
    base = items[variant % len(items)]["image"]
    bg = _cover(base, canvas_w, canvas_h).filter(ImageFilter.GaussianBlur(46))
    bg = ImageEnhance.Brightness(bg).enhance(0.42)

    top = int(canvas_h * 0.17)
    box_h = int(canvas_h * 0.40)
    gap = 10

    def panel(img: Image.Image, size: tuple[int, int], box: tuple[int, int]) -> None:
        tile = _cover(img, size[0], size[1])
        bg.paste(tile, box)
        d = ImageDraw.Draw(bg)
        d.rectangle([box[0], box[1], box[0] + size[0] - 1, box[1] + size[1] - 1],
                    outline=(255, 230, 0), width=3)

    if len(items) >= 3:
        main_w = int(canvas_w * 0.62)
        side_w = canvas_w - main_w - gap
        half_h = (box_h - gap) // 2
        order = [(variant + k) % len(items) for k in range(3)]
        panel(items[order[0]]["image"], (main_w, box_h), (0, top))
        panel(items[order[1]]["image"], (side_w, half_h), (main_w + gap, top))
        panel(items[order[2]]["image"], (side_w, half_h),
              (main_w + gap, top + half_h + gap))
    elif len(items) == 2:
        half_w = (canvas_w - gap) // 2
        panel(items[0]["image"], (half_w, box_h), (0, top))
        panel(items[1]["image"], (canvas_w - half_w, box_h), (half_w + gap, top))
    else:
        work = items[0]["image"]
        scale = min(canvas_w / work.width, box_h / work.height)
        fg = work.resize((int(work.width * scale), int(work.height * scale)),
                         Image.Resampling.LANCZOS)
        bg.paste(fg, ((canvas_w - fg.width) // 2, top + (box_h - fg.height) // 2))

    scrim = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(scrim)
    start = int(canvas_h * 0.54)
    for y in range(start, canvas_h):
        alpha = int(205 * ((y - start) / (canvas_h - start)) ** 1.4)
        draw.line([(0, y), (canvas_w, y)], fill=(0, 0, 0, alpha))
    return Image.alpha_composite(bg.convert("RGBA"), scrim).convert("RGB")


def prepare_stills(article: dict, cfg: dict, workdir: str | Path,
                   target_shots: int = 0) -> list[dict]:
    vcfg = cfg["video"]
    w, h = int(vcfg["width"]), int(vcfg["height"])
    min_w = int(cfg["gate"]["min_width"])
    max_shots = int(vcfg["max_shots"])
    target = min(max(target_shots, int(vcfg["min_shots"])), max_shots)
    skip = tuple(vcfg.get("skip_image_hosts", ()))

    sources = list(article.get("images", []))
    downloaded = download_images(sources, min_w, limit=12, skip_hosts=skip)
    if not downloaded:
        return []

    shots_dir = ensure_dir(Path(workdir) / "assets" / "stills")
    shots: list[dict] = []
    for idx in range(target):
        variant = idx % max(1, min(4, len(downloaded)))
        if len(downloaded) >= 3:
            rotation = downloaded[variant:] + downloaded[:variant]
            still = bake_collage(rotation[:3], w, h, variant)
            urls = [x["url"] for x in rotation[:3]]
            original = "+".join(f"{x['width']}x{x['height']}" for x in rotation[:3])
        elif len(downloaded) == 2:
            still = bake_collage(downloaded, w, h, variant)
            urls = [x["url"] for x in downloaded]
            original = "+".join(f"{x['width']}x{x['height']}" for x in downloaded)
        else:
            still = bake_still(downloaded[0], w, h, variant)
            urls = [downloaded[0]["url"]]
            original = f"{downloaded[0]['width']}x{downloaded[0]['height']}"
        name = f"shot_{idx:02d}.jpg"
        still.save(shots_dir / name, "JPEG", quality=93, subsampling=0)
        shots.append({
            "path": f"assets/stills/{name}",
            "source_url": urls[0],
            "source_urls": urls,
            "variant": variant,
            "original": original,
        })
    return shots
