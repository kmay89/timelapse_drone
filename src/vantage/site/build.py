"""Build the static story site: masters + YAML → StoryJSON → index.html + assets.

`build_site` resolves capture references against masters/index.json, renders markdown (raw
HTML disabled) with `{fact:id}` notes, encodes responsive images and video clips, writes the
theme's fonts, brand logos (SVGs sanitized on the way: `sanitize_svg`), share card, icons, web manifest
and service worker, and renders index.html (docs/ARCHITECTURE.md: "StoryJSON", "Site output layout").
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.parsers import expat

from markdown_it import MarkdownIt

from vantage import log
from vantage.config import (
    FACT_TOKEN_RE,
    Chapter,
    CompareChapter,
    CreditsChapter,
    ExploreChapter,
    FrameRef,
    GalleryChapter,
    HeroChapter,
    Hotspot,
    Project,
    ScrubChapter,
    StatsChapter,
    Step,
    TextChapter,
    TimelineChapter,
    VideoChapter,
)
from vantage.media import ffmpeg
from vantage.models import MastersIndex
from vantage.paths import RUNTIME_DIR
from vantage.site.facts import BRAND_TEXT, FactNotes, check_release
from vantage.site.images import (
    CardText,
    ImageSpec,
    app_icon,
    encode_images,
    fast_mode,
    initials,
    share_card,
    strip_raster,
)
from vantage.site.render import MONTHS, capture_label, render_page
from vantage.site.theme import font_file, theme_css

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NIGHT_TYPES = {"hero", "scrub", "compare", "video", "explore"}
_CLIP_VERSION = 1
_SW_CONFIG = re.compile(r"/\*@config\*/.*?/\*@end\*/", re.S)
_LINK_SCHEMES = {"", "http", "https", "mailto", "tel"}
_LOGO_TYPES = {".svg", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif"}
# SVG elements that run code or embed HTML, and the URL attributes whose scheme is checked.
_SVG_DROP = {"script", "foreignobject", "handler", "iframe", "embed", "object"}
_SVG_ANIMATE = {"set", "animate"}
_SVG_URL_ATTRS = {"href", "src", "action", "formaction"}
_SCRIPT_SCHEMES = {"javascript", "vbscript", "livescript"}
_SVG_NS = "http://www.w3.org/2000/svg"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_XML_PREFIXES = {
    _SVG_NS: "",
    "http://www.w3.org/1999/xlink": "xlink",
    "http://www.w3.org/XML/1998/namespace": "xml",
}
_SVG_MAX_DEPTH = 200  # a logo is a few levels deep; this keeps the recursive serializer safe
_CONTROL = re.compile(r"[\x00-\x20\x7f]+")


class ReleaseError(ValueError):
    """A `--release` build was asked for, but the story is not ready to ship."""


# --------------------------------------------------------------------------- #
# Capture references and labels
# --------------------------------------------------------------------------- #


def resolve_capture_ref(ref: str | None, dates: Sequence[str], *, default: int = 0) -> int:
    """'earliest' | 'latest' | '#N' (0-based index) | 'YYYY-MM-DD' → index into the sorted `dates`.

    A date that is not a capture resolves to the nearest capture on or before it (or the earliest).
    """
    if not dates:
        raise ValueError("no captures to reference")
    if ref is None:
        return default
    if ref == "earliest":
        return 0
    if ref == "latest":
        return len(dates) - 1
    if ref.startswith("#") and ref[1:].isdigit():
        i = int(ref[1:])
        if i >= len(dates):
            raise ValueError(
                f"capture {ref!r} is out of range: only {len(dates)} captures (#0–#{len(dates) - 1})"
            )
        return i
    if _ISO_DATE.match(ref):
        before = [i for i, d in enumerate(dates) if d <= ref]
        return before[-1] if before else 0
    raise ValueError(f"capture reference {ref!r} is not earliest/latest/#N/YYYY-MM-DD")


def _month_range(start: str, end: str) -> str:
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if (a.year, a.month) == (b.year, b.month):
        return capture_label(start)
    if a.year == b.year:
        return f"{MONTHS[a.month - 1]}–{MONTHS[b.month - 1]} {b.year}"
    return f"{capture_label(start)} – {capture_label(end)}"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


@cache
def markdown() -> MarkdownIt:
    """CommonMark + tables, smart quotes and dashes; raw HTML disabled; links get rel="noopener"."""
    md = MarkdownIt("commonmark", {"html": False, "typographer": True}).enable(
        ["replacements", "smartquotes", "table", "strikethrough"]
    )

    def link_open(self: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
        tokens[idx].attrSet("rel", "noopener")
        return self.renderToken(tokens, idx, options, env)

    md.add_render_rule("link_open", link_open)
    return md


def safe_url(url: str | None, where: str) -> str | None:
    """A YAML-supplied link target, or None (with a warning) for javascript:, data: and other schemes."""
    if not url:
        return None
    if urlsplit(url.strip()).scheme.lower() in _LINK_SCHEMES:
        return url.strip()
    log.warn(f"{where}: link {url!r} dropped (only http, https, mailto and tel links are published)")
    return None


def inside(path: Path, root: Path, what: str) -> Path:
    """`path`, after checking it lies within `root`: YAML must not publish files from elsewhere."""
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"{what} {path} is outside {root}; keep the files it names in that folder")
    return path


# --------------------------------------------------------------------------- #
# Brand SVGs
# --------------------------------------------------------------------------- #


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()


def _scheme(value: str) -> str:
    """The URL scheme a browser would see: whitespace and control characters inside it are ignored."""
    head = _CONTROL.sub("", value).lower()
    return head.split(":", 1)[0] if ":" in head.split("/", 1)[0] else ""


def _unsafe_url(value: str) -> bool:
    scheme = _scheme(value)
    if scheme == "data":
        return not _CONTROL.sub("", value).lower().startswith("data:image/")
    return scheme in _SCRIPT_SCHEMES


def _refuse_declarations(data: bytes, what: str) -> None:
    """Refuse entity declarations and DOCTYPE internal subsets before ElementTree sees the file.

    A plain `<!DOCTYPE svg PUBLIC …>` (common in design-tool exports) is allowed: no DTD is fetched and
    re-serializing drops it. Entities are where billion-laughs and external-entity tricks live.
    """
    reason: list[str] = []
    depth = [0]

    def refuse(why: str) -> None:
        reason.append(why)
        raise ValueError(why)

    def start(*_: object) -> None:
        depth[0] += 1
        if depth[0] > _SVG_MAX_DEPTH:
            refuse(f"elements nested more than {_SVG_MAX_DEPTH} deep")

    def end(*_: object) -> None:
        depth[0] -= 1

    parser = expat.ParserCreate()
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.StartDoctypeDeclHandler = lambda _n, _s, _p, internal: (
        internal and refuse("a DOCTYPE internal subset")
    )
    parser.EntityDeclHandler = lambda *_: refuse("an entity declaration")
    parser.UnparsedEntityDeclHandler = lambda *_: refuse("an entity declaration")
    parser.ExternalEntityRefHandler = lambda *_: refuse("an external entity")
    try:
        parser.Parse(data, True)
    except (ValueError, expat.ExpatError) as exc:
        detail = f"it has {reason[0]}" if reason else f"it is not well-formed XML ({exc})"
        raise ValueError(
            f"{what}: refusing to publish this SVG: {detail}; re-export it as plain SVG"
        ) from None


def sanitize_svg(data: bytes, what: str = "SVG") -> tuple[bytes, list[str]]:
    """A copy of an SVG without anything that can run code when the file is opened on its own.

    Logos are shown through <img> (where SVG never runs script), but the copy in assets/brand/ can also
    be opened directly, on the story's own origin. So this drops <script>, <foreignObject> and any
    XHTML element, event-handler attributes (on*), javascript: and non-image data: URLs, <set>/<animate>
    that rewrite links or handlers, comments and processing instructions (xml-stylesheet). Raises
    ValueError for entity declarations, DOCTYPE internal subsets and roots other than an SVG <svg>.
    Returns the bytes and what was removed.
    """
    _refuse_declarations(data, what)
    root = ET.fromstring(data)  # comments and processing instructions are not kept
    if root.tag != f"{{{_SVG_NS}}}svg":
        raise ValueError(f'{what}: not an SVG document (the root must be <svg xmlns="{_SVG_NS}">)')
    removed: list[str] = []
    parents = [root]
    while parents:  # depth-first; a dropped element's subtree is not visited (or reported)
        parent = parents.pop()
        for child in list(parent):
            name, target = _local(child.tag), _local(child.get("attributeName", ""))
            animates_code = name in _SVG_ANIMATE and (target in _SVG_URL_ATTRS or target.startswith("on"))
            if name in _SVG_DROP or child.tag.startswith(f"{{{_XHTML_NS}}}") or animates_code:
                parent.remove(child)
                removed.append(f"<{name}>")
            else:
                parents.append(child)
    for el in root.iter():
        for key, value in list(el.attrib.items()):
            name, scheme = _local(key), _scheme(value)
            if name.startswith("on"):
                removed.append(f"{name}=…")
            elif (name in _SVG_URL_ATTRS and _unsafe_url(value)) or scheme in _SCRIPT_SCHEMES:
                removed.append(f"{name}={scheme}:…")
            else:
                continue
            del el.attrib[key]
    return _xml_bytes(root), removed


def _xml_bytes(root: ET.Element) -> bytes:
    """Serialize with SVG as the default namespace and the usual xlink/xml prefixes (ElementTree would
    write ns0:svg). Attributes in the SVG namespace itself are not SVG attributes and are dropped."""
    prefixes = dict(_XML_PREFIXES)
    used: dict[str, str] = {}

    def qname(name: str) -> str:
        if not name.startswith("{"):
            return name
        uri, local = name[1:].split("}", 1)
        prefix = used[uri] = prefixes.setdefault(uri, f"ns{len(prefixes) - 2}")
        return f"{prefix}:{local}" if prefix else local

    def attr(value: str) -> str:
        return html.escape(value).replace("\t", "&#9;").replace("\n", "&#10;").replace("\r", "&#13;")

    def walk(el: ET.Element) -> str:
        tag = qname(el.tag)
        attrs = "".join(
            f' {qname(k)}="{attr(v)}"' for k, v in el.attrib.items() if not k.startswith(f"{{{_SVG_NS}}}")
        )
        if el is root:
            attrs = (
                "".join(f' xmlns{":" * bool(p)}{p}="{uri}"' for uri, p in used.items() if p != "xml") + attrs
            )
        inner = html.escape(el.text or "", quote=False) + "".join(walk(child) for child in el)
        tail = html.escape(el.tail or "", quote=False) if el is not root else ""
        return f"<{tag}{attrs}>{inner}</{tag}>{tail}" if inner else f"<{tag}{attrs}/>{tail}"

    for el in root.iter():  # every namespace in use, so the root can declare them all
        qname(el.tag)
        for key in el.attrib:
            qname(key)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{walk(root)}\n'.encode()


# --------------------------------------------------------------------------- #
# Vantages and captures
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Capture:
    date: str
    label: str
    note: str | None
    path: Path


@dataclass(frozen=True)
class _Vantage:
    id: str
    name: str
    kind: str
    aspect: float
    focus: list[float] | None
    captures: list[_Capture]

    @property
    def dates(self) -> list[str]:
        return [c.date for c in self.captures]


def load_masters(project: Project) -> MastersIndex:
    path = project.masters_dir / "index.json"
    if not path.is_file():
        raise FileNotFoundError(
            f"no masters for {project.slug}: {path} is missing. Run `vantage process {project.slug}` "
            "(or `vantage demo` for the demo) to align the footage first"
        )
    return MastersIndex.load(path)


def _vantages(project: Project, masters: MastersIndex) -> dict[str, _Vantage]:
    notes = {c.date.isoformat(): c for c in project.story.captures}
    out: dict[str, _Vantage] = {}
    excluded = {date for date, note in notes.items() if note.exclude}
    for v in project.story.vantages:
        mv = masters.vantages.get(v.id)
        caps = sorted((c for c in mv.captures if c.date not in excluded), key=lambda c: c.date) if mv else []
        if mv is None or not caps:
            log.warn(f"vantage {v.id!r} has no masters yet; chapters that use it are skipped")
            continue
        months = Counter(c.date[:7] for c in caps)
        captures = []
        for c in caps:
            note = notes.get(c.date)
            label = (note.label if note else None) or capture_label(c.date, day=months[c.date[:7]] > 1)
            captures.append(
                _Capture(c.date, label, note.note if note else None, project.masters_dir / c.file)
            )
        out[v.id] = _Vantage(v.id, v.name, v.kind, mv.width / mv.height, v.portrait_focus, captures)
    return out


# --------------------------------------------------------------------------- #
# Video clips (cached in work/clips, so a build without footage can reuse them)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Clip:
    h264: Path
    hevc: Path | None
    poster: Path


def _even(x: float) -> int:
    return max(2, round(x / 2) * 2)


def encode_clip(project: Project, ref: FrameRef, clip_s: float, *, hevc: bool = True) -> Clip | None:
    """H.264 (+ HEVC/hvc1) web clip of `clip_s` seconds from `ref`, short side ≤ 1080 px, + poster JPEG.

    Encodes from footage when present; otherwise reuses a cached encode; None when neither exists.
    """
    src = inside(project.footage_dir / ref.source, project.footage_dir, "clip source")
    key = hashlib.sha1(f"{_CLIP_VERSION}|{ref.source}|{ref.t:.3f}|{clip_s:.3f}".encode()).hexdigest()[:12]
    base = project.work_dir / "clips" / f"{Path(ref.source).stem}-{key}"
    h264, h265, poster = (
        base.with_name(base.name + suffix) for suffix in (".mp4", "-hevc.mp4", "-poster.jpg")
    )
    if src.is_file():
        info = ffmpeg.probe(src)
        if info.kind != "video":
            log.warn(f"{ref.source} is not a video; no clip made")
            return None
        w, h = (info.height, info.width) if abs(info.rotation) in (90, 270) else (info.width, info.height)
        scale = min(1.0, 1080 / min(w, h))
        vf = f"scale={_even(w * scale)}:{_even(h * scale)}:flags=lanczos"
        common = ["-ss", f"{ref.t:.3f}", "-i", src, "-t", f"{clip_s:.3f}", "-an", "-vf", vf]
        tail = ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-map_metadata", "-1",
                "-fflags", "+bitexact", "-flags:v", "+bitexact", "-threads", "4"]  # fmt: skip
        if not h264.is_file():
            _encode(
                h264,
                [*common, "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "23", *tail],
            )
        if hevc and not h265.is_file():
            x265 = ["-c:v", "libx265", "-tag:v", "hvc1", "-preset", "medium", "-crf", "26"]
            _encode(h265, [*common, *x265, "-x265-params", "log-level=error", *tail])
        if not poster.is_file():
            ffmpeg.extract_frame(src, ref.t, poster)
    if not h264.is_file():
        return None
    if not poster.is_file():
        ffmpeg.extract_frame(h264, 0.0, poster)
    return Clip(h264, h265 if hevc and h265.is_file() else None, poster)


def _encode(out: Path, args: list[str | Path]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".part" + out.suffix)
    ffmpeg.run([*args, part])
    part.replace(out)


# --------------------------------------------------------------------------- #
# StoryJSON
# --------------------------------------------------------------------------- #


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _generated_at() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = dt.datetime.fromtimestamp(int(epoch), dt.UTC) if epoch else dt.datetime.now(dt.UTC)
    return when.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _camel(key: str) -> str:
    head, *rest = key.split("_")
    return head + "".join(word.capitalize() for word in rest)


def _dest(*parts: str) -> str:
    return "/".join(re.sub(r"[^A-Za-z0-9._-]+", "-", p).strip("-") or "x" for p in parts)


class StoryBuilder:
    """Assembles StoryJSON; images are registered as placeholders and encoded in one parallel pass."""

    def __init__(self, project: Project, masters: MastersIndex, site_dir: Path) -> None:
        self.project = project
        self.site_dir = site_dir
        self.vantages = _vantages(project, masters)
        self.notes = FactNotes(project.facts.facts)
        self._specs: list[tuple[ImageSpec, dict[str, Any]]] = []
        self._placeholders: dict[str, dict[str, Any]] = {}

    # -- text ---------------------------------------------------------------- #

    def md(self, text: str | None) -> str | None:
        if not text:
            return None
        return self.notes.finish(markdown().render(self.notes.mark(text))).strip()

    def inline(self, text: str | None) -> str | None:
        if not text:
            return None
        return self.notes.finish(markdown().renderInline(self.notes.mark(text)))

    def plain(self, text: str | None) -> str | None:
        return self.notes.plain(text)

    # -- media --------------------------------------------------------------- #

    def img(self, src: Path, dest: str, alt: str) -> dict[str, Any]:
        """A placeholder Img, filled in by `encode()`; the same dest always yields the same dict."""
        if dest not in self._placeholders:
            self._placeholders[dest] = {"alt": alt}
            self._specs.append((ImageSpec(src, dest, alt), self._placeholders[dest]))
        return self._placeholders[dest]

    def encode(self) -> None:
        settings = self.project.config.output.images
        cache_dir = self.project.work_dir / "cache" / "img"
        imgs = encode_images([s for s, _ in self._specs], self.site_dir, settings, cache_dir=cache_dir)
        for spec, placeholder in self._specs:
            placeholder.clear()
            placeholder.update(imgs[spec.dest])

    def video(self, chapter_id: str, ref: FrameRef, clip_s: float, alt: str) -> dict[str, Any] | None:
        try:
            hevc = not fast_mode() and "libx265" in ffmpeg.encoders()
            clip = encode_clip(self.project, ref, clip_s, hevc=hevc)
        except ffmpeg.FFmpegError as exc:
            log.warn(f"chapter {chapter_id!r}: video encode failed ({exc}); using a still instead")
            return None
        if clip is None:
            log.warn(f"chapter {chapter_id!r}: no footage or cached clip for {ref.source}; video dropped")
            return None
        sources = []
        for path, name, mime in (
            (clip.hevc, f"{chapter_id}-hevc.mp4", 'video/mp4; codecs="hvc1"'),
            (clip.h264, f"{chapter_id}.mp4", "video/mp4"),
        ):
            if path is not None:
                target = self.site_dir / "assets" / "video" / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                sources.append({"src": f"assets/video/{name}", "type": mime})
        poster = self.img(clip.poster, _dest("assets", "img", "video", chapter_id), alt)
        return {"poster": poster, "sources": sources}

    def capture_img(self, v: _Vantage, i: int) -> dict[str, Any]:
        c = v.captures[i]
        note = self.plain(c.note)
        alt = f"{self.plain(v.name)}, {self.plain(c.label)}" + (f": {note}" if note else "")
        return self.img(c.path, _dest("assets", "img", v.id, c.date), alt)

    def brand_file(self, rel: str | None) -> str | None:
        brand = self.project.brand
        path = brand.resolve(rel)
        if path is None:
            return None
        if brand.root is not None:
            inside(path, brand.root, "brand.yaml: logo")
        if path.suffix.lower() not in _LOGO_TYPES:
            raise ValueError(f"brand.yaml: logo {rel!r} is not an image ({', '.join(sorted(_LOGO_TYPES))})")
        if not path.is_file():
            log.warn(f"brand file {path} is missing")
            return None
        try:
            name = path.resolve().relative_to(brand.root.resolve()).as_posix() if brand.root else path.name
        except ValueError:
            name = path.name
        url = "assets/brand/" + "/".join(_dest(p) for p in name.split("/"))
        target = self.site_dir / url
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == ".svg":
            data, removed = sanitize_svg(path.read_bytes(), f"brand.yaml: logo {rel!r}")
            if removed:
                log.warn(f"brand.yaml: logo {rel!r}: removed {', '.join(sorted(set(removed)))}")
            target.write_bytes(data)
        else:
            try:
                data, removed = strip_raster(path)
            except (OSError, ValueError) as exc:  # unreadable, truncated or not the image it claims to be
                raise ValueError(f"brand.yaml: logo {rel!r} is not a readable image: {exc}") from None
            if removed:
                log.warn(f"brand.yaml: logo {rel!r}: removed {', '.join(removed)}")
            target.write_bytes(data)
        return url

    # -- references ---------------------------------------------------------- #

    def vantage(self, vid: str | None, where: str) -> _Vantage | None:
        if vid is None:
            return next(iter(self.vantages.values()), None)
        v = self.vantages.get(vid)
        if v is None:
            log.warn(f"{where}: vantage {vid!r} has no captures; skipped")
        return v

    @staticmethod
    def ref(v: _Vantage, ref: str | None, default: int, where: str) -> int:
        try:
            return resolve_capture_ref(ref, v.dates, default=default)
        except ValueError as exc:
            raise ValueError(f"story.yaml {where}: {exc}") from None

    def steps(self, v: _Vantage, steps: list[Step], lo: int, hi: int, where: str) -> list[dict[str, Any]]:
        out = []
        for j, s in enumerate(steps):
            cap = None if s.capture is None else self.ref(v, s.capture, lo, f"{where} step {j}")
            if cap is not None and not lo <= cap <= hi:
                log.warn(f"{where} step {j}: capture {s.capture!r} is outside the chapter's range; clamped")
                cap = min(max(cap, lo), hi)
            out.append(
                _compact({"capture": cap, "html": self.md(s.text) or "", "focus": s.focus, "split": s.split})
            )
        return out

    def hotspots(self, v: _Vantage, hotspots: list[Hotspot], where: str) -> list[dict[str, Any]]:
        last = len(v.captures) - 1
        return [
            _compact({
                "id": h.id, "x": h.x, "y": h.y, "label": self.plain(h.label), "html": self.md(h.body),
                "from": self.ref(v, h.from_, 0, f"{where} hotspot {h.id}"),
                "to": self.ref(v, h.to, last, f"{where} hotspot {h.id}"),
            })
            for h in hotspots
        ]  # fmt: skip

    # -- chapters: one handler per type, each returning the chapter or None to drop it -------- #

    def chapter(self, ch: Chapter, cid: str) -> dict[str, Any] | None:
        dark = self.project.brand.theme == "dark"
        imagery = ch.type in _NIGHT_TYPES or (isinstance(ch, GalleryChapter) and ch.vantage is not None)
        base = {
            "type": ch.type,
            "id": cid,
            "surface": ch.surface or ("night" if dark and imagery else "paper"),
            "kicker": self.plain(ch.kicker),
            "title": self.plain(ch.title),
            "html": self.md(ch.body),
        }
        body = getattr(self, f"_{ch.type}")(ch, base, f"chapter {cid!r}")
        return None if body is None else _compact(body)

    def _hero(self, ch: HeroChapter, base: dict[str, Any], where: str) -> dict[str, Any]:
        cfg = self.project.config
        out = base | {
            "kicker": base["kicker"] or self.plain(cfg.kicker),
            "title": base["title"] or self.plain(cfg.title),
        }
        v = self.vantage(ch.vantage, where)
        if v is not None:
            out |= {"vantage": v.id, "capture": self.ref(v, ch.capture, len(v.captures) - 1, where)}
        if ch.video is not None:
            out["video"] = self.video(base["id"], ch.video, ch.clip_s, out["title"])
        return out

    def _scrub(self, ch: ScrubChapter, base: dict[str, Any], where: str) -> dict[str, Any] | None:
        v = self.vantage(ch.vantage, where)
        if v is None:
            return None
        lo = self.ref(v, ch.from_, 0, where)
        hi = self.ref(v, ch.to, len(v.captures) - 1, where)
        if lo > hi:
            raise ValueError(f"story.yaml {where}: 'from' ({ch.from_}) is after 'to' ({ch.to})")
        return base | {
            "vantage": v.id, "from": lo, "to": hi, "scrollVh": ch.scroll_vh or min(900, 75 * (hi - lo + 1)),
            "hold": ch.hold, "steps": self.steps(v, ch.steps, lo, hi, where),
            "hotspots": self.hotspots(v, ch.hotspots, where),
        }  # fmt: skip

    def _compare(self, ch: CompareChapter, base: dict[str, Any], where: str) -> dict[str, Any] | None:
        v = self.vantage(ch.vantage, where)
        if v is None:
            return None
        before = self.ref(v, ch.before, 0, where)
        after = self.ref(v, ch.after, len(v.captures) - 1, where)
        if before == after:
            log.warn(f"{where}: before and after are the same capture ({v.captures[before].date})")
        return base | {
            "vantage": v.id, "mode": ch.mode, "before": before, "after": after,
            "beforeLabel": self.plain(ch.before_label or v.captures[before].label),
            "afterLabel": self.plain(ch.after_label or v.captures[after].label),
            "steps": self.steps(v, ch.steps, 0, len(v.captures) - 1, where),
            "hotspots": self.hotspots(v, ch.hotspots, where),
        }  # fmt: skip

    def _video(self, ch: VideoChapter, base: dict[str, Any], where: str) -> dict[str, Any] | None:
        video = self.video(base["id"], ch.clip, ch.clip_s, base["title"] or "Video")
        if video is None:
            return None
        return base | {"video": video, "caption": self.inline(ch.caption), "loop": ch.loop}

    def _text(self, ch: TextChapter, base: dict[str, Any], where: str) -> dict[str, Any]:
        return base | {"pullQuote": self.inline(ch.pull_quote), "attribution": self.plain(ch.attribution)}

    def _stats(self, ch: StatsChapter, base: dict[str, Any], where: str) -> dict[str, Any]:
        items = []
        for s in ch.items:
            ids = FACT_TOKEN_RE.findall(s.value)
            items.append(_compact({
                "value": self.plain(s.value), "unit": self.plain(s.unit), "label": self.plain(s.label),
                "source": s.source, "note": self.notes.number(ids[0]) if ids else None,
            }))  # fmt: skip
        return base | {"items": items}

    def _timeline(self, ch: TimelineChapter, base: dict[str, Any], where: str) -> dict[str, Any]:
        items = []
        for item in ch.items:
            entry: dict[str, Any] = {
                "date": self.plain(item.date), "title": self.plain(item.title), "html": self.md(item.body),
                "status": item.status, "source": safe_url(item.source, f"{where} item {item.title!r}"),
            }  # fmt: skip
            v = self.vantage(item.vantage, f"{where} item {item.title!r}") if item.capture else None
            if v is not None:
                entry |= {
                    "vantage": v.id,
                    "capture": self.ref(v, item.capture, 0, f"{where} item {item.title!r}"),
                }
            items.append(_compact(entry))
        return base | {"items": items}

    def _gallery(self, ch: GalleryChapter, base: dict[str, Any], where: str) -> dict[str, Any] | None:
        out = base | {"captures": [], "images": []}
        v = self.vantage(ch.vantage, where) if ch.vantage else None
        if v is not None:
            refs = ch.captures or [f"#{i}" for i in range(len(v.captures))]
            out |= {"vantage": v.id, "captures": [self.ref(v, r, 0, where) for r in refs]}
        for gi in ch.images:
            path = inside(
                self.project.root / gi.file, self.project.root, f"story.yaml {where}: gallery image"
            )
            if not path.is_file():
                log.warn(f"{where}: gallery image {gi.file} is missing; skipped")
                continue
            alt = self.plain(gi.alt or gi.caption) or Path(gi.file).stem.replace("-", " ")
            name = Path(gi.file).with_suffix("").as_posix()  # the whole path: two folders may share a name
            out["images"].append(_compact({
                "img": self.img(path, _dest("assets", "img", "gallery", base["id"], name), alt),
                "caption": self.inline(gi.caption), "date": self.plain(gi.date), "credit": self.plain(gi.credit),
            }))  # fmt: skip
        return out if out["captures"] or out["images"] else None

    def _explore(self, ch: ExploreChapter, base: dict[str, Any], where: str) -> dict[str, Any] | None:
        ids = [vid for vid in (ch.vantages or list(self.vantages)) if self.vantage(vid, where)]
        return base | {"vantages": ids} if ids else None

    def _credits(self, ch: CreditsChapter, base: dict[str, Any], where: str) -> dict[str, Any]:
        return base | {
            "sources": [
                _compact({"label": self.plain(s.label), "url": safe_url(s.url, f"{where} source")})
                for s in ch.sources
            ],
            "notes": [self.inline(n) or "" for n in ch.notes],
        }

    # -- the whole story ----------------------------------------------------- #

    def meta(self) -> dict[str, Any]:
        cfg = self.project.config
        dates = sorted({c.date for v in self.vantages.values() for c in v.captures})
        url = safe_url(cfg.output.base_url, "project.yaml output.base_url")
        # Name and region only: lat/lon stay in project.yaml, so no edition pins an unannounced site.
        loc = cfg.location
        return _compact({
            "slug": cfg.slug, "title": self.plain(cfg.title), "subtitle": self.plain(cfg.subtitle),
            "kicker": self.plain(cfg.kicker), "byline": self.plain(cfg.byline), "lang": cfg.lang,
            "location": _compact({"name": loc.name, "region": loc.region}) if loc else None,
            "draft": cfg.draft, "simulated": cfg.simulated, "generatedAt": _generated_at(),
            "dateRange": {"start": dates[0], "end": dates[-1]} if dates else None,
            "flights": len(dates), "url": url, "shortTitle": self._short_title(),
            "shareImage": (url.rstrip("/") + "/share.jpg") if url else "share.jpg",
        })  # fmt: skip

    def _short_title(self) -> str:
        """The home-screen label: brand.short_name, else the title or its first word (≤ 14 characters)."""
        title = self.plain(self.project.config.title) or ""
        return self.project.brand.short_name or (title if len(title) <= 14 else title.split()[0])

    def brand(self) -> dict[str, Any]:
        b = self.project.brand
        return _compact({
            "name": b.name, "url": safe_url(b.url, "brand.yaml url"), "alt": b.logos.alt or b.name,
            "theme": b.theme, "grain": b.grain,
            "logos": _compact({
                "primary": self.brand_file(b.logos.primary), "onDark": self.brand_file(b.logos.on_dark),
                "mark": self.brand_file(b.logos.mark),
            }),
            "partners": [
                _compact({"name": p.name, "role": p.role, "url": safe_url(p.url, f"brand.yaml {p.name!r} url"),
                          "logo": self.brand_file(p.logo), "logoOnDark": self.brand_file(p.logo_on_dark)})
                for p in b.partners
            ],
            **{_camel(key): self.plain(getattr(b, key)) for key in BRAND_TEXT},
        })  # fmt: skip

    def build(self) -> dict[str, Any]:
        cfg = self.project.config
        if not self.vantages:
            raise ValueError(f"{self.project.slug}: no vantage has any masters to build from")
        meta = self.meta()
        chapters: list[dict[str, Any]] = []
        ids = Counter[str]()
        story_chapters = self.project.story.chapters
        # the dek is read right under the opening hero, so its facts are numbered there
        lead_hero = bool(story_chapters) and isinstance(story_chapters[0], HeroChapter)
        if not lead_hero:
            meta |= self._dek(cfg.dek)
        for i, ch in enumerate(story_chapters):
            cid = _dest(ch.id or f"{ch.type}-{i + 1}")
            ids[cid] += 1
            if ids[cid] > 1:
                cid = f"{cid}-{ids[cid]}"
            built = self.chapter(ch, cid)
            if built is not None:
                chapters.append(built)
            if i == 0 and lead_hero:
                meta |= self._dek(cfg.dek)
        vantages = [
            _compact({
                "id": v.id, "name": self.plain(v.name), "kind": v.kind, "aspect": round(v.aspect, 6),
                "portraitFocus": v.focus,
                "captures": [
                    _compact({"date": c.date, "label": self.plain(c.label), "note": self.plain(c.note),
                              "img": self.capture_img(v, i)})
                    for i, c in enumerate(v.captures)
                ],
            })
            for v in self.vantages.values()
        ]  # fmt: skip
        brand = self.brand()  # before counting: brand text can cite facts too
        meta["unverifiedFacts"] = self.notes.unverified()
        story = {
            "version": 1,
            "meta": meta,
            "brand": brand,
            "vantages": vantages,
            "chapters": chapters,
            "notes": self.notes.notes(),
        }
        self.encode()
        return story

    def _dek(self, dek: str | None) -> dict[str, Any]:
        """The standfirst as HTML (with fact notes) and as plain text for descriptions and cards."""
        if not dek:
            return {}
        text = html.unescape(re.sub(r"<[^>]+>", "", markdown().renderInline(self.plain(dek) or "")))
        return {"dek": text, "dekHtml": self.inline(dek)}


# --------------------------------------------------------------------------- #
# Site files
# --------------------------------------------------------------------------- #


_STORY_MARKER = b'<script id="vantage-story"'
_VCS_DIRS = (".git", ".hg", ".svn")


def _prepare(out_dir: Path) -> None:
    """Empty out_dir for a fresh build. `--out` can name any folder, so a non-empty one is wiped only
    when it is an earlier Vantage build (index.html carries the StoryJSON) outside version control."""
    if out_dir.exists() and any(out_dir.iterdir()):
        index = out_dir / "index.html"
        vcs = [name for name in _VCS_DIRS if (out_dir / name).exists()]
        reason = ""
        if not index.is_file() or _STORY_MARKER not in index.read_bytes():
            reason = "it is not empty and is not an earlier Vantage build"
        elif vcs:
            reason = f"it is under version control ({vcs[0]})"
        if reason:
            raise FileExistsError(
                f"refusing to replace {out_dir}: {reason}; build into an empty folder and copy the files over"
            )
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)


def _site_files(site: Path) -> list[Path]:
    return sorted((p for p in site.rglob("*") if p.is_file()), key=lambda p: p.relative_to(site).as_posix())


def _fallbacks(node: Any) -> Iterator[str]:
    """The fallback JPEG of every Img in the story (posters included)."""
    if isinstance(node, dict):
        if "fallback" in node and "lqip" in node:
            yield node["fallback"]
        for value in node.values():
            yield from _fallbacks(value)
    elif isinstance(node, list):
        for value in node:
            yield from _fallbacks(value)


def _service_worker(site: Path, story: dict[str, Any]) -> Path:
    files = [p for p in _site_files(site) if p.name != "sw.js"]
    rel = [p.relative_to(site).as_posix() for p in files]
    digest = hashlib.sha256()
    for path, name in zip(files, rel, strict=True):
        digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    core = {
        "./",
        "index.html",
        "manifest.webmanifest",
        "icon-192.png",
        "icon-512.png",
        "apple-touch-icon.png",
    }
    core |= {r for r in rel if r.startswith(("assets/fonts/", "assets/brand/"))}
    core |= set(_fallbacks(story))  # one JPEG per image; the rest is fetched by "Save for offline"
    config = {
        "cache": f"vantage-{story['meta']['slug']}-{digest.hexdigest()[:12]}",
        "core": sorted(core & ({"./"} | set(rel))),
        "assets": [[name, path.stat().st_size] for path, name in zip(files, rel, strict=True)],
    }
    template = (RUNTIME_DIR / "sw.js").read_text(encoding="utf-8")
    js = _SW_CONFIG.sub(lambda _: json.dumps(config, separators=(",", ":")), template, count=1)
    out = site / "sw.js"
    out.write_text(js, encoding="utf-8")
    return out


def _app_files(
    project: Project, site: Path, story: dict[str, Any], builder: StoryBuilder, colors: dict[str, str]
) -> None:
    """share.jpg (hero capture + title), monogram icons and manifest.webmanifest."""
    brand, meta = project.brand, story["meta"]
    hero = next((c for c in story["chapters"] if c["type"] == "hero"), {})
    v = builder.vantages.get(hero.get("vantage", "")) or next(iter(builder.vantages.values()))
    photo = v.captures[hero.get("capture", -1)].path
    span = meta.get("dateRange")
    card = CardText(
        title=meta["title"],
        kicker=meta.get("kicker"),
        wordmark=brand.name,
        detail=_month_range(span["start"], span["end"]) if span else None,
    )
    display, text = font_file(brand, "display"), font_file(brand, "text")
    share_card(photo, site / "share.jpg", card, colors=colors, display_font=display, text_font=text)
    letters = initials(brand.short_name or brand.name)
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        app_icon(site / name, size, letters, colors=colors, display_font=display)
    background = story["brand"]["themeColor"]
    manifest = _compact({
        "name": meta["title"], "short_name": meta["shortTitle"], "description": meta.get("dek"), "lang": meta["lang"],
        "id": "./", "start_url": "./", "scope": "./", "display": "standalone",
        "background_color": background, "theme_color": background,
        "icons": [
            {"src": "icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    })  # fmt: skip
    (site / "manifest.webmanifest").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def build_site(project: Project, out_dir: Path, *, release: bool = False) -> Path:
    """Build dist/<slug>/site (out_dir); returns out_dir / "index.html"."""
    if release:
        problems = check_release(project)
        if problems:
            raise ReleaseError("release build blocked:\n  " + "\n  ".join(problems))
    masters = load_masters(project)
    out_dir = out_dir.resolve()
    _prepare(out_dir)
    builder = StoryBuilder(project, masters, out_dir)
    story = builder.build()
    theme = theme_css(project.brand, italic="<em>" in json.dumps(story))
    for font in theme.fonts:
        target = out_dir / font.dest
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(font.src, target)
    dark = project.brand.theme == "dark"
    story["brand"]["themeColor"] = theme.colors["night" if dark else "paper"]
    _app_files(project, out_dir, story, builder, theme.colors)
    sw = project.config.output.service_worker
    index = out_dir / "index.html"
    index.write_text(render_page(story, theme_css=theme.css, service_worker=sw), encoding="utf-8")
    if sw:
        _service_worker(out_dir, story)
    return index
