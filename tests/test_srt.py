from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from vantage.ingest.dji_srt import circular_median, parse_srt, parse_srt_text, sample_at, summarize

MODERN = """1
00:00:00,000 --> 00:00:00,033
<font size="28">FrameCnt: 1, DiffTime: 33ms
2025-04-12 10:41:07.123
[iso: 100] [shutter: 1/1000.0] [fnum: 2.8] [ev: 0] [color_md: default] [focal_len: 24.00] \
[latitude: 12.340100] [longitude: -45.671200] [rel_alt: 60.000 abs_alt: 350.000] \
[gb_yaw: -170.0 gb_pitch: -30.0 gb_roll: 0.0] [ct: 5500] [dzoom_ratio: 10000, delta:0] </font>

2
00:00:00,033 --> 00:00:00,066
<font size="28">FrameCnt: 2, DiffTime: 33ms
2025-04-12 10:41:07.156
[iso: 110] [shutter: 1/800.0] [fnum: 2.8] [ev: -0.3] [focal_len: 24.00] \
[latitude: 12.340200] [longitude: -45.671300] [rel_alt: 61.000 abs_alt: 351.000] \
[gb_yaw: 170.0 gb_pitch: -31.0 gb_roll: 0.0] [ct: 5600] </font>

3
00:00:00,066 --> 00:00:00,100
<font size="28">FrameCnt: 3, DiffTime: 34ms
2025-04-12 10:41:07.190
[iso: 100] [shutter: 1/1000.0] [fnum: 2.8] [ev: 0] [latitude: 12.340300] [longitude: -45.671400] \
[rel_alt: 62.000 abs_alt: 352.000] [gb_yaw: 180.0 gb_pitch: -32.0 gb_roll: 0.0] [ct: 5500] </font>
"""

LEGACY_PHANTOM = """1
00:00:00,000 --> 00:00:01,000
HOME(-45.6700,12.3400) 2017.08.05 14:11:51
GPS(-45.6712,12.3401,350) BAROMETER:58.3
ISO:100 Shutter:1000 EV:0 Fnum:F2.8

2
00:00:01,000 --> 00:00:02,000
HOME(-45.6700,12.3400) 2017.08.05 14:11:52
GPS(-45.6713,12.3402,351) BAROMETER:58.9
ISO:200 Shutter:500 EV:-0.7 Fnum:F2.8
"""

LEGACY_MAVIC_PRO = """1
00:00:00,000 --> 00:00:00,033
F/2.8, SS 1000, ISO 100, EV 0, GPS (-45.6712, 12.3401, 19), D 24.50m, H 60.00m, H.S 0.00m/s, V.S 0.00m/s
"""

MINI = """1
00:00:00,000 --> 00:00:00,033
<font size="36">FrameCnt : 1, DiffTime : 33ms
2021-03-14 14:34:20,384,165
[iso : 100] [shutter : 1/320.0] [fnum : 280] [ev : 0] [ct : 5500] [color_md : default] \
[focal_len : 240] [latitude : 12.3401] [longtitude : -45.6712] [altitude: 350.000] </font>
"""

MATRICE = """1
00:00:00,000 --> 00:00:00,033
<font size="28">SrtCnt : 1, DiffTime : 33ms
2024-06-01 09:00:00.000
[iso : 100] [shutter : 1/400.0] [fnum : 280] [ev : 0] [focal_len : 47.10] [latitude: 12.34] \
[longitude: -45.67] [rel_alt: 50.6 abs_alt: 300] [Drone: Yaw:1.9, Pitch:-3.1, Roll:1.5] \
[Gimbal: Yaw:10.0, Pitch:-45.0, Roll:0.0] [weird_token] [unknown: ???] </font>
"""


def test_modern_bracket_format() -> None:
    samples = parse_srt_text(MODERN)
    assert [s.index for s in samples] == [1, 2, 3]
    s = samples[0]
    assert (s.t_start_s, s.t_end_s) == (0.0, 0.033)
    assert s.datetime == dt.datetime(2025, 4, 12, 10, 41, 7, 123000)
    assert (s.lat, s.lon, s.rel_alt, s.abs_alt) == (12.3401, -45.6712, 60.0, 350.0)
    assert (s.iso, s.shutter, s.fnum, s.ev, s.ct, s.focal_len) == (100, 0.001, 2.8, 0.0, 5500, 24.0)
    assert (s.gimbal_yaw, s.gimbal_pitch, s.gimbal_roll) == (-170.0, -30.0, 0.0)
    assert s.heading is None
    assert s.camera_heading == pytest.approx(190.0)
    assert samples[1].ev == -0.3


def test_legacy_phantom_format() -> None:
    a, b = parse_srt_text(LEGACY_PHANTOM)
    assert a.datetime == dt.datetime(2017, 8, 5, 14, 11, 51)
    assert (a.lon, a.lat, a.abs_alt, a.rel_alt) == (-45.6712, 12.3401, 350.0, 58.3)
    assert (a.iso, a.shutter, a.ev, a.fnum) == (100, 0.001, 0.0, 2.8)
    assert (b.iso, b.shutter, b.ev) == (200, 0.002, -0.7)


