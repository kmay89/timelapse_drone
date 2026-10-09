"""Photometric normalization so visits shot in different light read as one sequence.

Statistics are transferred in CIE Lab (float32): L carries exposure/contrast,
a/b carry white balance. `strength` blends between each flight's own look (0)
and a full match to the reference (1).
"""

from __future__ import annotations

from typing import Literal

import cv2
import numpy as np

GradeMode = Literal["none", "reinhard", "histogram"]

MIN_STD = 1e-3  # below this a channel is flat; only its mean is moved
MAX_GAIN = 4.0  # cap on per-channel contrast stretch/squeeze
MAX_SAMPLES = 250_000  # pixels used for statistics (strided subsample)
QUANTILES = 129  # knots of the histogram-matching curve
LUT_SIZE = 4096  # samples of that curve per Lab channel
CHROMA_HISTOGRAM_WEIGHT = 0.5  # a/b mix histogram mapping with mean/std transfer, for gentler color
LAB_RANGE = ((0.0, 100.0), (-127.0, 127.0), (-127.0, 127.0))


def _to_lab(img_bgr: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_bgr.astype(np.float32) * np.float32(1 / 255), cv2.COLOR_BGR2Lab)


def _to_bgr(lab: np.ndarray) -> np.ndarray:
    bgr = cv2.cvtColor(lab, cv2.COLOR_Lab2BGR)
    return np.clip(bgr * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _samples(lab: np.ndarray, mask: np.ndarray | None) -> np.ndarray:
    """(N, 3) float64 Lab samples under the mask, deterministically strided to ≤ MAX_SAMPLES."""
    px = lab.reshape(-1, 3) if mask is None else lab[np.asarray(mask, bool)]
    step = max(1, -(-len(px) // MAX_SAMPLES))
    return px[::step].astype(np.float64)


def _reinhard(lab: np.ndarray, src: np.ndarray, ref: np.ndarray) -> np.ndarray:
    mu_s, sd_s = src.mean(axis=0), src.std(axis=0)
    mu_r, sd_r = ref.mean(axis=0), ref.std(axis=0)
    gain = np.where(sd_s > MIN_STD, sd_r / np.maximum(sd_s, MIN_STD), 1.0)
    gain = np.clip(gain, 1 / MAX_GAIN, MAX_GAIN)
    return lab * gain.astype(np.float32) + (mu_r - mu_s * gain).astype(np.float32)


def _increasing(knots: np.ndarray) -> np.ndarray:
    return np.maximum.accumulate(knots) + np.arange(len(knots)) * 1e-6


def _apply_curve(x: np.ndarray, xs: np.ndarray, ys: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """np.interp(x, xs, ys) through a dense lookup table over [lo, hi] (far faster on megapixels)."""
    lut = np.interp(np.linspace(lo, hi, LUT_SIZE), xs, ys).astype(np.float32)
    idx = (x - np.float32(lo)) * np.float32((LUT_SIZE - 1) / (hi - lo)) + np.float32(0.5)
    return lut[np.clip(idx, 0, LUT_SIZE - 1).astype(np.intp)]


def _histogram(lab: np.ndarray, src: np.ndarray, ref: np.ndarray) -> np.ndarray:
    q = np.linspace(0.0, 1.0, QUANTILES)
    smooth = _reinhard(lab, src, ref)
    out = np.empty_like(lab)
    for c, (lo, hi) in enumerate(LAB_RANGE):
        xs = _increasing(np.quantile(src[:, c], q))
        ys = np.maximum.accumulate(np.quantile(ref[:, c], q))
        mapped = _apply_curve(lab[..., c], xs, ys, lo, hi)
        w = 1.0 if c == 0 else CHROMA_HISTOGRAM_WEIGHT
        out[..., c] = w * mapped + (1 - w) * smooth[..., c]
    return out


def match_color(
    img_bgr: np.ndarray,
    ref_bgr: np.ndarray,
    mode: GradeMode = "reinhard",
    strength: float = 1.0,
    mask: np.ndarray | None = None,
    *,
    ref_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Match img's color statistics to ref's and return a new uint8 BGR image.

    `mask` selects the pixels whose statistics are compared (e.g. the region both
    frames show); it also applies to ref when ref has the same size, unless
    `ref_mask` is given. strength=0 or mode="none" returns an unchanged copy.
    """
    strength = float(np.clip(strength, 0.0, 1.0))
    if mode == "none" or strength == 0.0:
        return img_bgr.copy()
    if ref_mask is None and mask is not None and ref_bgr.shape[:2] == img_bgr.shape[:2]:
        ref_mask = mask
    lab, ref_lab = _to_lab(img_bgr), _to_lab(ref_bgr)
    src, ref = _samples(lab, mask), _samples(ref_lab, ref_mask)
    if len(src) < 16 or len(ref) < 16:
        return img_bgr.copy()
    matched = _reinhard(lab, src, ref) if mode == "reinhard" else _histogram(lab, src, ref)
    for c, (lo, hi) in enumerate(LAB_RANGE):
        np.clip(matched[..., c], lo, hi, out=matched[..., c])
    return _to_bgr(lab + np.float32(strength) * (matched - lab))
