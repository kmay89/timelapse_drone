"""Register frames from different visits onto one reference frame.

Pipeline: CLAHE-normalized luminance at `detect_width` → SIFT (RootSIFT), AKAZE or
ORB features → Lowe-ratio matching → USAC-MAGSAC homography (similarity fallback
when the homography is degenerate) → optional ECC refinement at ~1024 px.

Every homography maps *image* pixels to *reference* pixels at full resolution,
using OpenCV's pixel-center convention, so `warp(img, reg.H, ref_size)` lands the
image exactly on the reference's pixel grid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import cv2
import numpy as np

from vantage.config import AlignSettings
from vantage.models import AlignInfo

Method = Literal["homography", "affine", "none"]
Size = tuple[int, int]  # (width, height)

LOWE_RATIO = 0.75
THRESHOLD_FRAC = 0.0015  # robust-fit inlier threshold, as a fraction of the detection width
ECC_WIDTH = 1024
ECC_ITERATIONS = 60
ECC_MAX_SHIFT_FRAC = 0.01  # ECC may move an image corner by at most 1% of the reference width
ECC_MARGIN_FRAC = 0.01  # ignore a thin frame (blur edges, encoder padding) around both images
MAX_PERSPECTIVE = 2.0  # max ratio of the homogeneous coordinate across the image corners
MAX_ANISOTROPY = 2.0  # max singular-value ratio of the local Jacobian at the image center
SCALE_RANGE = (1 / 3, 3.0)
LANCZOS_SUPPORT = 4  # source pixels a Lanczos-4 tap reaches beyond its center
MIN_MODEL_INLIERS = 12  # fewer inliers than this is indistinguishable from chance


@dataclass(frozen=True)
class Features:
    """Keypoints and descriptors of one image at detection resolution."""

    points: np.ndarray  # (N, 2) float32, detection pixels
    descriptors: np.ndarray  # (N, D) float32 RootSIFT or uint8 binary
    binary: bool
    S: np.ndarray  # 3x3: full-resolution pixels → detection pixels
    det_size: Size
    note: str | None = None

    def __len__(self) -> int:
        return len(self.points)


@dataclass
class Registration:
    """Result of registering one image to a reference."""

    H: np.ndarray  # 3x3 float64, image → reference, full-resolution pixels
    method: Method
    inliers: int = 0
    inlier_ratio: float = 0.0
    rmse_px: float = 0.0  # feature reprojection RMSE of the inliers, full-resolution reference pixels
    ecc: float | None = None  # correlation of the final alignment at ECC resolution, when ECC ran
    ok: bool = False
    note: str | None = None

    def info(self) -> AlignInfo:
        return AlignInfo(
            method=self.method,
            inliers=self.inliers,
            inlier_ratio=round(self.inlier_ratio, 4),
            rmse_px=round(self.rmse_px, 3),
            ecc=None if self.ecc is None else round(self.ecc, 4),
            ok=self.ok,
            note=self.note,
        )


# ------------------------------- features --------------------------------- #


def scale_matrix(sx: float, sy: float) -> np.ndarray:
    """Pixel-center-aligned scaling (the convention cv2.resize uses)."""
    return np.array([[sx, 0, 0.5 * sx - 0.5], [0, sy, 0.5 * sy - 0.5], [0, 0, 1]], np.float64)


def _size(img: np.ndarray) -> Size:
    return img.shape[1], img.shape[0]


def _luminance(img: np.ndarray, width: int, *, clahe: bool) -> tuple[np.ndarray, np.ndarray]:
    """Lab luminance downscaled to ≤ width, plus the full-res → downscaled pixel transform.

    CLAHE helps descriptors match across exposures and seasons, but its tiles are
    anchored to each frame, so intensity-based ECC uses plain luminance instead.
    """
    w, h = _size(img)
    nw, nh = w, h
    if w > width:
        nw, nh = width, max(1, round(h * width / w))
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)
    lum = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2Lab)[:, :, 0]
    if clahe:
        lum = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lum)
    return lum, scale_matrix(nw / w, nh / h)


def _detector(name: str, n: int) -> tuple[cv2.Feature2D, bool, str | None]:
    if name == "orb":
        return cv2.ORB_create(nfeatures=n, fastThreshold=10), True, None
    if name == "akaze":
        create = getattr(cv2, "AKAZE_create", None)
        if create is not None:
            return create(threshold=0.0005), True, None
        note = f"akaze unavailable in OpenCV {cv2.__version__}; used sift"
        return cv2.SIFT_create(nfeatures=n), False, note
    return cv2.SIFT_create(nfeatures=n), False, None


def detect(img_bgr: np.ndarray, settings: AlignSettings) -> Features:
    """Detect and describe features; reuse the result as `register(..., ref_features=)`."""
    gray, S = _luminance(img_bgr, settings.detect_width, clahe=True)
    detector, binary, note = _detector(settings.detector, settings.max_features)
    kps, desc = detector.detectAndCompute(gray, None)
    if desc is None or not kps:
        dim, dtype = (32, np.uint8) if binary else (128, np.float32)
        return Features(np.empty((0, 2), np.float32), np.empty((0, dim), dtype), binary, S, _size(gray), note)
    if len(kps) > settings.max_features:
        keep = np.argsort([-k.response for k in kps], kind="stable")[: settings.max_features]
        kps, desc = [kps[i] for i in keep], desc[keep]
    points = np.array([k.pt for k in kps], np.float32)
    if not binary:  # RootSIFT: Hellinger kernel via L1-normalize + sqrt
        desc = np.sqrt(desc / (np.abs(desc).sum(axis=1, keepdims=True) + 1e-7)).astype(np.float32)
    return Features(points, desc, binary, S, _size(gray), note)


def match(a: Features, b: Features) -> tuple[np.ndarray, np.ndarray]:
    """Lowe-ratio matches from a to b, at most one per b keypoint → (points_a, points_b)."""
    empty = np.empty((0, 2), np.float32)
    if len(a) < 2 or len(b) < 2:
        return empty, empty
    if a.binary != b.binary:
        raise ValueError("cannot match binary descriptors against float descriptors")
    if a.binary:
        matcher: cv2.DescriptorMatcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    else:
        cv2.setRNGSeed(0)  # FLANN's randomized kd-trees draw from OpenCV's RNG
        matcher = cv2.FlannBasedMatcher({"algorithm": 1, "trees": 4}, {"checks": 64})
    pairs = [
        (m[0].queryIdx, m[0].trainIdx, m[0].distance)
        for m in matcher.knnMatch(a.descriptors, b.descriptors, k=2)
        if len(m) == 2 and m[0].distance < LOWE_RATIO * m[1].distance
    ]
    if not pairs:
        return empty, empty
    q, t, d = (np.array(col) for col in zip(*pairs, strict=True))
    order = np.lexsort((q, d))
    _, first = np.unique(t[order], return_index=True)
    keep = np.sort(order[first])
    return a.points[q[keep]], b.points[t[keep]]


# -------------------------------- models ---------------------------------- #


def _project(H: np.ndarray, pts: np.ndarray) -> np.ndarray:
    p = np.asarray(pts, np.float64) @ H[:, :2].T + H[:, 2]
    return p[:, :2] / p[:, 2:3]


def _corners(size: Size) -> np.ndarray:
    w, h = size
    return np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float64)


def degeneracy(H: np.ndarray, size: Size) -> str | None:
    """Why H (image → reference, same pixel scale) is implausible for a repeat drone shot, else None."""
    if not np.isfinite(H).all() or abs(H[2, 2]) < 1e-12:
        return "non-finite"
    H = H / H[2, 2]
    corners = _corners(size)
    den = corners @ H[2, :2] + 1.0
    if (den <= 0).any():
        return "horizon crosses the frame"
    if den.max() / den.min() > MAX_PERSPECTIVE:
        return "extreme perspective"
    quad = _project(H, corners)
    x, y = quad[:, 0], quad[:, 1]
    area = 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    if area <= 0 or not cv2.isContourConvex(quad.astype(np.float32)):
        return "mirrored or twisted"
    scale = (area / (size[0] * size[1])) ** 0.5
    if not SCALE_RANGE[0] <= scale <= SCALE_RANGE[1]:
        return f"scale {scale:.2f} out of range"
    cx, cy = size[0] / 2, size[1] / 2
    d = H[2, 0] * cx + H[2, 1] * cy + 1.0
    u, v = _project(H, np.array([[cx, cy]]))[0]
    jac = (H[:2, :2] - np.outer([u, v], H[2, :2])) / d
    sv = np.linalg.svd(jac, compute_uv=False)
    if sv[1] <= 0 or sv[0] / sv[1] > MAX_ANISOTROPY:
        return "extreme shear"
    return None


def _fit(src: np.ndarray, dst: np.ndarray, kind: Method, thr: float) -> tuple[np.ndarray, np.ndarray] | None:
    if len(src) < 4:
        return None
    H: np.ndarray | None
    if kind == "homography":
        try:
            H, mask = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, thr, maxIters=10_000, confidence=0.999)
        except cv2.error:
            H = None
        if H is None:
            H, mask = cv2.findHomography(src, dst, cv2.RANSAC, thr, maxIters=10_000, confidence=0.999)
    else:
        M, mask = cv2.estimateAffinePartial2D(
            src,
            dst,
            method=cv2.RANSAC,
            ransacReprojThreshold=thr,
            maxIters=5000,
            confidence=0.999,
            refineIters=10,
        )
        H = None if M is None else np.vstack([M, [0.0, 0.0, 1.0]])
    if H is None or mask is None or not np.isfinite(H).all() or abs(H[2, 2]) < 1e-12:
        return None
    return H / H[2, 2], mask.ravel().astype(bool)


def _rmse(H: np.ndarray, src: np.ndarray, dst: np.ndarray) -> float:
    if not len(src):
        return 0.0
    return float(np.sqrt(np.mean(np.sum((_project(H, src) - dst) ** 2, axis=1))))


def resize_homography(img_size: Size, ref_size: Size) -> np.ndarray:
    """The 'no alignment' transform: stretch the image onto the reference grid."""
    return scale_matrix(ref_size[0] / img_size[0], ref_size[1] / img_size[1])


# ---------------------------------- ECC ----------------------------------- #


def _ecc_image(img: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Blurred luminance at ECC resolution, its interior mask and full-res → ECC-res transform."""
    gray, S = _luminance(img, ECC_WIDTH, clahe=False)
    h, w = gray.shape
    m = max(2, round(ECC_MARGIN_FRAC * w))
    mask = np.zeros((h, w), np.uint8)
    mask[m : h - m, m : w - m] = 1
    return cv2.GaussianBlur(gray.astype(np.float32) / 255.0, (5, 5), 0), mask, S


