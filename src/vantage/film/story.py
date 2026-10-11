"""The story cut: a film's shots derived from story.yaml's chapters on a beat grid (film/story.py).

`derive_cut` turns the chapters in a film's scope into shots, in story order:

    title    the first image at rest under a night scrim; the kicker tracks out across beat 0 and
             the title's word groups rise on frames 0, B/3 and 2B/3 of beat 1
    capture  one per capture of each scrub chapter (from..to, minus `exclude`d captures): 1 beat
             for a quiet capture, 3 for a step, 4 for a step that pushes in. Every capture but the
             film's first image opens with a dissolve; the step's words (film.line, else its
             leading **bold** run) land on beat 1 and a push reaches its target at beat 2⅓
    curtain  one per compare chapter: an entry beat at split 0 (a dissolve to `after` unless it is
             already on screen), then 2 beats of sweep and 2 of hold per step with a split; the
             labels show only while the rule is 8 px clear of their ink (`reveal`)
    stat     up to `stats` items of the first stats chapter whose value is a {fact:} token, 4 beats
             each on the chapter's surface; the figure is the fact's text and never counts
    card     between consecutive images of different vantages: the chapter's kicker and title on
             night, dipped in and out
    end      a dip to night, then the brand, credits, URL, fine print and the honesty line

A loop film (the `screen` preset) opens with a `rise` from pure night onto the title's image (the
rail's knob sweeping back to its first tick) and closes with a `dip` back to pure night, so frame N
(one past the last) equals frame 0. Its end card is only the brand and the short URL, its stats
need releasable facts (unless the project is a draft), and its end card is lengthened by a beat
when needed so the film is a whole number of GOPs (2 beats).

Every shot start, dissolve start, sweep, push start and word landing is a multiple of B
(film/beat.py). Push targets are clamped so a master is never enlarged beyond `max_upscale` and
recentred on the hotspots that changed since the previous step. A main vantage with one or two
captures degrades to title → capture (→ curtain) → stats → end. Dips to or from paper last
`surface_dip` frames so a paper card never flashes.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from vantage.config import (
    FACT_TOKEN_RE,
    CaptureNote,
    CompareChapter,
    HeroChapter,
    Hotspot,
    Project,
    ScrubChapter,
    StatsChapter,
    Step,
)
from vantage.film.beat import Grid, beat_grid
from vantage.film.layout import Layout, layout
from vantage.film.plan import (
    FILM_RENDERER,
    GOP_BEATS,
    HONESTY,
    LOOP_BEATS,
    SIMULATED_NOTE,
    FilmDef,
    _Printer,
    _sha256,
    encode_settings,
    fact_snapshot,
    film_size,
    has_tracks,
    honesty_line,
    said,
)
from vantage.film.safety import IMAGES, check_plan, surface_dip
from vantage.film.tracks import Tracks, iso639_2, plain_markdown, sentence, track_facts
from vantage.models import FilmPlan, MastersIndex
from vantage.site.build import resolve_capture_ref
from vantage.site.render import capture_label as date_label

LEAD_RE = re.compile(r"^\s*\*\*(.+?)\*\*", re.S)
MARGIN = 0.06  # subject margin, as a share of the crop width
SOFT = 1.25  # output px per master px beyond which a push is "soft"
MIN_ZOOM, MIN_SHIFT = 1.08, 0.03  # below both, a clamped push becomes a still step
Cam = tuple[float, float, float]


def lead_words(step: Step) -> tuple[str | None, str | None]:
    """(raw words, 'film.line' | 'lead') a step puts on screen in films, or (None, None)."""
    if step.film is not None and step.film.line:
        return step.film.line, "film.line"
    m = LEAD_RE.match(step.text)
    return (m.group(1).strip(), "lead") if m else (None, None)


# --------------------------------------------------------------------------- #
# Camera geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Geometry:
    """A W × H film cover-cropping a mw × mh master."""

    width: int
    height: int
    mw: int
    mh: int

    @property
    def s0(self) -> float:
        """Cover-fit scale: output px per master px at rest."""
        return max(self.width / self.mw, self.height / self.mh)

    def zmax(self, max_upscale: float) -> float:
        return max(1.0, max_upscale / self.s0)

    def half(self, zoom: float) -> tuple[float, float]:
        """Half the crop's width and height at `zoom`, normalized to the master."""
        k = self.s0 * zoom
        return self.width / (2 * k) / self.mw, self.height / (2 * k) / self.mh

    def centre(self, x: float, y: float, zoom: float) -> tuple[float, float]:
        """Where a camera aimed at (x, y) actually centres once the crop is kept inside the master."""
        hw, hh = self.half(zoom)
        return min(max(x, hw), 1 - hw), min(max(y, hh), 1 - hh)


def _fit(geo: Geometry, fx: float, fy: float, zoom: float, pts: Sequence[tuple[float, float]]) -> Cam | None:
    """A crop at `zoom` nearest to (fx, fy) that holds every point with the subject margin, or None."""
    hw, hh = geo.half(zoom)
    mx = MARGIN * 2 * hw
    my = mx * geo.mw / geo.mh
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x_lo, x_hi = max(max(xs) + mx - hw, hw), min(min(xs) - mx + hw, 1 - hw)
    y_lo, y_hi = max(max(ys) + my - hh, hh), min(min(ys) - my + hh, 1 - hh)
    if x_lo > x_hi + 1e-12 or y_lo > y_hi + 1e-12:
        return None
    cx, cy = geo.centre(fx, fy, zoom)
    return min(max(cx, x_lo), x_hi), min(max(cy, y_lo), y_hi), zoom


def aim(
    geo: Geometry, focus: Sequence[float], zmax: float, subjects: Sequence[Hotspot], *, explicit: bool
) -> tuple[Cam, str]:
    """(x, y, zoom) of a push and how it was chosen: 'film.focus', 'focus', 'focus+subject:<ids>' or
    'subject-centroid'. Zoom is clamped to [1, zmax]; subjects lower it toward 1 until they fit."""
    fx, fy, z = focus[0], focus[1], min(max(focus[2], 1.0), zmax)
    if explicit:
        return (fx, fy, z), "film.focus"
    if not subjects:
        return (fx, fy, z), "focus"
    pts = [(h.x, h.y) for h in subjects]
    names = ",".join(h.id for h in subjects)
    for k in range(33):  # from the clamped zoom down to 1 in 32 even steps
        zoom = z - (z - 1.0) * k / 32
        cam = _fit(geo, fx, fy, zoom, pts)
        if cam is not None:
            return cam, f"focus+subject:{names}"
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts), 1.0), "subject-centroid"


