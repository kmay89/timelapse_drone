"""Synthetic demo footage (vantage.demo.synth) and the demo-lakeside project that uses it."""

from __future__ import annotations

import datetime as dt
import itertools
import re
import time
import typing
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import ExifTags, Image

from vantage import config
from vantage.demo import synth
from vantage.media.ffmpeg import iter_frames, probe
from vantage.paths import repo_root

DEMO = repo_root() / "projects" / "demo-lakeside"
SRT_BLOCK = re.compile(
    r"(?P<n>\d+)\n"
    r"(?P<t0>\d\d:\d\d:\d\d,\d{3}) --> (?P<t1>\d\d:\d\d:\d\d,\d{3})\n"
    r'<font size="28">FrameCnt: (?P<cnt>\d+), DiffTime: (?P<diff>\d+)ms\n'
    r"(?P<when>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\n"
    r"(?P<tele>\[iso: \d+\].*\[ct: \d+\]) </font>"
)
TELEMETRY = re.compile(r"\[(\w+): ([-\d./]+)(?: (\w+): ([-\d.]+))?(?: (\w+): ([-\d.]+))?\]")


@pytest.fixture(scope="module")
def footage(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    root = tmp_path_factory.mktemp("footage")
    return root, synth.load_truth(synth.generate_demo_footage(root, fast=True))


def _files(truth: dict, vantage: str | None = None, kind: str | None = None) -> list[dict]:
    return [
        f
        for f in synth.truth_files(truth)
        if (vantage is None or f["vantage"] == vantage) and (kind is None or f["kind"] == kind)
    ]


def _still(truth: dict, date: str, vantage: str = "overview") -> dict:
    return next(f for f in _files(truth, vantage, "photo") if f["path"].startswith(date))


def _gradient(img: np.ndarray) -> np.ndarray:
    g = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32), (0, 0), 1.5)
    return cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))


def _ncc(a: np.ndarray, b: np.ndarray, mask: np.ndarray) -> float:
    a, b = a[mask] - a[mask].mean(), b[mask] - b[mask].mean()
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum()))


def _sift_homography(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    sift = cv2.SIFT_create(4000)
    ka, da = sift.detectAndCompute(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), None)
    kb, db = sift.detectAndCompute(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), None)
    good = [m for m, n in cv2.BFMatcher().knnMatch(da, db, k=2) if m.distance < 0.75 * n.distance]
    pa = np.float32([ka[m.queryIdx].pt for m in good])
    pb = np.float32([kb[m.trainIdx].pt for m in good])
    hom, inliers = cv2.findHomography(pa, pb, cv2.USAC_MAGSAC, 3.0)
    assert hom is not None and inliers.sum() >= 50
    return hom


def _grid_error(h_est: np.ndarray, h_true: np.ndarray, w: int, h: int) -> float:
    xs, ys = np.meshgrid(np.linspace(0.1 * w, 0.9 * w, 9), np.linspace(0.1 * h, 0.9 * h, 7))
    pts = np.stack([xs, ys], -1).reshape(-1, 1, 2).astype(np.float32)
    err = cv2.perspectiveTransform(pts, h_est) - cv2.perspectiveTransform(pts, h_true)
    return float(np.median(np.linalg.norm(err, axis=-1)))


# --------------------------------------------------------------------------- #
# Footage
# --------------------------------------------------------------------------- #


def test_visit_folders_and_takes(footage: tuple[Path, dict]) -> None:
    root, truth = footage
    folders = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert len(folders) == 12 and folders[0] == "2025-04-12" and folders[-1] == "2026-09-20"
    days = [dt.date.fromisoformat(d) for d in folders]
    assert all(35 <= (b - a).days <= 60 for a, b in itertools.pairwise(days))
    assert [v["date"] for v in truth["visits"]] == folders
    assert sum(v["season"] == "winter" for v in truth["visits"]) >= 2
    kinds = [{f["kind"] for f in v["files"] if f["vantage"] == "overview"} for v in truth["visits"]]
    assert kinds.count({"photo"}) >= 1 and kinds.count({"video"}) >= 1
    assert kinds.count({"photo", "video"}) > len(kinds) / 2
    assert len(_files(truth, "distractor")) == 1
    for f in synth.truth_files(truth):
        path = root / f["path"]
        assert path.is_file() and path.parent.name == f["path"][:10]
        if f["kind"] == "video":
            assert re.fullmatch(r"DJI_\d{4}\.MP4", path.name) and (root / f["telemetry"]).is_file()
        else:
            assert re.fullmatch(r"DJI_\d{14}_\d{4}\.JPG", path.name)
            assert path.name[4:12] == f["path"][:10].replace("-", "")


