"""Discover footage, date every flight, sample candidate frames → work/catalog.json.

Dates are local calendar dates of the flight, detected in priority order:
a ``YYYY-MM-DD`` folder name below footage/ → the DJI .SRT sidecar's first
timestamp → EXIF ``DateTimeOriginal`` → QuickTime ``creation_time`` (UTC,
converted to the project time zone) → camera file-name patterns → file mtime.

Re-runs are incremental: a source whose path, size and sidecar are unchanged
and whose candidate JPEGs still exist is reused as is (``force`` resamples).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import cv2
import numpy as np
from PIL import Image
from pydantic import ValidationError

from vantage import log
from vantage.config import Project
from vantage.ingest import frames
from vantage.ingest.dji_srt import TelemetrySample, parse_srt, sample_at, summarize
from vantage.ingest.exif import StillMeta, read_still_metadata
from vantage.media import ffmpeg
from vantage.models import Candidate, Catalog, DateSource, Source

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v"})
PHOTO_EXTS = frozenset({".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".heif"})
HEIF_EXTS = frozenset({".heic", ".heif"})
UNSUPPORTED = {".dng": "RAW stills are not supported; export JPEG/TIFF (or shoot JPEG+DNG)"}
SIDECAR_EXTS = (".SRT", ".srt")
CATALOG_NAME = "catalog.json"
WORKERS = max(1, min(4, os.cpu_count() or 1) // 2)  # ffmpeg decodes with its own threads

_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_NAME_DATETIME = re.compile(r"(?<!\d)(\d{4})-?(\d{2})-?(\d{2})(?:[ _T-]?(\d{2})[.:-]?(\d{2})[.:-]?(\d{2}))?")


@dataclass(frozen=True)
class _Stamp:
    when: dt.datetime
    source: DateSource


def source_id(rel_path: str, size: int) -> str:
    """First 8 hex chars of sha1(relative POSIX path + size)."""
    return hashlib.sha1(f"{rel_path}{size}".encode()).hexdigest()[:8]


def _visible(rel: Path) -> bool:
    return not any(part.startswith((".", "_")) for part in rel.parts)


def discover(footage_dir: Path) -> Iterator[Path]:
    """Supported media under `footage_dir`, sorted; hidden and '_'-prefixed names are skipped."""
    for path in sorted(footage_dir.rglob("*")):
        rel = path.relative_to(footage_dir)
        if not path.is_file() or not _visible(rel):
            continue
        ext = path.suffix.lower()
        if ext in VIDEO_EXTS or ext in PHOTO_EXTS:
            yield path
        elif ext in UNSUPPORTED:
            log.warn(f"skipping {rel.as_posix()}: {UNSUPPORTED[ext]}")


def has_media(footage_dir: Path) -> bool:
    """True when `footage_dir` holds at least one visible video or still."""
    return footage_dir.is_dir() and any(
        p.suffix.lower() in VIDEO_EXTS | PHOTO_EXTS and _visible(p.relative_to(footage_dir))
        for p in footage_dir.rglob("*")
    )


def folder_date(rel: Path) -> dt.date | None:
    """Date from the deepest ancestor folder whose name contains YYYY-MM-DD."""
    for name in reversed(rel.parent.parts):
        for m in _ISO_DATE.finditer(name):
            try:
                return dt.date(*map(int, m.groups()))
            except ValueError:
                continue
    return None


def filename_datetime(name: str) -> dt.datetime | None:
    """DJI_20250412104107_0001.JPG, 20250412_104107.mp4, PXL_20250412_104107123.jpg, IMG_20250412.jpg …"""
    for m in _NAME_DATETIME.finditer(Path(name).stem):
        y, mo, d, h, mi, s = (int(g) if g else 0 for g in m.groups())
        if not 1990 <= y <= 2100:
            continue
        try:
            return dt.datetime(y, mo, d, h, mi, s)
        except ValueError:
            continue
    return None


def quicktime_datetime(creation_time: str | None, zone: ZoneInfo) -> dt.datetime | None:
    """QuickTime/MP4 `creation_time` (UTC) → naive local time in `zone`."""
    if not creation_time:
        return None
    try:
        when = dt.datetime.fromisoformat(creation_time.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.year < 1990:  # unset clocks write 1904/1970 epochs
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return when.astimezone(zone).replace(tzinfo=None)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(
            f"unknown timezone {name!r} in project.yaml (use an IANA name like America/New_York)"
        ) from None


def _date_and_start(
    rel: Path, path: Path, stamps: list[_Stamp], zone: ZoneInfo
) -> tuple[str, DateSource, dt.datetime]:
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, zone).replace(tzinfo=None)
    best = stamps[0] if stamps else _Stamp(mtime, "mtime")
    folder = folder_date(rel)
    if folder is not None:
        if stamps and best.when.date() != folder:
            log.warn(
                f"{rel.as_posix()}: folder says {folder}, {best.source} says {best.when.date()}; using folder"
            )
        return folder.isoformat(), "folder", best.when
    if best.source == "mtime":
        log.warn(f"{rel.as_posix()}: no capture time found; dating it by file mtime ({mtime.date()})")
    return best.when.date().isoformat(), best.source, best.when


def _r(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _geo(meta: TelemetrySample | StillMeta | None) -> dict[str, float | None]:
    """Candidate position/attitude fields from a telemetry sample or still metadata."""
    if meta is None:
        return {}
    return {
        "lat": _r(meta.lat, 7),
        "lon": _r(meta.lon, 7),
        "rel_alt": _r(meta.rel_alt, 2),
        "heading_deg": _r(meta.camera_heading, 1),
        "gimbal_pitch_deg": _r(meta.gimbal_pitch, 1),
    }


@dataclass(frozen=True)
class _Job:
    path: Path
    rel: str
    size: int
    sid: str
    sidecar: Path | None


class _Ingester:
    """Describes and samples one source at a time; safe to run from worker threads."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.zone = _zone(project.config.timezone)
        self.settings = project.config.select
        self.footage = project.footage_dir
        self.work = project.work_dir

    def __call__(self, job: _Job) -> tuple[Source, list[Candidate]] | None:
        try:
            if job.path.suffix.lower() in VIDEO_EXTS:
                return self._video(job)
            return self._photo(job)
        except (OSError, ValueError, ffmpeg.FFmpegError, cv2.error, Image.DecompressionBombError) as exc:
            reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            if job.path.suffix.lower() in HEIF_EXTS:
                reason += " (HEIC needs the pillow-heif plugin; export JPEG instead)"
            log.warn(f"skipping {job.rel}: {reason}")
            return None

    def _candidate(
        self, job: _Job, t: float, img: np.ndarray, meta: TelemetrySample | StillMeta | None
    ) -> Candidate:
        file = frames.write_jpeg(self.work / "candidates" / job.sid / f"{t:09.3f}.jpg", img)
        return Candidate(
            source=job.sid,
            t=round(t, 3),
            file=file.relative_to(self.work).as_posix(),
            sharpness=round(frames.sharpness(img), 2),
            **_geo(meta),
        )

    def _fresh_dir(self, job: _Job) -> None:
        shutil.rmtree(self.work / "candidates" / job.sid, ignore_errors=True)

    def _video(self, job: _Job) -> tuple[Source, list[Candidate]]:
        info = ffmpeg.probe(job.path)
        width, height = (
            (info.height, info.width) if info.rotation in (90, -90, 270, -270) else (info.width, info.height)
        )
        samples: list[TelemetrySample] = []
        if job.sidecar is not None:
            samples = parse_srt(job.sidecar)
        stamps = [
            _Stamp(when, src)
            for when, src in (
                (next((s.datetime for s in samples if s.datetime), None), "srt"),
                (quicktime_datetime(info.creation_time, self.zone), "quicktime"),
                (filename_datetime(job.path.name), "filename"),
            )
            if when is not None
        ]
        date, date_source, start = _date_and_start(Path(job.rel), job.path, stamps, self.zone)
        summary = summarize(samples)
        self._fresh_dir(job)
        cands = [
            self._candidate(job, t, frame, sample_at(samples, t))
            for t, frame in frames.sample_video(
                job.path,
                every_s=self.settings.sample_every_s,
                max_frames=self.settings.max_candidates_per_source,
                duration_s=info.duration_s,
            )
        ]
        source = Source(
            id=job.sid,
            path=job.rel,
            kind="video",
            date=date,
            date_source=date_source,
            start=start.isoformat(timespec="seconds"),
            duration_s=round(info.duration_s, 3),
            width=width,
            height=height,
            fps=round(info.fps, 3),
            telemetry=job.sidecar.relative_to(self.footage).as_posix() if job.sidecar else None,
            lat=_r(summary["lat"], 7),
            lon=_r(summary["lon"], 7),
            rel_alt=_r(summary["rel_alt"], 2),
            heading_deg=_r(summary["heading"], 1),
            gimbal_pitch_deg=_r(summary["gimbal_pitch"], 1),
            size_bytes=job.size,
        )
        if not cands:
            log.warn(f"{job.rel}: no frames could be decoded")
        log.info(
            f"{job.rel}  video {width}×{height} {info.duration_s:.1f}s  {date} ({date_source})"
            f"{'  +srt' if samples else ''}  {len(cands)} candidates"
        )
        return source, cands

    def _photo(self, job: _Job) -> tuple[Source, list[Candidate]]:
        meta = read_still_metadata(job.path)
        stamps = [
            _Stamp(when, src)
            for when, src in ((meta.datetime, "exif"), (filename_datetime(job.path.name), "filename"))
            if when is not None
        ]
        date, date_source, start = _date_and_start(Path(job.rel), job.path, stamps, self.zone)
        img = frames.load_photo(job.path)
        self._fresh_dir(job)
        cand = self._candidate(job, 0.0, img, meta)
        source = Source(
            id=job.sid,
            path=job.rel,
            kind="photo",
            date=date,
            date_source=date_source,
            start=start.isoformat(timespec="seconds"),
            width=meta.width,
            height=meta.height,
            size_bytes=job.size,
            **_geo(meta),
        )
        log.info(f"{job.rel}  photo {meta.width}×{meta.height}  {date} ({date_source})")
        return source, [cand]


