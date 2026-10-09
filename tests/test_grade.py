"""Photometric matching: Reinhard / histogram transfer in Lab, strength blending, masks."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from vantage.process.grade import match_color


def _lab_stats(img: np.ndarray, mask: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    lab = cv2.cvtColor(img.astype(np.float32) / 255, cv2.COLOR_BGR2Lab)
    px = lab.reshape(-1, 3) if mask is None else lab[mask]
    return px.mean(axis=0), px.std(axis=0)


@pytest.fixture(scope="module")
def ref() -> np.ndarray:
    rng = np.random.default_rng(0)
    base = cv2.resize(rng.random((12, 20, 3), dtype=np.float32), (640, 400), interpolation=cv2.INTER_CUBIC)
    fine = rng.normal(0, 0.06, (400, 640, 3)).astype(np.float32)
    return np.clip((0.15 + 0.7 * base + fine) * 255, 0, 255).astype(np.uint8)


@pytest.fixture(scope="module")
def shifted(ref: np.ndarray) -> np.ndarray:
    """Same scene, darker, lower contrast and much warmer (a different flight / time of day)."""
    img = ref.astype(np.float32)
    img = (img - 128) * 0.7 + 128 - 25
    img *= np.array([0.8, 1.0, 1.25], np.float32)  # B, G, R
    return np.clip(img, 0, 255).astype(np.uint8)


def test_reinhard_matches_mean_and_std(ref, shifted):
    out = match_color(shifted, ref, "reinhard", 1.0)
    (m_out, s_out), (m_ref, s_ref) = _lab_stats(out), _lab_stats(ref)
    m_in, _ = _lab_stats(shifted)
    assert out.dtype == np.uint8 and out.shape == ref.shape
    assert np.abs(m_out - m_ref).max() < 1.5
    assert np.abs(m_out - m_ref).max() < 0.2 * np.abs(m_in - m_ref).max()
    assert np.allclose(s_out, s_ref, rtol=0.08, atol=0.5)


def test_histogram_matches_distribution(ref, shifted):
    out = match_color(shifted, ref, "histogram", 1.0)
    (m_out, _), (m_ref, _) = _lab_stats(out), _lab_stats(ref)
    assert np.abs(m_out - m_ref).max() < 2.0
    # L follows the reference CDF closely.
    q = np.linspace(0.05, 0.95, 10)
    lum = lambda im: cv2.cvtColor(im.astype(np.float32) / 255, cv2.COLOR_BGR2Lab)[..., 0].ravel()  # noqa: E731
    assert np.abs(np.quantile(lum(out), q) - np.quantile(lum(ref), q)).max() < 2.0


@pytest.mark.parametrize("mode", ["reinhard", "histogram", "none"])
def test_strength_zero_and_mode_none_are_identity(ref, shifted, mode):
    assert np.array_equal(match_color(shifted, ref, mode, 0.0), shifted)
    assert np.array_equal(match_color(shifted, ref, "none", 1.0), shifted)


def test_strength_blends_linearly(ref, shifted):
    m_in = _lab_stats(shifted)[0]
    m_ref = _lab_stats(ref)[0]
    m_half = _lab_stats(match_color(shifted, ref, "reinhard", 0.5))[0]
    assert np.allclose(m_half, (m_in + m_ref) / 2, atol=1.5)


def test_mask_restricts_statistics(ref, shifted):
    """A black border outside the mask must not skew the transfer."""
    img = shifted.copy()
    img[:, :160] = 0
    mask = np.ones(img.shape[:2], bool)
    mask[:, :160] = False
    out = match_color(img, ref, "reinhard", 1.0, mask)
    m_out, m_ref = _lab_stats(out, mask)[0], _lab_stats(ref, mask)[0]
    assert np.abs(m_out - m_ref).max() < 1.5


def test_flat_image_does_not_blow_up(ref):
    flat = np.full_like(ref, 90)
    out = match_color(flat, ref, "reinhard", 1.0)
    assert out.reshape(-1, 3).std(axis=0).max() < 1.0  # stays flat: the mean moves, nothing divides by ~0
    assert abs(_lab_stats(out)[0][0] - _lab_stats(ref)[0][0]) < 1.5
