"""Photosensitivity safety for films: a static check of every story plan and a luminance meter run on
every rendered frame.

Static (plan time, an error): every transition between two images is a dissolve of at least
ceil(0.34·fps) frames (a dip through night counts as two), there is no hard cut between images
(a curtain may open without one only on the image the film already shows), every curtain sweep
lasts at least 2 beats, and every dip into or out of a paper surface lasts `surface_dip` frames. A
loop film opens with a rise from night and closes with a dip to night, each at least as long as a
dissolve, and its length is a whole number of GOPs (2 beats).
`surface_dip` is the shortest dip from night to paper (in display gamma) in which no frame pair
changes luminance by THRESHOLD or more, so a paper stats card never flashes.

Flashes (render time, every frame, every cut, classic ones included): each frame is downscaled to
1/8 (INTER_AREA), converted from sRGB to linear light and averaged over 113 regions: an 8×8 grid of
cells plus the 7×7 cells offset by half a cell. Following WCAG 2.3.1's general and red flash
definitions, a transition is a monotonic run of a region's Rec.709 relative luminance, however many
frames it is ramped over, that changes it by ≥ 0.10 while its darker end is < 0.80, or a run of
its red value (R − G − B)·320 of more than 20 with saturated red (R / (R + G + B) ≥ 0.8) at one
end. A run ends only once the value comes back 0.10 (20) from its extreme, so noise does not split
it. A film fails if any region holds more than 3 transitions within `fps` frames (stricter than 3
flashes, i.e. 6 transitions, a second); the renderer then deletes its .part outputs and raises
FlashError. The meter only reads frames, so it never changes a film's bytes; its result is the
plan's `render.flash`. A region is 1/64 of the frame, smaller than WCAG's 25 % of a 10° field
(about 2.8 % of a screen), so a flash of that size changes some region in full.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import cv2
import numpy as np

from vantage.film.beat import min_dissolve, ramp

CELLS = 8  # 8×8 cells (1.6 % of the frame each) plus 7×7 offset ones
THRESHOLD = 0.10  # |ΔL| of a transition
BRIGHT = 0.80  # changes between two frames both at or above this are not counted
LIMIT = 3  # transitions allowed in any window of fps frames
RED_THRESHOLD = 20.0  # change of (R − G − B)·320 of a red transition
RED_SATURATION = 0.8  # R / (R + G + B) of a saturated red

_c = np.arange(256, dtype=np.float64) / 255
_SRGB_TO_LINEAR = np.where(_c <= 0.04045, _c / 12.92, ((_c + 0.055) / 1.055) ** 2.4).astype(np.float32)
_BGR_WEIGHTS = np.array([0.0722, 0.7152, 0.2126], np.float32)  # Rec.709, BGR order


class PlanError(ValueError):
    """A plan breaks a safety rule (a dissolve too short, a hard cut between images)."""


class FlashError(ValueError):
    """A rendered film exceeds the flash limit."""


def _luminance(bgr: tuple[int, int, int]) -> float:
    return float(_SRGB_TO_LINEAR[np.array(bgr, np.uint8)] @ _BGR_WEIGHTS)


def _bgr(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16)


def surface_dip(fps: int, dissolve: int, night: str, paper: str) -> int:
    """Frames of a dip into or out of a paper surface: the shortest n ≥ `dissolve` in which a
    display-gamma dip between the night and paper colours never changes luminance by THRESHOLD
    or more between two frames (images lie between the two, so they are covered too)."""
    a, b = np.array(_bgr(night), np.float64), np.array(_bgr(paper), np.float64)
    for n in range(dissolve, 4 * fps + 1):
        lum = []
        for k in range(-1, n + 1):
            w = ramp(k, 0, n)
            mixed = tuple(int(v) for v in np.clip(np.floor(a * (1 - w) + b * w + 0.5), 0, 255))
            lum.append(_luminance(mixed))  # type: ignore[arg-type]
        if max(abs(y - x) for x, y in pairwise(lum)) < THRESHOLD:
            return n
    return 4 * fps


IMAGES = frozenset({"title", "capture", "curtain"})
SURFACES = frozenset({"night", "paper"})


def check_plan(plan: dict[str, Any]) -> None:
    """Raise PlanError when a story plan cuts between images, dissolves faster than 0.34 s, sweeps a
    curtain in under 2 beats, dips into or out of paper faster than `surface_dip`, ends its title
    before its words are all in, or dips into a card, stat or end card for so long that less than
    a beat of the shot is left for its text.

    Classic plans keep their own hold/fade timing and are not checked here (the meter still
    measures them)."""
    if plan.get("cut") == "classic":
        return
    fps = plan["fps"]
    shortest = min_dissolve(fps)
    grid = plan.get("grid") or {}
    fpb = grid.get("fpb", 1)
    colors = plan["inputs"]["colors"]
    paper = surface_dip(fps, grid.get("dissolve", shortest), colors["night"], colors["paper"])
    problems: list[str] = []
    prev: dict[str, Any] | None = None

    def surface(shot: dict[str, Any] | None) -> str | None:
        if shot is None:
            return None
        if shot["kind"] in ("card", "end", "dip"):
            return "night"
        return shot.get("surface") if shot["kind"] == "stat" else None

    shots = plan["shots"]
    if plan.get("loop"):
        if not shots or shots[0]["kind"] != "rise" or shots[-1]["kind"] != "dip":
            problems.append("a loop film opens with a rise from night and closes with a dip to night")
        if plan["frames"] % (2 * fpb):
            problems.append(
                f"a loop film of {plan['frames']} frames is not a whole number of GOPs ({2 * fpb})"
            )
    for shot in shots:
        kind, sid = shot["kind"], shot["id"]
        if kind in ("rise", "dip") and shot["f1"] - shot["f0"] < shortest:
            problems.append(
                f"{sid}: a {shot['f1'] - shot['f0']}-frame {kind} is shorter than {shortest} frames"
            )
        if kind in ("capture", "curtain"):
            d = shot.get("dissolve")
            same = prev is not None and prev["kind"] == "title" and prev.get("capture") == shot.get("capture")
            if kind == "curtain":
                same = shot.get("from_after", False)
            if prev is not None and prev["kind"] in IMAGES and d is None and not same:
                problems.append(f"{sid}: a hard cut from {prev['id']} (every image change dissolves)")
            if prev is not None and prev["kind"] not in IMAGES and d is None:
                problems.append(f"{sid}: a hard cut from {prev['id']} (images dip in from a surface)")
            if d is not None:
                n = d["f1"] - d["f0"]
                need = paper if "paper" in (d["from"], surface(prev)) else shortest
                if n < need:
                    problems.append(
                        f"{sid}: a {n}-frame dissolve ({n / fps:.2f} s) is shorter than {need} frames"
                        + (" (0.34 s)" if need == shortest else " (a dip from paper)")
                    )
                if d["f0"] != shot["f0"]:
                    problems.append(f"{sid}: its dissolve does not start with the shot")
        if kind == "curtain":
            keys = shot.get("keys") or []
            for k0, k1 in pairwise(keys):
                if k1["a"] - k0["b"] < 2 * fpb:
                    problems.append(f"{sid}: a curtain sweep of {k1['a'] - k0['b']} frames is under 2 beats")
        if kind == "title" and shot.get("groups"):
            third = max(1, round(fpb / 3))  # beat.Grid.third
            if shot["groups"][-1] + third > shot["f1"] - third:
                problems.append(f"{sid}: its words are never all on screen (title_beats too short)")
        if kind in ("card", "end", "stat") and shot.get("dip", 0) > shot["f1"] - shot["f0"] - fpb:
            problems.append(
                f"{sid}: a {shot.get('dip', 0)}-frame dip leaves under a beat of its "
                f"{shot['f1'] - shot['f0']} frames for its text (end_beats too short?)"
            )
        if kind in ("card", "end", "stat") and prev is not None:
            n = shot.get("dip", 0)
            mine, theirs = surface(shot), surface(prev)
            need = paper if "paper" in (mine, theirs) and mine != theirs else shortest
            if (prev["kind"] in IMAGES or mine != theirs) and n < need:
                problems.append(f"{sid}: a {n}-frame dip is shorter than {need} frames")
        prev = shot
    if problems:
        raise PlanError("film plan is not safe to render:\n  " + "\n  ".join(problems))


def _sub_means(frame: np.ndarray, grid: int) -> np.ndarray:
    """Mean linear B, G, R of each sub-cell of a (2·grid) × (2·grid) split of a BGR uint8 frame."""
    n = 2 * grid
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (max(n, w // 8), max(n, h // 8)), interpolation=cv2.INTER_AREA)
    lin = _SRGB_TO_LINEAR[small]
    sh, sw = lin.shape[:2]
    ys = np.linspace(0, sh, n + 1).round().astype(int)
    xs = np.linspace(0, sw, n + 1).round().astype(int)
    return np.array(
        [
            [lin[ys[r] : ys[r + 1], xs[c] : xs[c + 1]].reshape(-1, 3).mean(axis=0) for c in range(n)]
            for r in range(n)
        ],
        np.float64,
    )


def regions(frame: np.ndarray, grid: int = CELLS) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(relative luminance, red value, saturated red) of each measured region of a BGR uint8 frame:
    the grid × grid cells plus the (grid − 1)² cells offset by half a cell, so a flash that straddles
    a cell edge is still seen whole. Red value is max(0, (R − G − B)·320) and saturated red is
    R / (R + G + B) ≥ 0.8, in linear light (WCAG 2.3.1's red flash)."""
    sub = _sub_means(frame, grid)
    n = 2 * grid
    blocks = [
        sub[r : r + 2, c : c + 2].reshape(-1, 3).mean(axis=0)
        for r in range(0, n - 1, 2)
        for c in range(0, n - 1, 2)
    ]
    blocks += [
        sub[r : r + 2, c : c + 2].reshape(-1, 3).mean(axis=0)
        for r in range(1, n - 2, 2)
        for c in range(1, n - 2, 2)
    ]
    bgr = np.array(blocks)
    lum = bgr @ _BGR_WEIGHTS.astype(np.float64)
    b, g, r = bgr[:, 0], bgr[:, 1], bgr[:, 2]
    red = np.maximum(0.0, (r - g - b) * 320)
    saturated = r >= RED_SATURATION * np.maximum(r + g + b, 1e-9)
    return lum, red, saturated


