"""The story cut: a film's shots derived from story.yaml's chapters on a beat grid (film/story.py).

`derive_cut` turns the chapters in a film's scope into shots, in story order:

    title    the first image at rest under a night scrim; the kicker tracks out across beat 0 and
             the title's word groups rise on frames 0, B/3 and 2B/3 of beat 1
    capture  one per capture of each scrub chapter (from..to, minus `exclude`d captures): 1 beat
             for a quiet capture, 3 for a step, 4 for a step that pushes in. Every capture but the
             film's first image opens with a dissolve; the step's words (film.line, else its
             leading **bold** run) land on beat 1, in the zone (`_Placer`: low or high, or low-right
             in 16:9) clear of the step's subject, and a push reaches its target at beat 2⅓
    curtain  one per compare chapter: an entry beat at split 0 (a dissolve to `after` unless it is
             already on screen), then 2 beats of sweep and 2 of hold per step with a split; the
             labels show only while the rule is 8 px clear of their ink (`reveal`)
    stat     up to `stats` items of the first stats chapter whose value is a {fact:} token, at least
             4 beats each, over the main vantage's latest picture (the one the curtain ends on) under
             a panel tinted by the chapter's surface; the first dissolves in, the next start on the
             beat and only their text crossfades; a stat whose fact a hotspot prints pushes toward
             it. The figure is the fact's text and never counts. (`stats_surface`: a plain card.)
    montage  one per gallery chapter on a drone vantage other than the main one: up to `montage` (5)
             of its captures spread evenly from first to last, 2 beats each, dissolving on the beat
             with no card; its kicker (or film.line, or title) lands over the first picture on beat
             1; no push, a 4 % drift over the run
    card     between consecutive series or curtains of different vantages: the chapter's kicker and
             title on night, dipped in and out
    end      one card: a dip to night, then the brand, credits, URL, the honesty line and fine
             print, at most END_MAX_S with its dip when it reads in that time; else the honesty
             line rides under the stats

A loop film (the `screen` preset) opens with a `rise` from pure night onto the title's image (the
rail's knob sweeping back to its first tick) and closes with a `dip` back to pure night, so frame N
(one past the last) equals frame 0. Its end card is only the brand and the short URL, every text
it prints needs releasable facts (unless the project is a draft), and its end card is lengthened by a beat
when needed so the film is a whole number of GOPs (2 beats).

Where a montage or a stat run dissolves in from another vantage's picture, that picture keeps its
last framing through the dissolve (`dissolve.cam`) and the camera cuts to the new rest view.

Every shot start, dissolve start, sweep, push start and word landing is a multiple of B
(film/beat.py). Push targets are clamped so a master is never enlarged beyond `max_upscale` and
recentred on the hotspots that changed since the previous step. A main vantage with one or two
captures degrades to title → capture (→ curtain) → stats → end. Dips to or from paper last
`surface_dip` frames so a paper card never flashes.

`derive_cut` keeps every film's flash margin (film/margin.py): it plans and meters a small proxy of
the cut, then holds a capture, a curtain or a stat a beat longer, softens a push or slows it where
too many flash transitions fall within a second (`_Builder.holds`, `caps`, `slows`), and records
what it did as plan `flash`.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from vantage.config import (
    FACT_TOKEN_RE,
    CaptureNote,
    CompareChapter,
    GalleryChapter,
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
from vantage.film.safety import check_plan, is_image, surface_dip
from vantage.film.tracks import Tracks, cue_frames, iso639_2, plain_markdown, sentence, track_facts
from vantage.models import FilmPlan, MastersIndex
from vantage.site.build import resolve_capture_ref
from vantage.site.facts import FactNotes
from vantage.site.render import capture_label as date_label

LEAD_RE = re.compile(r"^\s*\*\*(.+?)\*\*", re.S)
SOFT = 1.25  # output px per master px beyond which a push is "soft"
MIN_ZOOM, MIN_SHIFT = 1.08, 0.03  # below both, a clamped push becomes a still step
Cam = tuple[float, float, float]


def lead_words(step: Step) -> tuple[str | None, str | None]:
    """(raw words, 'film.line' | 'lead') a step puts on screen in films, or (None, None)."""
    if step.film is not None and step.film.line:
        return step.film.line, "film.line"
    m = LEAD_RE.match(step.text)
    return (m.group(1).strip(), "lead") if m else (None, None)


FIRST_SENTENCE_RE = re.compile(r"^\s*([^.!?]+[.!?])(?:\s|$)")
CURTAIN_SENTENCE_MAX = 40  # characters: a compare step's first sentence goes on screen up to this


def curtain_words(step: Step, measure: Callable[[str], int] = len) -> tuple[str | None, str | None]:
    """(raw words, 'film.line' | 'lead' | 'sentence') a compare step puts on screen: film.line,
    else its **bold** lead, else its first sentence when that prints at most 40 characters
    (`measure`: the printed length of a raw text, its facts resolved)."""
    raw, source = lead_words(step)
    if raw:
        return raw, source
    m = FIRST_SENTENCE_RE.match(step.text.replace("\n", " "))
    if m and measure(m.group(1)) <= CURTAIN_SENTENCE_MAX and "**" not in m.group(1):
        return m.group(1).strip(), "sentence"
    return None, None


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


Box = tuple[float, float, float, float]  # u0, u1, v0, v1: where subjects may land, as frame fractions


def subject_box(lay: Layout) -> Box:
    """Where a push may put its subjects on screen: inside the safe zone (1 % in from its sides),
    below the date label and above the lead words, so the words never cover what the camera frames."""
    W, H, u = lay.width, lay.height, lay.unit
    return (
        lay.margin / W + 0.01,
        lay.right / W - 0.01,
        (lay.label_top + 64 * u) / H,
        (lay.lead_baseline - 110 * u) / H,
    )


def _fit(
    geo: Geometry, fx: float, fy: float, zoom: float, pts: Sequence[tuple[float, float]], box: Box
) -> Cam | None:
    """A crop at `zoom` nearest to (fx, fy) that puts every point inside `box` on screen, or None."""
    hw, hh = geo.half(zoom)
    u0, u1, v0, v1 = box
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    # A point p lands at screen fraction u when the crop centre is p − (2u − 1)·hw.
    x_lo, x_hi = max(max(xs) - (2 * u1 - 1) * hw, hw), min(min(xs) - (2 * u0 - 1) * hw, 1 - hw)
    y_lo, y_hi = max(max(ys) - (2 * v1 - 1) * hh, hh), min(min(ys) - (2 * v0 - 1) * hh, 1 - hh)
    if x_lo > x_hi + 1e-12 or y_lo > y_hi + 1e-12:
        return None
    cx, cy = geo.centre(fx, fy, zoom)
    return min(max(cx, x_lo), x_hi), min(max(cy, y_lo), y_hi), zoom


MAX_TRAVEL = 0.5  # a push moves the crop centre at most this share of the rest crop's width (height)


def aim(
    geo: Geometry,
    focus: Sequence[float],
    zmax: float,
    subjects: Sequence[Hotspot],
    *,
    explicit: bool,
    rest: Sequence[float] = (0.5, 0.5),
    box: Box = (0.06, 0.94, 0.06, 0.94),
) -> tuple[Cam, str]:
    """(x, y, zoom) of a push and how it was chosen: 'film.focus' (as given), 'focus' (the step's
    focus), 'focus+subject:<ids>' (the crop nearest the focus that puts the subjects inside `box`),
    or 'focus, subject off-frame:<ids>' (no such crop within MAX_TRAVEL of the rest view: the
    step's focus). Zoom is clamped to [1, zmax]; subjects lower it toward 1 until they fit. Except
    for film.focus, the crop centre never travels more than MAX_TRAVEL of the rest crop from the
    rest view, so a push never whips across the master to reach a subject the viewer cannot see."""
    fx, fy, z = focus[0], focus[1], min(max(focus[2], 1.0), zmax)
    if explicit:
        return (fx, fy, z), "film.focus"
    a = geo.centre(rest[0], rest[1], 1.0)
    hw0, hh0 = geo.half(1.0)
    reach = (MAX_TRAVEL * 2 * hw0, MAX_TRAVEL * 2 * hh0)

    def near(cam: Cam) -> bool:
        c = geo.centre(*cam)
        return abs(c[0] - a[0]) <= reach[0] + 1e-9 and abs(c[1] - a[1]) <= reach[1] + 1e-9

    def capped(x: float, y: float, zoom: float) -> Cam:
        c = geo.centre(x, y, zoom)
        return (
            min(max(c[0], a[0] - reach[0]), a[0] + reach[0]),
            min(max(c[1], a[1] - reach[1]), a[1] + reach[1]),
            zoom,
        )

    if not subjects:
        return capped(fx, fy, z), "focus"
    pts = [(h.x, h.y) for h in subjects]
    names = ",".join(h.id for h in subjects)
    for k in range(33):  # from the clamped zoom down to 1 in 32 even steps
        zoom = z - (z - 1.0) * k / 32
        cam = _fit(geo, fx, fy, zoom, pts, box)
        if cam is not None and near(cam):
            return cam, f"focus+subject:{names}"
    return capped(fx, fy, z), f"focus, subject off-frame:{names}"


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
    before: str | None = None  # the vantage's previous capture's file (what changed comes from it)


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
    masters_name: str = ""  # the fallback of a name that cannot be printed (do-not-print fact)


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
    """Indices of the chapters a film uses: all, main (the main vantage's chapters, stats and gallery
    chapters, whose montages show other vantages) or ids."""
    ids = _chapter_ids(project)
    chapters = project.story.chapters
    if film.chapters == "all":
        return list(range(len(chapters)))
    if film.chapters == "main":  # plus the montages of the other vantages' gallery chapters
        main = main_vantage(project, index)
        return [
            i
            for i, ch in enumerate(chapters)
            if getattr(ch, "vantage", None) == main
            or ch.type == "stats"
            or (isinstance(ch, GalleryChapter) and ch.vantage is not None)
        ]
    wanted = list(film.chapters)
    unknown = [c for c in wanted if c not in ids]
    if unknown:
        warnings.append(f"films.{film.id}.chapters: unknown chapter id(s) {', '.join(unknown)}; ignored")
    return [i for i, cid in enumerate(ids) if cid in wanted]


class _Captures:
    """Every vantage's non-excluded captures with labels as the site prints them."""

    def __init__(self, project: Project, index: MastersIndex, warnings: list[str], strict: bool = False):
        self.project, self.index, self.warnings, self.strict = project, index, warnings, strict
        self.notes: dict[str, CaptureNote] = {n.date.isoformat(): n for n in project.story.captures}
        self.excluded = {d for d, n in self.notes.items() if n.exclude}

    def of(self, vid: str) -> list[_Capture]:
        mv = self.index.vantages[vid]
        caps = sorted((c for c in mv.captures if c.date not in self.excluded), key=lambda c: c.date)
        months = Counter(c.date[:7] for c in caps)
        out = []
        for i, c in enumerate(caps):
            say = _Printer(self.project, self.warnings, releasable_only=self.strict)
            entry = self.notes.get(c.date)
            label = say(entry.label if entry else None, f"captures {c.date} label")
            label = label or date_label(c.date, day=months[c.date[:7]] > 1)
            note, note_facts = said(
                self.project,
                entry.note if entry else None,
                f"captures {c.date} note",
                self.warnings,
                releasable_only=self.strict,
            )
            before = caps[i - 1].file if i else None
            out.append(_Capture(c.date, c.file, label, list(say.facts), i, note, note_facts, before=before))
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
        _geometry(project, caps.index, vid, *size), (pf[0], pf[1]), caps.index.vantages[vid].name,
    )  # fmt: skip