def _previous(path: Path) -> Catalog | None:
    if not path.exists():
        return None
    try:
        return Catalog.load(path)
    except (OSError, ValueError, ValidationError) as exc:
        log.warn(f"ignoring unreadable {path.name} ({type(exc).__name__}); re-ingesting everything")
        return None


def _sidecar(path: Path) -> Path | None:
    if path.suffix.lower() not in VIDEO_EXTS:
        return None
    return next((p for ext in SIDECAR_EXTS if (p := path.with_suffix(ext)).is_file()), None)


def build_catalog(project: Project, *, force: bool = False) -> Catalog:
    """Catalog footage_dir, sample candidates, write work/catalog.json and work/contact/<date>.jpg."""
    footage, work = project.footage_dir, project.work_dir
    if not footage.is_dir():
        raise FileNotFoundError(f"no footage folder at {footage} (put each flight in footage/YYYY-MM-DD/)")
    ingest = _Ingester(project)
    catalog_path = work / CATALOG_NAME
    previous = None if force else _previous(catalog_path)
    reusable = {s.id: s for s in previous.sources} if previous else {}
    old_cands: dict[str, list[Candidate]] = {}
    for cand in previous.candidates if previous else []:
        old_cands.setdefault(cand.source, []).append(cand)

    results: list[tuple[Source, list[Candidate]] | None] = []
    todo: list[tuple[int, _Job]] = []
    for path in discover(footage):
        rel = path.relative_to(footage).as_posix()
        size = path.stat().st_size
        sidecar = _sidecar(path)
        job = _Job(path, rel, size, source_id(rel, size), sidecar)
        old = reusable.get(job.sid)
        telemetry = sidecar.relative_to(footage).as_posix() if sidecar else None
        cands = old_cands.get(job.sid, [])
        if (
            old
            and old.path == rel
            and old.telemetry == telemetry
            and all((work / c.file).is_file() for c in cands)
        ):
            log.debug(f"{rel}: unchanged, reusing {len(cands)} candidates")
            results.append((old, cands))
        else:
            todo.append((len(results), job))
            results.append(None)
    if not results:
        raise FileNotFoundError(
            f"no supported footage in {footage} (videos: .mp4 .mov; stills: .jpg .png .tif)"
        )

    with ThreadPoolExecutor(WORKERS) as pool:
        for (slot, _), result in zip(todo, pool.map(ingest, [job for _, job in todo]), strict=True):
            results[slot] = result

    done = [r for r in results if r is not None]
    if not done:
        raise ValueError(f"none of the {len(results)} media files in {footage} could be read")
    done.sort(key=lambda r: (r[0].date, r[0].start or "", r[0].path))
    catalog = Catalog(
        sources=[s for s, _ in done],
        candidates=[c for _, cands in done for c in sorted(cands, key=lambda c: c.t)],
    )
    keep = {s.id for s in catalog.sources}
    cand_root = work / "candidates"
    if cand_root.is_dir():
        for stale in cand_root.iterdir():
            if stale.is_dir() and stale.name not in keep:
                shutil.rmtree(stale, ignore_errors=True)
    catalog.save(catalog_path)
    frames.write_contact_sheets(catalog, work)
    reused = len(results) - len(todo)
    log.debug(f"catalog: {len(todo)} sampled, {reused} reused → {catalog_path}")
    return catalog
