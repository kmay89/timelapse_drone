"""The story cut: a film's shots derived from story.yaml's chapters on a beat grid (film/story.py).

`derive_cut` turns the chapters in a film's scope into shots, in story order:

    title    the first image at rest under a night scrim; the kicker tracks out across beat 0 and
             the title's word groups rise on frames 0, B/3 and 2B/3 of beat 1
    capture  one per capture of each scrub chapter (from..to, minus `exclude`d captures): 1 beat
             for a quiet capture, 3 for a step, 4 for a step that pushes in. Every capture but the
             film's first image opens with a D-frame dissolve; the step's words (film.line, else its
             leading **bold** run) land on beat 1 and a push reaches its target at beat 2⅓
    card     between consecutive captures of different vantages: the chapter's kicker and title on
             night, dipped in and out over D frames
    end      a dip to night, then the brand, credits, URL, fine print and the honesty line

Every shot start, dissolve start, push start and word landing is a multiple of B (film/beat.py).
Push targets are clamped so a master is never enlarged beyond `max_upscale` and recentred on the
hotspots that changed since the previous step. Compare, stats, rise and dip shots are later steps
of the films work; a story with no scrub chapter in scope plays the main vantage as one quiet series.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from vantage.config import CaptureNote, HeroChapter, Hotspot, Project, ScrubChapter, Step
from vantage.film.beat import Grid, beat_grid
from vantage.film.layout import Layout, layout
from vantage.film.plan import (
    FILM_RENDERER,
    SIMULATED_NOTE,
    FilmDef,
    _Printer,
    _sha256,
    fact_snapshot,
    film_size,
)
from vantage.film.safety import check_plan
from vantage.models import FilmPlan, MastersIndex
from vantage.site.build import resolve_capture_ref
from vantage.site.render import capture_label as date_label

HONESTY = {
    "photo": "Every frame is made from real aerial photographs. No in-between images are invented.",
    "simulated": "Every frame is made from the simulated flights. No in-between images are invented.",
}
LEAD_RE = re.compile(r"^\s*\*\*(.+?)\*\*", re.S)
MARGIN = 0.06  # subject margin, as a share of the crop width
SOFT = 1.25  # output px per master px beyond which a push is "soft"
MIN_ZOOM, MIN_SHIFT = 1.08, 0.03  # below both, a clamped push becomes a still step
Cam = tuple[float, float, float]


def honesty_line(provenance: Sequence[str]) -> str:
    """The end card's honesty line, derived from the plan's provenance."""
    return HONESTY["simulated" if "simulated" in provenance else "photo"]


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
            note = self.notes.get(c.date)
            label = say(note.label if note else None, f"captures {c.date} label")
            label = label or date_label(c.date, day=months[c.date[:7]] > 1)
            out.append(_Capture(c.date, c.file, label, list(say.facts), i))
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


# --------------------------------------------------------------------------- #
# The cut
# --------------------------------------------------------------------------- #


@dataclass
class _Builder:
    project: Project
    film: FilmDef
    grid: Grid
    layout: Layout
    warnings: list[str]
    shots: list[dict[str, Any]] = field(default_factory=list)
    f: int = 0  # the next shot's first frame (always on the grid)

    def say(self) -> _Printer:
        return _Printer(self.project, self.warnings)

    def add(self, shot: dict[str, Any], beats: int) -> dict[str, Any]:
        shot["f0"], shot["f1"] = self.f, self.f + beats * self.grid.fpb
        self.f = shot["f1"]
        self.shots.append(shot)
        return shot

    def rest(self, series: _Series) -> Cam:
        if self.layout.width < self.layout.height:
            return series.portrait_focus[0], series.portrait_focus[1], 1.0
        return 0.5, 0.5, 1.0


def _title(b: _Builder, first: _Series) -> dict[str, Any]:
    project, cfg = b.project, b.project.config
    hero = next((ch for ch in project.story.chapters if isinstance(ch, HeroChapter)), None)
    say = b.say()
    kicker = say(hero.kicker, "hero kicker") if hero and hero.kicker else None
    kicker = kicker or say(cfg.kicker, "project.yaml kicker")
    if not kicker:
        place = cfg.location.name if cfg.location else None
        dates = [c.date for c in first.captures]
        years = "–".join(dict.fromkeys((dates[0][:4], dates[-1][:4])))
        kicker = " · ".join(filter(None, (place, years)))
    title = say(hero.title, "hero title") if hero and hero.title else None
    title = title or say(cfg.title, "project.yaml title") or ""
    cap = first.captures[0]
    B, third = b.grid.fpb, b.grid.third
    beats = b.film.title_beats
    shot = {
        "id": "T1",
        "kind": "title",
        "chapter": hero.id if hero and hero.id else None,
        "text": [
            t for t in ({"role": "kicker", "text": kicker}, {"role": "title", "text": title}) if t["text"]
        ],
        "facts": list(say.facts),
        "vantage": first.vantage,
        "capture": cap.date,
        "file": cap.file,
        "groups": [B, B + third, B + 2 * third],
    }
    b.add(shot, beats)
    shot["key"] = min(shot["f1"] - third - 1, 2 * B + third)
    return shot


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
    geo = series.geometry
    explicit = bool(step.film and step.film.focus)
    cam, target = aim(geo, requested, zmax, [] if explicit else cap.subjects, explicit=explicit)
    z = max(requested[2], 1.0)
    label = f"{series.chapter} {cap.date}"
    if zmax < z - 1e-9:
        b.warnings.append(
            f"{label}: push clamped from {z:g} to {zmax:.3g} ({b.film.id}; max_upscale {b.film.max_upscale:g})"
        )
    upscale = geo.s0 * cam[2]
    if upscale > SOFT + 1e-9:
        b.warnings.append(f"{label}: soft push, {upscale:.2f} output px per master px ({b.film.id})")
    a, c = geo.centre(rest[0], rest[1], 1.0), geo.centre(cam[0], cam[1], cam[2])
    shift = math.hypot(c[0] - a[0], c[1] - a[1])
    push = not (cam[2] < MIN_ZOOM and shift < MIN_SHIFT)
    record.update(
        focus=[cam[0], cam[1], cam[2]],
        target=target,
        subjects=[] if explicit else [h.id for h in cap.subjects],
        z_eff=cam[2],
        upscale=upscale,
        push=push,
        reason=None if push else "clamped",
    )
    return record, (cam if push else None)


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
    lay = layout(f"story-{film.aspect}", width, height, safe=film.safe, type_scale=film.type_scale)
    caps = _Captures(project, index, warnings)
    chapters = project.story.chapters
    series: list[_Series] = []
    for i in in_scope(project, film, index, warnings):
        ch = chapters[i]
        if (
            isinstance(ch, ScrubChapter)
            and (s := _series(project, i, ch, caps, (width, height)))
            and s.captures
        ):
            series.append(s)
    main = main_vantage(project, index)
    if not series and main is not None and caps.of(main):
        series = [_fallback_series(project, caps, main, (width, height))]
    if not series:
        raise FileNotFoundError(f"no masters to film in {project.masters_dir}; run `vantage process` first")
    total_images = sum(len(s.captures) for s in series)

    b = _Builder(project, film, grid, lay, warnings)
    upscaled = {s.vantage for s in series if s.geometry.s0 > film.max_upscale + 1e-9}
    for vid in sorted(upscaled):
        geo = next(s.geometry for s in series if s.vantage == vid)
        warnings.append(
            f"vantage {vid}: master upscaled even at rest ({geo.s0:.2f}× > max_upscale "
            f"{film.max_upscale:g}; {geo.mw}×{geo.mh} masters for a {width}×{height} film)"
        )
    _title(b, series[0])

    # Camera: hold windows in frames ({a, b, x, y, zoom}); smoothstep between them (beat.keyed).
    camera: list[dict[str, Any]] = []

    def hold(a: int, cam: Cam) -> None:
        if camera and (camera[-1]["x"], camera[-1]["y"], camera[-1]["zoom"]) == cam:
            return
        camera.append({"a": a, "b": a, "x": cam[0], "y": cam[1], "zoom": cam[2]})

    def until(f: int) -> None:
        camera[-1]["b"] = max(camera[-1]["b"], f)

    hold(0, b.rest(series[0]))
    n, cards = 0, 0
    prev: dict[str, Any] | None = None
    pushed: Cam | None = None  # the previous capture's push target, still held
    for number, s in enumerate(series):
        rest = b.rest(s)
        zmax = s.geometry.zmax(film.max_upscale)
        if prev is not None and prev["vantage"] != s.vantage:
            cards += 1
            say = b.say()
            kicker = say(s.kicker, f"chapter {s.chapter!r} kicker") if s.kicker else None
            title = (say(s.title, f"chapter {s.chapter!r} title") if s.title else None) or s.name
            card = b.add(
                {
                    "id": f"K{cards}",
                    "kind": "card",
                    "chapter": s.chapter,
                    "text": [
                        t
                        for t in ({"role": "kicker", "text": kicker}, {"role": "title", "text": title})
                        if t["text"]
                    ],
                    "facts": list(say.facts),
                    "vantage": s.vantage,
                    "dip": D,
                },
                4,
            )
            card["key"] = card["f0"] + 2 * B
            until(card["f0"] + D)
            camera.append({"a": card["f1"], "b": card["f1"], "x": rest[0], "y": rest[1], "zoom": rest[2]})
            pushed = None
            prev = card
        for j, cap in enumerate(s.captures):
            n += 1
            first = n == 1
            step, target = _push(b, s, cap, rest, zmax)
            beats = 1 if step is None else (4 if target else 3)
            if total_images == 1:
                beats = max(beats, 3)
            f0 = b.f
            if first:
                dissolve = None
            elif prev is not None and prev["kind"] == "card":
                dissolve = {"from": "night", "f0": f0, "f1": f0 + D}
            else:
                dissolve = {"from": prev["capture"] if prev else None, "f0": f0, "f1": f0 + D}
            if pushed is not None:  # the previous push eases back during this dissolve
                until(f0)
                hold(f0 + D, rest)
            if target is not None:
                until(f0 + B)
                camera.append(
                    {
                        "a": f0 + 2 * B + third,
                        "b": f0 + beats * B,
                        "x": target[0],
                        "y": target[1],
                        "zoom": target[2],
                    }
                )
            pushed = target
            facts = list(dict.fromkeys([*cap.label_facts, *(step["facts"] if step else [])]))
            text = [{"role": "label", "text": cap.label}]
            if step and step["words"]:
                text.append({"role": "words", "text": step["words"]})
                step["land"] = f0 if first else f0 + B
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
                    "series": number,
                    "rail": j,
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
            prev = shot

    # End card.
    brand = project.brand
    say = b.say()
    provenance = ["simulated" if cfg.simulated else "photo"]
    honesty = honesty_line(provenance)
    lines: list[dict[str, Any]] = [{"role": "brand", "text": brand.name}]
    if brand.credit_line and (t := say(brand.credit_line, "brand.yaml credit_line")):
        lines.append({"role": "credit", "text": t})
    for partner in brand.partners:
        lines += [{"role": "role", "text": partner.role}, {"role": "partner", "text": partner.name}]
    if cfg.output.base_url:
        lines.append({"role": "link", "text": cfg.output.base_url.split("://")[-1].rstrip("/")})
    for text, where in ((brand.copyright, "copyright"), (brand.disclaimer, "disclaimer")):
        if text and (t := say(text, f"brand.yaml {where}")):
            lines.append({"role": "fine", "text": t})
    lines.append({"role": "honesty", "text": honesty})
    if cfg.simulated:
        lines.append({"role": "fine", "text": SIMULATED_NOTE})
    if pushed is not None:
        until(b.f)
        hold(b.f + D, b.rest(series[-1]))
    end = b.add(
        {
            "id": "E1",
            "kind": "end",
            "chapter": None,
            "text": lines,
            "facts": list(say.facts),
            "dip": D,
            "honesty": honesty,
        },
        film.end_beats,
    )
    end["key"] = min(end["f1"] - 1, end["f0"] + D + 2 * B)
    frames = b.f
    until(frames)

    masters: dict[str, Any] = {}
    for shot in b.shots:
        if "file" in shot:
            mv = index.vantages[shot["vantage"]]
            masters[shot["file"]] = {"sha256": hashes[shot["file"]], "w": mv.width, "h": mv.height}
    colors = brand.colors
    poster = next(sh for sh in reversed(b.shots) if sh["kind"] == "capture")["f1"] - 1
    plan: dict[str, Any] = {
        "slug": project.slug,
        "cut": "story",
        "preset": film.preset,
        "renderer": FILM_RENDERER["story"],
        "width": width,
        "height": height,
        "fps": fps,
        "frames": frames,
        "loop": False,
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
        "camera": camera,
        "shots": b.shots,
        "captions": [],
        "chapters": [],
        "poster": poster,
    }
    check_plan(plan)
    used = sorted({f for shot in b.shots for f in shot["facts"]})
    return FilmPlan(
        id=film.id,
        plan=plan,
        facts=fact_snapshot(project, used),
        warnings=warnings,
        encode={
            "crf": 20 if fast else 18,
            "preset": "veryfast" if fast else "slow",
            "web": None,
            "gop": None,
            "tracks": False,
        },
    )


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