def _correlation(
    ref: tuple[np.ndarray, np.ndarray], img: tuple[np.ndarray, np.ndarray], W: np.ndarray
) -> float:
    """Zero-mean normalized correlation of ref and img warped by W (ref → img) over their overlap."""
    (ref_g, ref_m), (img_g, img_m) = ref, img
    size = _size(ref_g)
    flags = cv2.WARP_INVERSE_MAP
    warped = cv2.warpPerspective(img_g, W, size, flags=cv2.INTER_LINEAR | flags)
    valid = cv2.warpPerspective(img_m, W, size, flags=cv2.INTER_NEAREST | flags) & ref_m
    a, b = ref_g[valid > 0].astype(np.float64), warped[valid > 0].astype(np.float64)
    if a.size < 64:
        return 0.0
    a -= a.mean()
    b -= b.mean()
    den = np.sqrt(np.dot(a, a) * np.dot(b, b))
    return float(np.dot(a, b) / den) if den > 0 else 0.0


def _refine_ecc(
    ref_bgr: np.ndarray, img_bgr: np.ndarray, H: np.ndarray, kind: Method
) -> tuple[np.ndarray, float, bool]:
    """ECC-refine H within its model class; returns (H, correlation, refined).

    The refined H is kept only if it raises the correlation over the overlap and
    stays within a few pixels of the feature fit (ECC can lock onto changed content).
    """
    ref_g, ref_m, Sr = _ecc_image(ref_bgr)
    img_g, img_m, Si = _ecc_image(img_bgr)
    W0 = np.linalg.inv(Sr @ H @ np.linalg.inv(Si))
    W0 /= W0[2, 2]
    before = _correlation((ref_g, ref_m), (img_g, img_m), W0)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, ECC_ITERATIONS, 1e-6)
    motion = cv2.MOTION_HOMOGRAPHY if kind == "homography" else cv2.MOTION_AFFINE
    init = W0 if kind == "homography" else W0[:2]
    try:
        _, W = cv2.findTransformECC(ref_g, img_g, init.astype(np.float32), motion, criteria, img_m, 1)
    except cv2.error:
        return H, before, False
    W = W.astype(np.float64)
    if W.shape[0] == 2:
        W = np.vstack([W, [0.0, 0.0, 1.0]])
    if not np.isfinite(W).all() or abs(np.linalg.det(W)) < 1e-12:
        return H, before, False
    H2 = np.linalg.inv(Sr) @ np.linalg.inv(W) @ Si
    H2 /= H2[2, 2]
    corners = _corners(_size(img_bgr))
    shift = np.linalg.norm(_project(H2, corners) - _project(H, corners), axis=1).max()
    after = _correlation((ref_g, ref_m), (img_g, img_m), W)
    if after > before and shift <= ECC_MAX_SHIFT_FRAC * ref_bgr.shape[1]:
        return H2, after, True
    return H, before, False