# --------------------------------------------------------------------------- #
# Series
# --------------------------------------------------------------------------- #


@dataclass
class _Capture:
    date: str
    file: str
    label: str
    label_facts: list[str]
    index: int  # in the vantage's (non-excluded) captures
    note: str | None = None  # captures[].note as printed (tracks only: captions and transcript)
    note_facts: list[str] = field(default_factory=list)
    step: Step | None = None
    step_index: int | None = None
    subjects: list[Hotspot] = field(default_factory=list)


@dataclass
class _Series:
    chapter: str | None
    vantage: str
    name: str  # card title fallback
    kicker: str | None
    title: str | None
    captures: list[_Capture]
    geometry: Geometry
    portrait_focus: tuple[float, float]


def _chapter_ids(project: Project) -> list[str]:
    return [ch.id or f"{ch.type}-{i + 1}" for i, ch in enumerate(project.story.chapters)]


def main_vantage(project: Project, index: MastersIndex) -> str | None:
    """The first scrub chapter's vantage with masters, else the first compare's, else the first vantage."""
    for kind in ("scrub", "compare"):
        for ch in project.story.chapters:
            if ch.type == kind and getattr(ch, "vantage", None) in index.vantages:
                return ch.vantage  # type: ignore[union-attr]
    story = [v.id for v in project.story.vantages if v.id in index.vantages]
    return (story or list(index.vantages) or [None])[0]


def in_scope(project: Project, film: FilmDef, index: MastersIndex, warnings: list[str]) -> list[int]:
    """Indices of the chapters a film uses: all, main (the main vantage's chapters plus stats) or ids."""
    ids = _chapter_ids(project)
    chapters = project.story.chapters
    if film.chapters == "all":
        return list(range(len(chapters)))
    if film.chapters == "main":
        main = main_vantage(project, index)
        return [
            i for i, ch in enumerate(chapters) if getattr(ch, "vantage", None) == main or ch.type == "stats"
        ]
    wanted = list(film.chapters)
    unknown = [c for c in wanted if c not in ids]
    if unknown:
        warnings.append(f"films.{film.id}.chapters: unknown chapter id(s) {', '.join(unknown)}; ignored")
    return [i for i, cid in enumerate(ids) if cid in wanted]


class _Captures:
    """Every vantage's non-excluded captures with labels as the site prints them."""

    def __init__(self, project: Project, index: MastersIndex, warnings: list[str]):
        self.project, self.index, self.warnings = project, index, warnings
        self.notes: dict[str, CaptureNote] = {n.date.isoformat(): n for n in project.story.captures}
        self.excluded = {d for d, n in self.notes.items() if n.exclude}

    def of(self, vid: str) -> list[_Capture]:
        mv = self.index.vantages[vid]
        caps = sorted((c for c in mv.captures if c.date not in self.excluded), key=lambda c: c.date)
        months = Counter(c.date[:7] for c in caps)
        out = []
        for i, c in enumerate(caps):
            say = _Printer(self.project, self.warnings)
            entry = self.notes.get(c.date)
            label = say(entry.label if entry else None, f"captures {c.date} label")
            label = label or date_label(c.date, day=months[c.date[:7]] > 1)
            note, note_facts = said(
                self.project, entry.note if entry else None, f"captures {c.date} note", self.warnings
            )
            out.append(_Capture(c.date, c.file, label, list(say.facts), i, note, note_facts))
        return out


def _geometry(project: Project, index: MastersIndex, vid: str, width: int, height: int) -> Geometry:
    mv = index.vantages[vid]
    return Geometry(width, height, mv.width, mv.height)


def _series(
    project: Project, chapter_index: int, ch: ScrubChapter, caps: _Captures, size: tuple[int, int]
) -> _Series | None:
    vid = ch.vantage
    cid = _chapter_ids(project)[chapter_index]
    where = f"chapter {cid!r}"
    if vid not in caps.index.vantages:
        caps.warnings.append(f"{where}: vantage {vid!r} has no masters; left out of the films")
        return None
    every = caps.of(vid)
    if not every:
        return None
    dates = [c.date for c in every]
    lo = resolve_capture_ref(ch.from_, dates, default=0)
    hi = resolve_capture_ref(ch.to, dates, default=len(dates) - 1)
    if lo > hi:
        raise ValueError(f"story.yaml {where}: 'from' ({ch.from_}) is after 'to' ({ch.to})")
    at: dict[int, tuple[int, Step]] = {}
    keyed: list[int] = []
    for j, step in enumerate(ch.steps):
        k = min(max(resolve_capture_ref(step.capture, dates, default=lo) if step.capture else lo, lo), hi)
        keyed.append(k)
        if step.film is not None and step.film.skip:
            continue
        if k in at:
            caps.warnings.append(
                f"{where} step {j}: another step is on {dates[k]} already; films use the first"
            )
            continue
        at[k] = (j, step)
    last = len(dates) - 1
    spots = [
        (h, resolve_capture_ref(h.from_, dates, default=0), resolve_capture_ref(h.to, dates, default=last))
        for h in ch.hotspots
    ]
    series = every[lo : hi + 1]
    for cap in series:
        if cap.index not in at:
            continue
        j, step = at[cap.index]
        cap.step, cap.step_index = step, j
        earlier = [k for k in keyed[:j] + keyed[j + 1 :] if k < cap.index]
        prev = max(earlier) if earlier else lo
        cap.subjects = [h for h, a, b in spots if prev < a <= cap.index or prev <= b <= cap.index - 1]
    vantage = next((v for v in project.story.vantages if v.id == vid), None)
    pf = vantage.portrait_focus if vantage and vantage.portrait_focus else (0.5, 0.5)
    name = (vantage.name if vantage else None) or caps.index.vantages[vid].name
    return _Series(
        cid, vid, name, ch.kicker, ch.title, series,
        _geometry(project, caps.index, vid, *size), (pf[0], pf[1]),
    )  # fmt: skip


