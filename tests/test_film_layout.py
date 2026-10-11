"""Film layouts and web encodes: safe-zone presets, ink boxes inside the safe zone, the classic 16:9
layout unchanged, and the -web.mp4 copy (H.264, faststart, VBV-capped, reproducible)."""

from __future__ import annotations

import json
import struct
import subprocess
from pathlib import Path

import pytest

from test_film import FILM, make_project
from test_film_story import COMPARE, tiny
from vantage.config import Project
from vantage.film.layout import Layout, layout, outside
from vantage.film.plan import check_films, plan_films, web_maxrate
from vantage.film.render import (
    INK_TOLERANCE,
    _Film,
    _render_one,
    check_ink,
    encode_frames,
    render_film,
    renderer,
)
from vantage.media.ffmpeg import ffmpeg_bin, probe
from vantage.models import FilmPlan
from vantage.site.build import ReleaseError

CLASSIC = {**FILM, "vertical": True}


def test_layout_presets():
    story = layout("story-9x16", 1080, 1920)
    assert story.safe == (269, 130, 538, 65)  # px: top, right, bottom, left
    assert story.rect == (65, 269, 950, 1382)  # x0, y0, x1, y1: .14H–.72H, clear of .12W at the right
    assert (story.margin, story.label_top, story.lead_baseline, story.rail_y) == (65, 307, 1152, 1344)
    assert story.tag == "rail-right" and story.scrim is None and story.rail_x is None
    wide = layout("story-16x9", 1920, 1080)
    assert (wide.safe, wide.label_top, wide.lead_baseline, wide.rail_y) == ((65, 96, 65, 96), 65, 842, 950)
    for w, h in ((1920, 1080), (960, 540), (320, 180), (1280, 720)):
        classic = layout("classic-16x9", w, h)
        margin = round(0.055 * w)  # the step-3 constants
        assert classic.margin == margin and classic.rail_y == h - round(0.075 * h)
        assert classic.scrim == (h - round(0.42 * h), round(0.42 * h))
        assert classic.rail_x == (margin, w - margin) and classic.tag == "corner"
        assert classic.unit == min(w, h) / 1080
    portrait = layout("classic-9x16", 1080, 1920)
    assert portrait.safe == story.safe and portrait.rail_y == 1344 and portrait.tag == "top-left"
    assert portrait.rail_x == (65 + 5, 950 - 5)
    assert Layout.from_record(1080, 1920, portrait.record()) == portrait
    with pytest.raises(ValueError, match="unknown film layout"):
        layout("square", 100, 100)


def _ink_boxes(project: Project, fid: str) -> tuple[FilmPlan, Layout]:
    plan = plan_films(project, only=[fid], fast=False)[fid]
    film = renderer(project, plan)
    assert check_ink(plan, film) == []
    return plan, film.lay


@pytest.mark.parametrize("cut", ["story", "classic"])
def test_ink_inside_safe_9x16(tmp_path, cut):
    captures = [{"date": "2025-08-02", "note": "A long note about the new boardwalk along the water."}]
    if cut == "story":
        project = tiny(tmp_path / "p", film={"vertical": True}, chapters=[COMPARE], simulated=True)
    else:
        project = make_project(tmp_path / "p", film=CLASSIC, story={"captures": captures}, simulated=True)
    plan, lay = _ink_boxes(project, "9x16")
    w, h = plan.plan["width"], plan.plan["height"]
    checks = plan.checks
    assert checks["safe"] == list(lay.rect) and checks["outside"] == []
    layers = {item["layer"].split()[0] for item in checks["ink"]}
    assert {"tag", "rail", "stack"} <= layers, layers
    for item in checks["ink"]:
        x, y, bw, bh = item["box"]
        assert not outside(item["box"], lay.rect, INK_TOLERANCE), item  # 1 px of antialiasing
        assert 0.14 * h - 1 <= y and y + bh <= 0.72 * h + 1, item  # between 14 % and 72 % of the height
        assert x + bw <= 0.88 * w + 1, item  # clear of the buttons on the right edge
    # Not hashed: the plan hash is the same with or without the checks.
    assert plan.hash == plan_films(project, only=["9x16"], fast=False)["9x16"].hash


