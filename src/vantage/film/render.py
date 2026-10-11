"""MP4 time-lapse films rendered from their plans: every visit of a vantage as one slowly pushing move.

    <out>/<slug>-<id>.mp4               the film ('16x9': output.film width × height; '9x16': phones)
    <out>/<slug>-<id>-web.mp4           the same frames under a VBV cap (≤ 25 MiB), from the same pipe
    <out>/<slug>-<id>.jpg               poster
    <out>/<slug>-<id>.plan.json         its FilmPlan (film/plan.py) and the render record
    <out>/<slug>-<id>.storyboard.jpg    one tile per key frame (film/storyboard.py)
    <out>/<slug>-<id>.vtt .srt .chapters.vtt .txt   captions, chapters, transcript (film/tracks.py)
    <out>/usb/<slug>-<id>-<H>h.mp4      a loop film repeated for H hours (film/loop.py; never packaged)

A film is a title card, one section per vantage (the main vantage first; the
vertical cut keeps only the main one) and an end card. Inside a section each
capture holds `hold_s` and crossfades `fade_s` into the next (blended in linear
light) under an eased push that spans the whole section, with a date lower-third
and a calendar rail whose ticks sit at the real flight dates. Cards dissolve into
and out of the imagery while the camera keeps moving.

Story plans are rendered by `film/cut.py` (`_Cut`); `renderer` picks the one for a plan's cut, and
every film goes through `encode_frames` and the luminance meter of `film/safety.py`; then
`tracks.embed` remuxes its MP4s by stream copy to add the captions and chapters of the plan.
`render_film` renders several films side by side in worker processes (`film_jobs`;
VANTAGE_FILM_JOBS=1 renders them one after another, with the same bytes) and renames their
*.part outputs only once every film has succeeded.

`_Film` evaluates a classic plan: the timeline, card text, layout (film/layout.py: classic-16x9 keeps
the original numbers, classic-9x16 the story safe zone) and inputs all come from the plan, so a plan
read back from plan.json renders the same bytes. Both renderers record the ink box of every typeset
layer (`ink`); `check_ink` writes them to plan.json `checks` and turns any box outside the safe zone
into a plan warning (an error with --release). Frames are composed with numpy/OpenCV and
piped to ffmpeg (media.ffmpeg.FrameWriter). Text is typeset once per card and capture with Pillow in
the brand fonts and alpha-composited, so a frame costs one warp, the occasional blend and a few small
composites. VANTAGE_FAST=1 caps the long edge at 960 px and encodes with a fast preset.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import math
import multiprocessing
import os
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal, Protocol

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vantage import log
from vantage.config import Project
from vantage.film.layout import Layout, outside
from vantage.film.loop import MAX_HOURS, USB_DIR, concat_hours
from vantage.film.plan import (
    PLAN_SUFFIX,
    ROLES,
    SIMULATED_NOTE,
    STORYBOARD_SUFFIX,
    film_id,
    font_path,
    plan_films,
    resolve_films,
)
from vantage.film.plan import capture_labels as capture_labels
from vantage.film.safety import LumaMeter, check_plan
from vantage.film.storyboard import write_storyboard
from vantage.film.tracks import SUFFIXES, embed, track_texts
from vantage.media.ffmpeg import ENCODER_THREADS, FrameWriter, ffmpeg_version
from vantage.models import FilmPlan
from vantage.site.build import ReleaseError
from vantage.site.facts import check_release

PUSH = 0.06  # Ken-Burns zoom across a section (1.00 → 1.06)
PAN = 0.05  # vertical cut: drift either side of the portrait focus, in image widths
POSTER_QUALITY = 90
INK_TOLERANCE = 1  # px of antialiasing an ink box may spill past the safe zone
__all__ = ["SIMULATED_NOTE", "capture_labels", "check_ink", "check_seam", "render_film"]
# BT.709 tags and matrix (players guess wrong otherwise), no timestamps → reproducible bytes,
# and an explicit container (films are written to <name>.mp4.part, then renamed).
_ENCODE = (
    "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1", "-f", "mp4",
)  # fmt: skip
_SQUARE = ((np.arange(256, dtype=np.float32) / 255) ** 2).reshape(1, 256)  # gamma-2 "linear light"

Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
Role = Literal["display", "text", "numeric"]


def _ease(u: float) -> float:
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


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


# --------------------------------------------------------------------------- #
# Typography
# --------------------------------------------------------------------------- #


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
    color: Literal["paper", "accent", "ink"] = "paper"
    alpha: float = 1.0
    tracking: float = 0.0  # em
    upper: bool = False
    leading: float = 1.2


class _Type:
    """Brand fonts and colors (as a plan names them), sized for one frame."""

    def __init__(self, files: dict[Role, Path | None], colors: dict[str, str], unit: float) -> None:
        self.files = files
        self.unit = unit
        self.colors = {k: _hex(colors[k]) for k in ("paper", "accent", "ink") if k in colors}

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

    def wrap(
        self, text: str, s: _Style, max_width: float, scale: float = 1.0, *, balanced: bool = False
    ) -> list[str]:
        """Words filled onto lines of at most max_width. `balanced` keeps the greedy line count but
        evens the lines out (`balance`): no one-word last line, no line ending on a word that
        starts a sentence, when another break avoids it."""
        lines: list[str] = []
        for word in text.split():
            if lines and self.width(f"{lines[-1]} {word}", s, scale) <= max_width:
                lines[-1] += f" {word}"
            else:
                lines.append(word)
        if not balanced or len(lines) < 2:
            return lines
        words = text.split()
        rows = balance(words, len(lines), lambda a, b: self.width(" ".join(words[a:b]), s, scale), max_width)
        return [" ".join(words[a:b]) for a, b in rows] if rows else lines

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


def balance(
    words: Sequence[str], n: int, width: Callable[[int, int], float], max_width: float, limit: int = 20000
) -> list[tuple[int, int]] | None:
    """The best way to break `words` into n lines no wider than max_width, as [(start, end)] word
    index ranges (None when there is none, or too many to try). Best: no one-word last line, no line
    ending on a word that starts a sentence (after '.', '!' or '?'), then the narrowest widest line,
    then the most even lines."""
    from itertools import combinations

    k = len(words)
    if n < 2 or n > k or math.comb(k - 1, n - 1) > limit:
        return None
    best: tuple[tuple[int, float, float], list[tuple[int, int]]] | None = None
    for cut in combinations(range(1, k), n - 1):
        edges = [0, *cut, k]
        rows = list(itertools.pairwise(edges))
        widths = [width(a, b) for a, b in rows]
        if max(widths) > max_width:
            continue
        bad = int(rows[-1][1] - rows[-1][0] == 1)
        bad += sum(1 for a, b in rows[:-1] if b - a > 1 and words[b - 2].endswith((".", "!", "?")))
        mean = sum(widths) / n
        cost = (bad, round(max(widths), 3), round(sum((w - mean) ** 2 for w in widths), 3))
        if best is None or cost < best[0]:
            best = (cost, rows)
    return best[1] if best else None


def ink_box(x0: float, y0: float, x1: float, y1: float) -> list[int]:
    """[x, y, w, h] of the pixels a (x0, y0, x1, y1) box touches."""
    a, b = math.floor(x0), math.floor(y0)
    return [a, b, math.ceil(x1) - a, math.ceil(y1) - b]


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
    """One vantage's captures. Times are film seconds (from the plan); the layers are set per cut."""

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
STYLES = {
    name: style for name, style in globals().items() if isinstance(style, _Style) and name.isupper()
}  # plan blocks name their style: "KICKER", "TITLE", …


