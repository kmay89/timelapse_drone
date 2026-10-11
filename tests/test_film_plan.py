"""Film plans (the Reel Ledger): deterministic hashes, the classic timeline, render parity, storyboards."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image

from test_film import FILM, make_project
from vantage.config import Project, load_project
from vantage.film import storyboard
from vantage.film.plan import plan_films
from vantage.film.render import _render_one, render_film
from vantage.models import FilmPlan
from vantage.paths import RUNTIME_DIR

ONE_CUT = {**FILM, "vertical": False}
FONTS = RUNTIME_DIR / "fonts"


def _edit(root: Path, name: str, change: Any) -> Project:
    """Apply `change` to the YAML mapping in root/name and reload the project."""
    path = root / name
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return load_project(root)


def _with_facts(root: Path, **film: Any) -> Project:
    """The tiny project, a client display font and a capture note that prints a fact."""
    make_project(
        root,
        film={**ONE_CUT, **film},
        story={"captures": [{"date": "2026-01-20", "note": "{fact:acres} of meadow."}]},
    )
    (root / "facts.yaml").write_text(
        yaml.safe_dump({"facts": {"acres": {"text": "12 acres", "status": "verified"}}}), encoding="utf-8"
    )
    (root / "brand" / "fonts").mkdir()
    shutil.copy(FONTS / "inter" / "inter-latin-opsz-normal.woff2", root / "brand" / "fonts" / "display.woff2")

    def font(brand: dict[str, Any]) -> None:
        brand["typography"] = {"display": {"family": "Client Display", "files": ["fonts/display.woff2"]}}

    return _edit(root, "brand/brand.yaml", font)


def _plan(project: Project) -> FilmPlan:
    return plan_films(project, only=["16x9"], fast=True)["16x9"]


def test_plan_is_deterministic(tmp_path):
    project = _with_facts(tmp_path / "proj")
    first, second = _plan(project), _plan(project)
    assert first.hash == second.hash and len(first.hash) == 64
    a, b = first.save(tmp_path / "a.plan.json"), second.save(tmp_path / "b.plan.json")
    assert a.read_bytes() == b.read_bytes()
    loaded = FilmPlan.load(a)
    assert loaded == first and loaded.hash == first.hash
    assert json.loads(a.read_text())["hash"] == first.hash
    assert first.plan["inputs"]["fonts"]["display"]["file"] == "brand:fonts/display.woff2"
    assert first.plan["inputs"]["fonts"]["text"]["file"].startswith("runtime:inter/")
    assert set(first.plan["inputs"]["masters"]) == {
        f"overview/{d}.jpg" for d in ("2025-06-14", "2025-08-02", "2026-01-20")
    }
    assert first.facts == {"acres": {"status": "verified", "releasable": True}}
    assert first.plan["provenance"] == ["photo"] and first.plan["tag"] == []


def test_plan_hash_tracks_inputs(tmp_path):
    root = tmp_path / "proj"
    project = _with_facts(root)
    base = _plan(project)

    def changed(project: Project) -> bool:
        return _plan(project).hash != base.hash

    # Not hashed: a fact's status (a snapshot beside the plan) and the encode settings.
    project = _edit(root, "facts.yaml", lambda d: d["facts"]["acres"].update(status="needs-client"))
    assert not changed(project)
    assert _plan(project).facts["acres"] == {"status": "needs-client", "releasable": False}
    tweaked = _plan(project)
    tweaked.encode.update(crf=30, preset="ultrafast")
    assert tweaked.hash == base.hash
    project = _edit(root, "facts.yaml", lambda d: d["facts"]["acres"].update(status="verified"))

    project = _edit(root, "story.yaml", lambda d: d["captures"][0].update(note="Changed."))
    assert changed(project)
    project = _edit(root, "story.yaml", lambda d: d["captures"][0].update(note="{fact:acres} of meadow."))
    assert not changed(project)

    master = root / "masters" / "overview" / "2025-08-02.jpg"
    original = master.read_bytes()
    master.write_bytes(original[:-2] + b"\x00\xff\xd9")
    assert changed(project)
    master.write_bytes(original)

    font = root / "brand" / "fonts" / "display.woff2"
    shutil.copy(FONTS / "fraunces" / "fraunces-latin-opsz-normal.woff2", font)
    assert changed(project)
    shutil.copy(FONTS / "inter" / "inter-latin-opsz-normal.woff2", font)
    assert not changed(project)

    assert changed(_edit(root, "project.yaml", lambda d: d["output"]["film"].update(hold_s=0.6)))
    _edit(root, "project.yaml", lambda d: d["output"]["film"].update(hold_s=FILM["hold_s"]))
    assert changed(_edit(root, "project.yaml", lambda d: d.update(draft=True)))


def test_classic_plan_timeline(tmp_path):
    project = make_project(tmp_path / "proj", film=FILM)
    plans = plan_films(project, fast=True)
    assert list(plans) == ["16x9", "9x16"]
    plan = plans["16x9"].plan
    hold, fade, title, end = FILM["hold_s"], FILM["fade_s"], FILM["title_card_s"], FILM["end_card_s"]
    t1, s01, e1 = plan["shots"]
    assert (t1["id"], t1["kind"], t1["role"], s01["id"], s01["kind"], e1["id"], e1["role"]) == (
        "T1", "card", "title", "S01", "section", "E1", "end",
    )  # fmt: skip
    length = 3 * hold + 2 * fade
    assert t1["seconds"] == {"start": 0.0, "duration": title, "dissolve": min(fade, title / 3)}
    assert s01["seconds"] == {
        "start": title,
        "length": length,
        "lead": min(fade, title / 3),
        "tail": min(fade, end / 3),
    }
    assert e1["seconds"] == {"start": title + length, "duration": end, "dissolve": min(fade, end / 3)}
    assert plan["seconds_total"] == title + length + end
    assert plan["poster_seconds"] == title + 2 * (hold + fade) + 0.85 * hold
    assert plan["frames"] == round((title + length + end) * FILM["fps"])
    assert (t1["f0"], e1["f1"]) == (0, plan["frames"])
    assert [c["key"] for c in s01["shots"]] == [
        math.ceil(round((title + i * (hold + fade) + hold / 2) * 12, 6)) for i in range(3)
    ]
    assert [b["style"] for b in t1["blocks"]] == ["KICKER", "KICKER", "TITLE", "SPAN"]
    assert t1["blocks"][1]["text"] is None and t1["blocks"][2]["text"] == "A Tiny Site"
    assert (plan["width"], plan["height"], plans["9x16"].plan["width"], plans["9x16"].plan["height"]) == (
        320,
        180,
        180,
        320,
    )
    assert plan["grid"] is None and plan["cut"] == "classic" and plan["renderer"] == 1


def test_reserved_and_hand_edited_plans_are_refused(tmp_path):
    project = make_project(tmp_path / "proj", film=ONE_CUT)
    plan = _plan(project)
    for kind in ("ortho", "heatmap", "insert"):
        bad = json.loads(json.dumps(plan.plan))
        bad["shots"][0]["kind"] = kind
        with pytest.raises(ValueError, match="reserved"):
            FilmPlan(id="16x9", plan=bad)
    path = plan.save(tmp_path / "x.plan.json")
    path.write_text(path.read_text().replace('"A Tiny Site"', '"Another Site"'))
    with pytest.raises(ValueError, match="edited by hand"):
        FilmPlan.load(path)


def test_render_from_loaded_plan_is_identical(tmp_path):
    project = make_project(tmp_path / "proj", film={**ONE_CUT, "title_card_s": 0.5, "end_card_s": 0.5})
    plan = _plan(project)
    loaded = FilmPlan.load(plan.save(tmp_path / "saved.plan.json"))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    _render_one(project, plan, a, plan_only=False)
    _render_one(project, loaded, b, plan_only=False)
    for name in ("tiny-site-16x9.mp4", "tiny-site-16x9.jpg", "tiny-site-16x9.storyboard.jpg"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name
    saved = FilmPlan.load(a / "tiny-site-16x9.plan.json")
    assert saved.hash == plan.hash
    assert (
        saved.render is not None and saved.render["mp4"]["bytes"] == (a / "tiny-site-16x9.mp4").stat().st_size
    )
    assert saved.render["flash"]["pass"] is True and saved.render["ffmpeg"]


def test_storyboard_tiles_and_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    project = make_project(tmp_path / "proj", film=FILM)
    out = {}
    for run in ("a", "b"):
        render_film(project, tmp_path / run, plan_only=True)
        out[run] = tmp_path / run
    for fid, (cols, tile_w) in (("16x9", storyboard.LANDSCAPE), ("9x16", storyboard.PORTRAIT)):
        plan = FilmPlan.load(out["a"] / f"tiny-site-{fid}.plan.json")
        tiles = storyboard.tiles(plan)
        # title + end card, one per capture, the poster
        assert [t.shot for t in tiles] == ["T1", "S01", "S01", "S01", "E1", "P1"]
        board = out["a"] / f"tiny-site-{fid}.storyboard.jpg"
        tile_h = round(tile_w * plan.plan["height"] / plan.plan["width"])
        with Image.open(board) as im:
            assert im.format == "JPEG" and not im.info.get("progressive") and "exif" not in im.info
            assert im.size == (
                cols * tile_w,
                math.ceil(len(tiles) / cols) * (tile_h + storyboard.STRIP) + storyboard.FOOTER,
            )
        assert board.read_bytes() == (out["b"] / board.name).read_bytes()


def test_storyboard_chips_follow_fact_status():
    chip = storyboard.chip_color
    assert chip({"status": "verified", "releasable": True}) == "green"
    assert chip({"status": "needs-client", "releasable": False}) == "amber"
    assert chip({"status": "reported", "releasable": False}) == "amber"
    assert chip({"status": "do-not-print", "releasable": False}) == "red"
    assert storyboard.timecode(1410, 30) == "0:47.00" and storyboard.timecode(1923, 30) == "1:04.03"


def test_plan_only_writes_no_mp4(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    project = make_project(tmp_path / "proj", film=FILM)
    out = tmp_path / "film"
    written = render_film(project, out, plan_only=True)
    assert [p.name for p in written] == ["tiny-site-16x9.plan.json", "tiny-site-9x16.plan.json"]
    assert sorted(p.name for p in out.iterdir()) == [
        f"tiny-site-{fid}{suffix}"
        for fid in ("16x9", "9x16")
        for suffix in (".chapters.vtt", ".plan.json", ".srt", ".storyboard.jpg", ".txt", ".vtt")
    ]
    assert all(FilmPlan.load(p).render is None for p in written)


def test_do_not_print_facts_never_reach_a_film(tmp_path):
    root = tmp_path / "proj"
    project = _with_facts(root)
    project = _edit(root, "facts.yaml", lambda d: d["facts"]["acres"].update(status="do-not-print"))
    plan = _plan(project)
    notes = [c["note"] for s in plan.plan["shots"] if s["kind"] == "section" for c in s["shots"]]
    assert notes == [None, None, None]
    assert "acres" not in plan.facts and any("do-not-print" in w for w in plan.warnings)
    assert "withheld" not in json.dumps(plan.plan)