def test_ink_outside_safe_is_a_warning_and_a_release_error(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    project = make_project(tmp_path / "p", film={**FILM, "vertical": False}, simulated=True)
    plan = plan_films(project)["16x9"]
    film = _Film(project, plan)
    film.ink.append({"shot": "S01", "layer": "label", "box": [0, 0, 10, 10]})
    problems = check_ink(plan, film)
    assert len(problems) == 1 and "S01 label" in problems[0] and problems[0] in plan.warnings
    assert plan.checks["outside"] == [len(film.ink) - 1]
    # A custom safe zone that the classic tag cannot honour: refused by vantage film --release.
    tight = make_project(
        tmp_path / "q", film={**FILM, "vertical": False}, story={"films": {"16x9": {"safe": [0.44, 0, 0, 0]}}},
        simulated=True,
    )  # fmt: skip
    with pytest.raises(ReleaseError, match="outside the safe zone"):
        render_film(tight, tmp_path / "out", release=True)
    assert not list((tmp_path / "out").glob("*.mp4"))
    # Rendered without --release, the same film is refused by the package gate too.
    render_film(tight, tmp_path / "out")
    assert any("text outside its safe zone" in p for p in check_films(tight, tmp_path / "out"))


def test_custom_safe_override(tmp_path):
    films = {"9x16": {"safe": [0.2, 0.1, 0.3, 0.1]}}
    default = tiny(tmp_path / "a", film={"vertical": True})
    custom = tiny(tmp_path / "b", film={"vertical": True}, films=films)
    base = plan_films(default, only=["9x16"], fast=False)["9x16"].plan["layout"]
    moved = plan_films(custom, only=["9x16"], fast=False)["9x16"].plan["layout"]
    h, w = 320, 180  # the tiny 9:16 film
    assert moved["safe"] == [round(0.2 * h), round(0.1 * w), round(0.3 * h), round(0.1 * w)]
    assert moved["label_top"] == round((0.16 + 0.06) * h) > base["label_top"]  # follows the top down
    assert moved["rail_y"] == round((0.70 - 0.02) * h) < base["rail_y"]  # follows the bottom up
    assert moved["lead_baseline"] == round((0.60 - 0.02) * h)
    assert moved["margin"] == round(0.1 * w)
    # The classic cut takes the same override.
    classic = make_project(tmp_path / "c", film=CLASSIC, story={"films": films})
    lay = plan_films(classic, only=["9x16"], fast=False)["9x16"].plan["layout"]
    assert (lay["name"], lay["safe"], lay["rail_y"]) == ("classic-9x16", moved["safe"], moved["rail_y"])


def test_classic_16x9_unchanged(tmp_path):
    """The classic 16:9 film renders the bytes the step-3 constants gave: margin round(.055W), rail
    H − round(.075H), a bottom scrim over .42H, the tag .8·margin from the corner."""
    project = make_project(
        tmp_path / "p", film={**FILM, "vertical": False, "title_card_s": 0.5, "end_card_s": 0.5},
        simulated=True,
    )  # fmt: skip
    plan = plan_films(project, fast=True)["16x9"]
    out = tmp_path / "out"
    out.mkdir()
    _render_one(project, plan, out, plan_only=False)
    w, h = plan.plan["width"], plan.plan["height"]
    margin, rows = round(0.055 * w), round(0.42 * h)
    legacy = Layout(
        "classic-16x9", w, h, (0, 0, 0, 0), margin, 0, 0, h - round(0.075 * h), "corner", 1.0,
        (h - rows, rows), (margin, w - margin),
    )  # fmt: skip
    again = tmp_path / "legacy.mp4"
    encode_frames(
        _Film(project, plan, layout=legacy), again, crf=plan.encode["crf"], preset=plan.encode["preset"]
    )
    # The embedded tracks change the container, never the H.264 stream (film/tracks.py embed).
    assert _h264(again) == _h264(out / "tiny-site-16x9.mp4")
    # Without tracks, the file itself is the one the step-3 constants gave.
    plain = make_project(
        tmp_path / "q", film={**FILM, "vertical": False, "title_card_s": 0.5, "end_card_s": 0.5, "tracks": False},
        simulated=True,
    )  # fmt: skip
    bare = tmp_path / "bare"
    bare.mkdir()
    _render_one(plain, plan_films(plain, fast=True)["16x9"], bare, plan_only=False)
    assert again.read_bytes() == (bare / "tiny-site-16x9.mp4").read_bytes()


def _h264(path: Path) -> bytes:
    """The H.264 elementary stream of an MP4 (stream copy, Annex B)."""
    return subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "h264", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip


def _atoms(path: Path) -> list[str]:
    """The top-level MP4 boxes in file order."""
    data, names, i = path.read_bytes(), [], 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i : i + 8])
        if size == 1:
            size = struct.unpack(">Q", data[i + 8 : i + 16])[0]
        elif size == 0:
            size = len(data) - i
        names.append(kind.decode("latin-1"))
        i += size
    return names


def test_web_maxrate():
    assert web_maxrate(37.2) == 5_073_000  # floor(0.9 · 25 MiB · 8 / 37.2) = 5,073,755 → whole kbit/s
    assert web_maxrate(10.0) == 6_000_000  # capped
    assert web_maxrate(600.0) == 314_000


