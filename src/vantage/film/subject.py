"""Where a step's subject is on screen, so its words can sit clear of it (film/subject.py).

A step's subject, as a map over its master (`subject_map`, summing to 1): Gaussian spots of equal
mass on the step's focus point and on the hotspots that changed since the previous step (POINT_WEIGHT
of the mass; for a pushed step PUSH_WEIGHT, and its focus spot as wide as what the push frames,
`focus_sigma`, so its words stay off what the camera closes in on), plus the region that actually changed between the previous capture and this one (the rest):
the per-pixel CIELAB difference of the two masters at SEEN_WIDTH px wide, blurred by CHANGE_SIGMA of
the width, less its median (a change everywhere, like a new season's light, frames nothing), clipped
at zero. Without a previous capture, or when nothing changed more than the median, the spots carry
all the mass; without either, there is no subject.

`under` projects the map through a camera path (the crops it shows, as master fractions) and gives
how much of the subject on screen a box in frame fractions covers for its size (1: as much as
anywhere on screen, 0: none of it); `contrast` the least
contrast ratio (WCAG 1.4.3) of a text colour over the picture under a box, behind a scrim, against
the 95th-percentile luminance under the box, along the same path. Everything is computed in float64
from 8-bit masters resized with INTER_AREA, so the same masters give the same numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import cv2
import numpy as np

SEEN_WIDTH = 256  # px: masters are measured at this width
CHANGE_SIGMA = 0.02  # of the width: the change map's blur
POINT_SIGMA = 0.03  # of the width: a focus point's or hotspot's spot
# A push frames its focus: the focus spot's sigma is this share of the pushed crop's shorter side, so
# words stay clear of what the push closes in on (a round plaza, a pier), not only its centre.
FOCUS_SHARE = 0.12
POINT_WEIGHT = 0.5  # share of the subject's mass on its points (the rest on the change), when both exist
PUSH_WEIGHT = 0.75  # the same for a pushed step: what the push closes in on is most of its subject

Rect = tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions (of the master, or of the frame)


class Seen:
    """Masters at SEEN_WIDTH px wide (CIELAB and relative luminance), read once per plan."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._img: dict[str, np.ndarray] = {}

    def image(self, file: str) -> np.ndarray:
        """The master as 8-bit BGR, SEEN_WIDTH px wide."""
        if file not in self._img:
            im = cv2.imread(str(self.root / file), cv2.IMREAD_COLOR)
            if im is None:
                raise FileNotFoundError(f"cannot read master {self.root / file}")
            h = max(1, round(SEEN_WIDTH * im.shape[0] / im.shape[1]))
            self._img[file] = cv2.resize(im, (SEEN_WIDTH, h), interpolation=cv2.INTER_AREA)
        return self._img[file]

    def lab(self, file: str) -> np.ndarray:
        return cv2.cvtColor(self.image(file), cv2.COLOR_BGR2LAB).astype(np.float64)

    def change(self, before: str, after: str) -> np.ndarray | None:
        """Where `after` differs from `before`, as a map summing to 1 (None: nothing stands out)."""
        a, b = self.lab(before), self.lab(after)
        if a.shape != b.shape:
            return None
        d = np.abs(a - b).sum(axis=2)
        d = cv2.GaussianBlur(d, (0, 0), CHANGE_SIGMA * SEEN_WIDTH)
        d = np.maximum(d - np.median(d), 0.0)
        total = float(d.sum())
        return d / total if total > 1e-9 else None


Point = tuple[float, float] | tuple[float, float, float]  # x, y (master fractions)[, sigma of the width]


def focus_sigma(crop: Rect, aspect: float) -> float:
    """The focus spot's sigma (of the master's width) for a push that ends on `crop` (master
    fractions) of a master `aspect` (height / width) high: FOCUS_SHARE of the crop's shorter side,
    never under POINT_SIGMA."""
    return max(POINT_SIGMA, FOCUS_SHARE * min(crop[2] - crop[0], (crop[3] - crop[1]) * aspect))


def _spots(shape: tuple[int, int], points: Sequence[Point]) -> np.ndarray | None:
    """Gaussian spots of equal mass on `points` (each its own sigma, else POINT_SIGMA)."""
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    m = np.zeros((h, w))
    for point in points:
        x, y = point[0], point[1]
        sigma = (point[2] if len(point) > 2 else POINT_SIGMA) * w  # type: ignore[misc]
        spot = np.exp(-(((xx + 0.5 - x * w) ** 2 + (yy + 0.5 - y * h) ** 2) / (2 * sigma * sigma)))
        mass = float(spot.sum())
        if mass > 1e-12:
            m += spot / mass
    total = float(m.sum())
    return m / total if total > 1e-12 else None


