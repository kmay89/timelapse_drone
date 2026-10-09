"""DJI flight-telemetry subtitles (.SRT) → per-frame samples.

DJI drones write a subtitle block per video frame (older models: per second)
carrying the time, GPS position, altitude and exposure. The dialects differ by
model and firmware; this parser accepts all known ones, ignores tokens it does
not recognise and never raises on content:

* modern bracket format (Mavic 3, Air 3, Mini 3/4, Avata, Matrice …)::

      <font size="28">FrameCnt: 1, DiffTime: 33ms
      2025-04-12 10:41:07.123
      [iso: 100] [shutter: 1/1000.0] [fnum: 2.8] [ev: 0] [latitude: 41.3401] [longitude: -81.3712]
      [rel_alt: 60.000 abs_alt: 350.000] [gb_yaw: 12.3 gb_pitch: -30.0 gb_roll: 0.0] </font>

* Mini 2 / Air 2 / Mavic Pro variants: ``[latitude : …] [longtitude : …] [altitude: …]`` (sic),
  ``[barometer: …]``, ``fnum : 280``
  (×100), ``focal_len : 240`` (×10) and ``2021-03-14 14:34:20,384,165`` timestamps;
* legacy Phantom / Mavic Pro text: ``HOME(lon,lat) 2017.08.05 14:11:51``, ``GPS(lon,lat,alt)
  BAROMETER:58.3``, ``ISO:100 Shutter:1000 EV:0 Fnum:F2.8`` or
  ``F/2.8, SS 1000, ISO 100, EV 0, GPS (lon, lat, alt), D 24.50m, H 60.00m``.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import re
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TelemetrySample:
    """One subtitle block. Angles in degrees, altitudes in meters, `shutter` in seconds."""

    index: int
    t_start_s: float
    t_end_s: float
    datetime: dt.datetime | None = None  # naive local time, as the drone recorded it
    lat: float | None = None
    lon: float | None = None
    rel_alt: float | None = None  # above take-off point
    abs_alt: float | None = None  # above sea level (GPS/barometric)
    iso: int | None = None
    shutter: float | None = None
    fnum: float | None = None
    ev: float | None = None
    ct: int | None = None  # white balance, kelvin
    focal_len: float | None = None  # 35 mm-equivalent where the drone reports it
    gimbal_yaw: float | None = None
    gimbal_pitch: float | None = None
    gimbal_roll: float | None = None
    heading: float | None = None  # aircraft yaw

    @property
    def camera_heading(self) -> float | None:
        """Compass direction the camera looks, in [0, 360)."""
        yaw = self.gimbal_yaw if self.gimbal_yaw is not None else self.heading
        return None if yaw is None else yaw % 360.0


_TIMECODE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
_DATETIME = re.compile(
    r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})[ T]+(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?(?:,(\d{1,3}))?"
)
_TAG = re.compile(r"<[^>]*>")
_BRACKET = re.compile(r"\[([^\[\]]*)\]")
_GROUP = re.compile(r"^\s*(drone|aircraft|gimbal)\s*:\s*(?=[A-Za-z])", re.I)
_PAIR = re.compile(r"([A-Za-z][\w]*)\s*:\s*([^\s,\[\]]+)")
_NUM = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:/[+-]?(?:\d+\.?\d*|\.\d+))?")

# Bracket keys (lower-cased; group prefix "drone_"/"gimbal_" for "[Drone: Yaw:…]") → field.
_KEYS = {
    "iso": "iso",
    "shutter": "shutter",
    "fnum": "fnum",
    "ev": "ev",
    "ct": "ct",
    "focal_len": "focal_len",
    "latitude": "lat",
    "lat": "lat",
    "longitude": "lon",
    "longtitude": "lon",
    "lon": "lon",
    "lng": "lon",
    "rel_alt": "rel_alt",
    "abs_alt": "abs_alt",
    "altitude": "abs_alt",
    "barometer": "rel_alt",
    "gb_yaw": "gimbal_yaw",
    "gb_pitch": "gimbal_pitch",
    "gb_roll": "gimbal_roll",
    "gimbal_yaw": "gimbal_yaw",
    "gimbal_pitch": "gimbal_pitch",
    "gimbal_roll": "gimbal_roll",
    "drone_yaw": "heading",
    "aircraft_yaw": "heading",
}

# Legacy free-text fields, matched outside [brackets]; earlier fields win.
_LEGACY: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("iso", re.compile(r"(?<![\w])ISO\s*[:=]?\s*(\d+)", re.I)),
    ("shutter", re.compile(r"(?<![\w])(?:Shutter|SS)\s*[:=]?\s*([\d./]+)", re.I)),
    ("ev", re.compile(r"(?<![\w])EV\s*[:=]?\s*([-+]?[\d./]+)", re.I)),
    ("fnum", re.compile(r"(?<![\w])F(?:num)?\s*[:=]?\s*F?\s*/?\s*(\d+(?:\.\d+)?)")),
    ("rel_alt", re.compile(r"(?<![\w.])H(?![\w.])\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)")),
    ("rel_alt", re.compile(r"(?<![\w])BAROMETER\s*[:=]?\s*([-+]?\d+(?:\.\d+)?)", re.I)),
)
_GPS = re.compile(
    r"(?<![\w])GPS\s*\(\s*([-+]?\d+(?:\.\d+)?)\s*,\s*([-+]?\d+(?:\.\d+)?)(?:\s*,\s*([-+]?\d+(?:\.\d+)?))?"
)


def _num(raw: str) -> float | None:
    m = _NUM.match(raw.strip())
    if not m:
        return None
    num, _, den = m.group(0).partition("/")
    try:
        value = float(num) / float(den) if den else float(num)
    except (ValueError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) else None


def _shutter(raw: str) -> float | None:
    """'1/1000.0' → 0.001; a bare denominator ('SS 1000', 'Shutter:60') → its reciprocal."""
    value = _num(raw)
    if value is None or value <= 0:
        return None
    return 1.0 / value if "/" not in raw and value > 1 else value


def _scaled(divisor: float, threshold: float) -> Callable[[str], float | None]:
    """Some firmwares write fixed-point integers: 'fnum : 280' is f/2.8, 'focal_len : 240' is 24 mm."""

    def parse(raw: str) -> float | None:
        value = _num(raw.lstrip("Ff/ "))
        if value is not None and "." not in raw and value >= threshold:
            value /= divisor
        return value

    return parse


def _int(raw: str) -> int | None:
    value = _num(raw)
    return None if value is None else round(value)


_PARSERS: dict[str, Callable[[str], float | int | None]] = {
    "iso": _int,
    "ct": _int,
    "shutter": _shutter,
    "fnum": _scaled(100.0, 10.0),
    "focal_len": _scaled(10.0, 100.0),
}


def _datetime(text: str) -> dt.datetime | None:
    m = _DATETIME.search(text)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups()[:6])
    frac = ((m.group(7) or "") + (m.group(8) or "")).ljust(6, "0")[:6]
    try:
        return dt.datetime(y, mo, d, h, mi, s, int(frac))
    except ValueError:
        return None


def _bracket_fields(inner: str, fields: dict[str, str]) -> None:
    prefix = ""
    group = _GROUP.match(inner)
    if group:
        prefix = "gimbal_" if group.group(1).lower() == "gimbal" else "drone_"
        inner = inner[group.end() :]
    for key, raw in _PAIR.findall(inner):
        name = _KEYS.get(prefix + key.lower())
        if name:
            fields.setdefault(name, raw)


def _block_fields(body: str) -> dict[str, float | int | dt.datetime | None]:
    text = _TAG.sub(" ", body)
    raw: dict[str, str] = {}
    for inner in _BRACKET.findall(text):
        _bracket_fields(inner, raw)
    rest = _BRACKET.sub(" ", text)
    gps = _GPS.search(rest)
    if gps:
        a, b, alt = gps.groups()
        lon, lat = (a, b) if abs(float(b)) <= 90 or abs(float(a)) > 90 else (b, a)
        raw.setdefault("lon", lon)
        raw.setdefault("lat", lat)
        if alt is not None:
            raw.setdefault("abs_alt", alt)
    for name, pattern in _LEGACY:
        m = pattern.search(rest)
        if m:
            raw.setdefault(name, m.group(1))
    out: dict[str, float | int | dt.datetime | None] = {
        name: _PARSERS.get(name, _num)(value) for name, value in raw.items()
    }
    if out.get("lat") == 0 and out.get("lon") == 0:  # no GPS fix
        out["lat"] = out["lon"] = None
    out["datetime"] = _datetime(rest)
    return out


def _seconds(h: str, m: str, s: str, frac: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac) / 10 ** len(frac)


def _decode(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def parse_srt_text(text: str) -> list[TelemetrySample]:
    """Parse the contents of a DJI .SRT file (any dialect, any line ending)."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[int, float, float, list[str]]] = []
    previous = ""
    for line in lines:
        m = _TIMECODE.search(line)
        if m:
            index = int(previous) if previous.isdigit() else len(blocks) + 1
            if previous.isdigit() and blocks:
                blocks[-1][3].pop()  # this block's index line, not the previous block's text
            g = m.groups()
            blocks.append((index, _seconds(*g[:4]), _seconds(*g[4:]), []))
        elif blocks:
            blocks[-1][3].append(line)
        previous = line.strip()
    return [
        TelemetrySample(index=index, t_start_s=start, t_end_s=end, **_block_fields("\n".join(body)))  # type: ignore[arg-type]
        for index, start, end, body in blocks
    ]


