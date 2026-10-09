from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

import pytest
from PIL import ExifTags, Image

from vantage.ingest.exif import parse_exif_datetime, read_still_metadata


def _dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    d = int(value)
    m = int((value - d) * 60)
    return float(d), float(m), round((value - d - m / 60) * 3600, 4)


def _exif(
    when: str | None = None, lat: float | None = None, lon: float | None = None, orientation: int = 1
) -> Image.Exif:
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "DJI"
    exif[ExifTags.Base.Model] = "FC3582"
    exif[ExifTags.Base.Orientation] = orientation
    if when:
        exif[ExifTags.Base.DateTime] = "2000:01:01 00:00:00"
        sub = exif.get_ifd(ExifTags.IFD.Exif)
        sub[ExifTags.Base.DateTimeOriginal] = when
        sub[ExifTags.Base.SubsecTimeOriginal] = "25"
    if lat is not None and lon is not None:
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        gps[ExifTags.GPS.GPSLatitudeRef] = "N" if lat >= 0 else "S"
        gps[ExifTags.GPS.GPSLatitude] = _dms(lat)
        gps[ExifTags.GPS.GPSLongitudeRef] = "E" if lon >= 0 else "W"
        gps[ExifTags.GPS.GPSLongitude] = _dms(lon)
        gps[ExifTags.GPS.GPSAltitudeRef] = 0
        gps[ExifTags.GPS.GPSAltitude] = 350.5
    return exif


def _inject_app1_xmp(path: Path, packet: bytes) -> None:
    """Insert an XMP APP1 segment right after SOI, the way DJI firmware writes it."""
    data = path.read_bytes()
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + packet
    segment = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    path.write_bytes(data[:2] + segment + data[2:])


def test_exif_date_and_gps_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "DJI_0001.JPG"
    exif = _exif("2025:04:12 10:41:07", 41.3401234, -81.3712345)
    Image.new("RGB", (64, 48), (90, 120, 80)).save(path, exif=exif)
    meta = read_still_metadata(path)
    assert meta.datetime == dt.datetime(2025, 4, 12, 10, 41, 7, 250000)
    assert meta.lat == pytest.approx(41.3401234, abs=1e-6)
    assert meta.lon == pytest.approx(-81.3712345, abs=1e-6)
    assert meta.abs_alt == pytest.approx(350.5)
    assert (meta.width, meta.height, meta.make, meta.model) == (64, 48, "DJI", "FC3582")
    assert meta.rel_alt is None and meta.camera_heading is None


def test_dji_xmp_attributes_via_pillow(tmp_path: Path) -> None:
    path = tmp_path / "attr.jpg"
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"'
        ' drone-dji:RelativeAltitude="+60.10" drone-dji:GimbalPitchDegree="-30.50"'
        ' drone-dji:GimbalYawDegree="-170.00" drone-dji:GimbalRollDegree="+0.00"'
        ' drone-dji:FlightYawDegree="+12.30"/></rdf:RDF></x:xmpmeta>'
    )
    Image.new("RGB", (32, 32)).save(path, exif=_exif("2025:04:12 10:41:07"), xmp=xmp.encode())
    meta = read_still_metadata(path)
    assert (meta.rel_alt, meta.gimbal_pitch, meta.gimbal_yaw, meta.flight_yaw) == (60.1, -30.5, -170.0, 12.3)
    assert meta.camera_heading == pytest.approx(190.0)


def test_injected_xmp_elements_and_gps_fallback(tmp_path: Path) -> None:
    path = tmp_path / "elem.jpg"
    Image.new("RGB", (40, 30)).save(path, exif=_exif(orientation=6))
    packet = (
        b'<?xpacket begin="\xef\xbb\xbf" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description>'
        b"<drone-dji:RelativeAltitude>+45.00</drone-dji:RelativeAltitude>"
        b"<drone-dji:GimbalPitchDegree> -90.0 </drone-dji:GimbalPitchDegree>"
        b"<drone-dji:GpsLatitude>41.25</drone-dji:GpsLatitude>"
        b"<drone-dji:GpsLongtitude>-81.5</drone-dji:GpsLongtitude>"
        b"</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end='w'?>"
    )
    _inject_app1_xmp(path, packet)
    meta = read_still_metadata(path)
    assert (meta.rel_alt, meta.gimbal_pitch, meta.lat, meta.lon) == (45.0, -90.0, 41.25, -81.5)
    assert (meta.width, meta.height) == (30, 40)  # orientation 6 → displayed portrait
    assert meta.datetime is None


def test_bare_png_has_only_dimensions(tmp_path: Path) -> None:
    path = tmp_path / "plain.png"
    Image.new("RGB", (20, 10)).save(path)
    meta = read_still_metadata(path)
    assert (meta.width, meta.height, meta.datetime, meta.lat, meta.make) == (20, 10, None, None, None)


def test_unreadable_file_raises_oserror(tmp_path: Path) -> None:
    path = tmp_path / "x.heic"
    path.write_bytes(b"\x00\x00\x00\x18ftypheic not really")
    with pytest.raises(OSError):
        read_still_metadata(path)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2025:04:12 10:41:07", dt.datetime(2025, 4, 12, 10, 41, 7)),
        (b"2025-04-12 10:41:07\x00", dt.datetime(2025, 4, 12, 10, 41, 7)),
        ("    :  :     :  :  ", None),
        ("0000:00:00 00:00:00", None),
        (None, None),
    ],
)
def test_parse_exif_datetime(value: object, expected: dt.datetime | None) -> None:
    assert parse_exif_datetime(value) == expected
