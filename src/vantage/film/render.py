"""MP4 time-lapse films: every visit of a vantage as one continuous, slowly pushing camera move.

    <out>/<slug>-16x9.mp4 + .jpg poster    output.film width × height
    <out>/<slug>-9x16.mp4 + .jpg poster    phones and social (output.film.vertical)

A film is a title card, one section per vantage (the main vantage first; the
vertical cut keeps only the main one) and an end card. Inside a section each
capture holds `hold_s` and crossfades `fade_s` into the next (blended in linear
light) under an eased push that spans the whole section, with a date lower-third
and a calendar rail whose ticks sit at the real flight dates. Cards dissolve into
and out of the imagery while the camera keeps moving.

Frames are composed with numpy/OpenCV and piped to ffmpeg (media.ffmpeg.FrameWriter).
Text is typeset once per card and capture with Pillow in the brand fonts and
alpha-composited, so a frame costs one warp, the occasional blend and a few small
composites. VANTAGE_FAST=1 caps the long edge at 960 px and encodes with a fast preset.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vantage import log
from vantage.config import BrandKit, FontSpec, Project, ScrubChapter
from vantage.media.ffmpeg import FrameWriter
from vantage.models import MastersIndex
from vantage.paths import RUNTIME_DIR
from vantage.site.render import capture_label as date_label

PUSH = 0.06  # Ken-Burns zoom across a section (1.00 → 1.06)
PAN = 0.05  # vertical cut: drift either side of the portrait focus, in image widths
FAST_LONG_EDGE = 960
POSTER_QUALITY = 90
SIMULATED_NOTE = "Simulated imagery for demonstration; not actual aerial footage."
# BT.709 tags and matrix (players guess wrong otherwise), no timestamps → reproducible bytes.
_ENCODE = (
    "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
)  # fmt: skip
_SQUARE = ((np.arange(256, dtype=np.float32) / 255) ** 2).reshape(1, 256)  # gamma-2 "linear light"

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
Role = Literal["display", "text", "numeric"]


def _fast() -> bool:
    return os.environ.get("VANTAGE_FAST", "").lower() not in ("", "0", "false", "no")


def _ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def _even(n: float) -> int:
    return max(2, round(n / 2) * 2)


def _hex(color: str) -> tuple[int, int, int]:
    """'#rgb' / '#rrggbb' → (r, g, b)."""
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _mix(a: np.ndarray, b: np.ndarray, w: float) -> np.ndarray:
    """Crossfade two BGR uint8 frames in (gamma-2) linear light, so dissolves don't dip in brightness."""
    if w <= 0:
        return a
    if w >= 1:
        return b
    m = cv2.addWeighted(cv2.LUT(a, _SQUARE), 1 - w, cv2.LUT(b, _SQUARE), w, 0.0)
    return cv2.convertScaleAbs(cv2.sqrt(m), alpha=255.0)


def capture_labels(project: Project, dates: Sequence[str]) -> dict[str, str]:
    """Visit labels exactly as the site shows them: story.yaml's label, else 'June 2025'
    ('June 14, 2025' when the vantage has two visits that month)."""
    labels = {n.date.isoformat(): n.label for n in project.story.captures if n.label}
    months = Counter(d[:7] for d in dates)
    return {d: labels.get(d) or date_label(d, day=months[d[:7]] > 1) for d in dates}


# --------------------------------------------------------------------------- #
# Typography
# --------------------------------------------------------------------------- #


@cache
def _bundled() -> dict[str, dict]:
    return json.loads((RUNTIME_DIR / "fonts" / "fonts.json").read_text(encoding="utf-8"))


def _font_file(brand: BrandKit, spec: FontSpec) -> Path | None:
    """The upright face of a brand font: client files first, then the bundled font."""
    files = [p for f in spec.files if (p := brand.resolve(f)) and p.is_file()]
    if files:
        return next((p for p in files if "italic" not in p.name.lower()), files[0])
    entry = _bundled().get(spec.bundled or "")
    if entry:
        faces = sorted(entry["faces"], key=lambda f: f.get("style") != "normal")
        return RUNTIME_DIR / "fonts" / faces[0]["file"]
    log.debug(f"no font file for {spec.family!r}; using Pillow's default face")
    return None


