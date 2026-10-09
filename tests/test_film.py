"""Films from three tiny synthetic masters: timeline, encode settings, posters and card text.

`make_project` is shared with test_llm.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import yaml

from vantage.config import Project, load_project
from vantage.film.render import _sections, capture_labels, render_film
from vantage.media.ffmpeg import extract_frame, probe
from vantage.models import AlignInfo, MasterCapture, MastersIndex, MastersVantage

DATES = ("2025-06-14", "2025-08-02", "2026-01-20")
HUES = ((200, 90, 40), (60, 170, 60), (40, 80, 210))  # BGR: blue-ish, green-ish, red-ish
SIZE = (320, 200)


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _master(hue: tuple[int, int, int], seed: int) -> np.ndarray:
    """A textured image dominated by one color, so a frame can be traced back to its capture."""
    rng = np.random.default_rng(seed)
    img = np.empty((SIZE[1], SIZE[0], 3), np.float32)
    img[:] = hue
    img += np.linspace(-25, 25, SIZE[0], dtype=np.float32)[None, :, None]
    img += rng.normal(0, 6, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def make_project(
    root: Path,
    *,
    film: dict[str, Any] | None = None,
    story: dict[str, Any] | None = None,
    extra: dict[str, list[str]] | None = None,
    **config: Any,
) -> Project:
    """Project YAML + one 'overview' vantage with a master per DATES (+ `extra` vantages → dates)."""
    _write_yaml(
        root / "project.yaml",
        {
            "slug": "tiny-site",
            "title": "A Tiny Site",
            "location": {"name": "Testville"},
            "output": {"film": film or {}},
            **config,
        },
    )
    vantages = {"overview": list(DATES), **(extra or {})}
    _write_yaml(
        root / "story.yaml",
        {
            "vantages": [{"id": vid, "name": vid.title()} for vid in vantages],
            "chapters": [{"type": "text", "title": "Hello", "body": "World"}],
            **(story or {}),
        },
    )
    _write_yaml(
        root / "brand" / "brand.yaml",
        {
            "name": "Tiny Brand",
            "credit_line": "Made for testing.",
            "partners": [{"name": "Builder Co", "role": "General Contractor"}],
        },
    )
    index = MastersIndex()
    for vid, dates in vantages.items():
        captures = []
        for i, date in enumerate(dates):
            file = f"{vid}/{date}.jpg"
            (root / "masters" / vid).mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(root / "masters" / file), _master(HUES[i % len(HUES)], i))
            captures.append(
                MasterCapture(date=date, file=file, source=f"src{i}", align=AlignInfo(method="reference"))
            )
        index.vantages[vid] = MastersVantage(
            name=vid.title(), width=SIZE[0], height=SIZE[1], captures=captures
        )
    index.save(root / "masters" / "index.json")
    return load_project(root)


FILM = {
    "width": 320,
    "height": 180,
    "fps": 12,
    "hold_s": 0.5,
    "fade_s": 0.3,
    "title_card_s": 1.5,
    "end_card_s": 1.0,
}


def test_render_film(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    project = make_project(tmp_path / "proj", film=FILM, simulated=True)

    films = render_film(project, tmp_path / "film")

    assert [f.name for f in films] == ["tiny-site-16x9.mp4", "tiny-site-9x16.mp4"]
    expected = 1.5 + 3 * 0.5 + 2 * 0.3 + 1.0
    for film, (w, h) in zip(films, [(320, 180), (180, 320)], strict=True):
        info = probe(film)
        stream = next(s for s in info.raw["streams"] if s["codec_type"] == "video")
        assert (info.codec, info.width, info.height) == ("h264", w, h)
        assert (stream["profile"], stream["pix_fmt"], stream.get("color_space")) == (
            "High",
            "yuv420p",
            "bt709",
        )
        assert info.duration_s == pytest.approx(expected, abs=1.5 / 12)
        poster = cv2.imread(str(film.with_suffix(".jpg")))
        assert poster is not None and poster.shape[:2] == (h, w)
    assert not list((tmp_path / "film").glob("*.part"))

    def grab(t: float) -> np.ndarray:
        out = extract_frame(films[0], t, tmp_path / f"frame-{t:.2f}.png")
        return cv2.imread(str(out)).astype(np.float32)

    bright = lambda img: int((img.mean(axis=2) > 140).sum())  # noqa: E731
    assert bright(grab(0.0)) == 0  # the title card fades in from night
    title = grab(0.8)
    assert bright(title[40:140, 30:290]) > 150  # kicker + title + date span, in paper on night
    assert np.median(title[:20]) < 40
    # Mid-hold of each capture: the frame centre carries that capture's hue, in date order.
    for i, hue in enumerate(HUES):
        centre = grab(1.5 + i * 0.8 + 0.25)[60:110, 120:200].reshape(-1, 3).mean(axis=0)
        assert np.abs(centre - hue).max() < 30, (i, centre)
    assert bright(grab(expected - 0.5)[30:150, 30:290]) > 50  # end card credits


def test_render_film_without_vertical_removes_stale_cut(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    film = {**FILM, "vertical": False, "title_card_s": 0.5, "end_card_s": 0.5}
    project = make_project(tmp_path / "proj", film=film)
    out = tmp_path / "film"
    out.mkdir()
    (out / "tiny-site-9x16.mp4").write_bytes(b"old")
    (out / "tiny-site-9x16.jpg").write_bytes(b"old")

    assert [f.name for f in render_film(project, out)] == ["tiny-site-16x9.mp4"]
    assert sorted(p.name for p in out.iterdir()) == ["tiny-site-16x9.jpg", "tiny-site-16x9.mp4"]


def test_render_film_is_reproducible_and_never_leaves_partial_mp4s(tmp_path, monkeypatch):
    monkeypatch.setenv("VANTAGE_FAST", "1")
    film = {**FILM, "vertical": False, "title_card_s": 0.5, "end_card_s": 0.5}
    project = make_project(tmp_path / "proj", film=film)
    first, second = tmp_path / "a", tmp_path / "b"
    second.mkdir()
    (second / "tiny-site-16x9.mp4.part").write_bytes(b"killed mid-encode")

    for out in (first, second):
        render_film(project, out)

    # Packaging ships every film/*.mp4: only finished films may match, and they are byte-identical.
    assert sorted(p.name for p in second.iterdir()) == ["tiny-site-16x9.jpg", "tiny-site-16x9.mp4"]
    for name in ("tiny-site-16x9.mp4", "tiny-site-16x9.jpg"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


@pytest.mark.parametrize(
    "bad", [{"hold_s": 0}, {"fade_s": -0.5}, {"fps": 0}, {"end_card_s": -1}], ids=lambda b: next(iter(b))
)
def test_render_film_rejects_impossible_timing(tmp_path, bad):
    project = make_project(tmp_path / "proj", film={**FILM, **bad})
    with pytest.raises(ValueError, match=r"output\.film needs"):
        render_film(project, tmp_path / "film")
    assert not (tmp_path / "film").exists()


def test_render_film_needs_masters(tmp_path):
    project = make_project(tmp_path / "proj")
    (project.masters_dir / "index.json").unlink()
    with pytest.raises(FileNotFoundError, match="vantage process"):
        render_film(project, tmp_path / "film")


def test_sections_order_exclusions_and_focus(tmp_path):
    project = make_project(
        tmp_path / "proj",
        extra={"pier": list(DATES), "gate": ["2025-07-01"]},
        story={
            "vantages": [
                {"id": "overview", "name": "Overview"},
                {"id": "pier", "name": "The pier", "portrait_focus": [0.3, 0.6]},
                {"id": "gate", "name": "Gate"},
            ],
            "captures": [
                {"date": "2025-08-02", "exclude": True},
                {"date": "2026-01-20", "label": "Midwinter", "note": "Snow."},
            ],
            "chapters": [{"type": "scrub", "vantage": "pier"}],
        },
    )
    sections = _sections(project, MastersIndex.load(project.masters_dir / "index.json"))

    # The scrub chapter's vantage leads; single-visit vantages only appear as the main one.
    assert [s.name for s in sections] == ["The pier", "Overview"]
    assert sections[0].focus == (0.3, 0.6) and sections[1].focus == (0.5, 0.5)
    assert [(s.date, s.label, s.note) for s in sections[0].shots] == [
        ("2025-06-14", "June 2025", None),
        ("2026-01-20", "Midwinter", "Snow."),
    ]
    assert capture_labels(project, ["2025-06-02", "2025-06-14", "2026-01-20"]) == {
        "2025-06-02": "June 2, 2025",  # two visits in a month get their day, as on the site
        "2025-06-14": "June 14, 2025",
        "2026-01-20": "Midwinter",
    }
