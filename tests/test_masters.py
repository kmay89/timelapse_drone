"""End to end: select → masters on the synthetic project; masters must line up like a locked-off camera."""

from __future__ import annotations

import datetime as dt
import json

import cv2
import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError

from test_align import WORLD, make_world, random_homography, reference_view, render_view
from test_select import SITE, Shot, build_project
from vantage.config import Vantage
from vantage.models import FramePick, MastersIndex, Selection, VantageSelection
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
    project, catalog, shots = build_project(tmp_path, vantage={"hint": {"lat": SITE[0], "lon": SITE[1]}})
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


def test_visit_unlike_the_reference_is_chained_through_its_neighbour(tmp_path):
    """Before → during → after: 'before' shares nothing with the reference, half its frame with 'during'.

    Also crops to a portrait aspect, as a phone-first story might.
    """
    before, after = make_world(seed=3), make_world(seed=5)
    during = np.hstack([before[:, : WORLD[0] // 2], after[:, WORLD[0] // 2 :]])
    rng = np.random.default_rng(8)
    h_before, h_during = random_homography(rng), random_homography(rng)
    shots = {
        "after": Shot("2025-09-01/DJI_0300.JPG", reference_view(after), 900.0, np.eye(3)),
        "during": Shot("2025-06-01/DJI_0200.JPG", render_view(during, h_during, rng), 800.0, h_during),
        "before": Shot("2025-03-01/DJI_0100.JPG", render_view(before, h_before, rng), 700.0, h_before),
    }
    config = {"align": {"max_features": 4000, "ecc_refine": False, "output_aspect": 0.8}}
    project, catalog, _ = build_project(tmp_path, shots=shots, config=config)
    selection = select_frames(project, catalog)
    assert sorted(selection.vantages["overview"].picks) == ["2025-03-01", "2025-06-01", "2025-09-01"]

    mv = make_masters(project, selection).vantages["overview"]
    by_date = {c.date: c for c in mv.captures}
    assert list(by_date) == ["2025-03-01", "2025-06-01", "2025-09-01"]
    assert by_date["2025-03-01"].align.ok
    assert "chained via 2025-06-01" in (by_date["2025-03-01"].align.note or "")
    assert abs(mv.width / mv.height - 0.8) < 0.01

    crop = json.loads((project.work_dir / "review" / "overview" / "review.json").read_text())["crop"]
    x, y, w, h = (crop[k] for k in "xywh")
    # 'before' is fitted on the half it shares with 'during', so its far side is extrapolated.
    for date, world, tolerance in (("2025-03-01", before, 1.0), ("2025-06-01", during, 0.3)):
        truth = cv2.resize(
            reference_view(world)[y : y + h, x : x + w], (mv.width, mv.height), interpolation=cv2.INTER_AREA
        )
        master = cv2.imread(str(project.masters_dir / by_date[date].file))
        shifts = _tile_shifts(truth, master)
        assert shifts.max() < tolerance, (date, shifts)


@pytest.mark.parametrize(
    "vid", ["/tmp/elsewhere", "../../footage/2025-06-14", "a/b", "..", "Overview", "", "-x"]
)
def test_vantage_ids_are_plain_folder_names(vid):
    with pytest.raises(ValidationError, match="vantage id"):
        Vantage.model_validate({"id": vid, "name": "V"})
    assert Vantage.model_validate({"id": "north_2-b", "name": "V"}).id == "north_2-b"


def test_pick_keys_are_flight_dates():
    ref = {"source": "2025-06-14/DJI_0007.JPG"}
    for key in ("../../victim/x", "/tmp/x", "2025-6-14", "2025-02-30", "latest"):
        with pytest.raises(ValidationError, match="pick key"):
            Vantage.model_validate({"id": "v", "name": "V", "picks": {key: ref}})
    # Quoted or not in the YAML (an unquoted key loads as a date), the key is 'YYYY-MM-DD'.
    v = Vantage.model_validate(
        {"id": "v", "name": "V", "picks": {dt.date(2025, 6, 14): ref, "2025-07-02": ref}}
    )
    assert list(v.picks) == ["2025-06-14", "2025-07-02"]


def test_process_never_writes_or_clears_files_outside_the_project(tmp_path):
    """story.yaml and work/selection.json cannot steer masters/ or work/review/ writes and deletes elsewhere."""
    victim = tmp_path / "victim"
    victim.mkdir()
    for name in ("holiday-1.jpg", "holiday-2.jpg"):
        (victim / name).write_bytes(b"keep me")
    shots = {"ref": Shot("2025-09-01/DJI_0100.JPG", reference_view(make_world(seed=3)), 900.0, np.eye(3))}

    with pytest.raises(ValidationError, match="vantage id"):
        build_project(tmp_path / "abs", shots=shots, vantage={"id": str(victim)})
    with pytest.raises(ValidationError, match="pick key"):
        build_project(
            tmp_path / "rel", shots=shots, vantage={"picks": {"../../../victim/x": {"source": "a.jpg"}}}
        )

    # A hand-edited selection.json, or a story changed in memory, is refused before anything is touched.
    project, _, shots = build_project(tmp_path / "ok", shots=shots)
    pick = FramePick(source=shots["ref"].source.id, date="2025-09-01")
    tampered = Selection(
        vantages={"overview": VantageSelection(reference=pick, picks={"../../../victim/x": pick})}
    )
    with pytest.raises(ValueError, match="not YYYY-MM-DD dates"):
        make_masters(project, tampered)
    project.story.vantages[0].id = str(victim)
    with pytest.raises(ValueError, match="not a plain folder name"):
        make_masters(project, Selection(vantages={str(victim): VantageSelection(reference=pick, picks={})}))
    assert sorted(p.name for p in victim.iterdir()) == ["holiday-1.jpg", "holiday-2.jpg"]
    assert not project.masters_dir.exists() and not (project.work_dir / "review").exists()


@pytest.mark.parametrize(
    "file", ["../../secret.jpg", "/etc/secret.jpg", "overview/../../x.jpg", "a\\..\\b.jpg", ""]
)
def test_index_json_files_stay_inside_masters(file):
    """build, film and caption open masters/<file> and publish it."""
    capture = {"date": "2025-06-14", "file": file, "source": "s", "align": {"method": "reference"}}
    index = {"vantages": {"overview": {"name": "O", "width": 4, "height": 3, "captures": [capture]}}}
    with pytest.raises(ValidationError, match="inside masters/"):
        MastersIndex.model_validate(index)
    capture["file"] = "overview/2025-06-14.jpg"
    assert MastersIndex.model_validate(index).vantages["overview"].captures[0].file == capture["file"]
