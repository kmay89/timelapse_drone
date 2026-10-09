"""Public-repo hygiene: the files git would publish carry no real-world coordinates.

Invariant 1 (CLAUDE.md) keeps client data out of this public repo, and a site's GPS position is client
data: one fixture copied from real telemetry pins a client's site to a few metres. Tests, docs,
templates and skills use synthetic places only. The scan is an allowlist, so it names no real place:
every latitude/longitude written in a tracked (or new, unignored) text file must lie near one of
`ANCHORS`. Failures report file:line only, never the value, so CI logs do not repeat a leak.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from vantage.demo.synth import ORIGIN
from vantage.paths import PUBLIC_PROJECTS, repo_root

ROOT = repo_root()
TEST_SITE = (12.34, -45.67)  # open Atlantic: the place every test, doc and template example uses
ANCHORS: tuple[tuple[float, float], ...] = (TEST_SITE, (0.0, 0.0), ORIGIN)  # + "no fix", the demo
TOLERANCE_DEG = 0.02  # ~2 km
MAX_BYTES = 4 << 20
SELF = Path(__file__).resolve()  # holds deliberately misplaced samples for the scanner's own tests

_NUM = r"(-?\d{1,3}(?:\.\d+)?)"
# `lat: 1.5`, `"lon": 2`, `[latitude: {1.5 + i:.6f}]`, `lat=1.5`, `s.lat == pytest.approx(1.5)`,
# XMP `<drone-dji:GpsLatitude>1.5<` and `drone-dji:GpsLongitude="+1.5"`
_LABELLED = re.compile(
    r"(?i)\b(?:gps_?)?(lat|latitude|lon|lng|longitude|longtitude)\b"
    r"(?:>|[\"']?\s*(?:==\s*(?:pytest\.approx\()?|[:=]\s*[\"']?))\s*\{?\s*\+?" + _NUM
)
# DJI legacy text: HOME(lon,lat) and GPS(lon,lat,alt)
_LEGACY = re.compile(r"\b(?:HOME|GPS)\s*\(\s*" + _NUM + r"\s*,\s*" + _NUM)
_DMS = re.compile(  # EXIF GPSLatitude = (deg, min, sec)
    r"(?i)\bGPS_?(Latitude|Longitude)\b.*?[(\[]\s*(\d{1,3}(?:\.\d+)?)\s*,\s*(\d{1,2}(?:\.\d+)?)\s*,"
    r"\s*(\d{1,2}(?:\.\d+)?)\s*[)\]]"
)
_PAIR = re.compile(r"(?<![\w.])(-?\d{1,3}\.\d{4,})\s*,\s*(-?\d{1,3}\.\d{4,})(?![\w.])")  # (lat, lon) literals


def _near(value: float, axis: int | None, *, unsigned: bool = False) -> bool:
    """True if `value` is within tolerance of an anchor's lat (axis 0), lon (axis 1) or either (None)."""
    comps = [a[axis] for a in ANCHORS] if axis is not None else [c for a in ANCHORS for c in a]
    if unsigned:
        value, comps = abs(value), [abs(c) for c in comps]
    return any(abs(value - c) <= TOLERANCE_DEG for c in comps)


def misplaced_coordinates(line: str) -> list[str]:
    """Kinds of coordinate on this line that lie away from every synthetic anchor."""
    bad: list[str] = []
    for key, num in _LABELLED.findall(line):
        axis = 0 if key.lower().startswith("lat") else 1
        if abs(float(num)) <= (90, 180)[axis] and not _near(float(num), axis):
            bad.append(("latitude", "longitude")[axis])
    for lon, lat in _LEGACY.findall(line):
        if not (_near(float(lon), 1) and _near(float(lat), 0)):
            bad.append("HOME/GPS(lon, lat)")
    for key, deg, mins, secs in _DMS.findall(line):
        value = float(deg) + float(mins) / 60 + float(secs) / 3600
        if not _near(value, 0 if key.lower() == "latitude" else 1, unsigned=True):
            bad.append(f"EXIF {key} (deg, min, sec)")
    for a, b in _PAIR.findall(line):
        x, y = abs(float(a)), abs(float(b))
        if max(x, y) < 2 or max(x, y) > 180:
            continue  # near-unit numbers (matrices, colours, easing) or not a coordinate at all
        if not (_near(float(a), None) and _near(float(b), None)):
            bad.append("decimal pair")
    return bad


def _published_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return sorted(ROOT / p for p in out.decode().split("\0") if p)


