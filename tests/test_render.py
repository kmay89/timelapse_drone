"""render_page: semantic chapters, pictures, editions, and a StoryJSON that cannot break out of its script."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from vantage.site import render
from vantage.site.package import read_story
from vantage.site.render import picture, render_page, story_json


def img(stem: str, *, lqip_only: bool = False) -> dict[str, Any]:
    lqip = "data:image/jpeg;base64,AAAA"
    if lqip_only:
        return {
            "w": 1600,
            "h": 1000,
            "alt": stem,
            "color": "#334455",
            "lqip": lqip,
            "sources": [],
            "fallback": lqip,
        }
    return {
        "w": 2560, "h": 1600, "alt": f"{stem} alt", "color": "#334455", "lqip": lqip,
        "sources": [
            {"type": "image/avif", "srcset": [[f"assets/img/{stem}-960.avif", 960], [f"assets/img/{stem}-2560.avif", 2560]]},
            {"type": "image/jpeg", "srcset": [[f"assets/img/{stem}-960.jpg", 960], [f"assets/img/{stem}-2560.jpg", 2560]]},
        ],
        "fallback": f"assets/img/{stem}-960.jpg",
    }  # fmt: skip


def capture(date: str, label: str) -> dict[str, Any]:
    return {"date": date, "label": label, "img": img(date)}


def story(**meta: Any) -> dict[str, Any]:
    caps = [
        capture("2025-04-12", "April 2025"),
        capture("2025-09-01", "September 2025"),
        capture("2026-01-15", "Jan 2026"),
    ]
    video = {"poster": img("poster"), "sources": [
        {"src": "assets/video/v-hevc.mp4", "type": 'video/mp4; codecs="hvc1"'}, {"src": "assets/video/v.mp4", "type": "video/mp4"},
    ]}  # fmt: skip
    return {
        "version": 1,
        "meta": {
            "slug": "s", "title": "The Title", "kicker": "Place", "lang": "en", "draft": True, "simulated": True,
            "generatedAt": "2026-10-09T12:00:00Z", "dateRange": {"start": "2025-04-12", "end": "2026-01-15"},
            "flights": 3, "dek": "Plain dek", "dekHtml": "A <em>dek</em>", "shareImage": "share.jpg", **meta,
        },
        "brand": {"name": "Brand", "alt": "Brand logo", "theme": "dark", "grain": True, "themeColor": "#0b0c0b",
                  "logos": {"onDark": "assets/brand/w.svg"}, "partners": [{"name": "P", "role": "Owner"}]},
        "vantages": [{"id": "ov", "name": "Overview", "kind": "drone", "aspect": 1.6, "portraitFocus": [0.4, 0.6], "captures": caps}],
        "chapters": [
            {"type": "hero", "id": "open", "surface": "night", "title": "The Title", "kicker": "Place", "html": "<p>Line</p>",
             "vantage": "ov", "capture": 2, "video": video},
            {"type": "text", "id": "t", "surface": "paper", "title": "Text", "html": "<p>Body</p>", "pullQuote": "Quote", "attribution": "Someone"},
            {"type": "scrub", "id": "s1", "surface": "night", "vantage": "ov", "from": 0, "to": 2, "scrollVh": 225, "hold": 0.55,
             "steps": [{"capture": 0, "html": "<p>First</p>"}, {"capture": None, "html": "<p>Zoom</p>", "focus": [0.5, 0.5, 2]},
                       {"capture": None, "html": "<p>Aside</p>"}],
             "hotspots": [{"id": "h", "x": 0.25, "y": 0.75, "label": "Pin", "from": 0, "to": 2}]},
            {"type": "scrub", "id": "s2", "surface": "night", "vantage": "ov", "from": 1, "to": 2, "scrollVh": 150, "hold": 0.5,
             "steps": [], "hotspots": []},
            {"type": "compare", "id": "c", "surface": "night", "vantage": "ov", "before": 0, "after": 2, "mode": "curtain",
             "beforeLabel": "Then", "afterLabel": "Now", "steps": [{"capture": None, "html": "<p>Step</p>", "split": 0.3}], "hotspots": []},
            {"type": "video", "id": "v", "surface": "night", "title": "Clip", "video": video, "caption": "Cap", "loop": True},
            {"type": "stats", "id": "st", "surface": "paper", "items": [{"value": "48", "unit": "acres", "label": "Site", "note": 1,
                                                                        "source": "https://example.org"}]},
            {"type": "timeline", "id": "tl", "surface": "paper", "items": [
                {"date": "1970", "title": "Opened", "status": "done"},
                {"date": "Now", "title": "Rebuilt", "status": "planned", "vantage": "ov", "capture": 0}]},
            {"type": "gallery", "id": "g", "surface": "paper", "captures": [0, 1], "vantage": "ov",
             "images": [{"img": img("postcard"), "caption": "A <em>card</em>", "date": "1950", "credit": "Archive"}]},
            {"type": "explore", "id": "e", "surface": "night", "vantages": ["ov"]},
            {"type": "credits", "id": "cr", "surface": "paper", "title": "Credits", "sources": [{"label": "Src", "url": "https://x.org"}],
             "notes": ["Method <em>note</em>"]},
        ],
        "notes": [{"n": 1, "factId": "acres", "text": "48", "sources": ["https://www.example.org/plan"], "status": "verified"}],
    }  # fmt: skip


@pytest.fixture
def no_runtime_js(monkeypatch):
    monkeypatch.setattr(render, "runtime_js", lambda: "")


def test_story_json_cannot_close_its_script(no_runtime_js):
    s = story(title="</script><script>alert(1)</script> <!-- &  ")
    html = render_page(s, theme_css=":root{}")
    assert "</script><script>alert(1)" not in html
    assert html.count("</script>") == 2  # the class switch + the StoryJSON
    assert read_story(html) == s
    assert "<" not in story_json(s)


def test_page_shell(no_runtime_js):
    html = render_page(story(url="https://example.org/s/"), theme_css=":root{--v-accent:#fff}")
    assert html.startswith(
        '<!doctype html>\n<html lang="en" class="no-js" data-edition="site" data-theme="dark" data-sw="sw.js">'
    )
    assert '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">' in html
    assert 'classList.replace("no-js", "js")' in html
    assert '<style id="vantage-theme">:root{--v-accent:#fff}</style>' in html
    for needle in ('<link rel="manifest" href="manifest.webmanifest">', '<link rel="apple-touch-icon" href="apple-touch-icon.png">',
                   '<meta property="og:image" content="share.jpg">', '<meta name="twitter:card" content="summary_large_image">',
                   '<link rel="canonical" href="https://example.org/s/">', '<meta name="theme-color" content="#0b0c0b">',
                   '<meta name="apple-mobile-web-app-capable" content="yes">', '<meta name="robots" content="noindex">',
                   '<body class="v-surface-night v-grain">'):  # fmt: skip
        assert needle in html, needle
    assert "00-base.css" not in html and ".v-hero__stage" in html  # runtime CSS inlined
    assert '<script id="vantage-runtime">' not in html  # no runtime JS yet


def test_every_chapter_is_a_semantic_section(no_runtime_js):
    s = story()
    html = render_page(s, theme_css="")
    sections = re.findall(
        r'<section class="v-chapter v-(\w+) v-surface-(\w+)[^"]*" id="([\w-]+)" data-type="(\w+)" data-index="(\d+)"',
        html,
    )
    assert [(t, sf, i, int(n)) for t, sf, i, _, n in sections] == [
        (c["type"], c["surface"], c["id"], k) for k, c in enumerate(s["chapters"])
    ]
    assert all(t == d for t, _, _, d, _ in sections)
    assert (
        html.count("<h1") == 1
        and '<span class="v-badge">Preview</span>' in html
        and "Simulated imagery" in html
    )
    assert '<img class="v-logo" src="assets/brand/w.svg" alt="Brand logo">' in html
    assert 'style="--fx:40.0%;--fy:60.0%"' in html  # portrait focus
    assert (
        '<video autoplay muted loop playsinline preload="metadata" poster="assets/img/poster-960.jpg"' in html
    )
    hevc, h264 = html.index('src="assets/video/v-hevc.mp4"'), html.index('src="assets/video/v.mp4"')
    assert hevc < h264
    assert '<p class="v-dek">A <em>dek</em></p>' in html
    assert (
        'class="v-prose v-dropcap"' in html and '<blockquote class="v-pull"><p>Quote</p></blockquote>' in html
    )
    # scrub: one figure per step capture, a detail crop for focus steps, text for the rest; all captures without steps
    assert html.count('<li class="v-moment" data-capture="0">') == 2 and "--z:2" in html
    assert '<li class="v-moment v-moment--text"><div class="v-interlude"><p>Aside</p></div></li>' in html
    assert html.count('data-capture="1"') == 1 and html.count('data-capture="2"') == 1  # s2 shows 1..2
    assert '<span class="v-pin" style="--x:0.25;--y:0.75" aria-hidden="true">1</span>' in html
    assert (
        '<figcaption class="v-tag v-label"><b>Before</b><time datetime="2025-04-12">Then</time></figcaption>'
        in html
    )
    assert '<video controls muted playsinline loop preload="none"' in html
    assert '<sup class="v-note-ref"><a href="#v-note-1" aria-label="Note 1">1</a></sup>' in html
    assert '<li class="v-tl" data-status="planned">' in html
    assert (
        '<ul class="v-strip" aria-label="Gallery">' in html
        and '<span class="v-credit">Archive</span>' in html
    )
    assert (
        '<li id="v-note-1">48<span class="v-meta"><a href="https://www.example.org/plan" rel="noopener">example.org</a></span></li>'
        in html
    )
    assert 'Generated <time datetime="2026-10-09T12:00:00Z">Oct. 9, 2026</time> · Made with Vantage' in html


def test_pictures_are_responsive_and_stable():
    tag = picture(img("a"), "half")
    assert tag.startswith(
        '<picture><source type="image/avif" srcset="assets/img/a-960.avif 960w, assets/img/a-2560.avif 2560w"'
    )
    assert (
        '<img src="assets/img/a-960.jpg" srcset="assets/img/a-960.jpg 960w, assets/img/a-2560.jpg 2560w"'
        in tag
    )
    assert 'width="2560" height="1600" alt="a alt" loading="lazy" decoding="async"' in tag
    assert "background-color:#334455;background-image:url(data:image/jpeg;base64,AAAA)" in tag
    hero = picture(img("a"), "full", eager=True)
    assert 'fetchpriority="high"' in hero and "loading=" not in hero
    single = {**img("a"), "sources": [{"type": "image/jpeg", "srcset": [["assets/img/a-1600.jpg", 1600]]}]}
    assert picture(single).startswith('<img src="assets/img/a-960.jpg" width=')  # no <picture> for one JPEG
    assert 'class="v-lqip"' in picture(img("b", lqip_only=True))
    assert 'alt="say &#34;hi&#34;"' in picture({**img("c"), "alt": 'say "hi"'})


def test_embedded_editions(no_runtime_js):
    s = story()
    single = render_page(s, theme_css="", edition="single")
    assert 'data-edition="single"' in single and "data-sw" not in single
    assert 'rel="manifest"' not in single and 'rel="icon"' not in single and "og:image" not in single
    assert "<video" not in single and "<source" in single  # videos come from the runtime; pictures stay
    offline = render_page(s, theme_css="", edition="offline")
    assert "data-sw" not in offline and 'rel="manifest"' not in offline and 'rel="icon"' in offline
    assert "data-sw" not in render_page(s, theme_css="", service_worker=False)


def test_lite_drops_placeholders_from_grids(no_runtime_js):
    s = story()
    for c in s["vantages"][0]["captures"][1:]:
        c["img"] = img(c["date"], lqip_only=True)
    html = render_page(s, theme_css="", edition="lite")
    grid = html[html.index('id="e"') :]
    assert grid.count("<li><figure") == 1 and "Also flown: September 2025, Jan 2026." in grid
    assert "This lightweight edition" in grid


def test_runtime_js_is_one_strict_iife(monkeypatch, tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "10-b.js").write_text("var b = '</script>';")
    (tmp_path / "js" / "00-a.js").write_text("var a = 1;")
    monkeypatch.setattr(render, "RUNTIME_DIR", tmp_path)
    js = render.runtime_js()
    assert js.startswith('(function () {\n"use strict";\nvar a = 1;\nvar b') and js.endswith("})();\n")
    monkeypatch.setattr(render, "runtime_js", lambda: js)
    monkeypatch.setattr(render, "runtime_css", lambda: "")
    html = render_page(story(), theme_css="")
    assert "var b = '<\\/script>';" in html and '<script id="vantage-runtime">' in html
    assert json.loads(json.dumps(read_story(html))) == story()