def cells(frame: np.ndarray, grid: int = CELLS) -> np.ndarray:
    """Mean linear relative luminance of each cell of a grid × grid split of a BGR uint8 frame."""
    sub = _sub_means(frame, grid)
    lum = sub @ _BGR_WEIGHTS.astype(np.float64)
    return lum.reshape(grid, 2, grid, 2).mean(axis=(1, 3))


@dataclass
class _Runs:
    """Monotonic runs of a value per region, with `size` of hysteresis: a run ends only when the value
    has come back `size` from the run's extreme, so a change ramped over many frames is one run, and
    a run is long enough to be a transition once it spans `size` (`span`)."""

    size: float
    trend: np.ndarray | None = None  # +1 rising, -1 falling, 0 no run yet
    pivot: np.ndarray = field(default_factory=lambda: np.zeros(0))  # where the run started
    ext: np.ndarray = field(default_factory=lambda: np.zeros(0))  # its extreme so far
    hi: np.ndarray = field(default_factory=lambda: np.zeros(0))  # before the first run: the range seen
    lo: np.ndarray = field(default_factory=lambda: np.zeros(0))
    pivot_tag: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    ext_tag: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    hi_tag: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    lo_tag: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))
    counted: np.ndarray = field(default_factory=lambda: np.zeros(0, bool))  # the run is a transition already

    def push(self, v: np.ndarray, tag: np.ndarray) -> None:
        if self.trend is None:
            self.trend = np.zeros(v.shape, np.int8)
            self.pivot, self.ext, self.hi, self.lo = v.copy(), v.copy(), v.copy(), v.copy()
            self.pivot_tag, self.ext_tag, self.hi_tag, self.lo_tag = (
                tag.copy(),
                tag.copy(),
                tag.copy(),
                tag.copy(),
            )
            self.counted = np.zeros(v.shape, bool)
            return
        T = self.size
        idle, rising, falling = self.trend == 0, self.trend == 1, self.trend == -1
        # Before the first run: widen the range, and start a run once it spans T.
        for m, store, tags in (
            (idle & (v > self.hi), self.hi, self.hi_tag),
            (idle & (v < self.lo), self.lo, self.lo_tag),
        ):
            store[m], tags[m] = v[m], tag[m]
        up = idle & (v - self.lo >= T)
        down = idle & ~up & (self.hi - v >= T)
        for m, sign, start, start_tag in ((up, 1, self.lo, self.lo_tag), (down, -1, self.hi, self.hi_tag)):
            self.pivot[m], self.pivot_tag[m] = start[m], start_tag[m]
            self.ext[m], self.ext_tag[m] = v[m], tag[m]
            self.trend[m], self.counted[m] = sign, False
        # A run goes on while it moves its way, and turns once the value comes back T from its extreme.
        grow = (rising & (v > self.ext)) | (falling & (v < self.ext))
        self.ext[grow], self.ext_tag[grow] = v[grow], tag[grow]
        turn = (rising & (self.ext - v >= T)) | (falling & (v - self.ext >= T))
        self.pivot[turn], self.pivot_tag[turn] = self.ext[turn], self.ext_tag[turn]
        self.ext[turn], self.ext_tag[turn] = v[turn], tag[turn]
        self.trend[turn] = -self.trend[turn]
        self.counted[turn] = False

    @property
    def span(self) -> np.ndarray:
        assert self.trend is not None
        return (self.trend != 0) & (np.abs(self.ext - self.pivot) >= self.size)