def parse_srt(path: Path) -> list[TelemetrySample]:
    """Parse a DJI .SRT telemetry sidecar. Missing fields are None; unknown tokens are ignored."""
    return parse_srt_text(_decode(Path(path).read_bytes()))


def sample_at(samples: Sequence[TelemetrySample], t: float) -> TelemetrySample | None:
    """The sample covering time `t` (seconds into the clip), clamped to the first/last sample."""
    if not samples:
        return None
    i = bisect.bisect_right(samples, t, key=lambda s: s.t_start_s)
    return samples[max(i - 1, 0)]


def _median(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return float(statistics.median(known)) if known else None


def circular_median(angles: Sequence[float | None]) -> float | None:
    """Median of compass angles in degrees, robust to the 359°/1° wrap; result in [0, 360)."""
    known = [a for a in angles if a is not None]
    if not known:
        return None
    rad = [math.radians(a) for a in known]
    mean = math.degrees(math.atan2(sum(map(math.sin, rad)), sum(map(math.cos, rad))))
    deltas = [(a - mean + 180.0) % 360.0 - 180.0 for a in known]
    return (mean + statistics.median(deltas)) % 360.0


def summarize(samples: Sequence[TelemetrySample]) -> dict[str, float | None]:
    """Median position, height, camera heading and gimbal pitch of a clip (None when unknown)."""
    return {
        "lat": _median([s.lat for s in samples]),
        "lon": _median([s.lon for s in samples]),
        "rel_alt": _median([s.rel_alt for s in samples]),
        "heading": circular_median([s.camera_heading for s in samples]),
        "gimbal_pitch": _median([s.gimbal_pitch for s in samples]),
    }