@pytest.mark.parametrize(
    "line",
    [
        "  lat: 55.5555",
        '"lon": -66.6666, "rel_alt": 60.1',
        "[iso: 100] [latitude: {55.55 + i * 1e-5:.6f}] [longitude: -66.66]",
        "[latitude : 55.5555] [longtitude : -66.6666] [altitude: 350.000]",
        "assert video.lat == pytest.approx(55.5555, abs=1e-4)",
        "HOME(-66.6600,55.5500) 2017.08.05 14:11:51",
        "F/2.8, SS 1000, ISO 100, EV 0, GPS (-66.6612, 55.5501, 19), D 24.50m",
        'gps[ExifTags.GPS.GPSLatitudeRef], gps[ExifTags.GPS.GPSLatitude] = "N", (55.0, 33.0, 20.0)',
        "SITE = (55.5555, -66.6666)  # the vantage hint",
        'exif = _exif("2025:04:12 10:41:07", 55.5555555, -66.6666666)',
        'b"<drone-dji:GpsLatitude>55.25</drone-dji:GpsLatitude>"',
        '<rdf:Description drone-dji:GpsLongitude="+66.5" drone-dji:RelativeAltitude="+60.10">',
        '"GPSLongitude": [66, 39, 59.9]',
        "location = (51.5074, -0.1278)",
    ],
)
def test_scanner_flags_each_form(line: str) -> None:
    assert misplaced_coordinates(line)


@pytest.mark.parametrize(
    "line",
    [
        f"[latitude: {TEST_SITE[0] + 0.0001:.6f}] [longitude: {TEST_SITE[1]:.6f}] [rel_alt: 60.000 abs_alt: 350.000]",
        f"HOME({TEST_SITE[1]:.4f},{TEST_SITE[0]:.4f}) GPS({TEST_SITE[1]:.4f},{TEST_SITE[0]:.4f},350)",
        f"  lat: {ORIGIN[0] - 0.002:.4f}",
        "[latitude: 0.000000] [longitude: 0.000000]",
        'gps[ExifTags.GPS.GPSLongitude] = "W", (45.0, 40.0, 12.0)',
        "H = np.array([[1.0012, 0.0034, 5.0], [0.0021, 0.9987, 3.0]]); s = (1.0012, 1.0034)",
        "transform: translate(12.5px, 3px); line-height: 1.4;",
        "if abs(lat) > 90 or lat_deg == 91.5 or GPSLatitudeRef == 'N':",
        f'<drone-dji:GpsLatitude>{TEST_SITE[0]:.2f}</drone-dji:GpsLatitude> "lon": {ORIGIN[1]}',
    ],
)
def test_scanner_passes_synthetic_and_unrelated_numbers(line: str) -> None:
    assert misplaced_coordinates(line) == []


def test_published_files_hold_only_synthetic_coordinates() -> None:
    found: list[str] = []
    for path in _published_files():
        if path.resolve() == SELF or not path.is_file() or path.stat().st_size > MAX_BYTES:
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        for n, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            found += [
                f"{path.relative_to(ROOT).as_posix()}:{n}: {kind}" for kind in misplaced_coordinates(line)
            ]
    assert not found, (
        "real-looking coordinates (use the synthetic TEST_SITE or the demo ORIGIN):\n" + "\n".join(found)
    )


@pytest.mark.parametrize(
    "path",
    [
        "projects/acme-park/project.yaml",
        "projects/acme-park/facts.yaml",
        "projects/acme-park/README.md",
        "projects/acme-park/brand/logo.svg",
        "projects/acme-park/masters/overview/2025-06-14.jpg",
        "acme-park/project.yaml",  # `vantage new acme-park --dir ""` used to land at the top level
        "acme-park/story.yaml",
        "acme-park/facts.yaml",
        "acme-park/brand/brand.yaml",
        "acme-park/masters/overview/2025-06-14.jpg",
    ],
)
def test_gitignore_keeps_client_projects_out(path: str) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=ROOT, check=False)
    if result.returncode == 128:
        pytest.skip("not a git checkout")
    assert result.returncode == 0, f"{path} is not ignored: one `git add -A` would publish a client project"


def test_only_fictional_projects_are_published() -> None:
    project_files = [p for p in _published_files() if p.name == "project.yaml"]
    allowed = {ROOT / "projects" / slug / "project.yaml" for slug in PUBLIC_PROJECTS}
    allowed.add(ROOT / "src" / "vantage" / "templates" / "project" / "project.yaml")
    assert project_files and set(project_files) <= allowed, sorted(
        p.relative_to(ROOT).as_posix() for p in set(project_files) - allowed
    )
