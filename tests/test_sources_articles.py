"""Source clustering, quality ranking and article extraction (all offline)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import escape

import pipeline.articles as articles_mod
import pipeline.sources as sources_mod
from pipeline.articles import article_quality, enrich_and_gate, fetch_article
from pipeline.sources import _event_signature, _same_event, discover, source_quality


def _cfg() -> dict:
    return {
        "topic": {
            "primary": "Bigg Boss 20",
            "keywords": ["Bigg Boss", "Salman Khan", "Bollywood", "Kanika", "Qazi"],
            "bing_queries": ["bigg boss 20"],
            "direct_feeds": ["https://example.com/feed"],
            "preferred_sources": ["ABP News", "Indian Express", "Hindustan Times"],
            "skip_url_patterns": ["msn.com"],
        },
        "gate": {
            "lookback_days": 14,
            "min_paragraphs": 3,
            "min_chars": 600,
            "title_overlap_ratio": 0.34,
        },
    }


def _rss(items: list[dict]) -> str:
    blocks = []
    for item in items:
        blocks.append(
            "<item>"
            f"<title>{escape(item['title'])}</title>"
            f"<link>{escape(item['link'])}</link>"
            f"<description>{escape(item.get('description', ''))}</description>"
            f"<pubDate>{escape(item['pubdate'])}</pubDate>"
            f"<source>{escape(item['source'])}</source>"
            "</item>"
        )
    return '<?xml version="1.0"?><rss version="2.0"><channel>' + "".join(blocks) + "</channel></rss>"


def _recent() -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=1))


def _item(title: str, source: str, slug: str, *, description: str = "",
          thumb: str = "") -> dict:
    item = {
        "title": title, "source": source,
        "link": f"https://{slug}.example.com/story", "pubdate": _recent(),
        "description": description or "A short news summary from the outlet.",
    }
    if thumb:
        item["thumbnail"] = thumb
    return item


# --- entity-based clustering ---------------------------------------------------

def test_entity_signature_and_overlap_not_plain_jaccard():
    cfg = _cfg()
    a = _event_signature(
        "Bigg Boss 20 Ep 78 Highlights: Manish Exits House Following Leg Injury", cfg)
    b = _event_signature(
        "Bigg Boss 20 Weekend Ka Vaar: Manish Goel Expected To Exit Due To Injury", cfg)
    c = _event_signature(
        "Bigg Boss 20: Salman Khan's Show To Get Two-to-Four-Week Extension", cfg)
    assert {"manish", "exit", "injury"} <= a & b
    assert _same_event(a, b)
    assert not _same_event(a, c)


def test_generic_show_words_do_not_make_every_headline_one_event():
    cfg = _cfg()
    a = _event_signature("Bigg Boss 20 Weekend Ka Vaar Episode 78 Highlights", cfg)
    b = _event_signature("Bigg Boss 20 Captaincy Task Episode 79 Full Video", cfg)
    assert not _same_event(a, b)


def test_discover_collapses_cross_outlet_duplicates_and_keeps_best(monkeypatch):
    cfg = _cfg()
    same_event = [
        _item("Bigg Boss 20 Ep 78 Highlights: Manish Exits House Following Leg Injury",
              "Times Now", "times", description="short"),
        _item("Bigg Boss 20 Weekend Ka Vaar: Manish Goel Expected To Exit Due To Injury",
              "Zoom TV", "zoom"),
        _item("Manish Goel Exits Salman Khan's Bigg Boss 20 Due To Health Issues",
              "ABP News", "abp", description="A rich detailed summary of the event " * 6,
              thumb="https://img.example.com/lead.jpg"),
    ]
    distinct = [
        _item("Karan Wahi Reacts To Backlash Over Remarks On Qazi Touqeer's Gameplay",
              "ABP News", "abp2"),
        _item("Love Gill Evicted From Bigg Boss 20 After Captaincy Task",
              "India TV News", "indiatv"),
        _item("Salman Khan Apologised To Shraddha Kapoor On Call",
              "Indian Express", "indianexpress"),
        _item("Bollywood Actors May Get X-Category Security After Threats",
              "Hindustan Times", "hindustan"),
        _item("Manoj Bajpayee Says No Fraternity In Bollywood", "NDTV", "ndtv"),
    ]

    def fake_fetch(url, params=None):
        payload = _rss(same_event if "bing.com" in url else distinct)
        return sources_mod._parse_xml(payload)

    monkeypatch.setattr(sources_mod, "fetch_feed", fake_fetch)
    found = discover(cfg)

    manish = [c for c in found if "manish" in c["title"].lower()]
    assert len(manish) == 1, "three outlets covering one eviction must collapse"
    assert manish[0]["source"] == "ABP News", "highest-quality outlet wins"
    assert manish[0]["cluster_size"] == 3
    assert len(found) == len(distinct) + 1

    sources = {c["source"] for c in found}
    assert len(sources) >= 4


def test_source_quality_rewards_preferred_outlet_length_and_image():
    cfg = _cfg()
    base = _item("Salman Khan Bigg Boss 20 eviction", "Unknown Blog", "blog",
                 description="short")
    better = _item("Salman Khan Bigg Boss 20 eviction", "ABP News", "abp",
                   description="long " * 40, thumb="https://img.example.com/a.jpg")
    assert source_quality(base, cfg) < source_quality(better, cfg)


# --- article extraction -------------------------------------------------------

PAGE = """<html><head>
<meta property="og:image" content="https://img.example.com/lead.jpg">
</head><body>
<div class="related-stories"><p>Also read: Salman Khan and the Bigg Boss 20
eviction story continues with more updates from the studio every single day.</p></div>
<p>Salman Khan addressed the Bigg Boss 20 eviction during a live episode and
shared strong views with the audience about discipline and teamwork inside the
house before the final announcement of the night.</p>
<p>Photo credit: Getty Images. #Bollywood #BiggBoss Follow us for more updates
about the show and the contestants.</p>
<p>1. Kanika 2. Gullu 3. Mahhi 4. Love Gill 5. ScoutOP were part of the task.</p>
<p>Karan Wahi questioned Qazi Touqeer about the captaincy decision in front of
the audience, and the makers later clarified the result during the weekend
episode with a detailed explanation for the viewers at home.</p>
<p>The producers confirmed that the final eviction would be announced in the
next episode after the nomination task, and fans on social media discussed the
decision at length through the evening and into the following morning.</p>
<p>Salman Khan praised the contestants for their patience during the task and
reminded everyone that the show rewards both strategy and fair play inside the
house across every single week of the season.</p>
<p>Related stories: more coverage of Bigg Boss 20 and Bollywood news is
available on our website for readers who want to know everything about the
episode and the eviction.</p>
</body></html>"""


def test_fetch_article_strips_metadata_and_rejects_fragments(monkeypatch):
    class Resp:
        status_code = 200
        content = PAGE.encode("utf-8")

    monkeypatch.setattr(articles_mod.requests, "get", lambda *a, **k: Resp())
    cand = {"title": "Salman Khan Bigg Boss 20 eviction", "source": "ABP News",
            "description": "A news story. #Bollywood"}
    article = fetch_article("https://example.com/story", _cfg(), cand)
    assert article is not None and article["accepted"]
    body = article["body"]
    for marker in ("also read", "photo credit", "follow us", "#", "related stories",
                   "1. Kanika"):
        assert marker not in body.lower()
    assert len(article["paragraphs"]) >= 3
    assert article["og_image"].endswith("lead.jpg")
    assert article["images"]


def test_article_quality_and_enrich_order(monkeypatch):
    cfg = _cfg()
    def art(source, body, images):
        return {"source": source, "body": body, "images": images,
                "published": "2026-11-21T10:00:00Z", "accepted": True}
    weak = art("Unknown Blog", "x" * 100, [])
    strong = art("ABP News", "x" * 1500, ["a", "b", "c"])
    assert article_quality(strong, cfg) > article_quality(weak, cfg)

    mapping = {
        "https://weak.example.com": weak,
        "https://strong.example.com": strong,
    }
    monkeypatch.setattr(articles_mod, "fetch_article",
                        lambda url, cfg, cand=None: mapping.get(url))
    cands = [
        {"url": "https://weak.example.com", "published": "2026-11-22T10:00:00Z"},
        {"url": "https://strong.example.com", "published": "2026-11-20T10:00:00Z"},
    ]
    result = enrich_and_gate(cands, cfg, limit=2)
    assert [a["source"] for a in result] == ["ABP News", "Unknown Blog"]
