"""Pick the best frame of every visit for each vantage (work/selection.json).

The reference frame of a vantage is `vantage.reference`, or else the sharpest
candidate of the latest visit near the vantage hint. For every other visit a
manual `vantage.picks[date]` wins; otherwise the date's sharpest candidates
(after a telemetry prefilter) are registered against the reference at low
resolution and the best-overlapping, best-matching one is kept.
"""

from __future__ import annotations

import io
import math
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageCms, ImageOps

from vantage import log
from vantage.config import AlignSettings, FrameRef, Project, Vantage, VantageHint
from vantage.media import ffmpeg
from vantage.models import Candidate, Catalog, FramePick, Selection, Source, VantageSelection
from vantage.process import align

TOP_N = 24  # sharpest candidates per date that get registered
SELECT_WIDTH = 800  # detection width for scoring
SELECT_FEATURES = 3000
REF_T_TOLERANCE_S = 0.1  # a candidate this close to the requested reference time stands in for it
HEADING_TOLERANCE_DEG = 45.0
SCORE_TIE = 0.02  # scores within this of the best count as tied → sharpest wins
EARTH_RADIUS_M = 6_371_008.8
REF_PROXY_WIDTH = 1600
WORKERS = min(4, os.cpu_count() or 1)

_SRGB = ImageCms.createProfile("sRGB")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def _read_photo(path: Path) -> np.ndarray:
    """Still → BGR uint8, upright (EXIF orientation) and converted to sRGB when it embeds a profile."""
    with Image.open(path) as im:
        icc = im.info.get("icc_profile")
        img = ImageOps.exif_transpose(im).convert("RGB")
    if icc:
        try:
            img = ImageCms.profileToProfile(
                img, ImageCms.ImageCmsProfile(io.BytesIO(icc)), _SRGB, outputMode="RGB"
            )
        except (OSError, ImageCms.PyCMSError) as exc:
            log.debug(f"{path.name}: ignoring embedded color profile ({exc})")
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def load_frame(project: Project, source: Source, t: float = 0.0, *, width: int | None = None) -> np.ndarray:
    """Full-quality BGR frame of a catalog source, optionally downscaled to ≤ width.

    Video frames are extracted losslessly once and cached in work/frames/.
    """
    path = project.footage_dir / source.path
    if source.kind == "photo":
        img = _read_photo(path)
    else:
        cached = project.work_dir / "frames" / f"{source.id}-{round(t * 1000):09d}.png"
        if not cached.exists():
            tmp = cached.with_suffix(".tmp.png")
            ffmpeg.extract_frame(path, t, tmp)
            os.replace(tmp, cached)
        img = cv2.imread(str(cached), cv2.IMREAD_COLOR)
        if img is None:
            raise OSError(f"could not read extracted frame {cached}")
    if width and img.shape[1] > width:
        img = cv2.resize(
            img, (width, round(img.shape[0] * width / img.shape[1])), interpolation=cv2.INTER_AREA
        )
    return img


def _sharpest_first(cand: Candidate) -> tuple[float, float, str]:
    return -cand.sharpness, cand.t, cand.source


def _read_candidate(project: Project, cand: Candidate) -> np.ndarray | None:
    img = cv2.imread(str(project.work_dir / cand.file), cv2.IMREAD_COLOR)
    if img is None:
        log.warn(f"missing candidate frame {cand.file}")
    return img