@cache
def _font(path: Path | None, px: int, weight: int) -> Font:
    if path is None:
        return ImageFont.load_default(px)
    font = ImageFont.truetype(str(path), px)
    try:
        axes = font.get_variation_axes()
    except OSError:  # a static face
        return font
    values = []
    for axis in axes:
        name = bytes(axis["name"]).lower()
        value = weight if b"weight" in name else px if b"optical" in name else axis["default"]
        values.append(min(max(value, axis["minimum"]), axis["maximum"]))
    font.set_variation_by_axes(values)
    return font


@dataclass(frozen=True)
class _Style:
    role: Role
    size: float  # in units of 1/1080 of the frame's short edge
    weight: int = 400
    color: Literal["paper", "accent"] = "paper"
    alpha: float = 1.0
    tracking: float = 0.0  # em
    upper: bool = False
    leading: float = 1.2


class _Type:
    """Brand fonts and colors, sized for one frame."""

    def __init__(self, brand: BrandKit, unit: float) -> None:
        typo = brand.typography
        self.files: dict[Role, Path | None] = {
            "display": _font_file(brand, typo.display),
            "text": _font_file(brand, typo.text),
            "numeric": _font_file(brand, typo.numeric or typo.text),
        }
        self.unit = unit
        self.colors = {"paper": _hex(brand.colors.paper), "accent": _hex(brand.colors.accent)}

    def font(self, s: _Style, scale: float = 1.0) -> Font:
        return _font(self.files[s.role], max(6, round(s.size * self.unit * scale)), s.weight)

    def fill(self, s: _Style) -> tuple[int, int, int, int]:
        return (*self.colors[s.color], round(255 * s.alpha))

    def width(self, text: str, s: _Style, scale: float = 1.0) -> float:
        font = self.font(s, scale)
        text = text.upper() if s.upper else text
        if not s.tracking:
            return font.getlength(text)
        track = s.tracking * s.size * self.unit * scale
        return sum(font.getlength(c) for c in text) + track * (len(text) - 1)

    def wrap(self, text: str, s: _Style, max_width: float, scale: float = 1.0) -> list[str]:
        lines: list[str] = []
        for word in text.split():
            if lines and self.width(f"{lines[-1]} {word}", s, scale) <= max_width:
                lines[-1] += f" {word}"
            else:
                lines.append(word)
        return lines

    def draw(
        self,
        draw: ImageDraw.ImageDraw,
        x: float,
        baseline: float,
        text: str,
        s: _Style,
        *,
        scale: float = 1.0,
    ) -> None:
        """Draw `text` with its left end at x on the given baseline (tracking draws glyph by glyph)."""
        font, fill = self.font(s, scale), self.fill(s)
        text = text.upper() if s.upper else text
        if not s.tracking:
            draw.text((x, baseline), text, font=font, fill=fill, anchor="ls")
            return
        track = s.tracking * s.size * self.unit * scale
        for c in text:
            draw.text((x, baseline), c, font=font, fill=fill, anchor="ls")
            x += font.getlength(c) + track


@dataclass(frozen=True)
class _Layer:
    """A pre-rendered RGBA patch (premultiplied BGR float32 + alpha), composited at (x, y)."""

    x: int
    y: int
    rgb: np.ndarray
    alpha: np.ndarray

    @classmethod
    def of(cls, canvas: Image.Image, *, shadow: float = 0.0, blur: float = 0.0) -> _Layer:
        """Crop a frame-sized RGBA canvas to its ink, optionally under a soft dark shadow for legibility."""
        bbox = canvas.getbbox() or (0, 0, 1, 1)
        pad = math.ceil(3 * blur) + 1 if shadow else 0
        x0, y0 = max(bbox[0] - pad, 0), max(bbox[1] - pad, 0)
        x1, y1 = min(bbox[2] + pad, canvas.width), min(bbox[3] + pad, canvas.height)
        rgba = np.asarray(canvas.crop((x0, y0, x1, y1)), dtype=np.float32) / 255
        alpha = rgba[..., 3:]
        rgb = rgba[..., 2::-1] * alpha * 255  # RGB → BGR, premultiplied
        if shadow:
            halo = cv2.GaussianBlur(alpha[..., 0], (0, 0), blur)[..., None] * shadow
            alpha = alpha + halo * (1 - alpha)  # black halo: adds coverage, no color
        return cls(x0, y0, np.ascontiguousarray(rgb), np.ascontiguousarray(alpha))

    def over(self, frame: np.ndarray, opacity: float = 1.0) -> None:
        if opacity <= 0.002:
            return
        h, w = self.alpha.shape[:2]
        region = frame[self.y : self.y + h, self.x : self.x + w]
        region[:] = region * (1 - self.alpha * opacity) + self.rgb * opacity + 0.5


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Shot:
    date: str
    label: str
    note: str | None
    path: Path

    @property
    def day(self) -> int:
        return dt.date.fromisoformat(self.date).toordinal()


