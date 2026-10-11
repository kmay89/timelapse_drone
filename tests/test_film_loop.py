"""The event-screen loop (`preset: screen`): preset resolution, the exact seam, closed GOPs, the
--hours stream copy into film/usb/ and its guards, and stats that need releasable facts.

The tiny project of test_film_story.py at 12 fps; the screen preset's 75 bpm gives B = 10 frames
per beat (D = 7), so a GOP is 20 frames.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import numpy as np
import pytest

from test_film_story import STATS, STORY_FILM, _facts, tiny
from vantage.config import Project
from vantage.film import loop, render
from vantage.film.cut import _Cut
from vantage.film.plan import plan_films, resolve_films
from vantage.film.render import render_film
from vantage.media.ffmpeg import ffprobe_bin, probe
from vantage.models import FilmPlan
from vantage.site.package import package_project

B = 10
GOP = 2 * B
SCREEN = {"screen": {"preset": "screen"}}


def _project(root: Path, **kw) -> Project:
    return tiny(root, films=SCREEN, **kw)


def _screen(project: Project) -> FilmPlan:
    return plan_films(project, only=["screen"], fast=False)["screen"]


def _ffprobe(path: Path, *args: str) -> dict:
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-of", "json", *args, str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(out)


def test_screen_preset_resolution(tmp_path):
    project = _project(tmp_path / "p")
    screen = resolve_films(project)["screen"]
    assert (screen.auto, screen.loop, screen.bpm, screen.type_scale, screen.safe) == (
        False, True, 75.0, 1.6, (0.05, 0.05, 0.05, 0.05),
    )  # fmt: skip
    assert (screen.title_beats, screen.end_beats, screen.beats) == (4, 4, (2, 4, 5))
    plan = _screen(project)
    p = plan.plan
    assert (p["preset"], p["loop"], p["grid"]["fpb"], p["layout"]["name"]) == ("screen", True, B, "screen")
    assert p["layout"]["type_scale"] == 1.6 and p["layout"]["safe"] == [9, 16, 9, 16]  # 5 % of 180 / 320
    assert plan.encode["web"] is None and plan.encode["gop"] == GOP and plan.encode["tracks"] is False
    kinds = [s["kind"] for s in p["shots"]]
    assert kinds[:2] == ["rise", "title"] and kinds[-2:] == ["end", "dip"]
    rise, dip = p["shots"][0], p["shots"][-1]
    assert (rise["id"], rise["f0"], rise["f1"]) == ("R1", 0, 2 * B)
    assert (dip["id"], dip["f1"]) == ("D1", p["frames"]) and dip["f1"] - dip["f0"] == 2 * B
    # Captures linger: 2 beats quiet, 4 with a step, 5 with a push.
    caps = [s for s in p["shots"] if s["kind"] == "capture"]
    beats = [(s["f1"] - s["f0"]) // B for s in caps]
    assert beats == [4, 2, 5, 2, 4], beats
    # Brand only (no base_url): no credits, partners, fine print or honesty line on a loop's end card.
    end = p["shots"][-2]
    assert [t["role"] for t in end["text"]] == ["brand"]

    out = tmp_path / "film"
    render_film(project, out, plan_only=True)  # the auto films only
    assert not list(out.glob("*screen*"))
    render_film(project, out, only=["screen"], plan_only=True)
    assert (out / "tiny-site-screen.plan.json").is_file() and (
        out / "tiny-site-screen.storyboard.jpg"
    ).is_file()


def test_screen_end_card_prints_the_url_large(tmp_path):
    output = {"base_url": "https://example.org/story/", "film": STORY_FILM}
    project = tiny(tmp_path / "p", films=SCREEN, output=output)
    assert project.config.output.film.fps == 12
    end = next(s for s in _screen(project).plan["shots"] if s["kind"] == "end")
    assert end["text"] == [
        {"role": "brand", "text": "Tiny Brand"},
        {"role": "url", "text": "example.org/story"},
    ]


def test_loop_seam_is_exact(tmp_path):
    project = _project(tmp_path / "p")
    plan = _screen(project)
    cut = _Cut(project, plan)
    N = plan.plan["frames"]
    assert N % GOP == 0
    first, after = cut.frame_at(0), cut.frame_at(N)
    assert first.tobytes() == after.tobytes()
    night = tuple(int(plan.plan["inputs"]["colors"]["night"].lstrip("#")[k : k + 2], 16) for k in (4, 2, 0))
    assert np.array_equal(first, np.full_like(first, night))
    # It eases: the frames either side of the seam are close to night, not a jump.
    for f in (1, N - 1):
        assert np.abs(cut.frame_at(f).astype(np.int16) - first).mean() < 6, f
    # The rise ends on the title's image: the rise's last frame is close to the title's first.
    title = plan.plan["shots"][1]
    a, b = cut.frame_at(title["f0"] - 1).astype(np.float32), cut.frame_at(title["f0"]).astype(np.float32)
    assert np.abs(a - b).mean() < 8


def test_loop_encode(tmp_path, monkeypatch):
    project = _project(tmp_path / "p")
    commands: list[list[str]] = []

    class Spy(render.FrameWriter):
        def __enter__(self):
            commands.append(list(self.cmd))
            return super().__enter__()

    monkeypatch.setattr(render, "FrameWriter", Spy)
    out = tmp_path / "film"
    (mp4,) = render_film(project, out, only=["screen"])
    plan = FilmPlan.load(out / "tiny-site-screen.plan.json")
    gop = plan.encode["gop"]
    assert gop == GOP and plan.render is not None and plan.render["web"] is None
    assert not (out / "tiny-site-screen-web.mp4").exists()
    cmd = " ".join(commands[0])
    assert f"-g {gop} -keyint_min {gop} -sc_threshold 0 -x264-params open-gop=0 -level:v 4.1" in cmd
    streams = _ffprobe(mp4, "-show_streams")["streams"]
    assert [s["codec_type"] for s in streams] == ["video"]  # no audio, no subtitles
    frames = _ffprobe(
        mp4, "-select_streams", "v", "-show_entries", "frame=key_frame,pict_type", "-show_frames"
    )
    keys = [k for k, fr in enumerate(frames["frames"]) if fr["key_frame"] == 1]
    assert len(frames["frames"]) == plan.plan["frames"]
    assert keys == list(range(0, plan.plan["frames"], gop))
    assert all(frames["frames"][k]["pict_type"] == "I" for k in keys)
    assert plan.render["flash"]["pass"]


def test_hours_concat(tmp_path):
    root = tmp_path / "p"
    project = _project(root)
    dist = tmp_path / "dist"
    out = dist / "film"
    render_film(project, out, only=["screen"])
    plan = FilmPlan.load(out / "tiny-site-screen.plan.json")
    cycle = plan.plan["frames"] / plan.plan["fps"]
    k = math.ceil(36 / cycle)
    render_film(project, out, only=["screen"], hours=0.01)
    usb = out / "usb" / "tiny-site-screen-0.01h.mp4"
    assert usb.is_file() and not list((out / "usb").glob("*.part"))
    assert probe(usb).duration_s == pytest.approx(k * cycle, abs=1 / plan.plan["fps"] + 1e-6)
    keys = _ffprobe(usb, "-select_streams", "v", "-show_entries", "frame=key_frame", "-show_frames")
    assert len(keys["frames"]) == k * plan.plan["frames"]
    first = usb.read_bytes()
    assert loop.concat_hours(out / "tiny-site-screen.mp4", 0.01, out / "usb") == usb
    assert usb.read_bytes() == first  # the same cycle gives the same bytes
    # Never packaged: not in SHA256SUMS, not in build.json.
    package_project(project, dist)
    sums = (dist / "SHA256SUMS").read_text(encoding="utf-8")
    info = json.loads((dist / "build.json").read_text(encoding="utf-8"))
    assert "usb" not in sums and not any("usb" in f for f in info["files"])
    assert "film/tiny-site-screen.mp4" in info["files"]
    how = (dist / "HOW-TO-VIEW.txt").read_text(encoding="utf-8")
    assert (
        "silent loop for an event screen; for a TV's USB player use film/usb/tiny-site-screen-<H>h.mp4" in how
    )


def test_hours_guards(tmp_path, monkeypatch):
    project = _project(tmp_path / "p")
    out = tmp_path / "film"
    with pytest.raises(ValueError, match="not a loop film"):
        render_film(project, out, only=["16x9"], hours=1)
    with pytest.raises(ValueError, match="needs --film"):
        render_film(project, out, hours=1)
    with pytest.raises(ValueError, match="at most 12"):
        render_film(project, out, only=["screen"], hours=13)
    with pytest.raises(ValueError, match="drop --plan-only"):
        render_film(project, out, only=["screen"], hours=1, plan_only=True)
    assert not out.exists()  # refused before planning anything
    render_film(project, out, only=["screen"])
    monkeypatch.setattr(loop, "FAT32_LIMIT", 10 * (out / "tiny-site-screen.mp4").stat().st_size)
    with pytest.raises(ValueError, match="FAT32"):
        loop.concat_hours(out / "tiny-site-screen.mp4", 1, out / "usb")
    assert not (out / "usb").exists() or not list((out / "usb").iterdir())


def test_screen_stats_need_releasable_facts(tmp_path):
    root = tmp_path / "p"
    stats = {**STATS, "items": [STATS["items"][2]]}
    tiny(root, films=SCREEN, chapters=[stats])
    project = _facts(root, area={"text": "13,100", "status": "needs-client"})
    plan = _screen(project)
    assert not [s for s in plan.plan["shots"] if s["kind"] == "stat"]
    assert any("left out of the loop" in w and "area" in w for w in plan.warnings), plan.warnings
    wide = plan_films(project, only=["16x9"], fast=False)["16x9"]
    assert [s["kind"] for s in wide.plan["shots"]].count("stat") == 1  # the release gate guards those
    tiny(root, films=SCREEN, chapters=[stats], draft=True)
    project = _facts(root, area={"text": "13,100", "status": "needs-client"})
    stat = [s for s in _screen(project).plan["shots"] if s["kind"] == "stat"]
    assert len(stat) == 1 and stat[0]["facts"] == ["area"]


def test_screen_prints_only_releasable_facts(tmp_path, monkeypatch):
    """Every text of an unattended loop (step words, labels, cards), not only its stats, drops a
    fact that is not releasable unless the project is a draft; --hours refuses such a plan."""
    monkeypatch.setenv("VANTAGE_FAST", "1")
    root = tmp_path / "p"
    steps = [{"capture": "2025-07-01", "text": "**Opens {fact:opening}.** Then the rest."}]
    captures = [{"date": "2025-03-01", "label": "Spring {fact:opening}"}]
    tiny(root, films={"screen": {"preset": "screen", "hours": 0.01}}, steps=steps, captures=captures)
    project = _facts(root, opening={"text": "Aug. 1, 2026", "status": "needs-client"})
    plan = _screen(project)
    assert "Aug. 1" not in json.dumps(plan.plan) and plan.facts == {}
    assert any("left out of the loop film (fact 'opening'" in w for w in plan.warnings), plan.warnings
    wide = plan_films(project, only=["16x9"], fast=False)["16x9"]
    assert "Aug. 1" in json.dumps(wide.plan)  # the release gate guards the regular films
    tiny(root, films=SCREEN, steps=steps, captures=captures, draft=True)
    project = _facts(root, opening={"text": "Aug. 1, 2026", "status": "needs-client"})
    assert "opening" in _screen(project).facts  # a draft shows it (tagged Preview)
    # --hours refuses a loop plan that prints an unreleasable fact in a project that is not a draft.
    plan = _screen(project)
    monkeypatch.setattr(render, "plan_films", lambda *a, **k: {"screen": plan})
    project.config.draft = False
    with pytest.raises(Exception, match="not releasable"):
        render_film(project, tmp_path / "film", only=["screen"], hours=0.01)


def test_cli_hours(tmp_path):
    from typer.testing import CliRunner

    from vantage.cli import app

    root = tmp_path / "p"
    _project(root)
    runner = CliRunner()
    base = ["--dist-dir", str(tmp_path / "dist"), "film", str(root)]
    bad = runner.invoke(app, [*base, "--film", "16x9", "--hours", "1"])
    assert bad.exit_code != 0 and "not a loop film" in bad.output
    ok = runner.invoke(app, [*base, "--film", "screen", "--hours", "0.01"])
    assert ok.exit_code == 0, ok.output
    assert (tmp_path / "dist" / "tiny-site" / "film" / "usb" / "tiny-site-screen-0.01h.mp4").is_file()


def test_planned_only_screen_does_not_block_a_release(tmp_path, monkeypatch):
    """`--film screen --plan-only` (the approval step) leaves a plan without an MP4: a release skips
    it and ships none of its files; a stale rendered screen film names `--film screen` in its remedy."""
    monkeypatch.setenv("VANTAGE_FAST", "1")
    root, dist = tmp_path / "p", tmp_path / "dist"
    film_dir = dist / "film"
    project = _project(root)
    render_film(project, film_dir, only=["screen"], plan_only=True)
    render_film(project, film_dir)  # the auto films
    files = package_project(project, dist, release=True)
    sums = files["checksums"].read_text()
    assert "tiny-site-16x9.mp4" in sums and "tiny-site-screen" not in sums
    render_film(project, film_dir, only=["screen"])
    project = tiny(root, films={"screen": {"preset": "screen", "bpm": 80}})
    with pytest.raises(Exception, match=r"vantage film tiny-site --film screen"):
        package_project(project, dist, release=True)