def test_srt_telemetry(footage: tuple[Path, dict]) -> None:
    root, truth = footage
    video = _files(truth, "overview", "video")[0]
    text = (root / video["telemetry"]).read_text(encoding="utf-8")
    blocks = [b for b in text.strip().split("\n\n") if b]
    assert len(blocks) == video["frames"]
    for i, block in enumerate(blocks):
        m = SRT_BLOCK.fullmatch(block)
        assert m, block
        assert int(m["n"]) == int(m["cnt"]) == i + 1
        assert m["when"].startswith(video["path"][:10])
    tele: dict[str, float] = {}
    for groups in TELEMETRY.findall(SRT_BLOCK.fullmatch(blocks[0])["tele"]):
        for k, v in zip(groups[::2], groups[1::2], strict=True):
            if k:
                tele[k] = float(v.split("/")[-1])
    cam = video["camera"]
    assert tele["latitude"] == pytest.approx(cam["lat"], abs=2e-6)
    assert tele["longitude"] == pytest.approx(cam["lon"], abs=2e-6)
    assert tele["rel_alt"] == pytest.approx(cam["alt_m"], abs=0.01)
    assert tele["abs_alt"] == pytest.approx(cam["alt_m"] + synth.GROUND_ASL_M, abs=0.01)
    assert tele["gb_pitch"] == pytest.approx(-cam["pitch_deg"], abs=0.1)
    assert abs(tele["latitude"] - synth.ORIGIN[0]) < 0.01 and abs(tele["longitude"] - synth.ORIGIN[1]) < 0.01


def test_still_exif_and_xmp(footage: tuple[Path, dict]) -> None:
    root, truth = footage
    for f in _files(truth, kind="photo")[:4]:
        img = Image.open(root / f["path"])
        assert img.size == (f["width"], f["height"])
        exif = img.getexif()
        taken = exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal]
        assert dt.datetime.strptime(taken, "%Y:%m:%d %H:%M:%S") == dt.datetime.strptime(
            Path(f["path"]).name[4:18], "%Y%m%d%H%M%S"
        )
        gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
        d, m, s = (float(x) for x in gps[ExifTags.GPS.GPSLatitude])
        assert gps[ExifTags.GPS.GPSLatitudeRef] == "N" and gps[ExifTags.GPS.GPSLongitudeRef] == "W"
        assert d + m / 60 + s / 3600 == pytest.approx(f["camera"]["lat"], abs=1e-6)
        assert b"drone-dji:GimbalPitchDegree" in img.info["xmp"]


def test_videos_probe(footage: tuple[Path, dict]) -> None:
    root, truth = footage
    for f in _files(truth, kind="video"):
        info = probe(root / f["path"])
        assert (info.kind, info.codec, info.width, info.height) == ("video", "h264", 960, 540)
        assert info.fps == pytest.approx(24) and info.duration_s == pytest.approx(3.0, abs=0.1)
        assert info.creation_time and info.creation_time.startswith(f["start"][:10])


