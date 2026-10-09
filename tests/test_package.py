"""Packaging: self-contained single files, the lite budget, a deterministic offline zip, checksums."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from PIL import Image, PngImagePlugin

from test_build import FACTS, make_project
from vantage.config import load_project
from vantage.media import ffmpeg
from vantage.site import images
from vantage.site.build import ReleaseError, build_site
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
    markup = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
    assert "<source" not in markup  # one JPEG per image; videos come from embedded blobs
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


def test_package_reads_only_files_inside_the_site(packaged, tmp_path):
    """A tampered theme can't pull a local file from outside the site into the single files."""
    project, dist, _ = packaged
    shutil.copytree(dist / "site", tmp_path / "dist" / "site")
    secret = tmp_path / "dist" / "secret.txt"
    secret.write_text("aws_secret_access_key = not-for-email")
    index = tmp_path / "dist" / "site" / "index.html"
    page = index.read_text(encoding="utf-8")
    index.write_text(page.replace(":root{", ':root{--x:url("assets/../../secret.txt");', 1), encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        package_project(project, tmp_path / "dist")
    leaked = base64.b64encode(secret.read_bytes()).decode()
    for html in (tmp_path / "dist").glob("*.html"):
        assert leaked not in html.read_text(encoding="utf-8")


def test_release_package_is_gated(tmp_path):
    root = make_project(tmp_path / "tiny")
    with pytest.raises(ReleaseError, match="draft: true"):
        package_project(load_project(root), tmp_path / "dist", release=True)
    assert not (tmp_path / "dist").exists()


def test_release_package_never_ships_an_earlier_draft_site(tmp_path):
    """A site left in dist by a draft build (Preview badge, unverified facts) is rebuilt for a release
    from the YAML that passed the gates, never packaged as is under a release label."""
    root = make_project(tmp_path / "tiny")
    dist = tmp_path / "dist"
    build_site(load_project(root), dist / "site")
    config = yaml.safe_load((root / "project.yaml").read_text())
    (root / "project.yaml").write_text(yaml.safe_dump(config | {"draft": False}))
    facts = {fid: fact | {"status": "verified"} for fid, fact in FACTS["facts"].items()}
    (root / "facts.yaml").write_text(yaml.safe_dump({"facts": facts}))
    files = package_project(load_project(root), dist, release=True)
    info = json.loads(files["build_json"].read_text())
    assert info["release"] is True and info["draft"] is False and info["unverifiedFacts"] == 0
    unverified = re.compile(r'<(?:span class="v-fact"|li id="v-note-\d+")[^>]*data-releasable="false"')
    site = (dist / "site" / "index.html").read_text()
    for html in (site, files["single_file"].read_text(), files["lite"].read_text()):
        story = read_story(html)
        assert story["meta"]["draft"] is False and all(n["releasable"] for n in story["notes"])
        assert '<span class="v-badge">Preview</span>' not in html and not unverified.search(html)


@pytest.mark.parametrize("edition", ["single_file", "lite"])
def test_pictures_shown_again_reuse_or_shrink_their_bytes(packaged, edition):
    _, _, files = packaged
    html = files[edition].read_text()
    imgs = re.findall(
        r'<img src="(data:image/jpeg;base64,[^"]+)"( data-asset="[^"]+")?[^>]*? alt="([^"]*)"', html
    )
    carriers = [(uri, alt) for uri, carrier, alt in imgs if carrier]
    assert len(carriers) == len({alt for _, alt in carriers})  # one carrier per picture
    by_alt = {alt: uri for uri, alt in carriers}
    sections = {
        kind: body
        for kind, body in re.findall(r'<section class="v-chapter v-(\w+)(.*?)</section>', html, flags=re.S)
    }
    # the explore grid repeats pictures as small copies of their own...
    grid = re.findall(
        r'<img src="(data:image/jpeg;base64,[^"]+)" width[^>]*? alt="([^"]*)"', sections["explore"]
    )
    assert grid
    for uri, alt in grid:
        assert uri != by_alt[alt]
        with Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) as im:
            assert im.width <= 720
    # ...while the compare pair, which the runtime moves into its stage, keeps the full picture
    pair = re.findall(r'<img src="(data:image/jpeg;base64,[^"]+)"[^>]*? alt="([^"]*)"', sections["compare"])
    assert len(pair) == 2 and all(uri == by_alt[alt] for uri, alt in pair)


# --------------------------------------------------------------------------- #
# Metadata: nothing but pixels (and a colour profile) is published
# --------------------------------------------------------------------------- #

LEAKS = re.compile(rb"Jane Q\. Designer|/Users/jane/|Lav[cf]\d+\.\d+")  # author, local path, ffmpeg tag
META_KEYS = {"exif", "xmp", "comment", "photoshop", "XML:com.adobe.xmp", "Author"}


def _tagged(im: Image.Image, path: Path, *, orientation: int | None = None) -> None:
    """Save `im` the way a camera, scanner or design tool would: author, file path, history, notes."""
    exif = Image.Exif()
    exif[0x013B] = "Jane Q. Designer"  # Artist
    exif[0x010E] = "/Users/jane/Clients/brand/logo-final-v7.ai"  # ImageDescription
    if orientation:
        exif[0x0112] = orientation
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><dc:creator>Jane Q. Designer</dc:creator></x:xmpmeta>'
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".png":
        text = PngImagePlugin.PngInfo()
        text.add_text("Author", "Jane Q. Designer")
        text.add_itxt("XML:com.adobe.xmp", xmp.decode())
        im.save(path, pnginfo=text, exif=exif.tobytes())
    else:
        im.save(path, comment=b"Scanned at /Users/jane/scans", exif=exif.tobytes(), xmp=xmp, quality=90)


