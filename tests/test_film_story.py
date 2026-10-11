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
            *(chapters or []),
        ],
    }
    if captures is not None:
        story["captures"] = captures
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


def test_short_end_card_is_refused(tmp_path):
    """end_beats 2 after a paper stat: the 18-frame dip from paper would leave the end card's text
    under a beat (B = 8), so the plan is refused rather than ending mid-dip; transcript lines never
    start past the film's last frame."""
    root = tmp_path / "p"
    tiny(root, chapters=[{**STATS, "items": [STATS["items"][2]]}], films={"16x9": {"end_beats": 2}})
    project = _facts(root, area={"text": "13,100", "status": "verified", "sources": ["plan"]})
    with pytest.raises(PlanError, match="E1: a 18-frame dip leaves under a beat"):
        _plan(project)
    tiny(root, chapters=[{**STATS, "items": [STATS["items"][2]]}], films={"16x9": {"end_beats": 4}})
    plan = _plan(_facts(root, area={"text": "13,100", "status": "verified", "sources": ["plan"]}))
    p = plan.plan
    assert all(line["f0"] < p["frames"] for line in p["transcript"]["lines"])
    assert p["captions"][-1]["text"] == "Made for testing."  # the credit cue survives


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
    project = tiny(tmp_path / "p", chapters=[COMPARE], simulated=True)
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
    tiny(root, chapters=[STATS])
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
    assert stat["facts"] == ["area"] and stat["surface"] == "paper" and stat["f1"] - stat["f0"] == 4 * B
    assert any("'42' is not a {fact:} token" in w for w in plan.warnings), plan.warnings
    assert any("stat 1" in w and "do-not-print" in w for w in plan.warnings), plan.warnings
    assert "secret" not in plan.facts
    # The renderer sets one layer per glyph of the plan's value: it never draws another number.
    cut = _Cut(project, plan)
    assert len(cut.layers[stat["id"]]["glyphs"]) == len("13,100")
    first = cut.frame_at(stat["f0"] + 1).astype(np.float32)
    landed = cut.frame_at(stat["key"]).astype(np.float32)
    assert np.abs(first - landed).mean() > 1  # the figure slides in, then holds
    # No more than `stats` items.
    many = {**STATS, "items": [STATS["items"][2]] * 5}
    project = tiny(root, chapters=[many], films={"16x9": {"stats": 2}})
    project = _facts(root, area={"text": "13,100", "status": "verified"})
    assert sum(s["kind"] == "stat" for s in _plan(project).plan["shots"]) == 2


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
    steps = [STEPS[0], {**STEPS[1], "focus": [0.62, 0.54, 1.6]}]
    hotspots = [{"id": "tower", "x": 0.93, "y": 0.584, "label": "The tower", "to": DATES[1]}]
    project = tiny(tmp_path / "p", steps=steps, hotspots=hotspots, film={"vertical": True})
    for fid in ("16x9", "9x16"):
        p = _plan(project, fid).plan
        step = next(s["step"] for s in p["shots"] if s.get("step") and s["step"]["focus_requested"])
        assert step["target"] == "focus+subject:tower" and step["subjects"] == ["tower"], fid
        x, y, z = step["focus"]
        geo = Geometry(p["width"], p["height"], 320, 200)
        cx, cy = geo.centre(x, y, z)
        hw, hh = geo.half(z)
        assert cx - hw <= 0.93 <= cx + hw and cy - hh <= 0.584 <= cy + hh, (fid, cx, cy, hw, hh)


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


@pytest.mark.slow
def test_demo_story_cut(tmp_path):
    """The fictional demo end to end (fast footage → masters → plans): the 16:9 story cut is 62 beats,
    the Demolition push keeps the slide tower in frame in both cuts, and the stats print exactly the
    three figures the cut chooses."""
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
        assert p["cut"] == "story" and p["grid"]["beats"] == {"16x9": 62, "9x16": 60}[fid], (fid, p["grid"])
        check_plan(p)
        shot = next(s for s in p["shots"] if s.get("step") and s["step"]["words"] == "Demolition.")
        step = shot["step"]
        assert step["push"] and step["target"] == "focus+subject:slide-tower", (fid, step)
        mv = MastersIndex.load(root / "masters" / "index.json").vantages["overview"]
        geo = Geometry(p["width"], p["height"], mv.width, mv.height)
        cx, cy = geo.centre(*step["focus"])
        hw, hh = geo.half(step["focus"][2])
        assert cx - hw <= tower.x <= cx + hw and cy - hh <= tower.y <= cy + hh, fid
        stats = [s for s in p["shots"] if s["kind"] == "stat"]
        assert [f for s in stats for f in s["facts"]] == ["flights", "stalls-to-meadow", "wave-pool-area"]
        assert stats[2]["value"] == "13,100"
