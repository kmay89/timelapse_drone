"""StoryJSON → index.html: pre-rendered, semantic HTML that reads as a photo essay without JavaScript.

`render_page` is a pure function of the story (plus the runtime folder): Jinja2 templates in
`runtime/template.html` (page shell) and `site/templates/chapters/*.html` (one partial per chapter
type), inlined runtime CSS/JS, the theme CSS and the StoryJSON in
`<script id="vantage-story" type="application/json">` (with "<" escaped so no string can close it).

Editions: "site" (hosted: manifest, icons, `data-sw` for the runtime to register the service
worker), "offline" (zip: no manifest/worker), "single" and "lite" (one self-contained file:
videos carry no <source>, the runtime attaches embedded assets; see ARCHITECTURE.md).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from functools import cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader
from markupsafe import Markup, escape

from vantage.paths import RUNTIME_DIR

Edition = Literal["site", "offline", "single", "lite"]
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
_AP_MONTHS = (
    "Jan.",
    "Feb.",
    "March",
    "April",
    "May",
    "June",
    "July",
    "Aug.",
    "Sept.",
    "Oct.",
    "Nov.",
    "Dec.",
)
SIZES = {
    "bleed": "(min-width: 1400px) 1400px, (min-width: 48em) calc(100vw - 7rem), 100vw",
    "half": "(min-width: 1400px) 700px, (min-width: 48em) 50vw, 100vw",
    "strip": "(min-width: 48em) 34rem, 82vw",
    "thumb": "(min-width: 48em) 16vw, 45vw",
    "small": "22rem",
    "full": "100vw",
}


def _raw(text: str, tag: str) -> Markup:
    """Text for a raw <style>/<script> element: no "</tag" (any case) can end it early."""
    return Markup(re.sub(f"</({tag})", r"<\\/\1", text, flags=re.IGNORECASE))


def story_json(story: dict[str, Any]) -> str:
    """StoryJSON for a <script type="application/json"> block: "<" is escaped as \\u003c."""
    return json.dumps(story, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")


def _read_all(folder: Path, pattern: str) -> list[str]:
    return [p.read_text(encoding="utf-8") for p in sorted(folder.glob(pattern))] if folder.is_dir() else []


def runtime_css() -> str:
    return "\n".join(_read_all(RUNTIME_DIR / "css", "*.css"))


def runtime_js() -> str:
    """runtime/js/*.js concatenated (filename order) inside one strict IIFE; "" when there are none."""
    parts = _read_all(RUNTIME_DIR / "js", "*.js")
    return '(function () {\n"use strict";\n' + "\n".join(parts) + "\n})();\n" if parts else ""


def capture_label(date: str, *, day: bool = False) -> str:
    """'2025-06-14' → 'June 2025' (or 'June 14, 2025' when two flights share the month)."""
    d = dt.date.fromisoformat(date[:10])
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}" if day else f"{MONTHS[d.month - 1]} {d.year}"


def _date(iso: str) -> str:
    """'2026-09-20' → 'Sept. 20, 2026' (AP style)."""
    d = dt.date.fromisoformat(iso[:10])
    return f"{_AP_MONTHS[d.month - 1]} {d.day}, {d.year}"


def _host(url: str) -> str:
    host = urlsplit(url).netloc
    return host.removeprefix("www.") or url


def _srcset(entries: list[list[Any]]) -> str:
    return ", ".join(f"{url} {w}w" for url, w in entries)


def placeholder(img: dict[str, Any]) -> bool:
    """True for an image reduced to its blurred LQIP (lite edition): grids and strips leave it out."""
    return not img["sources"] and img["fallback"] == img["lqip"]


def picture(img: dict[str, Any], sizes: str = "bleed", *, eager: bool = False) -> Markup:
    """<picture> with AVIF/WebP <source>s and a JPEG <img> (srcset + intrinsic size + LQIP background)."""
    jpeg = next((s for s in img["sources"] if s["type"] == "image/jpeg"), None)
    sizes_attr = SIZES.get(sizes, sizes)
    sources = "".join(
        f'<source type="{s["type"]}" srcset="{escape(_srcset(s["srcset"]))}" sizes="{sizes_attr}">'
        for s in img["sources"]
        if s["type"] != "image/jpeg"
    )
    srcset = (
        f' srcset="{escape(_srcset(jpeg["srcset"]))}" sizes="{sizes_attr}"'
        if jpeg and len(jpeg["srcset"]) > 1
        else ""
    )
    loading = ' fetchpriority="high" decoding="async"' if eager else ' loading="lazy" decoding="async"'
    style = f"background-color:{img['color']};background-image:url({img['lqip']})"
    cls = ' class="v-lqip"' if placeholder(img) else ""
    tag = (
        f'<img src="{escape(img["fallback"])}"{srcset} width="{img["w"]}" height="{img["h"]}"'
        f' alt="{escape(img["alt"])}"{loading} style="{style}"{cls}>'
    )
    return Markup(f"<picture>{sources}{tag}</picture>" if sources else tag)


def _vantages(story: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["id"]: v for v in story["vantages"]}


@cache
def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader([str(RUNTIME_DIR), str(TEMPLATES_DIR)]),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.globals["picture"] = picture
    env.tests["placeholder"] = placeholder
    env.filters["date"] = _date
    env.filters["month"] = capture_label
    env.filters["host"] = _host
    env.filters["pad"] = lambda n, width=2: str(n).zfill(width)
    return env


def render_page(
    story: dict[str, Any],
    *,
    theme_css: str,
    base_href: str | None = None,
    edition: Edition = "site",
    service_worker: bool = True,
) -> str:
    """The full index.html for `story` (StoryJSON, see docs/ARCHITECTURE.md)."""
    meta = story["meta"]
    vantages = _vantages(story)
    share = meta.get("shareImage")
    if edition in ("single", "lite") and share and not share.startswith(("http://", "https://")):
        share = None  # nothing to point at next to a lone file
    first_text = next((c["id"] for c in story["chapters"] if c["type"] == "text" and c.get("html")), None)
    return (
        _env()
        .get_template("template.html")
        .render(
            meta=meta,
            brand=story["brand"],
            chapters=story["chapters"],
            notes=story.get("notes", []),
            vantages=vantages,
            edition=edition,
            hosted=edition == "site",
            embedded=edition in ("single", "lite"),
            service_worker=service_worker and edition == "site",
            share_image=share,
            base_href=base_href,
            dropcap=first_text,
            theme_css=_raw(theme_css, "style"),
            runtime_css=_raw(runtime_css(), "style"),
            runtime_js=_raw(runtime_js(), "script"),
            story_json=Markup(story_json(story)),
            has_credits=any(c["type"] == "credits" for c in story["chapters"]),
        )
    )
