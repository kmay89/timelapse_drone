"""The story cut: beat grid, runtime camera ports, derived shots, words and facts, zoom clamps, the
honesty line, flash safety and reproducible renders, on a tiny synthetic project.

The tiny project films one scrub chapter of five captures at 12 fps and 90 bpm (B = 8, D = 5); each
master is two-toned (a light band on top, a hue unique to its capture below), so the hue at the
frame centre tells which capture is on screen.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import math
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import yaml
from pydantic import ValidationError

from test_film import FILM, SIZE, make_project
from vantage.cli import check_project
from vantage.config import Project, load_project
from vantage.film import beat
from vantage.film.cut import CURTAIN_LABEL, RISE, SMALL_CONTRAST, _Cut, panel_from, words_set
from vantage.film.layout import Layout
from vantage.film.plan import SIMULATED_NOTE, check_films, plan_films, resolve_films
from vantage.film.render import _render_one, film_jobs, render_film
from vantage.film.safety import LumaMeter, PlanError, check_plan
from vantage.film.story import HONESTY, Geometry
from vantage.media.ffmpeg import ffmpeg_bin
from vantage.models import FilmPlan, MastersIndex
from vantage.site.build import ReleaseError
from vantage.site.package import package_project

DATES = ("2025-03-01", "2025-05-01", "2025-07-01", "2025-09-01", "2025-11-01")
HUES = ((200, 90, 40), (60, 170, 60), (40, 80, 210), (40, 190, 200), (170, 60, 170))  # BGR
STORY_FILM = {
    **{k: v for k, v in FILM.items() if k != "cut"},
    "bpm": 90,
    "vertical": False,
}  # story: the default
B, D = 8, 5
STEPS = [
    {"capture": DATES[0], "text": "**Before.** The old lot, weeds in the cracks."},
    {
        "capture": DATES[2],
        "text": "**Demolition.** In ten weeks the slides are gone.",
        "focus": [0.62, 0.55, 1.4],
    },
    {"capture": DATES[4], "text": "**Done.** The new park opens."},
]


def _masters(root: Path) -> None:
    """Two-tone masters: a light band on top, the capture's own hue below."""
    for i, date in enumerate(DATES):
        rng = np.random.default_rng(i)
        img = np.empty((SIZE[1], SIZE[0], 3), np.float32)
        img[:] = HUES[i]
        img[: SIZE[1] // 4] = (205, 210, 215)
        img += rng.normal(0, 5, img.shape).astype(np.float32)
        cv2.imwrite(str(root / "masters" / "site" / f"{date}.jpg"), np.clip(img, 0, 255).astype(np.uint8))


def tiny(
    root: Path,
    *,
    steps: list[dict[str, Any]] | None = None,
    film: dict[str, Any] | None = None,
    films: dict[str, Any] | None = None,
    hotspots: list[dict[str, Any]] | None = None,
    chapters: list[dict[str, Any]] | None = None,
    captures: list[dict[str, Any]] | None = None,
    extra: dict[str, list[str]] | None = None,
    **config: Any,
) -> Project:
    story: dict[str, Any] = {
        "vantages": [
            {"id": "overview", "name": "Overview"},
            {"id": "site", "name": "The site"},
            *({"id": vid, "name": vid.title()} for vid in extra or {}),
        ],
        "chapters": [
            {"type": "hero", "id": "opening", "kicker": "Testville · 2025", "title": "A Tiny Site Grows Up"},
            {
                "type": "scrub",
                "id": "flights",
                "kicker": "Five flights",
                "title": "One year",
                "vantage": "site",
                "steps": STEPS if steps is None else steps,
                "hotspots": hotspots or [],
            },
            *(chapters or []),
        ],
    }
    if captures is not None:
        story["captures"] = captures
    if films is not None:
        story["films"] = films
    make_project(
        root,
        film={**STORY_FILM, **(film or {})},
        story=story,
        extra={"site": list(DATES), **(extra or {})},
        **config,
    )
    _masters(root)
    return load_project(root)


def _plan(project: Project, fid: str = "16x9") -> FilmPlan:
    return plan_films(project, only=[fid], fast=False)[fid]


def _facts(root: Path, **facts: dict[str, Any]) -> Project:
    (root / "facts.yaml").write_text(yaml.safe_dump({"facts": facts}), encoding="utf-8")
    return load_project(root)


# --------------------------------------------------------------------------- #
# Beat grid and the runtime camera
# --------------------------------------------------------------------------- #


def test_beat_grid():
    assert beat.frames_per_beat(30, 100) == 18
    assert beat.dissolve_frames(30, 18) == 12
    assert (beat.frames_per_beat(12, 90), beat.dissolve_frames(12, 8)) == (B, D)
    assert beat.beat_grid(30, 99).bpm == 100.0  # 18.18 frames per beat snap to 18
    assert beat.beat_grid(25, 97).bpm == 100.0  # 15.46 → 15
    assert beat.bar_beat(0, 18) == "1.1" and beat.bar_beat(18 * 5, 18) == "2.2"
    with pytest.raises(ValueError, match="bpm too fast"):
        beat.beat_grid(30, 200)


def test_keyed_matches_runtime():
    wins = beat.hold_windows([0, 0, 1], 0.5)
    assert [(w.a, w.b) for w in wins] == [(0, 0.125), (0.25, 0.5), (1, 1.5)]
    vals = [0.0, 10.0, 30.0]
    lerp = beat.lerp
    assert beat.keyed(wins, vals, -1, lerp) == 0.0
    assert beat.keyed(wins, vals, 0.1, lerp) == 0.0  # inside the first window
    t = (0.2 - 0.125) / (0.25 - 0.125)  # easing between the first two windows
    assert beat.keyed(wins, vals, 0.2, lerp) == pytest.approx(10 * t * t * (3 - 2 * t))
    assert beat.keyed(wins, vals, 0.3, lerp) == 10.0
    assert beat.keyed(wins, vals, 0.75, lerp) == pytest.approx(20.0)  # halfway, smoothstep(0.5) = 0.5
    assert beat.keyed(wins, vals, 1.2, lerp) == 30.0
    assert beat.mix_cam((0, 0, 1), (1, 2, 2), 0.5) == (0.5, 1.0, 1.5)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def test_resolve_films_precedence(tmp_path):
    project = tiny(
        tmp_path / "p",
        film={"vertical": True, "bpm": 96},
        films={
            "9x16": {"bpm": 120},
            "screen": {"preset": "screen", "bpm": 70},
            "teaser": {"chapters": ["flights"], "enabled": True},
            "off": {"enabled": False},
        },
    )
    films = resolve_films(project)
    assert list(films) == ["16x9", "9x16", "screen", "teaser"]
    assert (films["16x9"].bpm, films["16x9"].chapters, films["16x9"].cut) == (96, "all", "story")
    assert (films["9x16"].bpm, films["9x16"].chapters, films["9x16"].aspect) == (120, "main", "9x16")
    screen = films["screen"]
    assert (screen.bpm, screen.loop, screen.auto, screen.type_scale, screen.safe) == (
        70, True, False, 1.6, (0.05, 0.05, 0.05, 0.05),
    )  # fmt: skip
    assert films["teaser"].chapters == ("flights",) and films["teaser"].aspect == "16x9"
    assert list(plan_films(project, fast=True)) == ["16x9", "9x16", "teaser"]  # auto films only
    assert plan_films(project, only=["screen"], fast=True)["screen"].plan["loop"] is True


@pytest.mark.parametrize(
    ("films", "match"),
    [
        ({"Big": {}}, "film id"),
        ({"teaser-web": {}}, "not end in -web"),
        ({"x": {"sound": "file"}}, "not supported yet"),
        ({"x": {"bpm": 200}}, "less than or equal"),
        ({"x": {"safe": [0.1, 0.1]}}, "safe must be"),
        ({"x": {"title_beats": 2}}, "greater than or equal to 3"),
        ({"x": {"end_beats": 1}}, "greater than or equal to 2"),
    ],
)
def test_film_specs_are_validated(tmp_path, films, match):
    with pytest.raises(ValidationError, match=match):
        tiny(tmp_path / "p", films=films)


# --------------------------------------------------------------------------- #
# Derived cut
# --------------------------------------------------------------------------- #


def test_derive_cut_shapes_and_grid(tmp_path):
    plan = _plan(tiny(tmp_path / "p")).plan
    shots = plan["shots"]
    assert [s["kind"] for s in shots] == ["title", *["capture"] * 5, "end"]
    assert plan["grid"] == {"bpm": 90.0, "fpb": B, "dissolve": D, "beats": plan["frames"] // B}
    caps = shots[1:-1]
    assert [(s["f1"] - s["f0"]) // B for s in caps] == [3, 1, 4, 1, 3]  # step, quiet, push, quiet, step
    assert caps[0]["dissolve"] is None and all(
        s["dissolve"]["f1"] - s["dissolve"]["f0"] == D for s in caps[1:]
    )
    assert math.ceil(0.34 * plan["fps"]) <= D
    events = [s["f0"] for s in shots] + [s["dissolve"]["f0"] for s in caps[1:]]
    for s in caps:
        step = s["step"]
        if step and step.get("land") is not None:
            events.append(step["land"])
        if step and step["push"]:
            events.append(step["push_f0"])
            assert step["push_f1"] == step["push_f0"] + B + round(B / 3)
    assert all(f % B == 0 for f in events), events
    assert caps[0]["step"]["land"] == caps[0]["f0"] + B  # beat 1, even on the film's first image
    # The title holds until its kicker reads at 17 characters/s (from B/3) and then "A Tiny Site
    # Grows Up" (once in, at 2B), and leaves over a beat.
    title = shots[0]
    kicker = round(B / 3) + math.ceil(len("Testville · 2025") * 12 / 17)
    assert (
        title["f1"] - title["f0"] >= max(2 * B, kicker) + math.ceil(len("A Tiny Site Grows Up") * 12 / 17) + B
    )
    assert caps[2]["step"]["land"] == caps[2]["f0"] + B
    assert shots[0]["capture"] == DATES[0] and shots[0]["file"] == caps[0]["file"]
    assert [s["rail"] for s in caps] == [0, 1, 2, 3, 4]
    assert plan["frames"] == shots[-1]["f1"] and plan["camera"][-1]["b"] == plan["frames"]
    # The push holds to the end of its shot and eases back from the next shot on.
    push = [c for c in plan["camera"] if c["zoom"] > 1]
    assert len(push) == 1 and (push[0]["a"], push[0]["b"]) == (
        caps[2]["f0"] + 2 * B + round(B / 3),
        caps[3]["f0"],
    )
    check_plan(plan)


def test_cards_between_vantages_and_chapter_scope(tmp_path):
    root = tmp_path / "p"
    project = tiny(root, film={"vertical": True})
    data = yaml.safe_load((root / "story.yaml").read_text())
    data["chapters"].append({"type": "scrub", "id": "again", "title": "The overview", "vantage": "overview"})
    (root / "story.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    project = load_project(root)
    wide, tall = _plan(project).plan, _plan(project, "9x16").plan
    kinds = [s["kind"] for s in wide["shots"]]
    assert kinds == ["title", *["capture"] * 5, "card", *["capture"] * 3, "end"]
    card = wide["shots"][6]
    assert card["text"] == [{"role": "title", "text": "The overview"}] and card["vantage"] == "overview"
    assert wide["shots"][7]["dissolve"]["from"] == "night"
    assert [s["kind"] for s in tall["shots"]].count("card") == 0  # chapters: main keeps the site only
    check_plan(wide)


def test_words_and_facts(tmp_path):
    root = tmp_path / "p"
    project = tiny(root)
    words = [s["step"]["words"] for s in _plan(project).plan["shots"] if s.get("step")]
    assert words == ["Before.", "Demolition.", "Done."]

    steps = [
        {**STEPS[0], "film": {"line": "Before: {fact:acres}"}},
        STEPS[1],
        {**STEPS[2], "text": "**{fact:trees} trees.** Planted."},
    ]
    project = tiny(root, steps=steps)
    project = _facts(
        root,
        acres={"text": "12 acres", "status": "verified"},
        trees={"text": "78", "status": "needs-client"},
    )
    plan = _plan(project)
    caps = [s for s in plan.plan["shots"] if s.get("step")]
    assert (caps[0]["step"]["words"], caps[0]["step"]["words_from"]) == ("Before: 12 acres", "film.line")
    assert caps[0]["facts"] == ["acres"] and caps[2]["facts"] == ["trees"]
    assert plan.facts["trees"] == {"status": "needs-client", "releasable": False}

    project = _facts(
        root,
        acres={"text": "12 acres", "status": "verified"},
        trees={"text": "78", "status": "do-not-print"},
    )
    plan = _plan(project)
    last = [s for s in plan.plan["shots"] if s.get("step")][-1]
    assert last["step"]["words"] is None and last["facts"] == [] and "trees" not in plan.facts
    assert {"role": "words", "text": "78 trees."} not in last["text"]
    assert any("do-not-print" in w for w in plan.warnings)


def test_skip_and_subjects(tmp_path):
    steps = [STEPS[0], {**STEPS[1], "focus": [0.4, 0.5, 1.5]}, {**STEPS[2], "film": {"skip": True}}]
    hotspots = [{"id": "tower", "x": 0.95, "y": 0.5, "label": "Tower", "to": DATES[1]}]
    plan = _plan(tiny(tmp_path / "p", steps=steps, hotspots=hotspots)).plan
    caps = [s for s in plan["shots"] if s["kind"] == "capture"]
    assert caps[4]["step"] is None and caps[4]["f1"] - caps[4]["f0"] == B  # skipped: a quiet beat
    step = caps[2]["step"]
    assert step["target"] == "focus+subject:tower" and step["subjects"] == ["tower"]
    x, _, z = step["focus"]
    half = 320 / (2 * 1.0 * z) / 320  # half the crop width, normalized (s0 = 1)
    assert x + half >= 0.95 + 0.06 * 2 * half - 1e-9  # the tower sits inside the crop, with its margin


def test_zoom_clamp(tmp_path):
    root = tmp_path / "p"
    steps = [STEPS[0], {**STEPS[1], "focus": [0.62, 0.5, 1.8]}, STEPS[2]]
    # A 320×200 master in a 180×320 cut: s0 = 1.6, so max_upscale 2.4 allows a zoom of 1.5.
    project = tiny(root, steps=steps, film={"vertical": True}, films={"9x16": {"max_upscale": 2.4}})
    plan = _plan(project, "9x16")
    step = next(s["step"] for s in plan.plan["shots"] if s.get("step") and s["step"]["focus_requested"])
    assert step["z_eff"] == pytest.approx(2.4 / 1.6) and step["push"] is True
    assert any("clamped" in w for w in plan.warnings) and any("soft push" in w for w in plan.warnings)
    # At the default 1.6 the zoom clamps to 1; a push that barely moves becomes a still step.
    steps[1] = {**STEPS[1], "focus": [0.51, 0.5, 1.8]}
    project = tiny(root, steps=steps, film={"vertical": True})
    shot = next(
        s for s in _plan(project, "9x16").plan["shots"] if s.get("step") and s["step"]["focus_requested"]
    )
    assert (shot["step"]["push"], shot["step"]["reason"], shot["step"]["z_eff"]) == (False, "clamped", 1.0)
    assert shot["f1"] - shot["f0"] == 3 * B
    _, warnings = check_project(project)
    assert any("9x16" in w and "clamped" in w for w in warnings), warnings


def test_validate_warns_about_films(tmp_path):
    steps = [
        STEPS[0],
        {**STEPS[1], "film": {"line": "A very long line of words that will not fit on a phone"}},
    ]
    project = tiny(tmp_path / "p", steps=steps, films={"teaser": {"chapters": ["nope"]}}, film={"bpm": 160})
    _, warnings = check_project(project)
    assert any("film words are" in w for w in warnings)
    assert any("films.teaser.chapters" in w and "nope" in w for w in warnings)
    assert any("bpm too fast" in w for w in warnings)  # 160 bpm at 12 fps: 4-frame beats < 5
    # STEPS[1] says "In ten weeks": a figure films print outside a {fact:} token.
    assert any("flights step 1: 'ten' outside a {fact:} token" in w for w in warnings), warnings


def test_bare_numbers():
    from vantage.cli import bare_numbers

    assert bare_numbers("In ten weeks, 132 stalls and {fact:area} sq ft; one frame") == ["ten", "132"]
    assert bare_numbers("Opened in 2025, closed in 2007.", {"2025"}) == ["2007"]
    assert bare_numbers("{fact:flights} flights over Twelve months") == ["Twelve"]
    assert bare_numbers(None) == []


def test_honesty_line(tmp_path):
    def end_text(project: Project) -> str:
        ends = [s for s in _plan(project).plan["shots"] if s["kind"] == "end"]
        assert [s["id"] for s in ends] == ["E1"]  # one closing card (no stats: the honesty line is on it)
        return "\n".join(t["text"] for t in ends[0]["text"])

    simulated = end_text(tiny(tmp_path / "a", simulated=True))
    assert HONESTY["simulated"] in simulated and SIMULATED_NOTE not in simulated  # said once
    assert "real photograph" not in simulated.lower() and "real aerial" not in simulated.lower()
    photo = end_text(tiny(tmp_path / "b"))
    assert HONESTY["photo"] in photo and SIMULATED_NOTE not in photo


def _kinds(root: Path, **kinds: str) -> Project:
    path = root / "story.yaml"
    story = yaml.safe_load(path.read_text(encoding="utf-8"))
    for v in story["vantages"]:
        if v["id"] in kinds:
            v["kind"] = kinds[v["id"]]
    path.write_text(yaml.safe_dump(story, sort_keys=False), encoding="utf-8")
    return load_project(root)


def test_films_show_only_drone_vantages(tmp_path):
    """An orthomosaic (or a plan, ground or archival vantage) is never called a real aerial
    photograph: films leave it out with a warning, and an unknown provenance has no honesty line."""
    from vantage.film.plan import honesty_line

    root = tmp_path / "p"
    compare = {**COMPARE, "vantage": "overview"}
    tiny(root, chapters=[compare])
    project = _kinds(root, overview="ortho")
    plan = _plan(project)
    assert plan.plan["provenance"] == ["photo"]
    assert {s.get("vantage") for s in plan.plan["shots"]} - {None} == {"site"}
    assert "vantage overview (kind: ortho): left out of the films" in " ".join(plan.warnings)
    _, warnings = check_project(project)
    assert any("vantage overview (kind: ortho)" in w for w in warnings)
    project = _kinds(root, site="archival")
    with pytest.raises(FileNotFoundError, match="no masters to film"):
        _plan(project)
    with pytest.raises(ValueError, match="no honesty line"):
        honesty_line(["ortho"])


def test_vertical_text_stays_in_the_safe_zone(tmp_path):
    project = tiny(tmp_path / "p", film={"vertical": True}, simulated=True)
    cut = _Cut(project, _plan(project, "9x16"))
    h = cut.height
    layers = [cut.tag, *cut.rail_layers.values()]
    for entry in cut.layers.values():
        for key, value in entry.items():
            if "scrim" not in key:
                layers += value if isinstance(value, list) else [value]
    for layer in layers:
        y0, y1 = layer.y, layer.y + layer.alpha.shape[0]
        assert 0.14 * h - 2 <= y0 and y1 <= 0.72 * h + 2, (y0, y1, h)


# --------------------------------------------------------------------------- #
# Safety
# --------------------------------------------------------------------------- #


def _feed(values: list[float], fps: int = 30) -> LumaMeter:
    meter = LumaMeter(fps)
    for v in values:
        meter.push(np.full((4, 4), v))
    return meter


def test_flash_meter():
    flicker = [0.2, 0.7, 0.2, 0.7, 0.2] + [0.2] * 30  # 4 opposing 0.5 steps within a second
    assert not _feed(flicker).passed and _feed(flicker).worst == 4
    assert _feed([0.2, 0.7, 0.2, 0.7] + [0.7] * 30).passed  # 3 is the limit
    dissolve = [0.9] * 5 + [0.9 + (0.05 - 0.9) * beat.ramp(k, 0, 12) for k in range(12)] + [0.05] * 20
    meter = _feed(dissolve)
    assert meter.passed and meter.max_delta > 0.1  # a 12-frame smoothstep dissolve is not a flash
    assert _feed([0.85, 0.99] * 20).passed  # both frames ≥ 0.8: not counted
    # Real frames: a uniform grey that flips between dark and light four times fails.
    meter = LumaMeter(30)
    for v in (40, 200, 40, 200, 40):
        meter.add(np.full((64, 64, 3), v, np.uint8))
    assert not meter.passed and meter.result()["pass"] is False


def _ramped(lo: float, hi: float, ramp_frames: int, n: int = 60) -> list[float]:
    """A flicker between lo and hi, ramped linearly over `ramp_frames` frames each way."""
    up = [lo + (hi - lo) * k / ramp_frames for k in range(ramp_frames)]
    down = [hi - (hi - lo) * k / ramp_frames for k in range(ramp_frames)]
    return ((up + down) * n)[:n]


def test_flash_meter_sees_ramped_flicker():
    """WCAG counts a change however many frames it takes: a 5 Hz flicker ramped over 3 frames each
    way, and a 2.5 Hz one over 6, fail although no single frame pair changes by 0.10."""
    five_hz = _ramped(0.0, 0.27, 3)
    assert max(abs(a - b) for a, b in itertools.pairwise(five_hz)) < 0.10
    assert not _feed(five_hz).passed and _feed(five_hz).worst >= 9
    assert not _feed(_ramped(0.0, 0.5, 6)).passed
    assert _feed(_ramped(0.0, 0.5, 15)).passed  # 1 Hz: two transitions a second
    assert _feed(_ramped(0.0, 0.08, 3)).passed  # under 0.10 each way: no transition
    # Noise does not split a slow dissolve into many transitions.
    rng = np.random.default_rng(0)
    slow = [0.6 - 0.5 * k / 30 + rng.normal(0, 0.01) for k in range(30)] + [0.1] * 30
    assert _feed(slow).worst == 1


def test_flash_meter_regions_and_red():
    """A flash in a quarter of one cell, straddling cell edges, is seen by the offset regions; a
    saturated red flash fails even where the luminance change alone would not."""
    meter = LumaMeter(30)
    frame = np.full((256, 256, 3), 30, np.uint8)
    for k in range(10):
        f = frame.copy()
        if k % 2:
            f[48:80, 48:80] = 255  # 32 × 32 px around the corner of four 64-px cells
        meter.add(f)
    assert not meter.passed
    meter = LumaMeter(30)
    for k in range(10):  # dark red ↔ black: ΔL ≈ 0.05, below the general threshold
        meter.add(np.full((64, 64, 3), (0, 0, 120 if k % 2 else 0), np.uint8))
    assert meter.max_delta < 0.10 and not meter.passed


def test_end_card_is_short_and_the_honesty_line_rides_the_stats(tmp_path):
    """One closing card on night. Without stats it carries the honesty line and is as long as its
    text takes to read at 17 characters a second after its dip, plus the beat its text fades out
    over (over END_MAX_S, so without the partners' beat to scan). With stats, a card that would run over END_MAX_S puts the
    honesty line under the stat run instead, where it reads in full; the card then fits in 6 s.
    Transcript lines never start past the film's last frame."""
    from vantage.film.story import END_MAX_S
    from vantage.film.tracks import cue_frames

    third = round(B / 3)
    root = tmp_path / "p"
    plan = _plan(tiny(root, films={"16x9": {"end_beats": 2}})).plan
    (end,) = (s for s in plan["shots"] if s["kind"] == "end")
    assert {"role": "honesty", "text": HONESTY["photo"]} in end["text"] and not end["ride"]
    read = f"Made for testing. {HONESTY['photo']}"
    # Over END_MAX_S with nothing to ride along with: the partners are left out (no beat to scan).
    assert all(t["role"] not in ("role", "partner") for t in end["text"]), end["text"]
    assert end["f1"] - end["f0"] - end["dip"] - third - B >= cue_frames(read, 12)
    assert end["f1"] == plan["frames"]

    tiny(root, chapters=[{**STATS, "items": [STATS["items"][2]]}], films={"16x9": {"end_beats": 2}})
    p = _plan(_facts(root, area={"text": "13,100", "status": "verified", "sources": ["plan"]})).plan
    (end,) = (s for s in p["shots"] if s["kind"] == "end")
    (stat,) = (s for s in p["shots"] if s["kind"] == "stat")
    assert end["ride"] and all(t["role"] != "honesty" for t in end["text"])
    assert [t["role"] for t in end["text"]] == ["brand", "credit", "role", "partner"]
    assert end["f1"] - end["f0"] <= END_MAX_S * 12  # its dip included
    assert end["f1"] - end["f0"] - end["dip"] - third - 2 * B >= cue_frames("Made for testing.", 12)
    assert (
        stat["honesty"] == HONESTY["photo"] and {"role": "honesty", "text": HONESTY["photo"]} in stat["text"]
    )
    assert stat["f1"] - stat["land"] >= cue_frames(HONESTY["photo"], 12)  # in with the panel, at land
    assert all(line["f0"] < p["frames"] for line in p["transcript"]["lines"])
    assert p["captions"][-1]["text"] == "Made for testing."  # the end card's cue
    short = json.loads(json.dumps(p))
    e1 = next(s for s in short["shots"] if s["id"] == "E1")
    e1["dip"] = e1["f1"] - e1["f0"] - B + 1
    with pytest.raises(PlanError, match=r"E1: a .*-frame dip leaves under a beat"):
        check_plan(short)


def test_check_plan_rejects_short_dissolve(tmp_path):
    project = tiny(tmp_path / "p", film={"fps": 30, "bpm": 100})
    plan = _plan(project).plan
    check_plan(plan)
    edited = json.loads(json.dumps(plan))
    cap = edited["shots"][2]
    cap["dissolve"]["f1"] = cap["dissolve"]["f0"] + 4
    with pytest.raises(PlanError, match="4-frame dissolve"):
        check_plan(edited)
    edited["shots"][2]["dissolve"] = None
    with pytest.raises(PlanError, match="hard cut"):
        check_plan(edited)


# --------------------------------------------------------------------------- #
# Renders
# --------------------------------------------------------------------------- #


def _frames(mp4: Path, w: int, h: int) -> np.ndarray:
    raw = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(mp4), "-vf",
         "scale=in_color_matrix=bt709:in_range=tv,format=bgr24", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, np.uint8).reshape(-1, h, w, 3)


def test_story_render_is_reproducible(tmp_path):
    project = tiny(tmp_path / "p", simulated=True)
    out = {}
    for run in ("a", "b"):
        out[run] = tmp_path / run
        render_film(project, out[run])
    names = sorted(p.name for p in out["a"].iterdir())
    assert names == [
        "tiny-site-16x9-web.mp4", "tiny-site-16x9.chapters.vtt", "tiny-site-16x9.jpg", "tiny-site-16x9.mp4",
        "tiny-site-16x9.plan.json", "tiny-site-16x9.srt", "tiny-site-16x9.storyboard.jpg", "tiny-site-16x9.txt",
        "tiny-site-16x9.vtt",
    ]  # fmt: skip
    for name in names:
        assert (out["a"] / name).read_bytes() == (out["b"] / name).read_bytes(), name
    plan = FilmPlan.load(out["a"] / "tiny-site-16x9.plan.json")
    assert plan.render is not None and plan.render["flash"]["pass"] is True
    p = plan.plan
    frames = _frames(out["a"] / "tiny-site-16x9.mp4", p["width"], p["height"])
    assert len(frames) == p["frames"]
    assert frames[0].mean() > 40  # frame 0 is the first image, not black
    caps = [s for s in p["shots"] if s["kind"] == "capture"]
    for i, shot in enumerate(caps):
        f = shot["f0"] + D + 1 if i else shot["f0"] + 1  # dissolved in, before any push
        centre = frames[f][80:110, 130:190].reshape(-1, 3).astype(np.float32).mean(axis=0)
        assert np.abs(centre - HUES[i]).max() < 30, (i, f, centre)
    assert frames[-1][40:140].mean() < frames[caps[-1]["key"]][40:140].mean()  # the end card is on night


def test_classic_still_identical(tmp_path):
    project = make_project(
        tmp_path / "p", film={**FILM, "vertical": False, "title_card_s": 0.5, "end_card_s": 0.5}
    )
    plan = plan_films(project, fast=True)["16x9"]
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    _render_one(project, plan, a, plan_only=False)
    _render_one(project, FilmPlan.load(a / "tiny-site-16x9.plan.json"), b, plan_only=False)
    for name in ("tiny-site-16x9.mp4", "tiny-site-16x9.jpg"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    record = FilmPlan.load(a / "tiny-site-16x9.plan.json").render
    assert record is not None and record["flash"]["pass"] is True and record["flash"]["max_delta"] > 0


# --------------------------------------------------------------------------- #
# Curtain, stats, degrade, subjects, defaults, parallel renders, release
# --------------------------------------------------------------------------- #

COMPARE = {
    "type": "compare",
    "id": "versus",
    "kicker": "Then and now",
    "title": "Side by side",
    "vantage": "site",
    "before": DATES[0],
    "after": DATES[4],
    "before_label": "Then",
    "after_label": "Now",
    "steps": [{"text": "Half and half.", "split": 0.5}],
}


def _split(shot: dict[str, Any], f: int) -> float:
    keys = shot["keys"]
    return beat.keyed([beat.Window(k["a"], k["b"]) for k in keys], [k["split"] for k in keys], f, beat.lerp)


def test_curtain_split_halves(tmp_path):
    quiet = {
        **COMPARE,
        "steps": [{"text": "Half and half, and a sentence too long for the screen.", "split": 0.5}],
    }
    project = tiny(tmp_path / "p", chapters=[quiet], simulated=True)
    plan = _plan(project)
    shots = plan.plan["shots"]
    assert [s["kind"] for s in shots] == ["title", *["capture"] * 5, "curtain", "end"]
    curtain = shots[6]
    assert (curtain["before"], curtain["after"], curtain["labels"]) == (DATES[0], DATES[4], ["Then", "Now"])
    assert curtain["f1"] - curtain["f0"] == (1 + 4) * B  # entry beat, then 2 + 2 beats per split
    assert [k["split"] for k in curtain["keys"]] == [0.0, 0.5]
    cut = _Cut(project, plan)
    f = curtain["keys"][1]["a"] + 2  # holding at split 0.5
    frame = cut.frame_at(f).astype(np.float32)
    h, w = frame.shape[:2]
    rows = slice(int(0.62 * h), int(0.75 * h))  # under the labels, clear of the grip and the band
    left = frame[rows, 10 : w // 2 - 12].reshape(-1, 3).mean(axis=0)
    right = frame[rows, w // 2 + 12 : w - 10].reshape(-1, 3).mean(axis=0)
    assert np.abs(left - HUES[0]).max() < 30, left
    assert np.abs(right - HUES[4]).max() < 30, right
    # Labels show only while the rule is at least 8 px clear of their ink.
    half = 1.5 * cut.u
    ink_b, ink_a = curtain["label_ink"]
    shown = [{f for a, b in side for f in range(a, b)} for side in curtain["reveal"]]
    assert shown[0] and shown[1]  # both labels are revealed at split 0.5
    for k in range(curtain["f0"], curtain["f1"]):
        x = _split(curtain, k) * w
        assert (k in shown[0]) == (x - half >= ink_b + 8), k
        assert (k in shown[1]) == (x + half <= ink_a - 8), k
        for side in (0, 1):
            if k not in shown[side]:
                assert cut._reveal(curtain, side, k) == 0.0
    assert min(shown[0]) > curtain["keys"][0]["b"]  # before is hidden at split 0, then revealed
    # Under the top-right tag, the labels sit at least a label's height lower: two blocks, not one.
    tag = next(i["box"] for i in cut.ink if i["layer"] == "tag")
    for side in ("before", "after"):
        label = next(i["box"] for i in cut.ink if i["shot"] == curtain["id"] and i["layer"] == side)
        assert label[1] - (tag[1] + tag[3]) >= label[3], (side, label, tag)


def test_curtain_words_fall_back_like_scrub_steps(tmp_path):
    """A compare step puts words on screen without film.line: its **bold** lead, else its first
    sentence when that prints at most 40 characters; a longer one stays off screen."""
    steps = [
        {"text": "**Then.** The lot, all of it.", "split": 0.8},
        {"text": "The pool is the beach now. It kept its outline.", "split": 0.5},
        {"text": "A first sentence far too long to put on the screen of a phone.", "split": 0.2},
        {"text": "Half and half.", "split": 0.4, "film": {"line": "Our words."}},
    ]
    p = _plan(tiny(tmp_path / "p", chapters=[{**COMPARE, "steps": steps}])).plan
    curtain = next(s for s in p["shots"] if s["kind"] == "curtain")
    assert [(s["words"], s["words_from"]) for s in curtain["steps"]] == [
        ("Then.", "lead"),
        ("The pool is the beach now.", "sentence"),
        (None, None),
        ("Our words.", "film.line"),
    ]


def test_curtain_entry_without_jump(tmp_path):
    project = tiny(tmp_path / "a", chapters=[COMPARE])
    plan = _plan(project)
    curtain = next(s for s in plan.plan["shots"] if s["kind"] == "curtain")
    assert curtain["dissolve"] is None and curtain["from_after"] is True  # the series ended on `after`
    cut = _Cut(project, plan)
    f0 = curtain["f0"]
    a, b = (cut.frame_at(f).astype(np.float32) for f in (f0 - 1, f0))
    assert np.abs(a - b).mean() < 4  # no jump
    project = tiny(tmp_path / "b", chapters=[{**COMPARE, "after": DATES[3]}])
    curtain = next(s for s in _plan(project).plan["shots"] if s["kind"] == "curtain")
    d = curtain["dissolve"]
    assert d is not None and (d["f0"], d["f1"] - d["f0"]) == (curtain["f0"], D)
    check_plan(_plan(project).plan)


STATS = {
    "type": "stats",
    "id": "numbers",
    "items": [
        {"value": "42", "label": "a number nobody sourced"},
        {"value": "{fact:secret}", "label": "hidden"},
        {"value": "{fact:area}", "unit": "sq ft", "label": "of old pool", "source": "Site plan"},
    ],
}


def test_do_not_print_vantage_name_falls_back_to_the_masters_name(tmp_path):
    """A vantage name that uses a do-not-print fact never prints its raw {fact:} token: the card,
    captions, chapters and transcript use the masters' name instead."""
    root = tmp_path / "p"
    compare = {**COMPARE, "vantage": "overview", "kicker": None, "title": None, "id": "ov"}
    compare = {k: v for k, v in compare.items() if v is not None}
    tiny(root, chapters=[compare])
    path = root / "story.yaml"
    story = yaml.safe_load(path.read_text(encoding="utf-8"))
    story["vantages"][0]["name"] = "Overview of {fact:secret} acres"
    path.write_text(yaml.safe_dump(story, sort_keys=False), encoding="utf-8")
    project = _facts(root, secret={"text": "999", "status": "do-not-print"})
    plan = _plan(project)
    assert any("vantage overview name: left out" in w for w in plan.warnings)
    text = json.dumps(plan.plan)
    assert "{fact:" not in text and "999" not in text
    card = next(s for s in plan.plan["shots"] if s["kind"] == "card")
    assert {"role": "title", "text": "Overview"} in card["text"]  # the masters' name
    assert any("Split screen (Overview)," in line["text"] for line in plan.plan["transcript"]["lines"])


def test_stats_are_facts_only(tmp_path):
    root = tmp_path / "p"
    tiny(root, chapters=[STATS], films={"16x9": {"stats_surface": "paper"}})  # the plain card
    project = _facts(
        root,
        area={"text": "13,100", "status": "verified", "sources": ["plan"]},
        secret={"text": "7", "status": "do-not-print"},
    )
    plan = _plan(project)
    stats = [s for s in plan.plan["shots"] if s["kind"] == "stat"]
    assert len(stats) == 1
    stat = stats[0]
    assert (stat["value"], stat["unit"], stat["label"], stat["source"]) == (
        "13,100", "sq ft", "of old pool", "Site plan",
    )  # fmt: skip
    assert stat["facts"] == ["area"] and stat["surface"] == "paper" and stat["backdrop"] == "plain"
    # The figure lands on the first beat after the dip from the image, then its label and source,
    # which then stay readable at 17 characters a second, so the shot is longer than 4 beats here.
    third = round(B / 3)
    assert stat["land"] == stat["f0"] + -(-stat["dip"] // B) * B
    assert stat["text_in"] == stat["land"] + round(B / 2)  # once the figure is whole
    visible = stat["f1"] - (stat["text_in"] + third)
    assert visible >= math.ceil(len("of old pool Site plan") * 12 / 17) and stat["f1"] - stat["f0"] >= 4 * B
    assert any("'42' is not a {fact:} token" in w for w in plan.warnings), plan.warnings
    assert any("stat 1" in w and "do-not-print" in w for w in plan.warnings), plan.warnings
    assert "secret" not in plan.facts
    # The renderer sets one layer per glyph of the plan's value, and its unit, which slides in with
    # it: it never draws another number.
    cut = _Cut(project, plan)
    assert len(cut.layers[stat["id"]]["glyphs"]) == len("13,100") + len(["sq ft"])
    first = cut.frame_at(stat["land"] + 1).astype(np.float32)
    landed = cut.frame_at(stat["key"]).astype(np.float32)
    assert np.abs(first - landed).mean() > 1  # the figure slides in, then holds
    # The value's first ink starts on the text column, like the label under it.
    frame = cut.frame_at(stat["key"])
    dark = frame.mean(axis=2) < 110
    rows = np.flatnonzero(dark.any(axis=1))
    blocks = np.split(rows, np.flatnonzero(np.diff(rows) > 2) + 1)
    lefts = [int(np.flatnonzero(dark[blk[0] : blk[-1] + 1].any(axis=0)).min()) for blk in blocks]
    assert len(lefts) >= 2 and abs(lefts[0] - lefts[1]) <= 2, lefts  # value, label
    # Two plain stats in a row: the second starts on the beat with no dip; only the text crossfades.
    two = {**STATS, "items": [STATS["items"][2], {**STATS["items"][2], "label": "of new lawn"}]}
    tiny(root, chapters=[two], films={"16x9": {"stats_surface": "paper"}})
    project2 = _facts(root, area={"text": "13,100", "status": "verified", "sources": ["plan"]})
    n1, n2 = (s for s in _plan(project2).plan["shots"] if s["kind"] == "stat")
    assert (n2["f0"], n2["dip"], n2["land"], n2["text_in"]) == (
        n1["f1"],
        0,
        n2["f0"],
        n2["f0"] + round(B / 2),
    )
    # By default stats sit over a picture, under a panel in the chapter's surface (else paper).
    tiny(root, chapters=[{**STATS, "items": [STATS["items"][2]], "surface": "night"}])
    night = _plan(_facts(root, area={"text": "13,100", "status": "verified", "sources": ["plan"]}))
    (shot,) = (s for s in night.plan["shots"] if s["kind"] == "stat")
    assert (shot["surface"], shot["backdrop"], shot["file"]) == ("night", "picture", f"site/{DATES[-1]}.jpg")
    # No more than `stats` items.
    many = {**STATS, "items": [STATS["items"][2]] * 5}
    project = tiny(root, chapters=[many], films={"16x9": {"stats": 2}})
    project = _facts(root, area={"text": "13,100", "status": "verified"})
    assert sum(s["kind"] == "stat" for s in _plan(project).plan["shots"]) == 2


AREA = {"text": "13,100", "status": "verified", "sources": ["plan"]}
TREES = {"text": "78", "status": "verified", "sources": ["plan"]}
RUN = {
    "type": "stats",
    "id": "numbers",
    "title": "What changed",
    "items": [
        {"value": "{fact:trees}", "label": "young trees planted along the paths", "source": "Planting plan"},
        {"value": "{fact:area}", "unit": "sq ft", "label": "of old pool, now a beach", "source": "Site plan"},
    ],
}
POOL = {"id": "pool", "x": 0.35, "y": 0.62, "label": "The pool", "body": "{fact:area} square feet of sand."}


def _big(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str | None = None) -> Project:
    """The tiny project at 640×360 (and 360×640) over 1280×800 masters, with a curtain, a run of two
    stats (the second's fact printed by a hotspot) and its facts releasable."""
    import test_film

    big = (1280, 800)
    monkeypatch.setattr(test_film, "SIZE", big)
    monkeypatch.setitem(globals(), "SIZE", big)
    run = {**RUN, **({"surface": surface} if surface else {})}
    root = tmp_path / f"big-{surface}"
    tiny(
        root, chapters=[COMPARE, run], hotspots=[POOL], simulated=True,
        film={"width": 640, "height": 360, "vertical": True},
    )  # fmt: skip
    return _facts(root, area=AREA, trees=TREES)


class _NoText:
    """Renders a stat run's frames without its text (the picture and panel only)."""

    def __init__(self, cut: _Cut) -> None:
        self.cut = cut

    def __enter__(self) -> None:
        cut = self.cut
        self.saved = {sid: dict(cut.layers[sid]) for sid in cut.stat_runs}
        self.rides = {id(r): (r, r["ride"]) for r in cut.stat_runs.values()}
        for sid in cut.stat_runs:
            cut.layers[sid].update(glyphs=[], rest=[])
        for r, _ in self.rides.values():
            r["ride"] = None

    def __exit__(self, *exc: object) -> None:
        for sid, layers in self.saved.items():
            self.cut.layers[sid].update(layers)
        for r, ride in self.rides.values():
            r["ride"] = ride


def _cores(layers: list[Any], w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """(ys, xs) of the fully covered pixels of text layers (their glyphs' insides)."""
    ys, xs = [], []
    for layer in layers:
        y, x = np.nonzero(layer.alpha[..., 0] >= 0.98)
        y, x = y + layer.y, x + layer.x
        keep = (y >= 0) & (y < h) & (x >= 0) & (x < w)
        ys.append(y[keep])
        xs.append(x[keep])
    return np.concatenate(ys), np.concatenate(xs)


def _lum(px: np.ndarray) -> np.ndarray:
    from vantage.film.safety import _BGR_WEIGHTS, _SRGB_TO_LINEAR

    return _SRGB_TO_LINEAR[px.astype(np.uint8)] @ _BGR_WEIGHTS.astype(np.float64)


@pytest.mark.parametrize("surface", [None, "night"])
@pytest.mark.parametrize("fid", ["16x9", "9x16"])
def test_stat_run_over_a_picture(tmp_path, monkeypatch, fid, surface):
    """A run of stats sits over the main vantage's latest picture (the one the curtain ends on), placed
    where its panel hides the least of what changed: the first dissolves in from the curtain, the next starts on the beat with no dip and only the text
    crossfades. The stat whose fact the pool hotspot prints pushes toward it and frames it clear of
    the panel (16:9); the other drifts or holds. Every frame from a stat's text_in to its end shows
    its words, every frame of the run from the first landing shows a figure (no blank frame), and
    each text keeps 4.5:1 over what is under it, measured on rendered frames. The text changes in
    sequence: no frame shows a label or unit beside a figure that is not whole, or two stats' text at
    once, and the panel is never up without text."""
    project = _big(tmp_path, monkeypatch, surface)
    plan = _plan(project, fid)
    p = plan.plan
    check_plan(p)
    shots = p["shots"]
    assert [s["kind"] for s in shots][-4:] == ["curtain", "stat", "stat", "end"]
    curtain, n1, n2, end = shots[-4:]
    assert end["ride"] and n2["honesty"] == HONESTY["simulated"]  # the end card alone would run over 6 s
    assert n1["file"] == n2["file"] == curtain["after_file"] and n1["surface"] == (surface or "paper")
    d = n1["dissolve"]
    held = p["flash"]["held"].get("N1.0", 0)  # the flash margin may hold the picture before the figure
    assert (d["from"], d["f0"], d["f1"]) == ("C1", n1["f0"], n1["f0"] + D)
    assert n1["land"] == n1["f0"] + B * (1 + held)
    assert n2["dissolve"] is None and n2["f0"] == n1["f1"] and n2["land"] == n2["f0"]
    assert n1["text_in"] == n1["land"] + round(B / 2) and n2["text_in"] == n2["land"] + round(B / 2)
    assert n1["move"]["subjects"] == [] and n1["move"]["how"] in ("drift", "hold")
    # Both places frame the pool; the run takes the one whose panel hides less of what changed (the
    # pool, low on the picture): high, in both aspects.
    covered = n1["place_subject"]
    assert n1["place"] == n2["place"] == "high" and covered["high"] < covered["low"], covered
    if fid == "16x9":
        move = n2["move"]
        assert move["subjects"] == ["pool"] and move["how"] == "focus+subject:pool", move
        x, y, z = move["focus"]
        mv = MastersIndex.load(project.masters_dir / "index.json").vantages["site"]
        geo = Geometry(p["width"], p["height"], mv.width, mv.height)
        cx, cy = geo.centre(x, y, z)
        hw, hh = geo.half(z)
        v = (POOL["y"] - (cy - hh)) / (2 * hh) * p["height"]
        assert z > 1.08 and 0 <= (POOL["x"] - (cx - hw)) / (2 * hw) <= 1
        cut = _Cut(project, plan)
        top, bottom = cut.panels[0]["box"][1], sum(cut.panels[0]["box"][1::2])
        assert v < top or v > bottom, (v, top, bottom)  # the hotspot is not under the panel's ink
    said = any("The camera moves in on The pool." in line["text"] for line in p["transcript"]["lines"])
    assert said == (n2["move"]["subjects"] == ["pool"])
    cut = _Cut(project, plan)
    (scrim,) = cut.panels
    assert scrim["shots"] == ["N1", "N2"] and scrim["tint"] == (surface or "paper")
    if fid == "9x16":  # the portrait panel spans the text column: as much gutter left as right
        bx, _, bw, _ = scrim["box"]
        assert abs(bx - (cut.width - bx - bw)) <= 1, (scrim["box"], cut.width)
    # Its labels and sources are small text: 4.5:1 (WCAG 1.4.3), as measured along the camera path,
    # at the least opacity that keeps it (not AAA's 7:1, which washes out the picture).
    assert scrim["target"] == SMALL_CONTRAST and scrim["contrast"] >= SMALL_CONTRAST, scrim
    if scrim["alpha"] > 0.3:
        assert scrim["contrast"] < 7.0, scrim
    w, h = cut.width, cut.height
    half = round(B / 2)
    for stat in (n1, n2):
        L = cut.layers[stat["id"]]
        words = _cores(L["rest"], w, h)
        figure = _cores(L["glyphs"], w, h)
        fully = stat["land"] + half  # the figure slides in as one block
        end = stat["f1"] - (round(B / 3) if stat is n1 else 0)  # N1's text leaves before N2 lands
        for f in range(stat["text_in"], end):
            frame = cut.frame_at(f).astype(np.int16)
            with _NoText(cut):
                bare = cut.frame_at(f).astype(np.int16)
            seen = np.abs(frame[words] - bare[words]).max(axis=1).mean()
            assert seen > 12, (stat["id"], f, seen)  # its words are on screen
            if f >= fully:
                assert np.abs(frame[figure] - bare[figure]).max(axis=1).mean() > 12, (stat["id"], f)
            if f in (max(stat["key"], fully), end - 1):  # every text's contrast over what is under it
                for layers in ([*L["glyphs"]], L["rest"], [cut.stat_runs[stat["id"]]["ride"]]):
                    ys, xs = _cores(layers, w, h)
                    ink = np.median(_lum(frame[ys, xs]))
                    under = _lum(bare[ys, xs])
                    ratio = (np.maximum(ink, under) + 0.05) / (np.minimum(ink, under) + 0.05)
                    assert np.quantile(ratio, 0.05) >= 4.5, (stat["id"], f, np.quantile(ratio, 0.05))
    # In sequence: before a figure is whole its words are not on screen; N1's text is gone before
    # N2's figure starts to land; no frame of the panel without a figure or words coming in.
    third = round(B / 3)
    n1_cores = [_cores(cut.layers["N1"][k], w, h) for k in ("glyphs", "rest")]
    for stat in (n1, n2):
        L = cut.layers[stat["id"]]
        words = _cores(L["rest"], w, h)
        for f in range(max(stat["land"] - third, stat["f0"]), stat["text_in"]):
            frame = cut.frame_at(f).astype(np.int16)
            with _NoText(cut):
                bare = cut.frame_at(f).astype(np.int16)
            assert np.abs(frame[words] - bare[words]).max(axis=1).mean() < 3, (stat["id"], f)
    # The unit lands with its figure, as one block: whole when the figure is, before the label.
    ux, uy, uw, uh = next(i["box"] for i in cut.ink if i["shot"] == "N2" and i["layer"] == "unit")
    whole = n2["land"] + half - 1  # the slide's last frame, the one before its label starts in
    frame = cut.frame_at(whole).astype(np.int16)
    with _NoText(cut):
        bare = cut.frame_at(whole).astype(np.int16)
    unit = np.abs(frame[uy : uy + uh, ux : ux + uw] - bare[uy : uy + uh, ux : ux + uw]).max(axis=2)
    assert np.quantile(unit, 0.95) > 40, np.quantile(unit, 0.95)
    frame = cut.frame_at(n2["f0"] - 1).astype(np.int16)  # N1's last frame: its text is gone
    with _NoText(cut):
        bare = cut.frame_at(n2["f0"] - 1).astype(np.int16)
    for c in n1_cores:
        assert np.abs(frame[c] - bare[c]).max(axis=1).mean() < 3, "N1 left over"
    panel = cut.stat_runs["N1"]["panel"]
    ys, xs = np.nonzero(panel.alpha[..., 0] > 0.5 * panel.alpha.max())
    ys, xs = ys + panel.y, xs + panel.x
    for f in range(n1["f0"], panel_from(n1, B)):  # before the panel rises: the bare picture
        frame = cut.frame_at(f).astype(np.int16)
        saved = cut.stat_runs["N1"]["panel"]
        cut.stat_runs["N1"]["panel"] = None
        try:
            without = cut.frame_at(f).astype(np.int16)
        finally:
            cut.stat_runs["N1"]["panel"] = saved
        assert np.abs(frame[ys, xs] - without[ys, xs]).max() <= 1, f
    # No blank frame: from N1's figure landing to the run's end, some figure is on screen (but for
    # the change, N1 out over its last B/3 and N2 in over half a beat).
    figures = [_cores(cut.layers[s["id"]]["glyphs"], w, h) for s in (n1, n2)]
    change = range(n1["f1"] - round(B / 3), n2["land"] + half)
    for f in [f for f in range(n1["land"] + half, n2["f1"]) if f not in change]:
        frame = cut.frame_at(f).astype(np.int16)
        with _NoText(cut):
            bare = cut.frame_at(f).astype(np.int16)
        assert max(np.abs(frame[c] - bare[c]).max(axis=1).mean() for c in figures) > 12, f


def test_stat_panel_calms_the_picture_under_it(tmp_path, monkeypatch):
    """Under a stat panel the picture is blurred and taken half way to grey, not only tinted, so busy
    or saturated detail (the masters' noise, their saturated hues) never punches through the text,
    and the panel is measured with a margin over what WCAG asks (`aim`)."""
    from vantage.film.cut import PANEL_MARGIN

    project = _big(tmp_path, monkeypatch)
    plan = _plan(project)
    cut = _Cut(project, plan)
    (scrim,) = cut.panels
    assert scrim["aim"] == round(scrim["target"] * PANEL_MARGIN, 2)
    assert scrim["contrast"] >= scrim["aim"] or scrim["alpha"] == 0.95, scrim
    n2 = next(s for s in plan.plan["shots"] if s["id"] == "N2")
    run = cut.stat_runs["N2"]
    x, y, w, h = scrim["box"]
    f = n2["key"]
    with _NoText(cut):
        calmed = cut.frame_at(f)[y : y + h, x : x + w]
        mask, run["calm"] = run["calm"], None
        try:
            busy = cut.frame_at(f)[y : y + h, x : x + w]
        finally:
            run["calm"] = mask

    def detail(img: np.ndarray) -> float:
        lap = cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float64), cv2.CV_64F)
        return float(np.quantile(np.abs(lap), 0.75))  # the grain, not the one edge between tones

    def chroma(img: np.ndarray) -> float:
        return float((img.max(axis=2).astype(int) - img.min(axis=2)).mean())

    assert detail(calmed) < 0.5 * detail(busy), (detail(calmed), detail(busy))
    assert chroma(calmed) < 0.7 * chroma(busy), (chroma(calmed), chroma(busy))


def test_stat_type_reads_on_a_phone(tmp_path, monkeypatch):
    """In 9:16 at 1080 wide a stat label's x-height is at least 44 px and its source's 30, and the
    whole run, honesty line included, stays inside the safe zone, above the tag. In 16:9 at 1080
    high, also read on a phone (held sideways), they are at least 36 and 24 px."""
    from vantage.film.cut import panel_contrast, stat_run
    from vantage.film.render import check_ink

    project = _big(tmp_path, monkeypatch)
    root = project.root
    data = yaml.safe_load((root / "project.yaml").read_text())
    data["output"]["film"].update(width=1920, height=1080)
    (root / "project.yaml").write_text(yaml.safe_dump(data))
    project = load_project(root)
    plan = _plan(project, "9x16")
    cut = _Cut(project, plan)
    assert cut.width == 1080
    stats = [s for s in plan.plan["shots"] if s["kind"] == "stat"]
    run = stat_run(cut.type, cut.lay, stats, stats[0]["honesty"], stats[0]["place"], True)
    for st in run.sets:
        x_label = -cut.type.font(st.label).getbbox("x", anchor="ls")[1]
        x_source = -cut.type.font(st.source).getbbox("x", anchor="ls")[1]
        assert x_label >= 44 and x_source >= 30, (x_label, x_source)
    assert check_ink(plan, cut) == []
    tag = next(i["box"] for i in cut.ink if i["layer"] == "tag")
    assert run.box[3] < tag[1], (run.box, tag)
    wide = _plan(project, "16x9")
    wcut = _Cut(project, wide)
    assert wcut.height == 1080
    wstats = [s for s in wide.plan["shots"] if s["kind"] == "stat"]
    for st in stat_run(wcut.type, wcut.lay, wstats, wstats[0]["honesty"], wstats[0]["place"], True).sets:
        x_label = -wcut.type.font(st.label).getbbox("x", anchor="ls")[1]
        x_source = -wcut.type.font(st.source).getbbox("x", anchor="ls")[1]
        assert x_label >= 36 and x_source >= 24, (x_label, x_source)
    assert check_ink(wide, wcut) == []
    # The panel keeps what WCAG 1.4.3 asks of the smallest text on it: 4.5:1 for these sources, 3:1
    # only when every text on the panel is large (18 pt seen on a phone: 66 px at 1080 wide).
    assert panel_contrast(cut.lay, run) == SMALL_CONTRAST

    def large(style: Any) -> Any:
        return dataclasses.replace(style, size=70 / cut.lay.unit)

    sets = tuple(
        dataclasses.replace(st, **{k: large(getattr(st, k)) for k in ("value", "unit", "label", "source")})
        for st in run.sets
    )
    assert panel_contrast(cut.lay, dataclasses.replace(run, sets=sets, ride=large(run.ride))) == 3.0
    assert panel_contrast(cut.lay, dataclasses.replace(run, sets=sets)) == SMALL_CONTRAST  # its ride


def test_degrade(tmp_path):
    def kinds(n: int, chapters: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        excluded = [{"date": d, "exclude": True} for d in DATES[n:]]
        project = tiny(tmp_path / f"p{n}", captures=excluded, chapters=chapters)
        plan = _plan(project).plan
        check_plan(plan)
        return plan["shots"]

    one = kinds(1)
    assert [s["kind"] for s in one] == ["title", "capture", "end"]
    assert one[1]["f1"] - one[1]["f0"] == 3 * B and one[1]["dissolve"] is None
    unsourced = {**STATS, "items": [STATS["items"][0]]}
    assert [s["kind"] for s in kinds(1, [unsourced])] == ["title", "capture", "end"]  # no fact, no stat
    two = kinds(2)
    assert [s["kind"] for s in two] == ["title", "capture", "curtain", "end"]
    assert two[1]["f1"] - two[1]["f0"] == 3 * B
    assert (two[2]["before"], two[2]["after"]) == (DATES[0], DATES[1])
    assert [k["split"] for k in two[2]["keys"]] == [0.0, 0.5]
    assert [s["kind"] for s in kinds(3)] == ["title", "capture", "capture", "capture", "end"]


def test_push_target_shows_subject(tmp_path):
    """16:9: the push frames the subject (near the master's edge) inside the safe zone. 9:16: the
    subject is far outside the portrait crop at rest and cannot be framed within MAX_TRAVEL of it,
    so the step holds its picture: a push to the focus alone would frame what the step does not
    name. Its transcript names no subject there."""
    from vantage.film.story import MAX_TRAVEL, subject_box

    steps = [STEPS[0], {**STEPS[1], "focus": [0.62, 0.54, 1.6]}]
    hotspots = [{"id": "tower", "x": 0.93, "y": 0.584, "label": "The tower", "to": DATES[1]}]
    project = tiny(tmp_path / "p", steps=steps, hotspots=hotspots, film={"vertical": True})
    for fid in ("16x9", "9x16"):
        plan = _plan(project, fid)
        p = plan.plan
        step = next(s["step"] for s in p["shots"] if s.get("step") and s["step"]["focus_requested"])
        x, y, z = step["focus"]
        geo = Geometry(p["width"], p["height"], 320, 200)
        cx, cy = geo.centre(x, y, z)
        hw, hh = geo.half(z)
        if fid == "16x9":
            assert step["target"] == "focus+subject:tower" and step["subjects"] == ["tower"], fid
            u0, u1, v0, v1 = subject_box(Layout.from_record(p["width"], p["height"], p["layout"]))
            u, v = (0.93 - (cx - hw)) / (2 * hw), (0.584 - (cy - hh)) / (2 * hh)
            assert u0 - 1e-9 <= u <= u1 + 1e-9 and v0 - 1e-9 <= v <= v1 + 1e-9, (u, v)
        else:
            assert step["target"] == "focus, subject off-frame:tower", step
            assert not step["push"] and step["reason"] == "subject off-frame", step
            assert "push_f0" not in step
            assert any("subject tower cannot be framed" in w and "holds" in w for w in plan.warnings)
            assert not [w for w in plan.warnings if "soft push" in w or "clamped" in w], plan.warnings
            rx, _ = geo.centre(0.5, 0.5, 1.0)  # the rest view (portrait_focus defaults to the centre)
            assert abs(cx - rx) <= MAX_TRAVEL * 2 * geo.half(1.0)[0] + 1e-9
            cams = {(w["x"], w["y"], w["zoom"]) for w in p["camera"]}
            assert cams == {(0.5, 0.5, 1.0)}, cams  # the camera never leaves the rest view
        lines = " ".join(line["text"] for line in p["transcript"]["lines"])
        assert ("moves in on The tower" in lines) == (fid == "16x9"), (fid, lines)


def test_push_keeps_its_subject_clear_of_the_words(tmp_path, monkeypatch):
    """A push frames its subject inside its words zone's subject box: above the low words (9:16
    lead baseline .60H), or below the high words and above the rail."""
    import test_film
    from vantage.film.story import _Placer

    big = (1280, 800)
    monkeypatch.setattr(test_film, "SIZE", big)
    monkeypatch.setitem(globals(), "SIZE", big)
    steps = [STEPS[0], {**STEPS[1], "focus": [0.5, 0.6, 1.6]}]
    hotspots = [{"id": "ring", "x": 0.5, "y": 0.7, "label": "The ring", "to": DATES[1]}]
    project = tiny(tmp_path / "p", steps=steps, hotspots=hotspots, film={"vertical": True})
    p = _plan(project, "9x16").plan
    step = next(s["step"] for s in p["shots"] if s.get("step") and s["step"]["focus_requested"])
    assert step["target"] == "focus+subject:ring" and step["push"], step
    x, y, z = step["focus"]
    geo = Geometry(p["width"], p["height"], *big)
    _, cy = geo.centre(x, y, z)
    hh = geo.half(z)[1]
    v = (0.7 - (cy - hh)) / (2 * hh)
    placer = _Placer(project, p["inputs"]["fonts"], Layout.from_record(p["width"], p["height"], p["layout"]))
    _, _, v0, v1 = placer.aim_box(step["words"], step["words_zone"], placer.label_base)
    words = placer.box(step["words"], step["words_zone"], placer.label_base)
    assert v0 - 1e-9 <= v <= v1 + 1e-9, (v, v0, v1, step["words_zone"])
    assert not words[1] <= v <= words[3], (v, words)


def test_poster_is_at_rest_without_words(tmp_path):
    """The poster is the last capture at rest, before its words land: not the film's tightest push
    with the words over its subject."""
    steps = [STEPS[0], {**STEPS[2], "focus": [0.5, 0.5, 1.6]}]
    p = _plan(tiny(tmp_path / "p", steps=steps)).plan
    last = [s for s in p["shots"] if s["kind"] == "capture"][-1]
    assert last["step"]["push"] and last["capture"] == DATES[4]
    assert p["poster"] == last["f0"] + B - 1 < min(last["step"]["push_f0"], last["step"]["land"])
    cut = _Cut(tiny(tmp_path / "p", steps=steps), FilmPlan(id="16x9", plan=p))
    assert cut.camera(p["poster"])[2] == 1.0


def _changed_at(root: Path, spots: dict[str, list[tuple[float, float]]]) -> None:
    """Masters that are one textured picture on every date, plus a bright square (0.12 × 0.10 of the
    master) at each (x, y) listed for a date and every date after it: what changes is only there."""
    rng = np.random.default_rng(7)
    base = np.empty((SIZE[1], SIZE[0], 3), np.float32)
    base[:] = (70, 110, 90)
    base += rng.normal(0, 6, base.shape).astype(np.float32)
    shown: list[tuple[float, float]] = []
    for date in DATES:
        shown += spots.get(date, [])
        img = base.copy()
        for x, y in shown:
            x0, y0 = round((x - 0.06) * SIZE[0]), round((y - 0.05) * SIZE[1])
            img[y0 : y0 + round(0.10 * SIZE[1]), x0 : x0 + round(0.12 * SIZE[0])] = (235, 240, 245)
        cv2.imwrite(str(root / "masters" / "site" / f"{date}.jpg"), np.clip(img, 0, 255).astype(np.uint8))


QUIET_STEPS = [STEPS[0], {"capture": DATES[2], "text": "**Demolition.** In ten weeks the slides are gone."}]


@pytest.mark.parametrize(("y", "lines"), [(0.45, 1), (0.2, None)])
def test_title_goes_on_one_line_off_what_changes(tmp_path, y, lines):
    """The screen's large title wraps; when its second line would sit on what the film shows changing
    on its picture (y 0.45), it goes on one line above it instead; when one line covers as much (the
    change is up top, y 0.2), it keeps its lines and the plan warns that it sits on the subject."""
    root = tmp_path / "p"
    project = tiny(root, steps=QUIET_STEPS, films={"screen": {"preset": "screen"}}, simulated=True)
    _changed_at(root, {DATES[4]: [(0.3, y), (0.6, y)]})
    plan = plan_films(project, only=["screen"], fast=False)["screen"]
    title = plan.plan["shots"][1]
    assert title["kind"] == "title" and title.get("title_lines") == lines, title
    assert title["title_subject"]["wrap"] > 1.0, title
    warned = [w for w in plan.warnings if w.startswith("title:")]
    cut = _Cut(project, plan)
    rows = {i["box"][1] for i in cut.ink if i["shot"] == title["id"] and i["layer"].startswith("title ")}
    if lines == 1:
        assert title["title_subject"]["one line"] < 0.5 and not warned, (title, warned)
        assert max(rows) - min(rows) <= 2, rows  # every group on one row
        x0, y0, x1, y1 = cut.lay.rect
        for i in cut.ink:
            if i["shot"] == title["id"]:
                bx, by, bw, bh = i["box"]
                assert x0 - 1 <= bx and bx + bw <= x1 + 1 and y0 - 1 <= by and by + bh <= y1 + 1, i
    else:
        assert max(rows) - min(rows) > 10 and len(warned) == 1, (rows, warned)


def _words_step(plan: FilmPlan) -> dict[str, Any]:
    return next(
        s["step"] for s in plan.plan["shots"] if s.get("step") and s["step"]["words"] == "Demolition."
    )


@pytest.mark.parametrize(("y", "zone"), [(0.62, "high"), (0.24, "low")])
def test_words_sit_clear_of_what_changed(tmp_path, y, zone):
    """In 9:16 a step's words go high when what changed since the previous capture is under the low
    zone, and stay low (the default) when it is up top. No focus, no hotspot: the change alone."""
    root = tmp_path / "p"
    project = tiny(root, steps=QUIET_STEPS, film={"vertical": True}, simulated=True)
    _changed_at(root, {DATES[2]: [(0.5, y)]})
    plan = _plan(project, "9x16")
    step = _words_step(plan)
    assert step["words_zone"] == zone, step
    assert step["words_reason"] == ("least subject" if zone == "high" else "clear"), step
    assert step["words_subject"]["low" if zone == "high" else "high"] > 1.0, step  # the other is on it
    assert step["words_subject"][zone] < 0.5 and step["words_contrast"][zone] >= 3.0, step
    assert not any("sit on the subject" in w for w in plan.warnings), plan.warnings
    # The renderer sets them there: under the date label in the high zone, at the lead baseline in
    # the low one, inside the safe zone either way.
    cut = _Cut(project, plan)
    shot = next(s for s in plan.plan["shots"] if s.get("step") is step)
    ink = next(i["box"] for i in cut.ink if i["shot"] == shot["id"] and i["layer"] == "words")
    label = next(i["box"] for i in cut.ink if i["shot"] == shot["id"] and i["layer"] == "label")
    lay = cut.lay
    assert ink[1] > label[1] + label[3]  # below the date label
    if zone == "high":
        assert ink[1] + ink[3] < 0.4 * cut.height, ink
    else:
        assert ink[1] < lay.lead_baseline < ink[1] + ink[3], ink
    x0, y0, x1, y1 = lay.rect
    assert x0 - 1 <= ink[0] and ink[0] + ink[2] <= x1 + 1 and y0 - 1 <= ink[1] and ink[1] + ink[3] <= y1 + 1


def test_landscape_words_move_off_a_low_left_subject(tmp_path):
    """16:9 has three zones (low, low-right, high): a change under the low-left words moves them to
    the zone over the least of it, and the plan records every zone's measure."""
    root = tmp_path / "p"
    project = tiny(root, steps=QUIET_STEPS)
    _changed_at(root, {DATES[2]: [(0.12, 0.8)]})
    step = _words_step(_plan(project, "16x9"))
    assert set(step["words_subject"]) == {"low", "low-right", "high"}, step
    assert step["words_zone"] in ("low-right", "high") and step["words_subject"]["low"] > 1.0, step


def test_words_on_the_subject_in_every_zone_is_a_warning(tmp_path):
    """A change under both 9:16 zones: the words take the zone over the least of it, with a warning."""
    root = tmp_path / "p"
    project = tiny(root, steps=QUIET_STEPS, film={"vertical": True})
    _changed_at(root, {DATES[2]: [(0.45, 0.25), (0.45, 0.6)]})
    plan = _plan(project, "9x16")
    assert any(
        "the words 'Demolition.' sit on the subject in every zone (9x16" in w for w in plan.warnings
    ), plan.warnings


def test_push_words_stay_off_what_the_push_frames(tmp_path, monkeypatch):
    """A push closes in on its focus: the words keep clear of what it frames, not only of the focus
    point. Here what changed is far up at the right and the push frames the low middle of the
    picture, so the 16:9 words leave the low zone, whose box runs over the rim of the framed area."""
    import test_film
    from vantage.film.story import WORDS_CLEAR

    big = (2560, 1600)
    monkeypatch.setattr(test_film, "SIZE", big)
    monkeypatch.setitem(globals(), "SIZE", big)
    root = tmp_path / "p"
    steps = [STEPS[0], {**QUIET_STEPS[1], "focus": [0.45, 0.7, 1.4]}]
    project = tiny(root, steps=steps)
    _changed_at(root, {DATES[2]: [(0.85, 0.15)]})
    step = _words_step(_plan(project, "16x9"))
    assert step["push"] and step["target"] == "focus", step
    assert step["words_zone"] != "low" and step["words_reason"] == "least subject", step
    assert step["words_subject"]["low"] > WORDS_CLEAR, step


def test_words_leave_a_zone_on_the_subject_when_another_is_clear(monkeypatch):
    """The words keep the step before's zone while it is near the least subject, but never while it
    sits on the subject (over WORDS_ON) and a legible zone does not. 'In every zone' is said only when
    every zone is over WORDS_ON; otherwise the warning names the zone and why it was kept."""
    import types

    import vantage.film.subject as subject
    from vantage.film.story import _Placer

    def run(on: dict[str, float], ratio: dict[str, float], default: str) -> tuple[dict[str, Any], list[str]]:
        monkeypatch.setattr(subject, "under", lambda _s, _p, box: on[box])
        monkeypatch.setattr(subject, "contrast", lambda _seen, _f, _p, box, *_a: ratio[box])
        placer = types.SimpleNamespace(
            zones=list(on),
            seen=None,
            paper=None,
            night=None,
            box=lambda _w, zone, _b, _s=None: zone,
            warn=_Placer.warn,
        )
        b = types.SimpleNamespace(warnings=[], film=types.SimpleNamespace(id="16x9"))
        paths: dict[str, list[Any]] = {zone: [] for zone in on}
        out = _Placer.choose(placer, b, "water", "From the water", 0.0, object(), ["m.jpg"], paths, default)
        return out, b.warnings

    legible = {"low": 7.0, "low-right": 7.0, "high": 7.0}
    out, warnings = run({"low": 1.035, "low-right": 2.263, "high": 0.959}, legible, "low")
    assert (out["words_zone"], out["words_reason"]) == ("high", "least subject"), out
    assert not warnings, warnings
    out, warnings = run({"low": 1.1, "low-right": 1.4, "high": 1.05}, legible, "low")
    assert (out["words_zone"], out["words_reason"]) == ("low", "near least subject"), out
    assert len(warnings) == 1 and "sit on the subject in every zone" in warnings[0], warnings
    out, warnings = run({"low": 1.2, "low-right": 1.4, "high": 0.3}, {**legible, "high": 2.0}, "low")
    assert out["words_zone"] == "low", out
    assert any("in low, as the zones clear of it keep under 3:1" in w for w in warnings), warnings
    assert not any("every zone" in w for w in warnings), warnings


def test_curtain_line_sits_clear_of_its_push(tmp_path):
    """A curtain step's line goes high in 9:16 when its push's subject (the step's focus, low on
    screen) and what changed are under the low zone."""
    root = tmp_path / "p"
    compare = {**COMPARE, "steps": [{"text": "The pad.", "split": 0.5, "focus": [0.5, 0.62, 1.3]}]}
    project = tiny(root, steps=QUIET_STEPS, chapters=[compare], film={"vertical": True})
    _changed_at(root, {DATES[4]: [(0.5, 0.64)]})
    curtain = next(s for s in _plan(project, "9x16").plan["shots"] if s["kind"] == "curtain")
    step = curtain["steps"][0]
    assert step["words"] == "The pad." and step["words_zone"] == "high", step


def test_curtain_rule_breaks_behind_its_words(tmp_path):
    """Where the curtain's rule crosses a step's line, it breaks behind the line's ink (and comes
    back above and below it), so it never slices through the words."""
    compare = {**COMPARE, "steps": [{"text": "Half and half, the pool and the beach.", "split": 0.5}]}
    project = tiny(tmp_path / "p", chapters=[compare], film={"vertical": True})
    for fid in ("16x9", "9x16"):
        plan = _plan(project, fid)
        curtain = next(s for s in plan.plan["shots"] if s["kind"] == "curtain")
        cut = _Cut(project, plan)
        bx, by, bw, bh = next(
            i["box"] for i in cut.ink if i["shot"] == curtain["id"] and i["layer"] == "words"
        )
        f = curtain["keys"][-1]["b"] - 1  # held at 0.5, the line fully in
        x = round(_split(curtain, f) * cut.width)
        assert bx < x < bx + bw, (fid, x, (bx, bw))
        frame = cut.frame_at(f).astype(np.int16)
        grip, cut._grip = cut._grip, lambda *a, **k: None  # type: ignore[method-assign]
        try:
            bare = cut.frame_at(f).astype(np.int16)
        finally:
            cut._grip = grip  # type: ignore[method-assign]
        rule = np.abs(frame[:, x - 2 : x + 3] - bare[:, x - 2 : x + 3]).max(axis=(1, 2))
        assert rule[by + bh + 4 :].max() > 40 and rule[: max(1, by - 8)].max() > 40, fid  # it is there
        assert rule[by : by + bh].max() <= 2, (fid, rule[by : by + bh].tolist())  # but not behind them


def test_curtain_rule_stops_short_of_the_tag(tmp_path):
    """A rule swept under the top-right tag (a split of 0.9) stops short of the tag and its scrim's
    padding, so the honesty marker is never struck through; lower down the rule is there."""
    compare = {**COMPARE, "steps": [{"text": "Most of it.", "split": 0.9}]}
    project = tiny(tmp_path / "p", chapters=[compare], simulated=True)
    plan = _plan(project)
    curtain = next(s for s in plan.plan["shots"] if s["kind"] == "curtain")
    cut = _Cut(project, plan)
    tx0, _, tw, th = next(i["box"] for i in cut.ink if i["layer"] == "tag")
    tx1, ty1 = tx0 + tw, th + next(i["box"][1] for i in cut.ink if i["layer"] == "tag")
    f = curtain["keys"][-1]["b"] - 1
    x = round(_split(curtain, f) * cut.width)
    assert tx0 < x < tx1, (x, tx0, tx1)
    frame = cut.frame_at(f).astype(np.int16)
    grip, cut._grip = cut._grip, lambda *a, **k: None  # type: ignore[method-assign]
    try:
        bare = cut.frame_at(f).astype(np.int16)
    finally:
        cut._grip = grip  # type: ignore[method-assign]
    rule = np.abs(frame[:, x - 1 : x + 2] - bare[:, x - 1 : x + 2]).max(axis=(1, 2))
    assert rule[: math.ceil(ty1) + 2].max() <= 2, rule[: math.ceil(ty1) + 2].tolist()
    assert rule[cut.height // 2 :].max() > 40


def test_curtain_lines_hand_off_without_overlap(tmp_path):
    """Consecutive curtain lines share a zone: the first is out by the downbeat the next lands on."""
    compare = {
        **COMPARE,
        "steps": [{"text": "Then.", "split": 0.8}, {"text": "Now.", "split": 0.3}],
    }
    project = tiny(tmp_path / "p", chapters=[compare])
    plan = _plan(project)
    curtain = next(s for s in plan.plan["shots"] if s["kind"] == "curtain")
    first, second = curtain["steps"]
    assert (first["words"], second["words"]) == ("Then.", "Now.")
    cut = _Cut(project, plan)
    land = second["land"]
    assert cut._line(curtain, 0, land - cut.third - 1) == (1.0, 1.0)  # whole until B/3 before
    assert 0.0 < cut._line(curtain, 0, land - cut.third)[1] < 1.0
    assert cut._line(curtain, 0, land - 1)[1] == 0.0  # out the frame before the next one lands
    for f in range(land, land + cut.third + 1):  # out while the next one fades in
        assert cut._line(curtain, 0, f)[1] == 0.0, f
    assert cut._line(curtain, 1, land - 1) == (0.0, 0.0) and cut._line(curtain, 1, land + cut.third)[0] == 1.0


def test_words_plan_is_deterministic(tmp_path):
    root = tmp_path / "p"
    project = tiny(root, steps=QUIET_STEPS, film={"vertical": True})
    _changed_at(root, {DATES[2]: [(0.5, 0.62)]})
    assert _plan(project, "9x16").hash == _plan(project, "9x16").hash


def test_push_eases_back_as_slowly_as_it_went_in(tmp_path):
    """Every camera move lasts at least a push: B + B/3 frames in a portrait film, 2 beats in a
    landscape one (a 1.6× push in under a second lurches on a desktop or a TV), so the release after
    a push never snaps back inside the next dissolve; check_plan refuses a move under B + B/3."""
    steps = [STEPS[0], {**STEPS[1], "focus": [0.5, 0.5, 1.6]}, STEPS[2]]
    project = tiny(tmp_path / "p", steps=steps, chapters=[COMPARE])
    p = _plan(project).plan
    push = next(s["step"] for s in p["shots"] if s.get("step") and s["step"]["push"])
    assert push["push_f1"] - push["push_f0"] == 2 * B
    cams = p["camera"]
    moves = [w1["a"] - w0["b"] for w0, w1 in itertools.pairwise(cams)]
    assert moves and min(moves) >= 2 * B, cams
    # A portrait film keeps B + B/3 (B/3 = 3 at B = 8): read on a phone in the hand.
    from vantage.film.story import _Builder

    portrait = dataclasses.replace(resolve_films(project)["16x9"], aspect="9x16", loop=False)
    lay = Layout.from_record(180, 320, p["layout"])
    b = _Builder(project, portrait, beat.beat_grid(12, 90), lay, [], 0)
    assert b.ease == B + 3
    fast = json.loads(json.dumps(p))
    fast["camera"][1]["a"] = fast["camera"][0]["b"] + 4
    with pytest.raises(PlanError, match="faster than a push"):
        check_plan(fast)


def test_default_is_story_and_classic_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    story_film = {k: v for k, v in FILM.items() if k != "cut"}
    project = make_project(tmp_path / "s", film=story_film)
    films = render_film(project, tmp_path / "s-out")
    assert [f.name for f in films] == ["tiny-site-16x9.mp4", "tiny-site-9x16.mp4"]
    for fid in ("16x9", "9x16"):
        assert FilmPlan.load(tmp_path / "s-out" / f"tiny-site-{fid}.plan.json").plan["cut"] == "story"
    classic = make_project(tmp_path / "c", film={**story_film, "cut": "classic", "vertical": False})
    out = {}
    for run in ("a", "b"):
        out[run] = tmp_path / f"c-{run}"
        render_film(classic, out[run])
    assert FilmPlan.load(out["a"] / "tiny-site-16x9.plan.json").plan["cut"] == "classic"
    for name in ("tiny-site-16x9.mp4", "tiny-site-16x9.jpg", "tiny-site-16x9.plan.json"):
        assert (out["a"] / name).read_bytes() == (out["b"] / name).read_bytes(), name


def test_parallel_equals_sequential(tmp_path, monkeypatch):
    project = tiny(tmp_path / "p", film={"vertical": True}, chapters=[COMPARE], simulated=True)
    out = {}
    for jobs in ("1", "2"):
        monkeypatch.setenv("VANTAGE_FILM_JOBS", jobs)
        assert film_jobs(2) == int(jobs)
        out[jobs] = tmp_path / f"jobs-{jobs}"
        render_film(project, out[jobs])
    names = sorted(p.name for p in out["1"].iterdir())
    assert names == sorted(p.name for p in out["2"].iterdir()) and len(names) == 18
    assert not [n for n in names if n.endswith(".part")]
    for name in names:
        assert (out["1"] / name).read_bytes() == (out["2"] / name).read_bytes(), name


def test_film_jobs(monkeypatch):
    monkeypatch.delenv("VANTAGE_FILM_JOBS", raising=False)
    assert film_jobs(1) == 1
    monkeypatch.setenv("VANTAGE_FILM_JOBS", "8")
    assert film_jobs(3) == 3
    monkeypatch.setenv("VANTAGE_FILM_JOBS", "many")
    with pytest.raises(ValueError, match="VANTAGE_FILM_JOBS"):
        film_jobs(2)


def test_release_gate_on_story_film(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    root, dist = tmp_path / "p", tmp_path / "dist"
    stats = {**STATS, "items": [STATS["items"][2]]}
    tiny(root, chapters=[stats])
    project = _facts(root, area={"text": "13,100", "status": "needs-client", "sources": ["plan"]})
    render_film(project, dist / "film")
    plan = FilmPlan.load(dist / "film" / "tiny-site-16x9.plan.json")
    assert plan.facts["area"]["releasable"] is False
    # The film gate itself (check_release, which runs first in package --release, also names 'area').
    assert "film/tiny-site-16x9.mp4 prints fact 'area', which is not releasable" in check_films(
        project, dist / "film"
    )
    info = json.loads(package_project(project, dist)["build_json"].read_text())
    assert info["films"]["16x9"]["unreleasable"] == ["area"]
    with pytest.raises(ReleaseError, match="area"):
        package_project(load_project(root), dist, release=True)


def test_fast_preview_makes_the_same_cut(tmp_path, monkeypatch):
    """max_upscale is judged at the full size: a VANTAGE_FAST plan clamps the same pushes, so its
    beats, captions and poster frame match the full-size film's."""
    import test_film

    big = (1280, 800)
    monkeypatch.setattr(test_film, "SIZE", big)
    monkeypatch.setitem(globals(), "SIZE", big)
    steps = [{"capture": DATES[2], "text": "**Middle.**", "focus": [0.5, 0.5, 1.4]}]
    project = tiny(tmp_path / "p", steps=steps, film={"width": 1920, "height": 1080, "max_upscale": 1.55})
    fast, full = (plan_films(project, only=["16x9"], fast=f)["16x9"] for f in (True, False))
    assert (fast.plan["width"], full.plan["width"]) == (960, 1920)
    step = next(s["step"] for s in full.plan["shots"] if s.get("step"))
    assert step["push"] is False and step["reason"] == "clamped"  # 1.55 / 1.5 < MIN_ZOOM at full size
    for key in ("grid", "frames", "captions", "chapters", "transcript", "poster"):
        assert fast.plan[key] == full.plan[key], key
    assert [s["step"]["push"] for s in fast.plan["shots"] if s.get("step")] == [False]


def test_gate_hints_at_vantage_fast(tmp_path, monkeypatch):
    """A film planned at the VANTAGE_FAST size is stale at full size, and the gate says why, even
    though a story cut's shots (px) differ too."""
    monkeypatch.setenv("VANTAGE_FAST", "1")
    project = tiny(tmp_path / "p", film={"width": 1920, "height": 1080})
    render_film(project, tmp_path / "film", plan_only=True)
    monkeypatch.delenv("VANTAGE_FAST")
    stale = [p for p in check_films(project, tmp_path / "film") if "is stale" in p]
    assert len(stale) == 1 and "(rendered with a different VANTAGE_FAST?)" in stale[0], stale


# --------------------------------------------------------------------------- #
# Montage: a gallery chapter of another drone vantage
# --------------------------------------------------------------------------- #

WATER = ("2025-02-01", "2025-04-01", "2025-06-01", "2025-08-01", "2025-10-01", "2025-12-01", "2026-02-01")
GALLERY = {
    "type": "gallery",
    "id": "from-the-water",
    "kicker": "From the water",
    "title": "Every flight, seen from the lake",
    "vantage": "water",
}
FIGURE = {
    "type": "stats",
    "id": "numbers",
    "items": [{"value": "{fact:area}", "unit": "sq ft", "label": "of old pool", "source": "Site plan"}],
}


def _montage(project: Project, fid: str = "16x9") -> tuple[dict[str, Any], list[dict[str, Any]]]:
    p = _plan(project, fid).plan
    return p, [s for s in p["shots"] if s.get("montage")]


def test_montage_shows_the_other_vantage(tmp_path):
    """A gallery chapter on another drone vantage plays as a montage in both cuts: five captures
    spread evenly from its first to its last, dissolving on the beat with no card, the date label per
    capture, the chapter's kicker over the first picture for as long as it takes to read, the last
    picture held 3 beats (and a beat more while the stats' panel comes in before the first figure
    lands), no push, and the outgoing vantage keeping its own framing through the dissolve. The stat run follows it on its last picture (no dissolve: only the panel and text come
    in), so the film's middle is not one photograph and it closes on the figures."""
    project = tiny(
        tmp_path / "p", chapters=[FIGURE, GALLERY], extra={"water": list(WATER)}, film={"vertical": True},
        simulated=True,
    )  # fmt: skip
    project = _facts(project.root, area=AREA)
    for fid in ("16x9", "9x16"):
        p, run = _montage(project, fid)
        kinds = [s["kind"] for s in p["shots"]]
        assert "card" not in kinds and kinds[-1] == "end", kinds
        stat = p["shots"][kinds.index("stat")]
        assert kinds.index("stat") == p["shots"].index(run[-1]) + 1, kinds  # the stats close the film
        assert stat["file"] == run[-1]["file"] and stat["vantage"] == "water" and stat["dissolve"] is None
        assert stat["land"] >= stat["f0"] + B  # the last picture: 3 beats clean, then the panel's beat
        assert [s["capture"] for s in run] == [WATER[k] for k in (0, 2, 3, 5, 6)], fid
        assert {s["vantage"] for s in run} == {"water"} and all(s["kind"] == "capture" for s in run)
        assert [s["label"] for s in run] == [s["text"][0]["text"] for s in run]
        assert [s["rail"] for s in run] == [0, 1, 2, 3, 4] and len({s["series"] for s in run}) == 1
        beats = [(s["f1"] - s["f0"]) // B for s in run]
        assert beats[1:] == [2, 2, 2, 3], beats
        for s in run:
            assert s["f0"] % B == 0 and s["dissolve"]["f0"] == s["f0"]
            assert s["dissolve"]["f1"] - s["dissolve"]["f0"] == D
        assert [s["dissolve"]["from"] for s in run[1:]] == [s["capture"] for s in run[:-1]]
        first = run[0]
        prev = p["shots"][p["shots"].index(first) - 1]
        assert first["dissolve"]["from"] == prev["capture"] and prev["kind"] == "capture"
        assert prev["vantage"] == "site"
        # The scrub's picture keeps its own camera through the dissolve; the camera cuts to the
        # montage's rest view on its first frame and then drifts in by 4 % over the montage, and
        # on through the stat on its last picture as one move: it never stops on the way (no
        # held window between the montage's first frame and the drift's end).
        cut = _Cut(project, FilmPlan(id=fid, plan=p))
        assert first["dissolve"]["cam"] == list(cut.camera(first["f0"] - 1))
        assert cut.camera(first["f0"]) == (0.5, 0.5, 1.0)  # the water vantage's rest view
        zooms = [cut.camera(f)[2] for f in range(first["f0"], run[-1]["f1"])]
        assert all(b >= a for a, b in itertools.pairwise(zooms)) and zooms[-1] <= 1.04**2 + 1e-9
        assert zooms[-1] > 1.0 or fid == "9x16"  # the tiny 9:16 masters are at max_upscale at rest
        if zooms[-1] > 1.0:
            move = stat["move"]
            assert move["how"] == "drift" and move["f1"] > stat["land"], move
            zooms = [cut.camera(f)[2] for f in range(first["f0"], move["f1"] + 1)]
            assert all(b > a for a, b in itertools.pairwise(zooms[1:])), "the drift stops on the way"
            assert zooms[-1] == pytest.approx(1.04**2) and move["focus"][2] == pytest.approx(1.04**2)
        step = first["step"]
        assert first["text"][1] == {"role": "words", "text": "From the water"}
        assert step["words"] == "From the water" and step["words_from"] == "kicker" and not step["push"]
        assert step["land"] == first["f0"] + B and step["words_zone"] in ("low", "high", "low-right")
        assert first["f1"] - step["land"] >= round(B / 3) + math.ceil(len("From the water") * 12 / 17)
        assert all(s["step"] is None for s in run[1:])
        check_plan(p)
        # Captions, chapters and transcript include it.
        heads = [c for c in p["chapters"] if c["f0"] == first["f0"]]
        # Titled by what it shows, not by its chapter title ("Every flight, …": it plays a few).
        assert heads and heads[0]["title"] == "From the water", p["chapters"]
        said = " ".join(c["text"] for c in p["captions"])
        assert "From the water" in said and all(s["label"] in said for s in run)
        lines = [ln["text"] for ln in p["transcript"]["lines"] if ln["f0"] >= first["f0"]]
        assert lines[0].startswith(f"Aerial view (Water), {first['label']}, with the words “From the water”")
    # The storyboard names the montage's tiles.
    from vantage.film.storyboard import tiles

    assert [t.kind for t in tiles(_plan(project)) if t.shot == run[0]["id"]] == ["montage"]


def test_stats_after_a_montage_close_on_two_pictures(tmp_path):
    """After a montage the run's first stat sits on its last picture and the next on the main
    vantage's latest, dissolving to it under the panel, so the close is not one photograph. The
    montage's drift carries on through the first stat as one move that never stops, and keeps what
    changed on the picture (a bright block near its right edge) in frame as it closes in, where a
    drift about the centre would cut it."""
    project = tiny(
        tmp_path / "p", chapters=[RUN, GALLERY], extra={"water": list(WATER)}, film={"vertical": True},
        simulated=True,
    )  # fmt: skip
    project = _facts(project.root, area=AREA, trees=TREES)
    rng = np.random.default_rng(3)
    base = np.empty((SIZE[1], SIZE[0], 3), np.float32)
    base[:] = (90, 80, 60)
    base += rng.normal(0, 6, base.shape).astype(np.float32)
    block = (0.86, 0.45, 0.98, 0.55)  # x0, y0, x1, y1: what the last flight shows that the first did not
    for date in WATER:
        img = base.copy()
        if date == WATER[-1]:
            img[
                round(block[1] * SIZE[1]) : round(block[3] * SIZE[1]),
                round(block[0] * SIZE[0]) : round(block[2] * SIZE[0]),
            ] = 240
        cv2.imwrite(str(project.masters_dir / "water" / f"{date}.jpg"), np.clip(img, 0, 255).astype(np.uint8))
    plan = _plan(project)
    p = plan.plan
    check_plan(p)
    run = [s for s in p["shots"] if s.get("montage")]
    n1, n2 = (s for s in p["shots"] if s["kind"] == "stat")
    latest = MastersIndex.load(project.masters_dir / "index.json").vantages["site"].captures[-1]
    assert n1["file"] == run[-1]["file"] and n1["dissolve"] is None
    assert (n2["vantage"], n2["file"]) == ("site", latest.file), n2
    d = n2["dissolve"]
    assert (d["from"], d["f0"], d["f1"]) == ("N1", n2["f0"], n2["f0"] + D) and n2["land"] == n2["f0"] + B
    # One drift from the montage's first frame to the end of N1's: it never stops on the way.
    cut = _Cut(project, plan)
    move = n1["move"]
    assert move["how"] == "drift", move
    zooms = [cut.camera(f)[2] for f in range(run[0]["f0"], move["f1"] + 1)]
    assert all(b > a for a, b in itertools.pairwise(zooms[1:])), "the drift stops on the way"
    # Its end view still holds the block whole; zooming in about the rest view's centre would not.
    mv = MastersIndex.load(project.masters_dir / "index.json").vantages["water"]
    geo = Geometry(p["width"], p["height"], mv.width, mv.height)
    x, y, z = cut.camera(move["f1"])
    cx, cy = geo.centre(x, y, z)
    hw, hh = geo.half(z)
    assert cx - hw <= block[0] and block[2] <= cx + hw and cy - hh <= block[1] and block[3] <= cy + hh
    assert 0.5 + geo.half(z)[0] < block[2]
    # Under the panel: half way through the dissolve the panel is up, N1's text gone, N2's not in.
    f = n2["f0"] + D // 2
    frame = cut.frame_at(f).astype(np.int16)
    panel = cut.stat_runs["N2"]["panel"]
    assert panel is cut.stat_runs["N1"]["panel"]
    cut.stat_runs["N1"]["panel"] = None
    try:
        without = cut.frame_at(f).astype(np.int16)
    finally:
        cut.stat_runs["N1"]["panel"] = panel
    assert np.abs(frame - without).max() > 20, "no panel under the change of picture"
    with _NoText(cut):
        bare = cut.frame_at(f).astype(np.int16)
    for sid in ("N1", "N2"):
        for layers in (cut.layers[sid]["glyphs"], cut.layers[sid]["rest"]):
            c = _cores(layers, cut.width, cut.height)
            assert np.abs(frame[c] - bare[c]).max(axis=1).mean() < 3, (sid, f)


def test_montage_frames(tmp_path):
    """Rendered: no jump into the dissolve (the scrub's picture keeps its framing), each montage
    picture is the capture it names, and the kicker is on the first one."""
    project = tiny(tmp_path / "p", chapters=[FIGURE, GALLERY], extra={"water": list(WATER)})
    project = _facts(project.root, area=AREA)
    p, run = _montage(project)
    cut = _Cut(project, FilmPlan(id="16x9", plan=p))
    f0, stat = run[0]["f0"], p["shots"][p["shots"].index(run[0]) - 1]
    held = tuple(run[0]["dissolve"]["cam"])
    assert held == cut.camera(f0 - 1) and stat["vantage"] == "site"
    assert cut.scrims  # the tag's and labels' scrims are measured first, over frames of their own
    warps: list[tuple[str, tuple[float, ...]]] = []
    warp = cut._warp
    cut._warp = lambda file, cam: warps.append((file, tuple(cam))) or warp(file, cam)  # type: ignore[method-assign]
    for f in range(f0, f0 + D):
        cut.frame_at(f)
    assert {c for file, c in warps if file == stat["file"]} == {held}  # no jump into the dissolve
    assert max(c[2] for file, c in warps if file == run[0]["file"]) < 1.001  # its own rest, drifting
    cut._warp = warp  # type: ignore[method-assign]
    from test_film import HUES as WATER_HUES

    for s in run:
        k = WATER.index(s["capture"])
        f = s["f1"] - 1
        centre = cut.frame_at(f)[80:110, 130:190].reshape(-1, 3).astype(np.float32).mean(axis=0)
        assert np.abs(centre - WATER_HUES[k % len(WATER_HUES)]).max() < 40, (s["capture"], centre)
    assert any(i["shot"] == run[0]["id"] and i["layer"] == "words" for i in cut.ink)


@pytest.mark.parametrize(
    ("gallery", "films", "extra", "count"),
    [
        ({}, None, None, 5),
        ({"film": {"skip": True}}, None, None, 0),
        ({}, {"16x9": {"montage": 0}}, None, 0),
        ({}, {"16x9": {"montage": 3}}, None, 3),
        ({"film": {"line": "Seen from the lake"}}, None, None, 5),
        ({"captures": ["2025-04-01", "2025-08-01", "latest"]}, None, None, 3),
        ({"vantage": "site"}, None, None, 0),  # the main vantage already plays as the scrub
        ({}, None, {"kind": "ground"}, 0),  # films show drone vantages only
    ],
)
def test_montage_opt_outs_and_cap(tmp_path, gallery, films, extra, count):
    root = tmp_path / "p"
    project = tiny(root, chapters=[{**GALLERY, **gallery}], films=films, extra={"water": list(WATER)})
    if extra:
        data = yaml.safe_load((root / "story.yaml").read_text())
        data["vantages"][-1].update(extra)
        (root / "story.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        project = load_project(root)
    _, run = _montage(project)
    assert len(run) == count
    if count:
        assert run[0]["capture"] == ((gallery.get("captures") and "2025-04-01") or WATER[0])
        assert run[-1]["capture"] == WATER[-1]
        assert run[0]["step"]["words"] == (gallery.get("film") or {}).get("line", "From the water")


def test_montage_needs_two_captures(tmp_path):
    with pytest.raises(ValidationError, match="montage"):
        tiny(tmp_path / "p", films={"16x9": {"montage": 1}})


def test_cross_vantage_dissolve_may_cut_the_camera(tmp_path):
    """check_plan lets the camera cut only on the first frame of a dissolve between two vantages
    whose outgoing picture keeps its own framing (`dissolve.cam`): here the scrub's last step
    pushes, and the montage of the water vantage dissolves in from that framing."""
    steps = [STEPS[0], STEPS[1], {**STEPS[2], "focus": [0.6, 0.5, 1.4]}]
    project = tiny(tmp_path / "p", steps=steps, chapters=[FIGURE, GALLERY], extra={"water": list(WATER)})
    p, run = _montage(_facts(project.root, area=AREA))
    check_plan(p)
    bad = json.loads(json.dumps(p))
    shot = next(s for s in bad["shots"] if s["id"] == run[0]["id"])
    del shot["dissolve"]["cam"]
    k = next(i for i, w in enumerate(bad["camera"]) if w["a"] == run[0]["f0"])
    bad["camera"][k]["zoom"] = 1.2  # a jump the dissolve does not cover
    with pytest.raises(PlanError, match="faster than a push"):
        check_plan(bad)


@pytest.mark.slow
def test_demo_story_cut(tmp_path):
    """The fictional demo end to end (fast footage → masters → plans): both story cuts are 77 beats
    (two figures, two or three montage pictures: story.yaml films, and what the flash margin holds
    or softens), both close on their stats (the first over the shoreline montage's last picture, the
    second over the overview's latest), both within
    the flash margin, the Demolition push frames the slide tower in 16:9 and keeps its reach in
    9:16, the stats print exactly the figures the films name, and "The beach." sits clear of the
    beach."""
    import shutil

    from vantage.demo import synth
    from vantage.ingest.catalog import build_catalog
    from vantage.paths import repo_root
    from vantage.process.masters import make_masters
    from vantage.process.select import select_frames

    demo = repo_root() / "projects" / "demo-lakeside"
    footage = tmp_path / "footage"
    synth.generate_demo_footage(footage, fast=True)
    root = tmp_path / "demo"
    root.mkdir()
    for name in ("project.yaml", "story.yaml", "facts.yaml"):
        shutil.copy(demo / name, root / name)
    shutil.copytree(demo / "brand", root / "brand")
    text = (root / "project.yaml").read_text(encoding="utf-8")
    (root / "project.yaml").write_text(
        text.replace("footage_dir: footage", f"footage_dir: {json.dumps(str(footage))}"), encoding="utf-8"
    )
    project = load_project(root)
    make_masters(project, select_frames(project, build_catalog(project)))
    plans = plan_films(project, fast=True)
    assert list(plans) == ["16x9", "9x16"]
    tower = next(h for ch in project.story.chapters if ch.id == "twelve-flights" for h in ch.hotspots)
    assert tower.id == "slide-tower"
    for fid, plan in plans.items():
        p = plan.plan
        assert p["cut"] == "story" and p["grid"]["beats"] == {"16x9": 77, "9x16": 77}[fid], (fid, p["grid"])
        check_plan(p)
        assert p["flash"]["limit"] == 2 and plan.checks["flash"]["estimate"] <= 2, (fid, plan.checks)
        shot = next(s for s in p["shots"] if s.get("step") and s["step"]["words"] == "Demolition.")
        step = shot["step"]
        mv = MastersIndex.load(root / "masters" / "index.json").vantages["overview"]
        geo = Geometry(p["width"], p["height"], mv.width, mv.height)
        cx, cy = geo.centre(*step["focus"])
        hw, hh = geo.half(step["focus"][2])
        if fid == "16x9":
            assert step["push"] and step["target"] == "focus+subject:slide-tower", (fid, step)
            assert cx - hw <= tower.x <= cx + hw and cy - hh <= tower.y <= cy + hh, fid
        else:  # far outside the portrait crop at rest: no whip across the master to reach it
            assert step["target"] == "focus, subject off-frame:slide-tower", (fid, step)
        # The shoreline gallery plays as a montage with no card, then the two figures the demo's
        # films name close each film over its last picture, then the end card.
        run = [s for s in p["shots"] if s.get("montage")]
        captures = [s["capture"] for s in run]  # spread from the gallery's first flight to its last
        assert len(captures) == {"16x9": 2, "9x16": 3}[fid], captures
        assert captures[0] == "2025-04-12" and captures[-1] == "2026-09-20", captures
        assert {s["vantage"] for s in run} == {"shoreline"}
        assert run[0]["step"]["words"] == "From the water" and "card" not in [s["kind"] for s in p["shots"]]
        stats = [s for s in p["shots"] if s["kind"] == "stat"]
        assert [f for s in stats for f in s["facts"]] == ["wave-pool-area", "flights"]
        assert stats[0]["value"] == "13,100", stats
        assert p["shots"].index(stats[0]) == p["shots"].index(run[-1]) + 1
        assert stats[0]["file"] == run[-1]["file"] and stats[0]["dissolve"] is None
        # The second figure closes over the whole site: the overview's latest, dissolved to under the panel.
        assert stats[1]["vantage"] == "overview" and stats[1]["capture"] == "2026-09-20", stats[1]
        assert stats[1]["dissolve"]["from"] == stats[0]["id"]
        # The CI-sized masters leave the stats no zoom to push with: they drift or hold.
        assert all(s["move"]["how"] in ("drift", "hold") for s in stats), [s["move"] for s in stats]
        end = p["shots"][-1]
        assert end["kind"] == "end" and p["shots"][-2] is stats[-1]
        assert end["ride"] and end["f1"] - end["f0"] <= 6 * p["fps"] and end["f1"] == p["frames"]
        # "The beach." leaves the beach and splash pad it names: in 9:16 it goes up over the water.
        beach = next(s["step"] for s in p["shots"] if s.get("step") and s["step"]["words"] == "The beach.")
        assert beach["words_subject"][beach["words_zone"]] <= 0.5, beach
        assert beach["words_zone"] == "high" or fid == "16x9", beach


def test_portrait_tag_stands_clear_of_the_rail(tmp_path):
    """In 9:16 the 'Simulated imagery' tag sits over the rail's right end with at least its own
    height of air between its ink and the rail's knob, so it reads as a tag, not as the rail's caption."""
    project = tiny(tmp_path / "p", simulated=True, film={"vertical": True, "width": 640, "height": 360})
    cut = _Cut(project, _plan(project, "9x16"))
    tag = next(i["box"] for i in cut.ink if i["layer"] == "tag")
    rail = next(i["box"] for i in cut.ink if i["layer"] == "rail")
    gap = rail[1] - (tag[1] + tag[3])
    assert gap >= tag[3], (tag, rail, gap)


@pytest.mark.parametrize("fid", ["16x9", "9x16"])
def test_tag_and_date_labels_keep_small_text_contrast(tmp_path, monkeypatch, fid):
    """The 'Simulated imagery' tag and the date labels are small type: their scrims are measured
    (plan.json checks.scrims) so each keeps 4.5:1 over every picture. Measured on rendered frames:
    the glyph cores' luminance against the 95th-percentile brightest pixel 1-4 px around them, on
    every half beat of the film. In 9:16 the stat panel's feather ends clear of the tag."""
    import cv2 as cv

    from vantage.film.cut import PANEL_PAD, PANEL_SIGMA

    project = _big(tmp_path, monkeypatch)
    plan = _plan(project, fid)
    cut = _Cut(project, plan)
    records = {r["text"]: r for r in cut.scrims if "text" in r}
    assert records["tag"]["contrast"] >= 4.5 and records["tag"]["alpha"] >= 0.3, records["tag"]
    labels = [r for k, r in records.items() if k.endswith(" label")]
    assert labels and all(r["contrast"] >= 4.5 for r in labels), labels

    def rings(layer: Any) -> tuple[tuple[np.ndarray, np.ndarray], tuple[np.ndarray, np.ndarray]]:
        a = layer.alpha[..., 0]
        m = (a > 0.02).astype(np.uint8)
        ring = cv.dilate(m, np.ones((9, 9), np.uint8)) & (1 - cv.dilate(m, np.ones((3, 3), np.uint8)))
        cy, cx = np.nonzero(a >= 0.9 * a.max())
        ry, rx = np.nonzero(ring)
        return (cy + layer.y, cx + layer.x), (ry + layer.y, rx + layer.x)

    def ratio(frame: np.ndarray, core: Any, ring: Any) -> float:
        lt, lb = float(np.median(_lum(frame[core]))), float(np.quantile(_lum(frame[ring]), 0.95))
        return (max(lt, lb) + 0.05) / (min(lt, lb) + 0.05)

    tag_core, tag_ring = rings(cut.tag)
    shots = plan.plan["shots"]
    B = plan.plan["grid"]["fpb"]
    for f in range(0, plan.plan["frames"], B // 2):
        shot = shots[max(k for k, s in enumerate(shots) if s["f0"] <= f)]
        if shot["kind"] in ("end", "card"):
            continue
        frame = cut.frame_at(f)
        assert ratio(frame, tag_core, tag_ring) >= 4.5, (fid, f, shot["id"])
        d = shot.get("dissolve")
        if shot["kind"] == "capture" and f >= max(shot["f0"] + B // 2, d["f1"] if d else 0):
            core, ring = rings(cut.layers[shot["id"]]["label"])
            assert ratio(frame, core, ring) >= 4.5, (fid, f, shot["id"], "label")
    if fid == "9x16":
        panel = cut.panels[0]["box"]
        tag = next(i["box"] for i in cut.ink if i["layer"] == "tag")
        assert panel[1] + panel[3] + (PANEL_PAD + 3 * PANEL_SIGMA) * cut.u <= tag[1], (panel, tag)


def test_title_kicker_keeps_small_text_contrast_over_a_light_picture(tmp_path):
    """The title's kicker is small accent type: over a light picture its scrim is raised from
    TITLE_SCRIM until the kicker keeps 4.5:1 (plan.json checks.scrims, '<title id> kicker'), and the
    rendered frame shows it."""
    import cv2 as cv

    from vantage.film.cut import TITLE_SCRIM

    root = tmp_path / "p"
    project = tiny(root, film={"width": 640, "height": 360})
    for date in DATES:  # a pale, sunlit picture: the accent kicker over it alone is far under 4.5:1
        img = np.full((SIZE[1], SIZE[0], 3), (214, 222, 226), np.uint8)
        cv.imwrite(str(root / "masters" / "site" / f"{date}.jpg"), img)
    plan = _plan(project)
    cut = _Cut(project, plan)
    title = next(s for s in plan.plan["shots"] if s["kind"] == "title")
    record = next(r for r in cut.scrims if r.get("text") == f"{title['id']} kicker")
    assert record["alpha"] > TITLE_SCRIM and record["contrast"] >= SMALL_CONTRAST, record
    kicker = cut.layers[title["id"]]["kicker"][-1]
    a = kicker.alpha[..., 0]
    m = (a > 0.02).astype(np.uint8)
    ring = cv.dilate(m, np.ones((9, 9), np.uint8)) & (1 - cv.dilate(m, np.ones((3, 3), np.uint8)))
    cy, cx = np.nonzero(a >= 0.9 * a.max())
    ry, rx = np.nonzero(ring)
    frame = cut.frame_at(title["f0"] + (title["f1"] - title["f0"]) // 2)
    lt = float(np.median(_lum(frame[cy + kicker.y, cx + kicker.x])))
    lb = float(np.quantile(_lum(frame[ry + kicker.y, rx + kicker.x]), 0.95))
    assert (max(lt, lb) + 0.05) / (min(lt, lb) + 0.05) >= SMALL_CONTRAST, (lt, lb)


def test_a_film_names_its_stats_by_fact(tmp_path):
    """films.<id>.stats may name the items to show by the facts their values print, in that order;
    an id no item prints is a warning. A count, or a list of more than six or with repeats, is
    validated like before."""
    from vantage.config import FilmSpec

    films = {"16x9": {"stats": ["area", "trees", "nowhere"]}}
    project = tiny(tmp_path / "p", chapters=[COMPARE, RUN], films=films, simulated=True)
    project = _facts(project.root, area=AREA, trees=TREES)
    plan = _plan(project)
    stats = [s for s in plan.plan["shots"] if s["kind"] == "stat"]
    assert [s["value"] for s in stats] == ["13,100", "78"]
    assert any("films.16x9.stats" in w and "nowhere" in w for w in plan.warnings), plan.warnings
    for bad in (7, ["a", "a"], [str(k) for k in range(7)]):
        with pytest.raises(ValidationError):
            FilmSpec(stats=bad)


def test_a_step_leaves_its_push_to_the_curtain_that_pushes_there(tmp_path):
    """A scrub's last step on the curtain's `after` picture does not push when the curtain right
    after it pushes to the same place: one push in, not in, out and in again."""
    steps = [STEPS[0], STEPS[1], {**STEPS[2], "focus": [0.5, 0.5, 1.4]}]
    compare = {**COMPARE, "steps": [{"text": "Half and half.", "split": 0.5, "focus": [0.51, 0.5, 1.4]}]}
    project = tiny(tmp_path / "p", steps=steps, chapters=[compare])
    p = _plan(project).plan
    last = next(s for s in p["shots"] if s["kind"] == "capture" and s["capture"] == DATES[4])
    assert not last["step"]["push"] and last["step"]["reason"] == "the curtain pushes it", last["step"]
    curtain = next(s for s in p["shots"] if s["kind"] == "curtain")
    assert curtain["steps"][0]["push"]
    far = {**COMPARE, "steps": [{"text": "Half and half.", "split": 0.5, "focus": [0.3, 0.5, 1.4]}]}
    project = tiny(tmp_path / "q", steps=steps, chapters=[far])
    last = next(
        s for s in _plan(project).plan["shots"] if s.get("capture") == DATES[4] and s["kind"] == "capture"
    )
    assert last["step"]["push"], last["step"]  # elsewhere: both push


def test_poster_comes_from_the_main_story_not_a_montage(tmp_path):
    """The poster is the main vantage's latest capture at rest, never a montage picture, even in the
    tiny 9:16 film whose montage cannot drift (its masters are at max_upscale at rest)."""
    project = tiny(
        tmp_path / "p", chapters=[FIGURE, GALLERY], extra={"water": list(WATER)}, film={"vertical": True}
    )
    p = _plan(_facts(project.root, area=AREA), "9x16").plan
    shot = next(s for s in p["shots"] if s["f0"] <= p["poster"] < s["f1"])
    assert shot["kind"] == "capture" and shot["vantage"] == "site" and not shot.get("montage"), shot


def test_the_disclaimer_is_read_and_rides_the_stats_when_the_card_is_full(tmp_path):
    """The brand's disclaimer says what is fictional: it is read at 17 characters a second, not
    glanced at like the copyright. When the end card would run over END_MAX_S, the honesty line and
    then the disclaimer ride under the stats, where both read in full; without stats the card keeps
    them and says it runs long."""
    from vantage.film.story import END_MAX_S
    from vantage.film.tracks import cue_frames

    disclaimer = "A demonstration: every place, person and figure in it is fictional."
    root = tmp_path / "p"
    tiny(root, chapters=[{**STATS, "items": [STATS["items"][2]]}], films={"16x9": {"end_beats": 2}})
    brand = yaml.safe_load((root / "brand" / "brand.yaml").read_text())
    brand |= {"disclaimer": disclaimer, "copyright": "© 2026 Tiny Brand"}
    (root / "brand" / "brand.yaml").write_text(yaml.safe_dump(brand))
    plan = _plan(_facts(root, area=AREA))
    p = plan.plan
    (end,) = (s for s in p["shots"] if s["kind"] == "end")
    (stat,) = (s for s in p["shots"] if s["kind"] == "stat")
    assert stat["honesty"] == f"{HONESTY['photo']} {disclaimer}", stat["honesty"]
    assert stat["f1"] - stat["land"] >= cue_frames(stat["honesty"], 12)
    assert [t["role"] for t in end["text"]] == ["brand", "credit", "role", "partner", "fine"]
    assert end["text"][-1]["text"] == "© 2026 Tiny Brand"
    assert end["f1"] - end["f0"] <= END_MAX_S * 12 and not [w for w in plan.warnings if "end card" in w]

    tiny(root, films={"16x9": {"end_beats": 2}})  # no stats: nothing to ride along with
    (root / "brand" / "brand.yaml").write_text(yaml.safe_dump(brand))
    plan = _plan(load_project(root))
    (end,) = (s for s in plan.plan["shots"] if s["kind"] == "end")
    roles = [t["role"] for t in end["text"]]
    assert "honesty" in roles and "disclaimer" in roles and not end["ride"], roles
    read = f"Made for testing. {HONESTY['photo']} {disclaimer}"
    assert end["f1"] - end["f0"] - end["dip"] - round(B / 3) - B >= cue_frames(read, 12)
    assert any("end card" in w and "over 6 s" in w for w in plan.warnings), plan.warnings
    # The partners are left out even though the card still runs long without them.
    assert "partner" not in roles and any("partners are left out" in w for w in plan.warnings), roles


def test_a_curtains_lines_share_one_size_and_land_with_their_picture(tmp_path):
    """Every line of a curtain is set at one size (`words_scale`, on at most two balanced lines in
    9:16 and one in 16:9), and lands on the beat nearest its sweep's midpoint, so it arrives with
    the picture it names; it stays until the next line lands (or the curtain ends), at least as
    long as it takes to read."""
    from vantage.film.tracks import cue_frames

    lines = ["The old lot, as it was left in the spring.", "Half.", "The new park in its first summer."]
    compare = {
        **COMPARE,
        "steps": [
            {"text": t, "split": sp, "film": {"line": t}}
            for t, sp in zip(lines, (0.8, 0.5, 0.2), strict=True)
        ],
    }
    project = tiny(tmp_path / "p", chapters=[compare], film={"vertical": True})
    for fid in ("16x9", "9x16"):
        plan = _plan(project, fid)
        p = plan.plan
        curtain = next(s for s in p["shots"] if s["kind"] == "curtain")
        steps, keys = curtain["steps"], curtain["keys"]
        third = round(B / 3)
        for k, step in enumerate(steps):
            sweep = (keys[k]["b"], keys[k + 1]["a"])  # from the last key's hold to this split
            assert step["land"] % B == 0 and sweep[0] < step["land"] < sweep[1], (fid, k, step, sweep)
            end = steps[k + 1]["land"] - third if k + 1 < len(steps) else curtain["f1"]
            assert end - (step["land"] + third) >= cue_frames(step["words"], 12), (fid, k)
        scale = curtain["words_scale"]
        assert 0 < scale <= 1.0
        cut = _Cut(project, plan)
        style = dataclasses.replace(CURTAIN_LABEL, size=CURTAIN_LABEL.size * curtain["label_scale"])
        cap_top = curtain["label_top"] + cut._cap(style)
        boxes = [
            (x, y, x + w, y + h)  # ink boxes are recorded as x, y, w, h
            for x, y, w, h in (
                i["box"] for i in cut.ink if i["shot"] == curtain["id"] and i["layer"] == "words"
            )
        ]
        rise = RISE * cut.u
        for step, box in zip(steps, boxes, strict=True):
            # The renderer set every line at the curtain's one size, on at most two lines between
            # the margins (before: each line shrunk on its own to fit one line).
            ws = words_set(cut.type, cut.lay, step["words"], step["words_zone"], cap_top, scale)
            assert ws.style.size == pytest.approx(120 * scale) and 1 <= len(ws.lines) <= 2, ws
            assert fid == "9x16" or len(ws.lines) == 1, ws  # landscape: one line, clear of the middle
            assert box == pytest.approx([*ws.box[:3], ws.box[3] + rise], abs=2), (box, ws.box)
            assert cut.lay.margin - 2 <= box[0] and box[2] <= cut.lay.right + 2, box
        # The short line is not set larger than the long ones: its ink is no taller than one row.
        assert boxes[1][3] - boxes[1][1] - rise <= 120 * scale * cut.u, boxes


def test_a_still_capture_does_not_warn_about_a_push_it_does_not_make(tmp_path):
    """With two captures the first is held still (the curtain shows both): a push that would have
    been clamped and soft is not made, so neither is reported."""
    steps = [{**STEPS[0], "focus": [0.62, 0.5, 1.8]}]
    excluded = [{"date": d, "exclude": True} for d in DATES[2:]]
    project = tiny(
        tmp_path / "p", steps=steps, captures=excluded, film={"vertical": True},
        films={"9x16": {"max_upscale": 2.4}},
    )  # fmt: skip
    plan = _plan(project, "9x16")
    step = next(s["step"] for s in plan.plan["shots"] if s.get("step"))
    assert step["push"] is False and step["reason"] == "two captures", step
    assert not [w for w in plan.warnings if "soft push" in w or "clamped" in w], plan.warnings


def test_a_portrait_end_card_is_set_larger_inside_the_safe_zone(tmp_path):
    """A 9:16 film is read on a phone in the hand: its end card is set END_PORTRAIT larger (in units
    of its frame) than the 16:9 card, and still fits between the safe top and bottom."""
    from vantage.film.cut import END_PORTRAIT

    project = tiny(tmp_path / "p", film={"vertical": True})
    heights = {}
    for fid in ("16x9", "9x16"):
        plan = _plan(project, fid)
        cut = _Cut(project, plan)
        (end,) = (s for s in plan.plan["shots"] if s["kind"] == "end")
        x, y, w, h = next(i["box"] for i in cut.ink if i["shot"] == end["id"] and i["layer"] == "stack")
        assert cut.lay.top - 1 <= y and y + h <= cut.lay.bottom + 1, (fid, y, h)
        assert cut.lay.margin - 1 <= x and x + w <= cut.lay.right + 1, (fid, x, w)
        heights[fid] = h / cut.u
    assert heights["9x16"] >= 0.95 * END_PORTRAIT * heights["16x9"], heights
