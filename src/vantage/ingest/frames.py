"""Candidate frames: sampling videos and stills, sharpness scoring, contact sheets.

Candidates are ≤1600-px JPEG proxies that the select stage scores and
registers; contact sheets are per-date grids of the sharpest candidates that a
human (or an LLM) reads to pick each vantage's reference frame.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path

import cv2
import numpy as np
from PIL import ExifTags, Image, ImageOps

from vantage.media import ffmpeg
from vantage.models import Candidate, Catalog

CANDIDATE_WIDTH = 1600
SHARPNESS_WIDTH = 800
JPEG_QUALITY = 88
CONTACT_TILES = 12
CONTACT_COLS = 4
CONTACT_TILE_W = 400

_BG = (17, 17, 17)
_FG = (235, 235, 235)
_DIM = (150, 150, 150)
_FONT = cv2.FONT_HERSHEY_SIMPLEX


def sharpness(img: np.ndarray, *, width: int = SHARPNESS_WIDTH) -> float:
    """Variance of the Laplacian of a `width`-px-wide grayscale copy (higher is crisper)."""
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    if w != width:
        interp = cv2.INTER_AREA if w > width else cv2.INTER_LINEAR
        gray = cv2.resize(gray, (width, max(1, round(h * width / w))), interpolation=interp)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def sample_video(
    path: Path, *, every_s: float, max_frames: int, duration_s: float, width: int = CANDIDATE_WIDTH
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield (t, BGR frame) about every `every_s` seconds, widened so at most `max_frames` span the clip."""
    step = max(every_s, duration_s / max_frames) if duration_s > 0 and max_frames > 0 else every_s
    count = 0
    for t, frame in ffmpeg.iter_frames(path, every_s=step, width=width):
        if count >= max_frames:
            break
        count += 1
        yield t, frame
    if count == 0:  # clip shorter than one sampling interval: take its first frame
        for _, frame in ffmpeg.iter_frames(path, width=width):
            yield 0.0, frame
            break


def load_photo(path: Path, *, width: int = CANDIDATE_WIDTH) -> np.ndarray:
    """Upright (EXIF orientation applied) BGR image no wider than `width`."""
    with Image.open(path) as im:
        if im.format == "JPEG":
            swapped = im.getexif().get(ExifTags.Base.Orientation) in (5, 6, 7, 8)
            scale = width / (im.height if swapped else im.width)
            if scale < 1:  # let libjpeg decode at 1/2, 1/4 or 1/8 size directly
                im.draft("RGB", (math.ceil(im.width * scale), math.ceil(im.height * scale)))
        img = ImageOps.exif_transpose(im).convert("RGB")
    if img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.Resampling.LANCZOS)
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


def write_jpeg(path: Path, img: np.ndarray, *, quality: int = JPEG_QUALITY) -> Path:
    """Write atomically, so an interrupted run never leaves a truncated candidate behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.tmp.jpg")
    if not cv2.imwrite(str(tmp), img, [cv2.IMWRITE_JPEG_QUALITY, quality]):
        raise OSError(f"could not write {path}")
    os.replace(tmp, path)
    return path


def _fit(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Letterbox `img` into a w×h cell."""
    scale = min(w / img.shape[1], h / img.shape[0])
    nw, nh = max(1, round(img.shape[1] * scale)), max(1, round(img.shape[0] * scale))
    cell = np.full((h, w, 3), _BG, np.uint8)
    y, x = (h - nh) // 2, (w - nw) // 2
    cell[y : y + nh, x : x + nw] = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    return cell


def _ascii(text: str) -> str:
    return text.encode("ascii", errors="replace").decode()


def _put(
    img: np.ndarray, text: str, x: int, y: int, scale: float, color: tuple[int, int, int], max_w: int
) -> None:
    text = _ascii(text)
    while len(text) > 4 and cv2.getTextSize(text, _FONT, scale, 1)[0][0] > max_w:
        text = "..." + text[4:]
    cv2.putText(img, text, (x, y), _FONT, scale, color, 1, cv2.LINE_AA)


def contact_sheet(
    tiles: Sequence[tuple[np.ndarray, str]],
    out: Path,
    *,
    title: str,
    cols: int = CONTACT_COLS,
    tile_w: int = CONTACT_TILE_W,
) -> Path:
    """Grid of labelled thumbnails under a title bar, written as a JPEG."""
    gap, label_h, title_h = 8, 26, 44
    tile_h = max(round(img.shape[0] * tile_w / img.shape[1]) for img, _ in tiles) if tiles else tile_w // 2
    cols = max(1, min(cols, len(tiles) or 1))
    rows = max(1, math.ceil(len(tiles) / cols))
    sheet_w = gap + cols * (tile_w + gap)
    sheet = np.full((title_h + rows * (tile_h + label_h + gap) + gap, sheet_w, 3), _BG, np.uint8)
    _put(sheet, title, gap + 4, 29, 0.6, _FG, sheet_w - 2 * gap)
    for i, (img, label) in enumerate(tiles):
        r, c = divmod(i, cols)
        x, y = gap + c * (tile_w + gap), title_h + r * (tile_h + label_h + gap)
        sheet[y : y + tile_h, x : x + tile_w] = _fit(img, tile_w, tile_h)
        _put(sheet, label, x + 4, y + tile_h + 18, 0.45, _DIM, tile_w - 8)
    return write_jpeg(out, sheet, quality=85)


def _diverse_top(cands: Sequence[Candidate], n: int) -> list[Candidate]:
    """Sharpest candidates, round-robin across sources so every flight is represented."""
    by_source: dict[str, list[Candidate]] = defaultdict(list)
    for cand in sorted(cands, key=lambda c: (-c.sharpness, c.source, c.t)):
        by_source[cand.source].append(cand)
    queues = sorted(by_source.values(), key=lambda q: (-q[0].sharpness, q[0].source))
    picked: list[Candidate] = []
    for rank in range(max((len(q) for q in queues), default=0)):
        picked += [q[rank] for q in queues if rank < len(q)]
        if len(picked) >= n:
            break
    return picked[:n]


def write_contact_sheets(catalog: Catalog, work_dir: Path, *, per_date: int = CONTACT_TILES) -> list[Path]:
    """work/contact/<date>.jpg for every flight date; stale sheets are removed."""
    contact_dir = work_dir / "contact"
    sources = {s.id: s for s in catalog.sources}
    by_date: dict[str, list[Candidate]] = defaultdict(list)
    for cand in catalog.candidates:
        by_date[sources[cand.source].date].append(cand)
    written: list[Path] = []
    for date in sorted(by_date):
        cands = by_date[date]
        tiles = []
        for cand in _diverse_top(cands, per_date):
            img = cv2.imread(str(work_dir / cand.file), cv2.IMREAD_COLOR)
            if img is not None:
                src = sources[cand.source]
                label = src.path if src.kind == "photo" else f"{src.path} t={cand.t:g}s"
                tiles.append((img, label))
        n_sources = len({c.source for c in cands})
        title = f"{date}  |  {n_sources} source(s), {len(cands)} candidates  |  sharpest per source first"
        written.append(contact_sheet(tiles, contact_dir / f"{date}.jpg", title=title))
    if contact_dir.is_dir():
        for stale in contact_dir.glob("*.jpg"):
            if stale not in written:
                stale.unlink()
    return written
