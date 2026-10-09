"""Registration, warping and common-crop tests on synthetic aerial-like scenes.

The helpers at the top are shared with test_select.py / test_masters.py.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from vantage.config import AlignSettings
from vantage.process import align

SIZE = (1600, 1000)  # view (width, height)
WORLD = (2100, 1400)  # rendered scene; views look at its middle so they never show black borders
OFFSET = ((WORLD[0] - SIZE[0]) // 2, (WORLD[1] - SIZE[1]) // 2)


def make_world(seed: int = 0, size: tuple[int, int] = WORLD) -> np.ndarray:
    """Multi-octave color noise plus hard-edged shapes: texture at every scale, like a site from above."""
    w, h = size
    rng = np.random.default_rng(seed)
    acc = np.zeros((h, w, 3), np.float32)
    for cells, amp in ((6, 0.45), (24, 0.25), (96, 0.18), (360, 0.12)):
        grid = rng.random((max(2, cells * h // w), cells, 3), dtype=np.float32)
        acc += amp * cv2.resize(grid, (w, h), interpolation=cv2.INTER_CUBIC)
    img = np.clip(acc / acc.max() * 255, 0, 255).astype(np.uint8)
    for _ in range(w * h // 9000):
        color = tuple(int(v) for v in rng.integers(0, 256, 3))
        x, y, s = (int(v) for v in (rng.integers(0, w), rng.integers(0, h), rng.integers(6, 60)))
        kind = rng.integers(3)
        if kind == 0:
            cv2.rectangle(img, (x, y), (x + s, y + int(rng.integers(6, 60))), color, -1)
        elif kind == 1:
            cv2.circle(img, (x, y), s // 2, color, -1, cv2.LINE_AA)
        else:
            end = (x + int(rng.integers(-80, 80)), y + int(rng.integers(-80, 80)))
            cv2.line(img, (x, y), end, color, 2, cv2.LINE_AA)
    return img


def random_homography(rng: np.random.Generator, *, perspective: bool = True) -> np.ndarray:
    """Reference → view: rotation ±4°, scale ±6%, translation ±5%, mild perspective, about the center."""
    w, h = SIZE
    a, s = np.deg2rad(rng.uniform(-4, 4)), 1 + rng.uniform(-0.06, 0.06)
    tx, ty = rng.uniform(-0.05, 0.05) * w, rng.uniform(-0.05, 0.05) * h
    px, py = rng.uniform(-2e-5, 2e-5, 2) if perspective else (0.0, 0.0)
    center = np.array([[1, 0, w / 2], [0, 1, h / 2], [0, 0, 1]])
    similarity = np.array(
        [[s * np.cos(a), -s * np.sin(a), tx], [s * np.sin(a), s * np.cos(a), ty], [0, 0, 1]]
    )
    persp = np.array([[1, 0, 0], [0, 1, 0], [px, py, 1]])
    return center @ similarity @ persp @ np.linalg.inv(center)


def reference_view(world: np.ndarray) -> np.ndarray:
    x, y = OFFSET
    return world[y : y + SIZE[1], x : x + SIZE[0]].copy()


def render_view(world: np.ndarray, H_true: np.ndarray, rng: np.random.Generator | None = None) -> np.ndarray:
    """The view whose pixel p shows reference pixel H_true⁻¹·p; rng adds exposure, white balance and noise."""
    to_ref = np.array([[1, 0, -OFFSET[0]], [0, 1, -OFFSET[1]], [0, 0, 1]], np.float64)
    img = cv2.warpPerspective(world, H_true @ to_ref, SIZE, flags=cv2.INTER_LINEAR)
    if rng is None:
        return img
    lit = img.astype(np.float32) * rng.uniform(0.75, 1.25, 3) + rng.uniform(-15, 15)
    return np.clip(lit + rng.normal(0, 3, img.shape), 0, 255).astype(np.uint8)


def corner_errors(H_est: np.ndarray, H_true: np.ndarray, size: tuple[int, int] = SIZE) -> np.ndarray:
    """Reprojection error (reference px) of the reference corners: ref → view (truth) → ref (estimate)."""
    w, h = size
    corners = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float64)
    return np.linalg.norm(align._project(H_est, align._project(H_true, corners)) - corners, axis=1)


# --------------------------------- fixtures -------------------------------- #


@pytest.fixture(scope="module")
def world() -> np.ndarray:
    return make_world(seed=3)


@pytest.fixture(scope="module")
def ref(world: np.ndarray) -> np.ndarray:
    return reference_view(world)


@pytest.fixture(scope="module")
def ref_features(ref: np.ndarray) -> align.Features:
    return align.detect(ref, AlignSettings())


# ------------------------------- registration ------------------------------ #


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_sift_recovers_known_homography(world, ref, ref_features, seed):
    rng = np.random.default_rng(seed)
    H_true = random_homography(rng)
    img = render_view(world, H_true, rng)
    reg = align.register(ref, img, AlignSettings(), ref_features=ref_features)
    err = corner_errors(reg.H, H_true)
    assert reg.ok and reg.method == "homography"
    assert reg.inliers >= 500 and reg.inlier_ratio > 0.5
    assert reg.rmse_px < 1.5
    assert reg.ecc is not None and reg.ecc > 0.9
    assert np.median(err) < 1.0, err
    assert err.max() < 2.0, err


@pytest.mark.parametrize("detector", ["akaze", "orb"])
def test_alternative_detectors(world, ref, detector):
    rng = np.random.default_rng(7)
    H_true = random_homography(rng)
    img = render_view(world, H_true, rng)
    reg = align.register(ref, img, AlignSettings(detector=detector, max_features=4000, ecc_refine=False))
    assert reg.ok, reg.note
    assert np.median(corner_errors(reg.H, H_true)) < 1.0


def test_ecc_refines_a_coarse_feature_fit(world, ref):
    rng = np.random.default_rng(11)
    H_true = random_homography(rng)
    img = render_view(world, H_true, rng)
    coarse = AlignSettings(detect_width=320, max_features=400, min_inliers=20, ecc_refine=False)
    before = align.register(ref, img, coarse)
    after = align.register(ref, img, coarse.model_copy(update={"ecc_refine": True}))
    assert before.ok and after.ok
    assert "ecc refined" in (after.note or "")
    assert np.median(corner_errors(after.H, H_true)) < 0.5 * np.median(corner_errors(before.H, H_true))


def test_resolution_change_maps_full_res_pixels(world, ref, ref_features):
    """A half-resolution image must still map onto full-resolution reference pixels."""
    rng = np.random.default_rng(5)
    H_true = random_homography(rng)
    img = render_view(world, H_true, rng)
    small = cv2.resize(img, (SIZE[0] // 2, SIZE[1] // 2), interpolation=cv2.INTER_AREA)
    reg = align.register(ref, small, AlignSettings(), ref_features=ref_features)
    to_small = align.scale_matrix(0.5, 0.5)
    assert reg.ok
    assert np.median(corner_errors(reg.H, to_small @ H_true)) < 1.0


def test_affine_method_recovers_similarity(world, ref, ref_features):
    rng = np.random.default_rng(9)
    H_true = random_homography(rng, perspective=False)
    img = render_view(world, H_true, rng)
    reg = align.register(ref, img, AlignSettings(method="affine"), ref_features=ref_features)
    assert reg.ok and reg.method == "affine" and reg.ecc is not None
    assert np.allclose(reg.H[2], [0, 0, 1])
    assert np.median(corner_errors(reg.H, H_true)) < 1.0


@pytest.mark.parametrize(
    "img",
    [
        np.random.default_rng(0).integers(0, 256, (SIZE[1], SIZE[0], 3), dtype=np.uint8),
        np.full((SIZE[1], SIZE[0], 3), 128, np.uint8),
    ],
    ids=["noise", "blank"],
)
def test_unrelated_or_blank_image_fails_gracefully(ref, ref_features, img):
    reg = align.register(ref, img, AlignSettings(), ref_features=ref_features)
    assert not reg.ok
    assert reg.H.shape == (3, 3) and np.isfinite(reg.H).all()
    assert reg.note
    assert reg.info().ok is False


def test_method_none_only_resizes(ref):
    img = np.zeros((500, 800, 3), np.uint8)
    reg = align.register(ref, img, AlignSettings(method="none"))
    assert reg.ok and reg.method == "none"
    warped, mask = align.warp(np.full_like(img, 200), reg.H, SIZE)
    assert warped.shape == (SIZE[1], SIZE[0], 3)
    assert mask.mean() > 0.97


def test_degeneracy_checks():
    assert align.degeneracy(np.eye(3), SIZE) is None
    assert align.degeneracy(np.diag([-1.0, 1.0, 1.0]), SIZE) is not None  # mirrored
    assert align.degeneracy(np.diag([5.0, 5.0, 1.0]), SIZE) is not None  # implausible zoom
    assert align.degeneracy(np.diag([1.0, 3.0, 1.0]), SIZE) is not None  # shear / squash
    assert align.degeneracy(np.array([[1, 0, 0], [0, 1, 0], [-1e-3, 0, 1.0]]), SIZE) is not None


def test_registration_is_deterministic(world, ref):
    rng = np.random.default_rng(4)
    img = render_view(world, random_homography(rng), rng)
    settings = AlignSettings(detect_width=800, max_features=3000)
    a = align.register(ref, img, settings)
    b = align.register(ref, img, settings)
    assert np.array_equal(a.H, b.H) and a.inliers == b.inliers


# ---------------------------------- warp ----------------------------------- #


def test_identity_warp_is_lossless_inside_mask(ref):
    warped, mask = align.warp(ref, np.eye(3), SIZE)
    assert np.array_equal(warped[mask], ref[mask])
    assert mask[10:-10, 10:-10].all()
    assert not mask[:2].any() and not mask[:, -2:].any()


def test_valid_mask_excludes_lanczos_edge_bleed(world, ref):
    H_true = random_homography(np.random.default_rng(6))
    warped, mask = align.warp(np.full_like(ref, 255), np.linalg.inv(H_true), SIZE)
    assert warped[mask].min() >= 250  # no black seeps into pixels marked valid


# ------------------------------- common crop ------------------------------- #


def _inside(masks: list[np.ndarray], rect: tuple[int, int, int, int]) -> bool:
    x, y, w, h = rect
    return all(m[y : y + h, x : x + w].all() for m in masks)


def test_common_crop_of_warped_frames():
    rng = np.random.default_rng(21)
    shape = (SIZE[1], SIZE[0])
    masks = [align.valid_mask(shape, np.linalg.inv(random_homography(rng)), SIZE) for _ in range(5)]
    rect = align.common_crop(masks)
    _, _, w, h = rect
    assert _inside(masks, rect)
    assert abs((w / h) / (SIZE[0] / SIZE[1]) - 1) < 0.01
    assert w * h > 0.55 * SIZE[0] * SIZE[1]
    # Maximal: one pixel wider (same aspect) no longer fits anywhere.
    inter = np.logical_and.reduce(masks)
    bigger_h = round((w + 1) / (SIZE[0] / SIZE[1]))
    win = cv2.integral((~inter).view(np.uint8))
    sums = (
        win[bigger_h:, w + 1 :]
        - win[:-bigger_h, w + 1 :]
        - win[bigger_h:, : -(w + 1)]
        + win[:-bigger_h, : -(w + 1)]
    )
    assert not (sums == 0).any()


def test_common_crop_known_rectangle_and_aspect():
    mask = np.zeros((1000, 1600), bool)
    mask[100:700, 200:1400] = True  # 1200 x 600 valid block
    assert align.common_crop([mask], 1.6) == (320, 100, 960, 600)  # centered on x = 800
    x, y, w, h = align.common_crop([mask], 16 / 9)
    assert _inside([mask], (x, y, w, h)) and h == 600 and abs(w / h - 16 / 9) < 0.01


def test_common_crop_in_circle_is_centered_and_maximal():
    mask = np.zeros((1000, 1600), np.uint8)
    cv2.circle(mask, (800, 500), 400, 1, -1)
    x, y, w, h = align.common_crop([mask.astype(bool)], 2.0)
    expected_w = 2 * 400 * np.cos(np.arctan(0.5))  # inscribed 2:1 rectangle
    assert _inside([mask.astype(bool)], (x, y, w, h))
    assert w > 0.98 * expected_w
    assert abs(x + w / 2 - 800) <= 1 and abs(y + h / 2 - 500) <= 1


def test_common_crop_rejects_disjoint_masks():
    a = np.zeros((100, 160), bool)
    b = np.zeros((100, 160), bool)
    a[:, :80], b[:, 80:] = True, True
    with pytest.raises(ValueError):
        align.common_crop([a, b])
