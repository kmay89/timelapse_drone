from __future__ import annotations

import datetime as dt
import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import pytest
from PIL import ExifTags, Image

from vantage.config import Project, load_project
from vantage.ingest import catalog as catalog_mod
from vantage.ingest.catalog import (
    build_catalog,
    filename_datetime,
    folder_date,
    quicktime_datetime,
    source_id,
)
from vantage.media.ffmpeg import FrameWriter
from vantage.media.ffmpeg import run as ffmpeg_run
from vantage.models import Catalog
from vantage.scaffold import create_project


def _texture(seed: int, w: int, h: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    small = rng.integers(0, 255, (h // 4, w // 4, 3), dtype=np.uint8)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def _video(path: Path, *, seed: int, creation_time: str | None = None, seconds: float = 2.0) -> None:
    w, h, fps = 160, 96, 10
    extra = ["-metadata", f"creation_time={creation_time}"] if creation_time else []
    base = _texture(seed, w, h)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FrameWriter(path, w, h, fps=fps, preset="ultrafast", extra=extra) as writer:
        for i in range(round(seconds * fps)):
            writer.write(np.roll(base, i * 2, axis=1))


def _srt(path: Path, start: dt.datetime, *, frames: int = 20, fps: int = 10) -> None:
    blocks = []
    for i in range(frames):
        t0, t1 = i / fps, (i + 1) / fps
        when = start + dt.timedelta(seconds=t0)
        blocks.append(
            f"{i + 1}\n00:00:{t0:06.3f} --> 00:00:{t1:06.3f}\n".replace(".", ",")
            + f'<font size="28">FrameCnt: {i + 1}, DiffTime: 100ms\n'
            f"{when:%Y-%m-%d %H:%M:%S}.{when.microsecond // 1000:03d}\n"
            f"[iso: 100] [shutter: 1/1000.0] [fnum: 2.8] [ev: 0] [latitude: {12.34 + i * 1e-5:.6f}] "
            f"[longitude: -45.67] [rel_alt: {60 + i * 0.1:.3f} abs_alt: 350.000] "
            f"[gb_yaw: -90.0 gb_pitch: -30.0 gb_roll: 0.0] </font>\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


def _photo(path: Path, *, seed: int, size: tuple[int, int] = (320, 200), when: str | None = None) -> None:
    exif = Image.Exif()
    if when:
        exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = when
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[ExifTags.GPS.GPSLatitudeRef], gps[ExifTags.GPS.GPSLatitude] = "N", (12.0, 20.0, 24.0)
        gps[ExifTags.GPS.GPSLongitudeRef], gps[ExifTags.GPS.GPSLongitude] = "W", (45.0, 40.0, 12.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    rgb = cv2.cvtColor(_texture(seed, *size), cv2.COLOR_BGR2RGB)
    Image.fromarray(rgb).save(path, exif=exif)


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = create_project(tmp_path / "site-story", "site-story", "Site Story")
    f = root / "footage"
    _video(f / "2025-04-12" / "DJI_0001.MP4", seed=1)
    _srt(f / "2025-04-12" / "DJI_0001.SRT", dt.datetime(2025, 4, 12, 10, 41, 7, 123000))
    _video(f / "flights" / "DJI_0002.MP4", seed=2)
    _srt(f / "flights" / "DJI_0002.srt", dt.datetime(2025, 5, 30, 18, 12, 40))
    _video(f / "flights" / "clip.mov", seed=3, creation_time="2025-09-06T02:30:00Z")
    _photo(f / "2025-07-18" / "pano.jpg", seed=4)
    _photo(f / "stills" / "IMG_0007.JPG", seed=5, size=(2000, 1200), when="2025:10:24 15:22:03")
    _photo(f / "stills" / "DJI_20251212121031_0001.JPG", seed=6)
    _photo(f / "stills" / "scan.png", seed=7)
    local = dt.datetime(2026, 1, 31, 13, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()
    os.utime(f / "stills" / "scan.png", (local, local))
    for skipped in (".hidden/a.jpg", "_trash/b.jpg", ".c.jpg", "_d.jpg"):
        _photo(f / skipped, seed=8)
    (f / "notes.txt").write_text("not media")
    (f / "_truth.json").write_text("{}")
    (f / "stills" / "raw.DNG").write_bytes(b"II*\x00")
    return load_project(root)


def test_catalog_dates_telemetry_and_candidates(project: Project) -> None:
    catalog = build_catalog(project)
    by_path = {s.path: s for s in catalog.sources}
    assert {p: (s.date, s.date_source) for p, s in by_path.items()} == {
        "2025-04-12/DJI_0001.MP4": ("2025-04-12", "folder"),
        "flights/DJI_0002.MP4": ("2025-05-30", "srt"),
        "2025-07-18/pano.jpg": ("2025-07-18", "folder"),
        "flights/clip.mov": ("2025-09-05", "quicktime"),
        "stills/IMG_0007.JPG": ("2025-10-24", "exif"),
        "stills/DJI_20251212121031_0001.JPG": ("2025-12-12", "filename"),
        "stills/scan.png": ("2026-01-31", "mtime"),
    }
    assert [s.date for s in catalog.sources] == sorted(s.date for s in catalog.sources)

    video = by_path["2025-04-12/DJI_0001.MP4"]
    rel = "2025-04-12/DJI_0001.MP4"
    assert (
        video.id
        == hashlib.sha1(f"{rel}{video.size_bytes}".encode()).hexdigest()[:8]
        == source_id(rel, video.size_bytes)
    )
    assert video.telemetry == "2025-04-12/DJI_0001.SRT"
    assert video.start == "2025-04-12T10:41:07"
    assert (video.kind, video.width, video.height, video.fps) == ("video", 160, 96, 10.0)
    assert video.duration_s == pytest.approx(2.0, abs=0.15)
    assert video.lat == pytest.approx(12.3401, abs=1e-4) and video.lon == -45.67
    assert video.heading_deg == 270.0 and video.gimbal_pitch_deg == -30.0
    assert by_path["flights/DJI_0002.MP4"].telemetry == "flights/DJI_0002.srt"
    assert by_path["flights/clip.mov"].start == "2025-09-05T22:30:00"
    assert by_path["flights/clip.mov"].telemetry is None
    assert by_path["stills/DJI_20251212121031_0001.JPG"].start == "2025-12-12T12:10:31"

    still = by_path["stills/IMG_0007.JPG"]
    assert (still.kind, still.width, still.height) == ("photo", 2000, 1200)
    assert still.lat == pytest.approx(12.34) and still.lon == pytest.approx(-45.67)

    vids = [c for c in catalog.candidates if c.source == video.id]
    assert 3 <= len(vids) <= 5  # 2 s sampled every 0.5 s
    assert [c.t for c in vids] == sorted(c.t for c in vids)
    for cand in vids:
        assert cand.file == f"candidates/{video.id}/{cand.t:09.3f}.jpg"
        assert cand.sharpness > 0 and cand.rel_alt is not None and 60 <= cand.rel_alt <= 62
        assert cand.lat == pytest.approx(12.34, abs=1e-3) and cand.heading_deg == 270.0
    (big,) = [c for c in catalog.candidates if c.source == still.id]
    assert big.t == 0.0
    assert cv2.imread(str(project.work_dir / big.file)).shape[:2] == (960, 1600)
    assert all((project.work_dir / c.file).is_file() for c in catalog.candidates)
    assert {s.id for s in catalog.sources} == {c.source for c in catalog.candidates}

    assert Catalog.load(project.work_dir / "catalog.json") == catalog
    sheets = sorted(p.name for p in (project.work_dir / "contact").glob("*.jpg"))
    assert sheets == [f"{d}.jpg" for d in catalog.dates()]
    sheet = cv2.imread(str(project.work_dir / "contact" / "2025-04-12.jpg"))
    assert sheet is not None and sheet.shape[1] > 400


def test_incremental_reuse_and_force(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    real = catalog_mod.frames.write_jpeg

    def counting(path: Path, img: np.ndarray, **kw: int) -> Path:
        calls.append(path)
        return real(path, img, **kw)

    monkeypatch.setattr(catalog_mod.frames, "write_jpeg", counting)
    first = build_catalog(project)
    sampled = len(calls)
    assert sampled == len(first.candidates) + len(first.dates())  # candidates + contact sheets

    calls.clear()
    assert build_catalog(project) == first
    assert all(p.parent.name == "contact" for p in calls)

    gone = first.source_by_path("stills/scan.png")
    (project.footage_dir / "stills" / "scan.png").unlink()
    vid = first.source_by_path("flights/DJI_0002.MP4")
    victim = next(c for c in first.candidates if c.source == vid.id)
    (project.work_dir / victim.file).unlink()
    calls.clear()
    second = build_catalog(project)
    resampled = {p.parent.name for p in calls if p.parent.name != "contact"}
    assert resampled == {vid.id}
    assert gone.id not in {s.id for s in second.sources}
    assert not (project.work_dir / "candidates" / gone.id).exists()
    assert not (project.work_dir / "contact" / f"{gone.date}.jpg").exists()

    calls.clear()
    build_catalog(project, force=True)
    assert len([p for p in calls if p.parent.name != "contact"]) == len(second.candidates)


def test_missing_or_empty_footage(tmp_path: Path) -> None:
    proj = load_project(create_project(tmp_path / "empty", "empty"))
    with pytest.raises(FileNotFoundError, match="no supported footage"):
        build_catalog(proj)
    (proj.footage_dir).rmdir()
    with pytest.raises(FileNotFoundError, match="no footage folder"):
        build_catalog(proj)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("DJI_20250412104107_0001_D.JPG", dt.datetime(2025, 4, 12, 10, 41, 7)),
        ("20250412_104107.mp4", dt.datetime(2025, 4, 12, 10, 41, 7)),
        ("PXL_20250412_104107123.mp4", dt.datetime(2025, 4, 12, 10, 41, 7)),
        ("IMG_20250412_104107.jpg", dt.datetime(2025, 4, 12, 10, 41, 7)),
        ("IMG_20250412.jpg", dt.datetime(2025, 4, 12)),
        ("2025-04-12 10.41.07.mov", dt.datetime(2025, 4, 12, 10, 41, 7)),
        ("DJI_0042.MP4", None),
        ("IMG_12345678.jpg", None),
    ],
)
def test_filename_datetime(name: str, expected: dt.datetime | None) -> None:
    assert filename_datetime(name) == expected


def test_folder_date_and_quicktime() -> None:
    assert folder_date(Path("site/2025-06-14 morning/sub/DJI_0001.MP4")) == dt.date(2025, 6, 14)
    assert folder_date(Path("2025-06-14/2025-06-15/x.jpg")) == dt.date(2025, 6, 15)
    assert folder_date(Path("2025-13-40/x.jpg")) is None
    assert folder_date(Path("x.jpg")) is None
    zone = ZoneInfo("America/New_York")
    assert quicktime_datetime("2025-01-10T15:00:00.000000Z", zone) == dt.datetime(2025, 1, 10, 10, 0)
    assert quicktime_datetime("1970-01-01T00:00:00Z", zone) is None
    assert quicktime_datetime("garbage", zone) is None
    assert quicktime_datetime(None, zone) is None


def test_portrait_rotated_video_capped_candidates_and_oversized_still(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = create_project(tmp_path / "portrait", "portrait")
    day = root / "footage" / "2025-06-14"
    landscape = tmp_path / "landscape.mp4"
    _video(landscape, seed=9, seconds=6.0)
    day.mkdir(parents=True)
    ffmpeg_run(["-display_rotation", "90", "-i", landscape, "-c", "copy", day / "DJI_0001.MP4"])
    no_fix = "[latitude: 0.000000] [longitude: 0.000000] [rel_alt: 0.000 abs_alt: 0.000]"
    (day / "DJI_0001.SRT").write_text(f"1\n00:00:00,000 --> 00:00:06,000\n{no_fix}\n")
    _photo(day / "ortho.jpg", seed=10)
    project = load_project(root)
    project.config.select.max_candidates_per_source = 3
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # ortho.jpg now trips Pillow's bomb guard

    catalog = build_catalog(project)
    (video,) = catalog.sources  # the oversized still is skipped, not fatal
    assert (video.width, video.height) == (96, 160)
    assert (video.lat, video.lon) == (None, None)
    cands = catalog.candidates
    assert len(cands) == 3
    assert cands[0].t < 2.0 < 4.0 < cands[-1].t  # spread over the whole clip, not its first seconds
    for cand in cands:
        assert cv2.imread(str(project.work_dir / cand.file)).shape[:2] == (160, 96)
        assert cand.lat is None
