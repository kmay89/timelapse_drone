"""Packaging: self-contained single files, the lite budget, a deterministic offline zip, checksums."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest

from test_build import make_project
from vantage.config import load_project
from vantage.media import ffmpeg
from vantage.site.build import ReleaseError
from vantage.site.package import package_project, read_story

ASSET_REFS = re.compile(r"""(?:\b(?:src|href|poster)=["']|url\(\s*["']?)([^"')\s]+)""")


def _external(html: str) -> list[str]:
    """Every src=/href=/poster=/url( target that is not data:, #fragment or a web link."""
    return sorted(
        {ref for ref in ASSET_REFS.findall(html) if not ref.startswith(("data:", "#", "https:", "http:"))}
    )


def _with_clip(root: Path) -> None:
    story = {
        "vantages": [{"id": "overview", "name": "Overview"}, {"id": "side", "name": "Side"}],
        "chapters": [
            {"type": "hero", "id": "open", "vantage": "overview"},
            {
                "type": "scrub",
                "id": "scrub",
                "vantage": "overview",
                "steps": [{"capture": "#1", "text": "One."}],
            },
            {"type": "compare", "id": "cmp", "vantage": "overview"},
            {"type": "video", "id": "film", "clip": {"source": "2025-04-12/clip.mp4"}, "clip_s": 0.5},
            {"type": "explore", "id": "explore"},
            {
                "type": "credits",
                "id": "credits",
                "sources": [{"label": "Plan", "url": "https://example.org"}],
            },
        ],
    }
    make_project(root, story=story, facts={"facts": {}}, dek="A dek.")
    clip = root / "footage" / "2025-04-12" / "clip.mp4"
    clip.parent.mkdir(parents=True)
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=1", "-pix_fmt", "yuv420p", clip])


@pytest.fixture(scope="module")
def packaged(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Path, dict[str, Path]]:
    root = tmp_path_factory.mktemp("pkg") / "tiny"
    _with_clip(root)
    project = load_project(root)
    dist = root.parent / "dist"
    return project, dist, package_project(project, dist)  # builds the site first


def test_outputs(packaged):
    _, dist, files = packaged
    assert set(files) == {"single_file", "lite", "zip", "how_to_view", "build_json", "checksums"}
    assert files["single_file"].name == "tiny.html" and files["lite"].name == "tiny-lite.html"
    assert (dist / "site" / "index.html").is_file()
    how_to = files["how_to_view"].read_text()
    assert "Add to Home Screen" in how_to and "tiny-offline.zip" in how_to and "tiny-lite.html" in how_to
    sums = dict(reversed(line.split("  ")) for line in files["checksums"].read_text().splitlines())
    assert set(sums) == {"HOW-TO-VIEW.txt", "build.json", "tiny-lite.html", "tiny-offline.zip", "tiny.html"}
    for name, digest in sums.items():
        assert hashlib.sha256((dist / name).read_bytes()).hexdigest() == digest
    info = json.loads(files["build_json"].read_text())
    assert info["release"] is False and info["draft"] is True and info["unverifiedFacts"] == 0
    assert info["files"]["tiny.html"]["bytes"] == files["single_file"].stat().st_size


@pytest.mark.parametrize("edition", ["single_file", "lite"])
def test_single_files_are_self_contained(packaged, edition):
    _, _, files = packaged
    html = files[edition].read_text()
    assert _external(html) == []
    assert 'rel="manifest"' not in html and "data-sw=" not in html and 'rel="icon"' not in html
    assert "<source" not in html  # one JPEG per image; videos come from embedded blobs
    story = read_story(html)
    assert story["meta"].get("shareImage") is None or story["meta"]["shareImage"].startswith("http")
    # every asset path the StoryJSON uses has its bytes in the document exactly once
    refs = set(re.findall(r'"(assets/[^"]+)"', json.dumps(story)))
    images = set(re.findall(r'<img [^>]*?src="data:[^"]+" data-asset="([^"]+)"', html))
    blocks = re.findall(r'<script type="application/octet-stream" data-asset="([^"]+)"', html)
    assert refs == images | set(blocks)
    assert len(blocks) == len(set(blocks)) and not images & set(blocks)
    assert re.search(r'url\("data:font/woff2;base64,', html)


def test_single_file_embeds_h264_only(packaged):
    _, _, files = packaged
    html = files["single_file"].read_text()
    story = read_story(html)
    film = next(c for c in story["chapters"] if c["id"] == "film")
    assert film["video"]["sources"] == [{"src": "assets/video/film.mp4", "type": "video/mp4"}]
    blob = re.search(r'data-asset="assets/video/film.mp4" data-type="video/mp4">([^<]+)</script>', html)
    assert blob and base64.b64decode(blob.group(1))[4:8] == b"ftyp"
    for img in (c["img"] for v in story["vantages"] for c in v["captures"]):
        assert [s["type"] for s in img["sources"]] == ["image/jpeg"] and img["fallback"].endswith(".jpg")


def test_lite_keeps_only_the_essential_images(packaged):
    _, _, files = packaged
    story = read_story(files["lite"].read_text())
    overview = story["vantages"][0]["captures"]
    sharp = [i for i, c in enumerate(overview) if c["img"]["sources"]]
    assert sharp == [0, 1, len(overview) - 1]  # compare before, the scrub step, hero + compare after
    assert all(c["img"]["fallback"] == c["img"]["lqip"] for c in story["vantages"][1]["captures"])
    assert all(re.search(r"-\d+q70\.jpg$", overview[i]["img"]["fallback"]) for i in sharp)
    film = next(c for c in story["chapters"] if c["id"] == "film")
    assert film["video"]["sources"] == [] and film["video"]["poster"]["sources"]
    assert "video" not in story["chapters"][0]
    html = files["lite"].read_text()
    assert "Also flown:" in html and "assets/video/" not in html


def test_lite_budget_degrades_quality(tmp_path):
    root = tmp_path / "tiny"
    _with_clip(root)
    project = load_project(root)
    loose = package_project(project, tmp_path / "a")["lite"].stat().st_size
    project.config.output.lite_max_mb = (loose - 2000) / 2**20  # just too big at 1080 px q70
    tight = package_project(project, tmp_path / "a")["lite"]
    assert tight.stat().st_size <= project.config.output.lite_max_mb * 2**20
    assert re.search(r"-\d+q64\.jpg", tight.read_text())


def test_offline_zip_is_deterministic(packaged, tmp_path, monkeypatch):
    project, dist, files = packaged
    first = hashlib.sha256(files["zip"].read_bytes()).hexdigest()
    again = package_project(project, dist)["zip"]
    assert hashlib.sha256(again.read_bytes()).hexdigest() == first
    with zipfile.ZipFile(again) as zf:
        names = zf.namelist()
        assert names == sorted(names) and all(n.startswith("tiny/") for n in names)
        assert (
            "tiny/index.html" in names
            and "tiny/sw.js" not in names
            and "tiny/manifest.webmanifest" not in names
        )
        assert {i.date_time for i in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        index = zf.read("tiny/index.html").decode()
        assert "data-sw=" not in index and 'data-edition="offline"' in index
        missing = [r for r in _external(index) if f"tiny/{r}" not in names]
        assert missing == []
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1767225600")  # 2026-01-01
    with zipfile.ZipFile(package_project(project, dist)["zip"]) as zf:
        assert zf.infolist()[0].date_time == (2026, 1, 1, 0, 0, 0)


def test_release_package_is_gated(tmp_path):
    root = make_project(tmp_path / "tiny")
    with pytest.raises(ReleaseError, match="draft: true"):
        package_project(load_project(root), tmp_path / "dist", release=True)
    assert not (tmp_path / "dist").exists()