def subject_map(
    seen: Seen, file: str, before: str | None, points: Sequence[Point], weight: float = POINT_WEIGHT
) -> np.ndarray | None:
    """The step's subject over its master `file` (see the module docstring), or None without one;
    `weight`: the points' share of it when there is a change too (PUSH_WEIGHT for a pushed step)."""
    shape = seen.image(file).shape[:2]
    spots = _spots((int(shape[0]), int(shape[1])), points)
    change = seen.change(before, file) if before else None
    if spots is not None and change is not None:
        return weight * spots + (1 - weight) * change
    return spots if spots is not None else change


def _sum(integral: np.ndarray, r: Rect) -> float:
    """The map's sum over a rectangle in master fractions (whole cells, clamped to the map)."""
    h, w = integral.shape[0] - 1, integral.shape[1] - 1
    x0, x1 = (min(max(round(v * w), 0), w) for v in (r[0], r[2]))
    y0, y1 = (min(max(round(v * h), 0), h) for v in (r[1], r[3]))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return float(integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0])


def _inside(crop: Rect, box: Rect) -> Rect:
    """A box in frame fractions as master fractions, for a crop of the master."""
    cw, ch = crop[2] - crop[0], crop[3] - crop[1]
    return crop[0] + box[0] * cw, crop[1] + box[1] * ch, crop[0] + box[2] * cw, crop[1] + box[3] * ch


def under(subject: np.ndarray | None, crops: Sequence[Rect], box: Rect) -> float:
    """How much of the subject on screen `box` (frame fractions) covers, for its size: the mean over
    the crops a camera shows of (the subject's share under the box) / (the box's share of the
    frame). 1 is a box over as much subject as anywhere on screen, 0 a box clear of it (and 0
    without a subject, or when none of it is on screen)."""
    area = max(0.0, min(box[2], 1.0) - max(box[0], 0.0)) * max(0.0, min(box[3], 1.0) - max(box[1], 0.0))
    if subject is None or not crops or area <= 0:
        return 0.0
    integral = cv2.integral(subject, sdepth=cv2.CV_64F)
    shares = []
    for crop in crops:
        seen = _sum(integral, crop)
        shares.append(_sum(integral, _inside(crop, box)) / seen if seen > 1e-12 else 0.0)
    return float(np.mean(shares)) / area


def luminance(rgb: np.ndarray) -> np.ndarray:
    """WCAG relative luminance of 8-bit sRGB values (last axis R, G, B)."""
    c = rgb.astype(np.float64) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return lin[..., 0] * 0.2126 + lin[..., 1] * 0.7152 + lin[..., 2] * 0.0722


def contrast(
    seen: Seen,
    file: str,
    crops: Sequence[Rect],
    box: Rect,
    ink: Sequence[int],
    scrim: Sequence[int],
    alpha: float,
) -> float:
    """The least contrast ratio of `ink` (RGB) over the picture under `box` (frame fractions) behind
    a `scrim` (RGB) of `alpha`, mixed in display gamma as film/cut.py does, against the 95th
    percentile luminance under the box, over the crops a camera shows."""
    img = seen.image(file)[..., ::-1].astype(np.float64)  # RGB
    h, w = img.shape[:2]
    li = float(luminance(np.array(ink, np.float64)))
    worst = float("inf")
    for crop in crops:
        r = _inside(crop, box)
        x0, x1 = min(max(int(r[0] * w), 0), w - 1), min(max(int(np.ceil(r[2] * w)), 1), w)
        y0, y1 = min(max(int(r[1] * h), 0), h - 1), min(max(int(np.ceil(r[3] * h)), 1), h)
        if x1 <= x0 or y1 <= y0:
            continue
        mixed = img[y0:y1, x0:x1] * (1 - alpha) + np.array(scrim, np.float64) * alpha
        lb = float(np.percentile(luminance(mixed), 95))
        hi, lo = max(li, lb), min(li, lb)
        worst = min(worst, (hi + 0.05) / (lo + 0.05))
    return worst if worst != float("inf") else 21.0


__all__ = ["Seen", "contrast", "focus_sigma", "luminance", "subject_map", "under"]