@dataclass
class LumaMeter:
    """Counts flash transitions per region, frame by frame; `result()` is the plan's render.flash.

    A transition is a monotonic run of a region's relative luminance (however many frames it takes)
    that changes it by at least THRESHOLD while its darker end is below BRIGHT, or a run of its red
    value of more than RED_THRESHOLD with saturated red at one end. Two opposing transitions are one
    flash (WCAG 2.3.1); a film fails when any region holds more than LIMIT transitions (of either
    kind) within `fps` frames, which is stricter than 3 flashes a second."""

    fps: int
    frames: int = 0
    worst: int = 0  # most transitions of one region in any window of fps frames
    worst_frame: int = 0  # last frame of that window
    max_delta: float = 0.0
    max_delta_frame: int = 0
    _prev: np.ndarray | None = None
    _luma: _Runs = field(default_factory=lambda: _Runs(THRESHOLD))
    _red: _Runs = field(default_factory=lambda: _Runs(RED_THRESHOLD))
    _windows: dict[tuple[str, int], deque[int]] = field(default_factory=dict)

    def add(self, frame: np.ndarray) -> None:
        self.push(*regions(frame))

    def push(
        self, lum: np.ndarray, red: np.ndarray | None = None, saturated: np.ndarray | None = None
    ) -> None:
        """Feed one frame's region luminances (linear, 0–1) and, optionally, its red values and
        saturated-red flags (see `regions`)."""
        k = self.frames
        self.frames += 1
        lum = np.asarray(lum, np.float64).ravel()
        if self._prev is not None:
            peak = float(np.abs(lum - self._prev).max())
            if peak > self.max_delta:
                self.max_delta, self.max_delta_frame = peak, k
        self._prev = lum
        runs = [("luma", self._luma, lum, np.zeros(lum.shape, bool))]
        if red is not None and saturated is not None:
            runs.append(
                ("red", self._red, np.asarray(red, np.float64).ravel(), np.asarray(saturated, bool).ravel())
            )
        for kind, run, values, tags in runs:
            run.push(values, tags)
            if kind == "luma":
                new = run.span & ~run.counted & (np.minimum(run.ext, run.pivot) < BRIGHT)
            else:
                new = run.span & ~run.counted & (run.pivot_tag | run.ext_tag)
            run.counted |= new
            for r in np.flatnonzero(new):
                self._windows.setdefault((kind, int(r)), deque()).append(k)
        for window in self._windows.values():
            while window and window[0] <= k - self.fps:
                window.popleft()
            if len(window) > self.worst:
                self.worst, self.worst_frame = len(window), k

    @property
    def passed(self) -> bool:
        return self.worst <= LIMIT

    def result(self) -> dict[str, Any]:
        return {
            "pass": self.passed,
            "max_transitions_per_s": self.worst,
            "max_delta": round(self.max_delta, 4),
            "frame": self.worst_frame if self.worst else self.max_delta_frame,
        }

    def check(self, name: str) -> None:
        if not self.passed:
            raise FlashError(
                f"{name}: {self.worst} flash transitions in one region within one second around frame "
                f"{self.worst_frame} (the limit is {LIMIT}); the film was not written"
            )


__all__ = [
    "IMAGES",
    "SURFACES",
    "FlashError",
    "LumaMeter",
    "PlanError",
    "cells",
    "check_plan",
    "regions",
    "surface_dip",
]