def test_truth_is_consistent(footage: tuple[Path, dict]) -> None:
    _, truth = footage
    assert truth["generator"]["seed"] == 7 and truth["generator"]["fast"] is True
    for f in synth.truth_files(truth):
        hom = synth.truth_homography(truth, f["path"])
        assert hom.shape == (3, 3) and hom[2, 2] == pytest.approx(1.0)
        if f["kind"] == "video":
            assert len(f["frame_homographies"]) == f["frames"] == round(f["duration_s"] * f["fps"])
            assert np.allclose(
                np.reshape(f["frame_homographies"][0], (3, 3)), np.reshape(f["homography"], (3, 3))
            )
            # Hover drift: smooth, small, and actually moving.
            centers = [
                cv2.perspectiveTransform(
                    np.float32([[[f["width"] / 2, f["height"] / 2]]]), np.linalg.inv(np.reshape(h, (3, 3)))
                )[0, 0]
                for h in f["frame_homographies"]
            ]
            steps = np.linalg.norm(np.diff(centers, axis=0), axis=1)
            assert 0 < steps.max() < 0.5 and np.linalg.norm(centers[-1] - centers[0]) < 6.0
    ab = synth.relative_homography(
        truth, _still(truth, "2025-04-12")["path"], _still(truth, "2026-09-20")["path"]
    )
    ba = synth.relative_homography(
        truth, _still(truth, "2026-09-20")["path"], _still(truth, "2025-04-12")["path"]
    )
    assert np.allclose(ab @ ba / (ab @ ba)[2, 2], np.eye(3), atol=1e-6)
    distractor = _files(truth, "distractor")[0]
    assert distractor["camera"]["pitch_deg"] == 90.0


def test_truth_matches_image_content(footage: tuple[Path, dict]) -> None:
    """Feature registration between two flights and within one clip agrees with the recorded truth."""
    root, truth = footage
    a, b = _still(truth, "2026-08-08"), _still(truth, "2026-09-20")
    img_a, img_b = cv2.imread(str(root / a["path"])), cv2.imread(str(root / b["path"]))
    h_true = synth.relative_homography(truth, a["path"], b["path"])
    assert _grid_error(_sift_homography(img_a, img_b), h_true, b["width"], b["height"]) < 1.5

    video = _files(truth, "overview", "video")[-1]
    t, frame = next((t, fr) for t, fr in iter_frames(root / video["path"]) if t >= 1.0)
    h_true = synth.relative_homography(truth, video["path"], b["path"], t_src=t)
    assert _grid_error(_sift_homography(frame, img_b), h_true, b["width"], b["height"]) < 1.5


def test_visits_differ_but_share_geometry(footage: tuple[Path, dict]) -> None:
    """April 2025 vs September 2026: the site changed completely; the shoreline did not move."""
    root, truth = footage
    a, b = _still(truth, "2025-04-12"), _still(truth, "2026-09-20")
    img_a, img_b = cv2.imread(str(root / a["path"])), cv2.imread(str(root / b["path"]))
    size = (b["width"], b["height"])
    h_ab = synth.relative_homography(truth, a["path"], b["path"])
    warped = cv2.warpPerspective(img_a, h_ab, size)
    valid = cv2.warpPerspective(np.full(img_a.shape[:2], 255, np.uint8), h_ab, size) == 255

    world = synth._layout(np.random.default_rng([7, 1]))
    h_b = synth.truth_homography(truth, b["path"])

    def region(polys: list[np.ndarray], closed: bool, width: int) -> np.ndarray:
        canvas = np.zeros((int(synth.WORLD_M[1]), int(synth.WORLD_M[0])), np.uint8)
        cv2.polylines(canvas, [np.round(p).astype(np.int32) for p in polys], closed, 1, width)
        return (cv2.warpPerspective(canvas, h_b, size, flags=cv2.INTER_NEAREST) > 0) & valid

    site = region([world.work], True, 40)
    diff = np.abs(warped.astype(np.int16) - img_b.astype(np.int16)).mean(axis=2)
    assert diff[site].mean() > 25

    shore = region([world.lake, world.island], True, 9)
    ga, gb = _gradient(warped), _gradient(img_b)
    shifted = cv2.warpAffine(ga, np.float32([[1, 0, 6], [0, 1, 4]]), size)
    aligned = _ncc(ga, gb, shore)
    assert aligned > 0.15 and aligned > _ncc(shifted, gb, shore) + 0.08


def test_reuses_current_footage(footage: tuple[Path, dict]) -> None:
    root, truth = footage
    start = time.perf_counter()
    assert synth.generate_demo_footage(root, fast=True) == root / synth.TRUTH_NAME
    assert time.perf_counter() - start < 2.0
    assert synth.load_truth(root) == truth


# --------------------------------------------------------------------------- #
# The demo project
# --------------------------------------------------------------------------- #