def test_legacy_mavic_pro_line() -> None:
    (s,) = parse_srt_text(LEGACY_MAVIC_PRO)
    assert (s.fnum, s.shutter, s.iso, s.ev) == (2.8, 0.001, 100, 0.0)
    assert (s.lon, s.lat, s.abs_alt, s.rel_alt) == (-45.6712, 12.3401, 19.0, 60.0)
    assert s.datetime is None


def test_mini_variant_with_sic_longtitude_and_fixed_point_values() -> None:
    (s,) = parse_srt_text(MINI)
    assert s.datetime == dt.datetime(2021, 3, 14, 14, 34, 20, 384165)
    assert (s.lat, s.lon, s.abs_alt, s.rel_alt) == (12.3401, -45.6712, 350.0, None)
    assert (s.fnum, s.focal_len, s.shutter) == (2.8, 24.0, pytest.approx(1 / 320))


def test_grouped_attitude_and_unknown_tokens() -> None:
    (s,) = parse_srt_text(MATRICE)
    assert s.heading == 1.9
    assert (s.gimbal_yaw, s.gimbal_pitch) == (10.0, -45.0)
    assert (s.fnum, s.focal_len, s.rel_alt) == (2.8, 47.1, 50.6)


def test_crlf_bom_and_file_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "DJI_0001.SRT"
    path.write_bytes(b"\xef\xbb\xbf" + MODERN.replace("\n", "\r\n").encode())
    samples = parse_srt(path)
    assert len(samples) == 3
    assert samples[2].lat == 12.3403
    utf16 = tmp_path / "utf16.srt"
    utf16.write_bytes(MINI.encode("utf-16"))
    assert parse_srt(utf16)[0].lon == -45.6712


def test_missing_fields_are_none_and_garbage_never_crashes() -> None:
    text = (
        "1\n00:00:00,000 --> 00:00:01,000\n[latitude: 0.0] [longitude: 0.0] [iso: n/a] [] [:] ]]\n\nnot srt"
    )
    (s,) = parse_srt_text(text)
    assert (s.lat, s.lon, s.iso, s.datetime, s.shutter) == (None, None, None, None, None)
    assert parse_srt_text("") == []
    assert parse_srt_text("hello\nworld") == []


def test_sample_at_and_summarize() -> None:
    samples = parse_srt_text(MODERN)
    assert sample_at(samples, -1.0) is samples[0]
    assert sample_at(samples, 0.05) is samples[1]
    assert sample_at(samples, 99.0) is samples[2]
    assert sample_at([], 1.0) is None
    summary = summarize(samples)
    assert summary["lat"] == pytest.approx(12.3402)
    assert summary["lon"] == pytest.approx(-45.6713)
    assert summary["rel_alt"] == 61.0
    assert summary["heading"] == pytest.approx(180.0)  # 190°, 170°, 180° across the wrap
    assert summary["gimbal_pitch"] == -31.0
    assert summarize([]) == {"lat": None, "lon": None, "rel_alt": None, "heading": None, "gimbal_pitch": None}


def test_circular_median_wraps() -> None:
    assert circular_median([359.0, 1.0, 3.0]) == pytest.approx(1.0)
    assert circular_median([None, None]) is None


def test_empty_legacy_values_never_swallow_the_next_block_index() -> None:
    blocks = [
        f"{i}\n00:00:0{i - 1},000 --> 00:00:0{i},000\n"
        f"HOME(-45.6700,12.3400) 2016.06.25 10:22:3{i}\n"
        "GPS(-45.6712,12.3401,17) BAROMETER:\n"
        "ISO:100 Shutter:120 Fnum:F2.8 EV:\n"
        for i in (1, 2, 3)
    ]
    samples = parse_srt_text("\n".join(blocks))
    assert [s.index for s in samples] == [1, 2, 3]
    assert [(s.ev, s.rel_alt) for s in samples] == [(None, None)] * 3
    assert [s.datetime.second for s in samples if s.datetime] == [31, 32, 33]


def test_bracket_barometer_is_height_above_takeoff() -> None:
    text = (
        '1\n00:00:00,000 --> 00:00:01,000\n<font size="28">SrtCnt : 1, DiffTime : 1000ms\n'
        "2018-08-10 13:32:46,190,543\n[iso : 100] [shutter : 1/500.0] [fnum : 220] [ev : 0] "
        "[latitude : 12.3401] [longtitude : -45.6712] [barometer: 60.4] </font>\n"
    )
    (s,) = parse_srt_text(text)
    assert (s.lat, s.lon, s.rel_alt, s.fnum) == (12.3401, -45.6712, 60.4, 2.2)
