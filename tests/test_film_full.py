"""The demo's films at full size, rendered once per session: what CI's small films cannot show (their
masters clamp every push to about 1.03×). Slow: synthesises the full-size demo footage (cached in
pytest's cache between sessions, or VANTAGE_TEST_FOOTAGE), aligns it into masters and renders the
9:16, 16:9 and screen films side by side; every assertion below reads those same renders.

For each film: the meter measures at most 2 flash transitions per region in any second (the
planner's margin, film/margin.py; WCAG 2.3.1 allows 3); every step that asks for a push really
pushes (zoom ≥ 1.2) and its subject ends inside the frame and outside its words' ink; every frame
of a stat from its words' landing to its text's exit shows its words and, once in, its figure; the film spends at most 6 s on
night after its last picture; and its length is within the target (9:16 40–48 s, 16:9 40–54 s with
every chapter, the screen loop at most 80 s).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from test_film_story import _cores, _NoText
from vantage.config import Project, load_project
from vantage.film.cut import _Cut
from vantage.film.render import render_film
from vantage.film.story import Geometry
from vantage.models import FilmPlan, MastersIndex

pytestmark = pytest.mark.slow

FILMS = ("9x16", "16x9", "screen")
LENGTH = {"9x16": (40.0, 48.0), "16x9": (40.0, 54.0), "screen": (40.0, 80.0)}
TAIL_S = 6.0  # seconds on night after the last picture (the end card, its dip, a loop's dip)


@pytest.fixture(scope="module")
def full(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, Path]:
    """The full-size demo project (its own masters) and the folder its three films were rendered to."""
    from vantage.demo import synth
    from vantage.ingest.catalog import build_catalog
    from vantage.paths import repo_root
    from vantage.process.masters import make_masters
    from vantage.process.select import select_frames

    demo = repo_root() / "projects" / "demo-lakeside"
    cached = os.environ.get("VANTAGE_TEST_FOOTAGE")
    footage = Path(cached) if cached else request.config.cache.mkdir("vantage-demo-footage-full")
    synth.generate_demo_footage(footage, fast=False)  # reused while the generator is unchanged
    root = tmp_path_factory.mktemp("full") / "demo"
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
    out = root / "film"
    render_film(project, out, only=list(FILMS))
    return project, out


def _plan(full: tuple[Project, Path], fid: str) -> FilmPlan:
    project, out = full
    return FilmPlan.load(out / f"{project.slug}-{fid}.plan.json")


@pytest.mark.parametrize("fid", FILMS)
def test_full_size_flashes_keep_the_margin(full, fid):
    plan = _plan(full, fid)
    assert plan.render is not None
    measured = plan.render["flash"]
    assert measured["pass"] and measured["max_transitions_per_s"] <= 2, (fid, measured, plan.plan["flash"])
    assert plan.checks["flash"]["estimate"] <= 2, plan.checks["flash"]
    assert not [w for w in plan.warnings if "flash" in w], plan.warnings


def _words_box(plan: FilmPlan, sid: str, k: int = 0) -> list[int]:
    boxes = [i["box"] for i in plan.checks["ink"] if i["shot"] == sid and i["layer"] == "words"]
    return boxes[k]


def _on_screen(geo: Geometry, cam: list[float], x: float, y: float) -> tuple[float, float]:
    """Where master point (x, y) lands, in output px, at camera (x, y, zoom)."""
    cx, cy = geo.centre(*cam)
    hw, hh = geo.half(cam[2])
    return (x - (cx - hw)) / (2 * hw) * geo.width, (y - (cy - hh)) / (2 * hh) * geo.height


def _subject(project: Project, step: dict[str, Any]) -> tuple[str, float, float]:
    """The hotspot a push frames, else (a step without subjects, a curtain step) its focus point."""
    target = step.get("target") or ""
    if target.startswith("focus+subject:"):
        hid = target.split(":", 1)[1].split(",")[0]
        spot = next(h for ch in project.story.chapters for h in getattr(ch, "hotspots", []) if h.id == hid)
        return hid, spot.x, spot.y
    assert not step.get("subjects"), step  # a named subject is framed, or the step does not push
    return "focus", step["focus_requested"][0], step["focus_requested"][1]


@pytest.mark.parametrize("fid", FILMS)
def test_full_size_steps_really_push_onto_their_subject(full, fid):
    """Every step asking for a push pushes to at least 1.2× and ends with its subject (the hotspot
    it frames, else its focus point) inside the frame and clear of its words' ink."""
    project, _ = full
    plan = _plan(full, fid)
    p = plan.plan
    index = MastersIndex.load(project.masters_dir / "index.json")
    pushes = []
    for shot in p["shots"]:
        steps = [shot["step"]] if shot["kind"] == "capture" and shot.get("step") else []
        if shot["kind"] == "curtain":
            steps = shot["steps"]
        worded = 0
        for step in steps:
            k = worded
            worded += bool(step.get("words"))
            if not step.get("focus_requested"):
                continue
            if step.get("reason") == "subject off-frame":  # this aspect cannot frame what it names
                assert not step["push"] and "push_f0" not in step, (fid, shot["id"], step)
                continue
            pushes.append(shot["id"])
            assert step["push"] and step["focus"][2] >= 1.2, (fid, shot["id"], step)
            mv = index.vantages[shot["vantage"]]
            geo = Geometry(p["width"], p["height"], mv.width, mv.height)
            name, x, y = _subject(project, step)
            u, v = _on_screen(geo, step["focus"], x, y)
            assert 0 <= u <= p["width"] and 0 <= v <= p["height"], (fid, shot["id"], name, u, v)
            if step.get("words"):
                bx, by, bw, bh = _words_box(plan, shot["id"], k if shot["kind"] == "curtain" else 0)
                assert not (bx <= u <= bx + bw and by <= v <= by + bh), (fid, shot["id"], name, (u, v))
    assert len(pushes) >= 4, (fid, pushes)


