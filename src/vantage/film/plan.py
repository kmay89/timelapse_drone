"""Film plans (the Reel Ledger): YAML + masters + fonts + settings → one FilmPlan per film.

Planning is pure and renders nothing. A plan holds everything that decides a film's frames (size,
timeline, printed text with its facts resolved, the sha256 of every master and font it reads, the
colours), so a renderer evaluating it gives the same bytes from the same plan, and `check_films`
can tell whether a rendered film still matches the YAML, the masters and the facts that would make
it today. `vantage film` writes the plan next to each MP4 as <slug>-<id>.plan.json.

The derived films are '16x9' and, with output.film.vertical, '9x16'; story.yaml `films:` overrides
them or adds more (`resolve_films`: FilmSpec field > preset > output.film > built-in default). Each
film has a cut: `story` (film/story.py: shots cut from the chapters on a beat grid) or `classic` (a
title card, one section per vantage in which a capture holds `hold_s` and dissolves `fade_s` into
the next, a card between vantages and an end card; the 9:16 classic film keeps the main vantage).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml

from vantage import log
from vantage.config import FILM_ID_RE, BrandKit, FilmSettings, FontSpec, Project, ScrubChapter
from vantage.film.layout import layout
from vantage.film.tracks import SUFFIXES, Tracks, iso639_2, sentence, track_facts, track_texts
from vantage.media.ffmpeg import ENCODER_THREADS
from vantage.models import FilmPlan, MastersIndex
from vantage.paths import RUNTIME_DIR
from vantage.site.facts import FactNotes
from vantage.site.render import capture_label as date_label

FILM_RENDERER = {"classic": 1, "story": 4}  # bumped when frame(i) changes for an equal plan
FAST_LONG_EDGE = 960
WEB_LIMIT = 25 * 1024 * 1024  # bytes: the largest file static hosts serve (Cloudflare Pages)
WEB_MAXRATE_CAP = 6_000_000  # bit/s
PLAN_SUFFIX = ".plan.json"
STORYBOARD_SUFFIX = ".storyboard.jpg"
SIMULATED_NOTE = "Simulated imagery for demonstration; not actual aerial footage."
HONESTY = {
    "photo": "Every frame is made from real aerial photographs. No in-between images are invented.",
    "simulated": "Every frame is made from the simulated flights. No in-between images are invented.",
}
Role = Literal["display", "text", "numeric"]
ROLES: tuple[Role, ...] = ("display", "text", "numeric")


def fast_mode() -> bool:
    return os.environ.get("VANTAGE_FAST", "").lower() not in ("", "0", "false", "no")


def _even(n: float) -> int:
    return max(2, round(n / 2) * 2)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Printed text and facts
# --------------------------------------------------------------------------- #


def _plain(project: Project, text: str | None) -> str | None:
    """Story, project and brand text as the site prints it: `{fact:…}` tokens become the fact's text."""
    return FactNotes(project.facts.facts).plain(text)


def honesty_line(provenance: Sequence[str]) -> str:
    """The end card's (and the transcript's) honesty line, derived from the plan's provenance. Only
    'photo' (drone vantages) and 'simulated' have one; any other provenance is an error, never
    described as photographs."""
    if "simulated" in provenance:
        return HONESTY["simulated"]
    unknown = sorted(set(provenance) - {"photo"})
    if unknown or not provenance:
        raise ValueError(
            f"no honesty line for provenance {unknown or list(provenance)}: films show only drone vantages"
        )
    return HONESTY["photo"]


FILM_KINDS = ("drone",)  # vantage kinds films show; ortho, ground, archival, plan need their own line and tag


def film_vantages(project: Project, index: MastersIndex) -> list[str]:
    """Leave the vantages films cannot show yet out of `index` (in place): every vantage whose
    story.yaml `kind` is not drone. An orthomosaic is not a photograph, a plan is a drawing, a ground
    view is not aerial and an archival image needs a recorded licence, while the honesty line says
    "real aerial photographs". Returns one warning per vantage left out."""
    kinds = {v.id: v.kind for v in project.story.vantages}
    warnings = []
    for vid in sorted(index.vantages):
        kind = kinds.get(vid, "drone")
        if kind not in FILM_KINDS:
            del index.vantages[vid]
            warnings.append(
                f"vantage {vid} (kind: {kind}): left out of the films, which show only drone vantages"
            )
    return warnings


def capture_labels(project: Project, dates: Sequence[str]) -> dict[str, str]:
    """Visit labels exactly as the site shows them: story.yaml's label, else 'June 2025'
    ('June 14, 2025' when the vantage has two visits that month)."""
    labels = {n.date.isoformat(): _plain(project, n.label) for n in project.story.captures if n.label}
    months = Counter(d[:7] for d in dates)
    return {d: labels.get(d) or date_label(d, day=months[d[:7]] > 1) for d in dates}


