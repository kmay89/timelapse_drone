"""Procedural "drone footage" of a fictional lakeside site changing over ~18 months.

The site is a planar, top-down world (meters; x east, y south) that is composed
and lit per visit -- an abandoned waterpark, then demolition, grading,
hardscape, structures, planting and a busy opening summer -- and photographed
through an exact pinhole drone camera. Because the world is a plane, the
world->image homography of every frame is known exactly; it is written to
``_truth.json`` so registration can be measured rather than eyeballed.

Everything is deterministic for a given seed; file names and dates are fixed so
``story.yaml`` can reference them.

    uv run python -m vantage.demo.synth projects/demo-lakeside/footage --fast
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import itertools
import json
import math
import multiprocessing
import os
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import ExifTags, Image

from vantage import log
from vantage.media.ffmpeg import FrameWriter

WORLD_M = (520.0, 340.0)
ORIGIN = (40.95120, -82.04780)  # lat/lon of world (0, 0): a generic, fictional site
GROUND_ASL_M = 296.0
HFOV_DEG = 73.7  # 24 mm-equivalent lens; same horizontal FOV for 16:9 video and 16:10 stills
TRUTH_NAME = "_truth.json"
_VERSION = 1

Points = np.ndarray  # (n, 2) float64, world meters
_Sprite = tuple[int, int, np.ndarray, np.ndarray]  # y0, x0, canopy coverage, normalized radius
_Grain = tuple[np.ndarray, np.ndarray]  # uint8 noise to add, uint8 noise to subtract


@dataclass(frozen=True)
class _Mode:
    ppm: float  # world texture pixels per meter
    video: tuple[int, int]
    fps: int
    seconds: float
    photo: tuple[int, int]
    crf: int
    preset: str


_FAST = _Mode(ppm=4.0, video=(960, 540), fps=24, seconds=3.0, photo=(1600, 1000), crf=23, preset="veryfast")
_FULL = _Mode(ppm=8.0, video=(1920, 1080), fps=30, seconds=5.0, photo=(3200, 2000), crf=19, preset="veryfast")

# Visit plan: date, local take-off time, takes. V/P overview video/photo, v/p shoreline
# video/photo, X a distractor clip shot straight down over the water.
_PLAN: tuple[tuple[str, str, str], ...] = (
    ("2025-04-12", "15:41:07", "VPp"),
    ("2025-05-30", "18:12:40", "Vv"),
    ("2025-07-18", "14:05:12", "VPp"),
    ("2025-09-05", "16:10:55", "VPpX"),
    ("2025-10-24", "15:22:03", "VPp"),
    ("2025-12-12", "13:40:31", "Pp"),
    ("2026-01-31", "14:02:44", "VPp"),
    ("2026-03-21", "15:47:19", "Vv"),
    ("2026-05-09", "16:31:26", "VPp"),
    ("2026-06-27", "19:05:50", "Pp"),
    ("2026-08-08", "16:15:08", "VPp"),
    ("2026-09-20", "17:24:36", "VPp"),
)
_TAKES = {  # code: vantage, kind, file number, seconds after take-off
    "V": ("overview", "video", 1, 0),
    "P": ("overview", "photo", 1, 52),
    "v": ("shoreline", "video", 2, 140),
    "p": ("shoreline", "photo", 2, 188),
    "X": ("distractor", "video", 3, 236),
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _rgb(r: float, g: float, b: float) -> np.ndarray:
    """A color as a (3, 1, 1) float32 array: broadcasts over channel-first (3, h, w) images."""
    return (np.array([r, g, b], np.float32) / 255.0).reshape(3, 1, 1)


def _smooth(x: np.ndarray | float) -> np.ndarray:
    x = np.clip(np.asarray(x, np.float32), 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def _ramp(p: float, a: float, b: float) -> float:
    return float(_smooth((p - a) / (b - a)))


def _lerp(a: np.ndarray, b: np.ndarray, t: np.ndarray | float) -> np.ndarray:
    return a + (b - a) * t


def _rect(cx: float, cy: float, w: float, depth: float, ang: float = 0.0) -> Points:
    """Corners of a w (along x) by depth (along y) rectangle rotated by `ang` degrees."""
    c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
    pts = np.array([[-w, -depth], [w, -depth], [w, depth], [-w, depth]], np.float64) / 2
    return pts @ np.array([[c, s], [-s, c]]) + (cx, cy)


def _arc(cx: float, cy: float, r: float, a0: float, a1: float, n: int = 48) -> Points:
    """Points on a circle; angles in degrees clockwise from north (world y points south)."""
    a = np.radians(np.linspace(a0, a1, n))
    return np.stack([cx + r * np.sin(a), cy - r * np.cos(a)], 1)


def _resample(line: Points, step: float) -> tuple[Points, Points]:
    """Evenly spaced points along a polyline and their unit normals."""
    seg = np.diff(line, axis=0)
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(seg[:, 0], seg[:, 1]))])
    s = np.arange(0.0, cum[-1] + 1e-9, step)
    pts = np.stack([np.interp(s, cum, line[:, 0]), np.interp(s, cum, line[:, 1])], 1)
    tang = np.gradient(pts, axis=0) if len(pts) > 1 else np.array([[1.0, 0.0]])
    tang /= np.maximum(np.hypot(tang[:, 0], tang[:, 1]), 1e-9)[:, None]
    return pts, np.stack([-tang[:, 1], tang[:, 0]], 1)


def _partial(line: Points, frac: float) -> Points | None:
    """The first `frac` of a polyline by arc length (None when nothing is built yet)."""
    if frac <= 0.0:
        return None
    seg = np.diff(line, axis=0)
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(seg[:, 0], seg[:, 1]))])
    end = cum[-1] * min(frac, 1.0)
    tail = np.array([np.interp(end, cum, line[:, 0]), np.interp(end, cum, line[:, 1])])
    return np.vstack([line[cum < end], tail])


def _crossbars(line: Points, step: float, width: float) -> list[Points]:
    """Short segments across a polyline every `step` meters (joints, planks)."""
    pts, nrm = _resample(line, step)
    return [np.stack([q - n * width / 2, q + n * width / 2]) for q, n in zip(pts, nrm, strict=True)]


def _latlon(x: float, y: float) -> tuple[float, float]:
    lat0, lon0 = ORIGIN
    return lat0 - y / 111_320.0, lon0 + x / (111_320.0 * math.cos(math.radians(lat0)))


# --------------------------------------------------------------------------- #
# Calendar: seasons, sun, visits
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Season:
    label: str
    snow: float  # ground snow cover 0..1
    ice: float  # lake ice 0..1
    green: float  # grass greenness (0 = dormant)
    leaf: float  # deciduous canopy leaf-out (0 = bare)
    autumn: float  # canopy turning
    blossom: float  # flowering ornamentals


# day of year: snow, ice, green, leaf, autumn, blossom
_SEASON_KEYS = np.array(
    [
        (0, 0.90, 0.70, 0.15, 0.00, 0.00, 0.00),
        (31, 1.00, 0.95, 0.12, 0.00, 0.00, 0.00),
        (59, 0.60, 0.50, 0.15, 0.00, 0.00, 0.00),
        (80, 0.14, 0.04, 0.25, 0.04, 0.00, 0.00),
        (102, 0.00, 0.00, 0.62, 0.40, 0.00, 0.85),
        (129, 0.00, 0.00, 0.92, 0.85, 0.00, 0.45),
        (150, 0.00, 0.00, 1.00, 1.00, 0.00, 0.05),
        (199, 0.00, 0.00, 0.92, 1.00, 0.00, 0.00),
        (248, 0.00, 0.00, 0.76, 1.00, 0.06, 0.00),
        (263, 0.00, 0.00, 0.72, 1.00, 0.22, 0.00),
        (297, 0.00, 0.00, 0.58, 0.85, 0.90, 0.00),
        (322, 0.05, 0.00, 0.35, 0.12, 1.00, 0.00),
        (346, 0.32, 0.10, 0.22, 0.00, 0.00, 0.00),  # a patchy first snow: bridges autumn and deep winter
        (366, 0.90, 0.70, 0.15, 0.00, 0.00, 0.00),
    ],
    np.float64,
)


def _season(day: dt.date) -> _Season:
    doy = day.timetuple().tm_yday
    vals = [float(np.interp(doy, _SEASON_KEYS[:, 0], _SEASON_KEYS[:, i])) for i in range(1, 7)]
    label = ("winter", "spring", "summer", "autumn")[(day.month % 12) // 3]
    return _Season(label, *vals)


def _sun(when_utc: dt.datetime, lat: float, lon: float) -> tuple[float, float]:
    """Approximate solar azimuth (degrees clockwise from north) and elevation (NOAA)."""
    doy = when_utc.timetuple().tm_yday
    hour = when_utc.hour + when_utc.minute / 60 + when_utc.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqt = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
        - 0.002697 * math.cos(3 * g)
        + 0.00148 * math.sin(3 * g)
    )
    ha = math.radians((hour * 60 + eqt + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    cz = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    el = 90 - math.degrees(math.acos(max(-1.0, min(1.0, cz))))
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(la) - math.tan(decl) * math.cos(la)))
    return (az + 180) % 360, el


def _utc_offset_h(day: dt.date) -> int:
    """US Eastern time: EDT from the 2nd Sunday of March to the 1st Sunday of November."""

    def sunday(month: int, n: int) -> dt.date:
        first = dt.date(day.year, month, 1)
        return first + dt.timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))

    return -4 if sunday(3, 2) <= day < sunday(11, 1) else -5


@dataclass(frozen=True)
class _Visit:
    index: int
    start: dt.datetime  # local wall clock at take-off
    utc_offset_h: int
    progress: float
    season: _Season
    sun_az: float
    sun_el: float
    codes: str

    @property
    def date(self) -> str:
        return self.start.date().isoformat()


def _visits() -> list[_Visit]:
    first = dt.date.fromisoformat(_PLAN[0][0])
    span = (dt.date.fromisoformat(_PLAN[-1][0]) - first).days
    out = []
    for i, (day, clock, codes) in enumerate(_PLAN):
        start = dt.datetime.fromisoformat(f"{day}T{clock}")
        off = _utc_offset_h(start.date())
        az, el = _sun(start - dt.timedelta(hours=off), *ORIGIN)
        progress = (start.date() - first).days / span
        out.append(_Visit(i, start, off, progress, _season(start.date()), az, el, codes))
    return out


@dataclass(frozen=True)
class _Stages:
    """Construction phase weights (0..1) derived from overall progress."""

    p: float
    demo: float
    grade: float
    hard: float
    struct: float
    soft: float
    live: float
    busy: float

    @classmethod
    def at(cls, p: float) -> _Stages:
        busy = _ramp(p, 0.03, 0.1) * (1 - _ramp(p, 0.8, 0.9))
        return cls(
            p,
            _ramp(p, 0.04, 0.26),
            _ramp(p, 0.17, 0.38),
            _ramp(p, 0.36, 0.58),
            _ramp(p, 0.48, 0.68),
            _ramp(p, 0.62, 0.86),
            _ramp(p, 0.84, 1.0),
            busy,
        )


# --------------------------------------------------------------------------- #
# Camera
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Camera:
    """Pinhole drone camera over the ground plane z = 0 (world: x east, y south, z down)."""

    x: float
    y: float
    alt: float
    heading: float  # degrees clockwise from north
    pitch: float  # degrees below the horizon (90 = nadir)
    roll: float = 0.0
    width: int = 1920
    height: int = 1080

    def rotation(self) -> np.ndarray:
        h, p, r = (math.radians(v) for v in (self.heading, self.pitch, self.roll))
        fwd = np.array([math.sin(h) * math.cos(p), -math.cos(h) * math.cos(p), math.sin(p)])
        right = np.array([math.cos(h), math.sin(h), 0.0])
        down = np.cross(fwd, right)
        return np.stack(
            [math.cos(r) * right + math.sin(r) * down, -math.sin(r) * right + math.cos(r) * down, fwd]
        )

    def intrinsics(self) -> np.ndarray:
        f = self.width / 2 / math.tan(math.radians(HFOV_DEG / 2))
        return np.array([[f, 0, (self.width - 1) / 2], [0, f, (self.height - 1) / 2], [0, 0, 1]])

    def homography(self) -> np.ndarray:
        """3x3 world (meters) -> image (pixels) homography of the ground plane."""
        rot = self.rotation()
        t = -rot @ np.array([self.x, self.y, -self.alt])
        hom = self.intrinsics() @ np.column_stack([rot[:, 0], rot[:, 1], t])
        return hom / hom[2, 2]

    def rays(self, step: int) -> np.ndarray:
        """Unit world-space view rays (east, south, down) on a grid every `step` pixels."""
        u, v = np.meshgrid(np.arange(0, self.width + step, step), np.arange(0, self.height + step, step))
        pix = np.stack([u, v, np.ones_like(u)], -1).astype(np.float64)
        d = pix @ (self.rotation().T @ np.linalg.inv(self.intrinsics())).T
        return d / np.linalg.norm(d, axis=-1, keepdims=True)


_FAMILIES = {
    "overview": _Camera(x=260.0, y=372.0, alt=115.0, heading=0.0, pitch=44.0),
    "shoreline": _Camera(x=302.0, y=124.0, alt=74.0, heading=196.0, pitch=45.0),
    "distractor": _Camera(x=286.0, y=166.0, alt=72.0, heading=24.0, pitch=90.0),
}
_JITTER = {  # translation m, yaw deg, roll deg, altitude fraction, pitch deg
    "overview": (7.0, 2.5, 1.5, 0.04, 2.0),
    "shoreline": (4.0, 2.5, 1.5, 0.04, 2.0),
    "distractor": (2.0, 4.0, 0.5, 0.04, 0.0),
}


def _jitter(cam: _Camera, rng: np.random.Generator, scale: Sequence[float]) -> _Camera:
    t, yaw, roll, alt, pitch = scale
    u = rng.uniform(-1, 1, 6)
    return replace(
        cam,
        x=cam.x + t * u[0],
        y=cam.y + t * u[1],
        heading=cam.heading + yaw * u[2],
        roll=cam.roll + roll * u[3],
        alt=cam.alt * (1 + alt * u[4]),
        pitch=min(90.0, cam.pitch + pitch * u[5]),
    )


def _hover(cam: _Camera, t: float, phase: Sequence[float]) -> _Camera:
    """Slow hover drift at time t (0 at the canonical frame): a gentle push plus wind wobble."""

    def wob(amp: float, freq: float, ph: float) -> float:
        return amp * (math.sin(freq * t + ph) - math.sin(ph))

    h = math.radians(cam.heading)
    push = 0.28 * t
    return replace(
        cam,
        x=cam.x + push * math.sin(h) + wob(0.9, 0.9, phase[0]),
        y=cam.y - push * math.cos(h) + wob(0.7, 0.7, phase[1]),
        alt=cam.alt + wob(0.35, 0.5, phase[2]),
        heading=cam.heading + wob(0.45, 0.33, phase[3]),
        roll=cam.roll + wob(0.12, 1.7, phase[4]),
        pitch=cam.pitch + wob(0.15, 0.6, phase[5]),
    )


# --------------------------------------------------------------------------- #
# Site layout (fixed geometry; organic edges and trees are seeded)
# --------------------------------------------------------------------------- #

_AXIS = 260.0
_LOT = (128.0, 306.0, 398.0, 338.0)  # x0, y0, x1, y1
_LOT_SPLIT = 252.0  # the old lot west of here becomes a meadow
_DRIVE = (254.0, 266.0)
_PLAZA = (260.0, 287.0, 12.0)
_GATEHOUSES = ((242.0, 299.0), (278.0, 299.0))
_BASIN = (260.0, 266.0, 46.0, 33.0)  # apex x, y, radius, half-angle: a fan opening north
_SPLASH = (260.0, 251.0, 9.0)
_LAZY = (172.0, 252.0, 14.0, 20.0)  # cx, cy, r_in, r_out
_TOWER = (352.0, 262.0)
_OLD_BUILDINGS = (  # cx, cy, w, depth, height, demolished at progress
    (205.0, 288.0, 24.0, 11.0, 4.5, 0.10),
    (318.0, 288.0, 18.0, 10.0, 4.0, 0.15),
    (260.0, 271.0, 16.0, 5.0, 5.0, 0.21),
)
_PAVILION = (322.0, 288.0, 17.0, 8.0)

POINTS_OF_INTEREST: dict[str, tuple[float, float]] = {
    "gatehouse_west": _GATEHOUSES[0],
    "gatehouse_east": _GATEHOUSES[1],
    "plaza": (_PLAZA[0], _PLAZA[1]),
    "splash_pad": (_SPLASH[0], _SPLASH[1]),
    "beach": (_AXIS, 232.0),
    "pier": (318.0, 196.0),
    "boardwalk": (240.0, 206.0),
    "pavilion": (_PAVILION[0], _PAVILION[1]),
    "garden_ring": (_LAZY[0], _LAZY[1]),
    "great_lawn": (356.0, 246.0),
    "slide_tower": _TOWER,
    "meadow": (226.0, 310.0),  # the strip of the old lot that stays inside the aligned overview frame
    "parking": (330.0, 322.0),
    "island": (150.0, 140.0),
}

_FOREST, _PARK, _VOLUNTEER, _ISLAND = 0, 1, 2, 3
_MAPLE, _GOLD, _OAK, _CONIFER, _ORNAMENTAL = 0, 1, 2, 3, 4


def _blob(rng: np.random.Generator, cx: float, cy: float, rx: float, ry: float, rough: float) -> Points:
    a = np.linspace(0, 2 * np.pi, 360, endpoint=False)
    r = np.ones_like(a)
    for k in range(2, 9):
        r += rough / k**0.8 * rng.uniform(0.3, 1.0) * np.sin(k * a + rng.uniform(0, 2 * np.pi))
    return np.stack([cx + rx * r * np.cos(a), cy + ry * r * np.sin(a)], 1)


@dataclass
class _Layout:
    lake: Points
    island: Points
    shore: Points  # south shoreline, west -> east
    roads: list[Points]
    work: Points  # the old park, the construction site
    paths: list[tuple[Points, float, float, float]]  # polyline, width, built between progress a..b
    old_walks: list[Points]
    tubes: list[Points]
    boardwalk: Points
    pier: Points
    slips: list[Points]
    old_dock: Points
    meadow_path: Points
    trees: np.ndarray  # x, y, radius, kind, tone, hue, group
    young: np.ndarray  # x, y, planting order, kind
    lights: Points
    cottages: list[tuple[float, float, float, float, float, int]]  # cx, cy, w, depth, angle, color
    lawns: list[Points]
    drives: list[Points]
    docks: list[Points]
    launch: Points  # gravel lot of the public boat launch
    ramp: Points
    riprap: list[Points]  # stone-armored stretches of shoreline

    def shore_y(self, x: float) -> float:
        return float(np.interp(x, self.shore[:, 0], self.shore[:, 1]))


def _layout(rng: np.random.Generator) -> _Layout:
    lake = _blob(rng, 262.0, 150.0, 206.0, 56.0, 0.11)
    south, north = lake[lake[:, 1] > 150.0], lake[lake[:, 1] < 150.0]
    shore, far = south[np.argsort(south[:, 0])], north[np.argsort(north[:, 0])]

    def sy(x: float) -> float:
        return float(np.interp(x, shore[:, 0], shore[:, 1]))

    def along_shore(x0: float, x1: float, off: float) -> Points:
        xs = np.linspace(x0, x1, 48)
        return np.stack([xs, [sy(x) + off for x in xs]], 1)

    def along_far(x0: float, x1: float) -> Points:
        xs = np.linspace(x0, x1, 48)
        return np.stack([xs, np.interp(xs, far[:, 0], far[:, 1]) - 0.6], 1)

    def pl(*pts: tuple[float, float]) -> Points:
        return np.array(pts, np.float64)

    roads = [
        pl((-5, 44), (80, 40), (180, 35), (300, 39), (420, 34), (525, 41)),
        pl((104, 345), (100, 300), (82, 250), (56, 200), (42, 150), (36, 90), (30, -5)),
        pl((420, 345), (430, 300), (452, 245), (474, 190), (486, 120), (492, -5)),
        pl((-5, 343), (104, 341), (260, 340), (420, 341), (525, 343)),
    ]
    xs = np.linspace(128, 402, 30)
    work = np.vstack([np.stack([xs, [sy(x) + 13 for x in xs]], 1), [(402, 304), (128, 304)]])
    ax, (lx, ly, *_), (tx, ty) = _AXIS, _LAZY, _TOWER
    paths = [
        (pl((250, 278), (232, 266), (222, 244), (228, 224), (246, 214)), 4.5, 0.36, 0.48),
        (pl((270, 278), (288, 266), (298, 244), (292, 224), (274, 214)), 4.5, 0.38, 0.50),
        (along_shore(126, 404, 9.0), 5.5, 0.40, 0.56),
        (_arc(lx, ly, 17.0, 0, 360, 72), 3.5, 0.44, 0.54),
        (pl((248, 290), (226, 282), (200, 274), (188, 268)), 3.5, 0.42, 0.50),
        (pl((272, 290), (300, 279), (330, 266), (352, 248), (366, 232), (372, 222)), 4.0, 0.44, 0.56),
        (pl((318, sy(318) + 9), (318, sy(318) - 1)), 4.0, 0.50, 0.52),
        (pl((ax, 276), (ax, 266)), 6.0, 0.36, 0.40),
    ]
    old_walks = [
        pl((ax, 300), (ax, 268)),
        pl((ax - 6, 284), (210, 270), (186, 262)),
        pl((ax + 6, 284), (310, 274), (344, 262)),
        _arc(_BASIN[0], _BASIN[1], _BASIN[2] + 4.0, -_BASIN[3] - 6, _BASIN[3] + 6),
        pl((200, 262), (205, 230), (232, 222)),
        pl((345, 255), (330, 232), (300, 222)),
    ]
    tubes = [
        pl((tx, ty), (tx - 8, ty - 6), (tx - 6, ty - 16), (tx - 16, ty - 22), (tx - 22, ty - 32)),
        pl((tx, ty), (tx + 6, ty - 8), (tx + 2, ty - 18), (tx - 6, ty - 26), (tx - 10, ty - 36)),
        pl((tx, ty), (tx - 10, ty + 2), (tx - 18, ty - 6), (tx - 24, ty - 18), (tx - 30, ty - 26)),
        pl((tx, ty), (tx + 10, ty - 2), (tx + 14, ty - 14), (tx + 8, ty - 24), (tx + 2, ty - 33)),
    ]
    s318 = sy(318)
    slips = [
        pl((318 + sx * 1.6, s318 - 8 - 6 * k), (318 + sx * 10, s318 - 8 - 6 * k))
        for k in range(4)
        for sx in (-1, 1)
    ]
    meadow_path = pl((150, 312), (176, 326), (210, 330), (236, 318), (214, 309), (180, 309), (150, 312))

    young = []
    for line, width, *_ in (paths[0], paths[1], paths[2], paths[5]):
        pts, nrm = _resample(line, 11.0)
        for side in (-1, 1):
            for q in pts[1:-1] + nrm[1:-1] * side * (width / 2 + 2.6):
                if not (ax - 30 < q[0] < ax + 30 and 214 < q[1] < 268):  # keep the beach open
                    young.append((q[0], q[1], rng.uniform(), rng.choice([_MAPLE, _GOLD, _OAK, _MAPLE])))
    light_pts, light_n = _resample(paths[2][0], 24.0)
    island = _blob(rng, 150.0, 140.0, 15.0, 10.0, 0.2)
    cottages, lawns, drives, docks = [], [], [], []
    for i, cx in enumerate((112, 158, 204, 251, 297, 343, 389, 432)):  # far-shore cottages
        x = cx + rng.uniform(-6, 6)
        ny = float(np.interp(x, far[:, 0], far[:, 1]))
        cy = ny - rng.uniform(15, 20)
        cottages.append((x, cy, rng.uniform(9, 12), rng.uniform(7, 9), rng.uniform(-12, 12), i % 5))
        lawns.append(_blob(rng, x, ny - 9, 15, 11, 0.25))
        drives.append(
            pl((x - 2, cy - 4), (x + rng.uniform(-8, 8), (cy + 38) / 2), (x + rng.uniform(-14, 14), 38))
        )
        docks.append(pl((x + 4, ny - 2), (x + 4, ny + rng.uniform(10, 16))))
    bx, by = 418.0, sy(418.0)  # public boat launch on the east shore
    launch = _rect(bx + 2, by + 16, 24, 16, -18)
    return _Layout(
        lake=lake,
        island=island,
        shore=shore,
        roads=roads,
        work=work,
        paths=paths,
        old_walks=old_walks,
        tubes=tubes,
        boardwalk=along_shore(208, 346, -1.8),
        pier=pl((318, s318 - 1), (318, s318 - 30)),
        slips=slips,
        old_dock=pl((296, sy(296) + 1), (296, sy(296) - 21)),
        meadow_path=meadow_path,
        trees=_scatter_trees(rng, lake, island, roads, work, sy, [*lawns, launch], drives),
        young=np.array(young, np.float64),
        lights=light_pts + light_n * (paths[2][1] / 2 + 0.8),
        cottages=cottages,
        lawns=lawns,
        drives=drives,
        docks=docks,
        launch=launch,
        ramp=pl((bx + 1, by + 9), (bx - 2, by - 9)),
        riprap=[
            along_shore(134, 204, 0.6),
            along_shore(350, 404, 0.6),
            along_far(96, 176),
            along_far(232, 286),
        ],
    )


def _scatter_trees(
    rng: np.random.Generator,
    lake: Points,
    island: Points,
    roads: list[Points],
    work: Points,
    sy: Callable[[float], float],
    clearings: list[Points],
    tracks: list[Points],
) -> np.ndarray:
    """Jittered-grid tree placement against a 1 px/m occupancy mask."""
    w, h = int(WORLD_M[0]), int(WORLD_M[1])

    def mask(polys: list[Points], grow: int = 0, shrink: int = 0) -> np.ndarray:
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [np.round(p).astype(np.int32) for p in polys], 1)
        if grow:
            m = cv2.dilate(m, np.ones((grow, grow), np.uint8))
        if shrink:
            m = cv2.erode(m, np.ones((shrink, shrink), np.uint8))
        return m

    blocked = mask([lake], grow=7) | mask([work]) | mask(clearings, grow=5)
    for line, width in [(r, 16) for r in roads] + [(t, 7) for t in tracks]:
        cv2.polylines(blocked, [np.round(line).astype(np.int32)], False, 1, width)
    cv2.rectangle(blocked, (int(_LOT[0]) - 4, int(_LOT[1]) - 4), (int(_LOT[2]) + 4, h), 1, -1)
    isl, inner = mask([island], shrink=3), mask([work], shrink=9)
    out = []
    for gy in np.arange(-2.0, h + 2, 6.0):
        for gx in np.arange(-2.0, w + 2, 6.0):
            x, y = gx + rng.uniform(-2.4, 2.4), gy + rng.uniform(-2.4, 2.4)
            xi, yi = int(np.clip(x, 0, w - 1)), int(np.clip(y, 0, h - 1))
            r = rng.uniform(2.2, 4.4) * (1.35 if rng.uniform() < 0.25 else 1.0)
            if isl[yi, xi]:
                group, keep = _ISLAND, 0.95
            elif inner[yi, xi]:
                group, keep, r = _VOLUNTEER, 0.05, r * 0.6
            elif blocked[yi, xi]:
                continue
            elif y < 95 or x < 70 or x > 462:
                group, keep = _FOREST, 0.95
            elif sy(x) + 3 < y < sy(x) + 15:
                group, keep = _PARK, 0.42  # old willows and cottonwoods along the shore
            else:
                group, keep = _FOREST, 0.62 if (y > 300 or x < 120 or x > 405) else 0.25
            if rng.uniform() > keep:
                continue
            kind = int(rng.choice([_MAPLE, _GOLD, _OAK, _CONIFER, _MAPLE, _OAK, _GOLD, _CONIFER]))
            if group == _PARK and rng.uniform() < 0.15:
                kind = _ORNAMENTAL
            out.append((x, y, r, kind, rng.uniform(-1, 1), rng.uniform(-1, 1), group))
    return np.array(out, np.float64)


# --------------------------------------------------------------------------- #
# Raster helpers
# --------------------------------------------------------------------------- #


def _fbm(
    rng: np.random.Generator,
    shape: tuple[int, int],
    cell: float,
    octaves: int = 4,
    stretch: tuple[float, float] = (1.0, 1.0),
) -> np.ndarray:
    """Fractal value noise (bicubic random lattices), zero mean and unit std, float32."""
    h, w = shape
    out = np.zeros(shape, np.float32)
    amp = 1.0
    for _ in range(octaves):
        cx, cy = max(cell * stretch[0], 1.5), max(cell * stretch[1], 1.5)
        gw, gh = int(w / cx) + 4, int(h / cy) + 4
        grid = rng.standard_normal((gh, gw), dtype=np.float32)
        big = cv2.resize(grid, (int(gw * cx), int(gh * cy)), interpolation=cv2.INTER_CUBIC)
        oy, ox = int(rng.integers(0, big.shape[0] - h + 1)), int(rng.integers(0, big.shape[1] - w + 1))
        out += amp * big[oy : oy + h, ox : ox + w]
        amp *= 0.5
        cell /= 2.0
    out -= out.mean()
    out /= out.std() + 1e-6
    return out


@dataclass
class _Stamp:
    """An anti-aliased alpha mask over a region of interest of the world texture."""

    y0: int
    x0: int
    a: np.ndarray

    @property
    def sl(self) -> tuple[slice, slice]:
        h, w = self.a.shape
        return slice(self.y0, self.y0 + h), slice(self.x0, self.x0 + w)


class _Canvas:
    """Rasterizes world-space (meter) polygons and polylines into ROI stamps."""

    SHIFT = 4

    def __init__(self, ppm: float, shape: tuple[int, int]) -> None:
        self.ppm, self.shape = ppm, shape

    def stamp(
        self,
        polys: Sequence[Points] = (),
        lines: Sequence[Points] = (),
        width: float = 1.0,
        feather: float = 0.0,
        closed: bool = False,
    ) -> _Stamp | None:
        parts = [p for p in (*polys, *lines) if len(p)]
        if not parts:
            return None
        pad = width * self.ppm / 2 + 3 * feather * self.ppm + 2
        cat = np.vstack(parts) * self.ppm
        x0, y0 = np.floor(cat.min(0) - pad).astype(int)
        x1, y1 = np.ceil(cat.max(0) + pad).astype(int)
        x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, self.shape[1]), min(y1, self.shape[0])
        if x1 <= x0 or y1 <= y0:
            return None
        buf = np.zeros((y1 - y0, x1 - x0), np.uint8)
        scale = 1 << self.SHIFT

        def fix(p: Points) -> np.ndarray:
            return np.round((p * self.ppm - (x0, y0)) * scale).astype(np.int32)

        if polys:
            cv2.fillPoly(buf, [fix(p) for p in polys], 255, cv2.LINE_AA, self.SHIFT)
        if lines:
            wpx = width * self.ppm  # sub-pixel lines: 1 px wide with proportional alpha
            val = round(255 * min(1.0, wpx))
            cv2.polylines(
                buf, [fix(p) for p in lines], closed, val, max(1, round(wpx)), cv2.LINE_AA, self.SHIFT
            )
        a = buf.astype(np.float32) * (1 / 255)
        if feather > 0:
            a = cv2.GaussianBlur(a, (0, 0), feather * self.ppm)
        return _Stamp(y0, x0, a)


def _paint(
    img: np.ndarray, st: _Stamp | None, color: np.ndarray | float, k: float | np.ndarray = 1.0
) -> None:
    """Alpha-blend a color (or ROI-sized field) into a (3, h, w) or (h, w) image under a stamp."""
    if st is None or (np.isscalar(k) and k <= 0):
        return
    reg = img[(..., *st.sl)]
    reg += (color - reg) * (st.a * k)


def _lift(hmap: np.ndarray, st: _Stamp | None, height: float | np.ndarray) -> None:
    if st is not None:
        reg = hmap[st.sl]
        np.maximum(reg, st.a * height, out=reg)


# --------------------------------------------------------------------------- #
# Palette
# --------------------------------------------------------------------------- #

_C = {
    "lush": _rgb(70, 104, 46),
    "lush2": _rgb(102, 130, 58),
    "olive": _rgb(118, 126, 70),
    "dormant": _rgb(150, 136, 102),
    "dry": _rgb(166, 150, 104),
    "soil": _rgb(116, 94, 72),
    "earth": _rgb(150, 122, 94),
    "earth_dark": _rgb(102, 82, 64),
    "asphalt_old": _rgb(112, 112, 108),
    "asphalt_new": _rgb(42, 44, 46),
    "milled": _rgb(130, 128, 122),
    "concrete_old": _rgb(166, 162, 152),
    "concrete_new": _rgb(210, 204, 192),
    "paver_warm": _rgb(192, 164, 134),
    "paver_dark": _rgb(110, 104, 98),
    "basin": _rgb(140, 182, 186),
    "stain": _rgb(70, 78, 64),
    "sand": _rgb(224, 206, 170),
    "water_deep": _rgb(18, 42, 50),
    "water_shallow": _rgb(50, 82, 74),
    "water_green": _rgb(60, 80, 48),
    "rust": _rgb(142, 84, 52),
    "roof_new": _rgb(58, 70, 72),
    "bronze": _rgb(128, 100, 76),
    "roof_flat": _rgb(150, 146, 138),
    "solar": _rgb(28, 38, 62),
    "timber_old": _rgb(122, 114, 104),
    "timber_new": _rgb(176, 134, 92),
    "snow": _rgb(238, 242, 248),
    "ice": _rgb(184, 198, 206),
    "yellow": _rgb(234, 170, 30),
    "dark": _rgb(40, 40, 38),
    "glass": _rgb(22, 28, 34),
    "mulch": _rgb(84, 60, 44),
    "meadow": _rgb(138, 140, 80),
    "line_white": _rgb(234, 234, 228),
    "line_yellow": _rgb(222, 180, 60),
    "fence": _rgb(34, 52, 42),
    "teal": _rgb(54, 150, 158),
    "sky_blue": _rgb(90, 160, 196),
    "orange": _rgb(226, 112, 58),
    "ev": _rgb(60, 130, 90),
}
_CAR_COLORS = [
    _rgb(*c)
    for c in [
        (232, 232, 230),
        (190, 192, 194),
        (30, 32, 36),
        (120, 122, 126),
        (40, 58, 96),
        (150, 30, 34),
        (70, 76, 70),
        (210, 206, 196),
    ]
]
_CLOTHES = [
    _rgb(*c)
    for c in [
        (230, 90, 60),
        (40, 70, 140),
        (240, 240, 236),
        (60, 60, 64),
        (230, 190, 60),
        (90, 150, 110),
        (200, 80, 130),
        (120, 180, 210),
    ]
]
_UMBRELLAS = [_rgb(*c) for c in [(226, 112, 58), (244, 240, 230), (54, 150, 158), (234, 190, 70)]]
_BLANKETS = [_rgb(*c) for c in [(196, 96, 74), (70, 96, 140), (222, 214, 196), (200, 170, 90)]]
_KAYAKS = [_rgb(*c) for c in [(236, 120, 40), (240, 200, 50), (220, 60, 50), (60, 160, 170)]]
_ROOFS = [_rgb(*c) for c in [(98, 60, 52), (74, 80, 88), (136, 124, 106), (62, 80, 66), (158, 156, 150)]]
_TUBES = [_rgb(70, 120, 170), _rgb(210, 170, 60), _rgb(180, 70, 60), _rgb(90, 150, 150)]
_CANOPY = {  # summer, autumn
    _MAPLE: (_rgb(62, 94, 42), _rgb(200, 76, 36)),
    _GOLD: (_rgb(82, 108, 46), _rgb(220, 172, 54)),
    _OAK: (_rgb(58, 84, 42), _rgb(156, 96, 48)),
    _CONIFER: (_rgb(36, 60, 42), _rgb(36, 60, 42)),
    _ORNAMENTAL: (_rgb(84, 114, 56), _rgb(180, 54, 44)),
}


_SUMMER = np.stack([_CANOPY[k][0].ravel() for k in range(5)])
_FALL = np.stack([_CANOPY[k][1].ravel() for k in range(5)])
_HUE = np.array([-1.0, 0.25, 1.0], np.float32)  # tilts greens toward yellow (-) or blue (+)


def _canopy_colors(
    kinds: np.ndarray, tone: np.ndarray, hue: np.ndarray, turn: np.ndarray, s: _Season, autumn: float = 1.0
) -> np.ndarray:
    """Per-tree crown colors (n, 3) for a season: leaf-out, autumn turning, blossom, bare winter twigs."""
    t = np.clip((s.autumn * autumn - 0.5 * turn) * 2.0, 0, 1)[:, None]
    col = (_SUMMER[kinds] + (_FALL[kinds] - _SUMMER[kinds]) * t) * (1 + 0.12 * tone)[:, None]
    col *= 1 + 0.08 * hue[:, None] * _HUE
    deciduous = (kinds != _CONIFER)[:, None]
    if s.leaf < 1:
        bare, fresh = np.array([0.24, 0.21, 0.19]), np.array([0.51, 0.62, 0.28])
        spring = bare + (fresh - bare) * min(1.0, s.leaf * 2.2)
        col = np.where(deciduous, spring + (col - spring) * s.leaf**2, col)
    if s.blossom > 0:
        col = np.where(
            (kinds == _ORNAMENTAL)[:, None], col + (np.array([0.91, 0.78, 0.82]) - col) * 0.6 * s.blossom, col
        )
    return col.astype(np.float32)


# --------------------------------------------------------------------------- #
# World: static fields + per-visit composition and lighting
# --------------------------------------------------------------------------- #


class _World:
    """The site at one resolution. Static noise and geometry are built once; `render` composes a visit."""

    def __init__(self, seed: int, ppm: float) -> None:
        self.seed, self.ppm = seed, ppm
        self.w, self.h = round(WORLD_M[0] * ppm), round(WORLD_M[1] * ppm)
        rng = np.random.default_rng([seed, 1])
        self.lay = _layout(rng)
        self.cv = _Canvas(ppm, (self.h, self.w))
        shape = (self.h, self.w)
        self.n_large = _fbm(rng, shape, 70 * ppm, 3)
        self.n_mid = _fbm(rng, shape, 14 * ppm, 4)
        self.n_fine = _fbm(rng, shape, 2.6 * ppm, 3)
        self.n_micro = _fbm(rng, shape, 0.7 * ppm, 2)
        self.n_crack = _fbm(rng, shape, 9 * ppm, 3)
        self.u_reveal = 0.5 + 0.5 * np.tanh(0.9 * _fbm(rng, shape, 22 * ppm, 2))  # ~uniform 0..1
        self.relief = 0.35 * self.n_large + 0.12 * self.n_mid
        self.xm = np.arange(self.w, dtype=np.float32) / ppm  # world x of each column
        self.ym = np.arange(self.h, dtype=np.float32) / ppm  # world y of each row
        self.g_mix = 0.5 + 0.5 * np.tanh(1.2 * self.n_large + 0.4 * self.n_mid)
        grain = cv2.GaussianBlur(rng.standard_normal(shape, dtype=np.float32), (0, 0), 0.7)
        self.g_var = 1 + 0.07 * self.n_mid + 0.09 * self.n_fine + 0.08 * self.n_micro + 0.11 * grain
        self.g_bare = 0.8 * _smooth((0.8 * self.n_mid + 0.5 * self.n_large - 1.5) * 1.5)

        lake = self.cv.stamp([self.lay.lake], feather=0.3)
        isl = self.cv.stamp([self.lay.island])
        assert lake is not None and isl is not None
        self.lake, self.island = lake, isl
        inside = (lake.a > 0.5).astype(np.uint8)
        self._on_lake(inside, isl, lambda r, a: np.where(a > 0.5, 0, r).astype(np.uint8))
        self.lake_dist = cv2.distanceTransform(inside, cv2.DIST_L2, 5) / ppm  # meters from shore
        self.ripple = _fbm(rng, lake.a.shape, 1.1 * ppm, 3, stretch=(3.0, 0.8))
        trng = np.random.default_rng([seed, 5])
        self.tree_phase = trng.uniform(0, 2 * np.pi, (len(self.lay.trees) + len(self.lay.young), 3))
        self.tree_turn = trng.uniform(0, 1, len(self.tree_phase))
        self.sprites = [
            self._sprite(x, y, r, self.tree_phase[i]) for i, (x, y, r, *_) in enumerate(self.lay.trees)
        ]

    def _on_lake(
        self, arr: np.ndarray, st: _Stamp, fn: Callable[[np.ndarray, np.ndarray], np.ndarray]
    ) -> None:
        """Apply fn(region, alpha) to the part of a lake-ROI array covered by `st`."""
        oy, ox = st.y0 - self.lake.y0, st.x0 - self.lake.x0
        h, w = st.a.shape
        arr[oy : oy + h, ox : ox + w] = fn(arr[oy : oy + h, ox : ox + w], st.a)

    def _sprite(self, x: float, y: float, r: float, ph: Sequence[float]) -> _Sprite | None:
        """A lumpy round canopy footprint."""
        ppm, rp = self.ppm, r * self.ppm * 1.2
        x0, y0 = max(int(x * ppm - rp) - 1, 0), max(int(y * ppm - rp) - 1, 0)
        x1, y1 = min(int(x * ppm + rp) + 2, self.w), min(int(y * ppm + rp) + 2, self.h)
        if x1 <= x0 or y1 <= y0:
            return None
        dx, dy = self.xm[None, x0:x1] - x, self.ym[y0:y1, None] - y
        ang = np.arctan2(dy, dx)
        reff = r * (
            1
            + 0.1 * np.sin(3 * ang + ph[0])
            + 0.07 * np.sin(5 * ang + ph[1])
            + 0.04 * np.sin(8 * ang + ph[2])
        )
        rr = np.hypot(dx, dy) / reff
        cover = np.clip((1 - rr) * reff * ppm * 0.8, 0, 1).astype(np.float32)
        return (y0, x0, cover, rr.astype(np.float32)) if cover.any() else None

    def _mat(
        self, st: _Stamp, base: np.ndarray, amp: Sequence[float] = (0.05, 0.05, 0.05, 0.04)
    ) -> np.ndarray:
        """A material color modulated by the four noise octaves under a stamp, (3, h, w)."""
        sl = st.sl
        v = 1 + amp[0] * self.n_large[sl] + amp[1] * self.n_mid[sl] + amp[2] * self.n_fine[sl]
        v += amp[3] * self.n_micro[sl]
        return base * v

    def _reveal(self, st: _Stamp, frac: float, soft: float = 0.08) -> np.ndarray:
        """Patchy spatial progress under a stamp: 0 where not reached yet, 1 where done."""
        return _smooth(((1 + 2 * soft) * frac - soft - self.u_reveal[st.sl]) / soft)

    def _px(self, pts: Points) -> np.ndarray:
        return np.round(np.asarray(pts) * self.ppm * 16).astype(np.int32)

    def _box(self, alb: np.ndarray, hmap: np.ndarray, pts: Points, color: np.ndarray, height: float) -> None:
        q = self._px(pts)
        for c in range(3):
            cv2.fillConvexPoly(alb[c], q, float(color[c, 0, 0]), cv2.LINE_8, 4)
        cv2.fillConvexPoly(hmap, q, float(height), cv2.LINE_8, 4)

    def _dot(
        self,
        alb: np.ndarray,
        hmap: np.ndarray,
        x: float,
        y: float,
        r: float,
        color: np.ndarray,
        height: float,
    ) -> None:
        c = (round(x * self.ppm * 16), round(y * self.ppm * 16))
        rad = max(12, round(r * self.ppm * 16))
        for ch in range(3):
            cv2.circle(alb[ch], c, rad, float(color[ch, 0, 0]), -1, cv2.LINE_8, 4)
        cv2.circle(hmap, c, rad, float(height), -1, cv2.LINE_8, 4)

    def _grass(self, s: _Season) -> tuple[np.ndarray, np.ndarray]:
        g = s.green
        a = (
            _lerp(_C["dormant"], _C["olive"], g * 2)
            if g < 0.5
            else _lerp(_C["olive"], _C["lush"], (g - 0.5) * 2)
        )
        return a, _lerp(_C["dry"], _C["lush2"], g)

    # -- per visit -------------------------------------------------------- #

    def render(self, v: _Visit, rng: np.random.Generator) -> np.ndarray:
        """Compose, light and return the visit's world texture as BGRA uint8 (A = open water)."""
        st, s = _Stages.at(v.progress), v.season
        a, b = self._grass(s)
        alb = (a + (b - a) * self.g_mix) * self.g_var
        alb += (_C["soil"] * (1 + 0.08 * self.n_fine) - alb) * self.g_bare
        hmap = np.zeros((self.h, self.w), np.float32)
        plowed = np.zeros_like(hmap)
        self._roads(alb, plowed)
        water = self._lake(alb, s)
        self._neighbors(alb, hmap, plowed, s)
        self._work_ground(alb, st, s)
        self._lot(alb, hmap, plowed, v, st)
        self._old_park(alb, hmap, st, s)
        self._plaza(alb, plowed, st)
        self._paths(alb, plowed, st)
        self._beach(alb, hmap, st, rng)
        self._shore(alb, hmap, plowed, st, rng)
        self._buildings(alb, hmap, st)
        if st.busy > 0:
            self._construction(alb, hmap, plowed, st, rng)
        if st.live > 0:
            self._people(alb, hmap, st.live, rng)
        canopy = self._trees(alb, hmap, v)
        self._snow(alb, hmap, plowed, canopy, s)
        ly, lx = self.lake.sl  # boats and crowns float on the water; shoreline trees darken it
        refl = cv2.GaussianBlur(canopy[ly, lx], (0, 0), 3.0 * self.ppm)
        alb[:, ly, lx] *= 1 - 0.35 * refl * self.lake.a
        water *= np.clip(1 - 4 * hmap[ly, lx], 0, 1) * (1 - 0.5 * refl)
        lit = self._light(alb, hmap, v)
        planes = [np.clip(lit[c] * 225.0, 0, 255).astype(np.uint8) for c in (2, 1, 0)]
        bgr = cv2.merge(planes)
        bgr = cv2.addWeighted(bgr, 1.45, cv2.GaussianBlur(bgr, (0, 0), 1.0), -0.45, 0)
        alpha = np.zeros((self.h, self.w), np.uint8)
        alpha[self.lake.sl] = np.clip(water * 255, 0, 255).astype(np.uint8)
        return cv2.merge([*cv2.split(bgr), alpha])

    def _roads(self, alb: np.ndarray, plowed: np.ndarray) -> None:
        for road in self.lay.roads:
            verge = self.cv.stamp(lines=[road], width=10.0, feather=0.6)
            if verge is not None:
                _paint(alb, verge, self._mat(verge, _C["dry"] * 0.9), 0.35)
            st = self.cv.stamp(lines=[road], width=7.6, feather=0.15)
            if st is None:
                continue
            _paint(alb, st, self._mat(st, _C["asphalt_old"] * 0.8, (0.04, 0.05, 0.05, 0.07)))
            _paint(plowed, st, 1.0)
            pts, nrm = _resample(road, 0.5)
            edges = self.cv.stamp(lines=[pts + nrm * 3.4, pts - nrm * 3.4], width=0.14)
            _paint(alb, edges, _C["line_white"], 0.7)
            dashes = [
                pts[i : i + 7] + nrm[i : i + 7] * off
                for i in range(0, len(pts) - 7, 18)
                for off in (-0.12, 0.12)
            ]
            _paint(alb, self.cv.stamp(lines=dashes, width=0.11), _C["line_yellow"], 0.85)

    def _neighbors(self, alb: np.ndarray, hmap: np.ndarray, plowed: np.ndarray, s: _Season) -> None:
        """What never changes: far-shore cottages with lawns, driveways and docks; the public boat launch."""
        cv, lay = self.cv, self.lay
        la, lb = self._grass(replace(s, green=min(1.0, 1.15 * s.green)))
        for lawn in lay.lawns:
            st = cv.stamp([lawn], feather=1.2)
            if st is not None:
                _paint(
                    alb,
                    st,
                    _lerp(la, lb, 0.4) * (1 + 0.04 * self.n_mid[st.sl] + 0.03 * self.n_fine[st.sl]),
                    0.9,
                )
        gravel = _rgb(174, 164, 144)
        for st in (cv.stamp(lines=lay.drives, width=3.0, feather=0.2), cv.stamp([lay.launch], feather=0.2)):
            if st is not None:
                _paint(alb, st, self._mat(st, gravel, (0.04, 0.06, 0.08, 0.1)))
                _paint(plowed, st, 1.0)
        ramp = cv.stamp(lines=[lay.ramp], width=5.0)
        if ramp is not None:
            _paint(alb, ramp, self._mat(ramp, _C["concrete_old"]))
        rip = cv.stamp(lines=lay.riprap, width=3.4, feather=0.3)
        if rip is not None:  # boulders: albedo-driven texture that survives any light or season
            rocks = _smooth((self.n_micro[rip.sl] + 0.6 * self.n_fine[rip.sl] + 0.2) * 1.6)
            _paint(alb, rip, _rgb(150, 144, 134) * (0.5 + 0.65 * rocks))
            _lift(hmap, rip, 0.2 + 0.5 * rocks)
        docks = cv.stamp(lines=lay.docks, width=2.2)
        if docks is not None:
            _paint(alb, docks, self._mat(docks, _C["timber_old"] * 1.1, (0.03, 0.05, 0.1, 0.12)))
            _lift(hmap, docks, 0.7)
        boats = s.ice == 0 and s.snow == 0
        for i, ((x, y, w, depth, ang, color), dock) in enumerate(zip(lay.cottages, lay.docks, strict=True)):
            self._roof(alb, hmap, x, y, w, depth, 3.0, 5.4, _ROOFS[color], seams=color % 2 == 1, ang=ang)
            if boats and i % 3 != 1:
                self._box(
                    alb, hmap, _rect(dock[1, 0] + 2.6, dock[1, 1] - 2.5, 1.9, 5.2), _rgb(236, 236, 230), 1.0
                )

    def _lake(self, alb: np.ndarray, s: _Season) -> np.ndarray:
        st, d = self.lake, self.lake_dist
        sl = st.sl
        n_mid, n_fine, n_micro = self.n_mid[sl], self.n_fine[sl], self.n_micro[sl]
        margin = self.cv.stamp([self.lay.lake], feather=1.6)
        if margin is not None:
            _paint(alb, margin, self._mat(margin, _C["soil"] * 0.75), 0.6)
        col = _lerp(_C["water_shallow"], _C["water_deep"], 1 - np.exp(-d / 16.0))
        col *= 1 + 0.09 * self.ripple + 0.05 * self.n_large[sl] + 0.03 * n_micro
        reeds = _smooth((n_mid + 0.4 * n_fine - 0.7) * 2) * (d < 5) * (d > 0.5)
        col = _lerp(col, _C["water_green"] * (0.8 + 0.25 * n_micro), reeds * 0.8)
        foam = np.clip(1 - d / 0.9, 0, 1) * (d > 0) * (0.5 + 0.5 * n_fine)
        col = _lerp(col, _rgb(200, 204, 196), foam * 0.35)
        _paint(alb, st, col)
        water = (
            st.a * (1 - reeds * 0.7) * (d > 0) * (0.72 + 0.28 * _smooth(0.5 + 0.6 * n_mid))
        )  # wind patches
        if s.ice > 0:
            edge = 4 + s.ice * 70 * (1 + 0.35 * self.n_large[sl]) + 4 * n_mid + 1.5 * n_fine
            ice = _smooth((edge - d) / 3.0) * (d > 0)
            skim = _smooth((edge + 7 - d) / 5.0) * (d > 0) * (1 - ice)  # thin gray new ice beyond the edge
            _paint(alb, st, _C["ice"] * 0.62, skim * 0.45)
            water *= 1 - 0.4 * skim
            swept = _smooth((self.n_large[sl] - 0.4) * 2)  # wind-swept patches of bare, darker ice
            snowy = _smooth(s.snow * 1.4 - 0.4 + 0.3 * n_mid - 0.8 * swept) * ice
            icecol = _lerp(_C["ice"] * (0.86 + 0.04 * n_fine), _C["snow"], snowy)
            icecol = _lerp(icecol, _C["ice"] * 0.7, (np.abs(self.n_crack[sl]) < 0.012) * 0.35 * (1 - snowy))
            _paint(alb, st, icecol, ice)
            water *= 1 - ice
        g, _ = self._grass(s)
        _paint(alb, self.island, self._mat(self.island, g * 0.9))
        self._on_lake(water, self.island, lambda r, a: r * (1 - a))
        return water.astype(np.float32)

    # -- the site through time ------------------------------------------- #

    def _work_ground(self, alb: np.ndarray, st: _Stages, s: _Season) -> None:
        """The park's ground: overgrown weeds -> demolition rubble -> graded earth -> new sod."""
        work = self.cv.stamp([self.lay.work], feather=1.5)
        assert work is not None
        sl = work.sl
        n_mid, n_fine, n_micro = self.n_mid[sl], self.n_fine[sl], self.n_micro[sl]
        ga, gb = self._grass(s)
        weeds = _lerp(_C["dry"], _lerp(ga, gb, 0.5), 0.6) * (1 + 0.12 * n_mid + 0.12 * n_fine + 0.1 * n_micro)
        _paint(alb, work, weeds, 0.85)
        gone = self._reveal(work, st.demo)
        earth = self._mat(work, _C["earth"], (0.05, 0.08, 0.06, 0.06))
        rubble = _lerp(
            earth * (0.84 + 0.05 * n_fine), _C["concrete_old"] * (0.8 + 0.1 * n_micro), (n_micro > 1.3) * 0.7
        )
        _paint(alb, work, rubble, gone * (1 - st.grade))
        graded = self._reveal(work, st.grade, 0.1) * gone
        stripes = np.sin(self.ym[sl[0], None] * 3.3 + 1.5 * n_mid)
        _paint(alb, work, earth * (1 + 0.035 * stripes), graded)
        if st.soft > 0:  # sod laid in strips, west to east
            east = (self.xm[None, sl[1]] - 120) / 290
            sod = _smooth((1.2 * st.soft - (0.25 * self.u_reveal[sl] + 0.75 * east)) / 0.03)
            la, lb = self._grass(replace(s, green=min(1.0, 1.25 * s.green)))  # new sod is irrigated
            lawn = _lerp(la, lb, 0.35) * (1 + 0.03 * n_mid + 0.04 * n_fine)
            mow = np.sign(np.sin(self.xm[None, sl[1]] * 0.45))
            seams = np.abs((self.xm[None, sl[1]] % 2.4) - 1.2) < 0.05
            lawn *= (1 + 0.035 * mow * st.live) * (1 - 0.08 * seams * (1 - st.live))
            _paint(alb, work, lawn, sod)
        if st.busy > 0:  # construction fence around the site
            fence = self.cv.stamp(lines=[self.lay.work], width=0.35, closed=True)
            _paint(alb, fence, _C["fence"], 0.85 * st.busy)

    def _lot(self, alb: np.ndarray, hmap: np.ndarray, plowed: np.ndarray, v: _Visit, st: _Stages) -> None:
        cv, p = self.cv, v.progress
        x0, y0, x1, y1 = _LOT
        lot = cv.stamp([_rect((x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0)], feather=0.15)
        drive = cv.stamp([_rect(_AXIS, 320, _DRIVE[1] - _DRIVE[0], 40)], feather=0.1)
        assert lot is not None and drive is not None
        sl = lot.sl
        crack = (np.abs(self.n_crack[sl]) < 0.03) | (np.abs(self.n_fine[sl]) < 0.012)
        stains = _smooth((self.n_mid[sl] - 1.1) * 2)
        old = (
            self._mat(lot, _C["asphalt_old"], (0.06, 0.07, 0.07, 0.08))
            * (1 - 0.28 * crack)
            * (1 - 0.18 * stains)
        )
        old = _lerp(old, _C["olive"] * 0.85, crack * (self.n_fine[sl] > 0.2) * 0.8 * (1 - st.demo))
        _paint(alb, lot, old)
        _paint(alb, drive, self._mat(drive, _C["asphalt_old"] * 0.95))
        xs = self.xm[None, sl[1]]
        west, east = (xs < _LOT_SPLIT).astype(np.float32), (xs > _DRIVE[1]).astype(np.float32)
        rip, mill, pave = _ramp(p, 0.2, 0.34), _ramp(p, 0.40, 0.46), _ramp(p, 0.46, 0.5)
        _paint(alb, lot, self._mat(lot, _C["earth_dark"], (0.05, 0.08, 0.08, 0.06)), west * rip)
        if st.soft > 0:  # west: a pollinator meadow with a mown loop
            ga, gb = self._grass(v.season)
            meadow = _lerp(_C["meadow"], gb, 0.4) * (
                1 + 0.12 * self.n_mid[sl] + 0.14 * self.n_fine[sl] + 0.1 * self.n_micro[sl]
            )
            if v.season.green > 0.5:  # scattered single blooms: coneflower, black-eyed susan, yarrow
                speck = np.random.default_rng([self.seed, 31]).random((2, *lot.a.shape), dtype=np.float32)
                bloom = (speck[0] > 0.94) * (self.n_mid[sl] > -0.4) * st.live
                tint = np.where(
                    speck[1] < 0.4,
                    _rgb(196, 140, 200),
                    np.where(speck[1] < 0.8, _rgb(238, 200, 70), _rgb(240, 238, 228)),
                )
                meadow = _lerp(meadow, tint, bloom * 0.9)
            _paint(alb, lot, meadow, west * st.soft)
            mp = cv.stamp(lines=[self.lay.meadow_path], width=2.2, feather=0.3)
            if mp is not None:
                _paint(alb, mp, self._mat(mp, _lerp(ga, gb, 0.5) * 0.95), st.soft)
        _paint(alb, lot, self._mat(lot, _C["milled"], (0.04, 0.05, 0.08, 0.1)), east * mill * (1 - pave))
        _paint(alb, lot, self._mat(lot, _C["asphalt_new"], (0.03, 0.04, 0.05, 0.08)), east * pave)
        _paint(alb, drive, self._mat(drive, _C["asphalt_new"], (0.03, 0.04, 0.05, 0.08)), pave)
        _paint(plowed, lot, 1.0, east * pave)
        _paint(plowed, drive, 1.0)
        # Stall lines: faded originals until milled or torn out, crisp new ones after repaving.
        rows = [(y0 + 0.5, y0 + 6.0), (y0 + 13.0, y0 + 18.5), (y0 + 18.5, y0 + 24.0)]
        stall_x = [x for x in np.arange(x0 + 4, x1 - 4, 2.75) if not _DRIVE[0] - 1 < x < _DRIVE[1] + 1]
        segs = [np.array([(x, ra), (x, rb)]) for ra, rb in rows for x in stall_x]
        mid = y0 + 18.5
        old_lines = cv.stamp(lines=[*segs, np.array([(x0 + 4, mid), (x1 - 4, mid)])], width=0.13)
        if old_lines is not None:
            lx = self.xm[None, old_lines.sl[1]]
            k = 0.75 * (1 - (lx < _LOT_SPLIT) * rip) * (1 - (lx > _DRIVE[0]) * mill)
            _paint(
                alb,
                old_lines,
                _C["line_white"] * 0.88,
                k * (0.55 + 0.45 * (self.n_fine[old_lines.sl] > -0.6)),
            )
        restripe = _ramp(p, 0.5, 0.53)
        if restripe > 0:
            new = [s for s in segs if s[0, 0] > _DRIVE[1]] + [np.array([(_DRIVE[1] + 1, mid), (x1 - 4, mid)])]
            _paint(alb, cv.stamp(lines=new, width=0.14), _C["line_white"], 0.95 * restripe)
            ev = cv.stamp([_rect(_DRIVE[1] + 2.75 * (k + 1), y0 + 3.2, 2.5, 5.2) for k in range(6)])
            _paint(alb, ev, _C["ev"], 0.8 * restripe)
            cross = cv.stamp([_rect(_AXIS + dx, y0 - 2.5, 0.6, 4.0) for dx in np.arange(-5, 5.1, 1.2)])
            _paint(alb, cross, _C["line_white"], 0.9 * restripe)
        for pole in np.arange(x0 + 22, x1 - 10, 30.0):  # light poles on the center line
            self._dot(alb, hmap, pole, mid, 0.3, _C["concrete_old"], 9.0)
        curb = [np.array([(_DRIVE[0], y0), (x0, y0), (x0, y1), (x1, y1), (x1, y0), (_DRIVE[1], y0)])]
        _paint(alb, cv.stamp(lines=curb, width=0.35), _C["concrete_old"] * 1.05, 0.9)
        # Cars: workers' pickups while busy, a full lot once open.
        crng = np.random.default_rng([self.seed, 77, v.index])
        for ra, rb in rows:
            for x in stall_x:
                if x > _DRIVE[1] and crng.uniform() < 0.55 * st.live:
                    self._car(
                        alb,
                        hmap,
                        x + 1.375 + crng.uniform(-0.15, 0.15),
                        (ra + rb) / 2 + crng.uniform(-0.3, 0.3),
                        180 * crng.integers(2) + crng.uniform(-3, 3),
                        crng,
                    )
        for _ in range(int(9 * st.busy)):
            self._car(
                alb,
                hmap,
                crng.uniform(340, 392),
                crng.uniform(y0 + 2, y0 + 24),
                crng.uniform(0, 180),
                crng,
                True,
            )

    def _car(
        self,
        alb: np.ndarray,
        hmap: np.ndarray,
        cx: float,
        cy: float,
        ang: float,
        rng: np.random.Generator,
        pickup: bool = False,
    ) -> None:
        palette = _CAR_COLORS[:3] if pickup else _CAR_COLORS
        col = palette[int(rng.integers(len(palette)))]
        a = math.radians(ang)
        fx, fy = math.sin(a), -math.cos(a)
        self._box(alb, hmap, _rect(cx, cy, 1.9, 5.4 if pickup else 4.6, ang), col, 1.5)
        self._box(alb, hmap, _rect(cx + fx * 0.45, cy + fy * 0.45, 1.6, 1.0, ang), _C["glass"], 1.4)
        self._box(
            alb, hmap, _rect(cx - fx * 0.5, cy - fy * 0.5, 1.5, 1.3 if pickup else 1.7, ang), col * 1.06, 1.55
        )

    def _old_park(self, alb: np.ndarray, hmap: np.ndarray, st: _Stages, s: _Season) -> None:
        """The abandoned waterpark: walkways, wave-pool basin, lazy river, slide tower."""
        cv = self.cv
        walks = cv.stamp(lines=self.lay.old_walks, width=5.0, feather=0.1)
        if walks is not None and st.demo < 1:
            c = self._mat(walks, _C["concrete_old"], (0.06, 0.08, 0.07, 0.06))
            cr = np.abs(self.n_crack[walks.sl]) < 0.05
            c = _lerp(c * (1 - 0.3 * cr), _C["olive"], cr * 0.4)
            _paint(alb, walks, c, 1 - self._reveal(walks, st.demo))
        ax, ay, r, half = _BASIN
        fan = np.vstack([[ax, ay], _arc(ax, ay, r, -half, half)])
        fill = _ramp(st.demo, 0.6, 1.0)  # broken up and backfilled
        if fill < 1:
            apron = cv.stamp([np.vstack([[ax, ay + 3], _arc(ax, ay, r + 5, -half - 4, half + 4)])])
            basin = cv.stamp([fan], feather=0.1)
            assert apron is not None and basin is not None
            _paint(alb, apron, self._mat(apron, _C["concrete_old"], (0.05, 0.06, 0.07, 0.06)), 1 - fill)
            sl = basin.sl
            n_mid, n_fine = self.n_mid[sl], self.n_fine[sl]
            deep = np.clip((self.ym[sl[0], None] - (ay - r)) / r, 0, 1)
            paint = _lerp(_C["basin"], _C["concrete_old"] * 0.9, (0.5 + 0.5 * np.tanh(1.5 * n_mid)) * 0.6)
            paint *= 1 + 0.06 * n_fine + 0.05 * self.n_micro[sl]
            paint = _lerp(
                paint, _C["stain"] * (1 + 0.15 * n_fine), 0.75 * _smooth(1.4 * deep - 0.5 + 0.35 * n_mid)
            )
            paint = _lerp(paint, _C["water_green"] * 0.7, _smooth((deep - 0.82 + 0.06 * n_fine) * 12))
            paint = _lerp(paint, _C["snow"] * 0.95, 0.8 * s.snow)
            _paint(alb, basin, paint, 1 - fill)
            rim = cv.stamp(lines=[fan], width=0.9, closed=True)
            _paint(alb, rim, _C["concrete_old"] * 1.1, 1 - fill)
            _lift(hmap, rim, 0.9 * (1 - fill))
            bands = cv.stamp(
                lines=[_arc(ax, ay, rr, -half, half) for rr in np.arange(12, r, 6.0)], width=0.25
            )
            _paint(alb, bands, _C["paver_dark"], 0.25 * (1 - fill))
        lx, ly, ri, ro = _LAZY
        gone = _ramp(st.demo, 0.3, 0.7)
        ring = cv.stamp(
            lines=[_arc(lx, ly, (ri + ro) / 2, 0, 360, 96)], width=ro - ri, closed=True, feather=0.1
        )
        if ring is not None and gone < 1:
            c = _lerp(_C["basin"] * 0.95, _C["stain"], (0.5 + 0.5 * np.tanh(2 * self.n_mid[ring.sl])) * 0.6)
            _paint(alb, ring, c * (1 + 0.07 * self.n_fine[ring.sl]), 1 - gone)
            edge = cv.stamp(lines=[_arc(lx, ly, rr, 0, 360, 96) for rr in (ri, ro)], width=0.8, closed=True)
            _paint(alb, edge, _C["concrete_old"], 1 - gone)
            _lift(hmap, edge, 0.6 * (1 - gone))
        tower = 1 - _ramp(st.demo, 0.25, 0.4)
        if tower > 0:
            tx, ty = _TOWER
            _paint(alb, cv.stamp([_rect(tx - 14, ty - 37, 30, 9)], feather=0.1), _C["basin"] * 0.9, tower)
            for tube, col in zip(self.lay.tubes, _TUBES, strict=True):
                pts = self._px(_resample(tube, 0.5)[0])
                faded = _lerp(col, _C["concrete_old"], 0.3)
                thick = max(1, round(1.5 * self.ppm))
                for c in range(3):
                    cv2.polylines(alb[c], [pts], False, float(faded[c, 0, 0]), thick, cv2.LINE_8, 4)
                    cv2.polylines(
                        alb[c], [pts], False, float(faded[c, 0, 0] * 1.25), max(1, thick // 3), cv2.LINE_8, 4
                    )
                for i, (a, b) in enumerate(itertools.pairwise(pts)):
                    cv2.line(hmap, a, b, float((14 - 12.5 * i / len(pts)) * tower), thick, cv2.LINE_8, 4)
            top = cv.stamp([_rect(tx, ty, 6, 6)])
            _paint(alb, top, _C["concrete_old"] * 0.9, tower)
            _lift(hmap, top, 15.0 * tower)

    def _plaza(self, alb: np.ndarray, plowed: np.ndarray, st: _Stages) -> None:
        cv = self.cv
        px, py, pr = _PLAZA
        old = cv.stamp([_rect(px, py + 2, 34, 26)], feather=0.1)
        assert old is not None
        sl = old.sl
        cr = np.abs(self.n_crack[sl]) < 0.05
        joints = (np.abs((self.xm[None, sl[1]] % 3.0) - 1.5) > 1.44) | (
            np.abs((self.ym[sl[0], None] % 3.0) - 1.5) > 1.44
        )
        c = (
            self._mat(old, _C["concrete_old"], (0.05, 0.07, 0.07, 0.06))
            * (1 - 0.3 * cr)
            * (1 - 0.15 * joints)
        )
        c = _lerp(c, _C["olive"], cr * 0.5 * (1 - st.demo))
        _paint(alb, old, c, 1 - _ramp(st.hard, 0.0, 0.3))
        k = _ramp(st.hard, 0.15, 0.6)
        if k <= 0:
            return
        disk = cv.stamp([_arc(px, py, pr + 4, 0, 360, 96)], feather=0.1)
        assert disk is not None
        dx, dy = self.xm[None, disk.sl[1]] - px, self.ym[disk.sl[0], None] - py
        rr, th = np.hypot(dx, dy), np.arctan2(dy, dx)
        ring = np.abs((rr % 1.6) - 0.8) > 0.74
        spoke = (np.abs(((th * (pr * 3.2 / np.pi)) % 1.0) - 0.5) > 0.45) & (rr > 4)
        col = np.where((rr % 6.4) < 1.6, _C["paver_warm"], _C["concrete_new"] * 0.97)
        col = col * (1 - 0.18 * (ring | spoke)) * (1 + 0.03 * self.n_fine[disk.sl])
        col = _lerp(col, _C["water_shallow"] * 1.6, _smooth((3.0 - rr) * 4) * st.struct)
        _paint(alb, disk, col, k)
        _paint(plowed, disk, 1.0, k)

    def _paths(self, alb: np.ndarray, plowed: np.ndarray, st: _Stages) -> None:
        cv, p = self.cv, st.p
        for i, (line, width, p0, p1) in enumerate(self.lay.paths):
            if i < 6 and 0.22 < p < p0:  # survey paint marks the edges before the pour
                pts, nrm = _resample(line, 1.0)
                survey = cv.stamp(lines=[pts + nrm * width / 2, pts - nrm * width / 2], width=0.18)
                _paint(alb, survey, _C["orange"], 0.9 * (1 - _ramp(p, p0 - 0.04, p0)))
            part = _partial(line, _ramp(p, p0, p1) * 1.02) if p > p0 else None
            path = cv.stamp(lines=[part], width=width, feather=0.1) if part is not None else None
            if path is None:
                continue
            _paint(alb, path, self._mat(path, _C["concrete_new"], (0.02, 0.03, 0.03, 0.03)))
            _paint(alb, cv.stamp(lines=_crossbars(part, 2.4, width), width=0.06), _C["paver_dark"], 0.5)
            _paint(plowed, path, 1.0)

    def _beach(self, alb: np.ndarray, hmap: np.ndarray, st: _Stages, rng: np.random.Generator) -> None:
        """The wave-pool basin reborn: a sand beach around a ringed splash pad."""
        cv = self.cv
        ax, ay, r, half = _BASIN
        sand = cv.stamp([np.vstack([[ax, ay], _arc(ax, ay, r, -half, half)])], feather=0.3)
        assert sand is not None
        ripple = np.sin(self.ym[sand.sl[0], None] * 5.0 + 4 * self.n_mid[sand.sl]) * (1 - st.live)
        _paint(
            alb,
            sand,
            self._mat(sand, _C["sand"], (0.03, 0.04, 0.05, 0.06)) * (1 + 0.03 * ripple),
            _ramp(st.soft, 0, 0.5),
        )
        sx, sy, sr = _SPLASH
        kp = _ramp(st.hard, 0.3, 0.8)
        pad = cv.stamp([_arc(sx, sy, sr + 1.5, 0, 360, 96)], feather=0.05)
        if kp > 0 and pad is not None:
            rr = np.hypot(self.xm[None, pad.sl[1]] - sx, self.ym[pad.sl[0], None] - sy)
            bands = (rr // 1.5) % 3
            col = np.select([bands == 0, bands == 1], [_C["teal"] * 1.1, _C["concrete_new"]], _C["sky_blue"])
            col = col * (1 + 0.03 * self.n_fine[pad.sl]) * (1 - 0.25 * _smooth((sr - rr) / 2) * st.live)
            _paint(alb, pad, col, kp)
            if st.live > 0:  # fountain jets
                for a in np.arange(0, 360, 30):
                    for rad in (3.0, 6.0):
                        t = math.radians(a + rad * 7)
                        self._dot(
                            alb,
                            hmap,
                            sx + rad * math.sin(t),
                            sy - rad * math.cos(t),
                            0.35,
                            _rgb(242, 247, 255),
                            1.6,
                        )
        for _ in range(int(26 * st.live)):
            rad, ang = rng.uniform(8, r - 4), math.radians(rng.uniform(-half + 5, half - 5))
            ux, uy = ax + rad * math.sin(ang), ay - rad * math.cos(ang)
            if math.hypot(ux - sx, uy - sy) < sr + 3.5:
                continue
            col = _UMBRELLAS[int(rng.integers(len(_UMBRELLAS)))]
            self._box(
                alb,
                hmap,
                _rect(ux + 1.8, uy + 0.6, 0.9, 1.8, rng.uniform(0, 180)),
                _CLOTHES[int(rng.integers(8))],
                0.05,
            )
            self._dot(alb, hmap, ux, uy, 1.3, col, 2.4)
            self._dot(alb, hmap, ux, uy, 0.15, col * 0.6, 2.5)

    def _shore(
        self, alb: np.ndarray, hmap: np.ndarray, plowed: np.ndarray, st: _Stages, rng: np.random.Generator
    ) -> None:
        cv, lay, p = self.cv, self.lay, st.p
        gone = _ramp(p, 0.3, 0.4)
        if gone < 1:  # the old dock: weathered planks, collapsed in the middle
            a, b = lay.old_dock
            dock = cv.stamp(
                lines=[np.stack([a, a + (b - a) * 0.45]), np.stack([a + (b - a) * 0.62, b])], width=2.4
            )
            if dock is not None:
                _paint(alb, dock, self._mat(dock, _C["timber_old"], (0.05, 0.05, 0.1, 0.12)), 1 - gone)
                _lift(hmap, dock, 0.6 * (1 - gone))
        if _ramp(p, 0.46, 0.52) > 0:  # piles first
            for q in np.vstack([_resample(lay.boardwalk, 3.0)[0], _resample(lay.pier, 3.0)[0]]):
                self._dot(alb, hmap, q[0], q[1], 0.25, _C["dark"], 0.8)
        deck = _ramp(p, 0.52, 0.66)
        for line, width in ((lay.boardwalk, 4.2), (lay.pier, 3.4)):
            part = _partial(line, deck)
            boards = cv.stamp(lines=[part], width=width) if part is not None and len(part) > 1 else None
            if boards is None or part is None:
                continue
            _paint(alb, boards, self._mat(boards, _C["timber_new"], (0.03, 0.04, 0.08, 0.1)))
            _paint(alb, cv.stamp(lines=_crossbars(part, 0.6, width), width=0.05), _C["timber_new"] * 0.7, 0.5)
            _lift(hmap, boards, 0.8)
            _paint(plowed, boards, 1.0)
        fingers = _ramp(p, 0.62, 0.7)
        if fingers > 0:
            slips = cv.stamp(lines=lay.slips, width=1.3)
            _paint(alb, slips, _C["timber_new"] * 0.95, fingers)
            _lift(hmap, slips, 0.6 * fingers)
        if st.live > 0:  # boats in the slips, kayaks on the water
            for i, seg in enumerate(lay.slips):
                if (i * 7 + 3) % 5 < 3:
                    mid = seg.mean(0) - (0.0, 3.0)
                    self._box(alb, hmap, _rect(mid[0], mid[1], 6.4, 2.2), _rgb(240, 240, 236), 1.2)
                    self._box(alb, hmap, _rect(mid[0] + 0.6, mid[1], 2.0, 1.4), _C["glass"], 1.6)
            for _ in range(int(14 * st.live)):
                x = rng.uniform(150, 380)
                y = lay.shore_y(x) - rng.uniform(8, 50)
                a = math.radians(rng.uniform(0, 180))
                hull = np.array(
                    [(1.8 * math.cos(t), 0.36 * math.sin(t)) for t in np.linspace(0, 2 * np.pi, 16)]
                )
                hull = hull @ np.array([[math.cos(a), math.sin(a)], [-math.sin(a), math.cos(a)]]) + (x, y)
                self._box(alb, hmap, hull, _KAYAKS[int(rng.integers(4))], 0.12)
        if st.struct > 0.5:
            for q in lay.lights:
                self._dot(alb, hmap, q[0], q[1], 0.18, _C["dark"], 6.0)

    def _buildings(self, alb: np.ndarray, hmap: np.ndarray, st: _Stages) -> None:
        for cx, cy, w, depth, height, at in _OLD_BUILDINGS:
            if st.p < at:
                self._roof(alb, hmap, cx, cy, w, depth, height, height, _C["roof_flat"], stained=True)
        renew = _ramp(st.p, 0.52, 0.62)
        for gx, gy in _GATEHOUSES:
            col = _lerp(_C["rust"], _C["bronze"], renew)
            self._roof(alb, hmap, gx, gy, 9.0, 9.0, 3.6, 7.2, col, seams=True, stained=renew < 0.5, hip=True)
        cx, cy, w, depth = _PAVILION
        if st.p > 0.4:
            if _ramp(st.p, 0.5, 0.62) > 0.5:
                roof = _lerp(_C["concrete_new"], _C["roof_new"], 0.3)
                self._roof(alb, hmap, cx, cy, w, depth, 3.4, 5.6, roof, seams=True, solar=True)
            else:
                _paint(alb, self.cv.stamp([_rect(cx, cy, w + 1, depth + 1)]), _C["concrete_new"] * 0.95)

    def _roof(
        self,
        alb: np.ndarray,
        hmap: np.ndarray,
        cx: float,
        cy: float,
        w: float,
        depth: float,
        eave: float,
        ridge: float,
        col: np.ndarray,
        *,
        seams: bool = False,
        stained: bool = False,
        hip: bool = False,
        solar: bool = False,
        ang: float = 0.0,
    ) -> None:
        """Flat (with parapet + HVAC), gable (ridge along x) or hip roof; shading comes from the height map."""
        st = self.cv.stamp([_rect(cx, cy, w, depth, ang)], feather=0.05)
        if st is None:
            return
        sl = st.sl
        ca, sa = math.cos(math.radians(ang)), math.sin(math.radians(ang))
        ex, ey = self.xm[None, sl[1]] - cx, self.ym[sl[0], None] - cy
        dx, dy = ex * ca + ey * sa, ey * ca - ex * sa  # roof-local coordinates
        du, dv = np.clip(w / 2 - np.abs(dx), 0, None), np.clip(depth / 2 - np.abs(dy), 0, None)
        flat = ridge <= eave
        rise = np.minimum(du, dv) / (min(w, depth) / 2) if hip else dv / (depth / 2)
        h = eave + (ridge - eave) * np.clip(rise, 0, 1)
        c = col * (1 + 0.03 * self.n_fine[sl] + 0.03 * self.n_micro[sl])
        if seams:
            c = c * (1 - 0.1 * (np.abs((dx % 0.6) - 0.3) > 0.25))
        if stained:
            c = _lerp(c, c * 0.62, _smooth(0.8 * self.n_mid[sl] + 0.5 * self.n_fine[sl]))
        if solar:
            panel = (dy < -0.8) & (np.abs(dx) < w / 2 - 1.2) & (dy > -depth / 2 + 0.8)
            grid = (np.abs((dx % 1.05) - 0.52) > 0.47) | (np.abs((dy % 1.7) - 0.85) > 0.8)
            c = np.where(panel, np.where(grid, _C["concrete_new"] * 0.8, _C["solar"]), c)
        if flat:
            parapet = (du < 0.5) | (dv < 0.5)
            c = np.where(parapet, _C["concrete_old"] * 1.05, c)
            h = h + parapet * 0.5
        _paint(alb, st, c)
        _lift(hmap, st, h)
        if flat:
            for k in range(3):
                hvac = _rect(cx - w / 4 + k * w / 4, cy + (k % 2 - 0.5) * depth / 4, 2.2, 1.6)
                self._box(alb, hmap, hvac, _rgb(158, 158, 152), eave + 1.4)

    def _construction(
        self, alb: np.ndarray, hmap: np.ndarray, plowed: np.ndarray, st: _Stages, rng: np.random.Generator
    ) -> None:
        """Haul tracks, debris piles, the staging yard and the machines of the moment."""
        cv, p = self.cv, st.p
        for _ in range(int(7 * st.busy)):
            a = np.array([rng.uniform(150, 380), rng.uniform(225, 296)])
            b = a + rng.uniform(-40, 40, 2)
            pts, nrm = _resample(np.array([a, (a + b) / 2 + rng.uniform(-10, 10, 2), b]), 0.8)
            ruts = cv.stamp(lines=[pts + nrm, pts - nrm], width=0.6, feather=0.15)
            _paint(alb, ruts, _C["earth_dark"], 0.55 * (1 - st.soft))
            _paint(plowed, ruts, 0.8)
        piles = _ramp(p, 0.04, 0.12) * (1 - _ramp(p, 0.3, 0.4))
        for cx, cy, rad in [
            (210, 270, 7.0),
            (318, 270, 6.0),
            (184, 286, 5.0),
            (300, 248, 6.5),
            (236, 280, 4.0),
        ]:
            pile = cv.stamp([_blob(rng, cx, cy, rad, rad * 0.75, 0.35)], feather=0.8) if piles > 0 else None
            if pile is not None:
                c = _lerp(_C["concrete_old"] * 0.8, _C["earth_dark"], (self.n_fine[pile.sl] > 0) * 0.6)
                _paint(alb, pile, c * (1 + 0.18 * self.n_micro[pile.sl]), piles)
                _lift(hmap, pile, pile.a * (3.0 + 0.6 * self.n_fine[pile.sl]) * piles)
        y0 = _LOT[1]
        for cx, cy, w, depth, ang, col in [
            (372, y0 + 6, 12, 3.2, 0, _rgb(236, 236, 230)),
            (386, y0 + 14, 12, 2.5, 90, _rgb(160, 60, 46)),
            (380, y0 + 14, 12, 2.5, 90, _rgb(46, 90, 120)),
        ]:
            self._box(alb, hmap, _rect(cx, cy, w, depth, ang), col, 2.8)
        for cx, cy, rad, col in [
            (352, y0 + 26, 4.5, _rgb(150, 146, 140)),
            (362, y0 + 27, 4.0, _C["earth"]),
            (344, y0 + 12, 3.5, _rgb(196, 178, 140)),
        ]:
            pile = cv.stamp([_blob(rng, cx, cy, rad, rad * 0.8, 0.2)], feather=0.9)
            if pile is not None:
                _paint(alb, pile, col * (1 + 0.12 * self.n_micro[pile.sl]))
                _lift(hmap, pile, pile.a * 2.2)
        if p < 0.3:
            fleet = ("excavator", "truck", "excavator", "truck", "dozer")
        elif p < 0.5:
            fleet = ("dozer", "roller", "truck", "excavator", "mixer")
        else:
            fleet = ("mixer", "loader", "truck", "loader", "excavator")
        for i in range(round(5 + 3 * st.busy)):
            self._machine(
                alb, hmap, fleet[i % 5], rng.uniform(150, 380), rng.uniform(222, 298), rng.uniform(0, 360)
            )

    def _machine(self, alb: np.ndarray, hmap: np.ndarray, kind: str, x: float, y: float, ang: float) -> None:
        f = np.array([math.sin(math.radians(ang)), -math.cos(math.radians(ang))])
        o = np.array([x, y])

        def box(at: np.ndarray, w: float, depth: float, col: np.ndarray, h: float, rot: float = ang) -> None:
            self._box(alb, hmap, _rect(at[0], at[1], w, depth, rot), col, h)

        yellow, dark = _C["yellow"], _C["dark"]
        if kind == "excavator":
            side = np.array([f[1], -f[0]])
            box(o + side * 1.3, 0.8, 4.6, dark, 1.0)
            box(o - side * 1.3, 0.8, 4.6, dark, 1.0)
            swing = ang + 25
            fb = np.array([math.sin(math.radians(swing)), -math.cos(math.radians(swing))])
            box(o, 3.0, 3.4, yellow, 2.8, swing)
            box(o + f * 0.3, 1.0, 1.2, _C["glass"], 3.1, swing)
            box(o + fb * 4.5, 0.7, 6.0, yellow, 3.5, swing)
            box(o + fb * 8.0, 1.4, 1.0, dark, 1.0, swing)
        elif kind == "truck":
            box(o + f * 3.2, 2.5, 2.2, yellow, 3.0)
            box(o - f * 1.2, 2.5, 6.0, _rgb(120, 98, 76), 3.2)
        elif kind == "mixer":
            box(o + f * 3.0, 2.4, 2.0, _rgb(236, 236, 232), 3.0)
            box(o - f * 1.2, 2.2, 5.4, _C["orange"], 3.4)
        else:  # dozer, roller, loader: a body and a blade/drum/bucket
            box(o, 2.6, 3.4, yellow, 2.5)
            box(o + f * 2.3, 3.4 if kind == "dozer" else 2.4, 1.3 if kind == "roller" else 0.8, dark, 1.2)

    def _people(self, alb: np.ndarray, hmap: np.ndarray, live: float, rng: np.random.Generator) -> None:
        lay = self.lay
        spots: list[np.ndarray] = []
        for line, width, *_ in lay.paths:
            pts, nrm = _resample(line, 3.0)
            spots += [
                q + n * rng.uniform(-width / 2.5, width / 2.5)
                for q, n in zip(pts, nrm, strict=True)
                if rng.uniform() < 0.22 * live
            ]
        for line in (lay.boardwalk, lay.pier):
            spots += [
                q + rng.uniform(-1, 1, 2) for q in _resample(line, 3.0)[0] if rng.uniform() < 0.3 * live
            ]
        ax, ay, r, half = _BASIN
        for _ in range(int(60 * live)):
            rad, ang = rng.uniform(4, r - 2), math.radians(rng.uniform(-half, half))
            spots.append(np.array([ax + rad * math.sin(ang), ay - rad * math.cos(ang)]))
        for _ in range(int(18 * live)):  # picnics on the great lawn
            c = np.array([rng.uniform(312, 388), rng.uniform(228, 270)])
            self._box(
                alb,
                hmap,
                _rect(c[0], c[1], 2.0, 1.6, rng.uniform(0, 90)),
                _BLANKETS[int(rng.integers(4))],
                0.05,
            )
            spots += [c + rng.uniform(-1.4, 1.4, 2) for _ in range(int(rng.integers(1, 4)))]
        for q in spots:
            self._dot(alb, hmap, q[0], q[1], 0.28, _CLOTHES[int(rng.integers(len(_CLOTHES)))], 1.7)

    # -- vegetation and snow ---------------------------------------------- #

    def _trees(self, alb: np.ndarray, hmap: np.ndarray, v: _Visit) -> np.ndarray:
        """Draw canopies (lumpy domes) into albedo and height map, shortest first; returns coverage."""
        p, s, lay = v.progress, v.season, self.lay
        canopy = np.zeros((self.h, self.w), np.float32)
        n = len(lay.trees)
        kinds = lay.trees[:, 3].astype(int)
        cols = _canopy_colors(kinds, lay.trees[:, 4], lay.trees[:, 5], self.tree_turn[:n], s)
        items = []
        for i, (r, group) in enumerate(lay.trees[:, [2, 6]].tolist()):
            if self.sprites[i] is not None and not (
                group == _VOLUNTEER and p > 0.06 + 0.1 * self.tree_turn[i]
            ):
                items.append((2.3 * r + 2.5, self.sprites[i], kinds[i] == _CONIFER, cols[i]))
        planted, grow = _ramp(p, 0.68, 0.8), 1.0 + 0.6 * _ramp(p, 0.75, 1.0)
        young = lay.young
        flat = np.zeros(len(young))
        ycols = _canopy_colors(young[:, 3].astype(int), flat, flat, self.tree_turn[n:], s, 0.5)
        for j, (x, y, order) in enumerate(young[:, :3].tolist()):
            sprite = self._sprite(x, y, 1.25 * grow, self.tree_phase[n + j]) if order < planted else None
            if sprite is not None:
                items.append((4.0 * grow, sprite, False, ycols[j]))
        items.sort(key=lambda t: t[0])
        for height, sprite, conifer, col in items:
            self._tree(alb, hmap, canopy, sprite, height, conifer, col.reshape(3, 1, 1), s)
        hmap += (0.3 * self.n_fine + 0.45 * self.n_micro) * canopy * (0.4 + 0.6 * s.leaf)
        return canopy

    def _tree(
        self,
        alb: np.ndarray,
        hmap: np.ndarray,
        canopy: np.ndarray,
        sprite: _Sprite,
        height: float,
        conifer: bool,
        col: np.ndarray,
        s: _Season,
    ) -> None:
        y0, x0, cover, rr = sprite
        sl = (slice(y0, y0 + cover.shape[0]), slice(x0, x0 + cover.shape[1]))
        leaf = 1.0 if conifer else s.leaf
        dome = np.clip(1 - rr, 0, 1) ** 0.8 if conifer else np.sqrt(np.clip(1 - rr * rr, 0, 1))
        h = height * (0.55 + 0.45 * dome) * (0.35 + 0.65 * leaf)
        micro = self.n_micro[sl]
        if leaf < 0.6:  # bare crowns: a twiggy, see-through fuzz with dappled shadows
            k = cover * np.clip(0.62 + 0.4 * leaf + 0.25 * micro, 0, 1)
            lift = h * cover * (micro > 0.1)
        else:
            k, lift = cover, h * cover
        hreg = hmap[sl]
        kk = np.where(h > hreg, k, 0.3 * k)
        reg = alb[:, sl[0], sl[1]]
        reg += (col * (1 + 0.08 * micro + 0.1 * self.n_fine[sl]) - reg) * kk
        np.maximum(hreg, lift, out=hreg)
        np.maximum(canopy[sl], k, out=canopy[sl])

    def _snow(
        self, alb: np.ndarray, hmap: np.ndarray, plowed: np.ndarray, canopy: np.ndarray, s: _Season
    ) -> None:
        """Snow cover: drifts, stubble poking through, plowed paths with banks, dusted crowns."""
        if s.snow <= 0:
            return
        cover = _smooth(
            (1.3 * s.snow - 0.3 + 0.2 * self.n_large + 0.25 * self.n_mid + 0.1 * self.n_fine) * 2.5
        )
        cover *= (
            (1 - 0.9 * plowed)
            * (1 - 0.9 * canopy)
            * (1 - 0.25 * _smooth((self.n_micro + 0.5 * self.n_fine - 1.6) * 2))
        )
        cover[self.lake.sl] *= 1 - self.lake.a
        alb += (_C["snow"] * (1 + 0.02 * self.n_fine + 0.015 * self.n_micro) - alb) * cover
        alb += (_C["snow"] - alb) * (canopy * s.snow * 0.18 * (self.n_micro > 0.6))
        banks = np.clip((cv2.GaussianBlur(plowed, (0, 0), 1.2 * self.ppm) - plowed) * 3, 0, 1) * s.snow
        alb += (_C["snow"] * 0.92 - alb) * banks
        hmap += (0.12 * self.n_fine + 0.5 * banks) * cover

    # -- lighting --------------------------------------------------------- #

    def _light(self, alb: np.ndarray, hmap: np.ndarray, v: _Visit) -> np.ndarray:
        """Sun (hillshade x cast shadows) plus sky (ambient occlusion), in place on the albedo."""
        hmap += self.relief
        az, el = math.radians(v.sun_az), math.radians(max(v.sun_el, 8.0))
        lx, ly, lz = math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)
        soft = cv2.GaussianBlur(hmap, (0, 0), 0.6)  # no stair-stepped slopes on small objects
        gx = cv2.Sobel(soft, cv2.CV_32F, 1, 0, ksize=3, scale=self.ppm / 8)
        gy = cv2.Sobel(soft, cv2.CV_32F, 0, 1, ksize=3, scale=self.ppm / 8)
        shade = np.clip((lz - gx * lx - gy * ly) / np.sqrt(1 + gx * gx + gy * gy) / lz, 0, 2.5)
        occ, ao = self._shadows(hmap, (lx, ly, lz))
        direct = shade * (1 - 0.86 * occ)
        warm = _ramp(math.degrees(el), 12, 45)
        sun = _lerp(_rgb(255, 204, 153), _rgb(255, 247, 235), warm) * 0.78
        sky = _rgb(128, 148, 184) * (0.95 - 0.15 * warm) * 0.62
        for c in range(3):
            alb[c] *= direct * float(sun[c, 0, 0]) + ao * float(sky[c, 0, 0])
        return alb

    def _shadows(self, hmap: np.ndarray, sun: tuple[float, float, float]) -> tuple[np.ndarray, np.ndarray]:
        """Cast shadows by ray-marching a max-pooled (~0.5 m) height map; plus ambient occlusion."""
        f = max(1, round(self.ppm * 0.5))
        hs = cv2.dilate(hmap, np.ones((f, f), np.uint8))[f // 2 :: f, f // 2 :: f].copy()
        step = f / self.ppm
        lx, ly, lz = sun
        hz = math.hypot(lx, ly)
        dx, dy, rise = lx / hz, ly / hz, lz / hz * step
        occ = np.zeros_like(hs)
        hh, ww = hs.shape
        for k in range(1, int(min(30.0, (float(hs.max()) + 0.5) * hz / lz) / step) + 1):
            ox, oy = round(k * dx), round(k * dy)
            if abs(ox) >= ww or abs(oy) >= hh:
                break
            src = hs[max(oy, 0) : hh + min(oy, 0), max(ox, 0) : ww + min(ox, 0)]
            dst = (slice(max(-oy, 0), hh + min(-oy, 0)), slice(max(-ox, 0), ww + min(-ox, 0)))
            np.maximum(occ[dst], src - hs[dst] - k * rise, out=occ[dst])
        occ = np.clip(occ / 0.6, 0, 1)
        ao = 1 - np.clip((cv2.GaussianBlur(hs, (0, 0), 2.5 / step) - hs) * 0.12, 0, 0.45)
        size = (self.w, self.h)
        occ = cv2.GaussianBlur(cv2.resize(occ, size, interpolation=cv2.INTER_LINEAR), (0, 0), 0.3 * self.ppm)
        return occ, cv2.resize(ao, size, interpolation=cv2.INTER_LINEAR)


# --------------------------------------------------------------------------- #
# Photography: atmosphere, water reflections, camera response
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Look:
    """Per-visit camera and atmosphere."""

    exposure: float
    wb: tuple[float, float, float]  # RGB gains
    haze_per_km: float
    haze_rgb: tuple[float, float, float]
    sky_zenith: tuple[float, float, float]
    contrast: float
    saturation: float
    kelvin: int


def _look(v: _Visit, rng: np.random.Generator) -> _Look:
    warm = 1 - _ramp(v.sun_el, 10, 40)
    shift = float(rng.uniform(-0.04, 0.04))
    winter = v.season.snow > 0.3
    horizon = (0.80, 0.84, 0.88) if winter else (0.78 + 0.1 * warm, 0.82 + 0.03 * warm, 0.88 - 0.06 * warm)
    return _Look(
        exposure=float(rng.uniform(0.88, 1.12)),
        wb=(1 + shift + 0.05 * warm, 1.0, 1 - shift - 0.04 * warm),
        haze_per_km=float(rng.uniform(0.25, 0.9)) * (1.3 if v.season.label == "summer" else 1.0),
        haze_rgb=horizon,
        sky_zenith=(0.55, 0.62, 0.72) if winter else (0.42, 0.56, 0.78),
        contrast=float(rng.uniform(0.16, 0.26)),
        saturation=float(rng.uniform(1.04, 1.16)) * (0.92 if winter else 1.0),
        kelvin=int(round(5600 - 900 * warm - 2500 * shift, -1)),
    )


def _grain(rng: np.random.Generator, w: int, h: int, n: int, sigma: float) -> list[_Grain]:
    """Luma-dominant sensor noise frames as (positive, negative) uint8 parts for saturating 8-bit adds."""
    out = []
    for _ in range(n):
        luma = rng.standard_normal((h, w, 1), dtype=np.float32) * sigma
        noise = np.clip(
            np.rint(luma + rng.standard_normal((h, w, 3), dtype=np.float32) * (0.35 * sigma)), -40, 40
        )
        out.append((np.maximum(noise, 0).astype(np.uint8), np.maximum(-noise, 0).astype(np.uint8)))
    return out


class _Optics:
    """Image-space effects for one camera and look, precomputed once per take (all 8-bit at run time)."""

    def __init__(
        self,
        cam: _Camera,
        look: _Look,
        v: _Visit,
        ppm: float,
        grain: list[_Grain],
        rng: np.random.Generator,
        still: bool = False,
    ) -> None:
        w, h = self.size = (cam.width, cam.height)
        self.tex_to_world = np.diag([1 / ppm, 1 / ppm, 1.0])
        step = 8
        d = cam.rays(step)
        dz = np.clip(d[..., 2], 1e-3, 1)
        gy, gx = np.mgrid[0 : d.shape[0], 0 : d.shape[1]] * step
        rad2 = ((gx - w / 2) ** 2 + (gy - h / 2) ** 2) / ((w / 2) ** 2 + (h / 2) ** 2)
        vig = (1 - 0.3 * rad2**1.5)[..., None]
        haze = (1 - np.exp(-cam.alt / dz * look.haze_per_km / 1000))[..., None]
        gain = np.array(look.wb[::-1]) * look.exposure  # BGR
        fres = 0.02 + 0.98 * (1 - dz) ** 5
        sky = _lerp(
            np.array(look.haze_rgb[::-1]),
            np.array(look.sky_zenith[::-1]),
            (np.arcsin(dz)[..., None] / (np.pi / 2)) ** 0.6,
        )
        az, el = math.radians(v.sun_az), math.radians(max(v.sun_el, 3))
        sun_dir = np.array([math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)])
        cos_sun = d @ sun_dir  # reflected ray (dx, dy, -dz) . sun (east, south, up)
        core, halo = np.exp((cos_sun - 1) / 0.006), np.exp((cos_sun - 1) / 0.02)

        def up(a: np.ndarray, dtype: type = np.float32) -> np.ndarray:
            a = np.ascontiguousarray(a, np.float32)
            big = cv2.resize(a, (a.shape[1] * step, a.shape[0] * step), interpolation=cv2.INTER_LINEAR)[
                :h, :w
            ]
            return np.clip(big, 0, 255).astype(np.uint8) if dtype is np.uint8 else big

        lum = np.array([[0.114, 0.587, 0.299]])
        sat = look.saturation * np.eye(3) + (1 - look.saturation) * np.ones((3, 1)) @ lum
        self.color = (np.diag(gain) @ sat).astype(np.float32)
        self.a_mul = up(np.repeat(vig * (1 - haze), 3, axis=2) * 255, np.uint8)
        self.b_add = up(vig * haze * gain * np.array(look.haze_rgb[::-1]) * 235, np.uint8)
        fres_f, halo_f = up(fres), up(halo)
        glint_f = up(0.45 * core + 0.06 * halo)
        if halo.max() > 0.02:  # the sun's glitter path: sparkles that thin out away from the reflection
            sparkle = (rng.random((h, w), dtype=np.float32) > 1 - 0.2 * halo_f**1.5).astype(np.float32)
            glint_f += 3.0 * cv2.GaussianBlur(sparkle, (0, 0), 0.6) * np.sqrt(halo_f)
        k = np.clip(fres_f + glint_f, 0, 1)
        refl = (
            fres_f[..., None] * up(sky) + glint_f[..., None] * np.array([0.92, 0.97, 1.0], np.float32)
        ) / np.maximum(k, 1e-3)[..., None]
        self.refl = np.clip(refl * 225, 0, 255).astype(np.uint8)
        self.refl_k = k * (1 / 255)  # times the 0..255 water alpha
        t = np.linspace(0, 1, 256)
        curve = _lerp(t, t * t * (3 - 2 * t), look.contrast + (0.05 if still else 0.0)) * 0.97 + 0.015
        self.lut = np.clip(curve * 255, 0, 255).astype(np.uint8)
        self.grain = grain
        self.interp = cv2.INTER_CUBIC if still else cv2.INTER_LINEAR

    def shoot(self, tex: np.ndarray, world_to_image: np.ndarray, frame: int = 0) -> np.ndarray:
        """Photograph the BGRA world texture through a world->image homography."""
        img = cv2.warpPerspective(
            tex,
            world_to_image @ self.tex_to_world,
            self.size,
            flags=self.interp,
            borderMode=cv2.BORDER_REFLECT,
        )
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        rows = np.flatnonzero(img[:, :, 3].max(axis=1))
        if len(rows):  # water reflects the sky (Fresnel) and the sun (glint)
            r0, r1 = rows[0], rows[-1] + 1
            k = img[r0:r1, :, 3] * self.refl_k[r0:r1]
            bgr[r0:r1] = cv2.blendLinear(bgr[r0:r1], self.refl[r0:r1], 1 - k, k)
        bgr = cv2.add(cv2.multiply(cv2.transform(bgr, self.color), self.a_mul, scale=1 / 255), self.b_add)
        brighter, darker = self.grain[frame % len(self.grain)]
        return cv2.subtract(cv2.add(cv2.LUT(bgr, self.lut), brighter), darker)


# --------------------------------------------------------------------------- #
# Writers: MP4 + DJI SRT, JPEG + EXIF/XMP, truth
# --------------------------------------------------------------------------- #


def _srt_time(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _exposure_meta(look: _Look, s: _Season) -> tuple[int, float, float]:
    """Plausible ISO, shutter denominator and EV for the telemetry."""
    snowy = s.snow > 0.3
    return 100, float(round(800 * look.exposure * (1.5 if snowy else 1.0) / 50) * 50), -0.3 if snowy else 0.0


def _gimbal_yaw(heading: float) -> float:
    return (heading + 180) % 360 - 180


def _srt_block(i: int, fps: int, when: dt.datetime, cam: _Camera, look: _Look, s: _Season) -> str:
    """One DJI-style subtitle record (modern Mavic/Air/Mini format, plus gimbal angles)."""
    iso, shutter, ev = _exposure_meta(look, s)
    lat, lon = _latlon(cam.x, cam.y)
    return (
        f"{i + 1}\n{_srt_time(i / fps)} --> {_srt_time((i + 1) / fps)}\n"
        f'<font size="28">FrameCnt: {i + 1}, DiffTime: {round(1000 / fps)}ms\n'
        f"{when:%Y-%m-%d %H:%M:%S}.{when.microsecond // 1000:03d}\n"
        f"[iso: {iso}] [shutter: 1/{shutter:.1f}] [fnum: 2.8] [ev: {ev:g}] [color_md: default] "
        f"[focal_len: 24.00] [latitude: {lat:.6f}] [longitude: {lon:.6f}] "
        f"[rel_alt: {cam.alt:.3f} abs_alt: {cam.alt + GROUND_ASL_M:.3f}] "
        f"[gb_yaw: {_gimbal_yaw(cam.heading):.1f} gb_pitch: {-cam.pitch:.1f} gb_roll: {cam.roll:.1f}] "
        f"[ct: {look.kelvin}] </font>\n\n"
    )


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    return float(d), float(m), round((value - d - m / 60) * 3600, 4)


def _save_photo(
    path: Path, img_bgr: np.ndarray, when: dt.datetime, offset_h: int, cam: _Camera, look: _Look, s: _Season
) -> None:
    """JPEG with EXIF date/time, GPS and DJI-style XMP gimbal tags."""
    lat, lon = _latlon(cam.x, cam.y)
    stamp = when.strftime("%Y:%m:%d %H:%M:%S")
    iso, shutter, _ = _exposure_meta(look, s)
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "DJI"
    exif[ExifTags.Base.Model] = "Vantage Synthetic Camera"
    exif[ExifTags.Base.Software] = "vantage.demo.synth"
    exif[ExifTags.Base.DateTime] = stamp
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[ExifTags.Base.DateTimeOriginal] = stamp
    sub[ExifTags.Base.DateTimeDigitized] = stamp
    sub[ExifTags.Base.OffsetTimeOriginal] = f"{offset_h:+03d}:00"
    sub[ExifTags.Base.ISOSpeedRatings] = iso
    sub[ExifTags.Base.ExposureTime] = 1 / shutter
    sub[ExifTags.Base.FNumber] = 2.8
    sub[ExifTags.Base.FocalLengthIn35mmFilm] = 24
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSVersionID] = b"\x02\x03\x00\x00"
    gps[ExifTags.GPS.GPSLatitudeRef] = "N" if lat >= 0 else "S"
    gps[ExifTags.GPS.GPSLatitude] = _dms(lat)
    gps[ExifTags.GPS.GPSLongitudeRef] = "E" if lon >= 0 else "W"
    gps[ExifTags.GPS.GPSLongitude] = _dms(lon)
    gps[ExifTags.GPS.GPSAltitudeRef] = 0
    gps[ExifTags.GPS.GPSAltitude] = round(cam.alt + GROUND_ASL_M, 3)
    yaw = _gimbal_yaw(cam.heading)
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"'
        f' drone-dji:GpsLatitude="{lat:.7f}" drone-dji:GpsLongitude="{lon:.7f}"'
        f' drone-dji:AbsoluteAltitude="{cam.alt + GROUND_ASL_M:+.2f}" drone-dji:RelativeAltitude="{cam.alt:+.2f}"'
        f' drone-dji:GimbalRollDegree="{cam.roll:+.2f}" drone-dji:GimbalYawDegree="{yaw:+.2f}"'
        f' drone-dji:GimbalPitchDegree="{-cam.pitch:+.2f}" drone-dji:FlightYawDegree="{yaw:+.2f}"'
        ' drone-dji:FlightPitchDegree="+0.00" drone-dji:FlightRollDegree="+0.00"/>'
        "</rdf:RDF></x:xmpmeta>"
    )
    Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)).save(
        path, "JPEG", quality=92, subsampling=0, exif=exif, xmp=xmp.encode()
    )