def _fallback_series(project: Project, caps: _Captures, vid: str, size: tuple[int, int]) -> _Series:
    """No scrub chapter in scope: the main vantage plays as one quiet series."""
    vantage = next((v for v in project.story.vantages if v.id == vid), None)
    pf = vantage.portrait_focus if vantage and vantage.portrait_focus else (0.5, 0.5)
    name = (vantage.name if vantage else None) or caps.index.vantages[vid].name
    geo = _geometry(project, caps.index, vid, *size)
    return _Series(None, vid, name, None, None, caps.of(vid), geo, (pf[0], pf[1]))


@dataclass
class _Curtain:
    chapter: str | None
    vantage: str
    name: str
    kicker: str | None
    title: str | None
    before: _Capture
    after: _Capture
    labels: tuple[str | None, str | None]  # raw before_label, after_label (None: the capture label)
    steps: list[tuple[int | None, Step | None, float]]  # (step index, step, split)
    geometry: Geometry
    portrait_focus: tuple[float, float]


def _vantage_info(project: Project, caps: _Captures, vid: str) -> tuple[str, tuple[float, float]]:
    vantage = next((v for v in project.story.vantages if v.id == vid), None)
    pf = vantage.portrait_focus if vantage and vantage.portrait_focus else (0.5, 0.5)
    name = (vantage.name if vantage else None) or caps.index.vantages[vid].name
    return name, (pf[0], pf[1])


def _curtain(
    project: Project,
    chapter_index: int | None,
    ch: CompareChapter | None,
    caps: _Captures,
    vid: str,
    size: tuple[int, int],
    *,
    pair: tuple[_Capture, _Capture] | None = None,
) -> _Curtain | None:
    """A compare chapter as a curtain (or, with `pair`, the curtain of a two-capture film)."""
    cid = _chapter_ids(project)[chapter_index] if chapter_index is not None else None
    where = f"chapter {cid!r}"
    if vid not in caps.index.vantages:
        caps.warnings.append(f"{where}: vantage {vid!r} has no masters; left out of the films")
        return None
    every = caps.of(vid)
    if pair is None:
        if len(every) < 2:
            caps.warnings.append(f"{where}: fewer than two captures to compare; left out of the films")
            return None
        assert ch is not None
        dates = [c.date for c in every]
        lo = resolve_capture_ref(ch.before, dates, default=0)
        hi = resolve_capture_ref(ch.after, dates, default=len(dates) - 1)
        if lo == hi:
            caps.warnings.append(f"{where}: before and after are the same capture; left out of the films")
            return None
        pair = (every[lo], every[hi])
    steps: list[tuple[int | None, Step | None, float]] = []
    for j, step in enumerate(ch.steps if ch else []):
        if step.film is not None and step.film.skip:
            continue
        if step.split is not None:
            steps.append((j, step, step.split))
    if not steps:
        steps = [(None, None, 0.5)]
    name, pf = _vantage_info(project, caps, vid)
    labels = (ch.before_label, ch.after_label) if ch else (None, None)
    return _Curtain(
        cid, vid, name, ch.kicker if ch else None, ch.title if ch else None, pair[0], pair[1], labels,
        steps, _geometry(project, caps.index, vid, *size), pf,
    )  # fmt: skip


def _stat_items(
    project: Project,
    cid: str,
    ch: StatsChapter,
    limit: int,
    warnings: list[str],
    *,
    releasable_only: bool = False,
) -> list[dict[str, Any]]:
    """Up to `limit` stats whose value is a {fact:} token, printed through the facts. Any other item,
    and any item that uses a do-not-print fact, is left out with a warning, as is (with
    `releasable_only`, loop films of a project that is not a draft) any item that prints a fact
    that is not releasable. Values never count."""
    out: list[dict[str, Any]] = []
    for k, item in enumerate(ch.items):
        if len(out) >= limit:
            break
        where = f"chapter {cid!r} stat {k}"
        if not FACT_TOKEN_RE.search(item.value):
            warnings.append(f"{where}: value {item.value!r} is not a {{fact:}} token; left out of the films")
            continue
        say = _Printer(project, warnings)
        value = say(item.value, f"{where} value")
        unit = say(item.unit, f"{where} unit") if item.unit else None
        label = say(item.label, f"{where} label")
        source = say(item.source, f"{where} source") if item.source else None
        if value is None or label is None or (item.unit and unit is None) or (item.source and source is None):
            warnings.append(f"{where}: left out of the films (it uses a do-not-print fact)")
            continue
        facts = project.facts.facts
        held = [f for f in say.facts if f not in facts or not facts[f].releasable]
        if releasable_only and held:
            warnings.append(
                f"{where}: left out of the loop (fact {', '.join(held)} is not releasable; "
                f"a loop plays unattended)"
            )
            continue
        out.append({"value": value, "unit": unit, "label": label, "source": source, "facts": list(say.facts)})
    return out


# --------------------------------------------------------------------------- #
# The cut
# --------------------------------------------------------------------------- #


class _Camera:
    """Hold windows in frames ({a, b, x, y, zoom}); beat.keyed smoothsteps between them."""

    def __init__(self) -> None:
        self.windows: list[dict[str, Any]] = []

    def hold(self, a: int, cam: Cam, b: int | None = None) -> None:
        b = a if b is None else b
        if self.windows:
            last = self.windows[-1]
            if (last["x"], last["y"], last["zoom"]) == cam:
                last["b"] = max(last["b"], b)
                return
        self.windows.append({"a": a, "b": b, "x": cam[0], "y": cam[1], "zoom": cam[2]})

    def until(self, f: int) -> None:
        self.windows[-1]["b"] = max(self.windows[-1]["b"], f)