@dataclass
class _Printer:
    """Resolves the text one shot prints and collects the fact ids it prints, in first-use order.

    A text that uses a do-not-print fact is never printed in a film: it comes back as None and the
    plan records a warning. With `releasable_only` (an unattended loop film of a project that is not
    a draft), neither is a text that prints a fact that is not releasable."""

    project: Project
    warnings: list[str]
    facts: list[str] = field(default_factory=list)
    releasable_only: bool = False

    def __call__(self, text: str | None, where: str) -> str | None:
        if text is None:
            return None
        notes = FactNotes(self.project.facts.facts)
        printed = notes.plain(text)
        facts = self.project.facts.facts
        if any(facts[f].status == "do-not-print" for f in notes.order):
            warning = f"{where}: left out of the films (it uses a do-not-print fact)"
            if warning not in self.warnings:
                self.warnings.append(warning)
            return None
        if self.releasable_only and (bad := [f for f in notes.order if not facts[f].releasable]):
            warning = f"{where}: left out of the loop film (fact {bad[0]!r} is not releasable)"
            if warning not in self.warnings:
                self.warnings.append(warning)
            return None
        self.add(notes.order)
        return printed

    def add(self, ids: Sequence[str]) -> None:
        self.facts += [f for f in ids if f not in self.facts]


# --------------------------------------------------------------------------- #
# Classic timeline
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Shot:
    date: str
    label: str
    note: str | None
    file: str  # relative to masters/
    label_facts: tuple[str, ...] = ()
    note_facts: tuple[str, ...] = ()


@dataclass
class _Section:
    name: str
    vantage: str
    shots: list[_Shot]
    focus: tuple[float, float]
    facts: tuple[str, ...] = ()  # printed by the name (on a section card)


def _sections(project: Project, index: MastersIndex, warnings: list[str] | None = None) -> list[_Section]:
    """Main vantage (the first scrub chapter's, else the first) first, then every other with ≥ 2 visits."""
    warnings = [] if warnings is None else warnings
    story = {v.id: v for v in project.story.vantages}
    order = [vid for vid in story if vid in index.vantages] + [v for v in index.vantages if v not in story]
    scrub = [ch.vantage for ch in project.story.chapters if isinstance(ch, ScrubChapter)]
    main = next((vid for vid in scrub if vid in index.vantages), order[0] if order else None)
    excluded = {n.date.isoformat() for n in project.story.captures if n.exclude}
    custom = {n.date.isoformat(): n for n in project.story.captures}
    sections: list[_Section] = []
    for vid in sorted(order, key=lambda v: v != main):
        mv = index.vantages[vid]
        captures = sorted((c for c in mv.captures if c.date not in excluded), key=lambda c: c.date)
        months = Counter(c.date[:7] for c in captures)
        shots = []
        for c in captures:
            entry = custom.get(c.date)
            on_label, on_note = _Printer(project, warnings), _Printer(project, warnings)
            label = on_label(entry.label if entry else None, f"captures {c.date} label")
            note = on_note(entry.note if entry else None, f"captures {c.date} note")
            label = label or date_label(c.date, day=months[c.date[:7]] > 1)
            shots.append(_Shot(c.date, label, note, c.file, tuple(on_label.facts), tuple(on_note.facts)))
        vantage = story.get(vid)
        focus = vantage.portrait_focus if vantage and vantage.portrait_focus else (0.5, 0.5)
        if shots and (not sections or len(shots) > 1):
            say = _Printer(project, warnings)
            name = (say(vantage.name, f"vantage {vid} name") if vantage else None) or mv.name
            sections.append(_Section(name, vid, shots, (focus[0], focus[1]), tuple(say.facts)))
    if not sections:
        raise FileNotFoundError(f"no masters to film in {project.masters_dir}; run `vantage process` first")
    return sections


def _span(shots: Sequence[_Shot], say: _Printer | None = None) -> str:
    """'June 2025 – January 2026' (noting the facts the two labels print)."""
    if say is not None:
        say.add([*shots[0].label_facts, *shots[-1].label_facts])
    first, last = shots[0].label, shots[-1].label
    return first if first == last else f"{first} – {last}"


def _frame(t: float, fps: int) -> int:
    """The first frame k with k / fps >= t."""
    return math.ceil(round(t * fps, 6))


# --------------------------------------------------------------------------- #
# Inputs: fonts, masters, colours
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


def font_files(brand: BrandKit) -> dict[Role, Path | None]:
    typo = brand.typography
    return {
        "display": _font_file(brand, typo.display),
        "text": _font_file(brand, typo.text),
        "numeric": _font_file(brand, typo.numeric or typo.text),
    }


def font_ref(path: Path | None, brand: BrandKit) -> str:
    """'runtime:<file under runtime/fonts>', 'brand:<file relative to brand.yaml>' or 'pillow:default'."""
    if path is None:
        return "pillow:default"
    bundled = RUNTIME_DIR / "fonts"
    if path.is_relative_to(bundled):
        return "runtime:" + path.relative_to(bundled).as_posix()
    root = brand.root or Path.cwd()
    return "brand:" + Path(os.path.relpath(path, root)).as_posix()


