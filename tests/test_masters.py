"""End to end: select → masters on the synthetic project; masters must line up like a locked-off camera."""

from __future__ import annotations

import json

import cv2
import numpy as np
from PIL import Image

from test_select import build_project
from vantage.models import FramePick, MastersIndex
from vantage.process.masters import make_masters
from vantage.process.select import select_frames


def _tile_shifts(a: np.ndarray, b: np.ndarray, n: int = 3) -> np.ndarray:
    """Sub-pixel phase-correlation shift of each tile in an n×n grid (catches rotation/scale/perspective)."""
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    h, w = ga.shape
    th, tw = h // n, w // n
    window = cv2.createHanningWindow((tw, th), cv2.CV_32F)
    shifts = []
    for i in range(n):
        for j in range(n):
            tile = np.s_[i * th : (i + 1) * th, j * tw : (j + 1) * tw]
            (dx, dy), _ = cv2.phaseCorrelate(ga[tile], gb[tile], window)
            shifts.append(np.hypot(dx, dy))
    return np.array(shifts)


def test_make_masters_end_to_end(tmp_path):
    project, catalog, shots = build_project(tmp_path, vantage={"hint": {"lat": 41.3170, "lon": -81.3530}})
    selection = select_frames(project, catalog)
    picks = selection.vantages["overview"].picks
    # A manual pick that cannot register is kept (flagged); an automatic one is dropped.
    picks["2025-08-10"] = FramePick(source=shots["lost"].source.id, date="2025-08-10", manual=True)
    picks["2025-05-01"] = FramePick(source=shots["decoy"].source.id, date="2025-05-01")
    stale = project.masters_dir / "overview" / "2020-01-01.jpg"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"old")

    index = make_masters(project, selection)

    assert MastersIndex.load(project.masters_dir / "index.json") == index
    mv = index.vantages["overview"]
    assert mv.name == "Overview" and mv.width == 960
    assert [c.date for c in mv.captures] == ["2025-06-14", "2025-07-20", "2025-08-10", "2025-09-01"]
    by_date = {c.date: c for c in mv.captures}
    assert by_date["2025-09-01"].align.method == "reference"
    assert by_date["2025-07-20"].source == shots["clip"].source.id  # video frame extracted at full res
    assert by_date["2025-08-10"].align.ok is False
    assert all(
        by_date[d].align.ok and by_date[d].align.method == "homography" for d in ("2025-06-14", "2025-07-20")
    )
    assert not stale.exists()

    masters = {c.date: cv2.imread(str(project.masters_dir / c.file)) for c in mv.captures}
    assert all(m.shape == (mv.height, mv.width, 3) for m in masters.values())
    with Image.open(project.masters_dir / mv.captures[0].file) as im:
        assert im.info.get("progressive") and im.info.get("icc_profile")

    ref = masters["2025-09-01"]
    for date in ("2025-06-14", "2025-07-20"):
        shifts = _tile_shifts(ref, masters[date])
        assert shifts.max() < 0.3, (date, shifts)
        diff = np.abs(
            cv2.GaussianBlur(ref, (5, 5), 0).astype(int) - cv2.GaussianBlur(masters[date], (5, 5), 0)
        )
        assert diff.mean() < 6, (date, diff.mean())  # graded to the reference and on the same pixels

    review_dir = project.work_dir / "review" / "overview"
    review = json.loads((review_dir / "review.json").read_text())
    assert 0.5 < review["crop"]["retained"] < 1.0
    rows = {c["date"]: c for c in review["captures"]}
    assert rows["2025-05-01"]["included"] is False
    assert rows["2025-05-01"]["align"]["note"].startswith("excluded")
    assert rows["2025-08-10"]["included"] is True and rows["2025-08-10"]["manual"] is True
    assert (review_dir / "contact.jpg").exists()
    assert sorted(p.name for p in review_dir.glob("20*.jpg")) == [f"{d}.jpg" for d in by_date]