@dataclass
class _Section:
    """One vantage's captures. Times are film seconds; they and the layers are set per cut."""

    name: str
    shots: list[_Shot]
    focus: tuple[float, float]
    start: float = 0.0  # first capture fully on screen
    length: float = 0.0  # n·hold + (n−1)·fade
    lead: float = 0.0  # dissolve in from the previous card (the camera already moves)
    tail: float = 0.0  # dissolve out into the next card
    days: list[int] = field(default_factory=list)
    thirds: list[tuple[_Layer, float]] = field(default_factory=list)  # lower third, rule baseline y
    rail: _Layer | None = None


@dataclass
class _Card:
    start: float
    duration: float
    layer: _Layer
    prev: _Section | None = None
    next: _Section | None = None
    dissolve: float = 0.0


def _sections(project: Project, index: MastersIndex) -> list[_Section]:
    """Main vantage (the first scrub chapter's, else the first) first, then every other with ≥ 2 visits."""
    story = {v.id: v for v in project.story.vantages}
    order = [vid for vid in story if vid in index.vantages] + [v for v in index.vantages if v not in story]
    scrub = [ch.vantage for ch in project.story.chapters if isinstance(ch, ScrubChapter)]
    main = next((vid for vid in scrub if vid in index.vantages), order[0] if order else None)
    excluded = {n.date.isoformat() for n in project.story.captures if n.exclude}
    notes = {n.date.isoformat(): n.note for n in project.story.captures}
    sections: list[_Section] = []
    for vid in sorted(order, key=lambda v: v != main):
        mv = index.vantages[vid]
        captures = sorted((c for c in mv.captures if c.date not in excluded), key=lambda c: c.date)
        labels = capture_labels(project, [c.date for c in captures])
        shots = [
            _Shot(c.date, labels[c.date], notes.get(c.date), project.masters_dir / c.file) for c in captures
        ]
        vantage = story.get(vid)
        focus = vantage.portrait_focus if vantage and vantage.portrait_focus else (0.5, 0.5)
        if shots and (not sections or len(shots) > 1):
            sections.append(_Section(vantage.name if vantage else mv.name, shots, (focus[0], focus[1])))
    if not sections:
        raise FileNotFoundError(f"no masters to film in {project.masters_dir}; run `vantage process` first")
    return sections


def _span(shots: Sequence[_Shot]) -> str:
    first, last = shots[0].label, shots[-1].label
    return first if first == last else f"{first} – {last}"


# --------------------------------------------------------------------------- #
# The film
# --------------------------------------------------------------------------- #

KICKER = _Style("numeric", 22, 600, "accent", tracking=0.22, upper=True)
TITLE = _Style("display", 96, 400, leading=1.08)
SECTION_TITLE = _Style("display", 72, 400, leading=1.08)
SUBTITLE = _Style("text", 30, 400, alpha=0.72, leading=1.35)
SPAN = _Style("numeric", 19, 500, alpha=0.6, tracking=0.16, upper=True)
DATE = _Style("numeric", 40, 600, tracking=0.1, upper=True)
NOTE = _Style("text", 26, 400, alpha=0.88)
TAG = _Style("numeric", 17, 600, alpha=0.85, tracking=0.16, upper=True)
BRAND = _Style("display", 56, 400, leading=1.1)
CREDIT = _Style("text", 28, 400, alpha=0.9, leading=1.35)
ROLE = _Style("numeric", 16, 600, alpha=0.6, tracking=0.16, upper=True)
PARTNER = _Style("text", 28, 500)
LINK = _Style("text", 26, 500, "accent")
FINE = _Style("text", 19, 400, alpha=0.6, leading=1.4)

_Block = tuple[str | None, _Style, float]  # text (None = accent rule), style, gap above (units)


