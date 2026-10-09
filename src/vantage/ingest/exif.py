"""Capture time, position and camera attitude of a still, using Pillow only.

EXIF gives the shutter time (``DateTimeOriginal``, local and naive by
definition) and GPS position; DJI additionally writes an XMP packet with
``drone-dji:RelativeAltitude``, ``GimbalPitchDegree``, ``GimbalYawDegree`` and
``FlightYawDegree``. The XMP packet is found by scanning the head of the file,
so it works for JPEG, TIFF and PNG alike without an XMP library.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image

XMP_SCAN_BYTES = 2 << 20  # DJI puts XMP right after EXIF, well inside the first 2 MB

_EXIF_DATETIME = re.compile(r"(\d{4})[:-](\d{2})[:-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_XMP_ATTR = re.compile(r"drone-dji:(\w+)\s*=\s*([\"'])(.*?)\2", re.S)
_XMP_ELEM = re.compile(r"<drone-dji:(\w+)>\s*([^<]*?)\s*</drone-dji:\1>", re.S)


@dataclass(frozen=True, slots=True)
class StillMeta:
    """What a photo knows about itself. Angles in degrees, altitudes in meters."""

    width: int  # as displayed (EXIF orientation applied)
    height: int
    datetime: dt.datetime | None = None  # naive local shutter time
    lat: float | None = None
    lon: float | None = None
    abs_alt: float | None = None
    rel_alt: float | None = None
    gimbal_pitch: float | None = None
    gimbal_yaw: float | None = None
    gimbal_roll: float | None = None
    flight_yaw: float | None = None
    make: str | None = None
    model: str | None = None

    @property
    def camera_heading(self) -> float | None:
        """Compass direction the camera looks, in [0, 360)."""
        yaw = self.gimbal_yaw if self.gimbal_yaw is not None else self.flight_yaw
        return None if yaw is None else yaw % 360.0


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    if not isinstance(value, str):
        return None
    value = value.strip("\x00 ").strip()
    return value or None


def _float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return out if math.isfinite(out) else None


def parse_exif_datetime(value: Any, subsec: Any = None) -> dt.datetime | None:
    """'2025:04:12 10:41:07' (+ optional SubSecTime '123') → naive datetime; None when blank/invalid."""
    text = _text(value)
    m = _EXIF_DATETIME.match(text or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", _text(subsec) or "")[:6]
    try:
        return dt.datetime(*(int(g) for g in m.groups()), int(digits.ljust(6, "0")) if digits else 0)
    except ValueError:
        return None


def _dms(value: Any, ref: Any) -> float | None:
    """GPS (degrees, minutes, seconds) rationals + 'N'/'S'/'E'/'W' ref → signed decimal degrees."""
    raw = value[:3] if isinstance(value, Sequence) and not isinstance(value, str | bytes) else [value]
    parts = [p for p in map(_float, raw) if p is not None]
    if not parts or len(parts) != len(raw):
        return None
    deg = sum(p / 60**i for i, p in enumerate(parts))
    return -deg if (_text(ref) or "").upper() in {"S", "W"} else deg


def _xmp_fields(path: Path) -> dict[str, str]:
    with open(path, "rb") as f:
        head = f.read(XMP_SCAN_BYTES)
    start = head.find(b"<x:xmpmeta")
    if start < 0:
        return {}
    end = head.find(b"</x:xmpmeta>", start)
    packet = head[start : end if end >= 0 else len(head)].decode("utf-8", errors="replace")
    fields = {k: v.strip() for k, v in _XMP_ELEM.findall(packet)}
    fields.update({k: v.strip() for k, _, v in _XMP_ATTR.findall(packet)})
    return fields


def read_still_metadata(path: Path) -> StillMeta:
    """Read EXIF date/GPS and DJI XMP attitude from a still. Raises OSError if Pillow can't open it."""
    with Image.open(path) as im:
        width, height = im.size
        exif = im.getexif()
        upright = im.format == "TIFF"  # Pillow already reports TIFF sizes with orientation applied
    if not upright and exif.get(ExifTags.Base.Orientation) in (5, 6, 7, 8):
        width, height = height, width
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    xmp = _xmp_fields(path)

    when = (
        parse_exif_datetime(
            sub.get(ExifTags.Base.DateTimeOriginal), sub.get(ExifTags.Base.SubsecTimeOriginal)
        )
        or parse_exif_datetime(
            sub.get(ExifTags.Base.DateTimeDigitized), sub.get(ExifTags.Base.SubsecTimeDigitized)
        )
        or parse_exif_datetime(exif.get(ExifTags.Base.DateTime), sub.get(ExifTags.Base.SubsecTime))
    )
    lat = _dms(gps.get(ExifTags.GPS.GPSLatitude), gps.get(ExifTags.GPS.GPSLatitudeRef))
    lon = _dms(gps.get(ExifTags.GPS.GPSLongitude), gps.get(ExifTags.GPS.GPSLongitudeRef))
    if lat is None or lon is None or (lat == 0 and lon == 0):
        lat = _float(xmp.get("GpsLatitude"))
        lon = _float(xmp.get("GpsLongitude") or xmp.get("GpsLongtitude"))
    if lat == 0 and lon == 0:  # no GPS fix
        lat = lon = None
    abs_alt = _float(gps.get(ExifTags.GPS.GPSAltitude))
    if abs_alt is not None and gps.get(ExifTags.GPS.GPSAltitudeRef) in (1, b"\x01"):
        abs_alt = -abs_alt
    return StillMeta(
        width=width,
        height=height,
        datetime=when,
        lat=lat,
        lon=lon,
        abs_alt=abs_alt if abs_alt is not None else _float(xmp.get("AbsoluteAltitude")),
        rel_alt=_float(xmp.get("RelativeAltitude")),
        gimbal_pitch=_float(xmp.get("GimbalPitchDegree")),
        gimbal_yaw=_float(xmp.get("GimbalYawDegree")),
        gimbal_roll=_float(xmp.get("GimbalRollDegree")),
        flight_yaw=_float(xmp.get("FlightYawDegree")),
        make=_text(exif.get(ExifTags.Base.Make)),
        model=_text(exif.get(ExifTags.Base.Model)),
    )