# ------------------------------- register --------------------------------- #

_KINDS: dict[str, tuple[Method, ...]] = {
    "auto": ("homography", "affine"),
    "homography": ("homography",),
    "affine": ("affine",),
}


def register(
    ref_bgr: np.ndarray,
    img_bgr: np.ndarray,
    settings: AlignSettings,
    *,
    ref_features: Features | None = None,
) -> Registration:
    """Estimate the homography mapping img onto ref (full-resolution pixels).

    `ref_features` (from `detect(ref_bgr, settings)`) avoids re-detecting the
    reference when many images are registered against it. Never raises on
    unmatched content: failures come back with ok=False and a note.
    """
    ref_size, img_size = _size(ref_bgr), _size(img_bgr)
    fallback = resize_homography(img_size, ref_size)
    if settings.method == "none":
        return Registration(fallback, "none", ok=True, note="alignment disabled")
    ref_f = ref_features if ref_features is not None else detect(ref_bgr, settings)
    img_f = detect(img_bgr, settings)
    src, dst = match(img_f, ref_f)
    thr = max(1.0, THRESHOLD_FRAC * ref_f.det_size[0])
    notes = [img_f.note] if img_f.note else []

    fit: tuple[Method, np.ndarray, np.ndarray] | None = None
    for kind in _KINDS[settings.method]:
        result = _fit(src, dst, kind, thr)
        if result is None or result[1].sum() < MIN_MODEL_INLIERS:
            notes.append(f"{kind}: no model from {len(src)} matches")
            continue
        reason = degeneracy(result[0], img_f.det_size)
        if reason is None:
            fit = (kind, *result)
            break
        notes.append(f"{kind} rejected: {reason}")
    if fit is None:
        return Registration(fallback, "none", note="; ".join(notes))

    kind, Hd, inl = fit
    n_in = int(inl.sum())
    det_per_px = float(ref_f.S[0, 0])
    rmse = _rmse(Hd, src[inl], dst[inl]) / det_per_px
    ok = n_in >= settings.min_inliers and rmse <= thr / det_per_px
    if n_in < settings.min_inliers:
        notes.append(f"only {n_in} inliers (< {settings.min_inliers})")
    H = np.linalg.inv(ref_f.S) @ Hd @ img_f.S
    H /= H[2, 2]
    ecc = None
    if settings.ecc_refine and ok:
        H, ecc, refined = _refine_ecc(ref_bgr, img_bgr, H, kind)
        if refined:
            notes.append("ecc refined")
    return Registration(
        H=H,
        method=kind,
        inliers=n_in,
        inlier_ratio=n_in / len(src),
        rmse_px=rmse,
        ecc=ecc,
        ok=ok,
        note="; ".join(notes) or None,
    )


