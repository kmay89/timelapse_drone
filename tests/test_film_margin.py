"""The flash margin (film/margin.py): the planner meters a small proxy of each story film and holds,
softens or slows what lies between too many flash transitions, so every film stays at most
`output.film.flash_limit` (default 2) transitions per region in any second; the render meter still
refuses more than 3 and warns above the limit.

The flashing project is the tiny story project at 144 bpm (B = D = 5 frames at 12 fps) with no steps,
over masters that alternate near-black and near-white: every capture is a one-beat quiet capture,
so three dissolves (each a transition in every region) fall within a second unless they are held.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from test_film import SIZE
from test_film_story import DATES, tiny
from vantage.config import FilmSettings, Project, load_project
from vantage.film import margin
from vantage.film.plan import plan_films
from vantage.film.render import render_film
from vantage.film.safety import check_plan
from vantage.models import FilmPlan


def _flashing(root: Path, **film: Any) -> Project:
    tiny(root, steps=[], film={"bpm": 144, **film})
    for i, date in enumerate(DATES):
        level = 20 if i % 2 == 0 else 235
        img = np.full((SIZE[1], SIZE[0], 3), level, np.uint8)
        cv2.imwrite(str(root / "masters" / "site" / f"{date}.jpg"), img)
    return load_project(root)


def test_flash_limit_setting():
    assert FilmSettings().flash_limit == 2
    assert FilmSettings(flash_limit=3).flash_limit == 3
    for bad in (0, 4):
        with pytest.raises(ValidationError):
            FilmSettings(flash_limit=bad)


def test_margin_holds_quiet_captures_until_the_film_keeps_its_limit(tmp_path):
    """One-beat captures alternating black and white flash three times a second: the plan holds
    quiet captures a beat longer (on the grid) and records it; the rendered film measures at most 2
    transitions per region in any second. With flash_limit 3 nothing is held, and it measures 3."""
    project = _flashing(tmp_path / "p")
    plan = plan_films(project, only=["16x9"], fast=False)["16x9"]
    p = plan.plan
    check_plan(p)
    flash = p["flash"]
    assert flash["limit"] == 2 and flash["estimate"] <= 2, flash
    assert flash["held"] and set(flash["held"]) <= {"S02", "S03", "S04", "S05"}, flash
    assert not flash["slowed"] and not flash["softened"]
    B = p["grid"]["fpb"]
    caps = [s for s in p["shots"] if s["kind"] == "capture"]
    for s in caps:  # every shot still starts and ends on the beat grid
        assert s["f0"] % B == 0 and s["f1"] % B == 0
        assert s["f1"] - s["f0"] == B * (1 + flash["held"].get(s["id"], 0)), (s["id"], flash)
    assert not [w for w in plan.warnings if "flash margin" in w], plan.warnings

    out = tmp_path / "out"
    render_film(project, out, only=["16x9"])
    rendered = FilmPlan.load(out / "tiny-site-16x9.plan.json")
    assert rendered.render is not None
    measured = rendered.render["flash"]
    assert measured["pass"] and measured["max_transitions_per_s"] <= 2 and measured["limit"] == 2, measured
    assert not [w for w in rendered.warnings if "measured" in w]

    loose = _flashing(tmp_path / "q", flash_limit=3)
    p3 = plan_films(loose, only=["16x9"], fast=False)["16x9"].plan
    assert p3["flash"]["held"] == {} and p3["flash"]["estimate"] == 3 and p3["frames"] < p["frames"]
    render_film(loose, tmp_path / "out3", only=["16x9"])
    measured = FilmPlan.load(tmp_path / "out3" / "tiny-site-16x9.plan.json").render["flash"]  # type: ignore[index]
    assert measured["pass"] and measured["max_transitions_per_s"] == 3 and measured["limit"] == 3


def test_margin_plans_are_reproducible_and_fast_previews_match(tmp_path):
    """The margin is part of the plan: the same inputs give the same plan hash, and a VANTAGE_FAST
    plan (a smaller film) gets the same holds and beats, since both meter the same proxy."""
    project = _flashing(tmp_path / "p")
    a = plan_films(project, only=["16x9"], fast=False)["16x9"]
    margin._MEMO.clear()
    margin._DECODED.clear()
    b = plan_films(project, only=["16x9"], fast=False)["16x9"]
    fast = plan_films(project, only=["16x9"], fast=True)["16x9"]
    assert a.hash == b.hash
    assert fast.plan["flash"] == a.plan["flash"]
    assert [(s["id"], s["f0"], s["f1"]) for s in fast.plan["shots"]] == [
        (s["id"], s["f0"], s["f1"]) for s in a.plan["shots"]
    ]


def _plan(
    shots: list[dict[str, Any]], camera: list[dict[str, Any]] | None = None, fps: int = 30
) -> dict[str, Any]:
    return {"fps": fps, "grid": {"fpb": 18}, "shots": shots, "camera": camera or []}


def _events(frames: list[int], region: int = 0) -> list[margin.Event]:
    return [(f, "luma", region) for f in frames]


def test_fixes_hold_the_quiet_capture_between_dissolves():
    """Three dissolves within a second (40 frames here): a beat more on a quiet capture between
    them (the later of two that split them as evenly), and the shifted transitions keep the limit."""
    shots = [
        {"id": "S01", "kind": "capture", "f0": 0, "f1": 18, "step": None},
        {"id": "S02", "kind": "capture", "f0": 18, "f1": 36, "step": None},
        {"id": "S03", "kind": "capture", "f0": 36, "f1": 90, "step": {"land": 54}},
    ]
    events = _events([4, 22, 40])
    assert margin.violation(events, 40, 2) == [4, 22, 40]
    fixes, added = margin.fixes(events, _plan(shots, fps=40), 2, margin.Fixes())
    assert added and fixes.holds == {"S02": 1} and not fixes.slows and not fixes.caps
    assert margin.violation(_events([4, 22, 58]), 40, 2) is None


def test_fixes_soften_a_push_that_flashes_on_its_own_then_slow_it():
    """Three transitions all inside one push: the push is softened (its log zoom scaled by 2/3 and
    0.9), never below SOFTEST; a push already at or under SOFTEST is slowed instead, a beat at a time until the
    stretched transitions keep the limit."""
    shots = [
        {"id": "S01", "kind": "capture", "f0": 0, "f1": 72,
         "step": {"land": 18, "push_f0": 18, "push_f1": 42, "focus": [0.5, 0.5, 1.8]}},
    ]  # fmt: skip
    camera = [
        {"a": 0, "b": 18, "x": 0.5, "y": 0.5, "zoom": 1.0},
        {"a": 42, "b": 72, "x": 0.5, "y": 0.5, "zoom": 1.8},
    ]
    events = _events([24, 30, 36])
    fixes, added = margin.fixes(events, _plan(shots, camera), 2, margin.Fixes())
    assert added and fixes.caps == {"S01": 0.6} and not fixes.holds and not fixes.slows  # 2/3 · 0.9
    camera[1]["zoom"] = 1.2  # a gentle push already: nothing to soften
    fixes, added = margin.fixes(events, _plan(shots, camera), 2, margin.Fixes())
    assert added and fixes.slows == {"S01": 2} and not fixes.caps


def test_fixes_give_up_when_nothing_lies_between():
    """Transitions inside one dissolve-free stretch with no slot and no move: nothing to lengthen."""
    shots = [{"id": "E1", "kind": "end", "f0": 0, "f1": 90}]
    fixes, added = margin.fixes(_events([10, 15, 20]), _plan(shots), 2, margin.Fixes())
    assert not added and fixes == margin.Fixes()


def test_proxy_size_matches_what_the_meter_measures():
    assert margin.proxy_size(1920, 1080) == ((480, 270), (240, 135))
    assert margin.proxy_size(1080, 1920) == ((270, 480), (135, 240))
    assert margin.proxy_size(320, 180) == ((320, 180), (40, 22))
    assert margin.proxy_size(64, 36) == ((64, 36), (16, 16))
