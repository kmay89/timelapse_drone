"""Caption, chapter and transcript tracks made from the plan: formats, the 17 characters-per-second
rule, chapters and the transcript, the embedded mov_text and chapter tracks (and the video stream
they leave untouched), facts that only the tracks print, and packaging.

The tiny project of test_film_story.py (12 fps, 90 bpm: B = 8, D = 5).
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
from pathlib import Path

import pytest

from test_film import FILM, make_project
from test_film_story import COMPARE, DATES, STEPS, _facts, tiny
from vantage.config import Project
from vantage.film import tracks
from vantage.film.plan import HONESTY, check_films, plan_films
from vantage.film.render import encode_frames, render_film, renderer
from vantage.media.ffmpeg import ffmpeg_bin, ffprobe_bin
from vantage.models import FilmPlan
from vantage.site.build import ReleaseError
from vantage.site.package import package_project

FPS = 12
STATS = {
    "type": "stats",
    "id": "numbers",
    "title": "What changed",
    "items": [{"value": "{fact:area}", "unit": "sq ft", "label": "of old pool", "source": "Site plan"}],
}
AREA = {"text": "13,100", "status": "verified", "sources": ["Site plan, sheet L-101"]}
VTT_STAMP = re.compile(r"^(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})$")
SRT_STAMP = re.compile(r"^(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})$")


def _full(root: Path, **kw) -> Project:
    """The tiny project with a curtain and a stat, its facts releasable."""
    tiny(root, chapters=[COMPARE, STATS], simulated=True, **kw)
    return _facts(root, area=AREA)


def _plan(project: Project, fid: str = "16x9") -> FilmPlan:
    return plan_films(project, only=[fid], fast=False)[fid]


def _ms(stamp: str) -> int:
    h, m, rest = stamp.replace(",", ".").split(":")
    s, ms = rest.split(".")
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms)


def _ffprobe(path: Path, *args: str) -> dict:
    out = subprocess.run(
        [ffprobe_bin(), "-v", "error", "-of", "json", *args, str(path)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(out)


def _h264(path: Path) -> bytes:
    """The H.264 elementary stream of an MP4 (stream copy, Annex B)."""
    return subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(path), "-map", "0:v", "-c", "copy", "-f", "h264", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip


# --------------------------------------------------------------------------- #
# Formats
# --------------------------------------------------------------------------- #


def test_vtt_srt_format(tmp_path):
    assert tracks.timestamp(36, 30) == "00:00:01.200"
    assert tracks.timestamp(36, 30, srt=True) == "00:00:01,200"
    assert tracks.timestamp(30 * 3725 + 1, 30) == "01:02:05.033"
    assert tracks.minutes(30 * 75, 30) == "1:15"
    assert [tracks.iso639_2(x) for x in ("en", "en-GB", "fr_CA", "es", "de", "pt", "")] == [
        "eng", "eng", "fra", "spa", "deu", "und", "und",
    ]  # fmt: skip
    root = tmp_path / "p"
    project = _full(root, captures=[{"date": DATES[1], "label": "{fact:month}"}])
    project = _facts(root, area=AREA, month={"text": "Late May", "status": "verified", "sources": ["log"]})
    plan = _plan(project)
    p = plan.plan
    assert p["captions"] and all("{fact:" not in c["text"] for c in p["captions"])
    assert any(c["text"] == "Late May" and c["facts"] == ["month"] for c in p["captions"])

    vtt = tracks.to_vtt(plan)
    assert vtt.startswith("WEBVTT\n\n") and "{fact:" not in vtt
    spans = [tuple(map(_ms, m.groups())) for line in vtt.splitlines() if (m := VTT_STAMP.match(line))]
    assert len(spans) == len(p["captions"])
    assert spans[0][0] == 0 and spans[-1][1] == round(p["frames"] * 1000 / FPS)
    for (a0, a1), (b0, _) in itertools.pairwise(spans):
        assert a0 < a1 <= b0  # sorted, never overlapping
    for cue, (a, b) in zip(p["captions"], spans, strict=True):
        assert (a, b) == (round(cue["f0"] * 1000 / FPS), round(cue["f1"] * 1000 / FPS))

    srt = tracks.to_srt(plan)
    blocks = srt.strip().split("\n\n")
    assert [b.splitlines()[0] for b in blocks] == [str(k) for k in range(1, len(blocks) + 1)]
    assert [tuple(map(_ms, SRT_STAMP.match(b.splitlines()[1]).groups())) for b in blocks] == spans  # type: ignore[union-attr]
    assert [b.splitlines()[2] for b in blocks] == [c["text"] for c in p["captions"]]
    assert "{fact:" not in srt

    stem = tmp_path / "out" / "tiny-site-16x9"
    stem.parent.mkdir()
    written = tracks.write_tracks(plan, stem)
    assert [w.name for w in written] == [
        "tiny-site-16x9.vtt", "tiny-site-16x9.srt", "tiny-site-16x9.chapters.vtt", "tiny-site-16x9.txt",
    ]  # fmt: skip
    assert written[0].read_text(encoding="utf-8") == vtt
    # Text that WebVTT would read as markup is escaped there, and only there.
    assert tracks._vtt_text("A & B <i>") == "A &amp; B &lt;i&gt;"


def test_caption_readability_rule(tmp_path):
    short, long = "Weeds.", "The old lot, the cracked paving and the rusted fence, all of it gone by autumn."
    notes = [{"date": DATES[0], "note": short}, {"date": DATES[2], "note": long}]
    plan = _plan(tiny(tmp_path / "p", captures=notes, simulated=True))
    p = plan.plan
    shots = {s["capture"]: s for s in p["shots"] if s["kind"] == "capture"}
    cues = {c["f0"]: c for c in p["captions"]}

    def cue(date: str) -> dict:
        d = shots[date]["dissolve"]
        return cues[shots[date]["f0"] if d is None else d["f0"] + (d["f1"] - d["f0"]) // 2]

    first, third = cue(DATES[0]), cue(DATES[2])
    assert first["text"] == f"March 2025. Before. {short}"  # fits: appended
    assert len(first["text"]) <= tracks.CPS * (first["f1"] - first["f0"]) / FPS
    assert third["text"] == "July 2025. Demolition."  # the note would not read in time: left out
    assert len(f"{third['text']} {long}") > tracks.CPS * (third["f1"] - third["f0"]) / FPS
    # The same rule on a bare collector: the longer text only when it reads at ≤ 17 characters/s.
    t = tracks.Tracks(10)
    t.cue(0, "June", [], ("June. " + "x" * 28, ["n"]))  # 34 characters over 2 s: fits
    t.cue(20, "July", [], ("July. " + "x" * 29, ["n"]))  # 35 characters over 2 s: too many
    t.cue(40, "July", [])  # the same words again: one longer cue
    caps = t.record(60, title="T", subtitle=None, honesty="H")["captions"]
    assert [(c["f0"], c["f1"], c["text"][:5], c["facts"]) for c in caps] == [
        (0, 20, "June.", ["n"]), (20, 60, "July", []),
    ]  # fmt: skip


def test_chapters_and_transcript(tmp_path):
    project = _full(tmp_path / "p")
    plan = _plan(project)
    p = plan.plan
    # The title belongs to the first chapter, which starts at frame 0.
    assert [c["title"] for c in p["chapters"]] == ["One year", "Side by side", "What changed", "Credits"]
    assert p["chapters"][0]["f0"] == 0
    starts = [c["f0"] for c in p["chapters"]]
    assert starts == sorted(set(starts))
    curtain = next(s for s in p["shots"] if s["kind"] == "curtain")
    stat = next(s for s in p["shots"] if s["kind"] == "stat")
    end = p["shots"][-1]
    assert starts[1:] == [curtain["f0"], stat["f0"], end["f0"]]
    chapters = tracks.to_chapters_vtt(plan)
    assert chapters.startswith("WEBVTT\n\n") and chapters.count(" --> ") == 4

    text = tracks.to_transcript(plan)
    lines = text.splitlines()
    seconds = round(p["frames"] / FPS)
    assert lines[0] == "A Tiny Site"
    assert lines[1] == f"{seconds // 60}:{seconds % 60:02d} film, no sound. {HONESTY['simulated']}"
    assert lines[2:4] == ["On screen throughout: Simulated imagery.", ""]  # the tag; not a draft
    timed = [line for line in lines if re.match(r"^\[\d+:\d{2}\] ", line)]
    assert len(timed) == len(p["shots"])  # one line per shot
    assert "The site" in text  # the vantage name
    for shot in p["shots"]:
        if shot["kind"] == "capture":
            assert shot["label"] in text
    assert "Demolition. In ten weeks the slides are gone." in text  # the step's full text, as plain text
    assert "Split screen (The site), Then left and Now right. The line moves to 50%: Half and half." in text
    assert "Figure: 13,100 sq ft of old pool (source: Site plan)." in text
    assert "End card: Tiny Brand / Made for testing." in text
    assert "**" not in text and "{fact:" not in text
    # A loop film has none of it.
    loop = plan_films(tiny(tmp_path / "q", films={"screen": {"preset": "screen"}}), only=["screen"])["screen"]
    assert (loop.plan["captions"], loop.plan["chapters"], loop.plan["transcript"]) == ([], [], None)
    assert loop.encode["tracks"] is False and tracks.track_texts(loop) == {}


# --------------------------------------------------------------------------- #
# Embedded tracks
# --------------------------------------------------------------------------- #


def test_embedded_tracks(tmp_path):
    project = _full(tmp_path / "p")
    out = tmp_path / "film"
    render_film(project, out)
    plan = FilmPlan.load(out / "tiny-site-16x9.plan.json")
    titles = [c["title"] for c in plan.plan["chapters"]]
    for name in ("tiny-site-16x9.mp4", "tiny-site-16x9-web.mp4"):
        info = _ffprobe(out / name, "-show_streams", "-show_chapters")
        subs = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
        assert len(subs) == 1, name
        sub = subs[0]
        assert sub["codec_name"] == "mov_text" and sub["tags"]["language"] == "eng"
        assert sub["disposition"]["default"] == 0  # offered, not shown
        assert not [s for s in info["streams"] if s["codec_type"] == "audio"]
        # `-map_metadata -1` in the remux would blank these titles.
        got = [c.get("tags", {}).get("title", "") for c in info["chapters"]]
        assert got == titles and all(got), name
        assert float(info["chapters"][-1]["end_time"]) == pytest.approx(plan.plan["frames"] / FPS, abs=1e-3)
    # The embedded captions are the sidecar's.
    srt = subprocess.run(
        [ffmpeg_bin(), "-v", "error", "-i", str(out / "tiny-site-16x9.mp4"), "-map", "0:s", "-f", "srt", "-"],
        check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip
    assert [b.splitlines()[2:] for b in srt.strip().split("\n\n")] == [
        b.splitlines()[2:] for b in (out / "tiny-site-16x9.srt").read_text().strip().split("\n\n")
    ]


def test_embed_is_bitexact_and_keeps_video(tmp_path):
    project = _full(tmp_path / "p")
    runs = {}
    for run in ("a", "b"):
        runs[run] = tmp_path / run
        render_film(project, runs[run])
    for path in sorted(runs["a"].iterdir()):
        assert path.read_bytes() == (runs["b"] / path.name).read_bytes(), path.name
    plan = FilmPlan.load(runs["a"] / "tiny-site-16x9.plan.json")
    bare = tmp_path / "bare.mp4"
    encode_frames(renderer(project, plan), bare, crf=plan.encode["crf"], preset=plan.encode["preset"])
    assert not [s for s in _ffprobe(bare, "-show_streams")["streams"] if s["codec_type"] != "video"]
    assert _h264(bare) == _h264(runs["a"] / "tiny-site-16x9.mp4")  # the remux copies video bit for bit
    # The render record is of the file as shipped, tracks included.
    record = plan.render["mp4"]  # type: ignore[index]
    assert record["bytes"] == (runs["a"] / "tiny-site-16x9.mp4").stat().st_size


def test_caption_only_fact_is_gated(tmp_path):
    root = tmp_path / "p"
    tiny(root, captures=[{"date": DATES[1], "note": "{fact:pending} young trees."}], simulated=True)
    project = _facts(root, pending={"text": "78", "status": "needs-client"})
    plan = _plan(project)
    shots = [s for s in plan.plan["shots"] if "pending" in s["facts"]]
    assert shots == []  # nothing on screen prints it ...
    assert "pending" in plan.facts and plan.facts["pending"]["releasable"] is False  # ... the tracks do
    assert any("78 young trees." in line["text"] for line in plan.plan["transcript"]["lines"])
    out = tmp_path / "dist" / "film"
    render_film(project, out, plan_only=True)
    assert any("prints fact 'pending'" in p for p in check_films(project, out))
    with pytest.raises(ReleaseError, match="pending"):
        package_project(project, tmp_path / "dist", release=True)


def test_package_lists_tracks(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    root, dist = tmp_path / "proj", tmp_path / "dist"
    project = make_project(
        root, film={**FILM, "title_card_s": 0.5, "end_card_s": 0.5}
    )  # classic, 16x9 + 9x16
    render_film(project, dist / "film")
    files = package_project(project, dist)
    sums = files["checksums"].read_text()
    info = json.loads(files["build_json"].read_text())
    for fid in ("16x9", "9x16"):
        plan = FilmPlan.load(dist / "film" / f"tiny-site-{fid}.plan.json")
        assert plan.plan["chapters"] and plan.plan["captions"] and plan.encode["tracks"] is True
        for suffix in tracks.SUFFIXES:
            name = f"film/tiny-site-{fid}{suffix}"
            assert f"  {name}\n" in sums and name in info["files"], name
    how = files["how_to_view"].read_text()
    assert "Captions (.srt, .vtt) and a timed text transcript (.txt) come with each film" in how
    # The classic cut's captions are its capture labels; its chapters its sections and the credits.
    plan = FilmPlan.load(dist / "film" / "tiny-site-16x9.plan.json")
    assert [c["title"] for c in plan.plan["chapters"]] == ["Overview", "Credits"]
    assert [c["text"] for c in plan.plan["captions"]][:4] == [
        "A Tiny Site",
        "June 2025",
        "August 2025",
        "January 2026",
    ]


def test_sidecars_are_checked_against_the_plan(tmp_path):
    """A hand-edited caption file, or a sidecar of no current film, is a release problem."""
    root = tmp_path / "p"
    project = tiny(root, simulated=True)
    out = tmp_path / "dist" / "film"
    render_film(project, out, plan_only=True)
    assert ["has no rendered film" in p for p in check_films(project, out)] == [True]  # plan only
    srt = out / "tiny-site-16x9.srt"
    srt.write_text(srt.read_text(encoding="utf-8") + "\n99\n00:00:01,000 --> 00:00:02,000\n42 acres\n")
    (out / "tiny-site-gone.vtt").write_text("WEBVTT\n", encoding="utf-8")
    problems = check_films(project, out)
    assert any("tiny-site-16x9.srt does not match its plan" in p for p in problems)
    assert any("tiny-site-gone.vtt: film 'gone' is not in the current film set" in p for p in problems)


def test_transcript_says_preview_unverified_and_pushes(tmp_path):
    """The transcript (the film's text alternative) carries what the picture says: the Preview tag,
    which lines print unverified figures, and what each push moves in on."""
    root = tmp_path / "p"
    steps = [
        {**STEPS[0], "text": "**Before.** {fact:pending} trees."},
        {**STEPS[1], "focus": [0.62, 0.54, 1.6]},
    ]
    hotspots = [{"id": "tower", "x": 0.93, "y": 0.584, "label": "The tower", "to": DATES[1]}]
    tiny(root, steps=steps, hotspots=hotspots, draft=True)
    project = _facts(root, pending={"text": "78", "status": "needs-client"})
    plan = _plan(project)
    text = tracks.to_transcript(plan)
    assert "On screen throughout: Preview." in text
    assert "Preview, not for release. Lines marked [unverified]" in text
    assert re.search(r"^\[\d+:\d{2}\] \[unverified\] Aerial view .*78 trees\.", text, re.M), text
    assert "The camera moves in on The tower." in text