class _Film:
    """One classic cut (16:9 or 9:16) evaluated from its plan: layout, timeline and a pure frame(t)."""

    def __init__(self, project: Project, plan: FilmPlan, layout: Layout | None = None):
        p = plan.plan
        if p["cut"] != "classic":
            raise ValueError(f"film {plan.id}: no renderer for cut {p['cut']!r}")
        width, height = p["width"], p["height"]
        self.project, self.plan, self.width, self.height = project, plan, width, height
        self.portrait = height > width
        self.fps, self.hold, self.fade = p["fps"], p["hold_s"], p["fade_s"]
        self.lay = lay = layout or Layout.from_record(width, height, p["layout"])
        assert lay.scrim is not None and lay.rail_x is not None, "a classic layout"
        self.unit = u = lay.unit
        self.ink: list[dict] = []
        inputs = p["inputs"]
        files = {role: font_path(inputs["fonts"][role]["file"], project.brand) for role in ROLES}
        colors = inputs["colors"]
        self.type = _Type(files, colors, u)
        night = np.array(_hex(colors["night"])[::-1], np.float32)
        paper = np.array(_hex(colors["paper"])[::-1], np.float32)
        self.accent = _hex(colors["accent"])[::-1]
        self.margin, self.rail_y, self.rail_ends = lay.margin, lay.rail_y, lay.rail_x
        # Cards: brand night with a faint central glow.
        yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
        glow = np.exp(-(((xx - width / 2) / (0.6 * width)) ** 2 + ((yy - height / 2) / (0.6 * height)) ** 2))
        self.card_bg = (night + (paper - night) * 0.05 * glow[..., None] + 0.5).astype(np.uint8)
        # Bottom scrim behind the lower third and rail, as uint8 maps for cv2 (multiply, then add):
        # it rises over `ramp` rows from the layout's first scrim row, then holds to the bottom.
        self.scrim_top, ramp = lay.scrim
        rows = height - self.scrim_top
        a = np.array([0.62 * _ease(i / ramp) for i in range(rows)], np.float32)[:, None, None]
        self.scrim_keep = np.ascontiguousarray(
            np.broadcast_to((1 - a) * 255 + 0.5, (rows, width, 3)), np.uint8
        )
        self.scrim_add = np.ascontiguousarray(np.broadcast_to(night * a + 0.5, (rows, width, 3)), np.uint8)
        self.tag = self._tag(p["tag"])
        self.sections: list[_Section] = []
        self.cards: list[_Card] = []
        self._bases: dict[Path, np.ndarray] = {}
        self._layout(p["shots"])
        self.total, self.poster_t, self.frames = p["seconds_total"], p["poster_seconds"], p["frames"]

    # ----------------------------------------------------------- layout --- #

    def _canvas(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        return canvas, ImageDraw.Draw(canvas)

    def _stack(self, blocks: Sequence[_Block], sid: str | None = None) -> _Layer:
        """Centered blocks (wrapped to 80 % of the width), shrunk to fit 86 % of the height (classic
        9:16: of the safe zone, centred in it)."""
        lay = self.lay
        legacy = lay.name == "classic-16x9"  # today's numbers, byte for byte
        t, u = self.type, self.unit
        max_w = 0.8 * self.width if legacy else min(0.8 * self.width, lay.text_width)
        room = 0.86 * self.height if legacy else min(0.86 * self.height, lay.bottom - lay.top)
        scale = 1.0
        for attempt in range(3):
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
            if y <= room or attempt == 2:
                break
            scale *= room / y
        canvas, draw = self._canvas()
        top = (self.height - y) / 2 if legacy else (lay.top + lay.bottom) / 2 - y / 2
        cx = self.width / 2 if legacy else (lay.margin + lay.right) / 2
        for text, style, base in lines:
            if text is None:
                x0, y0 = cx - 22 * u * scale, top + base
                draw.rectangle((x0, y0, x0 + 44 * u * scale, y0 + 3 * u * scale), fill=t.colors["accent"])
            elif legacy:
                t.draw(
                    draw, (self.width - t.width(text, style, scale)) / 2, top + base, text, style, scale=scale
                )
            else:
                t.draw(draw, cx - t.width(text, style, scale) / 2, top + base, text, style, scale=scale)
        self._note(sid, "stack", canvas.getbbox())
        return _Layer.of(canvas)

    def _note(self, sid: str | None, layer: str, bbox: Sequence[float] | None) -> None:
        """Record the ink box of a typeset layer (x0, y0, x1, y1) for plan.json `checks.ink`."""
        if bbox:
            self.ink.append({"shot": sid, "layer": layer, "box": ink_box(*bbox)})

    def _tag(self, words: Sequence[str]) -> _Layer | None:
        """'Simulated imagery' / 'Preview' in the top corner of every capture: films travel without context."""
        if not words:
            return None
        canvas, draw = self._canvas()
        ascent, _ = self.type.font(TAG).getmetrics()
        top = 0.8 * self.margin if self.lay.tag == "corner" else self.lay.top
        self.type.draw(draw, self.margin, top + ascent, " · ".join(words), TAG)
        self._note(None, "tag", canvas.getbbox())
        return _Layer.of(canvas, shadow=0.6, blur=3 * self.unit)

    def _third(self, shot: _Shot, sid: str) -> tuple[_Layer, float]:
        """Date label (+ note) above the rail, and the baseline of the accent rule animated above it."""
        canvas, draw = self._canvas()
        t, u = self.type, self.unit
        base = self.rail_y - 34 * u
        if shot.note:
            for line in reversed(t.wrap(shot.note, NOTE, min(0.7 * self.width, self.lay.text_width))):
                t.draw(draw, self.margin, base, line, NOTE)
                base -= NOTE.size * u * 1.3
            base -= 10 * u
        t.draw(draw, self.margin, base, shot.label, DATE)
        cap = -t.font(DATE).getbbox("H", anchor="ls")[1]
        rule = base - cap - 16 * u
        bbox = canvas.getbbox() or (self.margin, rule, self.margin, rule)
        self._note(sid, f"third {shot.date}", (
            min(bbox[0], self.margin), min(bbox[1], rule - 4 * u),
            max(bbox[2], self.margin + 56 * u), max(bbox[3], rule),
        ))  # fmt: skip
        return _Layer.of(canvas, shadow=0.55, blur=4 * u), rule

    def _rail_x(self, section: _Section, position: float) -> float:
        """x of a fractional capture index on the rail, proportional to real time."""
        days = section.days
        i = min(int(position), len(days) - 1)
        day = days[i] + (days[min(i + 1, len(days) - 1)] - days[i]) * (position - i)
        (x0, x1), span = self.rail_ends, days[-1] - days[0]
        return x1 if span == 0 else x0 + (x1 - x0) * (day - days[0]) / span

    def _rail(self, section: _Section, sid: str) -> _Layer:
        canvas, draw = self._canvas()
        u, y, paper = self.unit, self.rail_y, self.type.colors["paper"]
        (x0, x1), knob = self.rail_ends, 5 * u
        draw.rectangle((x0, y - u, x1, y + u), fill=(*paper, 90))
        for i in range(len(section.shots)):
            x = self._rail_x(section, i)
            draw.rectangle((x - u, y - 6 * u, x + u, y + 6 * u), fill=(*paper, 150))
        self._note(sid, "rail", (x0 - knob, y - 6 * u, x1 + knob, y + 6 * u))  # the knob reaches either end
        return _Layer.of(canvas)

    def _layout(self, shots: Sequence[dict]) -> None:
        """Typeset every card and capture of the plan, and take its timeline."""
        masters = self.project.masters_dir
        by_id: dict[str, _Section] = {}
        for shot in shots:
            if shot["kind"] != "section":
                continue
            caps = [_Shot(c["date"], c["label"], c["note"], masters / c["file"]) for c in shot["shots"]]
            sec = shot["seconds"]
            section = _Section(
                shot["name"], caps, (shot["focus"][0], shot["focus"][1]),
                sec["start"], sec["length"], sec["lead"], sec["tail"], [c.day for c in caps],
            )  # fmt: skip
            section.thirds = [self._third(c, shot["id"]) for c in caps]
            section.rail = self._rail(section, shot["id"])
            by_id[shot["id"]] = section
            self.sections.append(section)
        for shot in shots:
            if shot["kind"] != "card":
                continue
            blocks = [(b["text"], STYLES[b["style"]], b["gap"]) for b in shot["blocks"]]
            sec = shot["seconds"]
            card = _Card(
                sec["start"], sec["duration"], self._stack(blocks, shot["id"]), dissolve=sec["dissolve"]
            )
            card.prev, card.next = by_id.get(shot.get("prev") or ""), by_id.get(shot.get("next") or "")
            self.cards.append(card)

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
        self._bar(frame, self.rail_ends[0], y - u, x, y + u, 1.0)
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

    def frame_at(self, k: int) -> np.ndarray:
        """Frame k of the film (classic plans keep their timeline in seconds)."""
        return self.frame(k / self.fps)

    def poster(self) -> bytes:
        """The poster JPEG: the main vantage's final hold."""
        params = [cv2.IMWRITE_JPEG_QUALITY, POSTER_QUALITY, cv2.IMWRITE_JPEG_PROGRESSIVE, 1]
        ok, data = cv2.imencode(".jpg", self.frame(self.poster_t), params)
        if not ok:
            raise ValueError(f"could not encode the poster of film {self.plan.id}")
        return data.tobytes()


class Renderable(Protocol):
    width: int
    height: int
    fps: int
    frames: int
    lay: Layout
    ink: list[dict]

    def frame_at(self, k: int) -> np.ndarray: ...

    def poster(self) -> bytes: ...


def web_args(web: Mapping[str, Any]) -> list[str]:
    """Output options of the -web.mp4 encode: H.264 High at crf under a VBV cap (maxrate, bufsize),
    faststart and the same BT.709, bitexact settings as the main film."""
    return [
        "-c:v", "libx264", "-profile:v", "high", "-crf", str(web["crf"]), "-preset", web["preset"],
        "-threads", str(web.get("threads", ENCODER_THREADS)),
        "-maxrate", str(web["maxrate"]), "-bufsize", str(web["bufsize"]),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", *_ENCODE,
    ]  # fmt: skip


def loop_args(gop: int) -> list[str]:
    """Output options of a loop film: closed GOPs of exactly `gop` frames from frame 0 (no scene-cut
    keyframes), so each cycle of a repeated file starts on an IDR frame."""
    return [
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-x264-params", "open-gop=0", "-level:v", "4.1",
    ]  # fmt: skip


def encode_frames(
    film: Renderable,
    part: Path,
    *,
    crf: int,
    preset: str,
    meter: LumaMeter | None = None,
    web: tuple[Path, Mapping[str, Any]] | None = None,
    gop: int | None = None,
    threads: int = ENCODER_THREADS,
) -> None:
    """Pipe every frame of a film to ffmpeg (and through the luminance meter, which only reads);
    `web` (path, plan.json encode.web) adds the web encode as a second output of the same pipe;
    `gop` (plan.json encode.gop, loop films) closes every GOP at that many frames. A loop film's
    meter also reads frame N (= frame 0), so the seam is measured like any other cut."""
    extra_outputs = [(web[0], web_args(web[1]))] if web is not None else []
    extra = [*loop_args(gop), *_ENCODE] if gop else list(_ENCODE)
    with FrameWriter(
        part, film.width, film.height, fps=film.fps, crf=crf, preset=preset, extra=extra,
        extra_outputs=extra_outputs, threads=threads,
    ) as writer:  # fmt: skip
        for k in range(film.frames):
            frame = film.frame_at(k)
            if meter is not None:
                meter.add(frame)
            writer.write(frame)
    if gop and meter is not None:
        meter.add(film.frame_at(film.frames))


def check_seam(plan: FilmPlan, film: Renderable) -> None:
    """A loop film's frame N (one past the last) must equal frame 0, byte for byte."""
    if plan.plan.get("loop") and not np.array_equal(film.frame_at(film.frames), film.frame_at(0)):
        raise ValueError(f"film {plan.id}: frame {film.frames} differs from frame 0, so the loop would jump")


def renderer(project: Project, plan: FilmPlan) -> Renderable:
    """The renderer of a plan's cut: `_Film` (classic) or `_Cut` (story)."""
    cut = plan.plan["cut"]
    if cut == "classic":
        return _Film(project, plan)
    if cut == "story":
        from vantage.film.cut import _Cut

        check_plan(plan.plan)
        return _Cut(project, plan)
    raise ValueError(f"film {plan.id}: no renderer for cut {cut!r}")


def check_ink(plan: FilmPlan, film: Renderable) -> list[str]:
    """Write the ink box of every typeset layer to plan.json `checks` ({safe: [x0, y0, x1, y1], ink:
    [{shot, layer, box: [x, y, w, h]}], outside: [indices]}) and return one line per layer that
    leaves the safe zone (more than INK_TOLERANCE px). Each is also a plan warning."""
    rect = list(film.lay.rect)
    bad = [k for k, item in enumerate(film.ink) if outside(item["box"], rect, INK_TOLERANCE)]
    plan.checks = {"safe": rect, "ink": film.ink, "outside": bad}
    problems = []
    for k in bad:
        item = film.ink[k]
        where = f"{item['shot']} {item['layer']}" if item["shot"] else item["layer"]
        problems.append(
            f"{where}: ink box {item['box']} (x, y, w, h) leaves the safe zone {rect} ({film.lay.name})"
        )
    for problem in problems:
        if problem not in plan.warnings:
            plan.warnings.append(problem)
    return problems


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _render_parts(
    project: Project, plan: FilmPlan, out_dir: Path, *, plan_only: bool, release: bool = False
) -> tuple[dict[Path, Path], FilmPlan]:
    """Write one film's outputs as *.part files: ({final: part}, the plan with its render record).
    On failure the parts written so far are deleted. With `release`, text outside the safe zone is
    an error."""
    stem = f"{project.slug}-{plan.id}"
    mp4, poster, web_mp4 = out_dir / f"{stem}.mp4", out_dir / f"{stem}.jpg", out_dir / f"{stem}-web.mp4"
    plan_path, board = out_dir / f"{stem}{PLAN_SUFFIX}", out_dir / f"{stem}{STORYBOARD_SUFFIX}"
    parts: dict[Path, Path] = {}  # final → part; never *.mp4: packaging ships every film/*.mp4

    def part(final: Path) -> Path:
        parts[final] = final.with_name(f"{final.name}.part")
        return parts[final]

    film = renderer(project, plan)
    check_seam(plan, film)
    ink = check_ink(plan, film)
    if release and ink:
        raise ReleaseError(
            f"release film {plan.id} blocked: text outside the safe zone:\n  " + "\n  ".join(ink)
        )
    try:
        if plan_only:
            plan.render = None
            if plan_path.is_file() and mp4.is_file():  # an unchanged plan keeps the film it rendered
                try:
                    old = FilmPlan.load(plan_path)
                    plan.render = old.render if old.hash == plan.hash else None
                except (OSError, ValueError):
                    pass
        else:
            meter = LumaMeter(plan.plan["fps"])
            web = plan.encode.get("web")
            encode_frames(
                film, part(mp4), crf=plan.encode["crf"], preset=plan.encode["preset"], meter=meter,
                web=(part(web_mp4), web) if web else None, gop=plan.encode.get("gop"),
                threads=plan.encode.get("threads", ENCODER_THREADS),
            )  # fmt: skip
            meter.check(f"film {plan.id}")
            if plan.encode.get("tracks"):  # captions and chapters, by stream copy (video unchanged)
                embed(parts[mp4], plan)
                if web:
                    embed(parts[web_mp4], plan)
            part(poster).write_bytes(film.poster())

            def record(path: Path) -> dict[str, Any]:
                return {"bytes": parts[path].stat().st_size, "sha256": _sha256(parts[path])}

            plan.render = {
                "mp4": record(mp4),
                "web": record(web_mp4) if web else None,
                "flash": meter.result(),
                "ffmpeg": ffmpeg_version(),
            }
        for suffix, text in track_texts(plan).items():
            part(out_dir / f"{stem}{suffix}").write_text(text, encoding="utf-8", newline="\n")
        write_storyboard(plan, film.frame_at, plan.facts, part(board))
        plan.save(part(plan_path))
    except BaseException:
        for p in parts.values():
            p.unlink(missing_ok=True)
        raise
    return parts, plan


def _commit(parts: dict[Path, Path]) -> None:
    """Rename a film's *.part files to their final names (the plan.json last: it was added last)."""
    for final, p in parts.items():
        p.replace(final)


def _render_one(project: Project, plan: FilmPlan, out_dir: Path, *, plan_only: bool) -> Path:
    """Write one film's outputs as *.part files, then rename them all (the plan last). Returns the
    MP4 (or, with plan_only, the plan.json)."""
    parts, plan = _render_parts(project, plan, out_dir, plan_only=plan_only)
    _commit(parts)
    return _final(project, plan, out_dir, plan_only=plan_only)


def _final(project: Project, plan: FilmPlan, out_dir: Path, *, plan_only: bool) -> Path:
    stem = f"{project.slug}-{plan.id}"
    return out_dir / (f"{stem}{PLAN_SUFFIX}" if plan_only else f"{stem}.mp4")


def _worker(
    project: Project, plan: FilmPlan, out_dir: Path, release: bool
) -> tuple[dict[Path, Path], FilmPlan]:
    """One film rendered in a worker process (OpenCV single-threaded: the films run side by side)."""
    cv2.setNumThreads(1)
    return _render_parts(project, plan, out_dir, plan_only=False, release=release)


def film_jobs(n: int) -> int:
    """Worker processes for n films: VANTAGE_FILM_JOBS when set (1: sequential), else one per film
    when there is more than one and at least 4 CPUs."""
    env = os.environ.get("VANTAGE_FILM_JOBS", "").strip()
    if env:
        try:
            jobs = int(env)
        except ValueError as exc:
            raise ValueError(f"VANTAGE_FILM_JOBS must be a whole number, not {env!r}") from exc
    else:
        cpus = os.cpu_count() or 1
        jobs = cpus if n > 1 and cpus >= 4 else 1
    return max(1, min(jobs, n))


def _render_all(
    project: Project, plans: Sequence[FilmPlan], out_dir: Path, *, plan_only: bool, release: bool = False
) -> list[FilmPlan]:
    """Render every plan (in worker processes when film_jobs allows) and rename the outputs only
    once every film has succeeded; on any failure every film's *.part files are deleted."""
    jobs = 1 if plan_only else film_jobs(len(plans))
    done: list[tuple[dict[Path, Path], FilmPlan]] = []
    try:
        if jobs == 1:
            for plan in plans:
                done.append(_render_parts(project, plan, out_dir, plan_only=plan_only, release=release))
        else:
            ctx = multiprocessing.get_context("spawn")  # never fork a process that holds OpenCV threads
            with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as pool:
                futures = [pool.submit(_worker, project, plan, out_dir, release) for plan in plans]
                errors = []
                for future in futures:
                    try:
                        done.append(future.result())
                    except BaseException as exc:  # keep collecting so every worker's parts are known
                        errors.append(exc)
                if errors:
                    raise errors[0]
    except BaseException:
        for parts, _ in done:
            for part in parts.values():
                part.unlink(missing_ok=True)
        for part in out_dir.glob(f"{project.slug}-*.part"):
            part.unlink(missing_ok=True)
        raise
    for parts, _ in done:
        _commit(parts)
    return [plan for _, plan in done]


def _clean(project: Project, out_dir: Path, ids: Sequence[str]) -> None:
    """Delete <slug>-<id>.* film files whose id is no longer in the film set (and stray *.part files)."""
    for path in sorted(out_dir.glob(f"{project.slug}-*")):
        if path.is_file() and (film_id(project.slug, path.name) not in ids or path.name.endswith(".part")):
            log.info(f"removing {path.name}: not a current film")
            path.unlink()


def _hours(
    project: Project, only: Sequence[str] | None, hours: float | None, plan_only: bool
) -> dict[str, float]:
    """film id → hours of film/usb/ copy to write: `hours` for the named films (each must be a loop
    film), else the `hours` of each loop film's FilmSpec. ValueError for --hours without --film,
    on a film that is not a loop, out of (0, 12], or with --plan-only."""
    films = resolve_films(project)
    if hours is None:
        named = only if only is not None else [f for f, film in films.items() if film.auto]
        return {f: films[f].hours for f in named if f in films and films[f].loop and films[f].hours}  # type: ignore[misc]
    if not only:
        raise ValueError("--hours needs --film naming a loop film (e.g. --film screen)")
    if not 0 < hours <= MAX_HOURS:
        raise ValueError(f"--hours must be more than 0 and at most {MAX_HOURS:g} (got {hours:g})")
    if plan_only:
        raise ValueError("--hours copies a rendered loop film; drop --plan-only")
    for fid in only:
        if fid in films and not films[fid].loop:
            raise ValueError(
                f"--hours: film {fid!r} is not a loop film (only loop films, such as preset: screen)"
            )
    return dict.fromkeys(only, hours)


def _current(project: Project, plan: FilmPlan, out_dir: Path) -> bool:
    """Whether out_dir already holds this plan's film exactly as its render record says."""
    stem = f"{project.slug}-{plan.id}"
    plan_path, mp4 = out_dir / f"{stem}{PLAN_SUFFIX}", out_dir / f"{stem}.mp4"
    if not (plan_path.is_file() and mp4.is_file()):
        return False
    try:
        old = FilmPlan.load(plan_path)
    except (OSError, ValueError):
        return False
    record = (old.render or {}).get("mp4") or {}
    return old.hash == plan.hash and old.encode == plan.encode and record.get("sha256") == _sha256(mp4)


def render_film(
    project: Project,
    out_dir: Path,
    *,
    only: Sequence[str] | None = None,
    release: bool = False,
    plan_only: bool = False,
    hours: float | None = None,
) -> list[Path]:
    """Plan and render the films (all of them, or the ids in `only`) into out_dir = dist/<slug>/film:
    MP4 (+ -web.mp4) + poster + plan.json + storyboard each. `plan_only` writes the plan and
    storyboard without encoding; `release` first refuses a draft project or non-releasable facts,
    and refuses text outside a film's safe zone. `hours` (with `only` naming loop films; else a
    loop FilmSpec's `hours`) also repeats each loop film into out_dir/usb/ by stream copy
    (film/loop.py), reusing a loop film already rendered from the same plan. Returns the MP4s (the
    plan.json files with plan_only)."""
    usb = _hours(project, only, hours, plan_only)
    if release:
        problems = check_release(project)
        if problems:
            raise ReleaseError("release films blocked:\n  " + "\n  ".join(problems))
    plans = plan_films(project, only=only)
    out_dir.mkdir(parents=True, exist_ok=True)
    if only is None:
        _clean(project, out_dir, list(resolve_films(project)))
    reuse = [p for p in plans.values() if hours is not None and not release and _current(project, p, out_dir)]
    for plan in reuse:
        log.info(f"{project.slug}-{plan.id}.mp4 matches its plan {plan.hash[:12]}; not re-rendered")
    kept = {p.id for p in reuse}
    todo = [p for p in plans.values() if p.id not in kept]
    done = {p.id: p for p in _render_all(project, todo, out_dir, plan_only=plan_only, release=release)}
    outputs = []
    for plan in reuse:
        outputs.append(_final(project, plan, out_dir, plan_only=False))
    for plan in done.values():
        if not track_texts(plan):  # tracks turned off (or a loop film): no stale sidecars
            for suffix in SUFFIXES:
                (out_dir / f"{project.slug}-{plan.id}{suffix}").unlink(missing_ok=True)
        if not plan.encode.get("web"):  # web copy turned off (or a loop film): no stale web copy
            (out_dir / f"{project.slug}-{plan.id}-web.mp4").unlink(missing_ok=True)
        out = _final(project, plan, out_dir, plan_only=plan_only)
        p = plan.plan
        sections = sum(s["kind"] in ("section", "capture") for s in p["shots"])
        done = "planned" if plan_only else "rendered"
        log.info(
            f"{out.name}: {p['width']}×{p['height']}, {p['frames']} frames ({p['frames'] / p['fps']:.1f}s), "
            f"{sections} {'capture' if p['cut'] == 'story' else 'section'}(s), plan {plan.hash[:12]} {done}"
        )
        for warning in plan.warnings:
            log.warn(f"film {plan.id}: {warning}")
        outputs.append(out)
    if not plan_only:
        for fid, h in usb.items():
            bad = [f for f, snap in plans[fid].facts.items() if not snap.get("releasable")]
            if bad and not project.config.draft:  # an unattended public screen: nobody to explain them
                raise ReleaseError(
                    f"--hours: film {fid} prints fact(s) {', '.join(map(repr, bad))} that are not "
                    "releasable; a USB copy plays unattended, so verify them (or mark the project draft)"
                )
            copy = concat_hours(out_dir / f"{project.slug}-{fid}.mp4", h, out_dir / USB_DIR)
            log.ok(f"{fid}: {h:g} h for a USB player → {copy} ({copy.stat().st_size / 1024**2:.1f} MiB)")
    return outputs