def test_demo_project_validates() -> None:
    project = config.load_project(DEMO)
    assert config.ProjectConfig.model_validate(project.config.model_dump(by_alias=True)) == project.config
    assert project.slug == "demo-lakeside" and project.config.simulated and not project.config.draft
    assert project.config.footage_dir == "footage"
    chapter_types = {
        m.model_fields["type"].default for m in typing.get_args(typing.get_args(config.Chapter)[0])
    }
    assert {ch.type for ch in project.story.chapters} == chapter_types
    assert {v.id for v in project.story.vantages} == {"overview", "shoreline"}
    tokens = set(config.FACT_TOKEN_RE.findall((DEMO / "story.yaml").read_text(encoding="utf-8")))
    assert tokens <= set(project.facts.facts) and all(f.releasable for f in project.facts.facts.values())


def test_brand_files_exist_and_are_clean_svg() -> None:
    brand = config.load_brand(DEMO / "brand" / "brand.yaml")
    refs = [brand.logos.primary, brand.logos.on_dark, brand.logos.mark]
    refs += [r for p in brand.partners for r in (p.logo, p.logo_on_dark)]
    assert len(brand.partners) == 3 and all(refs)
    for ref in refs:
        path = brand.resolve(ref)
        assert path is not None and path.is_file() and path.stat().st_size < 4000
        root = ET.parse(path).getroot()
        tags = {el.tag.split("}")[-1] for el in root.iter()}
        assert root.tag.endswith("svg") and root.get("viewBox") and "title" in tags
        assert not tags & {"text", "image", "use", "script", "foreignObject"}


def test_demo_is_fictional() -> None:
    text = " ".join(
        p.read_text(encoding="utf-8").lower() for p in DEMO.rglob("*") if p.suffix in {".yaml", ".md", ".svg"}
    )
    for name in ("seaworld", "sea world", "geauga", "aurora", "six flags", "cedar fair"):
        assert name not in text


def test_story_matches_generated_footage(footage: tuple[Path, dict]) -> None:
    """Every file the story points at is generated, every dated ref is a flight, and hotspots sit on their features."""
    _, truth = footage
    story = config.load_project(DEMO).story
    paths = {f["path"] for f in synth.truth_files(truth)}
    refs = [v.reference.source for v in story.vantages if v.reference]
    for ch in story.chapters:
        if isinstance(ch, config.HeroChapter) and ch.video:
            refs.append(ch.video.source)
        if isinstance(ch, config.VideoChapter):
            refs.append(ch.clip.source)
    assert refs and set(refs) <= paths

    by_date = {v["date"]: {f["vantage"] for f in v["files"]} for v in truth["visits"]}
    assert {str(c.date) for c in story.captures} == set(by_date)
    dated: list[tuple[str, str]] = []
    for ch in story.chapters:
        if isinstance(ch, config.ScrubChapter | config.CompareChapter):
            dated += [(s.capture, ch.vantage) for s in ch.steps if s.capture]
            dated += [(r, ch.vantage) for h in ch.hotspots for r in (h.from_, h.to) if r]
        if isinstance(ch, config.TimelineChapter):
            dated += [(i.capture, i.vantage or "overview") for i in ch.items if i.capture]
    for ref, vantage in dated:
        if re.fullmatch(r"\d{4}-\d\d-\d\d", ref):
            assert vantage in by_date[ref], (ref, vantage)

    reference = next(v.reference.source for v in story.vantages if v.id == "overview" and v.reference)
    ref_file = next(f for f in synth.truth_files(truth) if f["path"] == reference)
    hom = synth.truth_homography(truth, reference)
    for ch in story.chapters:
        if isinstance(ch, config.ScrubChapter | config.CompareChapter) and ch.vantage == "overview":
            for spot in ch.hotspots:
                x, y = synth.POINTS_OF_INTEREST[spot.id.replace("-", "_")]
                u, v, w = hom @ (x, y, 1.0)
                assert abs(u / w / (ref_file["width"] - 1) - spot.x) < 0.02, spot.id
                assert abs(v / w / (ref_file["height"] - 1) - spot.y) < 0.02, spot.id