class _Film:
    """One cut (16:9 or 9:16): its layout, its timeline and a pure frame(t) function."""

    def __init__(
        self, project: Project, sections: Sequence[_Section], width: int, height: int, *, portrait: bool
    ):
        self.project, self.width, self.height, self.portrait = project, width, height, portrait
        self.sections = [replace(s, days=[shot.day for shot in s.shots]) for s in sections]
        settings = project.config.output.film
        self.fps, self.hold, self.fade = settings.fps, settings.hold_s, settings.fade_s
        self.unit = u = min(width, height) / 1080
        self.type = _Type(project.brand, u)
        colors = project.brand.colors
        night = np.array(_hex(colors.night)[::-1], np.float32)
        paper = np.array(_hex(colors.paper)[::-1], np.float32)
        self.accent = _hex(colors.accent)[::-1]
        self.margin = round((0.075 if portrait else 0.055) * width)
        self.rail_y = height - round((0.13 if portrait else 0.075) * height)
        # Cards: brand night with a faint central glow.
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        glow = np.exp(-(((xx - width / 2) / (0.6 * width)) ** 2 + ((yy - height / 2) / (0.6 * height)) ** 2))
        self.card_bg = (night + (paper - night) * 0.05 * glow[..., None] + 0.5).astype(np.uint8)
        # Bottom scrim behind the lower third and rail, as uint8 maps for cv2 (multiply, then add).
        rows = round((0.36 if portrait else 0.42) * height)
        a = np.array([0.62 * _ease(i / rows) for i in range(rows)], np.float32)[:, None, None]
        self.scrim_top = height - rows
        self.scrim_keep = np.ascontiguousarray(
            np.broadcast_to((1 - a) * 255 + 0.5, (rows, width, 3)), np.uint8
        )
        self.scrim_add = np.ascontiguousarray(np.broadcast_to(night * a + 0.5, (rows, width, 3)), np.uint8)
        self.tag = self._tag()
        self.cards: list[_Card] = []
        self._bases: dict[Path, np.ndarray] = {}
        self._layout()

    # ----------------------------------------------------------- layout --- #

    def _canvas(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        return canvas, ImageDraw.Draw(canvas)

    def _stack(self, blocks: Sequence[_Block]) -> _Layer:
        """Centered blocks (wrapped to 80 % of the width), shrunk to fit 86 % of the height."""
        t, u, max_w = self.type, self.unit, 0.8 * self.width
        scale = 1.0
        for _ in range(3):
            lines: list[tuple[str | None, _Style, float]] = []  # text (None = rule), style, baseline
            y = 0.0
            for text, style, gap in blocks:
                y += gap * u * scale
                if text is None:
                    lines.append((None, style, y))
                    y += 3 * u * scale
                    continue
                ascent, descent = t.font(style, scale).getmetrics()
                for i, line in enumerate(t.wrap(text, style, max_w, scale)):
                    y += ascent if i == 0 else style.size * u * scale * style.leading
                    lines.append((line, style, y))
                y += descent
            if y <= 0.86 * self.height:
                break
            scale *= 0.86 * self.height / y
        canvas, draw = self._canvas()
        top = (self.height - y) / 2
        for text, style, base in lines:
            if text is None:
                x0, y0 = self.width / 2 - 22 * u * scale, top + base
                draw.rectangle((x0, y0, x0 + 44 * u * scale, y0 + 3 * u * scale), fill=t.colors["accent"])
            else:
                t.draw(
                    draw, (self.width - t.width(text, style, scale)) / 2, top + base, text, style, scale=scale
                )
        return _Layer.of(canvas)

    def _tag(self) -> _Layer | None:
        """'Simulated imagery' / 'Preview' in the top corner of every capture: films travel without context."""
        cfg = self.project.config
        words = [w for w, on in (("Simulated imagery", cfg.simulated), ("Preview", cfg.draft)) if on]
        if not words:
            return None
        canvas, draw = self._canvas()
        ascent, _ = self.type.font(TAG).getmetrics()
        self.type.draw(draw, self.margin, 0.8 * self.margin + ascent, " · ".join(words), TAG)
        return _Layer.of(canvas, shadow=0.6, blur=3 * self.unit)

    def _third(self, shot: _Shot) -> tuple[_Layer, float]:
        """Date label (+ note) above the rail, and the baseline of the accent rule animated above it."""
        canvas, draw = self._canvas()
        t, u = self.type, self.unit
        base = self.rail_y - 34 * u
        if shot.note:
            for line in reversed(t.wrap(shot.note, NOTE, 0.7 * self.width)):
                t.draw(draw, self.margin, base, line, NOTE)
                base -= NOTE.size * u * 1.3
            base -= 10 * u
        t.draw(draw, self.margin, base, shot.label, DATE)
        cap = -t.font(DATE).getbbox("H", anchor="ls")[1]
        return _Layer.of(canvas, shadow=0.55, blur=4 * u), base - cap - 16 * u

    def _rail_x(self, section: _Section, position: float) -> float:
        """x of a fractional capture index on the rail, proportional to real time."""
        days = section.days
        i = min(int(position), len(days) - 1)
        day = days[i] + (days[min(i + 1, len(days) - 1)] - days[i]) * (position - i)
        x0, x1, span = self.margin, self.width - self.margin, days[-1] - days[0]
        return x1 if span == 0 else x0 + (x1 - x0) * (day - days[0]) / span

    def _rail(self, section: _Section) -> _Layer:
        canvas, draw = self._canvas()
        u, y, paper = self.unit, self.rail_y, self.type.colors["paper"]
        draw.rectangle((self.margin, y - u, self.width - self.margin, y + u), fill=(*paper, 90))
        for i in range(len(section.shots)):
            x = self._rail_x(section, i)
            draw.rectangle((x - u, y - 6 * u, x + u, y + 6 * u), fill=(*paper, 150))
        return _Layer.of(canvas)

    def _layout(self) -> None:
        """Typeset every card and capture, and lay the timeline out."""
        cfg, brand, settings = self.project.config, self.project.brand, self.project.config.output.film
        first, last = self.sections[0], self.sections[-1]
        place = cfg.location.name if cfg.location else None
        years = "–".join(dict.fromkeys((first.shots[0].date[:4], first.shots[-1].date[:4])))
        title: list[_Block] = [
            (cfg.kicker or " · ".join(filter(None, (place, years))), KICKER, 0),
            (None, KICKER, 26),
            (cfg.title, TITLE, 34),
        ]
        if cfg.subtitle:
            title.append((cfg.subtitle, SUBTITLE, 20))
        title.append((_span(first.shots), SPAN, 34))

        end: list[_Block] = [(brand.name, BRAND, 0)]
        if brand.credit_line:
            end.append((brand.credit_line, CREDIT, 18))
        for i, partner in enumerate(brand.partners):
            end += [(partner.role, ROLE, 22 if i else 40), (partner.name, PARTNER, 8)]
        if cfg.output.base_url:
            end.append((cfg.output.base_url.split("://")[-1].rstrip("/"), LINK, 40))
        fine = filter(None, (brand.copyright, brand.disclaimer, SIMULATED_NOTE if cfg.simulated else None))
        end += [(text, FINE, 8 if i else 40) for i, text in enumerate(fine)]

        t = settings.title_card_s
        self.cards.append(_Card(0.0, t, self._stack(title), next=first, dissolve=min(self.fade, t / 3)))
        first.lead = self.cards[0].dissolve
        for k, section in enumerate(self.sections):
            section.thirds = [self._third(shot) for shot in section.shots]
            section.rail = self._rail(section)
            if k:
                d = settings.title_card_s
                blocks: list[_Block] = [(_span(section.shots), KICKER, 0), (None, KICKER, 26)]
                card = _Card(t, d, self._stack([*blocks, (section.name, SECTION_TITLE, 34)]))
                card.prev, card.next, card.dissolve = self.sections[k - 1], section, min(self.fade, d / 3)
                card.prev.tail = section.lead = card.dissolve
                self.cards.append(card)
                t += d
            n = len(section.shots)
            section.start, section.length = t, n * self.hold + (n - 1) * self.fade
            t += section.length
        d = settings.end_card_s
        self.cards.append(_Card(t, d, self._stack(end), prev=last, dissolve=min(self.fade, d / 3)))
        last.tail = self.cards[-1].dissolve
        self.total = t + d
        self.poster_t = first.start + (len(first.shots) - 1) * (self.hold + self.fade) + 0.85 * self.hold

    # ----------------------------------------------------------- frames --- #

    def _base(self, path: Path) -> np.ndarray:
        """The master resized so that the end of the push samples it 1:1 (three kept at a time)."""
        if path not in self._bases:
            img = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if img is None:
                raise FileNotFoundError(f"cannot read master {path}")
            mh, mw = img.shape[:2]
            s = max(self.width / mw, self.height / mh) * (1 + PUSH)
            size = (max(1, round(mw * s)), max(1, round(mh * s)))
            if len(self._bases) >= 3:
                del self._bases[next(iter(self._bases))]
            self._bases[path] = cv2.resize(
                img, size, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC
            )
        return self._bases[path]

    def _warp(self, path: Path, zoom: float, cx: float, cy: float) -> np.ndarray:
        """Cover-fit the base at `zoom`, centred on normalized (cx, cy) but never past its edges."""
        base = self._base(path)
        bh, bw = base.shape[:2]
        k = max(self.width / bw, self.height / bh) * zoom
        hw, hh = self.width / (2 * k), self.height / (2 * k)
        x, y = min(max(cx * bw, hw), bw - hw), min(max(cy * bh, hh), bh - hh)
        # Pixel centres: out + 0.5 = k·(src + 0.5 − x) + W/2.
        m = np.array(
            [[k, 0, self.width / 2 - 0.5 + k * (0.5 - x)], [0, k, self.height / 2 - 0.5 + k * (0.5 - y)]]
        )
        size = (self.width, self.height)
        return cv2.warpAffine(base, m, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    def _section_frame(self, section: _Section, local: float) -> np.ndarray:
        """Section time `local` (may run into the dissolves on either side)."""
        n, step = len(section.shots), self.hold + self.fade
        i = min(max(math.floor(local / step), 0), n - 1)
        into = local - i * step
        w = _ease((into - self.hold) / self.fade) if i < n - 1 and into > self.hold else 0.0
        u = _ease((local + section.lead) / (section.lead + section.length + section.tail))
        cx, cy = (section.focus[0] + PAN * (2 * u - 1), section.focus[1]) if self.portrait else (0.5, 0.5)
        zoom = 1 + PUSH * u
        frame = self._warp(section.shots[i].path, zoom, cx, cy)
        if w > 0:
            frame = _mix(frame, self._warp(section.shots[i + 1].path, zoom, cx, cy), w)
        band = frame[self.scrim_top :]
        band[:] = cv2.add(cv2.multiply(band, self.scrim_keep, scale=1 / 255), self.scrim_add)
        for j in range(i, min(i + 2, n)):
            self._lower_third(frame, section, j, local)
        assert section.rail is not None
        section.rail.over(frame)
        self._progress(frame, section, i + w)
        if self.tag:
            self.tag.over(frame)
        return frame

    def _lower_third(self, frame: np.ndarray, section: _Section, j: int, local: float) -> None:
        """The rule grows, then the label fades in from mid-crossfade; both leave over the next crossfade's first half."""
        step, half = self.hold + self.fade, self.fade / 2
        start = j * step - half if j else 0.0
        end = j * step + self.hold if j < len(section.shots) - 1 else math.inf
        grow = min(0.45, 0.4 * step)
        out = 1 - _ease((local - end) / half) if half else float(local < end)
        layer, rule_y = section.thirds[j]
        layer.over(frame, _ease((local - start - 0.12) / grow) * out)
        x0, u = self.margin, self.unit
        self._bar(frame, x0, rule_y - 4 * u, x0 + 56 * u * _ease((local - start) / grow), rule_y, out)

    def _bar(self, frame: np.ndarray, x0: float, y0: float, x1: float, y1: float, opacity: float) -> None:
        """Anti-aliased, sub-pixel accent rectangle."""
        if x1 - x0 < 0.25 or opacity <= 0.002:
            return
        r0, r1 = max(int(y0) - 1, 0), min(math.ceil(y1) + 1, self.height)
        c0, c1 = max(int(x0) - 1, 0), min(math.ceil(x1) + 1, self.width)
        region = frame[r0:r1, c0:c1]
        ink = region.copy()
        pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]) - (c0, r0)
        cv2.fillConvexPoly(ink, np.round(pts * 16).astype(np.int32), self.accent, cv2.LINE_AA, 4)
        region[:] = ink if opacity >= 0.998 else cv2.addWeighted(ink, opacity, region, 1 - opacity, 0)

    def _progress(self, frame: np.ndarray, section: _Section, position: float) -> None:
        """Accent fill up to the current date, lit ticks for the visits passed, and a marker dot."""
        u, y = self.unit, self.rail_y
        x = self._rail_x(section, position)
        self._bar(frame, self.margin, y - u, x, y + u, 1.0)
        for i in range(min(int(position + 1e-6), len(section.shots) - 1) + 1):
            xi = self._rail_x(section, i)
            self._bar(frame, xi - u, y - 6 * u, xi + u, y + 6 * u, 1.0)
        cv2.circle(frame, (round(x * 16), round(y * 16)), round(5 * u * 16), self.accent, -1, cv2.LINE_AA, 4)

    def _card_frame(self, card: _Card, t: float) -> np.ndarray:
        """Text fades in once the imagery has gone and out before it returns (or back to night at the end)."""
        local, d = t - card.start, card.duration
        delay = 0.75 * card.dissolve if card.prev else min(0.2, d / 10)
        fade = min(0.9, d / 3)
        clear = d - card.dissolve if card.next else d  # text gone by then
        out = min(0.5, d / 6)
        opacity = _ease((local - delay) / fade) * (1 - _ease((local - clear + out) / out))
        frame = self.card_bg.copy()
        card.layer.over(frame, opacity)
        # Dissolves to and from night read best in display gamma (a dip, like a film fade).
        if card.prev and local < card.dissolve:
            before = self._section_frame(card.prev, t - card.prev.start)
            frame = cv2.addWeighted(
                before, 1 - _ease(local / card.dissolve), frame, _ease(local / card.dissolve), 0
            )
        if card.next and local > clear:
            after = self._section_frame(card.next, t - card.next.start)
            w = _ease((local - clear) / card.dissolve)
            frame = cv2.addWeighted(frame, 1 - w, after, w, 0)
        return frame

    def frame(self, t: float) -> np.ndarray:
        for card in self.cards:
            if card.start <= t < card.start + card.duration:
                return self._card_frame(card, t)
        for section in self.sections:
            if section.start <= t < section.start + section.length:
                return self._section_frame(section, t - section.start)
        return self._card_frame(self.cards[-1], t)

    def render(self, out: Path, *, crf: int, preset: str) -> int:
        """Encode the film to `out` and its poster (the main vantage's final hold) next to it."""
        frames = max(1, round(self.total * self.fps))
        part = out.with_name(f"{out.stem}.part{out.suffix}")
        try:
            with FrameWriter(
                part, self.width, self.height, fps=self.fps, crf=crf, preset=preset, extra=_ENCODE
            ) as writer:
                for k in range(frames):
                    writer.write(self.frame(k / self.fps))
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        part.replace(out)
        params = [cv2.IMWRITE_JPEG_QUALITY, POSTER_QUALITY, cv2.IMWRITE_JPEG_PROGRESSIVE, 1]
        cv2.imwrite(str(out.with_suffix(".jpg")), self.frame(self.poster_t), params)
        return frames


