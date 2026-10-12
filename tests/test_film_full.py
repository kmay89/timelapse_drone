"""The demo's films at full size, rendered once per session: what CI's small films cannot show (their
masters clamp every push to about 1.03×). Slow: synthesises the full-size demo footage (cached in
pytest's cache between sessions, or VANTAGE_TEST_FOOTAGE), aligns it into masters and renders the
9:16, 16:9 and screen films side by side; every assertion below reads those same renders.

For each film: the meter measures at most 2 flash transitions per region in any second (the
planner's margin, film/margin.py; WCAG 2.3.1 allows 3); every step that asks for a push really
pushes (zoom ≥ 1.2) and its subject ends inside the frame and outside its words' ink; every frame
of a stat from its words' landing to its text's exit shows its words and, once in, its figure;
every measured scrim (the tag, date labels, title kickers, stat panels) keeps the contrast its text
asks, and the tag, date labels and stat text keep 4.5:1 in frames decoded from the encoded MP4; the film spends at most 6 s on night after its last picture; and its length is within the
target (both cuts 40–48 s, the screen loop at most 80 s). CI runs this module on pull requests that
touch the films (.github/workflows/films.yml).
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
from vantage.film.cut import SMALL_CONTRAST, _Cut
from vantage.film.plan import plan_films
from vantage.film.render import render_film
from vantage.film.story import Geometry
from vantage.models import FilmPlan, MastersIndex

pytestmark = pytest.mark.slow

FILMS = ("9x16", "16x9", "screen")
LENGTH = {"9x16": (40.0, 48.0), "16x9": (40.0, 48.0), "screen": (40.0, 80.0)}
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
            if step.get("reason") == "the curtain pushes it":  # the curtain right after makes the push
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
    # The scrub's "Plan" and "Demolition" (out of reach in 9:16) and the curtain's beach, which also
    # makes the push of the step before it (`the curtain pushes it`).
    assert len(pushes) >= {"9x16": 2}.get(fid, 3), (fid, pushes)


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


def _decoded(mp4: Path, frames: list[int], width: int, height: int) -> dict[int, np.ndarray]:
    """Frames `frames` of an encoded film, decoded to BGR as a player shows them (BT.709, TV range)."""
    import subprocess

    from vantage.media.ffmpeg import ffmpeg_bin

    order = sorted(set(frames))
    pick = "+".join(f"eq(n\\,{f})" for f in order)
    out = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(mp4), "-vf",
         f"select={pick},scale=in_color_matrix=bt709:in_range=tv,format=bgr24",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    size = width * height * 3
    assert len(out) == size * len(order), (mp4, len(out), order)
    return {
        f: np.frombuffer(out[k * size : (k + 1) * size], np.uint8).reshape(height, width, 3)
        for k, f in enumerate(order)
    }


def _ink_contrast(frame: np.ndarray, layers: list[Any]) -> float:
    """The contrast of text layers as a reader meets them in a decoded frame (as film/cut.py measures
    a scrim): the median luminance of the glyph cores against the worst 5 % of the pixels 1 to 4 px
    around the glyphs (the brightest for light text, the darkest for dark text)."""
    import cv2

    from test_film_story import _lum

    h, w = frame.shape[:2]
    alpha = np.zeros((h, w), np.float32)
    for layer in layers:
        a = layer.alpha[..., 0]
        y0, x0 = max(layer.y, 0), max(layer.x, 0)
        y1, x1 = min(layer.y + a.shape[0], h), min(layer.x + a.shape[1], w)
        alpha[y0:y1, x0:x1] = np.maximum(
            alpha[y0:y1, x0:x1], a[y0 - layer.y : y1 - layer.y, x0 - layer.x : x1 - layer.x]
        )
    near = (alpha > 0.02).astype(np.uint8)
    ring = (
        cv2.dilate(near, np.ones((9, 9), np.uint8)) & (1 - cv2.dilate(near, np.ones((3, 3), np.uint8)))
    ) > 0
    core = alpha >= 0.9 * alpha.max()
    lt = float(np.median(_lum(frame[core])))
    around = _lum(frame[ring])
    lb = float(np.quantile(around, 0.95 if lt > float(np.median(around)) else 0.05))
    return (max(lt, lb) + 0.05) / (min(lt, lb) + 0.05)


@pytest.mark.parametrize("fid", FILMS)
def test_full_size_text_keeps_its_contrast_in_the_encoded_film(full, fid):
    """WCAG 1.4.3 sampled from the encoded film, not the plan's own measurement: in decoded frames
    of the MP4, the tag (on every capture's key frame), each date label (on its key frame) and every
    stat's figure, label and source (once its text is fully in, and on its last frame before it
    leaves) keep 4.5:1 against what is around their glyphs."""
    project, out = full
    plan = _plan(full, fid)
    p = plan.plan
    cut = _Cut(project, plan)
    B, third = p["grid"]["fpb"], round(p["grid"]["fpb"] / 3)
    checks: list[tuple[int, str, list[Any]]] = []
    shots = p["shots"]
    for k, shot in enumerate(shots):
        if shot["kind"] == "capture":
            d = shot.get("dissolve")
            f = max(shot["key"], shot["f0"] + B // 2, d["f1"] if d else 0)
            if f < shot["f1"]:
                checks.append((f, f"{shot['id']} label", [cut.layers[shot["id"]]["label"]]))
                if cut.tag is not None:
                    checks.append((f, f"{shot['id']} tag", [cut.tag]))
        elif shot["kind"] == "stat":
            nxt = shots[k + 1] if k + 1 < len(shots) else None
            last = shot["f1"] - (third if nxt is not None and nxt["kind"] == "stat" else 0) - 1
            layers = cut.layers[shot["id"]]
            for f in (max(shot["key"], shot["text_in"] + third, shot["land"] + B // 2), last):
                checks.append((f, f"{shot['id']} figure", list(layers["glyphs"])))
                checks.append((f, f"{shot['id']} label and source", list(layers["rest"])))
    assert any("label and source" in what for _, what, _ in checks), fid
    frames = _decoded(out / f"{project.slug}-{fid}.mp4", [f for f, _, _ in checks], cut.width, cut.height)
    low = [
        (what, f, round(r, 2))
        for f, what, layers in checks
        if (r := _ink_contrast(frames[f], layers)) < SMALL_CONTRAST
    ]
    assert not low, (fid, low)


@pytest.mark.parametrize("fid", FILMS)
def test_full_size_scrims_keep_their_contrast(full, fid):
    """Every scrim the cut measured keeps its text's contrast over the full-size pictures it crosses
    (WCAG 1.4.3): the tag, each date label and title kicker at 4.5:1, a stat panel at its target."""
    plan = _plan(full, fid)
    scrims = plan.checks["scrims"]
    texts = {r.get("text") for r in scrims}
    assert "tag" in texts and any(" label" in str(t) for t in texts), (fid, texts)
    for record in scrims:
        target = record.get("target", SMALL_CONTRAST)
        assert record["contrast"] >= target, (fid, record)


def test_full_size_default_films_keep_the_length_budget(full, tmp_path):
    """The demo on the engine's defaults (no per-film stats or montage) still makes cuts of at most
    48 s: the length budget (`max_s`) trims montage pictures, then stats, and says what it left out.
    And a stat run really pushes at full size: the demo's own films set their run after the
    shoreline montage, whose picture has no hotspots, so its figures only drift; on the defaults the
    16:9 cut's second figure (the meadow's parking spaces, over the overview) pushes to its hotspot,
    which ends in frame and clear of the run's ink."""
    import yaml

    from vantage.film.cut import stat_run

    project, _ = full
    root = tmp_path / "defaults"
    root.mkdir()
    for name in ("project.yaml", "facts.yaml"):
        shutil.copy(project.root / name, root / name)
    shutil.copytree(project.root / "brand", root / "brand")
    (root / "masters").symlink_to(project.masters_dir, target_is_directory=True)
    story = yaml.safe_load((project.root / "story.yaml").read_text(encoding="utf-8"))
    story.pop("films", None)
    (root / "story.yaml").write_text(yaml.safe_dump(story, sort_keys=False), encoding="utf-8")
    plans = plan_films(load_project(root), only=["9x16", "16x9"], fast=False)
    for fid, plan in plans.items():
        seconds = plan.plan["frames"] / plan.plan["fps"]
        assert seconds <= 48.0, (fid, seconds)
        assert [w for w in plan.warnings if "max_s" in w], (fid, plan.warnings)
    defaults = load_project(root)
    plan = plans["16x9"]
    p = plan.plan
    stats = [s for s in p["shots"] if s["kind"] == "stat"]
    pushed = [s for s in stats if s["move"]["how"].startswith("focus+subject")]
    assert pushed, [s["move"] for s in stats]
    cut = _Cut(defaults, plan)
    run = stat_run(cut.type, cut.lay, stats, stats[0]["honesty"], stats[0]["place"], True)
    index = MastersIndex.load(defaults.masters_dir / "index.json")
    for stat in pushed:
        assert stat["move"]["focus"][2] >= 1.2, stat["move"]
        mv = index.vantages[stat["vantage"]]
        geo = Geometry(p["width"], p["height"], mv.width, mv.height)
        hid = stat["move"]["subjects"][0]
        spot = next(h for ch in defaults.story.chapters for h in getattr(ch, "hotspots", []) if h.id == hid)
        u, v = _on_screen(geo, stat["move"]["focus"], spot.x, spot.y)
        assert 0 <= u <= p["width"] and 0 <= v <= p["height"], (stat["id"], hid, u, v)
        x0, y0, x1, y1 = run.box
        assert not (x0 <= u <= x1 and y0 <= v <= y1), (stat["id"], hid, (u, v), run.box)