@dataclass
class _Builder:
    project: Project
    film: FilmDef
    grid: Grid
    layout: Layout
    warnings: list[str]
    paper_dip: int
    shots: list[dict[str, Any]] = field(default_factory=list)
    f: int = 0  # the next shot's first frame (always on the grid)
    camera: _Camera = field(default_factory=_Camera)
    tracks: Tracks = field(default_factory=lambda: Tracks(1, enabled=False))

    def say(self) -> _Printer:
        return _Printer(self.project, self.warnings)

    def said(self, raw: str | None, where: str) -> tuple[str | None, list[str]]:
        """(raw text printed through the facts, its fact ids), or (None, []) without it."""
        return said(self.project, raw, where, self.warnings) if raw else (None, [])

    def step_text(self, step: Step | None, where: str) -> tuple[str | None, list[str]]:
        """A step's full text as plain text (tracks only), printed through the facts."""
        text, facts = self.said(step.text if step else None, f"{where} text")
        return (plain_markdown(text) or None, facts) if text else (None, [])

    def add(self, shot: dict[str, Any], beats: int) -> dict[str, Any]:
        shot["f0"], shot["f1"] = self.f, self.f + beats * self.grid.fpb
        self.f = shot["f1"]
        self.shots.append(shot)
        return shot

    def rest(self, part: _Series | _Curtain) -> Cam:
        if self.layout.width < self.layout.height:
            return part.portrait_focus[0], part.portrait_focus[1], 1.0
        return 0.5, 0.5, 1.0

    @property
    def prev(self) -> dict[str, Any] | None:
        return self.shots[-1] if self.shots else None

    def entry(self) -> dict[str, Any]:
        """The dissolve an image shot starting now opens with, after the previous shot."""
        prev, f0, D = self.prev, self.f, self.grid.dissolve
        assert prev is not None
        if prev["kind"] in ("card", "end"):
            return {"from": "night", "f0": f0, "f1": f0 + D}
        if prev["kind"] == "stat":
            n = self.paper_dip if prev["surface"] == "paper" else D
            return {"from": prev["surface"], "f0": f0, "f1": f0 + n}
        return {
            "from": prev.get("capture") if prev["kind"] == "capture" else prev["id"],
            "f0": f0,
            "f1": f0 + D,
        }

    def dip(self, surface: str) -> int:
        """Frames of the dip into a `surface` shot starting now."""
        prev = self.prev
        theirs = None
        if prev is not None and prev["kind"] in ("card", "end"):
            theirs = "night"
        elif prev is not None and prev["kind"] == "stat":
            theirs = prev["surface"]
        return self.paper_dip if "paper" in (surface, theirs) and surface != theirs else self.grid.dissolve


def _title(b: _Builder, vantage: str, cap: _Capture, dates: Sequence[str]) -> dict[str, Any]:
    project, cfg = b.project, b.project.config
    hero = next((ch for ch in project.story.chapters if isinstance(ch, HeroChapter)), None)
    kicker, kicker_facts = b.said(hero.kicker if hero else None, "hero kicker")
    if not kicker:
        kicker, kicker_facts = b.said(cfg.kicker, "project.yaml kicker")
    if not kicker:
        place = cfg.location.name if cfg.location else None
        years = "–".join(dict.fromkeys((dates[0][:4], dates[-1][:4])))
        kicker = " · ".join(filter(None, (place, years)))
    title, title_facts = b.said(hero.title if hero else None, "hero title")
    if not title:
        title, title_facts = b.said(cfg.title, "project.yaml title")
    title = title or ""
    say = b.say()
    say.add([*kicker_facts, *title_facts])
    B, third = b.grid.fpb, b.grid.third
    f0 = b.f  # 0, or the end of a loop's rise
    shot = {
        "id": "T1",
        "kind": "title",
        "chapter": hero.id if hero and hero.id else None,
        "text": [
            t for t in ({"role": "kicker", "text": kicker}, {"role": "title", "text": title}) if t["text"]
        ],
        "facts": list(say.facts),
        "vantage": vantage,
        "capture": cap.date,
        "file": cap.file,
        "groups": [f0 + B, f0 + B + third, f0 + B + 2 * third],
    }
    b.add(shot, b.film.title_beats)
    shot["key"] = min(shot["f1"] - third - 1, f0 + 2 * B + third)
    b.tracks.cue(f0, title or kicker, title_facts if title else kicker_facts)
    b.tracks.line(
        f0,
        f"Title over the {cap.label} view: {sentence(kicker, title)}",
        [*kicker_facts, *title_facts, *cap.label_facts],
    )
    return shot


def _aim_step(
    b: _Builder,
    geo: Geometry,
    requested: Sequence[float],
    explicit: bool,
    subjects: Sequence[Hotspot],
    rest: Cam,
    zmax: float,
    label: str,
) -> tuple[Cam, dict[str, Any]]:
    """A step's camera target, clamped by max_upscale (with warnings), and its plan record."""
    cam, target = aim(geo, requested, zmax, [] if explicit else subjects, explicit=explicit)
    z = max(requested[2], 1.0)
    if zmax < z - 1e-9:
        b.warnings.append(
            f"{label}: push clamped from {z:g} to {zmax:.3g} ({b.film.id}; max_upscale {b.film.max_upscale:g})"
        )
    upscale = geo.s0 * cam[2]
    if upscale > SOFT + 1e-9:
        b.warnings.append(f"{label}: soft push, {upscale:.2f} output px per master px ({b.film.id})")
    a, c = geo.centre(rest[0], rest[1], 1.0), geo.centre(cam[0], cam[1], cam[2])
    push = not (cam[2] < MIN_ZOOM and math.hypot(c[0] - a[0], c[1] - a[1]) < MIN_SHIFT)
    return cam, {
        "focus": [cam[0], cam[1], cam[2]],
        "target": target,
        "subjects": [] if explicit else [h.id for h in subjects],
        "z_eff": cam[2],
        "upscale": upscale,
        "push": push,
        "reason": None if push else "clamped",
    }


def _push(
    b: _Builder, series: _Series, cap: _Capture, rest: Cam, zmax: float
) -> tuple[dict[str, Any] | None, Cam | None]:
    """The step record of a capture and its push target (None without a push)."""
    step = cap.step
    if step is None:
        return None, None
    raw, source = lead_words(step)
    where = f"chapter {series.chapter!r} step {cap.step_index}"
    say = b.say()
    words = say(raw, f"{where} film words") if raw else None
    if raw and words is None:
        source = None
    requested = step.film.focus if step.film and step.film.focus else step.focus
    record: dict[str, Any] = {
        "index": cap.step_index,
        "words": words,
        "words_from": source,
        "facts": list(say.facts),
        "focus_requested": list(requested) if requested else None,
        "focus": None,
        "target": None,
        "subjects": [],
        "z_eff": None,
        "upscale": None,
        "push": False,
        "reason": None if requested else "no focus",
    }
    if not requested:
        return record, None
    explicit = bool(step.film and step.film.focus)
    cam, aimed = _aim_step(
        b, series.geometry, requested, explicit, cap.subjects, rest, zmax, f"{series.chapter} {cap.date}"
    )
    record.update(aimed)
    return record, (cam if aimed["push"] else None)


