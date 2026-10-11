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

Luminance (render time, every frame, every cut): each frame is downscaled to 1/8 (INTER_AREA),
converted from sRGB to linear light and reduced to Rec.709 relative luminance on a 4×4 grid of
cells. A transition is a frame pair in which any cell changes by |ΔL| ≥ 0.10 while the darker of the
two values is < 0.80 (WCAG 2.3.1's general flash threshold, per cell). A film fails if any window of
`fps` frames holds more than 3 transitions; the renderer then deletes its .part outputs and raises
FlashError. The meter only reads frames, so it never changes a film's bytes; its result is the
plan's `render.flash`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

import cv2
import numpy as np

from vantage.film.beat import min_dissolve, ramp

CELLS = 4
THRESHOLD = 0.10  # |ΔL| of a transition
BRIGHT = 0.80  # changes between two frames both at or above this are not counted
LIMIT = 3  # transitions allowed in any window of fps frames

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


def cells(frame: np.ndarray, grid: int = CELLS) -> np.ndarray:
    """Mean linear relative luminance of each cell of a grid × grid split of a BGR uint8 frame."""
    h, w = frame.shape[:2]
    small = cv2.resize(frame, (max(grid, w // 8), max(grid, h // 8)), interpolation=cv2.INTER_AREA)
    lum = _SRGB_TO_LINEAR[small] @ _BGR_WEIGHTS
    sh, sw = lum.shape
    ys = np.linspace(0, sh, grid + 1).round().astype(int)
    xs = np.linspace(0, sw, grid + 1).round().astype(int)
    return np.array(
        [[lum[ys[r] : ys[r + 1], xs[c] : xs[c + 1]].mean() for c in range(grid)] for r in range(grid)],
        np.float64,
    )


@dataclass
class LumaMeter:
    """Counts luminance transitions frame by frame; `result()` is the plan's render.flash record."""

    fps: int
    frames: int = 0
    worst: int = 0  # most transitions in any window of fps frames
    worst_frame: int = 0  # last frame of that window
    max_delta: float = 0.0
    max_delta_frame: int = 0
    _prev: np.ndarray | None = None
    _window: deque[int] = field(default_factory=deque)  # frame indices of recent transitions

    def add(self, frame: np.ndarray) -> None:
        self.push(cells(frame))

    def push(self, grid: np.ndarray) -> None:
        """Feed one frame's cell luminances (linear, 0–1)."""
        k = self.frames
        self.frames += 1
        prev, self._prev = self._prev, np.asarray(grid, np.float64)
        if prev is None:
            return
        delta = np.abs(self._prev - prev)
        darker = np.minimum(self._prev, prev)
        peak = float(delta.max())
        if peak > self.max_delta:
            self.max_delta, self.max_delta_frame = peak, k
        if bool(((delta >= THRESHOLD) & (darker < BRIGHT)).any()):
            self._window.append(k)
        while self._window and self._window[0] <= k - self.fps:
            self._window.popleft()
        if len(self._window) > self.worst:
            self.worst, self.worst_frame = len(self._window), k

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
                f"{name}: {self.worst} luminance transitions within one second around frame "
                f"{self.worst_frame} (the limit is {LIMIT}); the film was not written"
            )


__all__ = ["IMAGES", "SURFACES", "FlashError", "LumaMeter", "PlanError", "cells", "check_plan", "surface_dip"]