def _payloads(html: str) -> list[bytes]:
    """Every raster image the single file carries: data: URIs and embedded asset blocks."""
    uris = re.findall(r"data:image/(?!svg)[\w.-]+;base64,([A-Za-z0-9+/=]+)", html)
    blocks = re.findall(r'data-type="image/(?!svg)[^"]+">([A-Za-z0-9+/=\s]+)</script>', html)
    return [base64.b64decode(b) for b in uris + blocks]


def _no_metadata(name: str, data: bytes) -> None:
    assert not LEAKS.search(data), f"{name}: {LEAKS.search(data)}"
    with Image.open(io.BytesIO(data)) as im:
        assert not META_KEYS & set(im.info) and not getattr(im, "text", None), f"{name}: {set(im.info)}"


def test_published_images_carry_no_metadata(tmp_path, capsys):
    root = tmp_path / "tiny"
    _with_clip(root)  # the video poster comes from ffmpeg, which tags the JPEGs it writes
    master = root / "masters" / "overview" / "2025-04-12.jpg"
    with Image.open(master) as im:
        _tagged(im.convert("RGB"), master)
    logo = Image.new("RGBA", (60, 20), (0, 0, 0, 0))
    logo.paste((200, 40, 40, 255), (5, 5, 55, 15))
    _tagged(logo, root / "brand" / "logo.png")
    upright = Image.new("RGB", (20, 40), (20, 90, 160))
    upright.paste((250, 250, 250), (0, 0, 20, 10))  # the top band, once turned upright
    _tagged(
        upright.transpose(Image.Transpose.ROTATE_90), root / "brand" / "partners" / "city.jpg", orientation=6
    )
    brand = root / "brand" / "brand.yaml"
    brand.write_text(
        brand.read_text().replace("primary: logo.svg", "primary: logo.png").replace("city.svg", "city.jpg")
    )
    dist = tmp_path / "dist"
    files = package_project(load_project(root), dist)
    err = capsys.readouterr().err
    assert "logo 'logo.png': removed EXIF, XMP, text chunks" in err
    assert "logo 'partners/city.jpg': removed EXIF, XMP, comment" in err

    site = dist / "site"
    published = sorted(p for p in site.rglob("*") if p.suffix in {".jpg", ".png", ".webp", ".avif"})
    assert {"assets/brand/logo.png", "assets/brand/partners/city.jpg"} <= {
        p.relative_to(site).as_posix() for p in published
    }
    assert any("/video/" in p.as_posix() for p in published)
    for path in published:
        _no_metadata(path.relative_to(site).as_posix(), path.read_bytes())
    for edition in ("single_file", "lite"):
        for i, data in enumerate(_payloads(files[edition].read_text())):
            _no_metadata(f"{edition} image {i}", data)
    with zipfile.ZipFile(files["zip"]) as zf:
        for name in zf.namelist():
            if name.endswith((".jpg", ".png", ".webp", ".avif")):
                _no_metadata(name, zf.read(name))

    with Image.open(site / "assets/brand/logo.png") as im:  # PNG stays lossless, alpha and all
        assert im.mode == "RGBA" and np.array_equal(np.asarray(im), np.asarray(logo))
    with Image.open(site / "assets/brand/partners/city.jpg") as im:  # EXIF orientation is applied
        assert im.size == (20, 40) and np.asarray(im.convert("L"))[:8].mean() > 200
    frame = ffmpeg.extract_frame(root / "footage" / "2025-04-12" / "clip.mp4", 0.2, tmp_path / "f.jpg")
    assert not LEAKS.search(frame.read_bytes())  # bitexact: no encoder version in the bytes


@pytest.mark.parametrize(
    ("fmt", "options", "exact"),
    [
        ("GIF", {"comment": b"Jane Q. Designer", "transparency": 0}, True),
        ("WEBP", {"lossless": True, "xmp": b"<x>/Users/jane/logo.ai</x>"}, True),
        ("AVIF", {"quality": 90, "xmp": b"<x>/Users/jane/logo.ai</x>"}, False),
    ],
)
def test_raster_logos_keep_pixels_and_lose_metadata(tmp_path, fmt, options, exact):
    frames = [Image.new("RGB", (48, 24), color) for color in ((200, 40, 40), (40, 40, 200))]
    for im in frames:
        im.paste((250, 250, 250), (8, 8, 40, 16))
    if fmt == "GIF":
        frames = [im.convert("P", palette=Image.Palette.ADAPTIVE, colors=8) for im in frames]
    path = tmp_path / f"logo.{fmt.lower()}"
    frames[0].save(path, fmt, save_all=True, append_images=frames[1:], duration=200, loop=0, **options)

    data, removed = images.strip_raster(path)
    assert images.EXTRA_FRAMES in removed and len(removed) == 2  # and the comment or XMP
    _no_metadata(path.name, data)
    with Image.open(io.BytesIO(data)) as out, Image.open(path) as src:
        assert out.format == fmt and getattr(out, "n_frames", 1) == 1  # a logo does not move
        got, want = (np.asarray(i.convert("RGBA"), dtype=np.int16) for i in (out, src))
        assert np.array_equal(got, want) if exact else np.abs(got - want).mean() < 3
    assert images.strip_raster(path)[0] == data  # deterministic