# --------------------------------- warp ----------------------------------- #


def valid_mask(img_shape: tuple[int, ...], H: np.ndarray, size: Size) -> np.ndarray:
    """Bool mask of reference pixels that `warp` fills from real image data (no edge bleed)."""
    h, w = img_shape[:2]
    src = np.zeros((h, w), np.uint8)
    m = LANCZOS_SUPPORT
    src[m : h - m, m : w - m] = 255
    mask = cv2.warpPerspective(src, H, size, flags=cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
    return cv2.erode(mask, np.ones((3, 3), np.uint8)) > 0


def warp(img: np.ndarray, H: np.ndarray, size: Size) -> tuple[np.ndarray, np.ndarray]:
    """Warp img onto a (width, height) grid with Lanczos; returns (warped, valid_mask)."""
    warped = cv2.warpPerspective(
        img, H, size, flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_CONSTANT, borderValue=0
    )
    return warped, valid_mask(img.shape, H, size)


def coverage(H: np.ndarray, img_size: Size, ref_size: Size) -> float:
    """Fraction of the reference frame covered by the warped image (0 when H is not drawable)."""
    corners = _corners(img_size)
    if (corners @ H[2, :2] + H[2, 2] <= 0).any():
        return 0.0
    quad = _project(H, corners).astype(np.float32)
    rect = _corners(ref_size).astype(np.float32)
    try:
        area, _ = cv2.intersectConvexConvex(quad, rect)
    except cv2.error:
        return 0.0
    return float(np.clip(area / (ref_size[0] * ref_size[1]), 0.0, 1.0))


def common_crop(masks: list[np.ndarray], aspect: float | None = None) -> tuple[int, int, int, int]:
    """Largest (x, y, w, h) of the given aspect (default: mask aspect) inside every mask.

    Binary search on the width with an O(1)-per-position summed-area test, so the
    cost is O(pixels · log width). Among equally large rectangles the one closest
    to the frame center wins.
    """
    if not masks:
        raise ValueError("common_crop needs at least one mask")
    inside = np.logical_and.reduce([np.asarray(m, bool) for m in masks])
    H, W = inside.shape
    aspect = aspect or W / H
    if aspect <= 0:
        raise ValueError(f"aspect must be positive, got {aspect}")
    bad = cv2.integral((~inside).view(np.uint8))

    def positions(w: int) -> tuple[int, np.ndarray, np.ndarray] | None:
        h = max(1, round(w / aspect))
        if h > H:
            return None
        sums = bad[h:, w:] - bad[:-h, w:] - bad[h:, :-w] + bad[:-h, :-w]
        ys, xs = np.nonzero(sums == 0)
        return (h, ys, xs) if len(ys) else None

    lo, hi = 0, min(W, int(H * aspect) + 1)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if positions(mid):
            lo = mid
        else:
            hi = mid - 1
    found = positions(lo) if lo else None
    if found is None:
        raise ValueError("valid regions do not overlap")
    h, ys, xs = found
    dist = (xs + lo / 2 - W / 2) ** 2 + (ys + h / 2 - H / 2) ** 2
    i = int(np.argmin(dist))
    return int(xs[i]), int(ys[i]), lo, h