def _hflat(h: np.ndarray) -> list[float]:
    return [float(f"{x:.10g}") for x in (h / h[2, 2]).ravel()]


def _camera_json(cam: _Camera) -> dict:
    lat, lon = _latlon(cam.x, cam.y)
    vals = {
        "x_m": cam.x,
        "y_m": cam.y,
        "alt_m": cam.alt,
        "heading_deg": cam.heading,
        "pitch_deg": cam.pitch,
        "roll_deg": cam.roll,
    }
    return {k: round(v, 4) for k, v in vals.items()} | {
        "hfov_deg": HFOV_DEG,
        "lat": round(lat, 7),
        "lon": round(lon, 7),
    }


def _source_hash() -> str:
    return hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]


def _is_current(truth_path: Path, seed: int, fast: bool) -> bool:
    try:
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    gen = truth.get("generator", {})
    if [gen.get(k) for k in ("version", "source", "seed", "fast")] != [_VERSION, _source_hash(), seed, fast]:
        return False
    return all((truth_path.parent / f["path"]).exists() for f in truth_files(truth))


def generate_demo_footage(footage_dir: Path, *, fast: bool = False, seed: int = 7) -> Path:
    """Write 12 synthetic visits (MP4 + DJI SRT, JPEG + EXIF/XMP) and `_truth.json`; return the truth path.

    Visits render in parallel worker processes (``VANTAGE_SYNTH_WORKERS`` overrides the count).
    Re-running with the same seed and mode reuses footage made by an identical generator.
    """
    footage_dir = Path(footage_dir)
    truth_path = footage_dir / TRUTH_NAME
    if _is_current(truth_path, seed, fast):
        log.info(f"demo footage is up to date in {footage_dir}")
        return truth_path
    # An interrupted run must not leave behind a truth file that vouches for half-rewritten footage.
    truth_path.unlink(missing_ok=True)
    footage_dir.mkdir(parents=True, exist_ok=True)
    visits = _visits()
    workers = int(os.environ.get("VANTAGE_SYNTH_WORKERS", 0)) or min(3, max(1, (os.cpu_count() or 1) - 1))
    with log.step(f"synthesize demo footage ({'fast' if fast else 'full'}, seed {seed}, {workers} workers)"):
        jobs = [(v.index, str(footage_dir), seed, fast) for v in visits]
        if workers > 1:
            threads = max(1, (os.cpu_count() or 1) // workers)
            with ProcessPoolExecutor(
                workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker,
                initargs=(seed, fast, threads),
            ) as pool:
                futures = [pool.submit(_visit_job, *job) for job in jobs]
                results = [_logged(f.result()) for f in futures]
        else:
            _init_worker(seed, fast)
            results = [_logged(_visit_job(*job)) for job in jobs]
        truth = {
            "version": 1,
            "generator": {
                "name": "vantage.demo.synth",
                "version": _VERSION,
                "source": _source_hash(),
                "seed": seed,
                "fast": fast,
            },
            "world": {
                "width_m": WORLD_M[0],
                "height_m": WORLD_M[1],
                "axes": "x east, y south, meters; homographies map (x, y, 1) to image pixels",
                "origin": {"lat": ORIGIN[0], "lon": ORIGIN[1]},
                "ground_asl_m": GROUND_ASL_M,
            },
            "features": {k: list(v) for k, v in POINTS_OF_INTEREST.items()},
            "visits": results,
        }
        truth_path.write_text(json.dumps(truth, indent=2) + "\n", encoding="utf-8")
    return truth_path


_WORKER: dict[str, Any] = {}


def _init_worker(seed: int, fast: bool, threads: int | None = None) -> None:
    """Build the world and grain banks once per process."""
    if threads:
        cv2.setNumThreads(threads)
    mode = _FAST if fast else _FULL
    grng = np.random.default_rng([seed, 9])
    _WORKER["world"] = _World(seed, mode.ppm)
    _WORKER["grain"] = {
        "video": _grain(grng, *mode.video, 4, 2.6),
        "photo": _grain(grng, *mode.photo, 1, 2.0),
    }


def _visit_job(index: int, root: str, seed: int, fast: bool) -> dict:
    return _render_visit(
        _WORKER["world"], _visits()[index], _FAST if fast else _FULL, Path(root), seed, _WORKER["grain"]
    )


def _logged(visit: dict) -> dict:
    names = " ".join(f["path"].split("/")[1] for f in visit["files"])
    log.info(f"{visit['date']}  progress {visit['progress']:.2f}  {visit['season']:<6}  {names}")
    return visit


def _take_name(v: _Visit, code: str) -> str:
    """File name of a take: DJI_0001.MP4 for clips, DJI_<shutter time>_0001.JPG for stills."""
    _, kind, number, offset = _TAKES[code]
    if kind == "video":
        return f"DJI_{number:04d}.MP4"
    return f"DJI_{v.start + dt.timedelta(seconds=offset):%Y%m%d%H%M%S}_{number:04d}.JPG"


def _render_visit(
    world: _World, v: _Visit, mode: _Mode, root: Path, seed: int, grain: dict[str, list[_Grain]]
) -> dict:
    rng = np.random.default_rng([seed, 100 + v.index])
    tex = world.render(v, np.random.default_rng([seed, 200 + v.index]))
    look = _look(v, rng)
    base = {name: _jitter(cam, rng, _JITTER[name]) for name, cam in _FAMILIES.items()}
    folder = root / v.date
    folder.mkdir(parents=True, exist_ok=True)
    names = {code: _take_name(v, code) for code in v.codes}
    keep = {
        *names.values(),
        *(Path(n).with_suffix(".SRT").name for n in names.values() if n.endswith(".MP4")),
    }
    for stale in folder.glob("DJI_*"):  # left behind by an older flight plan
        if stale.name not in keep:
            stale.unlink()
    files = []
    for code in v.codes:
        vantage, kind, _, offset = _TAKES[code]
        when = v.start + dt.timedelta(seconds=offset, milliseconds=int(rng.integers(0, 999)))
        trng = np.random.default_rng([seed, 300 + v.index, ord(code)])
        name = names[code]
        if kind == "video":
            cam = replace(base[vantage], width=mode.video[0], height=mode.video[1])
            entry = _write_video(
                folder / name, tex, cam, look, v, mode, when, world.ppm, grain["video"], trng
            )
        else:
            cam = replace(
                _jitter(base[vantage], trng, (1.2, 0.8, 0.3, 0.01, 0.8)),
                width=mode.photo[0],
                height=mode.photo[1],
            )
            img = _Optics(cam, look, v, world.ppm, grain["photo"], trng, still=True).shoot(
                tex, cam.homography()
            )
            _save_photo(folder / name, img, when, v.utc_offset_h, cam, look, v.season)
            entry = {"kind": "photo", "width": cam.width, "height": cam.height}
        frames = entry.pop("frame_homographies", None)
        files.append(
            {
                "path": f"{v.date}/{name}",
                "vantage": vantage,
                **entry,
                "start": when.isoformat(timespec="milliseconds"),
                "camera": _camera_json(cam),
                "homography": _hflat(cam.homography()),
            }
            | ({"frame_homographies": frames} if frames else {})
        )
    return {
        "date": v.date,
        "progress": round(v.progress, 4),
        "season": v.season.label,
        "snow": round(v.season.snow, 3),
        "local_time": v.start.time().isoformat(),
        "utc_offset_h": v.utc_offset_h,
        "sun": {"azimuth_deg": round(v.sun_az, 2), "elevation_deg": round(v.sun_el, 2)},
        "files": files,
    }


def _write_video(
    path: Path,
    tex: np.ndarray,
    cam: _Camera,
    look: _Look,
    v: _Visit,
    mode: _Mode,
    when: dt.datetime,
    ppm: float,
    grain: list[_Grain],
    rng: np.random.Generator,
) -> dict:
    optics = _Optics(cam, look, v, ppm, grain, rng)
    phase = rng.uniform(0, 2 * np.pi, 6).tolist()
    n = round(mode.seconds * mode.fps)
    utc = when - dt.timedelta(hours=v.utc_offset_h)
    meta = ["-metadata", f"creation_time={utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"]
    homs, srt = [], []
    with FrameWriter(
        path, cam.width, cam.height, fps=mode.fps, crf=mode.crf, preset=mode.preset, extra=meta
    ) as out:
        for i in range(n):
            c = _hover(cam, i / mode.fps, phase)
            hom = c.homography()
            out.write(optics.shoot(tex, hom, i))
            homs.append(_hflat(hom))
            srt.append(_srt_block(i, mode.fps, when + dt.timedelta(seconds=i / mode.fps), c, look, v.season))
    path.with_suffix(".SRT").write_text("".join(srt), encoding="utf-8")
    return {
        "kind": "video",
        "width": cam.width,
        "height": cam.height,
        "fps": mode.fps,
        "frames": n,
        "duration_s": round(n / mode.fps, 4),
        "telemetry": f"{v.date}/{path.with_suffix('.SRT').name}",
        "frame_homographies": homs,
    }


# --------------------------------------------------------------------------- #
# Truth helpers (for tests and alignment diagnostics)
# --------------------------------------------------------------------------- #


def load_truth(path: Path) -> dict:
    """Read `_truth.json` (pass the file or the footage folder)."""
    path = Path(path)
    return json.loads((path / TRUTH_NAME if path.is_dir() else path).read_text(encoding="utf-8"))


def truth_files(truth: dict) -> Iterator[dict]:
    for visit in truth["visits"]:
        yield from visit["files"]


def truth_homography(truth: dict, rel_path: str, t: float = 0.0) -> np.ndarray:
    """Exact world (meters) -> image (pixels) homography of a footage file at `t` seconds."""
    for f in truth_files(truth):
        if f["path"] == rel_path:
            flat = f["homography"]
            if f["kind"] == "video":
                flat = f["frame_homographies"][min(max(round(t * f["fps"]), 0), f["frames"] - 1)]
            return np.array(flat, np.float64).reshape(3, 3)
    raise KeyError(rel_path)


def relative_homography(
    truth: dict, src: str, dst: str, t_src: float = 0.0, t_dst: float = 0.0
) -> np.ndarray:
    """Exact homography mapping pixels of `src` onto pixels of `dst`."""
    h = truth_homography(truth, dst, t_dst) @ np.linalg.inv(truth_homography(truth, src, t_src))
    return h / h[2, 2]


def _main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m vantage.demo.synth", description=__doc__.splitlines()[0])
    ap.add_argument("footage_dir", type=Path)
    ap.add_argument("--fast", action="store_true", help="small, quick footage (CI)")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    print(generate_demo_footage(args.footage_dir, fast=args.fast, seed=args.seed))


if __name__ == "__main__":
    _main()