def _scoring_settings(base: AlignSettings) -> AlignSettings:
    return base.model_copy(
        update={
            "method": "auto" if base.method == "none" else base.method,
            "detect_width": SELECT_WIDTH,
            "max_features": min(base.max_features, SELECT_FEATURES),
            "ecc_refine": False,
            "min_inliers": max(15, base.min_inliers // 3),
        }
    )


def _near_hint(cand: Candidate, src: Source, hint: VantageHint | None, radius_m: float) -> bool:
    """False only when telemetry is known and places the frame away from the hint."""
    if hint is None:
        return True
    lat = cand.lat if cand.lat is not None else src.lat
    lon = cand.lon if cand.lon is not None else src.lon
    if (
        hint.lat is not None
        and hint.lon is not None
        and lat is not None
        and lon is not None
        and haversine_m(hint.lat, hint.lon, lat, lon) > radius_m
    ):
        return False
    heading = cand.heading_deg if cand.heading_deg is not None else src.heading_deg
    if hint.heading_deg is not None and heading is not None:
        diff = abs((heading - hint.heading_deg + 180.0) % 360.0 - 180.0)
        return diff <= HEADING_TOLERANCE_DEG
    return True


def _source_for(catalog: Catalog, ref: FrameRef, what: str) -> Source:
    try:
        return catalog.source_by_path(ref.source)
    except KeyError:
        raise ValueError(
            f"{what}: {ref.source!r} is not in the catalog (path relative to footage_dir)"
        ) from None


class _VantageSelector:
    """Selection state for one vantage: reference proxy image + cached features."""

    def __init__(
        self, project: Project, catalog: Catalog, by_date: dict[str, list[Candidate]], vantage: Vantage
    ):
        self.project, self.catalog, self.by_date, self.vantage = project, catalog, by_date, vantage
        self.sources = {s.id: s for s in catalog.sources}
        self.settings = _scoring_settings(project.config.align)
        self.reference, self.ref_img = self._reference()
        self.ref_features = align.detect(self.ref_img, self.settings)

    def _plausible(self, cands: list[Candidate]) -> list[Candidate]:
        radius = self.project.config.select.gps_radius_m
        near = [c for c in cands if _near_hint(c, self.sources[c.source], self.vantage.hint, radius)]
        if not near:
            log.debug(f"{self.vantage.id}: no candidates match the telemetry hint; using all {len(cands)}")
        return near or cands

    def _reference(self) -> tuple[FramePick, np.ndarray]:
        ref = self.vantage.reference
        if ref is not None:
            src = _source_for(self.catalog, ref, f"vantage {self.vantage.id!r} reference")
            t = ref.t if src.kind == "video" else 0.0
            own = [c for c in self.catalog.candidates if c.source == src.id]
            near = min(own, key=lambda c: (abs(c.t - t), c.t), default=None)
            img = None
            if near is not None and abs(near.t - t) <= REF_T_TOLERANCE_S:
                img = _read_candidate(self.project, near)
            if img is None:
                img = load_frame(self.project, src, t, width=REF_PROXY_WIDTH)
            return FramePick(source=src.id, t=t, date=src.date, score=1.0, manual=True), img
        if not self.by_date:
            raise ValueError(f"vantage {self.vantage.id!r}: the catalog has no candidate frames")
        latest = max(self.by_date)
        for best in sorted(self._plausible(self.by_date[latest]), key=_sharpest_first):
            img = _read_candidate(self.project, best)
            if img is not None:
                return FramePick(source=best.source, t=best.t, date=latest, score=1.0), img
        raise ValueError(f"vantage {self.vantage.id!r}: no readable candidate frames for {latest}")

    def manual(self, date: str, ref: FrameRef) -> FramePick:
        src = _source_for(self.catalog, ref, f"vantage {self.vantage.id!r} pick for {date}")
        t = ref.t if src.kind == "video" else 0.0
        return FramePick(source=src.id, t=t, date=date, score=1.0, manual=True)

    def _score(self, cand: Candidate) -> tuple[float, int, Candidate] | None:
        """Score in [0, 1): match strength x share of the reference covered x inlier ratio."""
        img = _read_candidate(self.project, cand)
        if img is None:
            return None
        reg = align.register(self.ref_img, img, self.settings, ref_features=self.ref_features)
        if not reg.ok:
            return None
        ref_size = (self.ref_img.shape[1], self.ref_img.shape[0])
        overlap = align.coverage(reg.H, (img.shape[1], img.shape[0]), ref_size)
        strength = reg.inliers / (reg.inliers + self.settings.min_inliers)
        return strength * overlap * (0.5 + 0.5 * reg.inlier_ratio), reg.inliers, cand

    def best(self, date: str) -> FramePick | None:
        """Register the date's sharpest plausible candidates; None when nothing matches."""
        pool = sorted(self._plausible(self.by_date.get(date, [])), key=_sharpest_first)[:TOP_N]
        with ThreadPoolExecutor(WORKERS) as executor:  # OpenCV releases the GIL
            scored = [s for s in executor.map(self._score, pool) if s is not None]
        if not scored:
            return None
        top = max(s for s, _, _ in scored)
        score, inliers, cand = min(
            (x for x in scored if x[0] >= top - SCORE_TIE), key=lambda x: _sharpest_first(x[2])
        )
        return FramePick(source=cand.source, t=cand.t, date=date, score=round(score, 4), inliers=inliers)


def select_frames(project: Project, catalog: Catalog | None = None) -> Selection:
    """Choose the reference and one frame per visit for every vantage; writes work/selection.json."""
    if catalog is None:
        catalog = Catalog.load(project.work_dir / "catalog.json")
    excluded = {c.date.isoformat() for c in project.story.captures if c.exclude}
    sources = {s.id: s for s in catalog.sources}
    by_date: dict[str, list[Candidate]] = defaultdict(list)
    for cand in catalog.candidates:
        src = sources.get(cand.source)
        if src is not None and src.date not in excluded:
            by_date[src.date].append(cand)

    selection = Selection()
    seen_refs: dict[tuple[str, float], str] = {}
    for vantage in project.story.vantages:
        with log.step(f"select {vantage.id}"):
            sel = _VantageSelector(project, catalog, by_date, vantage)
            ref = sel.reference
            key = (ref.source, ref.t)
            if key in seen_refs:
                log.warn(
                    f"vantages {seen_refs[key]!r} and {vantage.id!r} share a reference; add a hint or reference"
                )
            seen_refs.setdefault(key, vantage.id)
            picks: dict[str, FramePick] = {}
            for date in sorted((set(by_date) | set(vantage.picks) | {ref.date}) - excluded):
                if date in vantage.picks:
                    picks[date] = sel.manual(date, vantage.picks[date])
                elif date == ref.date:
                    picks[date] = ref
                elif (pick := sel.best(date)) is not None:
                    picks[date] = pick
                else:
                    log.warn(
                        f"{vantage.id}: nothing on {date} registers to the reference; skipping that visit"
                    )
            selection.vantages[vantage.id] = VantageSelection(reference=ref, picks=picks)
            log.info(f"{vantage.id}: {len(picks)} visits, reference {ref.date} ({sources[ref.source].path})")
    selection.save(project.work_dir / "selection.json")
    return selection