def font_path(ref: str, brand: BrandKit) -> Path | None:
    """The file a plan's font reference names (inverse of `font_ref`)."""
    kind, _, rel = ref.partition(":")
    if kind == "runtime":
        return RUNTIME_DIR / "fonts" / rel
    if kind == "brand":
        return (brand.root or Path.cwd()) / rel
    return None


# --------------------------------------------------------------------------- #
# The film set
# --------------------------------------------------------------------------- #


Aspect = Literal["16x9", "9x16"]


@dataclass(frozen=True)
class FilmDef:
    """One film's resolved settings (see `resolve_films`)."""

    id: str
    aspect: Aspect
    cut: Literal["story", "classic"] = "classic"
    preset: str | None = None
    bpm: float = 100.0
    chapters: str | tuple[str, ...] = "all"  # all | main | chapter ids
    stats: int = 3
    stats_surface: Literal["night", "paper"] = "night"
    title_beats: int = 4
    end_beats: int = 6
    safe: tuple[float, float, float, float] | None = None  # None: the layout preset's
    type_scale: float = 1.0
    max_upscale: float = 1.6
    loop: bool = False
    sound: str = "none"
    auto: bool = True
    enabled: bool = True
    hours: float | None = None
    beats: tuple[int, int, int] = (1, 3, 4)  # capture beats: no step, a step, a step that pushes


# The `screen` preset: the silent event-screen loop (rendered only on request). Its captures linger
# (2 beats quiet, 4 with a step, 5 with a push), its end card is 4 beats of brand and URL.
PRESETS: dict[str, dict[str, Any]] = {
    "screen": {
        "cut": "story",
        "aspect": "16x9",
        "loop": True,
        "bpm": 75.0,
        "type_scale": 1.6,
        "safe": (0.05, 0.05, 0.05, 0.05),
        "auto": False,
        "title_beats": 4,
        "end_beats": 4,
        "beats": (2, 4, 5),
    }
}
LOOP_BEATS = {"rise": 2, "dip": 2}  # a loop rises from night and dips back to it
GOP_BEATS = 2  # a loop film's GOP (and its length's multiple), in beats


def resolve_films(project: Project) -> dict[str, FilmDef]:
    """Every enabled film this project can make, by id: the derived '16x9' (chapters all) and, with
    output.film.vertical, '9x16' (chapters main), merged with story.yaml `films:` in that order.
    Precedence: FilmSpec field > preset > output.film (cut, bpm, max_upscale) > built-in default."""
    settings = project.config.output.film
    derived: dict[str, dict[str, Any]] = {"16x9": {"aspect": "16x9", "chapters": "all"}}
    if settings.vertical:
        derived["9x16"] = {"aspect": "9x16", "chapters": "main"}
    specs = project.story.films
    output = {"cut": settings.cut, "bpm": settings.bpm, "max_upscale": settings.max_upscale}
    films: dict[str, FilmDef] = {}
    for fid in [*derived, *(f for f in specs if f not in derived)]:
        if not FILM_ID_RE.fullmatch(fid) or fid.endswith("-web"):
            raise ValueError(f"film id {fid!r} must match [a-z0-9][a-z0-9-]{{0,31}} and not end in -web")
        builtin = {"aspect": fid if fid in ("16x9", "9x16") else "16x9", **derived.get(fid, {})}
        explicit = specs[fid].model_dump(exclude_unset=True) if fid in specs else {}
        preset = PRESETS[explicit["preset"]] if explicit.get("preset") else {}
        merged = {**builtin, **output, **preset, **explicit}
        if isinstance(merged.get("chapters"), list):
            merged["chapters"] = tuple(merged["chapters"])
        if merged.get("safe") is not None:
            merged["safe"] = tuple(merged["safe"])
        film = FilmDef(id=fid, **merged)
        if film.enabled:
            films[fid] = film
    return films


def film_size(settings: FilmSettings, aspect: Aspect, *, fast: bool) -> tuple[int, int]:
    """16:9 is output.film width × height; 9:16 is short × short·16/9 (short: the 16:9 short edge).
    VANTAGE_FAST caps the long edge at 960 px."""

    def fit(w: float, h: float) -> tuple[int, int]:
        s = min(1.0, FAST_LONG_EDGE / max(w, h)) if fast else 1.0
        return _even(w * s), _even(h * s)

    short = min(settings.width, settings.height)
    return fit(short, short * 16 / 9) if aspect == "9x16" else fit(settings.width, settings.height)


def film_id(slug: str, name: str) -> str | None:
    """The film id in a file name under film/ (<slug>-<id>.mp4, -web.mp4, .plan.json, …), or None."""
    if not name.startswith(f"{slug}-"):
        return None
    fid = name[len(slug) + 1 :].split(".", 1)[0]
    return fid.removesuffix("-web") or None


