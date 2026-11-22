"""Generate a deterministic HyperFrames project for one reel."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

import requests

from .utils import ensure_dir, slugify

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"}

SKILL_SFX = Path(r"C:\Users\MASHU\.agents\skills\media-use\audio\assets\sfx")
BGM_FALLBACK = "https://cdn.freesound.org/previews/61/61321_569237-hq.mp3"


def _esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def _fetch_bgm(dest: Path, duration: float) -> dict | None:
    """Download one CC-licensed bed from Openverse; loop it to video length."""
    try:
        resp = requests.get(
            "https://api.openverse.org/v1/audio/",
            params={"q": "news intro", "page_size": 5, "license_type": "commercial"},
            headers=UA, timeout=20,
        )
        results = resp.json().get("results", []) if resp.status_code == 200 else []
    except Exception:
        results = []
    candidates = [r for r in results if str(r.get("url", "")).lower().endswith(".mp3")]
    url = candidates[0]["url"] if candidates else BGM_FALLBACK
    try:
        audio = requests.get(url, headers=UA, timeout=40)
        if audio.status_code == 200 and len(audio.content) > 10000:
            raw = dest.with_name("bgm_raw.mp3")
            raw.write_bytes(audio.content)
            from .utils import run
            run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1",
                 "-i", str(raw), "-t", str(round(duration + 1, 2)),
                 "-c:a", "libmp3lame", "-b:a", "128k", str(dest)])
            raw.unlink(missing_ok=True)
            meta = candidates[0] if candidates else {}
            return {
                "path": "assets/audio/bgm.mp3",
                "url": url,
                "title": meta.get("title", "News Background"),
                "creator": meta.get("creator", "Freesound contributor"),
                "license": meta.get("license", "CC"),
                "source": "Openverse / Freesound",
            }
    except Exception:
        pass
    return None


def _prepare_audio(workdir: Path, sfx_name: str = "whoosh-short.mp3") -> str | None:
    audio_dir = ensure_dir(workdir / "assets" / "audio")
    src = SKILL_SFX / sfx_name
    if src.exists():
        shutil.copyfile(src, audio_dir / sfx_name)
        return f"assets/audio/{sfx_name}"
    return None


def write_project(workdir: str | Path, story: dict, voice: dict, captions: list[dict],
                  shots: list[dict], cfg: dict, manifest: dict) -> dict:
    workdir = Path(workdir)
    vcfg = cfg["video"]
    width, height = int(vcfg["width"]), int(vcfg["height"])
    fps = int(vcfg["fps"])

    audio_duration = float(voice["duration"])
    duration = round(audio_duration + float(vcfg["tail_seconds"]), 2)
    shot_len = float(vcfg["shot_seconds"])

    # ---- audio assets ----
    audio_dir = ensure_dir(workdir / "assets" / "audio")
    bgm = _fetch_bgm(audio_dir / "bgm.mp3", duration)
    sfx = _prepare_audio(workdir)

    # Devanagari font ships with the project so renders never fetch it.
    font_dest = ensure_dir(workdir / "assets" / "fonts")
    font_src = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    for font in font_src.glob("*.woff2"):
        shutil.copyfile(font, font_dest / font.name)

    is_hi = story.get("language") == "hi"
    ui = {
        "brand": "बॉलीवुड बज़" if is_hi else "BOLLYWOOD BUZZ",
        "kicker": "मनोरंजन  •  टॉप स्टोरी" if is_hi else "ENTERTAINMENT  •  TOP STORY",
        "font": "'Noto Sans Devanagari', Outfit, sans-serif" if is_hi
                else "Outfit, Montserrat, sans-serif",
    }
    font_faces = ""
    if is_hi:
        font_faces = """@font-face{font-family:'Noto Sans Devanagari';
          src:url('assets/fonts/NotoSansDevanagari-900.woff2') format('woff2');
          font-weight:900;font-display:swap}
        @font-face{font-family:'Noto Sans Devanagari';
          src:url('assets/fonts/NotoSansDevanagari-700.woff2') format('woff2');
          font-weight:700;font-display:swap}
        """

    # ---- shot clips + motion ----
    shot_markup, tweens = [], []
    total_shots = len(shots)
    for i, shot in enumerate(shots):
        start = round(i * shot_len, 2)
        dur = round(min(shot_len, duration - start), 2)
        if dur <= 0.4:
            break
        wrapper = f"shotmotion{i}"
        direction = -1 if i % 2 == 0 else 1
        shot_markup.append(f'''<div id="shot{i}" class="clip shot" data-start="{start}"
          data-duration="{dur}" data-track-index="0" style="z-index:{1 + i}">
          <div id="{wrapper}" class="shot-motion">
            <img src="{shot['path']}" alt="" style="width:100%;height:100%;object-fit:cover" />
          </div>
        </div>''')
        tweens.append(
            f'tl.fromTo("#{wrapper}", {{scale:1.055,x:0,y:0}}, '
            f'{{scale:1.0,x:{direction * 16},y:{-direction * 10},duration:{dur},'
            f'ease:"power2.inOut",immediateRender:true}}, {start});'
        )

    # ---- hook title card (short display form; full headline is spoken) ----
    hook_words = story["hook"].split()
    display_hook = " ".join(hook_words[:10]) + ("…" if len(hook_words) > 10 else "")
    hook = _esc(display_hook)
    hook_markup = f'''<div id="hookcard" class="clip" data-start="0.15" data-duration="3.0"
      data-track-index="1" style="z-index:60">
      <div id="hookinner" class="hook-inner">
        <div class="hook-kicker">{_esc(ui['kicker'])}</div>
        <div class="hook-text">{hook}</div>
      </div>
    </div>'''

    # ---- persistent news-channel graphics ----
    graphics_markup = f'''<div id="topbar" class="clip" data-start="0" data-duration="{duration}"
      data-track-index="2" style="z-index:70">
      <div id="topbarinner" class="topbar-inner">
        <span class="live-dot"></span><span>{_esc(ui['brand'])}</span>
        <span class="topbar-rule"></span>
      </div>
    </div>
    <div id="lowerthird" class="clip" data-start="0.6" data-duration="{round(max(0.1, duration - 1.2), 2)}"
      data-track-index="2" style="z-index:71">
      <div id="lowerinner" class="lower-inner">
        <span class="lower-accent"></span>
        <span>{_esc(story['headline'])}</span>
      </div>
    </div>
    <div id="progress" class="clip" data-start="0" data-duration="{duration}"
      data-track-index="2" style="z-index:72"><div id="progressbar" class="progress-bar"></div></div>'''

    # ---- caption host ----
    caption_host = f'''<div id="captions" data-composition-id="captions"
      data-composition-src="compositions/captions.html" data-start="0"
      data-duration="{duration}" data-track-kind="captions" data-width="{width}"
      data-height="{height}" style="z-index:80"></div>'''

    # ---- audio ----
    audio_markup = [f'''<audio id="voice" class="clip" src="{voice['audio']}"
      data-start="0" data-duration="{audio_duration}" data-volume="1"
      data-fade-out="0.4" data-track-index="10"></audio>''']
    if bgm:
        audio_markup.append(f'''<audio id="bgm" class="clip" src="{bgm['path']}"
          data-start="0" data-duration="{duration}" data-volume="0.12"
          data-fade-in="1.5" data-fade-out="2.0" data-track-index="11"></audio>''')
    if sfx:
        for i in range(1, total_shots):
            t = round(i * shot_len, 2)
            if t >= audio_duration:
                break
            audio_markup.append(f'''<audio id="cut{i}" class="clip" src="{sfx}"
              data-start="{t}" data-duration="0.3" data-volume="0.22"
              data-track-index="12"></audio>''')

    audio_js = "\n".join([
        f'tl.fromTo("#topbarinner", {{y:-18,opacity:0}}, {{y:0,opacity:1,duration:0.4,ease:"power4.out"}}, 0.2);',
        f'tl.fromTo("#hookinner", {{opacity:0,filter:"blur(10px)"}}, '
        f'{{opacity:1,filter:"blur(0px)",duration:0.25,ease:"power4.out"}}, 0.15);',
        f'tl.fromTo("#lowerinner", {{x:-24,opacity:0}}, {{x:0,opacity:1,duration:0.45,ease:"power3.out"}}, 0.6);',
        f'tl.fromTo("#progressbar", {{scaleX:0}}, {{scaleX:1,duration:{duration},ease:"none"}}, 0);',
    ] + tweens)

    index_html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={width}, height={height}" />
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
<style>
{font_faces}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{width}px;height:{height}px;overflow:hidden;background:#05060a}}
#root{{position:absolute;inset:0;font-family:{ui['font']};color:#fff}}
.shot{{position:absolute;inset:0;overflow:hidden}}
.shot-motion{{position:absolute;inset:-2%;will-change:transform}}
.hook-inner{{position:absolute;left:140px;right:200px;top:430px;text-align:center}}
.hook-kicker{{font-size:30px;font-weight:800;letter-spacing:.16em;color:#FFE600;margin-bottom:18px}}
.hook-text{{font-size:{54 if is_hi else 62}px;font-weight:900;line-height:1.05;letter-spacing:-.02em;
  text-shadow:0 6px 26px rgba(0,0,0,.95);color:#fff}}
.topbar-inner{{position:absolute;left:140px;right:200px;top:340px;display:flex;align-items:center;gap:14px;
  font-size:30px;font-weight:800;letter-spacing:.08em;text-shadow:0 3px 12px #000}}
.live-dot{{width:16px;height:16px;border-radius:50%;background:#FF2D2D;box-shadow:0 0 14px #FF2D2D}}
.topbar-rule{{height:2px;flex:1;background:linear-gradient(90deg,#FFE600,transparent)}}
.topbar-source{{font-weight:600;opacity:.85;letter-spacing:.04em}}
.lower-inner{{position:absolute;left:140px;right:200px;top:930px;display:flex;align-items:center;gap:16px;
  font-size:30px;font-weight:700;line-height:1.15;text-shadow:0 3px 12px #000}}
.lower-accent{{width:7px;align-self:stretch;background:#FFE600;border-radius:4px;min-height:34px}}
.progress-bar{{position:absolute;left:0;bottom:0;height:7px;width:100%;background:#FFE600;
  transform-origin:left center}}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{duration}"
  data-width="{width}" data-height="{height}">
{chr(10).join(shot_markup)}
{hook_markup}
{graphics_markup}
{caption_host}
{chr(10).join(audio_markup)}
</div>
<script>
const tl = gsap.timeline({{paused:true}});
{audio_js}
window.__timelines["main"] = tl;
tl.seek(0);
</script>
</body>
</html>'''

    # ---- captions sub-composition ----
    cap_markup, cap_js = [], []
    for group in captions:
        start = max(0.1, round(group["start"], 3))
        end = round(max(group["end"], start + 0.4), 3)
        words_html = " ".join(
            f'<span id="{group["id"]}_w{i}" class="cap-word">{_esc(w["text"])}</span>'
            for i, w in enumerate(group["words"])
        )
        inner = f"{group['id']}_inner"
        cap_markup.append(f'''<div id="{group['id']}" class="clip cap-group"
          data-start="{start}" data-duration="{round(end - start, 3)}"
          data-track-index="0" style="z-index:{10 + int(start * 10)}">
          <div id="{inner}" class="cap-inner"><div class="cap-line">{words_html}</div></div>
        </div>''')
        cap_js.append(
            f'tl.set("#{inner}", {{opacity:1,visibility:"visible"}}, {start});'
        )
        cap_js.append(
            f'tl.fromTo("#{inner}", {{opacity:0}}, {{opacity:1,duration:0.12,'
            f'ease:"power2.out"}}, {start});'
        )
        for i, w in enumerate(group["words"]):
            wid = f"{group['id']}_w{i}"
            ws = max(start, round(float(w["start"]), 3))
            we = max(ws + 0.05, round(float(w["end"]), 3))
            cap_js.append(f'tl.set("#{wid}", {{color:"#FFE600"}}, {ws});')
            cap_js.append(f'tl.set("#{wid}", {{color:"#FFFFFF"}}, {we});')
        cap_js.append(
            f'tl.to("#{inner}", {{opacity:0,duration:0.12,ease:"power2.in"}}, '
            f'{round(max(start, end - 0.12), 3)});'
        )
        cap_js.append(f'tl.set("#{inner}", {{opacity:0,visibility:"hidden"}}, {end});')

    captions_html = f'''<template>
<style>
{font_faces}
#root{{position:absolute;inset:0}}
.cap-group{{position:absolute;left:150px;right:150px;bottom:740px;text-align:center;
  overflow:visible;max-width:780px;margin:auto}}
.cap-line{{font-family:{ui['font']};font-size:{52 if is_hi else 56}px;font-weight:900;
  line-height:1.2;letter-spacing:-.015em;color:#FFFFFF;
  text-shadow:0 3px 12px rgba(0,0,0,.95),0 0 3px rgba(0,0,0,.9);
  background:rgba(0,0,0,.42);border-radius:18px;padding:10px 20px;
  display:inline-block;-webkit-text-stroke:1px rgba(0,0,0,.35)}}
.cap-word{{display:inline-block;white-space:pre}}
</style>
<div id="root" data-composition-id="captions" data-width="{width}" data-height="{height}">
{chr(10).join(cap_markup)}
</div>
<script>
const tl = gsap.timeline({{paused:true}});
{chr(10).join(cap_js)}
window.__timelines["captions"] = tl;
tl.seek(0);
</script>
</template>'''

    # ---- write files ----
    (workdir / "index.html").write_text(index_html, encoding="utf-8")
    comps = ensure_dir(workdir / "compositions")
    (comps / "captions.html").write_text(captions_html, encoding="utf-8")
    (workdir / "hyperframes.json").write_text(json.dumps({
        "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
        "paths": {"blocks": "compositions", "assets": "assets"},
        "media": {"autoProxy": True},
        "authoringSkill": "hyperframes",
    }, indent=2), encoding="utf-8")
    (workdir / "package.json").write_text(json.dumps({
        "name": slugify(story["headline"], 40), "private": True, "type": "module",
        "scripts": {
            "check": "npx --yes hyperframes@0.9.77 check",
            "render": "npx --yes hyperframes@0.9.77 render",
        },
    }, indent=2), encoding="utf-8")

    manifest["video"] = {
        "duration": duration,
        "fps": fps,
        "shots": shots,
        "bgm": bgm,
        "voice": voice["audio"],
        "captions": len(captions),
    }
    return {"duration": duration, "bgm": bgm, "shots": len(shots)}