def test_web_encode(tmp_path):
    project = tiny(tmp_path / "p", simulated=True)
    out = {}
    for run in ("a", "b"):
        out[run] = tmp_path / run
        render_film(project, out[run])
    web, main = out["a"] / "tiny-site-16x9-web.mp4", out["a"] / "tiny-site-16x9.mp4"
    assert web.read_bytes() == (out["b"] / web.name).read_bytes()
    info = probe(web)
    stream = next(s for s in info.raw["streams"] if s["codec_type"] == "video")
    assert (info.codec, stream["profile"], stream["pix_fmt"], stream.get("color_space")) == (
        "h264", "High", "yuv420p", "bt709",
    )  # fmt: skip
    kinds = [s["codec_type"] for s in info.raw["streams"]]
    assert kinds == ["video", "subtitle", "data"]  # no audio; captions and the chapter track
    atoms = _atoms(web)
    assert atoms.index("moov") < atoms.index("mdat")  # faststart
    assert info.duration_s == pytest.approx(probe(main).duration_s, abs=1e-6)
    plan = FilmPlan.load(out["a"] / "tiny-site-16x9.plan.json")
    p, encode = plan.plan, plan.encode
    rate = web_maxrate(p["frames"] / p["fps"])
    assert encode["web"] == {"crf": 23, "preset": "slow", "maxrate": rate, "bufsize": 2 * rate}
    assert plan.render is not None and plan.render["web"]["bytes"] == web.stat().st_size
    assert check_films(project, out["a"]) == []
    # A hand-replaced web copy is caught like a hand-replaced film.
    web.write_bytes(web.read_bytes() + b"\0")
    assert any(
        "tiny-site-16x9-web.mp4 does not match its render record" in p for p in check_films(project, out["a"])
    )


def test_no_web_copy_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    project = make_project(tmp_path / "p", film={**FILM, "vertical": False, "web": False})
    render_film(project, tmp_path / "out")
    assert not (tmp_path / "out" / "tiny-site-16x9-web.mp4").exists()
    plan = json.loads((tmp_path / "out" / "tiny-site-16x9.plan.json").read_text())
    assert plan["encode"]["web"] is None and plan["render"]["web"] is None


def test_stale_web_copy_and_orphans_never_ship(tmp_path, monkeypatch):
    """Render with a web copy, turn it off and re-render: the old web copy is deleted, and one put
    back (or any file of no current film) is a release problem that packaging never lists."""
    from test_film_story import _facts
    from vantage.config import load_project
    from vantage.site.package import package_project

    monkeypatch.setenv("VANTAGE_FAST", "1")
    root, dist = tmp_path / "p", tmp_path / "dist"
    film_dir = dist / "film"
    tiny(root, film={"web": True})
    project = _facts(root)
    render_film(project, film_dir)
    web = film_dir / "tiny-site-16x9-web.mp4"
    old_web = web.read_bytes()
    assert check_films(project, film_dir) == []

    _edit_film(root, web=False)
    project = load_project(root)
    assert any("tiny-site-16x9-web.mp4 is not the web copy" in p for p in check_films(project, film_dir))
    render_film(project, film_dir)
    assert not web.exists()  # the re-render removed it
    assert check_films(project, film_dir) == []
    web.write_bytes(old_web)  # put back by hand, or left by an older engine
    (film_dir / "tiny-site-old.txt").write_text("orphan\n", encoding="utf-8")
    problems = check_films(project, film_dir)
    assert any("tiny-site-16x9-web.mp4 is not the web copy" in p for p in problems)
    assert any("tiny-site-old.txt: film 'old' is not in the current film set" in p for p in problems)
    with pytest.raises(ReleaseError, match="not the web copy"):
        package_project(project, dist, release=True)
    files = package_project(project, dist)  # a draft package logs them and leaves them out
    sums = files["checksums"].read_text()
    assert "tiny-site-16x9.mp4" in sums and "-web.mp4" not in sums and "tiny-site-old.txt" not in sums

    # Encode-only drift (lang is not hashed) is caught, and a disabled film set ships nothing.
    _edit_film(root, web=False, lang="fr")
    web.unlink()
    (film_dir / "tiny-site-old.txt").unlink()
    project = load_project(root)
    assert any("encode differs in lang" in p for p in check_films(project, film_dir))
    _edit_film(root, web=False, enabled=False)
    project = load_project(root)
    assert check_films(project, film_dir) == []
    assert "film/" not in package_project(project, dist)["checksums"].read_text()


def _edit_film(root: Path, *, lang: str | None = None, **film: object) -> None:
    import yaml

    path = root / "project.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["output"]["film"].update(film)
    if lang:
        data["lang"] = lang
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