def web_maxrate(duration_s: float) -> int:
    """The web encode's VBV cap R in bit/s: min(6 Mbit/s, floor(0.9 · 25 MiB · 8 / duration)),
    floored to a whole kbit/s. With bufsize 2R the web copy stays under 25 MiB."""
    rate = math.floor(0.9 * WEB_LIMIT * 8 / max(duration_s, 1e-3))
    return min(WEB_MAXRATE_CAP, rate // 1000 * 1000)


def has_tracks(film: FilmDef, settings: FilmSettings) -> bool:
    """Whether a film gets captions, chapters and a transcript: output.film.tracks, never a loop film."""
    return settings.tracks and not film.loop


def encode_settings(
    film: FilmDef,
    settings: FilmSettings,
    frames: int,
    fps: int,
    *,
    fast: bool,
    fpb: int | None = None,
    lang: str = "und",
) -> dict:
    """plan.json `encode` (not hashed: it changes bytes, not frames). `web` is the -web.mp4 encode:
    crf 23 under a VBV cap, null for loop films and when output.film.web is false. `gop` is a loop
    film's closed GOP in frames (2 beats of `fpb`), else null. `tracks`: the MP4s embed the captions
    and chapters (film/tracks.py), whose subtitle track is tagged `lang` (ISO 639-2). `threads`:
    x264's thread count, pinned because it changes the bytes (media/ffmpeg.py ENCODER_THREADS)."""
    preset = "veryfast" if fast else "slow"
    web = None
    if settings.web and not film.loop:
        rate = web_maxrate(frames / fps)
        web = {"crf": 23, "preset": preset, "maxrate": rate, "bufsize": 2 * rate, "threads": ENCODER_THREADS}
    gop = GOP_BEATS * fpb if film.loop and fpb else None
    return {
        "crf": 20 if fast else 18,
        "preset": preset,
        "threads": ENCODER_THREADS,
        "web": web,
        "gop": gop,
        "tracks": has_tracks(film, settings),
        "lang": lang,
    }


def _check_timing(project: Project) -> None:
    settings = project.config.output.film
    timing = (settings.title_card_s, settings.fade_s, settings.end_card_s)
    if settings.fps <= 0 or settings.hold_s <= 0 or min(timing) < 0:
        raise ValueError("output.film needs fps and hold_s > 0, and fade_s, title_card_s, end_card_s >= 0")


def plan_films(
    project: Project, *, only: Sequence[str] | None = None, fast: bool | None = None
) -> dict[str, FilmPlan]:
    """One FilmPlan per film (the `auto` films, or the ids in `only`, in that order). Pure: reads
    the YAML already loaded, masters/index.json and the master and font files; renders nothing."""
    films = resolve_films(project)
    ids = [f for f, film in films.items() if film.auto] if only is None else list(dict.fromkeys(only))
    unknown = [fid for fid in ids if fid not in films]
    if unknown:
        raise ValueError(f"unknown film {', '.join(map(repr, unknown))}; this project has {', '.join(films)}")
    for fid in ids:
        if films[fid].loop and films[fid].cut != "story":
            raise ValueError(f"film {fid!r}: a loop film needs the story cut (cut: story)")
    index_path = project.masters_dir / "index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"no {index_path}; run `vantage process {project.slug}` first")
    _check_timing(project)
    fast = fast_mode() if fast is None else fast
    index = MastersIndex.load(index_path)
    left_out = film_vantages(project, index)
    hashes: dict[str, str] = {}
    fonts = {}
    for role, path in font_files(project.brand).items():
        fonts[role] = {
            "file": font_ref(path, project.brand),
            "sha256": _sha256(path) if path is not None and path.is_file() else None,
        }
    plans = {}
    for fid in ids:
        warnings: list[str] = list(left_out)
        if films[fid].cut == "story":
            from vantage.film.story import derive_cut

            plans[fid] = derive_cut(project, films[fid], index, fonts, hashes, warnings, fast=fast)
            continue
        sections = _sections(project, index, warnings)
        plans[fid] = _classic(project, films[fid], sections, index, fonts, hashes, warnings, fast=fast)
    return plans


def _classic(
    project: Project,
    film: FilmDef,
    sections: list[_Section],
    index: MastersIndex,
    fonts: dict[str, Any],
    hashes: dict[str, str],
    warnings: list[str],
    *,
    fast: bool,
) -> FilmPlan:
    cfg, brand, settings = project.config, project.brand, project.config.output.film
    portrait = film.aspect == "9x16"
    if portrait:
        sections = sections[:1]

    width, height = film_size(settings, film.aspect, fast=fast)
    fps, hold, fade = settings.fps, settings.hold_s, settings.fade_s
    first = sections[0]

    # Cards: (text | None for the accent rule, style name, gap above in units), as _Film typesets them.
    def card(
        sid: str, role: str, blocks: list[tuple[str | None, str, float]], say: _Printer, **links: str | None
    ) -> dict[str, Any]:
        return {
            "id": sid,
            "kind": "card",
            "role": role,
            "chapter": None,
            "text": [{"role": style.lower(), "text": text} for text, style, _ in blocks if text is not None],
            "facts": list(say.facts),
            "blocks": [{"text": text, "style": style, "gap": gap} for text, style, gap in blocks],
            **links,
        }

    say = _Printer(project, warnings)
    place = cfg.location.name if cfg.location else None
    years = "–".join(dict.fromkeys((first.shots[0].date[:4], first.shots[-1].date[:4])))
    kicker = say(cfg.kicker, "project.yaml kicker") or " · ".join(filter(None, (place, years)))
    title: list[tuple[str | None, str, float]] = [(kicker, "KICKER", 0), (None, "KICKER", 26)]
    if (text := say(cfg.title, "project.yaml title")) is not None:
        title.append((text, "TITLE", 34))
    if cfg.subtitle and (text := say(cfg.subtitle, "project.yaml subtitle")) is not None:
        title.append((text, "SUBTITLE", 20))
    title.append((_span(first.shots, say), "SPAN", 34))
    shots: list[dict[str, Any]] = [card("T1", "title", title, say, prev=None, next="S01")]

    t = settings.title_card_s
    timing: list[dict[str, float]] = [{"start": 0.0, "duration": t, "dissolve": min(fade, t / 3)}]
    lead = timing[0]["dissolve"]
    section_seconds: list[dict[str, float]] = []
    for k, section in enumerate(sections):
        sid = f"S{k + 1:02d}"
        if k:
            d = settings.title_card_s
            say = _Printer(project, warnings)
            blocks = [(_span(section.shots, say), "KICKER", 0.0), (None, "KICKER", 26.0)]
            say.add(section.facts)
            shots.append(
                card(
                    f"K{k}",
                    "section",
                    [*blocks, (section.name, "SECTION_TITLE", 34)],
                    say,
                    prev=f"S{k:02d}",
                    next=sid,
                )
            )
            dissolve = min(fade, d / 3)
            timing.append({"start": t, "duration": d, "dissolve": dissolve})
            section_seconds[-1]["tail"] = lead = dissolve
            t += d
        n = len(section.shots)
        length = n * hold + (n - 1) * fade
        section_seconds.append({"start": t, "length": length, "lead": lead, "tail": 0.0})
        lead = 0.0
        caps = []
        for i, shot in enumerate(section.shots):
            mv = index.vantages[section.vantage]
            if shot.file not in hashes:
                hashes[shot.file] = _sha256(project.masters_dir / shot.file)
            caps.append(
                {
                    "date": shot.date,
                    "label": shot.label,
                    "note": shot.note,
                    "file": shot.file,
                    "key": _frame(t + i * (hold + fade) + hold / 2, fps),
                    "facts": list(dict.fromkeys([*shot.label_facts, *shot.note_facts])),
                    "w": mv.width,
                    "h": mv.height,
                }
            )
        say = _Printer(project, warnings)
        for cap in caps:
            say.add(cap["facts"])
        text = []
        for cap in caps:
            text.append({"role": "label", "text": cap["label"]})
            if cap["note"]:
                text.append({"role": "note", "text": cap["note"]})
        shots.append(
            {
                "id": sid,
                "kind": "section",
                "chapter": None,
                "text": text,
                "facts": say.facts,
                "vantage": section.vantage,
                "name": section.name,
                "focus": [section.focus[0], section.focus[1]],
                "shots": caps,
            }
        )
        t += length
    d = settings.end_card_s
    say = _Printer(project, warnings)
    end: list[tuple[str | None, str, float]] = [(brand.name, "BRAND", 0)]
    if brand.credit_line and (text := say(brand.credit_line, "brand.yaml credit_line")) is not None:
        end.append((text, "CREDIT", 18))
    for i, partner in enumerate(brand.partners):
        end += [(partner.role, "ROLE", 22 if i else 40), (partner.name, "PARTNER", 8)]
    if cfg.output.base_url:
        end.append((cfg.output.base_url.split("://")[-1].rstrip("/"), "LINK", 40))
    fine = [
        say(brand.copyright, "brand.yaml copyright"),
        say(brand.disclaimer, "brand.yaml disclaimer"),
        SIMULATED_NOTE if cfg.simulated else None,
    ]
    end += [(line, "FINE", 8 if i else 40) for i, line in enumerate(filter(None, fine))]
    shots.append(card("E1", "end", end, say, prev=f"S{len(sections):02d}", next=None))
    end_dissolve = min(fade, d / 3)
    timing.append({"start": t, "duration": d, "dissolve": end_dissolve})
    section_seconds[-1]["tail"] = end_dissolve
    total = t + d
    poster = section_seconds[0]["start"] + (len(first.shots) - 1) * (hold + fade) + 0.85 * hold
    frames = max(1, round(total * fps))

    cards = iter(timing)
    secs = iter(section_seconds)
    for shot in shots:
        if shot["kind"] == "card":
            s = next(cards)
            shot["seconds"] = s
            shot["f0"], shot["f1"] = _frame(s["start"], fps), _frame(s["start"] + s["duration"], fps)
            shot["key"] = _frame(s["start"] + s["duration"] / 2, fps)
        else:
            s = next(secs)
            shot["seconds"] = s
            shot["f0"], shot["f1"] = _frame(s["start"], fps), _frame(s["start"] + s["length"], fps)
            shot["key"] = shot["shots"][0]["key"]
    shots[-1]["f1"] = frames
    lay = layout(f"classic-{film.aspect}", width, height, safe=film.safe, type_scale=film.type_scale)
    masters = {}
    for shot in shots:
        for cap in shot.get("shots", []):
            masters[cap["file"]] = {"sha256": hashes[cap["file"]], "w": cap.pop("w"), "h": cap.pop("h")}
    colors = brand.colors
    plan: dict[str, Any] = {
        "slug": project.slug,
        "cut": "classic",
        "preset": None,
        "renderer": FILM_RENDERER["classic"],
        "width": width,
        "height": height,
        "fps": fps,
        "frames": frames,
        "loop": False,
        "sound": "none",
        "grid": None,
        "layout": lay.record(),
        "draft": cfg.draft,
        "simulated": cfg.simulated,
        "tag": [w for w, on in (("Simulated imagery", cfg.simulated), ("Preview", cfg.draft)) if on],
        "provenance": ["simulated" if cfg.simulated else "photo"],
        "inputs": {
            "masters": masters,
            "fonts": fonts,
            "colors": {
                "night": colors.night,
                "paper": colors.paper,
                "ink": colors.ink,
                "accent": colors.accent,
            },
        },
        "camera": [],
        "shots": shots,
        **_classic_tracks(project, film, sections, shots, frames, kicker, warnings),
        "hold_s": hold,
        "fade_s": fade,
        "seconds_total": total,
        "poster_seconds": poster,
    }
    used = sorted({f for shot in shots for f in shot["facts"]} | set(track_facts(plan)))
    return FilmPlan(
        id=film.id,
        plan=plan,
        facts=fact_snapshot(project, used),
        warnings=warnings,
        encode=encode_settings(film, settings, frames, fps, fast=fast, lang=iso639_2(cfg.lang)),
    )


def said(
    project: Project, raw: str | None, where: str, warnings: list[str], *, releasable_only: bool = False
) -> tuple[str | None, list[str]]:
    """(text as printed through the facts, the fact ids it prints); (None, []) for a text that uses
    a do-not-print fact, or with `releasable_only` a fact that is not releasable (with a warning)."""
    say = _Printer(project, warnings, releasable_only=releasable_only)
    text = say(raw, where)
    return text, list(say.facts) if text is not None else []


def _classic_tracks(
    project: Project,
    film: FilmDef,
    sections: list[_Section],
    shots: list[dict[str, Any]],
    frames: int,
    kicker: str,
    warnings: list[str],
) -> dict[str, Any]:
    """The classic cut's captions (title, each capture's label and, when it fits, its note, section
    cards, the credit line), chapters (one per section, then Credits) and transcript lines."""
    cfg, brand, settings = project.config, project.brand, project.config.output.film
    fps, step = settings.fps, settings.hold_s + settings.fade_s
    tracks = Tracks(fps, enabled=has_tracks(film, settings))
    title, title_facts = said(project, cfg.title, "project.yaml title", warnings)
    subtitle, subtitle_facts = said(project, cfg.subtitle, "project.yaml subtitle", warnings)
    _, kicker_facts = said(project, cfg.kicker, "project.yaml kicker", warnings)
    credit, credit_facts = said(project, brand.credit_line, "brand.yaml credit_line", warnings)
    by_vantage = {s.vantage: s for s in sections}
    for shot in shots:
        sec = shot["seconds"]
        if shot["kind"] == "card" and shot["role"] == "title":
            tracks.cue(0, title or kicker, title_facts if title else kicker_facts)
            tracks.line(0, f"Title card: {sentence(kicker, title)}", [*kicker_facts, *title_facts])
        elif shot["kind"] == "card" and shot["role"] == "section":
            section = by_vantage[next(s["vantage"] for s in shots if s["id"] == shot["next"])]
            start = _frame(sec["start"] + sec["dissolve"] / 2, fps)
            tracks.chapter(shot["f0"], section.name, section.facts)
            tracks.cue(start, section.name, section.facts)
            tracks.line(start, f"Chapter card: {section.name}", section.facts)
        elif shot["kind"] == "section":
            section = by_vantage[shot["vantage"]]
            if section is sections[0]:
                tracks.chapter(shot["f0"], section.name, section.facts)
            for j, cap in enumerate(section.shots):
                start = _frame(sec["start"] + j * step - (settings.fade_s / 2 if j else 0), fps)
                with_note = sentence(cap.label, cap.note) if cap.note else None
                tracks.cue(
                    start,
                    cap.label,
                    cap.label_facts,
                    (with_note, [*cap.label_facts, *cap.note_facts]) if with_note else None,
                    span=cap.label,
                )
                lead = f"Aerial view ({section.name}), {cap.label}" if j == 0 else f"Same view, {cap.label}"
                facts = [*cap.label_facts, *cap.note_facts, *(section.facts if j == 0 else ())]
                tracks.line(start, f"{lead}: {cap.note}" if cap.note else f"{lead}.", facts)
        elif shot["kind"] == "card":  # the end card
            start = _frame(sec["start"] + sec["dissolve"] / 2, fps)
            tracks.chapter(shot["f0"], "Credits")
            tracks.cue(start, credit or brand.name, credit_facts)
            lines = " / ".join(t["text"] for t in shot["text"])
            tracks.line(start, f"End card: {lines}", shot["facts"])
    provenance = ["simulated" if cfg.simulated else "photo"]
    return tracks.record(
        frames,
        title=title,
        subtitle=subtitle,
        honesty=honesty_line(provenance),
        facts=[*title_facts, *subtitle_facts],
    )


def fact_snapshot(project: Project, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    facts = project.facts.facts
    return {f: {"status": facts[f].status, "releasable": facts[f].releasable} for f in sorted(ids)}


# --------------------------------------------------------------------------- #
# Release gate
# --------------------------------------------------------------------------- #


def _approved(project: Project) -> set[str]:
    """Plan hashes (or 12-character prefixes) listed under approvals[].films in approvals.yaml."""
    path = project.root / "approvals.yaml"
    if not path.is_file():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("approvals") or [] if isinstance(data, dict) else []
    return {str(h) for e in entries if isinstance(e, dict) for h in (e.get("films") or [])}


def _unreleasable(project: Project, ids: Sequence[str]) -> list[str]:
    facts = project.facts.facts
    return [f for f in ids if f not in facts or not facts[f].releasable]


# Files of one film under film/, by suffix (the longest first: .chapters.vtt before .vtt).
_FILM_SUFFIXES = tuple(
    sorted({".mp4", "-web.mp4", ".jpg", PLAN_SUFFIX, STORYBOARD_SUFFIX, *SUFFIXES}, key=len, reverse=True)
)


def _stem(name: str) -> str | None:
    """'<slug>-<id>' of a film file name (any output `vantage film` writes), else None."""
    suffix = next((s for s in _FILM_SUFFIXES if name.endswith(s)), None)
    return name[: -len(suffix)] if suffix else None


def film_records(project: Project, film_dir: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """(build.json `films` entries by id, every release problem) for the films in film_dir.

    Each entry's `files` lists the film/ files that ship with it (MP4, web copy, plan, storyboard,
    tracks), each one checked: packaging ships those and nothing else from film/. A file that belongs
    to no current film (an old id, an orphaned web copy or sidecar) is a problem and never ships."""
    slug, records, problems = project.slug, {}, []
    if not film_dir.is_dir():
        return {}, []
    if not project.config.output.film.enabled:
        log.warn(f"output.film.enabled is false: the files in {film_dir} are not packaged")
        return {}, []
    films = resolve_films(project)
    present = sorted(p.name for p in film_dir.iterdir() if p.is_file())
    names = sorted({stem for n in present if (stem := _stem(n)) is not None})
    ids = {name: film_id(slug, name) for name in names}

    def again(fid: str | None) -> str:
        film = films.get(fid or "")
        return (
            f"`vantage film {slug}`" if film is None or film.auto else f"`vantage film {slug} --film {fid}`"
        )

    current: dict[str, FilmPlan] = {}
    try:
        current = plan_films(project, only=sorted({i for i in ids.values() if i in films}))
    except (OSError, ValueError) as exc:
        problems.append(f"cannot plan the films to check them: {exc}")
    approved = _approved(project) if project.config.output.film.require_approval else set()
    for name in names:
        fid, mp4, plan_path = ids[name], film_dir / f"{name}.mp4", film_dir / f"{name}{PLAN_SUFFIX}"
        web_mp4 = film_dir / f"{name}-web.mp4"
        own_files = [n for n in present if _stem(n) == name]
        if fid not in films:
            problems.append(
                f"film/{own_files[0]}: film {fid or name!r} is not in the current film set "
                f"({', '.join(films)}): remove film/{name}.*, or run `vantage film {slug}`"
            )
            continue
        if not plan_path.is_file():
            shown = f"film/{name}.mp4" if mp4.is_file() else f"film/{own_files[0]}"
            problems.append(
                f"{shown} has no {name}{PLAN_SUFFIX} (rendered by an older engine, or left over?): "
                f"remove film/{name}.*, or run {again(fid)}"
            )
            continue
        own: list[str] = []
        try:
            plan = FilmPlan.load(plan_path)
        except (OSError, ValueError) as exc:
            problems.append(f"film/{name}{PLAN_SUFFIX}: cannot read it ({exc}): run {again(fid)}")
            continue
        if not mp4.is_file() and not films[fid].auto:  # an on-demand film that was only planned
            log.info(f"film/{name}{PLAN_SUFFIX}: planned only (no MP4), so film {fid!r} is not packaged")
            continue
        fresh = current.get(fid)
        if fresh is not None and fresh.hash != plan.hash:
            keys = sorted(
                k for k in fresh.plan.keys() | plan.plan.keys() if fresh.plan.get(k) != plan.plan.get(k)
            )
            size = (plan.plan.get("width"), plan.plan.get("height"))
            hint = (
                " (rendered with a different VANTAGE_FAST?)"
                if size != (fresh.plan.get("width"), fresh.plan.get("height"))
                else ""
            )
            own.append(
                f"film/{name}.mp4 is stale: plan {plan.hash[:12]} ≠ current {fresh.hash[:12]} "
                f"(differs in {', '.join(keys)}){hint}: run {again(fid)}"
            )
        elif fresh is not None and fresh.encode != plan.encode:
            keys = sorted(
                k
                for k in fresh.encode.keys() | plan.encode.keys()
                if fresh.encode.get(k) != plan.encode.get(k)
            )
            own.append(
                f"film/{name}.mp4 was encoded with other settings (encode differs in {', '.join(keys)}): "
                f"run {again(fid)}"
            )
        if plan.plan.get("draft"):
            own.append(f"film/{name}.mp4 is a draft (plan.draft is true): set draft: false and re-render")
        web_ok = False
        if not mp4.is_file():
            own.append(f"film/{name}{PLAN_SUFFIX} has no rendered film: run {again(fid)}")
        elif plan.render is None:
            own.append(f"film/{name}.mp4 has no render record in its plan: run {again(fid)}")
        else:
            for key, path in (("mp4", mp4), ("web", web_mp4)):
                record = plan.render.get(key)
                if record is None:
                    continue
                if not path.is_file() or _sha256(path) != record.get("sha256"):
                    own.append(
                        f"film/{path.name} does not match its render record (replaced by hand?): "
                        f"run {again(fid)}"
                    )
                elif key == "web":
                    web_ok = True
            if not (plan.render.get("flash") or {}).get("pass", False):
                own.append(f"film/{name}.mp4 failed the flash check")
        if web_mp4.is_file() and not (
            web_ok and plan.encode.get("web") and (fresh or plan).encode.get("web")
        ):
            own.append(
                f"film/{web_mp4.name} is not the web copy of the current film (left from an earlier "
                f"render?): run {again(fid)}, or remove it"
            )
        checks = plan.checks or {}
        if "outside" not in checks:
            own.append(f"film/{name}{PLAN_SUFFIX} has no ink checks: run {again(fid)}")
        elif checks["outside"]:
            own.append(
                f"film/{name}.mp4 has text outside its safe zone ({len(checks['outside'])} layer(s); "
                f"see the plan warnings)"
            )
        tracks = track_texts(plan)
        sidecars = []
        for suffix in SUFFIXES:
            path = film_dir / f"{name}{suffix}"
            if suffix not in tracks:
                if path.is_file():
                    own.append(f"film/{path.name} is not a track of the current film: remove it")
            elif not path.is_file():
                own.append(f"film/{path.name} is missing: run {again(fid)}")
            elif path.read_text(encoding="utf-8") != tracks[suffix]:
                own.append(f"film/{path.name} does not match its plan (edited by hand?): run {again(fid)}")
            else:
                sidecars.append(path.name)
        used = sorted((fresh or plan).facts)
        bad = _unreleasable(project, used)
        own += [f"film/{name}.mp4 prints fact {f!r}, which is not releasable" for f in bad]
        needs_approval = project.config.output.film.require_approval
        if needs_approval and plan.hash not in approved and plan.hash[:12] not in approved:
            own.append(
                f"film/{name}.mp4: plan {plan.hash[:12]} is not approved "
                f"(list it under approvals[].films in approvals.yaml)"
            )
        problems += own
        shipped = [
            *([mp4.name] if mp4.is_file() else []),
            *([web_mp4.name] if web_mp4.is_file() and web_ok else []),
            plan_path.name,
            *([f"{name}{STORYBOARD_SUFFIX}"] if (film_dir / f"{name}{STORYBOARD_SUFFIX}").is_file() else []),
            *sidecars,
        ]
        records[fid] = {
            "mp4": f"film/{name}.mp4" if mp4.is_file() else None,
            "plan": f"film/{name}{PLAN_SUFFIX}",
            "hash": plan.hash,
            "cut": plan.plan.get("cut"),
            "preset": plan.plan.get("preset"),
            "loop": bool(plan.plan.get("loop")),
            "width": plan.plan.get("width"),
            "height": plan.plan.get("height"),
            "fps": plan.plan.get("fps"),
            "frames": plan.plan.get("frames"),
            "fresh": fresh is not None and fresh.hash == plan.hash,
            "draft": bool(plan.plan.get("draft")),
            "facts": used,
            "unreleasable": bad,
            "problems": own,
            "files": sorted(f"film/{n}" for n in shipped),
        }
    return records, problems


def check_films(project: Project, film_dir: Path) -> list[str]:
    """Why the films in film_dir cannot ship in a release (empty when they can)."""
    return film_records(project, film_dir)[1]


__all__ = [
    "FILM_KINDS",
    "FILM_RENDERER",
    "GOP_BEATS",
    "HONESTY",
    "LOOP_BEATS",
    "PRESETS",
    "FilmDef",
    "capture_labels",
    "check_films",
    "encode_settings",
    "film_records",
    "film_size",
    "film_vantages",
    "has_tracks",
    "honesty_line",
    "plan_films",
    "resolve_films",
    "said",
    "web_maxrate",
]
