"""Aligned, cropped and graded masters for every vantage.

For each vantage: register every selected frame to the full-resolution
reference (or, when the site changed too much for that, to the registered visit
nearest in time, composing the transforms), intersect their valid regions, crop
all of them to the largest common rectangle, color-match to the reference and
write

    masters/<vantage>/<date>.jpg      (sRGB, progressive, ≤ master_width)
    masters/index.json                (MastersIndex)
    work/review/<vantage>/            contact.jpg, <date>.jpg overlays, review.json

Frames are streamed twice (register, then render) so memory stays bounded by
one full-resolution frame plus the reference, whatever the number of visits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageCms

from vantage import log
from vantage.config import ISO_DATE_RE, VANTAGE_ID_RE, Project, Vantage
from vantage.models import (
    AlignInfo,
    Catalog,
    FramePick,
    MasterCapture,
    MastersIndex,
    MastersVantage,
    Selection,
    Source,
    VantageSelection,
)
from vantage.process import align, grade
from vantage.process.select import chain_by_date, load_frame

JPEG_QUALITY = 92
MIN_OVERLAP = (
    0.5  # automatic picks covering less of the reference are dropped; manual ones don't shape the crop
)
MIN_RETAINED = 0.5  # warn when the common crop keeps less of the reference than this
REVIEW_WIDTH = 960
THUMB_WIDTH = 480
CONTACT_COLUMNS = 4
CHECKER_TILES = 12

_SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


@dataclass
class _Capture:
    date: str
    pick: FramePick
    source: Source
    shape: tuple[int, ...]  # full-resolution frame shape
    H: np.ndarray  # frame → reference pixels
    info: AlignInfo
    overlap: float  # share of the reference the warped frame covers
    included: bool = True
    features: align.Features | None = None  # kept while chaining

    @property
    def registered(self) -> bool:
        return self.info.ok and self.overlap >= MIN_OVERLAP


def _write_jpeg(path: Path, bgr: np.ndarray) -> None:
    rgb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    rgb.save(path, "JPEG", quality=JPEG_QUALITY, progressive=True, optimize=True, icc_profile=_SRGB_ICC)


def _fit_width(img: np.ndarray, width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w == width:
        return img
    return cv2.resize(img, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_AREA)


def _label(img: np.ndarray, text: str) -> np.ndarray:
    """Draw white text on a dark box in the bottom-left corner (in place)."""
    font, scale = cv2.FONT_HERSHEY_SIMPLEX, max(0.45, img.shape[1] / 1100)
    thick, pad = max(1, round(scale * 1.6)), round(8 * scale)
    (tw, th), base = cv2.getTextSize(text, font, scale, thick)
    bottom = img.shape[0]
    cv2.rectangle(img, (0, bottom - th - base - 2 * pad), (tw + 2 * pad, bottom), (12, 12, 12), -1)
    cv2.putText(img, text, (pad, bottom - base - pad), font, scale, (255, 255, 255), thick, cv2.LINE_AA)
    return img


def _overlay(ref: np.ndarray, cap: np.ndarray, date: str) -> np.ndarray:
    """Checkerboard (top) and red/cyan anaglyph (bottom): misalignment shows as broken edges/fringes."""
    ref, cap = _fit_width(ref, REVIEW_WIDTH), _fit_width(cap, REVIEW_WIDTH)
    h, w = ref.shape[:2]
    tile = max(8, w // CHECKER_TILES)
    yy, xx = np.indices((h, w))
    checker = np.where(((yy // tile + xx // tile) % 2 == 0)[..., None], ref, cap)
    g_ref, g_cap = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY), cv2.cvtColor(cap, cv2.COLOR_BGR2GRAY)
    anaglyph = cv2.merge([g_cap, g_cap, g_ref])
    _label(checker, f"checkerboard: reference / {date}")
    _label(anaglyph, f"red = reference, cyan = {date}")
    return np.vstack([checker, np.zeros((8, w, 3), np.uint8), anaglyph])


def _contact_sheet(tiles: list[tuple[str, np.ndarray]]) -> np.ndarray:
    thumbs = [_label(_fit_width(img, THUMB_WIDTH).copy(), text) for text, img in tiles]
    th, tw = thumbs[0].shape[:2]
    cols = min(CONTACT_COLUMNS, len(thumbs))
    rows = -(-len(thumbs) // cols)
    gap = 6
    sheet = np.full((rows * (th + gap) + gap, cols * (tw + gap) + gap, 3), 24, np.uint8)
    for i, thumb in enumerate(thumbs):
        r, c = divmod(i, cols)
        y, x = gap + r * (th + gap), gap + c * (tw + gap)
        sheet[y : y + th, x : x + tw] = thumb[:th, :tw]
    return sheet


def _register_all(
    project: Project, vs: VantageSelection, sources: dict[str, Source], ref: np.ndarray
) -> tuple[list[_Capture], np.ndarray]:
    """Register every pick to the reference; returns captures + intersection of included valid masks."""
    settings = project.config.align
    ref_size = (ref.shape[1], ref.shape[0])
    features = align.detect(ref, settings) if settings.method != "none" else None
    captures: list[_Capture] = []
    for date, pick in sorted(vs.picks.items()):
        src = sources[pick.source]
        if pick.source == vs.reference.source and abs(pick.t - vs.reference.t) < 1e-3:
            captures.append(
                _Capture(date, pick, src, ref.shape, np.eye(3), AlignInfo(method="reference"), 1.0)
            )
            continue
        img = load_frame(project, src, pick.t)
        own = align.detect(img, settings) if features is not None else None
        reg = align.register(ref, img, settings, ref_features=features, img_features=own)
        overlap = float(align.valid_mask(img.shape, reg.H, ref_size).mean())
        captures.append(_Capture(date, pick, src, img.shape, reg.H, reg.info(), overlap, features=own))
    if features is not None:
        _chain(project, captures, ref_size)
    for cap in captures:
        cap.features = None

    common = np.ones(ref.shape[:2], bool)
    for cap in captures:
        info = cap.info
        problem = None if info.ok else f"registration failed ({info.note or 'too few inliers'})"
        if problem is None and cap.overlap < MIN_OVERLAP:
            problem = f"covers only {cap.overlap:.0%} of the reference"
        cap.included = problem is None or cap.pick.manual
        if problem and cap.included:
            log.warn(f"{cap.date}: manual pick kept although {problem}")
            cap.info = info.model_copy(update={"ok": False, "note": f"manual pick kept: {problem}"})
        elif problem:
            log.warn(f"{cap.date}: excluded, {problem}")
            cap.info = info.model_copy(update={"ok": False, "note": f"excluded: {problem}"})
        # Only registered frames shape the crop: a stray manual pick must not shrink every master.
        if cap.registered and cap.info.method != "reference":
            common &= align.valid_mask(cap.shape, cap.H, ref_size)
        log.debug(f"{cap.date}: {info.method} inliers={info.inliers} rmse={info.rmse_px}px ecc={info.ecc}")
    return captures, common


def _chain(project: Project, captures: list[_Capture], ref_size: tuple[int, int]) -> None:
    """Register captures that miss the reference to registered captures nearest in time (in place)."""
    by_date = {c.date: c for c in captures}
    anchors = [c.date for c in captures if c.registered and c.info.method != "reference"]
    pending = [c.date for c in captures if not c.info.ok]

    def attempt(date: str, via: str) -> bool:
        cap, stone = by_date[date], by_date[via]
        stone_img = load_frame(project, stone.source, stone.pick.t)
        img = load_frame(project, cap.source, cap.pick.t)
        reg = align.register(
            stone_img, img, project.config.align, ref_features=stone.features, img_features=cap.features
        )
        if not reg.ok:
            return False
        H = stone.H @ reg.H
        H /= H[2, 2]
        overlap = float(align.valid_mask(img.shape, H, ref_size).mean())
        if overlap < MIN_OVERLAP:
            return False
        note = "; ".join(filter(None, [f"chained via {via}", reg.note]))
        cap.H, cap.overlap, cap.info = H, overlap, reg.info().model_copy(update={"note": note})
        log.info(f"{date}: registered via {via} ({reg.inliers} inliers)")
        return True

    if pending and anchors:
        chain_by_date(pending, anchors, attempt)


def _make_vantage(
    project: Project, vantage: Vantage, vs: VantageSelection, sources: dict[str, Source]
) -> MastersVantage | None:
    ref_src = sources[vs.reference.source]
    ref = load_frame(project, ref_src, vs.reference.t)
    ref_h, ref_w = ref.shape[:2]
    captures, common = _register_all(project, vs, sources, ref)
    kept = [c for c in captures if c.included]
    review_dir = project.work_dir / "review" / vantage.id
    review_dir.mkdir(parents=True, exist_ok=True)
    for old in review_dir.glob("*.jpg"):
        old.unlink()
    if not kept:
        log.warn(f"{vantage.id}: no capture survived registration")
        return None

    aspect = project.config.align.output_aspect or ref_w / ref_h
    x, y, w, h = align.common_crop([common], aspect)
    retained = w * h / (ref_w * ref_h)
    if retained < MIN_RETAINED:
        log.warn(f"{vantage.id}: common crop keeps only {retained:.0%} of the reference; check review sheets")
    out_w = min(project.config.output.images.master_width, w)
    out_h = max(1, round(out_w * h / w))
    to_crop = np.array([[1.0, 0.0, -x], [0.0, 1.0, -y], [0.0, 0.0, 1.0]])
    ref_master = cv2.resize(ref[y : y + h, x : x + w], (out_w, out_h), interpolation=cv2.INTER_AREA)

    out_dir = project.masters_dir / vantage.id
    out_dir.mkdir(parents=True, exist_ok=True)
    gs = project.config.grade
    entries: list[MasterCapture] = []
    tiles: list[tuple[str, np.ndarray]] = []
    for cap in kept:
        if cap.info.method == "reference":
            master = ref_master
        else:
            warped, valid = align.warp(load_frame(project, cap.source, cap.pick.t), to_crop @ cap.H, (w, h))
            master = cv2.resize(warped, (out_w, out_h), interpolation=cv2.INTER_AREA)
            valid = cv2.resize(valid.view(np.uint8), (out_w, out_h), interpolation=cv2.INTER_NEAREST) > 0
            master = grade.match_color(master, ref_master, gs.mode, gs.strength, valid)
        _write_jpeg(out_dir / f"{cap.date}.jpg", master)
        cv2.imwrite(str(review_dir / f"{cap.date}.jpg"), _overlay(ref_master, master, cap.date))
        flag = "" if cap.info.ok else "  (check)"
        tiles.append((f"{cap.date}{' manual' if cap.pick.manual else ''}{flag}", master))
        entries.append(
            MasterCapture(
                date=cap.date,
                file=f"{vantage.id}/{cap.date}.jpg",
                source=cap.pick.source,
                t=cap.pick.t,
                align=cap.info,
            )
        )
    written = {f"{c.date}.jpg" for c in kept}
    for stale in out_dir.glob("*.jpg"):
        if stale.name not in written:
            stale.unlink()
            log.info(f"{vantage.id}: removed stale master {stale.name}")

    cv2.imwrite(str(review_dir / "contact.jpg"), _contact_sheet(tiles))
    review: dict[str, Any] = {
        "vantage": vantage.id,
        "reference": {
            "date": vs.reference.date,
            "source": ref_src.id,
            "path": ref_src.path,
            "t": vs.reference.t,
            "width": ref_w,
            "height": ref_h,
        },
        "crop": {"x": x, "y": y, "w": w, "h": h, "retained": round(retained, 4)},
        "master": {"width": out_w, "height": out_h},
        "captures": [
            {
                "date": c.date,
                "source": c.source.id,
                "path": c.source.path,
                "t": c.pick.t,
                "manual": c.pick.manual,
                "included": c.included,
                "overlap": round(c.overlap, 4),
                "align": c.info.model_dump(exclude_none=True),
                "master": f"{vantage.id}/{c.date}.jpg" if c.included else None,
                "review": f"{c.date}.jpg" if c.included else None,
            }
            for c in captures
        ],
    }
    (review_dir / "review.json").write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")
    log.info(
        f"{vantage.id}: {len(kept)}/{len(captures)} masters at {out_w}×{out_h}, crop keeps {retained:.0%}"
    )
    return MastersVantage(name=vantage.name, width=out_w, height=out_h, captures=entries)


def _check_names(vantage: Vantage, vs: VantageSelection | None) -> None:
    """Refuse an id or date that would write or clear files outside masters/<id>/ and work/review/<id>/.

    story.yaml is checked when it loads; this also covers a hand-edited work/selection.json.
    """
    if not VANTAGE_ID_RE.fullmatch(vantage.id):
        raise ValueError(f"vantage id {vantage.id!r} is not a plain folder name (a-z, 0-9, '-', '_')")
    bad = sorted(d for d in (vs.picks if vs else {}) if not ISO_DATE_RE.fullmatch(d))
    if bad:
        raise ValueError(
            f"{vantage.id}: selection picks {bad} are not YYYY-MM-DD dates; re-run `vantage select`"
        )


def make_masters(project: Project, selection: Selection | None = None) -> MastersIndex:
    """Write masters/<vantage>/<date>.jpg, masters/index.json and work/review/** for every vantage."""
    if selection is None:
        selection = Selection.load(project.work_dir / "selection.json")
    catalog = Catalog.load(project.work_dir / "catalog.json")
    sources = {s.id: s for s in catalog.sources}
    for vantage in project.story.vantages:
        _check_names(vantage, selection.vantages.get(vantage.id))
    index = MastersIndex()
    for vantage in project.story.vantages:
        vs = selection.vantages.get(vantage.id)
        if vs is None or not vs.picks:
            log.warn(f"{vantage.id}: nothing selected; run `vantage select` first")
            continue
        missing = {p.source for p in [vs.reference, *vs.picks.values()]} - sources.keys()
        if missing:
            raise ValueError(
                f"{vantage.id}: selection references sources missing from the catalog: {sorted(missing)}"
            )
        with log.step(f"masters {vantage.id}"):
            result = _make_vantage(project, vantage, vs, sources)
        if result is not None:
            index.vantages[vantage.id] = result
    index.save(project.masters_dir / "index.json")
    return index
