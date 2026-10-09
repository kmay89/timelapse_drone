"""Frame selection on a tiny synthetic project (photos, one video, decoys and a GPS twin).

`build_project` is shared with test_masters.py.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
import yaml
from PIL import Image, ImageCms

from test_align import SIZE, make_world, random_homography, reference_view, render_view
from vantage.config import Project, load_project
from vantage.media.ffmpeg import FrameWriter
from vantage.models import Candidate, Catalog, Selection, Source
from vantage.process.select import haversine_m, load_frame, select_frames

SITE = (41.3170, -81.3530)  # the vantage hint
FAR = (41.3260, -81.3530)  # ~1 km north of it
CANDIDATE_WIDTH = 800


@dataclass
class Shot:
    """One footage file of the fake project and its ground truth."""

    path: str  # relative to footage/
    img: np.ndarray  # full-resolution BGR content
    sharpness: float
    H: np.ndarray | None  # reference → this view; None for unrelated frames
    kind: str = "photo"
    gps: tuple[float, float] | None = None
    ts: tuple[float, ...] = (0.0,)
    source: Source | None = field(default=None, repr=False)

    @property
    def date(self) -> str:
        return self.path.split("/")[0]


def _shots() -> dict[str, Shot]:
    world, other = make_world(seed=3), make_world(seed=99)
    rng = np.random.default_rng(42)
    h_june, h_july = random_homography(rng), random_homography(rng)
    july = render_view(world, h_july, rng)
    unrelated = [render_view(other, random_homography(rng)) for _ in range(3)]
    return {
        "ref": Shot("2025-09-01/DJI_0100.JPG", reference_view(world), 900.0, np.eye(3), gps=SITE),
        "side": Shot("2025-09-01/DJI_0101.JPG", unrelated[0], 500.0, None, gps=SITE),
        "june": Shot("2025-06-14/DJI_0007.JPG", render_view(world, h_june, rng), 400.0, h_june, gps=SITE),
        "decoy": Shot("2025-06-14/DJI_0008.JPG", unrelated[1], 950.0, None, gps=SITE),
        "clip": Shot("2025-07-20/DJI_0042.MP4", july, 600.0, h_july, kind="video", gps=SITE, ts=(0.5, 1.5)),
        "twin": Shot("2025-07-20/DJI_0043.JPG", july, 990.0, h_july, gps=FAR),
        "lost": Shot("2025-08-10/DJI_0200.JPG", unrelated[2], 700.0, None),
    }


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def build_project(
    root: Path, *, vantage: dict[str, Any] | None = None, captures: list[dict[str, Any]] | None = None
) -> tuple[Project, Catalog, dict[str, Shot]]:
    """Write project.yaml/story.yaml/brand, footage, candidate JPEGs and work/catalog.json."""
    _write_yaml(
        root / "project.yaml",
        {
            "slug": "test-site",
            "title": "Test Site",
            "align": {"max_features": 4000},
            "grade": {"mode": "reinhard", "strength": 1.0},
            "output": {"images": {"master_width": 960}},
        },
    )
    _write_yaml(
        root / "story.yaml",
        {
            "vantages": [{"id": "overview", "name": "Overview", **(vantage or {})}],
            "captures": captures or [],
            "chapters": [{"type": "text", "title": "Hello", "body": "World"}],
        },
    )
    _write_yaml(root / "brand" / "brand.yaml", {"name": "Test Brand"})

    shots = _shots()
    catalog = Catalog()
    for shot in shots.values():
        path = root / "footage" / shot.path
        path.parent.mkdir(parents=True, exist_ok=True)
        if shot.kind == "video":
            with FrameWriter(path, SIZE[0], SIZE[1], fps=5, crf=14, preset="veryfast") as writer:
                for _ in range(10):
                    writer.write(shot.img)
        else:
            cv2.imwrite(str(path), shot.img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        size = path.stat().st_size
        sid = hashlib.sha1(f"{shot.path}{size}".encode()).hexdigest()[:8]
        photo_gps = shot.gps if shot.kind == "photo" and shot.gps else (None, None)
        shot.source = Source(
            id=sid, path=shot.path, kind=shot.kind, date=shot.date, date_source="folder",
            width=SIZE[0], height=SIZE[1], duration_s=2.0 if shot.kind == "video" else 0.0,
            fps=5.0 if shot.kind == "video" else 0.0, lat=photo_gps[0], lon=photo_gps[1], size_bytes=size,
        )  # fmt: skip
        catalog.sources.append(shot.source)
        thumb = cv2.resize(
            shot.img, (CANDIDATE_WIDTH, CANDIDATE_WIDTH * SIZE[1] // SIZE[0]), interpolation=cv2.INTER_AREA
        )
        video_gps = shot.gps if shot.kind == "video" and shot.gps else (None, None)
        for i, t in enumerate(shot.ts):
            rel = f"candidates/{sid}/{t:08.3f}.jpg"
            (root / "work" / rel).parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(root / "work" / rel), thumb, [cv2.IMWRITE_JPEG_QUALITY, 92])
            catalog.candidates.append(
                Candidate(
                    source=sid,
                    t=t,
                    file=rel,
                    sharpness=shot.sharpness + i,
                    lat=video_gps[0],
                    lon=video_gps[1],
                )
            )
    catalog.save(root / "work" / "catalog.json")
    return load_project(root), catalog, shots


def _picked(selection: Selection, shots: dict[str, Shot]) -> dict[str, str]:
    """date → shot name of the pick."""
    by_id = {s.source.id: name for name, s in shots.items() if s.source}
    return {date: by_id[p.source] for date, p in selection.vantages["overview"].picks.items()}


def test_selects_registered_frames_not_sharper_decoys(tmp_path):
    project, catalog, shots = build_project(tmp_path)
    selection = select_frames(project, catalog)

    vs = selection.vantages["overview"]
    assert vs.reference.source == shots["ref"].source.id and vs.reference.date == "2025-09-01"
    # Decoys are sharper but don't register; the twin ties the clip visually and wins on sharpness;
    # 2025-08-10 has nothing that matches, so that visit is skipped.
    assert _picked(selection, shots) == {"2025-06-14": "june", "2025-07-20": "twin", "2025-09-01": "ref"}
    june = vs.picks["2025-06-14"]
    assert 0.3 < june.score < 1.0 and june.inliers >= 20 and not june.manual
    assert Selection.load(project.work_dir / "selection.json") == selection


def test_gps_hint_prefilters_far_frames(tmp_path):
    project, catalog, shots = build_project(tmp_path, vantage={"hint": {"lat": SITE[0], "lon": SITE[1]}})
    selection = select_frames(project, catalog)
    pick = selection.vantages["overview"].picks["2025-07-20"]
    assert pick.source == shots["clip"].source.id
    assert pick.t == 1.5  # the sharper of the clip's two candidates


def test_manual_reference_picks_and_exclusions(tmp_path):
    project, _, shots = build_project(
        tmp_path,
        vantage={
            "reference": {"source": "2025-07-20/DJI_0042.MP4", "t": 1.2},
            "picks": {"2025-08-10": {"source": "2025-08-10/DJI_0200.JPG"}},
        },
        captures=[{"date": "2025-06-14", "exclude": True}],
    )
    selection = select_frames(project)  # loads work/catalog.json

    vs = selection.vantages["overview"]
    assert (vs.reference.source, vs.reference.t, vs.reference.date) == (
        shots["clip"].source.id,
        1.2,
        "2025-07-20",
    )
    assert vs.reference.manual
    assert (project.work_dir / "frames").is_dir()  # no candidate near t=1.2, so the exact frame was extracted
    assert set(vs.picks) == {"2025-07-20", "2025-08-10", "2025-09-01"}
    assert vs.picks["2025-07-20"] == vs.reference
    assert vs.picks["2025-08-10"].manual and vs.picks["2025-08-10"].source == shots["lost"].source.id
    assert _picked(selection, shots)["2025-09-01"] == "ref"


def test_unknown_reference_path_is_a_clear_error(tmp_path):
    project, catalog, _ = build_project(tmp_path, vantage={"reference": {"source": "nope/IMG_1.JPG"}})
    with pytest.raises(ValueError, match="not in the catalog"):
        select_frames(project, catalog)


def test_load_frame_honours_exif_orientation_and_icc(tmp_path):
    project, catalog, shots = build_project(tmp_path)
    img = shots["june"].img[:200, :300]
    rgb = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise for display
    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    rel = "2025-06-14/rotated.jpg"
    rgb.save(tmp_path / "footage" / rel, quality=98, exif=exif, icc_profile=srgb)
    src = catalog.sources[0].model_copy(update={"path": rel, "kind": "photo"})
    out = load_frame(project, src)
    assert out.shape == (300, 200, 3)
    expected = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    assert np.abs(out.astype(int) - expected).mean() < 3


def test_haversine():
    assert haversine_m(*SITE, *SITE) == 0
    assert abs(haversine_m(*SITE, *FAR) - 1000.8) < 2
    assert abs(haversine_m(0, 0, 1, 0) - 111_195) < 10