def _fallback_series(project: Project, caps: _Captures, vid: str, size: tuple[int, int]) -> _Series:
    """No scrub chapter in scope: the main vantage plays as one quiet series."""
    vantage = next((v for v in project.story.vantages if v.id == vid), None)
    pf = vantage.portrait_focus if vantage and vantage.portrait_focus else (0.5, 0.5)
    name = (vantage.name if vantage else None) or caps.index.vantages[vid].name
    geo = _geometry(project, caps.index, vid, *size)
    return _Series(
        None, vid, name, None, None, caps.of(vid), geo, (pf[0], pf[1]), caps.index.vantages[vid].name
    )


@dataclass
class _Montage(_Series):
    """A gallery chapter of another drone vantage as a montage: `captures` spread evenly over the
    chapter's captures (first and last included), and the words over its first picture."""

    words: str | None = None  # raw: film.line, else the kicker, else the title
    words_from: str | None = None  # film.line | kicker | title


def _montage(
    project: Project,
    chapter_index: int,
    ch: GalleryChapter,
    caps: _Captures,
    size: tuple[int, int],
    *,
    main: str | None,
    count: int,
) -> _Montage | None:
    """A gallery chapter's montage, or None: no vantage, the main vantage (it plays as the scrub),
    `film.skip`, a montage cap of 0, a vantage films do not show (not a drone) or one with fewer than
    two captures (a warning)."""
    vid = ch.vantage
    if vid is None or vid == main or count < 2 or (ch.film is not None and ch.film.skip):
        return None
    vantage = next((v for v in project.story.vantages if v.id == vid), None)
    if vantage is not None and vantage.kind != "drone":
        return None  # film_vantages has warned
    where = f"chapter {_chapter_ids(project)[chapter_index]!r}"
    if vid not in caps.index.vantages:
        caps.warnings.append(f"{where}: vantage {vid!r} has no masters; left out of the films")
        return None
    every = caps.of(vid)
    if ch.captures and every:
        dates = [c.date for c in every]
        every = [every[k] for k in sorted({resolve_capture_ref(r, dates) for r in ch.captures})]
    if len(every) < 2:
        caps.warnings.append(f"{where}: fewer than two captures for a montage; left out of the films")
        return None
    n = min(count, len(every))
    last = len(every) - 1
    picks = sorted({(2 * k * last + n - 1) // (2 * (n - 1)) for k in range(n)})  # k·last/(n−1), half up
    words, source = ch.title, "title"
    if ch.film is not None and ch.film.line:
        words, source = ch.film.line, "film.line"
    elif ch.kicker:
        words, source = ch.kicker, "kicker"
    name, pf = _vantage_info(project, caps, vid)
    return _Montage(
        _chapter_ids(project)[chapter_index], vid, name, ch.kicker, ch.title, [every[k] for k in picks],
        _geometry(project, caps.index, vid, *size), pf, caps.index.vantages[vid].name,
        words=words, words_from=source if words else None,
    )  # fmt: skip


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
    masters_name: str = ""  # the fallback of a name that cannot be printed (do-not-print fact)


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
        steps, _geometry(project, caps.index, vid, *size), pf, caps.index.vantages[vid].name,
    )  # fmt: skip


def _stat_items(
    project: Project,
    cid: str,
    ch: StatsChapter,
    limit: int | tuple[str, ...],
    warnings: list[str],
    *,
    releasable_only: bool = False,
    film: str = "",
) -> list[dict[str, Any]]:
    """Up to `limit` stats whose value is a {fact:} token (or, when `limit` names fact ids, the items
    whose value prints those facts, in that order), printed through the facts. Any other item, and
    any item that uses a do-not-print fact, is left out with a warning, as is (with
    `releasable_only`, loop films of a project that is not a draft) any item that prints a fact
    that is not releasable. Values never count."""
    out: list[dict[str, Any]] = []
    order = list(enumerate(ch.items))
    if isinstance(limit, tuple):  # by fact id, in the film's order
        named = {fid: k for k, item in order for fid in FACT_TOKEN_RE.findall(item.value)}
        missing = [fid for fid in limit if fid not in named]
        if missing:
            warnings.append(
                f"films.{film}.stats: no item of chapter {cid!r} prints {', '.join(missing)} in its value; "
                "left out"
            )
        order = [(named[fid], ch.items[named[fid]]) for fid in limit if fid in named]
        limit = len(order)
    for k, item in order:
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

    def at(self, f: int) -> Cam:
        """The camera on frame f (as film/cut.py evaluates it)."""
        from vantage.film.beat import Window, keyed, mix_cam

        wins = [Window(w["a"], w["b"]) for w in self.windows]
        cam = keyed(wins, [(w["x"], w["y"], w["zoom"]) for w in self.windows], f, mix_cam)
        return cam[0], cam[1], cam[2]

    def cut(self, f: int, cam: Cam) -> None:
        """Cut to `cam` on frame f: the windows before hold through f − 1, so no frame shows a
        camera in between (only on a dissolve between two vantages, see `_Builder.cross`)."""
        self.until(f - 1)
        self.hold(f, cam)


@dataclass
class _Builder:
    project: Project
    film: FilmDef
    grid: Grid
    layout: Layout
    warnings: list[str]
    paper_dip: int
    px: float = 1.0  # this film's px per px of the full-size film (< 1 under VANTAGE_FAST)
    shots: list[dict[str, Any]] = field(default_factory=list)
    f: int = 0  # the next shot's first frame (always on the grid)
    camera: _Camera = field(default_factory=_Camera)
    tracks: Tracks = field(default_factory=lambda: Tracks(1, enabled=False))
    placer: _Placer | None = None  # chooses the words' zones (None: always the default zone)
    # The flash margin (film/margin.py): beats held at a slot (a shot id, "<curtain id>.<k>" or
    # "<first stat id>.0"), beats added to a capture's push (and its ease back) or a curtain step's
    # move, and the share of its move a push keeps (by push key).
    holds: dict[str, int] = field(default_factory=dict)
    slows: dict[str, int] = field(default_factory=dict)
    caps: dict[str, float] = field(default_factory=dict)
    back: int | None = None  # frames the next ease back from a push takes (None: `ease`)
    # What depends on where text sets, decided at the full size (`decide`): fixed inputs, and this
    # plan's own decisions (recorded for the proxy and a VANTAGE_FAST plan).
    decided: dict[str, Any] | None = None
    decisions: dict[str, Any] = field(default_factory=dict)

    def decide(self, key: str, compute: Callable[[], Any]) -> Any:
        """The decision `key` (a words zone, a stat run's place and aims, a push target): as decided
        at the full size when given (`decided`), else computed here; recorded either way."""
        import copy

        if self.decided is not None and key in self.decided:
            value = copy.deepcopy(self.decided[key])
        else:
            value = compute()
        self.decisions[key] = copy.deepcopy(value)
        return value

    @property
    def strict(self) -> bool:
        """An unattended loop film of a project that is not a draft prints only releasable facts."""
        return self.film.loop and not self.project.config.draft

    def say(self) -> _Printer:
        return _Printer(self.project, self.warnings, releasable_only=self.strict)

    def said(self, raw: str | None, where: str) -> tuple[str | None, list[str]]:
        """(raw text printed through the facts, its fact ids), or (None, []) without it."""
        return (
            said(self.project, raw, where, self.warnings, releasable_only=self.strict) if raw else (None, [])
        )

    def step_text(self, step: Step | None, where: str) -> tuple[str | None, list[str]]:
        """A step's full text as plain text (tracks only), printed through the facts."""
        text, facts = self.said(step.text if step else None, f"{where} text")
        return (plain_markdown(text) or None, facts) if text else (None, [])

    def add(self, shot: dict[str, Any], beats: int) -> dict[str, Any]:
        shot["f0"], shot["f1"] = self.f, self.f + beats * self.grid.fpb
        self.f = shot["f1"]
        self.shots.append(shot)
        return shot

    @property
    def ease(self) -> int:
        """Frames a push takes to go in, and to ease back: B + B/3, or 2 beats in a loop film, whose
        unattended screen should move calmly and whose meter must pass at full size (a fast pull back
        sweeps a bright path through a region more than 3 times a second)."""
        return 2 * self.grid.fpb if self.film.loop else self.grid.fpb + self.grid.third

    def push_ease(self, sid: str) -> int:
        """Frames the push of shot `sid` takes: `ease`, plus a beat per level the flash margin slowed
        it (`slows`); it eases back as slowly (`back`)."""
        return self.ease + self.slows.get(sid, 0) * self.grid.fpb

    @property
    def easing_back(self) -> int:
        """Frames the ease back from the last push takes (as slowly as it went in)."""
        return self.ease if self.back is None else self.back

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
        if prev["kind"] == "stat" and not prev.get("file"):
            n = self.paper_dip if prev["surface"] == "paper" else D
            return {"from": prev["surface"], "f0": f0, "f1": f0 + n}
        return {
            "from": prev.get("capture") if prev["kind"] == "capture" else prev["id"],
            "f0": f0,
            "f1": f0 + D,
        }

    def cross(self, vantage: str, dissolve: dict[str, Any] | None, rest: Cam) -> bool:
        """A dissolve from another vantage's picture: that picture keeps the framing of its last frame
        through the dissolve (`dissolve.cam`), and the camera cuts to this shot's `rest` on its
        first frame, so one vantage's push never frames the other. Returns whether it did."""
        prev = self.prev
        if dissolve is None or not is_image(prev) or prev is None or prev.get("vantage") in (None, vantage):
            return False
        dissolve["cam"] = list(self.camera.at(self.f - 1))
        self.camera.cut(self.f, rest)
        return True

    def dip(self, surface: str) -> int:
        """Frames of the dip into a `surface` shot starting now."""
        prev = self.prev
        theirs = None
        if prev is not None and prev["kind"] in ("card", "end"):
            theirs = "night"
        elif prev is not None and prev["kind"] == "stat" and not prev.get("file"):
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
    # All in by 2B; held until kicker and title read at 17 characters a second, the kicker first (it
    # is in from B/3, while the title rises), then the title once it is in; out over the last beat.
    fps = b.grid.fps
    read_kicker = third + cue_frames(kicker, fps) if kicker else 0
    need = max(2 * B, read_kicker) + (cue_frames(title, fps) if title else 0) + B
    b.add(shot, max(b.film.title_beats, -(-need // B)))
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
    box: Box | None = None,
    key: str = "",
) -> tuple[Cam, dict[str, Any]]:
    """A step's camera target, clamped by max_upscale (with warnings), and its plan record. `box`:
    where its subjects may land (default: above the low words, `subject_box`). The flash margin may
    soften push `key` to a share of its move (`b.caps`, recorded as `gentled`): it asks for that
    share of the log zoom it would get (then aims as usual), and a push that zooms under MIN_ZOOM (a pan)
    moves that share of the way from the rest view."""
    amount = b.caps.get(key, 1.0)
    asked = requested
    if amount < 1.0:  # a share of the zoom it would get (after the max_upscale clamp)
        requested = (
            requested[0],
            requested[1],
            math.exp(amount * math.log(min(max(requested[2], 1.0), zmax))),
        )
    if b.decided is not None and f"aim:{label}" in b.decided:  # aimed at the full size
        cam, target = b.decide(f"aim:{label}", lambda: None)
        cam = (cam[0], cam[1], cam[2])
    else:
        cam, target = aim(
            geo, requested, zmax, [] if explicit else subjects, explicit=explicit, rest=rest,
            box=subject_box(b.layout) if box is None else box,
        )  # fmt: skip
        b.decisions[f"aim:{label}"] = [list(cam), target]
    if amount < 1.0 and cam[2] < MIN_ZOOM:
        cam = (rest[0] + amount * (cam[0] - rest[0]), rest[1] + amount * (cam[1] - rest[1]), cam[2])
    off = target.startswith("focus, subject off-frame")
    if off:  # pushing to the focus alone would frame what the step does not name: hold instead
        b.warnings.append(
            f"{label}: subject {target.split(':', 1)[1]} cannot be framed above the words within reach "
            f"of the rest view ({b.film.id}); the step holds its picture instead of pushing"
        )
    z = max(asked[2], 1.0)
    if zmax < z - 1e-9 and not off:
        b.warnings.append(
            f"{label}: push clamped from {z:g} to {zmax:.3g} ({b.film.id}; max_upscale {b.film.max_upscale:g})"
        )
    upscale = geo.s0 * cam[2]
    if (
        upscale / b.px > SOFT + 1e-9 and not off
    ):  # judged at the full size, so a VANTAGE_FAST plan warns alike
        b.warnings.append(f"{label}: soft push, {upscale / b.px:.2f} output px per master px ({b.film.id})")
    a, c = geo.centre(rest[0], rest[1], 1.0), geo.centre(cam[0], cam[1], cam[2])
    push = not off and not (cam[2] < MIN_ZOOM and math.hypot(c[0] - a[0], c[1] - a[1]) < MIN_SHIFT)
    return cam, {
        "focus": [cam[0], cam[1], cam[2]],
        **({"gentled": amount} if amount < 1.0 else {}),
        "target": target,
        "subjects": [] if explicit else [h.id for h in subjects],
        "z_eff": cam[2],
        "upscale": upscale,
        "push": push,
        "reason": None if push else "subject off-frame" if off else "clamped",
    }


def _push(
    b: _Builder, series: _Series, cap: _Capture, rest: Cam, zmax: float, box: Box | None = None, sid: str = ""
) -> tuple[dict[str, Any] | None, Cam | None]:
    """The step record of a capture and its push target (None without a push); `box`: where the
    push may put its subjects (the words' zone decides it)."""
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
        b, series.geometry, requested, explicit, cap.subjects, rest, zmax, f"{series.chapter} {cap.date}", box,
        sid,
    )  # fmt: skip
    record.update(aimed)
    return record, (cam if aimed["push"] else None)


# --------------------------------------------------------------------------- #
# Where a step's words sit
# --------------------------------------------------------------------------- #

# How much of the subject on screen a zone's words cover for their size (film/subject.py `under`; 1:
# as much as anywhere on screen): a zone is clear at most WORDS_CLEAR, and the words keep their zone
# unless another one covers WORDS_MARGIN less (so lines do not hop between zones for a few
# per cent); words over more than WORDS_ON of it sit on the subject.
WORDS_CLEAR, WORDS_MARGIN, WORDS_ON = 0.5, 0.25, 1.0
WORDS_CONTRAST = 3.0  # WCAG 1.4.3 for large text: the words (120 units) over the picture behind their scrim


def _crop(geo: Geometry, cam: Sequence[float]) -> tuple[float, float, float, float]:
    """The part of the master a camera shows, as master fractions (x0, y0, x1, y1)."""
    hw, hh = geo.half(cam[2])
    cx, cy = geo.centre(cam[0], cam[1], cam[2])
    return cx - hw, cy - hh, cx + hw, cy + hh


def _path(windows: list[dict[str, Any]], geo: Geometry, f0: int, f1: int, step: int) -> list[Any]:
    """The crops a camera (hold windows) shows on frames f0, f0 + step, … and f1 − 1."""
    from vantage.film.beat import Window, keyed, mix_cam

    wins = [Window(w["a"], w["b"]) for w in windows]
    cams = [(w["x"], w["y"], w["zoom"]) for w in windows]
    frames = sorted({*range(f0, f1, max(1, step)), f1 - 1})
    return [_crop(geo, keyed(wins, cams, f, mix_cam)) for f in frames]


class _Placer:
    """Chooses each step's words zone (`cut.words_set`) from where its subject is on screen while
    the words are up (film/subject.py), and the contrast they keep over the picture."""

    def __init__(self, project: Project, fonts: dict[str, Any], lay: Layout, seen: Any = None) -> None:
        from vantage.film.cut import DATE, _cap_of, words_zones
        from vantage.film.render import _hex
        from vantage.film.subject import Seen

        self.lay = lay
        self.t = _stat_type(project, fonts, lay)
        self.seen = seen or Seen(project.masters_dir)
        self.zones = words_zones(lay)
        self.label_base = lay.label_top + _cap_of(self.t, DATE)  # a capture's date label baseline
        c = project.brand.colors
        self.paper, self.night = _hex(c.paper), _hex(c.night)

    def curtain_base(self, label_top: float, scale: float) -> float:
        """The baseline of a curtain's labels (film/cut.py `_typeset_curtain`)."""
        from vantage.film.cut import CURTAIN_LABEL, _cap_of

        return label_top + _cap_of(
            self.t, dataclasses.replace(CURTAIN_LABEL, size=CURTAIN_LABEL.size * scale)
        )

    def set(self, words: str, zone: str, label_base: float, scale: float | None = None) -> Any:
        from vantage.film.cut import words_set

        return words_set(self.t, self.lay, words, zone, label_base, scale)

    def aim_box(self, words: str, zone: str, label_base: float) -> Box:
        """Where a push may put its subjects with the words in `zone`: above the low words
        (`subject_box`), or below the high words and above the rail."""
        from vantage.film.cut import RISE

        if zone != "high":
            return subject_box(self.lay)
        lay, u = self.lay, self.lay.unit
        u0, u1, _, _ = subject_box(lay)
        bottom = self.set(words, zone, label_base).box[3]
        return u0, u1, (bottom + (RISE + 40) * u) / lay.height, (lay.rail_y - 40 * u) / lay.height

    def box(
        self, words: str, zone: str, label_base: float, scale: float | None = None
    ) -> tuple[float, float, float, float]:
        """The words' ink in `zone` with their scrim's padding and rise, as frame fractions."""
        from vantage.film.cut import RISE, WORDS_PAD

        lay, u = self.lay, self.lay.unit
        x0, y0, x1, y1 = self.set(words, zone, label_base, scale).box
        px, py = WORDS_PAD[0] * u, WORDS_PAD[1] * u
        return (
            (x0 - px) / lay.width, (y0 - py) / lay.height,
            (x1 + px) / lay.width, (y1 + py + RISE * u) / lay.height,
        )  # fmt: skip

    def choose(
        self,
        b: _Builder,
        label: str,
        words: str,
        label_base: float,
        subject: Any,
        files: Sequence[str],
        paths: dict[str, list[Any]],
        default: str,
        scale: float | None = None,
    ) -> dict[str, Any]:
        """The zone for a step's words and why: `default` (the step before's zone, else the first)
        while it is clear (`under` at most WORDS_CLEAR) or within WORDS_MARGIN of the least, else
        the zone over the least of the subject (ties: zone order); a zone whose words keep less
        than WORDS_CONTRAST over the picture is chosen only when every zone does. `paths`: the
        crops the camera shows in each zone while the words are up; `files`: the pictures under
        them. Warns when even that zone sits on the subject (`under` over WORDS_ON) or misses the
        contrast."""
        from vantage.film.cut import WORDS_SCRIM
        from vantage.film.subject import contrast, under

        on, ratio = {}, {}
        for zone in self.zones:
            box = self.box(words, zone, label_base, scale)
            on[zone] = round(under(subject, paths[zone], box), 3)
            ratio[zone] = round(
                min(
                    contrast(self.seen, f, paths[zone], box, self.paper, self.night, WORDS_SCRIM)
                    for f in files
                ),
                2,
            )
        legible = [z for z in self.zones if ratio[z] >= WORDS_CONTRAST] or list(self.zones)
        order = {z: k for k, z in enumerate(self.zones)}
        if subject is None:
            zone, reason = (default if default in legible else legible[0]), "no subject"
        else:
            best = min(legible, key=lambda z: (on[z], order[z]))
            if default in legible and on[default] <= WORDS_CLEAR:
                zone, reason = default, "clear"
            elif default in legible and on[default] <= on[best] + WORDS_MARGIN:
                zone, reason = default, "near least subject"
            else:
                zone, reason = best, "least subject"
        out = {"words_zone": zone, "words_reason": reason, "words_subject": on, "words_contrast": ratio}
        self.warn(b, label, words, out)
        return out

    @staticmethod
    def warn(b: _Builder, label: str, words: str, record: dict[str, Any]) -> None:
        """Warn when a zone record's words sit on the subject or miss WORDS_CONTRAST."""
        zone, on, ratio = record["words_zone"], record["words_subject"], record["words_contrast"]
        if on[zone] > WORDS_ON:
            b.warnings.append(
                f"{label}: the words {words!r} sit on the subject in every zone ({b.film.id}; "
                f"{zone} covers {on[zone]:.1f}× its share of it)"
            )
        if ratio[zone] < WORDS_CONTRAST:
            b.warnings.append(
                f"{label}: the words {words!r} keep {ratio[zone]:.1f}:1 over the picture "
                f"({b.film.id}; WCAG 1.4.3 asks {WORDS_CONTRAST:g}:1 of large text)"
            )

    def zone(
        self, b: _Builder, label: str, words: str, choose: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """The zone record of `label`'s words: decided at the full size (warned again here), or chosen."""
        key = f"zone:{label}"
        if b.decided is not None and key in b.decided:
            record = b.decide(key, choose)
            self.warn(b, label, words, record)
            return record
        return b.decide(key, choose)


def _card(b: _Builder, chapter: str | None, kicker: str | None, title: str | None, part: _Series | _Curtain):
    vid = part.vantage
    k, k_facts = b.said(kicker, f"chapter {chapter!r} kicker")
    t, t_facts = b.said(title, f"chapter {chapter!r} title")
    if not t:
        t, t_facts = b.said(part.name, f"vantage {vid} name")
    t = t or part.masters_name  # the vantage name uses a do-not-print fact
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
    label fits between its text edge and a rule at the centre, and the row sits a label line under a
    top-right tag when there is one."""
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
    if tagged and lay.tag == "top-right":  # a label line's height under the tag: two blocks, not one
        top += round(-t.font(TAG).getbbox("H", anchor="ls")[1] + CURTAIN_LABEL.size * lay.unit)
    return scale, top, [t.width(text, CURTAIN_LABEL, scale) for text in texts]


def _intervals(flags: Sequence[bool], f0: int) -> list[list[int]]:
    out: list[list[int]] = []
    for k, on in enumerate(flags):
        if on and (not out or out[-1][1] != f0 + k):
            out.append([f0 + k, f0 + k + 1])
        elif on:
            out[-1][1] += 1
    return out


# --------------------------------------------------------------------------- #
# Stats and the end card
# --------------------------------------------------------------------------- #

STAT_ZOOM = 1.6  # the zoom a stat's push asks for (clamped by max_upscale, lowered to frame its hotspot)
DRIFT = 1.04  # a stat without a hotspot to push to drifts in this much over its shot
STAT_MIN_BEATS = 4
END_MAX_S = 6.0  # the end card, its dip included, is at most this long when its text allows it


@dataclass(frozen=True)
class _StatsOn:
    """The picture a run of stats sits on: the main vantage's latest capture (the picture the
    curtain ends on), its geometry and portrait focus, and its hotspots that print a fact."""

    vantage: str
    capture: _Capture
    geometry: Geometry
    portrait_focus: tuple[float, float]
    hotspots: list[tuple[Hotspot, frozenset[str]]]


def _stat_hotspots(
    project: Project, caps: _Captures, vid: str, cap: _Capture
) -> list[tuple[Hotspot, frozenset[str]]]:
    """The hotspots of the scrub and compare chapters on `vid` that are shown at `cap` and print a
    fact in their label or body, with those fact ids (the first of each id, in story order)."""
    dates = [c.date for c in caps.of(vid)]
    at, last = dates.index(cap.date), len(dates) - 1
    seen: set[str] = set()
    out: list[tuple[Hotspot, frozenset[str]]] = []
    for ch in project.story.chapters:
        if not isinstance(ch, ScrubChapter | CompareChapter) or ch.vantage != vid:
            continue
        for h in ch.hotspots:
            lo = resolve_capture_ref(h.from_, dates, default=0)
            hi = resolve_capture_ref(h.to, dates, default=last)
            ids = frozenset(FACT_TOKEN_RE.findall(f"{h.label} {h.body or ''}"))
            if h.id in seen or not ids or not lo <= at <= hi:
                continue
            seen.add(h.id)
            out.append((h, ids))
    return out


def _stat_type(project: Project, fonts: dict[str, Any], lay: Layout) -> Any:
    """The brand's type at this film's unit (film/render._Type), to set stat runs as film/cut.py does."""
    from vantage.film.plan import font_path
    from vantage.film.render import _Type

    files = {role: font_path(fonts[role]["file"], project.brand) for role in ("display", "text", "numeric")}
    c = project.brand.colors
    return _Type(files, {"paper": c.paper, "accent": c.accent, "ink": c.ink}, lay.unit)  # type: ignore[arg-type]


def _shows(shot: dict[str, Any] | None, file: str) -> bool:
    """Whether `shot` is a capture of the picture `file` (a stat run then starts on it as it is)."""
    return shot is not None and shot["kind"] == "capture" and shot.get("file") == file


def _place_stats(
    b: _Builder,
    items: list[dict[str, Any]],
    honesty: str | None,
    subjects: Sequence[Hotspot | None],
    geo: Geometry,
    zmax: float,
    rest: Cam,
    t: Any,
    tagged: bool,
) -> tuple[int, str, list[tuple[Cam | None, str] | None]]:
    """(score, place, aims) of a stat run: 'low' or 'high', whichever frames more of its stats'
    hotspots clear of the panel; each aim is (target, how) or None for a stat with no hotspot."""
    from vantage.film.cut import stat_run, stat_subject_box

    lay = b.layout
    best: tuple[int, str, list[tuple[Cam | None, str] | None]] | None = None
    for place in ("low", "high"):
        box = stat_subject_box(lay, stat_run(t, lay, items, honesty, place, tagged))
        aims: list[tuple[Cam | None, str] | None] = []
        first = sum(s["kind"] == "stat" for s in b.shots) + 1
        for k, h in enumerate(subjects):
            if h is None:
                aims.append(None)
                continue
            zoom = min(STAT_ZOOM, zmax) ** b.caps.get(f"N{first + k}", 1.0)  # the flash margin may ask less
            target, how = aim(geo, (h.x, h.y, zoom), zmax, [h], explicit=False, rest=rest, box=box)
            a, c = geo.centre(*rest), geo.centre(*target)
            moves = not (target[2] < MIN_ZOOM and math.hypot(c[0] - a[0], c[1] - a[1]) < MIN_SHIFT)
            framed = how.startswith("focus+subject") and moves
            aims.append((target, how) if framed else (None, f"drift, subject off-frame:{h.id}"))
        score = sum(1 for x in aims if x is not None and x[0] is not None)
        if best is None or score > best[0]:
            best = (score, place, aims)
    assert best is not None
    return best


def _stats(
    b: _Builder,
    cid: str,
    tint: str,
    items: list[dict[str, Any]],
    *,
    plain: bool,
    on: _StatsOn,
    fonts: dict[str, Any],
    hashes: dict[str, str],
    honesty: str | None,
) -> Cam | None:
    """A run of stat shots. Over a picture (`on`) unless `plain`: the first dissolves in from the
    image before it, and the run is placed low (default) or high, whichever frames more of its
    stats' hotspots clear of the panel. A stat whose facts a hotspot prints pushes slowly toward it
    (`aim`, clamped like a step's push); any other stat drifts in by DRIFT, or holds. Consecutive
    stats start on the beat with no dip and only the text crossfades. Returns the camera the run
    ends on when that is not the rest view (what follows eases back from it)."""
    project, film, lay = b.project, b.film, b.layout
    B, third, fps = b.grid.fpb, b.grid.third, b.grid.fps
    cam = b.camera
    geo = on.geometry
    rest: Cam = (*on.portrait_focus, 1.0) if lay.width < lay.height else (0.5, 0.5, 1.0)
    zmax = geo.zmax(film.max_upscale * b.px)
    subjects = [
        None if plain else next((h for h, ids in on.hotspots if ids & set(item["facts"])), None)
        for item in items
    ]
    tagged = project.config.simulated or project.config.draft
    t = _stat_type(project, fonts, lay)

    def placed() -> tuple[str, list[Any]]:
        best = _place_stats(b, items, honesty, subjects, geo, zmax, rest, t, tagged)
        return best[1], [None if x is None else [list(x[0]) if x[0] else None, x[1]] for x in best[2]]

    place, kept = b.decide(f"stats:{cid}", placed)
    aims: list[tuple[Cam | None, str] | None] = [
        None if x is None else ((tuple(x[0]) if x[0] else None), x[1])  # type: ignore[misc]
        for x in kept
    ]
    first_text_in = 0
    for k, item in enumerate(items):
        f0 = b.f
        dissolve: dict[str, Any] | None = None
        dip = 0
        # On the picture already on screen (a montage's last): no dissolve, a beat's lead.
        same = k == 0 and not plain and _shows(b.prev, on.capture.file)
        if k == 0 and plain:
            dip = b.dip(tint)
            if is_image(b.prev):
                cam.until(f0 + dip)
        elif k == 0 and not same:
            dissolve = b.entry()
            b.cross(on.vantage, dissolve, rest)  # from another vantage: the camera cuts to rest
        entry = dissolve["f1"] - dissolve["f0"] if dissolve else B if same else dip
        # The first figure lands on the first beat after its entry; the next ones on their first
        # frame, once the text before them has faded out (over that stat's last B/3). Each figure
        # slides in whole over half a beat; label and source fade in after it (`text_in`) and stay
        # until the shot's end (or its last B/3 before another stat), long enough to read at 17
        # characters a second. (The flash margin may hold the run's picture before the first
        # figure lands, "N1.0", and any stat longer before the next, "N<n>".)
        n = sum(s["kind"] == "stat" for s in b.shots) + 1
        land = f0 + (-(-entry // B) + b.holds.get(f"N{n}.0", 0)) * B if k == 0 else f0
        text_in = land + round(B / 2)
        if k == 0:
            first_text_in = text_in
        read = " ".join(filter(None, (item["label"], item["source"])))
        out = third if k + 1 < len(items) else 0  # its text leaves before the next figure lands
        need = text_in - f0 + third + cue_frames(read, fps) + out
        if honesty and k == len(items) - 1:  # the riding honesty line reads in full by the run's end
            need = max(need, first_text_in + third + cue_frames(honesty, fps) - f0)
        beats = max(STAT_MIN_BEATS, -(-need // B)) + b.holds.get(f"N{n}", 0)
        f1 = f0 + beats * B
        move: dict[str, Any] | None = None
        if not plain:
            start = land
            current = cam.windows[-1]
            if k == 0 and not same and (current["x"], current["y"], current["zoom"]) != rest:
                cam.until(f0)  # a push before eases back to the rest view first
                cam.hold(f0 + b.easing_back, rest)
                start = max(start, f0 + b.easing_back)
            current = cam.windows[-1]
            now: Cam = (current["x"], current["y"], current["zoom"])
            target, how = aims[k] or (None, "drift")
            if target is None:
                z = min(now[2] * DRIFT, zmax)
                off = how.split("drift", 1)[1]  # ", subject off-frame:<id>" or ""
                target, how = ((now[0], now[1], z), how) if z > now[2] + 1e-6 else (None, f"hold{off}")
            reach = max(start + b.ease, f1 - third)
            if target is not None and target != now and reach <= f1:
                cam.until(start)
                cam.hold(reach, target, f1)
            else:
                target, start, reach = None, f0, f0
                how = how if how.startswith("hold") else "hold"
                cam.until(f1)
            h = subjects[k]
            share = b.caps.get(f"N{n}", 1.0)
            move = {
                **({"gentled": share} if share < 1.0 and target is not None else {}),
                "how": how,
                "subjects": [h.id] if h is not None and how.startswith("focus+subject") else [],
                "focus": list(target) if target is not None else None,
                "upscale": geo.s0 * target[2] if target is not None else None,
                "f0": start,
                "f1": reach,
            }
            if move["subjects"] and geo.s0 * target[2] / b.px > SOFT + 1e-9:  # type: ignore[index]
                b.warnings.append(
                    f"chapter {cid!r} stat {k}: soft push, {geo.s0 * target[2] / b.px:.2f} output px "
                    f"per master px ({film.id})"
                )
        text = [
            {"role": role, "text": item[role]} for role in ("value", "unit", "label", "source") if item[role]
        ]
        if honesty:
            text.append({"role": "honesty", "text": honesty})
        shot = b.add(
            {
                "id": f"N{n}",
                "kind": "stat",
                "chapter": cid,
                "text": text,
                "facts": item["facts"],
                "value": item["value"],
                "unit": item["unit"],
                "label": item["label"],
                "source": item["source"],
                "surface": tint,
                "backdrop": "plain" if plain else "picture",
                **(
                    {}
                    if plain
                    else {"vantage": on.vantage, "capture": on.capture.date, "file": on.capture.file}
                ),
                "place": place,
                "dissolve": dissolve,
                "dip": dip,
                "land": land,
                "text_in": text_in,
                "honesty": honesty,
                "move": move,
            },
            beats,
        )
        shot["key"] = min(shot["f1"] - 1, land + B)
        if not plain and on.capture.file not in hashes:
            hashes[on.capture.file] = _sha256(project.masters_dir / on.capture.file)
        start_cue = f0 + entry // 2
        b.tracks.cue(
            start_cue, " ".join(filter(None, (item["value"], item["unit"], item["label"]))), item["facts"]
        )
        figure = " ".join(filter(None, (item["value"], item["unit"])))
        source = f" (source: {item['source']})" if item["source"] else ""
        line, facts = f"Figure: {figure} {item['label']}{source}.", list(item["facts"])
        if move is not None and move["subjects"]:
            spot = subjects[k]
            assert spot is not None
            label, label_facts = b.said(spot.label, f"hotspot {spot.id} label")
            if label:
                line = sentence(line, f"The camera moves in on {plain_markdown(label)}.")
                facts += label_facts
        b.tracks.line(start_cue, line, facts)
    last = cam.windows[-1]
    held = (last["x"], last["y"], last["zoom"])
    b.back = None
    return None if plain or held == rest else held


def _end_text(b: _Builder, honesty: str, has_stats: bool, last_part: tuple[str, Any]) -> dict[str, Any]:
    """The end card's text and length. A loop's: the brand and the short URL, `end_beats`. Any other
    film's: the brand, credit line, partners and link, then the honesty line and the copyright and
    disclaimer as fine print (read at a glance, as legal text is). The card takes as long as its
    words take to read at 17 characters a second after its dip (the partners a beat to scan), plus
    the last beat its text fades out over, and at least `end_beats`; when that is over END_MAX_S
    and the film has stats, the honesty line rides along under the stats instead (`ride`), then the
    partners are left out (a warning) when that makes it fit. Returns {text, facts, ride, beats(dip)}."""
    project, film = b.project, b.film
    brand, cfg = project.brand, project.config
    B, third, fps = b.grid.fpb, b.grid.third, b.grid.fps
    say = b.say()
    lines: list[dict[str, Any]] = [{"role": "brand", "text": brand.name}]
    short_url = cfg.output.base_url.split("://")[-1].rstrip("/") if cfg.output.base_url else None
    if film.loop:  # a loop's end card: the brand and the URL in large type, nothing else
        if short_url:
            lines.append({"role": "url", "text": short_url})
        return {"text": lines, "facts": list(say.facts), "ride": None, "beats": lambda dip: film.end_beats}
    credit = say(brand.credit_line, "brand.yaml credit_line") if brand.credit_line else None
    if credit:
        lines.append({"role": "credit", "text": credit})
    partners = [
        item for partner in brand.partners
        for item in ({"role": "role", "text": partner.role}, {"role": "partner", "text": partner.name})
    ]  # fmt: skip
    link = [{"role": "link", "text": short_url}] if short_url else []
    fine = [
        {"role": "fine", "text": t}
        for text_, where in ((brand.copyright, "copyright"), (brand.disclaimer, "disclaimer"))
        if text_ and (t := say(text_, f"brand.yaml {where}"))
    ]
    reading = " ".join(filter(None, (credit, short_url)))
    # The dip into the end card: from paper after a plain paper stat card, else D frames.
    paper_last = last_part[0] == "stats" and last_part[1][5] and last_part[1][1] == "paper"
    dip0 = b.paper_dip if paper_last else b.grid.dissolve

    def need(read: str, scan: bool, dip: int) -> int:
        return max(film.end_beats, -(-(dip + third + cue_frames(read, fps) + (B if scan else 0) + B) // B))

    most = int(END_MAX_S * fps // B)
    ride = None
    if has_stats and need(sentence(reading, honesty), bool(partners), dip0) > most:
        ride = honesty
    read = reading if ride else sentence(reading, honesty)
    if partners and need(read, True, dip0) > most >= need(read, False, dip0):
        b.warnings.append(
            f"end card: the partners are left out of film {film.id} (with them it would run over "
            f"{END_MAX_S:g} s)"
        )
        partners = []
    honest = [] if ride else [{"role": "honesty", "text": honesty}]
    scan = bool(partners)
    return {
        "text": [*lines, *partners, *link, *honest, *fine],
        "facts": list(say.facts),
        "ride": ride,
        "beats": lambda dip: need(read, scan, dip),
    }


def derive_cut(
    project: Project,
    film: FilmDef,
    index: MastersIndex,
    fonts: dict[str, Any],
    hashes: dict[str, str],
    warnings: list[str],
    *,
    fast: bool,
    margin_on: bool = True,
) -> FilmPlan:
    """The story cut of `film` as a FilmPlan (raises ValueError when there is nothing to film), held,
    softened and slowed where needed to keep its flash margin (film/margin.py): planned and metered
    at a proxy size until the estimate keeps `output.film.flash_limit`, then planned at its own size
    with the same fixes (and so the same beats), recorded as plan `flash` (the estimate itself in
    checks `flash`). What depends on where text sets (words zones, the stats' place, every push
    target) is decided once per pass at the full size and passed to the proxy and to a VANTAGE_FAST
    plan as fixed inputs, so all three frame the same film. The proxy is metered at the 1/8 sizes of
    both the full-size film and the VANTAGE_FAST one, and the margin keeps both. `margin_on=False`
    (vantage validate) plans without metering: no fixes, no estimate."""
    from vantage.film import margin
    from vantage.film.subject import Seen

    seen = Seen(project.masters_dir)
    settings = project.config.output.film
    limit = settings.flash_limit
    full = film_size(settings, film.aspect, fast=False)
    proxy, measure = margin.proxy_size(*full)
    _, fast_measure = margin.proxy_size(*film_size(settings, film.aspect, fast=True))
    measures = [measure] if fast_measure == measure else [measure, fast_measure]
    fixes = margin.Fixes()
    measured: margin.Measured | None = None
    estimate, frame, passes = 0, 0, 0
    small: FilmPlan | None = None

    def decide() -> dict[str, Any]:
        decided: dict[str, Any] = {}
        _derive_cut(
            project, film, index, fonts, hashes, [], fast=False, size=full,
            holds=fixes.holds, slows=fixes.slows, gentle=fixes.caps, seen=seen, record=decided,
        )  # fmt: skip
        return decided

    decided = decide()
    while margin_on:
        passes += 1
        small = _derive_cut(
            project, film, index, fonts, hashes, [], fast=fast, size=proxy,
            holds=fixes.holds, slows=fixes.slows, gentle=fixes.caps, seen=seen, decided=decided,
        )  # fmt: skip
        measured = margin.meter_proxy(project, small, measures, measured)
        estimate, frame = margin.worst(measured.events, small.plan["fps"])
        if estimate <= limit or passes >= margin.PASSES:
            break
        fixes, added = margin.fixes(measured.events, small.plan, limit, fixes)
        if not added:
            break
        decided = decide()
    plan = _derive_cut(
        project, film, index, fonts, hashes, warnings, fast=fast,
        holds=fixes.holds, slows=fixes.slows, gentle=fixes.caps, seen=seen, decided=decided,
    )  # fmt: skip
    p = plan.plan
    p["flash"] = {"limit": limit, **fixes.record()}
    if not margin_on:
        return plan
    plan.checks["flash"] = {
        "estimate": estimate, "frame": frame, "proxy": list(proxy), "measures": [list(m) for m in measures],
        "passes": passes,
    }  # fmt: skip
    assert small is not None
    if (differs := _proxy_differs(p, small.plan)) is not None:
        plan.warnings.append(
            f"flash margin: the {proxy[0]}×{proxy[1]} proxy plans other {differs} than the film ({film.id}); "
            "the estimate may not hold"
        )
    if estimate > limit:
        plan.warnings.append(
            f"flash margin: the planner estimates {estimate} flash transitions in one region within a "
            f"second around frame {frame}, over the limit of {limit} ({film.id}); nothing left to lengthen "
            "there (the render meter refuses more than 3)"
        )
    return plan


def _proxy_differs(a: dict[str, Any], b: dict[str, Any]) -> str | None:
    """What a film's plan and its proxy's frame differently ('beats', 'words zones', 'stat places',
    'camera moves'), or None: every shot's beats, every zone and place, and every camera window
    (within 1e-6 of the master) must match for the proxy's estimate to hold for the film."""
    if [(s["id"], s["f0"], s["f1"]) for s in a["shots"]] != [(s["id"], s["f0"], s["f1"]) for s in b["shots"]]:
        return "beats"

    def zones(p: dict[str, Any]) -> list[Any]:
        out = []
        for s in p["shots"]:
            steps = s.get("steps") or ([s["step"]] if s.get("step") else [])
            out += [st.get("words_zone") for st in steps]
            out.append(s.get("place"))
        return out

    if zones(a) != zones(b):
        return "words zones or stat places"
    ca, cb = a["camera"], b["camera"]
    if len(ca) != len(cb) or any(
        x["a"] != y["a"] or x["b"] != y["b"] or any(abs(x[k] - y[k]) > 1e-6 for k in ("x", "y", "zoom"))
        for x, y in zip(ca, cb, strict=True)
    ):
        return "camera moves"
    return None


def _derive_cut(
    project: Project,
    film: FilmDef,
    index: MastersIndex,
    fonts: dict[str, Any],
    hashes: dict[str, str],
    warnings: list[str],
    *,
    fast: bool,
    size: tuple[int, int] | None = None,
    holds: dict[str, int] | None = None,
    slows: dict[str, int] | None = None,
    gentle: dict[str, float] | None = None,
    seen: Any = None,
    decided: dict[str, Any] | None = None,
    record: dict[str, Any] | None = None,
) -> FilmPlan:
    """The story cut of `film` at its size (or `size`, a flash proxy), with the margin's holds,
    slows and zoom caps (`gentle`, `_Builder.caps`); `seen`: the masters as film/subject.py reads
    them, shared by the plans of one `derive_cut`. `decided`: words zones, stat places and push
    targets decided at the full size (`_Builder.decide`); `record`: filled with this plan's."""
    cfg, settings = project.config, project.config.output.film
    fps = settings.fps
    if fps <= 0:
        raise ValueError("output.film needs fps > 0")
    grid = beat_grid(fps, film.bpm)
    B, D = grid.fpb, grid.dissolve
    width, height = size or film_size(settings, film.aspect, fast=fast)
    preset = "screen" if film.preset == "screen" and film.aspect == "16x9" else f"story-{film.aspect}"
    lay = layout(preset, width, height, safe=film.safe, type_scale=film.type_scale)
    colors = project.brand.colors
    paper_dip = surface_dip(fps, D, colors.night, colors.paper)
    strict = film.loop and not cfg.draft  # _Builder.strict
    caps = _Captures(project, index, warnings, strict)
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
        elif isinstance(ch, GalleryChapter):
            main_vid = main_vantage(project, index)
            if m := _montage(project, i, ch, caps, size, main=main_vid, count=film.montage):
                parts.append(("montage", m))
        elif isinstance(ch, StatsChapter) and not any(k == "stats" for k, _ in parts):
            items = (
                _stat_items(project, ids[i], ch, film.stats, warnings, releasable_only=strict, film=film.id)
                if film.stats
                else []
            )
            if items:
                heading, heading_facts = said(
                    project, ch.title, f"chapter {ids[i]!r} title", warnings, releasable_only=strict
                )
                if not heading:
                    heading, heading_facts = said(
                        project, ch.kicker, f"chapter {ids[i]!r} kicker", warnings, releasable_only=strict
                    )
                # Over a picture by default, under a panel in the chapter's surface (the web's
                # default for stats: paper); a FilmSpec `stats_surface` asks for a plain card instead.
                tint = film.stats_surface or ch.surface or "paper"
                plain = film.stats_surface is not None
                parts.append(("stats", (ids[i], tint, items, heading or "Figures", heading_facts, plain)))

    # Degrade on the main vantage's captures (after exclusions).
    main = main_vantage(project, index)
    every = caps.of(main) if main is not None else []
    if not every:
        raise FileNotFoundError(f"no masters to film in {project.masters_dir}; run `vantage process` first")
    assert main is not None
    stats = [p for p in parts if p[0] in ("stats", "montage")]  # kept, in story order, when degrading
    # id() of captures that must not push, and why: the two-capture cut's before, and a step whose
    # picture the next curtain shows as `after` and pushes to the same place (it pushes there itself:
    # pushing twice, with an ease back between, frames one subject in, out and in again).
    still: dict[int, str] = {}
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
            still[id(series.captures[0])] = "two captures"
            c = _curtain(project, compare[0], compare[1], caps, main, size, pair=(every[0], every[1]))
            if c is not None:
                parts.append(("curtain", c))
        parts += stats
    elif not any(k in ("series", "curtain") for k, _ in parts):
        parts.insert(0, ("series", _fallback_series(project, caps, main, size)))
    for (k1, p1), (k2, p2) in itertools.pairwise(parts):
        if k1 == "series" and k2 == "curtain" and p1.vantage == p2.vantage and _curtain_pushes_it(p1, p2):
            still.setdefault(id(p1.captures[-1]), "the curtain pushes it")
    images = [p for k, p in parts if k in ("series", "curtain")]
    montages: list[_Montage] = [p for k, p in parts if k == "montage"]
    total_images = sum(len(p.captures) if isinstance(p, _Series) else 1 for p in images)

    # Pushes are clamped at the full size (max_upscale is in full-size output px), so a VANTAGE_FAST
    # preview makes the same pushes, beats and captions as the film it previews.
    px = width / film_size(settings, film.aspect, fast=False)[0]
    b = _Builder(
        project, film, grid, lay, warnings, paper_dip, px, holds=dict(holds or {}), slows=dict(slows or {}),
        caps=dict(gentle or {}), decided=decided, decisions={} if record is None else record,
    )  # fmt: skip
    b.tracks = Tracks(fps, enabled=has_tracks(film, settings))
    b.placer = _Placer(project, fonts, lay, seen)
    for part in [*images, *montages]:
        geo = part.geometry
        if geo.s0 / b.px > film.max_upscale + 1e-9:
            text = (
                f"vantage {part.vantage}: master upscaled even at rest ({geo.s0 / b.px:.2f}× > max_upscale "
                f"{film.max_upscale:g}; {geo.mw}×{geo.mh} masters for a {width}×{height} film)"
            )
            if text not in warnings:
                warnings.append(text)
    first = images[0]
    first_cap = first.captures[0] if isinstance(first, _Series) else first.before
    provenance = ["simulated" if cfg.simulated else "photo"]
    honesty = honesty_line(provenance)
    # A run of stats over a picture closes on the last montage's last picture when the film has a
    # montage (after it, so the middle of the film is not one photograph and the figures land on
    # the finished site seen anew); else it sits on the main vantage's latest picture, in story order.
    on_vid, on_cap = main, every[-1]
    stat_part = next((p for p in parts if p[0] == "stats"), None)
    if montages and stat_part is not None and not stat_part[1][5]:
        parts.remove(stat_part)
        last = max(k for k, p in enumerate(parts) if p[0] == "montage")
        parts.insert(last + 1, stat_part)
        on_vid, on_cap = montages[-1].vantage, montages[-1].captures[-1]
    end = _end_text(b, honesty, any(k == "stats" for k, _ in parts), parts[-1])
    stats_on = _StatsOn(on_vid, on_cap, _geometry(project, index, on_vid, width, height),
                        _vantage_info(project, caps, on_vid)[1], _stat_hotspots(project, caps, on_vid, on_cap))  # fmt: skip
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
            if is_image(b.prev):
                cam.until(b.f + b.dip("night"))
            _card(b, part.chapter, part.kicker, part.title, part)
            pushed = None
        if kind == "series":
            s: _Series = part
            series_no += 1
            rest = b.rest(s)
            zmax = s.geometry.zmax(film.max_upscale * b.px)
            vname, vname_facts = b.said(s.name, f"vantage {s.vantage} name")
            vname = vname or s.masters_name
            heading, heading_facts = b.said(s.title, f"chapter {s.chapter!r} title")
            b.tracks.chapter(part_f0, heading or vname, heading_facts if heading else vname_facts)
            last_zone: str | None = None  # a series' words keep their zone while it is clear
            for cap in s.captures:
                n += 1
                sid, f0 = f"S{n:02d}", b.f
                lead, ease = (1 + b.holds.get(sid, 0)) * B, b.push_ease(sid)
                step, target, beats, zone = _capture_step(
                    b, s, cap, rest, zmax, pushed, still, total_images, last_zone, sid
                )
                dissolve = None if n == 1 and b.prev and b.prev["kind"] == "title" else b.entry()
                _capture_camera(b, cam, f0, rest, pushed, target, beats, lead, ease)
                b.back = ease if target is not None else None
                if zone is not None:
                    assert step is not None
                    step.update(zone)
                    last_zone = zone["words_zone"]
                pushed = target
                facts = list(dict.fromkeys([*cap.label_facts, *(step["facts"] if step else [])]))
                text = [{"role": "label", "text": cap.label}]
                if step and step["words"]:
                    text.append({"role": "words", "text": step["words"]})
                    # on beat 1 (after the title, the picture plays clean first), or later when held
                    step["land"] = f0 + lead
                if step is not None:
                    step.pop("facts")
                    if target is not None:
                        step["push_f0"], step["push_f1"] = f0 + lead, f0 + lead + ease
                shot = b.add(
                    {
                        "id": sid,
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
                    shot["key"] = min(f1 - 1, f0 + lead + ease + B - b.grid.third)
                elif step is not None:
                    shot["key"] = f0 + lead + B
                else:
                    shot["key"] = f1 - 1
                if cap.file not in hashes:
                    hashes[cap.file] = _sha256(project.masters_dir / cap.file)
                _capture_tracks(b, shot, cap, s, vname, vname_facts, first=cap is s.captures[0])
            last_vantage = s.vantage
        elif kind == "curtain":
            pushed = _curtain_shot(b, part, fonts, hashes, part_f0)
            last_vantage = part.vantage
        elif kind == "montage":
            series_no += 1
            n, pushed = _montage_shots(b, part, hashes, n, series_no)
            last_vantage = part.vantage
        else:
            cid, tint, items, heading, heading_facts, plain = part
            b.tracks.chapter(part_f0, heading, heading_facts)
            pushed = _stats(
                b, cid, tint, items, plain=plain, on=stats_on, fonts=fonts, hashes=hashes,
                honesty=end["ride"],
            )  # fmt: skip

    # The end card (E1): a dip to night, then the brand, credits and fine print (a loop: the brand
    # and the short URL), at most END_MAX_S long with its dip (see `_end_text`).
    dip = b.dip("night")
    if is_image(b.prev):  # a push holds while the image dips out
        cam.until(b.f + dip)
    end_beats = end["beats"](dip)
    if film.loop:  # a whole number of GOPs: lengthen the end card
        end_beats += -(b.f // B + end_beats + LOOP_BEATS["dip"]) % GOP_BEATS
    card = b.add(
        {
            "id": "E1",
            "kind": "end",
            "chapter": None,
            "text": end["text"],
            "facts": end["facts"],
            "dip": dip,
            "honesty": honesty,
            "ride": end["ride"] is not None,
        },
        end_beats,
    )
    card["key"] = min(card["f1"] - 1, card["f0"] + dip + 2 * B)
    credit, credit_facts = b.said(project.brand.credit_line, "brand.yaml credit_line")
    b.tracks.chapter(card["f0"], "Credits")
    b.tracks.cue(card["f0"] + dip // 2, credit or project.brand.name, credit_facts)
    b.tracks.line(
        card["f0"] + dip // 2, "End card: " + " / ".join(t["text"] for t in end["text"]), end["facts"]
    )
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
    poster = _poster(b, [*images, *montages])
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
        "poster": poster,
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


def _capture_camera(
    b: _Builder,
    cam: _Camera,
    f0: int,
    rest: Cam,
    pushed: Cam | None,
    target: Cam | None,
    beats: int,
    lead: int | None = None,
    ease: int | None = None,
) -> None:
    """A capture's camera from its first frame: back to rest after a card or stat, easing back from
    the previous push, or pushing in to `target` over `ease` frames from `lead` (beat 1, later when
    the flash margin holds the capture)."""
    B = b.grid.fpb
    lead = B if lead is None else lead
    ease = b.ease if ease is None else ease
    if b.prev is not None and not is_image(b.prev):
        cam.hold(f0, rest)
    if pushed is not None and target is None:  # the previous push eases back from the
        cam.until(f0)  # dissolve on, as slowly as it went in
        cam.hold(f0 + b.easing_back, rest)
    # (pushed and target: the camera glides from the last subject to this one)
    if target is not None:
        cam.until(f0 + lead)
        cam.hold(f0 + lead + ease, target, f0 + beats * B)


def _capture_step(
    b: _Builder,
    s: _Series,
    cap: _Capture,
    rest: Cam,
    zmax: float,
    pushed: Cam | None,
    still: dict[int, str],
    total_images: int,
    default: str | None = None,
    sid: str = "",
) -> tuple[dict[str, Any] | None, Cam | None, int, dict[str, Any] | None]:
    """A capture's step record, push target, beats and (with words) its words zone record. Each
    zone aims the push with its own subject box; the zone is chosen (`_Placer.choose`, keeping
    `default`, the series' last words zone, while it is clear) from where the subject is on screen
    from the words' landing (beat 1) to the shot's end along that camera path. The flash margin
    (`b.holds`, `b.slows` of `sid`) adds beats before the step lands and to a push."""
    film, B = b.film, b.grid.fpb
    held, lead, ease = b.holds.get(sid, 0), (1 + b.holds.get(sid, 0)) * B, b.push_ease(sid)

    def plan(box: Box | None) -> tuple[dict[str, Any] | None, Cam | None, int]:
        step, target = _push(b, s, cap, rest, zmax, box, sid)
        if id(cap) in still and target is not None:
            assert step is not None
            step.update(push=False, reason=still[id(cap)])
            target = None
        quiet, stepped, pushing = film.beats
        beats = quiet if step is None else (pushing if target else stepped)
        if total_images == 1 or id(cap) in still:
            beats = max(beats, stepped)
        beats += held
        if target is not None:  # a slowed push still lands a third of a beat before the shot ends
            beats = max(beats, -(-(lead + ease + b.grid.third) // B))
        return step, target, beats

    placer = b.placer
    words = cap.step and _push_words(b, s, cap)
    if placer is None or not words or cap.step is None:
        return (*plan(None), None)
    from vantage.film.subject import subject_map

    f0 = b.f
    label = f"{s.chapter} {cap.date}"
    step_ = cap.step

    def choose() -> dict[str, Any]:
        keep = b.warnings
        paths: dict[str, list[Any]] = {}
        for zone in placer.zones:  # dry runs: their warnings are dropped, the chosen zone's kept below
            b.warnings = []
            try:
                _, target, beats = plan(placer.aim_box(words, zone, placer.label_base))
            finally:
                b.warnings = keep
            scratch = _Camera()
            scratch.windows = [dict(w) for w in b.camera.windows]
            _capture_camera(b, scratch, f0, rest, pushed, target, beats, lead, ease)
            scratch.until(f0 + beats * B)
            paths[zone] = _path(scratch.windows, s.geometry, f0 + lead, f0 + beats * B, b.grid.third)
        requested = step_.film.focus if step_.film and step_.film.focus else step_.focus
        points = [(h.x, h.y) for h in cap.subjects]
        if requested:
            points.insert(0, (requested[0], requested[1]))
        subject = subject_map(placer.seen, cap.file, cap.before, points)
        return placer.choose(
            b, label, words, placer.label_base, subject, [cap.file], paths, default or placer.zones[0]
        )

    zone = placer.zone(b, label, words, choose)
    step, target, beats = plan(placer.aim_box(words, zone["words_zone"], placer.label_base))
    return step, target, beats, zone


def _push_words(b: _Builder, s: _Series, cap: _Capture) -> str | None:
    """The words a capture's step prints (as `_push` resolves them), without warnings."""
    assert cap.step is not None
    raw, _ = lead_words(cap.step)
    if not raw:
        return None
    keep = b.warnings
    b.warnings = []
    try:
        return b.say()(raw, "words")
    finally:
        b.warnings = keep


def _curtain_pushes_it(series: _Series, curtain: _Curtain) -> bool:
    """Whether a series' last step is on the curtain's `after` picture and the curtain pushes within
    MIN_SHIFT of where that step asks to push."""
    cap = series.captures[-1]
    step = cap.step
    if step is None or cap.date != curtain.after.date:
        return False
    mine = step.film.focus if step.film and step.film.focus else step.focus
    if not mine:
        return False
    for _, st, _ in curtain.steps:
        theirs = (st.film.focus if st.film and st.film.focus else st.focus) if st else None
        if theirs and math.hypot(theirs[0] - mine[0], theirs[1] - mine[1]) < MIN_SHIFT:
            return True
    return False


def _poster(b: _Builder, images: Sequence[_Series | _Curtain]) -> int:
    """The poster frame: the latest capture of a series (never a montage's: the thumbnail shows the
    main story whatever drift a master's size allows) shown at rest and without its words (the frame before
    a step's words land, else its last frame), once its dissolve is over and the camera is back at
    the rest view; else the title's key frame. A push is the most enlarged picture in a film, and
    words over it hide the subject, so neither makes the thumbnail."""
    from vantage.film.beat import Window, keyed, mix_cam

    windows = [Window(w["a"], w["b"]) for w in b.camera.windows]
    cams = [(w["x"], w["y"], w["zoom"]) for w in b.camera.windows]
    rests = {p.vantage: b.rest(p) for p in images}
    B = b.grid.fpb
    for shot in reversed(b.shots):
        if shot["kind"] != "capture" or shot.get("montage"):  # the main story's pictures only
            continue
        f = shot["f0"] + B - 1 if shot.get("step") else shot["f1"] - 1
        d = shot["dissolve"]
        if d is not None and f < d["f1"]:
            continue
        cam = keyed(windows, cams, f, mix_cam)
        rest = rests[shot["vantage"]]
        if abs(cam[2] - 1.0) < 1e-9 and math.hypot(cam[0] - rest[0], cam[1] - rest[1]) < 1e-9:
            return f
    return b.shots[0]["key"]


def _capture_tracks(
    b: _Builder,
    shot: dict[str, Any],
    cap: _Capture,
    s: _Series,
    vname: str,
    vname_facts: Sequence[str],
    *,
    first: bool,
    words_facts: Sequence[str] = (),
) -> None:
    """A capture's caption cue ('<label>. <words>', plus its note when that still reads at 17
    characters per second) and transcript line (the step's full text, else the note). `words_facts`:
    the facts of words that are not a step's (a montage's)."""
    d = shot["dissolve"]
    start = shot["f0"] if d is None else d["f0"] + (d["f1"] - d["f0"]) // 2
    step = shot["step"]
    words = step["words"] if step else None
    words_facts = list(words_facts) if words else []
    if words and cap.step is not None:
        raw, _ = lead_words(cap.step)
        _, words_facts = b.said(raw, f"chapter {s.chapter!r} step {cap.step_index} film words")
    base = sentence(cap.label, words)
    base_facts = [*cap.label_facts, *words_facts]
    more = (sentence(base, cap.note), [*base_facts, *cap.note_facts]) if cap.note else None
    b.tracks.cue(start, base, base_facts, more, span=None if words else cap.label)
    text, text_facts = b.step_text(cap.step, f"chapter {s.chapter!r} step {cap.step_index}")
    body, body_facts = (text, text_facts) if text else (cap.note, cap.note_facts)
    lead = f"Aerial view ({vname}), {cap.label}" if first else f"Same view, {cap.label}"
    facts = [*cap.label_facts, *body_facts, *(vname_facts if first else ())]
    line = f"{lead}: {body}" if body else f"{lead}."
    if step and step["push"]:  # the push carries information: say what it frames (and only that)
        framed = step["subjects"] if (step.get("target") or "").startswith("focus+subject") else []
        named = [b.said(h.label, f"hotspot {h.id} label") for h in cap.subjects if h.id in framed]
        labels = [plain_markdown(text) for text, _ in named if text]
        facts += [f for text, ids in named if text for f in ids]
        on = f" on {' and '.join(labels)}" if labels else ""
        line = sentence(line, f"The camera moves in{on}.")
    b.tracks.line(start, line, facts)


MONTAGE_BEATS = 2  # a montage picture's beats (at least the film's quiet capture beats)
MONTAGE_LAST_BEATS = 4  # its last picture's: the closing look holds, drifting, before what follows


def _montage_shots(
    b: _Builder, m: _Montage, hashes: dict[str, str], n: int, series_no: int
) -> tuple[int, Cam | None]:
    """A gallery chapter's montage: one capture shot (`montage: true`) per picture, MONTAGE_BEATS
    each, every one dissolving in on its first beat, no card (from another vantage the outgoing
    picture keeps its framing, `_Builder.cross`). The words (film.line, else the kicker, else the
    title) land on the first picture's beat 1, in the zone clear of what changes over the montage,
    and it lasts until they read; no push, only a drift in of DRIFT from the rest view over the
    whole run. Returns the last shot number and the camera the run ends on."""
    project, film, B, third = b.project, b.film, b.grid.fpb, b.grid.third
    cam, geo = b.camera, m.geometry
    rest = b.rest(m)
    f0 = b.f
    quiet = max(MONTAGE_BEATS, film.beats[0])
    words, words_facts = b.said(m.words, f"chapter {m.chapter!r} film words")
    first = quiet
    if words:  # the words land on beat 1 and stay until they read at 17 characters a second
        first = max(quiet, -(-(B + third + cue_frames(words, b.grid.fps)) // B))
    beats = [first] + [quiet] * (len(m.captures) - 2) + [max(quiet, MONTAGE_LAST_BEATS)]
    holds = [b.holds.get(f"S{n + 1 + k:02d}", 0) for k in range(len(m.captures))]  # the flash margin's
    beats = [length + h for length, h in zip(beats, holds, strict=True)]
    end = f0 + sum(beats) * B
    dissolve = b.entry()
    if not b.cross(m.vantage, dissolve, rest):
        if is_image(b.prev):  # the same vantage: ease back from where it was
            cam.until(f0)
            cam.hold(f0 + b.easing_back, rest)
        else:
            cam.hold(f0, rest)
    b.back = None
    zmax = geo.zmax(film.max_upscale * b.px)
    drift: Cam = (rest[0], rest[1], min(DRIFT, zmax))
    if drift[2] > 1.0 + 1e-6 and end - cam.windows[-1]["b"] >= b.ease:
        cam.hold(end, drift)
    vname, vname_facts = b.said(m.name, f"vantage {m.vantage} name")
    vname = vname or m.masters_name
    heading, heading_facts = b.said(m.title, f"chapter {m.chapter!r} title")
    b.tracks.chapter(f0, heading or vname, heading_facts if heading else vname_facts)
    for k, (cap, length) in enumerate(zip(m.captures, beats, strict=True)):
        n += 1
        s0 = b.f
        lead = (1 + holds[k]) * B  # its words land a beat in, later when the margin holds it
        if k:
            dissolve = b.entry()
        step: dict[str, Any] | None = None
        text = [{"role": "label", "text": cap.label}]
        if k == 0 and words:
            step = {
                "index": None, "words": words, "words_from": m.words_from, "focus_requested": None,
                "focus": None, "target": None, "subjects": [], "z_eff": None, "upscale": None,
                "push": False, "reason": "montage", "land": s0 + lead,
            }  # fmt: skip
            text.append({"role": "words", "text": words})
            if b.placer is not None:
                step.update(_montage_zone(b, m, words, s0 + lead, s0 + length * B))
        shot = b.add(
            {
                "id": f"S{n:02d}",
                "kind": "capture",
                "chapter": m.chapter,
                "text": text,
                "facts": list(dict.fromkeys([*cap.label_facts, *(words_facts if step else [])])),
                "vantage": m.vantage,
                "capture": cap.date,
                "label": cap.label,
                "file": cap.file,
                "dissolve": dissolve,
                "series": series_no,
                "rail": k,
                "step": step,
                "montage": True,
            },
            length,
        )
        shot["key"] = s0 + lead + B if step else shot["f1"] - 1
        if cap.file not in hashes:
            hashes[cap.file] = _sha256(project.masters_dir / cap.file)
        _capture_tracks(b, shot, cap, m, vname, vname_facts, first=k == 0, words_facts=words_facts)
    held = cam.at(b.f - 1)
    return n, None if held == rest else held


def _montage_zone(b: _Builder, m: _Montage, words: str, land: int, f1: int) -> dict[str, Any]:
    """The montage words' zone (`_Placer.choose`): its subject is what changes from the montage's
    first picture to its last, seen along the camera from the words' landing to the first shot's
    end, over the first two pictures (the words fade out over the second's dissolve)."""
    from vantage.film.subject import subject_map

    placer = b.placer
    assert placer is not None
    first, last = m.captures[0], m.captures[-1]

    def choose() -> dict[str, Any]:
        path = _path(b.camera.windows, m.geometry, land, f1, b.grid.third)
        subject = subject_map(placer.seen, first.file, last.file, [])
        return placer.choose(
            b,
            f"{m.chapter} montage",
            words,
            placer.label_base,
            subject,
            [c.file for c in m.captures[:2]],
            {zone: path for zone in placer.zones},
            placer.zones[0],
        )

    return placer.zone(b, f"{m.chapter} montage", words, choose)


def _curtain_shot(
    b: _Builder, c: _Curtain, fonts: dict[str, Any], hashes: dict[str, str], part_f0: int | None = None
) -> Cam | None:
    """The curtain of a compare chapter: an entry beat at split 0, then 2 beats of sweep and 2 of hold
    per step with a split. Before is left of the rule, after right of it. The flash margin may hold
    the entry ("<id>.0") or a step's sweep ("<id>.<k>", k from 1) a beat or two longer (`b.holds`).
    Returns the camera target it ends on when that is not the rest camera."""
    project, lay, film = b.project, b.layout, b.film
    B = b.grid.fpb
    cam = b.camera
    f0 = b.f
    cid = f"C{sum(s['kind'] == 'curtain' for s in b.shots) + 1}"
    rest = b.rest(c)
    prev = b.prev
    same = (
        prev is not None
        and prev["kind"] in ("title", "capture", "stat")
        and prev.get("vantage") == c.vantage
        and prev.get("capture") == c.after.date
    )
    dissolve = None if same else b.entry()
    entry = (1 + b.holds.get(f"{cid}.0", 0)) * B
    if is_image(prev):  # a push before eases back as slowly as it went in
        cam.until(f0)
        cam.hold(f0 + b.easing_back, rest)
    else:
        cam.hold(f0, rest, f0 + entry)
    b.back = None
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
    zmax = c.geometry.zmax(film.max_upscale * b.px)
    keys: list[dict[str, Any]] = [{"a": f0, "b": f0 + entry, "split": 0.0}]
    steps: list[dict[str, Any]] = []
    t = f0 + entry
    target = rest
    tagged = project.config.simulated or project.config.draft
    scale, label_top, (wb, wa) = _curtain_labels(project, fonts, lay, [before_label, after_label], tagged)
    placer = b.placer
    zone = placer.zones[0] if placer is not None else None  # each line keeps the last line's zone if clear
    lines: list[tuple[str | None, str | None]] = []
    for j, step, _ in c.steps:
        line, line_from = (
            curtain_words(step, lambda t: len(FactNotes(project.facts.facts).plain(t) or ""))
            if step is not None
            else (None, None)
        )
        words = say(line, f"{where} step {j} film words") if line else None
        lines.append((words, line_from if words else None))
    # Every line of the curtain at one size, on at most two balanced lines (`cut.words_scale`).
    words_scale = None
    if placer is not None and any(w for w, _ in lines):
        from vantage.film.cut import words_scale as one_size

        words_scale = one_size(placer.t, lay, [w for w, _ in lines if w])
    for k, (j, step, split) in enumerate(c.steps):
        record: dict[str, Any] = {"index": j, "split": split, "f0": t, "words": None, "words_from": None}
        target = rest
        slot = f"{cid}.{len(steps) + 1}"
        words, line_from = lines[k]
        slow = b.slows.get(slot, 0) * B  # the flash margin's slower move, then its sweep
        # Words land on the beat nearest the sweep's midpoint, with the picture they name, and stay
        # until the next line lands (or the curtain ends): the step holds 2 beats after its sweep,
        # longer when its words need it to be read at 17 characters a second.
        land = t + B + (slow // (2 * B)) * B
        hold = 2
        if words:
            record.update(words=words, words_from=line_from, land=land)
            # Fully in B/3 after landing, until B/3 before the next line lands (a beat into the next
            # sweep), as the next sweep starts when that has no line, or to the curtain's end.
            third = b.grid.third
            after = B - 2 * third if k + 1 < len(lines) and lines[k + 1][0] else 0
            need = cue_frames(words, b.grid.fps) + third + (land - t) - (2 * B + slow) - after
            hold = max(2, -(-need // B))
        if step is not None:
            requested = step.film.focus if step.film and step.film.focus else step.focus
            record["focus_requested"] = list(requested) if requested else None
            if requested:
                aimed_cam, aimed = _aim_step(
                    b, c.geometry, requested, bool(step.film and step.film.focus), [], rest, zmax,
                    f"{c.chapter} step {j}", key=slot,
                )  # fmt: skip
                record.update(aimed)
                if aimed["push"]:
                    target = aimed_cam
        now = cam.windows[-1]
        start = max(t, now["b"])  # after an ease back that is still going
        end = max(t + 2 * B + slow, start + b.ease)
        if (now["x"], now["y"], now["zoom"]) != target:
            record["move"] = [start, end]
        span = (2 + hold) * B + slow
        cam.until(t)
        cam.hold(end, target, max(end, t + span))
        if placer is not None and zone is not None and record["words"]:
            record.update(
                _curtain_zone(
                    b, c, f"{c.chapter} step {j}", step, record["words"], placer, (scale, label_top), land,
                    zone, t + span + B - land, words_scale,
                )
            )  # fmt: skip
            zone = record["words_zone"]
        held = b.holds.get(slot, 0) * B
        keys.append({"a": t + 2 * B + slow, "b": t + span + held, "split": split})
        steps.append(record)
        t += span + held
    # Labels show only while the rule (3 units wide) is at least 8 px clear of their ink.
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
    text = [{"role": "before", "text": before_label}, {"role": "after", "text": after_label}]
    text += [{"role": "words", "text": s["words"]} for s in steps if s["words"]]
    shot = b.add(
        {
            "id": cid,
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
            **({"words_scale": words_scale} if words_scale is not None else {}),
        },
        (t - f0) // B,
    )
    best = min(range(len(steps)), key=lambda s: abs(steps[s]["split"] - 0.5))
    shot["key"] = keys[best + 1]["a"] + B
    _curtain_tracks(b, c, shot, label_facts, f0 if part_f0 is None else part_f0)
    for file in (c.before.file, c.after.file):
        if file not in hashes:
            hashes[file] = _sha256(project.masters_dir / file)
    return None if target == rest else target


def _curtain_zone(
    b: _Builder,
    c: _Curtain,
    label: str,
    step: Step | None,
    words: str,
    placer: _Placer,
    labels: tuple[float, float],
    t: int,
    default: str,
    span: int | None = None,
    words_scale: float | None = None,
) -> dict[str, Any]:
    """The zone of a curtain step's line, from its landing `t` for 4 beats (or `span` frames: to the
    next line's landing), over the camera's path; its subject is the step's focus and what changed
    from before to after (a curtain shows both: the contrast is the least over either picture);
    `words_scale`: the curtain's one size for its lines."""
    from vantage.film.subject import subject_map

    B = b.grid.fpb

    def choose() -> dict[str, Any]:
        base = placer.curtain_base(labels[1], labels[0])
        path = _path(b.camera.windows, c.geometry, t, t + (4 * B if span is None else span), b.grid.third)
        requested = (step.film.focus if step.film and step.film.focus else step.focus) if step else None
        points = [(requested[0], requested[1])] if requested else []
        subject = subject_map(placer.seen, c.after.file, c.before.file, points)
        paths = dict.fromkeys(placer.zones, path)  # a curtain's push has no subjects: one path for all
        return placer.choose(
            b, label, words, base, subject, [c.before.file, c.after.file], paths, default, words_scale
        )

    return placer.zone(b, label, words, choose)


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
    vname = vname or c.masters_name
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
