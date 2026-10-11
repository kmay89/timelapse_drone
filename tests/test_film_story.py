"""The story cut: beat grid, runtime camera ports, derived shots, words and facts, zoom clamps, the
honesty line, flash safety and reproducible renders, on a tiny synthetic project.

The tiny project films one scrub chapter of five captures at 12 fps and 90 bpm (B = 8, D = 5); each
master is two-toned (a light band on top, a hue unique to its capture below), so the hue at the
frame centre tells which capture is on screen.
"""

from __future__ import annotations

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
from vantage.film.cut import _Cut
from vantage.film.plan import SIMULATED_NOTE, plan_films, resolve_films
from vantage.film.render import _render_one, render_film
from vantage.film.safety import LumaMeter, PlanError, check_plan
from vantage.film.story import HONESTY
from vantage.media.ffmpeg import ffmpeg_bin
from vantage.models import FilmPlan

DATES = ("2025-03-01", "2025-05-01", "2025-07-01", "2025-09-01", "2025-11-01")
HUES = ((200, 90, 40), (60, 170, 60), (40, 80, 210), (40, 190, 200), (170, 60, 170))  # BGR
STORY_FILM = {**FILM, "cut": "story", "bpm": 90, "vertical": False}
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
    **config: Any,
) -> Project:
    story: dict[str, Any] = {
        "vantages": [{"id": "overview", "name": "Overview"}, {"id": "site", "name": "The site"}],
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
        ],
    }
    if films is not None:
        story["films"] = films
    make_project(
        root, film={**STORY_FILM, **(film or {})}, story=story, extra={"site": list(DATES)}, **config
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
    with pytest.raises(ValueError, match="not supported yet"):
        plan_films(project, only=["screen"])


@pytest.mark.parametrize(
    ("films", "match"),
    [
        ({"Big": {}}, "film id"),
        ({"teaser-web": {}}, "not end in -web"),
        ({"x": {"sound": "file"}}, "not supported yet"),
        ({"x": {"bpm": 200}}, "less than or equal"),
        ({"x": {"safe": [0.1, 0.1]}}, "safe must be"),
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
    assert caps[0]["step"]["land"] == caps[0]["f0"]  # beat 0 on the film's first image
    assert caps[2]["step"]["land"] == caps[2]["f0"] + B
    assert shots[0]["capture"] == DATES[0] and shots[0]["file"] == caps[0]["file"]
    assert [s["rail"] for s in caps] == [0, 1, 2, 3, 4]
    assert plan["frames"] == shots[-1]["f1"] and plan["camera"][-1]["b"] == plan["frames"]
    # The push holds to the end of its shot and eases back during the next dissolve.
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


def test_honesty_line(tmp_path):
    def end_text(project: Project) -> str:
        end = _plan(project).plan["shots"][-1]
        assert end["kind"] == "end"
        return "\n".join(t["text"] for t in end["text"])

    simulated = end_text(tiny(tmp_path / "a", simulated=True))
    assert HONESTY["simulated"] in simulated and SIMULATED_NOTE in simulated
    assert "real photograph" not in simulated.lower() and "real aerial" not in simulated.lower()
    photo = end_text(tiny(tmp_path / "b"))
    assert HONESTY["photo"] in photo and SIMULATED_NOTE not in photo


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
        "tiny-site-16x9.jpg", "tiny-site-16x9.mp4", "tiny-site-16x9.plan.json", "tiny-site-16x9.storyboard.jpg",
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