@pytest.mark.parametrize("fid", FILMS)
def test_full_size_stats_always_show_their_ink(full, fid):
    """Every third frame of every stat, from its text's landing (and fade in, B/3) to its end, shows
    its words; from the moment its figure is fully in, its figure too (rendered with and without
    the text)."""
    project, _ = full
    plan = _plan(full, fid)
    p = plan.plan
    stats = [s for s in p["shots"] if s["kind"] == "stat"]
    assert stats, fid
    cut = _Cut(project, plan)
    w, h, B = cut.width, cut.height, p["grid"]["fpb"]
    for stat in stats:
        layers = cut.layers[stat["id"]]
        words = _cores(layers["rest"], w, h)
        figure = _cores(layers["glyphs"], w, h)
        fully = stat["land"] + round(B / 2)  # the figure slides in as one block
        nxt = p["shots"][p["shots"].index(stat) + 1]
        end = stat["f1"] - (round(B / 3) if nxt["kind"] == "stat" else 0)  # its text leaves first
        for f in range(stat["text_in"] + round(B / 3), end, 3):  # once its fade in is over
            frame = cut.frame_at(f).astype(np.int16)
            with _NoText(cut):
                bare = cut.frame_at(f).astype(np.int16)
            assert np.abs(frame[words] - bare[words]).max(axis=1).mean() > 12, (fid, stat["id"], f)
            if f >= fully:
                assert np.abs(frame[figure] - bare[figure]).max(axis=1).mean() > 12, (fid, stat["id"], f)


@pytest.mark.parametrize("fid", FILMS)
def test_full_size_length_and_tail(full, fid):
    plan = _plan(full, fid)
    p = plan.plan
    seconds = p["frames"] / p["fps"]
    lo, hi = LENGTH[fid]
    assert lo <= seconds <= hi, (fid, seconds)
    end = next(s for s in p["shots"] if s["kind"] == "end")
    assert (p["frames"] - end["f0"]) / p["fps"] <= TAIL_S, (fid, end, p["frames"])
    images = [s for s in p["shots"] if s["kind"] in ("title", "capture", "curtain", "stat")]
    assert images[-1]["f1"] == end["f0"], fid  # the last picture runs into the end card's dip
