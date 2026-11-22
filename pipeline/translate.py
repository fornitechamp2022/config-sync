"""Free machine translation (not an LLM).

Used only to render the deterministic English news brief in Hindi. No model is
hosted locally; the free Google Translate endpoint is called with short text.
"""
from __future__ import annotations

import re
import unicodedata

import requests
import time

TERM_FIX = {
    "gemaple": "gameplay", "pratiyogi": "contestant", "bedakhal": "evicted",
    "vikend": "weekend", "salaman": "salman", "taukir": "touqeer",
    "kaji": "qazi", "diesae": "dsa", "namankit": "nominated",
    "ghar": "ghar", "savasthan": "swasth",
}

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"}


def translate(text: str, src: str = "en", tgt: str = "hi") -> str:
    text = (text or "").strip()
    if not text:
        return ""
    chunks = [text[i:i + 1400] for i in range(0, len(text), 1400)]
    out = []
    for chunk in chunks:
        translated = None
        for attempt in range(3):
            try:
                resp = requests.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": src, "tl": tgt, "dt": "t", "q": chunk},
                    headers=UA, timeout=25,
                )
                if resp.status_code == 200:
                    translated = "".join(seg[0] for seg in resp.json()[0] if seg and seg[0])
                    break
            except Exception:
                pass
            time.sleep(1 + attempt)
        out.append(translated or chunk)
    return " ".join(out).strip()


def romanize(text: str) -> str:
    """Devanagari -> readable Hinglish (Roman), offline and deterministic."""
    from indic_transliteration import sanscript
    from indic_transliteration.sanscript import transliterate

    text = (text or "").replace("\u0949", "\u094b")  # modern 'aw' vowel -> o
    raw = transliterate(text, sanscript.DEVANAGARI, sanscript.ITRANS)
    raw = raw.replace("M", "n").replace("~", "n")
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(c for c in raw if not unicodedata.combining(c)).lower()
    raw = re.sub(r"([bcdfghjklmnpqrstvwxyz])a\b", r"\1", raw)
    raw = re.sub(r"(.)\1+", r"\1", raw)
    # ITRANS renders the Devanagari danda as '|'; caption grouping needs a
    # sentence-ending pause marker, so convert it back to a full stop.
    raw = raw.replace("|", ".")
    return " ".join(TERM_FIX.get(word, word) for word in raw.split())