def render_film(project: Project, out_dir: Path) -> list[Path]:
    """Render <slug>-16x9.mp4 (and <slug>-9x16.mp4 when output.film.vertical) with JPEG posters."""
    index_path = project.masters_dir / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"no {index_path}; run `vantage process {project.slug}` first")
    sections = _sections(project, MastersIndex.load(index_path))
    settings, fast = project.config.output.film, _fast()

    def fit(w: float, h: float) -> tuple[int, int]:
        s = min(1.0, FAST_LONG_EDGE / max(w, h)) if fast else 1.0
        return _even(w * s), _even(h * s)

    short = min(settings.width, settings.height)
    cuts = [("16x9", fit(settings.width, settings.height), sections, False)]
    if settings.vertical:
        cuts.append(("9x16", fit(short, short * 16 / 9), sections[:1], True))
    else:
        for stale in out_dir.glob(f"{project.slug}-9x16.*"):
            stale.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    films = []
    for name, (w, h), secs, portrait in cuts:
        film = _Film(project, secs, w, h, portrait=portrait)
        out = out_dir / f"{project.slug}-{name}.mp4"
        frames = film.render(out, crf=20 if fast else 18, preset="veryfast" if fast else "slow")
        log.info(f"{out.name}: {w}×{h}, {frames} frames ({frames / film.fps:.1f}s), {len(secs)} section(s)")
        films.append(out)
    return films
