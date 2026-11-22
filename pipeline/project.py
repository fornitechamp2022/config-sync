"""Generate a deterministic HyperFrames project for one reel."""
from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

import requests

from .captions import build_caption_timeline, render_timeline_js
from .utils import ensure_dir, slugify

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"}

SKILL_SFX = Path(r"C:\Users\MASHU\.agents\skills\media-use\audio\assets\sfx")
BGM_FALLBACK = "https://cdn.freesound.org/previews/61/61321_569237-hq.mp3"


def _esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def _fit_copy(value: str, limit: int) -> str:
    """Shorten on-screen copy at a word boundary; full copy stays in the manifest.

    Bounded strings keep every graphic inside the 1080x1920 safe box without
    CSS ellipsis/clipping (which the layout audit flags as clipped text).
    """
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:|।.-")
    return (cut or text[:limit]) + "…"


def _ticker_phrases(story: dict, count: int = 2, limit: int = 16) -> list[str]:
    """Build the ticker from the script's own sentences — no external feed.

    Phrases are deliberately short: a single nowrap line must fit beside the
    label inside the 630 px ticker box (x 150..780 at 30 px Devanagari).
    """
    skip = ("follow", "subscribe", "जारी", "फॉलो")
    sentences = [
        s for s in story.get("sentences", [])
        if len(s or "") > 18 and not (s or "").strip().lower().startswith(skip)
    ]
    phrases = [_fit_copy(s, limit).rstrip("।.") for s in sentences]
    if not phrases:
        phrases = [_fit_copy(story.get("headline", ""), limit) or "TOP STORY"]
    return phrases[:count]


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

    # Tuned audio levels live in config.yaml (mix:). Fallbacks keep older
    # configs — and existing generated projects — behaving exactly as before.
    mix = cfg.get("mix", {}) or {}
    bgm_volume = float(mix.get("bgm_volume", 0.12))
    whoosh_volume = float(mix.get("whoosh_volume", 0.22))

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
        "breaking": "ब्रेकिंग" if is_hi else "BREAKING",
        "lower_label": "टॉप स्टोरी" if is_hi else "TOP STORY",
        "ticker_label": "ताज़ा" if is_hi else "NOW",
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
    # Three deterministic camera moves, cycled by shot index.  The cycle length
    # (3) and its three distinct entries guarantee both acceptance properties:
    # at least three different moves per reel, and never the same move on two
    # consecutive shots.  Every tween is a fromTo at an absolute time on a child
    # wrapper of the .clip host, so nothing here can trip
    # gsap_animates_clip_element or the CSS-transform conflict rule.
    camera_cycle = ("push", "pan", "punch")

    def camera_move(index: int) -> dict:
        kind = camera_cycle[index % len(camera_cycle)]
        if kind == "push":
            return {
                "kind": kind,
                "from": {"scale": 1.0, "x": 0, "y": 0},
                "to": {"scale": 1.085, "x": 0, "y": -8},
                "ease": "power2.inOut", "dur_factor": 1.0,
            }
        if kind == "pan":
            return {
                "kind": kind,
                "from": {"scale": 1.055, "x": -30, "y": 0},
                "to": {"scale": 1.055, "x": 30, "y": 0},
                "ease": "sine.inOut", "dur_factor": 1.0,
            }
        # punch-in: a short accelerating snap that lands and holds
        return {
            "kind": kind,
            "from": {"scale": 1.0, "x": 12, "y": 6},
            "to": {"scale": 1.10, "x": 0, "y": 0},
            "ease": "power3.out", "dur_factor": 0.78,
        }

    total_shots = len(shots)
    # The hero panel is the first shot that is fully visible once the hook card
    # has cleared (~3.15 s).  It gets one extra, quick punch-in on a nested
    # wrapper so the hero shot reads as a studio move, not a static still.
    hero_index = 2 if total_shots > 2 else 0
    shot_markup, tweens, dip_markup, dip_tweens = [], [], [], []
    for i, shot in enumerate(shots):
        start = round(i * shot_len, 2)
        dur = round(min(shot_len, duration - start), 2)
        if dur <= 0.4:
            break
        wrapper = f"shotmotion{i}"
        move = camera_move(i)
        move_dur = round(max(0.4, dur * move["dur_factor"]), 2)
        frm, to = move["from"], move["to"]
        img = (f'<img src="{shot["path"]}" alt="" '
               'style="width:100%;height:100%;object-fit:cover" />')
        inner = (f'<div id="heropunch" class="hero-punch">{img}</div>'
                 if i == hero_index else img)
        shot_markup.append(f'''<div id="shot{i}" class="clip shot" data-start="{start}"
          data-duration="{dur}" data-track-index="0" style="z-index:{1 + i}">
          <div id="{wrapper}" class="shot-motion" data-camera="{move['kind']}">
            {inner}
          </div>
        </div>''')
        tweens.append(
            f'tl.fromTo("#{wrapper}", '
            f'{{scale:{frm["scale"]},x:{frm["x"]},y:{frm["y"]}}}, '
            f'{{scale:{to["scale"]},x:{to["x"]},y:{to["y"]},duration:{move_dur},'
            f'ease:"{move["ease"]}",immediateRender:true}}, {start});'
        )
        if i == hero_index:
            tweens.append(
                'tl.fromTo("#heropunch", {scale:1}, '
                f'{{scale:1.06,duration:0.38,ease:"power3.out",immediateRender:true}}, '
                f'{round(start + 0.25, 2)});'
            )
        # Dip-to-dark on every third cut (a cut exists at the start of shots
        # i >= 1).  It is a dedicated .clip whose *child* carries the opacity
        # tween; the child starts at opacity:0 in CSS so check never sees a
        # full-frame overlay appear before its first tween.
        if i and i % 3 == 0:
            dip_id = f"dip{i}"
            dip_start = round(start - 0.06, 2)
            dip_markup.append(f'''<div id="{dip_id}" class="clip dip-overlay"
              data-start="{dip_start}" data-duration="0.24" data-track-index="8"
              style="z-index:90" data-layout-allow-occlusion>
              <div id="{dip_id}inner" class="dip-inner"></div>
            </div>''')
            dip_tweens.append(
                f'tl.fromTo("#{dip_id}inner", {{opacity:0}}, '
                f'{{opacity:0.55,duration:0.07,ease:"power2.in",'
                f'immediateRender:true}}, {dip_start});'
            )
            dip_tweens.append(
                f'tl.to("#{dip_id}inner", {{opacity:0,duration:0.14,'
                f'ease:"power2.out"}}, {round(dip_start + 0.08, 2)});'
            )

    # ---- hook title card (short display form; full headline is spoken) ----
    hook_words = story["hook"].split()
    display_hook = " ".join(hook_words[:10]) + ("…" if len(hook_words) > 10 else "")
    hook = _esc(display_hook)
    hook_markup = f'''<div id="hookcard" class="clip" data-start="0.0" data-duration="3.15"
      data-track-index="1" style="z-index:60">
      <div id="hookinner" class="hook-inner">
        <div class="hook-kicker">{_esc(ui['kicker'])}</div>
        <div class="hook-text">{hook}</div>
        <div id="hookrule" class="hook-rule"></div>
      </div>
    </div>'''

    # ---- persistent news-channel graphics ----
    # Each graphic is a .clip host; every GSAP tween below targets an inner
    # wrapper only (never the .clip itself) so Studio stays editable and the
    # lint rule gsap_animates_clip_element cannot fire.
    headline = _fit_copy(story["headline"], 58)
    ticker = " • ".join(_ticker_phrases(story))
    graphics_markup = f'''<div id="breakingbar" class="clip" data-start="0" data-duration="{duration}"
      data-track-index="2" style="z-index:70">
      <div class="breaking-band">
        <div id="breakinginner" class="breaking-inner">
          <span id="breakingtag" class="breaking-tag">{_esc(ui['breaking'])}</span>
          <span class="breaking-rule"></span>
          <span id="breakingtext" class="breaking-text">{_esc(ui['kicker'])}</span>
          <span id="breakingbrand" class="breaking-brand">{_esc(ui['brand'])}</span>
        </div>
      </div>
    </div>
    <div id="channelbug" class="clip" data-start="0" data-duration="{duration}"
      data-track-index="3" style="z-index:73">
      <div id="bugpanel" class="bug-panel">
        <span id="bugmark" class="bug-mark">BB</span>
        <span id="buglive" class="bug-live"><span class="bug-dot"></span>LIVE</span>
      </div>
    </div>
    <div id="lowerthird" class="clip" data-start="0" data-duration="{duration}"
      data-track-index="4" style="z-index:71">
      <div id="lowerpanel" class="lower-panel">
        <span id="lowerlabel" class="lower-label">{_esc(ui['lower_label'])}</span>
        <div id="lowerinner" class="lower-inner">{_esc(headline)}</div>
        <div id="lowerpulse" class="lower-pulse"></div>
      </div>
    </div>
    <div id="ticker" class="clip" data-start="0" data-duration="{duration}"
      data-track-index="5" style="z-index:72">
      <div class="ticker-band">
        <div id="tickerinner" class="ticker-inner">
          <span id="tickerlabel" class="ticker-label">{_esc(ui['ticker_label'])}</span>
          <span class="ticker-rule"></span>
          <span id="tickertext" class="ticker-text">{_esc(ticker)}</span>
        </div>
      </div>
    </div>
    <div id="progress" class="clip" data-start="0" data-duration="{duration}"
      data-track-index="6" style="z-index:74">
      <div class="progress-track"></div>
      <div id="progressbar" class="progress-bar"></div>
    </div>'''

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
          data-start="0" data-duration="{duration}" data-volume="{bgm_volume}"
          data-fade-in="1.5" data-fade-out="2.0" data-track-index="11"></audio>''')
    if sfx:
        for i in range(1, total_shots):
            t = round(i * shot_len, 2)
            if t >= audio_duration:
                break
            audio_markup.append(f'''<audio id="cut{i}" class="clip" src="{sfx}"
              data-start="{t}" data-duration="0.3" data-volume="{whoosh_volume}"
              data-track-index="12"></audio>''')

    # Everything is a fromTo/to at absolute times; the first 0.8 s is the
    # graphic package assembling itself, the last second is a quiet exit.
    outro_at = round(max(0.0, duration - 1.1), 2)
    # One subtle lower-third highlight pulse per reel, on a child element only.
    pulse_at = round(min(max(0.0, duration - 1.6),
                         max(1.2, shot_len * 4 + 0.12)), 2)
    audio_js = "\n".join([
        f'tl.fromTo("#breakinginner", {{y:-46,opacity:0}}, '
        f'{{y:0,opacity:1,duration:0.45,ease:"power4.out"}}, 0.05);',
        f'tl.fromTo("#hookinner", {{opacity:0,filter:"blur(10px)",scale:0.96}}, '
        f'{{opacity:1,filter:"blur(0px)",scale:1,duration:0.4,ease:"power4.out"}}, 0.12);',
        f'tl.fromTo("#hookrule", {{scaleX:0}}, '
        f'{{scaleX:1,duration:0.5,ease:"power3.out"}}, 0.22);',
        f'tl.fromTo("#bugpanel", {{y:-24,opacity:0}}, '
        f'{{y:0,opacity:1,duration:0.35,ease:"power4.out"}}, 0.34);',
        f'tl.fromTo("#lowerpanel", {{x:-34,opacity:0}}, '
        f'{{x:0,opacity:1,duration:0.4,ease:"power3.out"}}, 0.46);',
        f'tl.fromTo("#tickerinner", {{y:22,opacity:0}}, '
        f'{{y:0,opacity:1,duration:0.34,ease:"power3.out"}}, 0.56);',
        f'tl.fromTo("#progressbar", {{scaleX:0}}, '
        f'{{scaleX:1,duration:{duration},ease:"none"}}, 0);',
        f'tl.fromTo("#lowerpulse", {{opacity:0}}, '
        f'{{opacity:0.9,duration:0.12,ease:"power2.out",immediateRender:true}}, {pulse_at});',
        f'tl.to("#lowerpulse", {{opacity:0,duration:0.28,ease:"power2.in"}}, '
        f'{round(pulse_at + 0.16, 2)});',
        f'tl.to("#lowerpanel", {{opacity:0,y:-18,duration:0.7,ease:"power2.in"}}, {outro_at});',
        f'tl.to("#tickerinner", {{opacity:0,y:18,duration:0.6,ease:"power2.in"}}, {outro_at});',
        f'tl.to("#breakinginner", {{opacity:0,y:-18,duration:0.55,ease:"power2.in"}}, {outro_at});',
        f'tl.to("#bugpanel", {{opacity:0,y:-14,duration:0.5,ease:"power2.in"}}, {outro_at});',
    ] + tweens + dip_tweens)

    index_html = f'''<!doctype html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width={width}, height={height}" />
