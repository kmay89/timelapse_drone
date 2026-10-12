"""The beat grid of story cuts, and the runtime's eased hold-window camera math ported to frames.

A story cut lives on a grid of B frames per beat (B = round(60·fps / bpm)); every shot start,
dissolve start, push start and lead-word landing falls on a frame divisible by B. Dissolves last
D = max(ceil(0.34·fps), round(2B/3)) frames, so none is shorter than 0.34 s (film/safety.py checks
it again on every plan). `hold_windows` and `keyed` are line-for-line ports of
runtime/js/25-stage.js (smoothstep easing, without the reduced-motion branch), so a film's camera
moves the way the web scrub's does.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")
MIN_DISSOLVE_S = 0.34


def smooth(t: float) -> float:
    """Smoothstep on [0, 1] (runtime/js/00-util.js `smooth`, clamped)."""
    t = min(max(t, 0.0), 1.0)
    return t * t * (3 - 2 * t)


def ramp(f: float, start: float, length: float) -> float:
    """Smoothstep 0 → 1 whose first changed frame is `start` and which reaches 1 on start + length − 1."""
    return smooth((f - start + 1) / max(length, 1))


def min_dissolve(fps: int) -> int:
    """The shortest dissolve allowed between two images, in frames: ceil(0.34·fps)."""
    return math.ceil(round(MIN_DISSOLVE_S * fps, 6))


def frames_per_beat(fps: int, bpm: float) -> int:
    """B = round(60·fps / bpm)."""
    return max(1, round(60 * fps / bpm))


def dissolve_frames(fps: int, fpb: int) -> int:
    """D = max(ceil(0.34·fps), round(2B/3))."""
    return max(min_dissolve(fps), round(2 * fpb / 3))


def bar_beat(f: int, fpb: int) -> str:
    """'<bar>.<beat>' (both 1-based, 4 beats to the bar) of frame f."""
    return f"{f // (4 * fpb) + 1}.{f // fpb % 4 + 1}"


@dataclass(frozen=True)
class Grid:
    fps: int
    fpb: int  # B
    dissolve: int  # D

    @property
    def bpm(self) -> float:
        """The effective tempo: 60·fps / B."""
        return 60 * self.fps / self.fpb

    @property
    def third(self) -> int:
        """B/3, rounded: sub-beat staggers and text fades."""
        return max(1, round(self.fpb / 3))


def beat_grid(fps: int, bpm: float) -> Grid:
    """The grid for fps and bpm; ValueError when a beat is shorter than a dissolve."""
    if fps <= 0 or bpm <= 0:
        raise ValueError("fps and bpm must be positive")
    b = frames_per_beat(fps, bpm)
    d = dissolve_frames(fps, b)
    if b < d:
        raise ValueError(
            f"bpm too fast for fps: {bpm:g} bpm at {fps} fps gives {b} frames per beat, "
            f"shorter than the {d}-frame minimum dissolve"
        )
    return Grid(fps, b, d)


@dataclass(frozen=True)
class Window:
    a: float
    b: float


def hold_windows(keys: Sequence[float], hold: float) -> list[Window]:
    """Hold windows: key k holds over [k, k + hold]. Keys sharing a k split its hold into equal
    slots, each still for its first half and easing into the next one over the second (the last
    holds to the end). Port of runtime/js/25-stage.js `holdWindows`."""
    out = []
    for idx, k in enumerate(keys):
        n = sum(1 for x in keys if x == k)
        j = sum(1 for x in keys[:idx] if x == k)
        a = k + hold * j / n
        out.append(Window(a, k + hold if j == n - 1 else a + hold / n / 2))
    return out


def keyed(windows: Sequence[Window], values: Sequence[T], v: float, mix: Callable[[T, T, float], T]) -> T:
    """Value at v: held inside each window, smoothstep-eased between windows. Port of
    runtime/js/25-stage.js `keyed` (without its reduced-motion cut)."""
    i = -1
    while i + 1 < len(windows) and windows[i + 1].a <= v:
        i += 1
    if i < 0:
        return values[0]
    if i == len(windows) - 1 or v <= windows[i].b:
        return values[i]
    t = (v - windows[i].b) / max(1e-6, windows[i + 1].a - windows[i].b)
    return mix(values[i], values[i + 1], smooth(t))


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def mix_cam(a: Sequence[float], b: Sequence[float], t: float) -> tuple[float, ...]:
    """runtime `mixCam`: every component interpolated linearly."""
    return tuple(lerp(x, y, t) for x, y in zip(a, b, strict=True))


__all__ = [
    "Grid",
    "Window",
    "bar_beat",
    "beat_grid",
    "dissolve_frames",
    "frames_per_beat",
    "hold_windows",
    "keyed",
    "min_dissolve",
    "mix_cam",
    "ramp",
    "smooth",
]