def _card(b: _Builder, chapter: str | None, kicker: str | None, title: str | None, name: str, vid: str):
    k, k_facts = b.said(kicker, f"chapter {chapter!r} kicker")
    t, t_facts = b.said(title, f"chapter {chapter!r} title")
    if not t:
        t, t_facts = b.said(name, f"vantage {vid} name")
    t = t or name
    say = b.say()
    say.add([*k_facts, *t_facts])
    n = sum(s["kind"] == "card" for s in b.shots) + 1
    dip = b.dip("night")
    card = b.add(
        {
            "id": f"K{n}",
            "kind": "card",
            "chapter": chapter,
            "text": [x for x in ({"role": "kicker", "text": k}, {"role": "title", "text": t}) if x["text"]],
            "facts": list(say.facts),
            "vantage": vid,
            "dip": dip,
        },
        4,
    )
    card["key"] = card["f0"] + 2 * b.grid.fpb
    start = card["f0"] + dip // 2
    b.tracks.cue(start, t, t_facts)
    b.tracks.line(start, f"Chapter card: {sentence(k, t)}", [*k_facts, *t_facts])
    return card


def _curtain_labels(
    project: Project, fonts: dict[str, Any], lay: Layout, texts: Sequence[str], tagged: bool
) -> tuple[float, int, list[float]]:
    """(scale, cap-top y, advance widths in px) of the curtain labels as film/cut.py sets them: each
    label fits between its text edge and a rule at the centre, and the row sits under a top-right tag when there is one."""
    from vantage.film.cut import CURTAIN_LABEL, TAG
    from vantage.film.plan import font_path
    from vantage.film.render import _Type

    files = {role: font_path(fonts[role]["file"], project.brand) for role in ("display", "text", "numeric")}
    c = project.brand.colors
    t = _Type(files, {"paper": c.paper, "accent": c.accent, "ink": c.ink}, lay.unit)  # type: ignore[arg-type]
    room = min(lay.width / 2 - lay.margin, lay.right - lay.width / 2) - 12 - 1.5 * lay.unit
    widest = max(t.width(text, CURTAIN_LABEL) for text in texts)
    scale = min(1.0, room / widest) if widest > 0 else 1.0
    top = lay.label_top
    if tagged and lay.tag == "top-right":
        top += round(-t.font(TAG).getbbox("H", anchor="ls")[1] + 18 * lay.unit)
    return scale, top, [t.width(text, CURTAIN_LABEL, scale) for text in texts]


def _intervals(flags: Sequence[bool], f0: int) -> list[list[int]]:
    out: list[list[int]] = []
    for k, on in enumerate(flags):
        if on and (not out or out[-1][1] != f0 + k):
            out.append([f0 + k, f0 + k + 1])
        elif on:
            out[-1][1] += 1
    return out