<script src="https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"></script>
<style>
{font_faces}
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:{width}px;height:{height}px;overflow:hidden;background:#05070D}}
#root{{position:absolute;inset:0;font-family:{ui['font']};color:#fff;
  --news-red:#E11D2E;--news-yellow:#FFE600;--news-navy:#0A0F1E;--news-ink:#05070D}}
.shot{{position:absolute;inset:0;overflow:hidden}}
/* Extra bleed keeps the frame filled during lateral pans and punch-ins. */
.shot-motion{{position:absolute;inset:-5%;will-change:transform}}
.hero-punch{{position:absolute;inset:0;will-change:transform}}
/* Dip-to-dark transition: a full-frame child that only the timeline lights. */
.dip-overlay{{position:absolute;inset:0;pointer-events:none}}
.dip-inner{{position:absolute;inset:0;background:#000;opacity:0;will-change:opacity}}
.hook-inner{{position:absolute;left:150px;right:250px;top:430px;text-align:center}}
.hook-kicker{{font-size:30px;font-weight:900;letter-spacing:.14em;color:var(--news-yellow);
  margin-bottom:18px;text-shadow:0 3px 12px #000}}
.hook-text{{font-size:{54 if is_hi else 62}px;font-weight:900;line-height:1.05;letter-spacing:-.02em;
  text-shadow:0 6px 26px rgba(0,0,0,.95);color:#fff}}
.hook-rule{{height:6px;width:180px;margin:22px auto 0;border-radius:3px;
  background:var(--news-yellow);transform-origin:center center}}
/* Breaking-news band: navy field, red accent rule, yellow keyline */
.breaking-band{{position:absolute;left:0;right:0;top:288px;height:90px;
  background:linear-gradient(90deg,var(--news-navy) 0%,rgba(10,15,30,.95) 70%,rgba(10,15,30,.6) 100%);
  border-top:3px solid var(--news-red);border-bottom:3px solid var(--news-yellow);
  box-shadow:0 12px 30px rgba(0,0,0,.55)}}
.breaking-inner{{position:absolute;left:150px;right:250px;top:0;height:100%;
  display:flex;align-items:center;gap:16px;white-space:nowrap}}
.breaking-tag{{background:var(--news-red);color:#fff;font-size:30px;font-weight:900;letter-spacing:.06em;
  padding:8px 14px;border-radius:4px;box-shadow:0 3px 14px rgba(225,29,46,.55)}}
.breaking-rule{{width:3px;height:38px;background:var(--news-yellow);flex:none}}
.breaking-text{{font-size:32px;font-weight:800;letter-spacing:.03em;color:#fff;
  text-shadow:0 3px 10px #000}}
.breaking-brand{{margin-left:auto;font-size:30px;font-weight:900;letter-spacing:.04em;
  color:var(--news-yellow);text-shadow:0 3px 10px #000}}

/* Persistent channel bug with LIVE indicator */
.bug-panel{{position:absolute;top:758px;right:300px;width:196px;height:72px;
  background:rgba(10,15,30,.92);border:2px solid var(--news-red);border-radius:6px;
  display:flex;align-items:center;gap:12px;padding:0 12px;box-shadow:0 8px 24px rgba(0,0,0,.6)}}
.bug-mark{{width:46px;height:46px;border-radius:4px;background:var(--news-red);color:#fff;
  font-size:30px;font-weight:900;display:flex;align-items:center;justify-content:center;flex:none}}
.bug-live{{font-size:30px;font-weight:900;letter-spacing:.08em;color:#fff;white-space:nowrap}}
.bug-dot{{display:inline-block;width:14px;height:14px;margin-right:7px;border-radius:50%;
  background:var(--news-red);box-shadow:0 0 12px var(--news-red)}}

/* Lower-third: thin coloured rule, label tab and bounded headline */
.lower-panel{{position:absolute;left:150px;right:300px;top:866px;min-height:130px;
  background:linear-gradient(100deg,rgba(10,15,30,.96),rgba(10,15,30,.78));
  border-top:5px solid var(--news-yellow);border-left:7px solid var(--news-red);
  box-shadow:0 -10px 34px rgba(0,0,0,.55);padding:16px 18px 14px 20px}}
.lower-label{{display:inline-block;background:var(--news-red);color:#fff;font-size:28px;font-weight:900;
  letter-spacing:.08em;padding:5px 12px;border-radius:3px;margin-bottom:10px}}
.lower-inner{{font-size:32px;font-weight:800;line-height:1.18;color:#fff;
  text-shadow:0 3px 10px #000;overflow-wrap:anywhere}}
/* One-beat highlight pulse on the lower-third (child only, starts hidden). */
.lower-pulse{{position:absolute;left:0;bottom:0;height:6px;width:100%;
  background:var(--news-yellow);opacity:0;transform-origin:left center}}

/* Ticker strip: short phrases taken from the script itself */
.ticker-band{{position:absolute;left:0;right:0;top:1188px;height:54px;
  background:rgba(5,7,13,.9);border-top:2px solid var(--news-red);
  border-bottom:2px solid rgba(255,230,0,.55)}}
.ticker-inner{{position:absolute;left:150px;right:300px;top:0;height:100%;
  display:flex;align-items:center;gap:14px;white-space:nowrap}}
.ticker-label{{font-size:28px;font-weight:900;letter-spacing:.06em;color:var(--news-yellow);flex:none}}
.ticker-rule{{width:2px;height:28px;background:rgba(255,255,255,.4);flex:none}}
.ticker-text{{font-size:30px;font-weight:700;color:#fff;text-shadow:0 2px 8px #000}}

.progress-track{{position:absolute;left:0;right:0;bottom:0;height:8px;background:rgba(255,255,255,.16)}}
.progress-bar{{position:absolute;left:0;bottom:0;height:8px;width:100%;background:var(--news-yellow);
  transform-origin:left center}}
</style>
</head>
<body>
<div id="root" data-composition-id="main" data-start="0" data-duration="{duration}"
  data-width="{width}" data-height="{height}">
{chr(10).join(shot_markup)}
{chr(10).join(dip_markup)}
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
    # Timing is owned by captions.py::build_caption_timeline (monotonic word
    # highlighting + validation). project.py only renders its events into the
    # sub-composition and keeps the soft fades on the *inner child* of each clip.
    prepared = []
    for group in captions:
        g = dict(group)
        g["start"] = max(0.1, round(float(group["start"]), 3))
        g["end"] = round(max(float(group["end"]), g["start"] + 0.4), 3)
        prepared.append(g)
    timeline = build_caption_timeline(prepared, cfg)

    cap_markup = []
    cap_fades = []
    for group in timeline["groups"]:
        start, end = group["start"], group["end"]
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
        # Visibility + karaoke colours come from the shared timeline below;
        # these two short fades are presentation only, on the child wrapper.
        cap_fades.append(
            f'tl.fromTo("#{inner}", {{opacity:0}}, {{opacity:1,duration:0.12,'
            f'ease:"power2.out"}}, {start});'
        )
        cap_fades.append(
            f'tl.to("#{inner}", {{opacity:0,duration:0.12,ease:"power2.in"}}, '
            f'{round(max(start, end - 0.12), 3)});'
        )
    cap_js = render_timeline_js(timeline) + (
        ("\n" + "\n".join(cap_fades)) if cap_fades else ""
    )

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
{cap_js}
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
        "caption_timeline_valid": timeline["valid"],
        "caption_timeline_errors": timeline["errors"],
    }
    return {"duration": duration, "bgm": bgm, "shots": len(shots)}