def derive_cut(
    project: Project,
    film: FilmDef,
    index: MastersIndex,
    fonts: dict[str, Any],
    hashes: dict[str, str],
    warnings: list[str],
    *,
    fast: bool,
) -> FilmPlan:
    """The story cut of `film` as a FilmPlan (raises ValueError when there is nothing to film)."""
    cfg, settings = project.config, project.config.output.film
    fps = settings.fps
    if fps <= 0:
        raise ValueError("output.film needs fps > 0")
    grid = beat_grid(fps, film.bpm)
    B, D, third = grid.fpb, grid.dissolve, grid.third
    width, height = film_size(settings, film.aspect, fast=fast)
    preset = "screen" if film.preset == "screen" and film.aspect == "16x9" else f"story-{film.aspect}"
    lay = layout(preset, width, height, safe=film.safe, type_scale=film.type_scale)
    colors = project.brand.colors
    paper_dip = surface_dip(fps, D, colors.night, colors.paper)
    caps = _Captures(project, index, warnings)
    chapters = project.story.chapters
    ids = _chapter_ids(project)
    size = (width, height)
    scope = in_scope(project, film, index, warnings)

    # The parts of the cut, in story order.
    parts: list[tuple[str, Any]] = []
    for i in scope:
        ch = chapters[i]
        if isinstance(ch, ScrubChapter):
            if (s := _series(project, i, ch, caps, size)) and s.captures:
                parts.append(("series", s))
        elif isinstance(ch, CompareChapter):
            if c := _curtain(project, i, ch, caps, ch.vantage, size):
                parts.append(("curtain", c))
        elif isinstance(ch, StatsChapter) and not any(k == "stats" for k, _ in parts):
            items = (
                _stat_items(
                    project, ids[i], ch, film.stats, warnings, releasable_only=film.loop and not cfg.draft
                )
                if film.stats
                else []
            )
            if items:
                heading, heading_facts = said(project, ch.title, f"chapter {ids[i]!r} title", warnings)
                if not heading:
                    heading, heading_facts = said(project, ch.kicker, f"chapter {ids[i]!r} kicker", warnings)
                parts.append(
                    ("stats", (ids[i], ch.surface or "paper", items, heading or "Figures", heading_facts))
                )

    # Degrade on the main vantage's captures (after exclusions).
    main = main_vantage(project, index)
    every = caps.of(main) if main is not None else []
    if not every:
        raise FileNotFoundError(f"no masters to film in {project.masters_dir}; run `vantage process` first")
    assert main is not None
    stats = [p for p in parts if p[0] == "stats"]
    still: set[int] = set()  # id() of captures that must not push (the two-capture cut's before)
    if len(every) <= 2:
        series = next((s for k, s in parts if k == "series" and s.vantage == main), None)
        series = series or _fallback_series(project, caps, main, size)
        series.captures = [c for c in series.captures if c.date == every[0].date] or [every[0]]
        parts = [("series", series)]
        if len(every) == 2:
            compare = next(
                (
                    (i, ch)
                    for i, ch in enumerate(chapters)
                    if isinstance(ch, CompareChapter) and ch.vantage == main and i in scope
                ),
                (None, None),
            )  # fmt: skip
            still.add(id(series.captures[0]))
            c = _curtain(project, compare[0], compare[1], caps, main, size, pair=(every[0], every[1]))
            if c is not None:
                parts.append(("curtain", c))
        parts += stats
    elif not any(k in ("series", "curtain") for k, _ in parts):
        parts.insert(0, ("series", _fallback_series(project, caps, main, size)))
    images = [p for k, p in parts if k in ("series", "curtain")]
    total_images = sum(len(p.captures) if isinstance(p, _Series) else 1 for p in images)

    b = _Builder(project, film, grid, lay, warnings, paper_dip)
    b.tracks = Tracks(fps, enabled=has_tracks(film, settings))
    for part in images:
        geo = part.geometry
        if geo.s0 > film.max_upscale + 1e-9:
            text = (
                f"vantage {part.vantage}: master upscaled even at rest ({geo.s0:.2f}× > max_upscale "
                f"{film.max_upscale:g}; {geo.mw}×{geo.mh} masters for a {width}×{height} film)"
            )
            if text not in warnings:
                warnings.append(text)
    first = images[0]
    first_cap = first.captures[0] if isinstance(first, _Series) else first.before
    if film.loop:  # from pure night onto the title's image
        rise = b.add(
            {"id": "R1", "kind": "rise", "chapter": None, "text": [], "facts": []}, LOOP_BEATS["rise"]
        )
        rise["key"] = rise["f0"] + B
    _title(b, first.vantage, first_cap, [c.date for c in every])
    cam = b.camera
    cam.hold(0, b.rest(first))

    n, series_no = 0, -1
    pushed: Cam | None = None  # the previous capture's push target, still held
    last_vantage = first.vantage
    for kind, part in parts:
        part_f0 = b.f  # the chapter (in the tracks) starts with the card that introduces it
        if kind in ("series", "curtain") and part.vantage != last_vantage:
            if b.prev is not None and b.prev["kind"] in IMAGES:
                cam.until(b.f + b.dip("night"))
            _card(b, part.chapter, part.kicker, part.title, part.name, part.vantage)
            pushed = None
        if kind == "series":
            s: _Series = part
            series_no += 1
            rest = b.rest(s)
            zmax = s.geometry.zmax(film.max_upscale)
            vname, vname_facts = b.said(s.name, f"vantage {s.vantage} name")
            vname = vname or caps.index.vantages[s.vantage].name
            heading, heading_facts = b.said(s.title, f"chapter {s.chapter!r} title")
            b.tracks.chapter(part_f0, heading or vname, heading_facts if heading else vname_facts)
            for cap in s.captures:
                n += 1
                f0 = b.f
                step, target = _push(b, s, cap, rest, zmax)
                if id(cap) in still and target is not None:
                    assert step is not None
                    step.update(push=False, reason="two captures")
                    target = None
                quiet, stepped, pushing = film.beats
                beats = quiet if step is None else (pushing if target else stepped)
                if total_images == 1 or id(cap) in still:
                    beats = max(beats, stepped)
                dissolve = None if n == 1 and b.prev and b.prev["kind"] == "title" else b.entry()
                if b.prev is not None and b.prev["kind"] not in IMAGES:
                    cam.hold(f0, rest)
                if pushed is not None:  # the previous push eases back during this dissolve
                    cam.until(f0)
                    cam.hold(f0 + D, rest)
                if target is not None:
                    cam.until(f0 + B)
                    cam.hold(f0 + 2 * B + third, target, f0 + beats * B)
                pushed = target
                facts = list(dict.fromkeys([*cap.label_facts, *(step["facts"] if step else [])]))
                text = [{"role": "label", "text": cap.label}]
                if step and step["words"]:
                    text.append({"role": "words", "text": step["words"]})
                    step["land"] = f0 if dissolve is None else f0 + B
                if step is not None:
                    step.pop("facts")
                    if target is not None:
                        step["push_f0"], step["push_f1"] = f0 + B, f0 + 2 * B + third
                shot = b.add(
                    {
                        "id": f"S{n:02d}",
                        "kind": "capture",
                        "chapter": s.chapter,
                        "text": text,
                        "facts": facts,
                        "vantage": s.vantage,
                        "capture": cap.date,
                        "label": cap.label,
                        "file": cap.file,
                        "dissolve": dissolve,
                        "series": series_no,
                        "rail": s.captures.index(cap),
                        "step": step,
                    },
                    beats,
                )
                f1 = shot["f1"]
                if target is not None:
                    shot["key"] = min(f1 - 1, f0 + 3 * B)
                elif step is not None:
                    shot["key"] = f0 + 2 * B
                else:
                    shot["key"] = f1 - 1
                if cap.file not in hashes:
                    hashes[cap.file] = _sha256(project.masters_dir / cap.file)
                _capture_tracks(b, shot, cap, s, vname, vname_facts, first=cap is s.captures[0])
            last_vantage = s.vantage
        elif kind == "curtain":
            pushed = _curtain_shot(b, part, fonts, hashes, part_f0)
            last_vantage = part.vantage
        else:
            cid, surface, items, heading, heading_facts = part
            b.tracks.chapter(part_f0, heading, heading_facts)
            for item in items:
                dip = b.dip(surface)
                if b.prev is not None and b.prev["kind"] in IMAGES:
                    cam.until(b.f + dip)
                k = sum(s["kind"] == "stat" for s in b.shots) + 1
                text = [
                    {"role": role, "text": item[role]}
                    for role in ("value", "unit", "label", "source")
                    if item[role]
                ]
                shot = b.add(
                    {
                        "id": f"N{k}",
                        "kind": "stat",
                        "chapter": cid,
                        "text": text,
                        "facts": item["facts"],
                        "value": item["value"],
                        "unit": item["unit"],
                        "label": item["label"],
                        "source": item["source"],
                        "surface": surface,
                        "dip": dip,
                    },
                    4,
                )
                shot["key"] = shot["f0"] + 3 * B
                start = shot["f0"] + dip // 2
                b.tracks.cue(
                    start, " ".join(filter(None, (item["value"], item["unit"], item["label"]))), item["facts"]
                )
                figure = " ".join(filter(None, (item["value"], item["unit"])))
                source = f" (source: {item['source']})" if item["source"] else ""
                b.tracks.line(start, f"Figure: {figure} {item['label']}{source}.", item["facts"])
            pushed = None

    # End card.
    brand = project.brand
    say = b.say()
    provenance = ["simulated" if cfg.simulated else "photo"]
    honesty = honesty_line(provenance)
    lines: list[dict[str, Any]] = [{"role": "brand", "text": brand.name}]
    short_url = cfg.output.base_url.split("://")[-1].rstrip("/") if cfg.output.base_url else None
    if film.loop and short_url:  # a loop's end card: the brand and the URL in large type, nothing else
        lines.append({"role": "url", "text": short_url})
    if not film.loop and brand.credit_line and (t := say(brand.credit_line, "brand.yaml credit_line")):
        lines.append({"role": "credit", "text": t})
    if not film.loop:
        for partner in brand.partners:
            lines += [{"role": "role", "text": partner.role}, {"role": "partner", "text": partner.name}]
        if short_url:
            lines.append({"role": "link", "text": short_url})
        for text_, where in ((brand.copyright, "copyright"), (brand.disclaimer, "disclaimer")):
            if text_ and (t := say(text_, f"brand.yaml {where}")):
                lines.append({"role": "fine", "text": t})
        lines.append({"role": "honesty", "text": honesty})
        if cfg.simulated:
            lines.append({"role": "fine", "text": SIMULATED_NOTE})
    dip = b.dip("night")
    if b.prev is not None and b.prev["kind"] in IMAGES:
        cam.until(b.f + dip)
    if pushed is not None:
        cam.until(b.f)
        cam.hold(b.f + D, b.rest(images[-1]))
    end_beats = film.end_beats
    if film.loop:  # a whole number of GOPs: lengthen the end card
        end_beats += -(b.f // B + end_beats + LOOP_BEATS["dip"]) % GOP_BEATS
    end = b.add(
        {
            "id": "E1",
            "kind": "end",
            "chapter": None,
            "text": lines,
            "facts": list(say.facts),
            "dip": dip,
            "honesty": honesty,
        },
        end_beats,
    )
    end["key"] = min(end["f1"] - 1, end["f0"] + dip + 2 * B)
    credit, credit_facts = b.said(brand.credit_line, "brand.yaml credit_line")
    b.tracks.chapter(end["f0"], "Credits")
    b.tracks.cue(end["f0"] + dip // 2, credit or brand.name, credit_facts)
    b.tracks.line(end["f0"] + dip // 2, "End card: " + " / ".join(t["text"] for t in lines), say.facts)
    if film.loop:  # back to pure night: frame N is frame 0
        back = b.add({"id": "D1", "kind": "dip", "chapter": None, "text": [], "facts": []}, LOOP_BEATS["dip"])
        back["key"] = back["f0"] + B
    frames = b.f
    cam.until(frames)
    head, head_facts = b.said(cfg.title, "project.yaml title")
    subtitle, subtitle_facts = b.said(cfg.subtitle, "project.yaml subtitle")

    masters: dict[str, Any] = {}
    for shot in b.shots:
        for file in (shot.get("file"), shot.get("before_file"), shot.get("after_file")):
            if file:
                mv = index.vantages[shot["vantage"]]
                masters[file] = {"sha256": hashes[file], "w": mv.width, "h": mv.height}
    poster_shot = next(
        (sh for sh in reversed(b.shots) if sh["kind"] == "capture"),
        next((sh for sh in reversed(b.shots) if sh["kind"] == "curtain"), None),
    )
    plan: dict[str, Any] = {
        "slug": project.slug,
        "cut": "story",
        "preset": film.preset,
        "renderer": FILM_RENDERER["story"],
        "width": width,
        "height": height,
        "fps": fps,
        "frames": frames,
        "loop": film.loop,
        "sound": "none",
        "grid": {"bpm": grid.bpm, "fpb": B, "dissolve": D, "beats": frames // B},
        "layout": lay.record(),
        "draft": cfg.draft,
        "simulated": cfg.simulated,
        "tag": [w for w, on in (("Simulated imagery", cfg.simulated), ("Preview", cfg.draft)) if on],
        "provenance": provenance,
        "inputs": {
            "masters": dict(sorted(masters.items())),
            "fonts": fonts,
            "colors": {
                "night": colors.night,
                "paper": colors.paper,
                "ink": colors.ink,
                "accent": colors.accent,
            },
        },
        "camera": cam.windows,
        "shots": b.shots,
        **b.tracks.record(
            frames, title=head, subtitle=subtitle, honesty=honesty, facts=[*head_facts, *subtitle_facts]
        ),
        "poster": poster_shot["f1"] - 1 if poster_shot else b.shots[0]["key"],
    }
    check_plan(plan)
    used = sorted({f for shot in b.shots for f in shot["facts"]} | set(track_facts(plan)))
    return FilmPlan(
        id=film.id,
        plan=plan,
        facts=fact_snapshot(project, used),
        warnings=warnings,
        encode=encode_settings(film, settings, frames, fps, fast=fast, fpb=B, lang=iso639_2(cfg.lang)),
    )


def _capture_tracks(
    b: _Builder,
    shot: dict[str, Any],
    cap: _Capture,
    s: _Series,
    vname: str,
    vname_facts: Sequence[str],
    *,
    first: bool,
) -> None:
    """A capture's caption cue ('<label>. <words>', plus its note when that still reads at 17
    characters per second) and transcript line (the step's full text, else the note)."""
    d = shot["dissolve"]
    start = shot["f0"] if d is None else d["f0"] + (d["f1"] - d["f0"]) // 2
    step = shot["step"]
    words = step["words"] if step else None
    words_facts: list[str] = []
    if words and cap.step is not None:
        raw, _ = lead_words(cap.step)
        _, words_facts = b.said(raw, f"chapter {s.chapter!r} step {cap.step_index} film words")
    base = sentence(cap.label, words)
    base_facts = [*cap.label_facts, *words_facts]
    more = (sentence(base, cap.note), [*base_facts, *cap.note_facts]) if cap.note else None
    b.tracks.cue(start, base, base_facts, more)
    text, text_facts = b.step_text(cap.step, f"chapter {s.chapter!r} step {cap.step_index}")
    body, body_facts = (text, text_facts) if text else (cap.note, cap.note_facts)
    lead = f"Aerial view ({vname}), {cap.label}" if first else f"Same view, {cap.label}"
    facts = [*cap.label_facts, *body_facts, *(vname_facts if first else ())]
    b.tracks.line(start, f"{lead}: {body}" if body else f"{lead}.", facts)


def _curtain_shot(
    b: _Builder, c: _Curtain, fonts: dict[str, Any], hashes: dict[str, str], part_f0: int | None = None
) -> Cam | None:
    """The curtain of a compare chapter: an entry beat at split 0, then 2 beats of sweep and 2 of hold
    per step with a split. Before is left of the rule, after right of it. Returns the camera target
    it ends on when that is not the rest camera."""
    project, lay, film = b.project, b.layout, b.film
    B = b.grid.fpb
    cam = b.camera
    f0 = b.f
    rest = b.rest(c)
    prev = b.prev
    same = (
        prev is not None
        and prev["kind"] in ("title", "capture")
        and prev.get("vantage") == c.vantage
        and prev.get("capture") == c.after.date
    )
    dissolve = None if same else b.entry()
    if prev is not None and prev["kind"] in IMAGES:
        cam.until(f0)
    cam.hold(f0 + B if prev is not None and prev["kind"] in IMAGES else f0, rest, f0 + B)
    say = b.say()
    where = f"chapter {c.chapter!r}"
    labels = []
    for raw, cap, side in zip(c.labels, (c.before, c.after), ("before", "after"), strict=True):
        label = say(raw, f"{where} {side}_label") if raw else None
        if label is None:
            label = cap.label
            say.add(cap.label_facts)
        labels.append(label)
    before_label, after_label = labels
    label_facts = list(say.facts)
    zmax = c.geometry.zmax(film.max_upscale)
    keys: list[dict[str, Any]] = [{"a": f0, "b": f0 + B, "split": 0.0}]
    steps: list[dict[str, Any]] = []
    t = f0 + B
    target = rest
    for j, step, split in c.steps:
        record: dict[str, Any] = {"index": j, "split": split, "f0": t, "words": None, "words_from": None}
        target = rest
        if step is not None:
            line = step.film.line if step.film and step.film.line else None
            words = say(line, f"{where} step {j} film words") if line else None
            if words:
                record.update(words=words, words_from="film.line", land=t)
            requested = step.film.focus if step.film and step.film.focus else step.focus
            record["focus_requested"] = list(requested) if requested else None
            if requested:
                aimed_cam, aimed = _aim_step(
                    b, c.geometry, requested, bool(step.film and step.film.focus), [], rest, zmax,
                    f"{c.chapter} step {j}",
                )  # fmt: skip
                record.update(aimed)
                if aimed["push"]:
                    target = aimed_cam
        cam.until(t)
        cam.hold(t + 2 * B, target, t + 4 * B)
        keys.append({"a": t + 2 * B, "b": t + 4 * B, "split": split})
        steps.append(record)
        t += 4 * B
    # Labels show only while the rule (3 units wide) is at least 8 px clear of their ink.
    tagged = project.config.simulated or project.config.draft
    scale, label_top, (wb, wa) = _curtain_labels(project, fonts, lay, [before_label, after_label], tagged)
    ink = [lay.margin + wb, lay.right - wa]
    half = 1.5 * lay.unit
    from vantage.film.beat import Window, keyed, lerp

    windows = [Window(k["a"], k["b"]) for k in keys]
    splits = [k["split"] for k in keys]
    xs = [keyed(windows, splits, f, lerp) * lay.width for f in range(f0, t)]
    reveal = [
        _intervals([x - half >= ink[0] + 8 for x in xs], f0),
        _intervals([x + half <= ink[1] - 8 for x in xs], f0),
    ]
    k = sum(s["kind"] == "curtain" for s in b.shots) + 1
    text = [{"role": "before", "text": before_label}, {"role": "after", "text": after_label}]
    text += [{"role": "words", "text": s["words"]} for s in steps if s["words"]]
    shot = b.add(
        {
            "id": f"C{k}",
            "kind": "curtain",
            "chapter": c.chapter,
            "text": text,
            "facts": list(say.facts),
            "vantage": c.vantage,
            "before": c.before.date,
            "after": c.after.date,
            "before_file": c.before.file,
            "after_file": c.after.file,
            "labels": [before_label, after_label],
            "label_scale": scale,
            "label_top": label_top,
            "label_ink": ink,
            "reveal": reveal,
            "dissolve": dissolve,
            "from_after": same,
            "keys": keys,
            "steps": steps,
        },
        1 + 4 * len(steps),
    )
    best = min(range(len(steps)), key=lambda s: abs(steps[s]["split"] - 0.5))
    shot["key"] = keys[best + 1]["a"] + B
    _curtain_tracks(b, c, shot, label_facts, f0 if part_f0 is None else part_f0)
    for file in (c.before.file, c.after.file):
        if file not in hashes:
            hashes[file] = _sha256(project.masters_dir / file)
    return None if target == rest else target


def _curtain_tracks(
    b: _Builder, c: _Curtain, shot: dict[str, Any], label_facts: Sequence[str], part_f0: int
) -> None:
    """A curtain's chapter (the compare title), one caption cue per step ('<before> / <after>: <step
    text>' when that reads at 17 characters per second, else '<before> / <after>') and one
    transcript line describing the split and where the line moves."""
    before, after = shot["labels"]
    d = shot["dissolve"]
    start = shot["f0"] if d is None else d["f0"] + (d["f1"] - d["f0"]) // 2
    where = f"chapter {c.chapter!r}"
    vname, vname_facts = b.said(c.name, f"vantage {c.vantage} name")
    vname = vname or c.name
    heading, heading_facts = b.said(c.title, f"{where} title")
    b.tracks.chapter(part_f0, heading or f"{before} / {after}", heading_facts if heading else label_facts)
    pair = f"{before} / {after}"
    describe = f"Split screen ({vname}), {before} left and {after} right."
    facts = [*vname_facts, *label_facts]
    for k, ((j, step, split), record) in enumerate(zip(c.steps, shot["steps"], strict=True)):
        text, text_facts = b.step_text(step, f"{where} step {j}")
        more = (f"{pair}: {text}", [*label_facts, *text_facts]) if text else None
        b.tracks.cue(start if k == 0 else record["f0"], pair, label_facts, more)
        moves = "The line moves to" if k == 0 else "Then to"
        describe += f" {moves} {round(split * 100)}%" + (f": {text}" if text else ".")
        facts += text_facts
    b.tracks.line(start, describe, facts)


__all__ = [
    "HONESTY",
    "Geometry",
    "aim",
    "derive_cut",
    "honesty_line",
    "in_scope",
    "lead_words",
    "main_vantage",
]
