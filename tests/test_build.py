"""Story compiler: capture refs, labels, StoryJSON shape, assets on disk, video clips, release gate."""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import yaml
from PIL import Image

from vantage.config import load_project
from vantage.media import ffmpeg
from vantage.site import build, images
from vantage.site.build import ReleaseError, build_site, resolve_capture_ref
from vantage.site.package import read_story
from vantage.site.render import capture_label

DATES = {
    "overview": ["2025-04-12", "2025-06-01", "2025-06-20", "2025-09-01", "2026-01-15"],
    "side": ["2025-06-01", "2025-09-01"],
}
MASTER_SIZE = (480, 300)


def _master(path: Path, seed: int) -> None:
    rng = np.random.default_rng(seed)
    w, h = MASTER_SIZE
    yy, xx = np.mgrid[0:h, 0:w]
    base = np.stack([60 + xx * 0.3, 90 + yy * 0.3, 70 + 20 * np.sin(xx / 17.0)], axis=-1)
    base[60 : 60 + 40 * (seed % 4 + 1), 100:260] = rng.integers(80, 220, 3)
    img = np.clip(base + rng.normal(0, 6, base.shape), 0, 255).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(path, quality=90)


def _logo(path: Path, fill: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 32"><rect width="120" height="32" fill="{fill}"/></svg>'
    )


STORY: dict[str, Any] = {
    "vantages": [
        {"id": "overview", "name": "Overview", "portrait_focus": [0.4, 0.6]},
        {"id": "side", "name": "From the side"},
    ],
    "captures": [
        {"date": "2025-04-12", "label": "Before it began", "note": "The old lot."},
        {"date": "2025-06-20", "exclude": True},
    ],
    "chapters": [
        {"type": "hero", "id": "open", "body": "It took {fact:months} to change.", "vantage": "overview"},
        {
            "type": "text",
            "id": "story",
            "title": "A <b>bold</b> start",
            "body": "First paragraph with {fact:cost} and a [link](https://example.org).\n\n"
            "<script>alert(1)</script> raw HTML stays text. Opened {fact:opened}.",
            "pull_quote": 'It\'s "different" now </script><script>alert(2)</script>',
            "attribution": "A neighbor",
        },
        {
            "type": "scrub",
            "id": "scrub",
            "vantage": "overview",
            "steps": [
                {"capture": "2025-05-15", "text": "Nearest before: April."},
                {"text": "A detail.", "focus": [0.5, 0.5, 2]},
                {"capture": "latest", "text": "Now, again {fact:cost}."},
            ],
            "hotspots": [{"id": "pin", "x": 0.3, "y": 0.4, "label": "Here", "body": "Body", "from": "#1"}],
        },
        {"type": "compare", "id": "cmp", "vantage": "overview", "before": "earliest", "after": "#3"},
        {"type": "stats", "items": [{"value": "{fact:acres}", "unit": "acres", "label": "Site"}]},
        {
            "type": "timeline",
            "id": "tl",
            "items": [
                {"date": "1970", "title": "Opened", "status": "done"},
                {
                    "date": "Now",
                    "title": "Rebuilt",
                    "capture": "latest",
                    "vantage": "side",
                    "status": "in-progress",
                },
            ],
        },
        {
            "type": "gallery",
            "id": "archive",
            "vantage": "side",
            "images": [
                {
                    "file": "archive/postcard.jpg",
                    "caption": "A *postcard*",
                    "date": "c. 1950",
                    "credit": "Archive",
                }
            ],
        },
        {"type": "explore", "id": "explore"},
        {
            "type": "credits",
            "id": "credits",
            "sources": [{"label": "Plan", "url": "https://example.org/plan"}],
        },
    ],
}
FACTS = {
    "facts": {
        "months": {"text": "nine months", "status": "verified", "sources": ["https://example.org/a"]},
        "cost": {"text": "$5 million", "status": "needs-client"},
        "opened": {"text": "Oct. 23, 2025", "status": "reported", "attribution": "City"},
        "acres": {"text": "48", "status": "client-approved"},
        "unused": {"text": "nothing", "status": "do-not-print"},
    }
}


def make_project(
    root: Path, *, story: dict[str, Any] | None = None, facts: dict[str, Any] | None = None, **config: Any
) -> Path:
    """A tiny project: small synthetic masters for two vantages, a story using every chapter type."""
    index: dict[str, Any] = {"version": 1, "vantages": {}}
    for k, (vid, dates) in enumerate(DATES.items()):
        caps = []
        for i, date in enumerate(dates):
            _master(root / "masters" / vid / f"{date}.jpg", seed=k * 10 + i)
            caps.append(
                {"date": date, "file": f"{vid}/{date}.jpg", "source": "x", "align": {"method": "homography"}}
            )
        index["vantages"][vid] = {
            "name": vid.title(),
            "width": MASTER_SIZE[0],
            "height": MASTER_SIZE[1],
            "captures": caps,
        }
    (root / "masters" / "index.json").write_text(json.dumps(index))
    _master(root / "archive" / "postcard.jpg", seed=99)
    _logo(root / "brand" / "logo.svg", "#123")
    _logo(root / "brand" / "logo-on-dark.svg", "#fff")
    _logo(root / "brand" / "partners" / "city.svg", "#456")
    cfg = {
        "slug": "tiny",
        "title": "A Tiny Park",
        "kicker": "Somewhere · 2025",
        "dek": "A *standfirst* about {fact:months} of work.",
        "draft": True,
        "simulated": True,
        "output": {"images": {"widths": [320, 800], "formats": ["avif", "webp", "jpeg"]}, "base_url": None},
    } | config
    brand = {
        "name": "Tiny Parks",
        "logos": {"primary": "logo.svg", "on_dark": "logo-on-dark.svg"},
        "colors": {"accent": "#555555", "night": "#101010", "paper": "#f5f2ea", "ink": "#1a1a1a"},
        "partners": [{"name": "City", "role": "Owner", "logo": "partners/city.svg"}],
        "disclaimer": "Renderings are conceptual.",
    }
    (root / "project.yaml").write_text(yaml.safe_dump(cfg))
    (root / "story.yaml").write_text(yaml.safe_dump(story or STORY, sort_keys=False))
    (root / "brand" / "brand.yaml").write_text(yaml.safe_dump(brand))
    (root / "facts.yaml").write_text(yaml.safe_dump(facts or FACTS))
    return root


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[Any, Path, dict[str, Any]]:
    root = make_project(tmp_path_factory.mktemp("proj") / "tiny")
    project = load_project(root)
    site = root.parent / "dist" / "site"
    index = build_site(project, site)
    return project, site, read_story(index.read_text())


# --------------------------------------------------------------------------- #
# Capture references and labels
# --------------------------------------------------------------------------- #


def test_resolve_capture_ref():
    dates = ["2025-04-12", "2025-06-01", "2025-09-01"]
    assert resolve_capture_ref("earliest", dates) == 0
    assert resolve_capture_ref("latest", dates) == 2
    assert resolve_capture_ref("#1", dates) == 1
    assert resolve_capture_ref("2025-06-01", dates) == 1  # exact
    assert resolve_capture_ref("2025-08-31", dates) == 1  # nearest on or before
    assert resolve_capture_ref("2030-01-01", dates) == 2
    assert resolve_capture_ref("2020-01-01", dates) == 0  # before everything: the earliest
    assert resolve_capture_ref(None, dates, default=2) == 2
    with pytest.raises(ValueError, match="out of range"):
        resolve_capture_ref("#3", dates)
    with pytest.raises(ValueError, match="not earliest"):
        resolve_capture_ref("yesterday", dates)


def test_capture_labels():
    assert capture_label("2025-06-14") == "June 2025"
    assert capture_label("2025-06-14", day=True) == "June 14, 2025"


# --------------------------------------------------------------------------- #
# StoryJSON
# --------------------------------------------------------------------------- #

CHAPTER_KEYS = {
    "hero": {"title"},
    "scrub": {"vantage", "from", "to", "scrollVh", "hold", "steps", "hotspots"},
    "compare": {"vantage", "before", "after", "beforeLabel", "afterLabel", "mode", "steps", "hotspots"},
    "video": {"video", "loop"},
    "text": set(),
    "stats": {"items"},
    "timeline": {"items"},
    "gallery": {"captures", "images"},
    "explore": {"vantages"},
    "credits": {"sources", "notes"},
}


def assert_img(img: dict[str, Any]) -> None:
    assert set(img) == {"w", "h", "alt", "color", "lqip", "sources", "fallback"}
    assert re.fullmatch(r"#[0-9a-f]{6}", img["color"])
    assert img["lqip"].startswith("data:image/jpeg;base64,") and len(img["lqip"]) < 2500
    assert img["fallback"].endswith(".jpg")
    assert [s["type"] for s in img["sources"]] == ["image/avif", "image/webp", "image/jpeg"]
    for s in img["sources"]:
        widths = [w for _, w in s["srcset"]]
        assert widths == sorted(widths) and max(widths) == img["w"]


def test_story_shape(built):
    _, _, story = built
    assert story["version"] == 1
    assert set(story) == {"version", "meta", "brand", "vantages", "chapters", "notes"}
    meta = story["meta"]
    assert meta["slug"] == "tiny" and meta["draft"] and meta["simulated"] and meta["lang"] == "en"
    assert meta["dateRange"] == {"start": "2025-04-12", "end": "2026-01-15"}  # 2025-06-20 excluded
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", meta["generatedAt"])
    assert meta["shareImage"] == "share.jpg"
    assert meta["dekHtml"] == 'A <em>standfirst</em> about <span class="v-fact" data-fact="months"' + (
        ' data-status="verified" data-releasable="true">nine months</span><sup class="v-note-ref">'
        '<a href="#v-note-1" aria-label="Note 1">1</a></sup> of work.'
    )
    assert meta["dek"] == "A standfirst about nine months of work."
    assert meta["shortTitle"] == "A Tiny Park"
    brand = story["brand"]
    assert brand["logos"] == {"primary": "assets/brand/logo.svg", "onDark": "assets/brand/logo-on-dark.svg"}
    assert brand["partners"][0]["logo"] == "assets/brand/partners/city.svg"
    assert brand["themeColor"] == "#101010"

    by_id = {v["id"]: v for v in story["vantages"]}
    overview = by_id["overview"]
    assert [c["date"] for c in overview["captures"]] == [
        "2025-04-12",
        "2025-06-01",
        "2025-09-01",
        "2026-01-15",
    ]
    assert [c["label"] for c in overview["captures"]] == [
        "Before it began",
        "June 2025",
        "September 2025",
        "January 2026",
    ]
    assert overview["captures"][0]["note"] == "The old lot."
    assert overview["aspect"] == pytest.approx(1.6) and overview["portraitFocus"] == [0.4, 0.6]
    assert overview["kind"] == "drone"
    for v in story["vantages"]:
        for c in v["captures"]:
            assert_img(c["img"])
            assert c["img"]["w"] == MASTER_SIZE[0]  # 800 requested, never upscaled

    chapters = {c["id"]: c for c in story["chapters"]}
    assert [c["type"] for c in story["chapters"]] == [
        "hero", "text", "scrub", "compare", "stats", "timeline", "gallery", "explore", "credits",
    ]  # fmt: skip
    for ch in story["chapters"]:
        assert CHAPTER_KEYS[ch["type"]] <= set(ch), ch["type"]
        assert ch["surface"] in ("night", "paper")
    assert [chapters[k]["surface"] for k in ("open", "story", "scrub", "cmp", "archive", "credits")] == [
        "night", "paper", "night", "night", "night", "paper",
    ]  # fmt: skip
    assert chapters["open"]["capture"] == 3 and chapters["open"]["title"] == "A Tiny Park"
    assert chapters["open"]["kicker"] == "Somewhere · 2025"

    scrub = chapters["scrub"]
    assert (scrub["from"], scrub["to"], scrub["scrollVh"], scrub["hold"]) == (0, 3, 300, 0.55)
    assert [s.get("capture") for s in scrub["steps"]] == [0, None, 3]
    assert scrub["steps"][1]["focus"] == [0.5, 0.5, 2.0]
    assert scrub["hotspots"] == [
        {"id": "pin", "x": 0.3, "y": 0.4, "label": "Here", "html": "<p>Body</p>", "from": 1, "to": 3}
    ]
    cmp = chapters["cmp"]
    assert (cmp["before"], cmp["after"], cmp["beforeLabel"], cmp["afterLabel"], cmp["mode"]) == (
        0, 3, "Before it began", "January 2026", "curtain",
    )  # fmt: skip
    assert chapters["stats-5"]["items"] == [{"value": "48", "unit": "acres", "label": "Site", "note": 4}]
    tl = chapters["tl"]["items"]
    assert tl[1]["vantage"] == "side" and tl[1]["capture"] == 1 and tl[1]["status"] == "in-progress"
    gallery = chapters["archive"]
    assert gallery["captures"] == [0, 1] and gallery["images"][0]["caption"] == "A <em>postcard</em>"
    assert_img(gallery["images"][0]["img"])
    assert chapters["explore"]["vantages"] == ["overview", "side"]


def test_markdown_is_safe_and_typographic(built):
    _, _, story = built
    text = next(c for c in story["chapters"] if c["id"] == "story")
    assert "<script>" not in text["html"] and "&lt;script&gt;alert(1)&lt;/script&gt;" in text["html"]
    assert '<a href="https://example.org" rel="noopener">link</a>' in text["html"]
    assert text["title"] == "A <b>bold</b> start"  # plain text, escaped by the template
    assert "“different”" in text["pullQuote"] and "<script>" not in text["pullQuote"]


def test_facts_are_numbered_in_reading_order(built):
    _, site, story = built
    assert [(n["n"], n["factId"]) for n in story["notes"]] == [
        (1, "months"),
        (2, "cost"),
        (3, "opened"),
        (4, "acres"),
    ]
    assert story["notes"][0]["sources"] == ["https://example.org/a"]
    assert story["meta"]["unverifiedFacts"] == 1  # cost needs the client; "unused" is ignored
    text = next(c for c in story["chapters"] if c["id"] == "story")["html"]
    assert 'data-fact="cost" data-status="needs-client" data-releasable="false"' in text
    assert 'href="#v-note-2"' in text
    scrub = next(c for c in story["chapters"] if c["id"] == "scrub")
    assert 'href="#v-note-2"' in scrub["steps"][2]["html"]  # second use keeps its number
    html = (site / "index.html").read_text()
    assert all(f'id="v-note-{n}"' in html for n in range(1, 5))


def test_every_referenced_asset_exists(built):
    _, site, story = built
    html = (site / "index.html").read_text()
    refs = set(re.findall(r'"(assets/[^"]+)"', json.dumps(story)))
    refs |= set(re.findall(r'(?:src|href|poster)="([^"#:]+)"', html))
    refs |= {u for s in re.findall(r'srcset="([^"]+)"', html) for u in re.findall(r"(\S+) \d+w", s)}
    refs |= set(re.findall(r'url\("?([^")]+?)"?\)', html)) - {r for r in refs if r.startswith("data:")}
    refs = {r for r in refs if not r.startswith(("data:", "http"))}
    assert refs, "nothing referenced?"
    missing = sorted(r for r in refs if not (site / r).is_file())
    assert not missing
    for name, size in (("share.jpg", (1200, 630)), ("icon-192.png", (192, 192)), ("icon-512.png", (512, 512)),
                       ("apple-touch-icon.png", (180, 180))):  # fmt: skip
        with Image.open(site / name) as im:
            assert im.size == size
    manifest = json.loads((site / "manifest.webmanifest").read_text())
    assert manifest["short_name"] and manifest["icons"][1]["purpose"] == "any maskable"
    fonts = sorted(p.name for p in (site / "assets/fonts").iterdir())
    assert fonts == [
        "fraunces-latin-opsz-normal.woff2",
        "inter-latin-opsz-italic.woff2",
        "inter-latin-opsz-normal.woff2",
    ]


def test_service_worker_lists_every_file(built):
    _, site, _ = built
    sw = (site / "sw.js").read_text()
    config = json.loads(re.search(r"const CONFIG = (\{.*?\});", sw).group(1))
    files = sorted(
        p.relative_to(site).as_posix() for p in site.rglob("*") if p.is_file() and p.name != "sw.js"
    )
    assert [a for a, _ in config["assets"]] == files
    assert re.fullmatch(r"vantage-tiny-[0-9a-f]{12}", config["cache"])
    assert {"./", "index.html", "assets/fonts/inter-latin-opsz-normal.woff2"} <= set(config["core"])
    assert set(config["core"]) - {"./"} <= set(files)
    assert 'data-sw="sw.js"' in (site / "index.html").read_text()


def test_rebuild_reuses_encoded_images(built, monkeypatch, tmp_path):
    project, _, story = built
    calls = []
    monkeypatch.setattr(images, "encode", lambda *a, **k: calls.append(a) or b"")
    site = tmp_path / "again"
    build_site(project, site)
    assert calls == []  # every variant came from work/cache/img
    again = read_story((site / "index.html").read_text())
    assert again["vantages"] == story["vantages"]


def test_missing_masters_explains_what_to_do(tmp_path):
    root = make_project(tmp_path / "p")
    (root / "masters" / "index.json").unlink()
    with pytest.raises(FileNotFoundError, match="vantage process tiny"):
        build_site(load_project(root), tmp_path / "site")


def test_unknown_fact_fails_the_build(tmp_path):
    story = json.loads(json.dumps(STORY))
    story["chapters"][1]["body"] = "Uses {fact:nope}."
    with pytest.raises(ValueError, match=r"unknown fact \{fact:nope\}"):
        build_site(load_project(make_project(tmp_path / "p", story=story)), tmp_path / "site")


def test_fact_tokens_never_print_raw(tmp_path):
    """Tokens in vantage names, capture labels and notes and gallery alt text resolve everywhere they
    print: image alt text (and so the JS-off essay), the compare chapter's default labels, StoryJSON."""
    story = json.loads(json.dumps(STORY))
    story["vantages"][0]["name"] = "Over the {fact:lot}"
    story["captures"][0] |= {"label": "Before {fact:season}", "note": "Crews lay {fact:boardwalk}."}
    story["chapters"][6]["images"][0]["alt"] = "A postcard of the {fact:lot}"
    facts = json.loads(json.dumps(FACTS))
    facts["facts"] |= {
        "lot": {"text": "old parking lot", "status": "verified", "sources": ["Survey"]},
        "season": {"text": "spring 2025", "status": "verified", "sources": ["Log"]},
        "boardwalk": {"text": "400 ft of boardwalk", "status": "needs-client"},
    }
    site = tmp_path / "site"
    html = build_site(load_project(make_project(tmp_path / "p", story=story, facts=facts)), site).read_text()
    built = read_story(html)
    assert "{fact:" not in html and "{fact:" not in json.dumps(built)
    alt = "Over the old parking lot, Before spring 2025: Crews lay 400 ft of boardwalk."
    assert built["vantages"][0]["captures"][0]["img"]["alt"] == alt and f'alt="{alt}"' in html
    chapters = {c["id"]: c for c in built["chapters"]}
    assert chapters["cmp"]["beforeLabel"] == "Before spring 2025"
    assert chapters["archive"]["images"][0]["img"]["alt"] == "A postcard of the old parking lot"
    assert {"lot", "season", "boardwalk"} <= {n["factId"] for n in built["notes"]}
    assert built["meta"]["unverifiedFacts"] == 2  # cost and boardwalk


def test_site_coordinates_are_never_published(tmp_path):
    """project.yaml may record the site's lat/lon, but no edition publishes where the site is."""
    location = {"name": "Lot Nine", "region": "Somewhere", "lat": 12.3456, "lon": -45.6789}
    site = tmp_path / "site"
    html = build_site(load_project(make_project(tmp_path / "p", location=location)), site).read_text()
    assert read_story(html)["meta"]["location"] == {"name": "Lot Nine", "region": "Somewhere"}
    for f in site.rglob("*"):
        if f.suffix in {".html", ".js", ".json", ".webmanifest", ".txt"}:
            text = f.read_text()
            assert "12.3456" not in text and "45.6789" not in text, f.name


def test_release_gate(tmp_path):
    root = make_project(tmp_path / "p")
    with pytest.raises(ReleaseError) as err:
        build_site(load_project(root), tmp_path / "site", release=True)
    assert "draft: true" in str(err.value) and "'cost'" in str(err.value) and "unused" not in str(err.value)
    facts = json.loads(json.dumps(FACTS))
    facts["facts"]["cost"]["status"] = "verified"
    root = make_project(tmp_path / "q", facts=facts, draft=False)
    build_site(load_project(root), tmp_path / "site2", release=True)


def _tree(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


@pytest.mark.parametrize(
    "index",
    [
        None,  # no index.html at all
        "<!doctype html><title>My website</title>",  # someone else's site
        '<!doctype html><script id="vantage-story" type="application/json">{}</script>',  # a build, under git
    ],
)
def test_refuses_to_clobber_a_foreign_folder(tmp_path, index):
    """--out may point at any folder: only an earlier Vantage build, outside version control, is replaced."""
    out = tmp_path / "website"
    (out / ".git").mkdir(parents=True)
    (out / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (out / "blog").mkdir()
    (out / "blog" / "post.html").write_text("<p>mine</p>")
    if index is not None:
        (out / "index.html").write_text(index)
    before = _tree(out)
    with pytest.raises(FileExistsError, match="empty folder"):
        build_site(load_project(make_project(tmp_path / "p")), out)
    assert _tree(out) == before


def test_rebuild_replaces_an_earlier_build(tmp_path):
    root = make_project(tmp_path / "p")
    site = tmp_path / "site"
    build_site(load_project(root), site)
    (site / "stale.jpg").write_bytes(b"old")
    build_site(load_project(root), site)
    assert not (site / "stale.jpg").exists() and (site / "index.html").is_file()


# --------------------------------------------------------------------------- #
# Video
# --------------------------------------------------------------------------- #


def _clip_story() -> dict[str, Any]:
    return {
        "vantages": [{"id": "overview", "name": "Overview"}],
        "chapters": [
            {
                "type": "hero",
                "id": "open",
                "video": {"source": "2025-04-12/clip.mp4", "t": 0.2},
                "clip_s": 0.6,
            },
            {"type": "video", "id": "film", "clip": {"source": "2025-04-12/clip.mp4", "t": 0}, "clip_s": 0.5},
        ],
    }


def test_video_clips_encode_cache_and_fall_back(tmp_path, monkeypatch):
    root = make_project(tmp_path / "p", story=_clip_story())
    footage = root / "footage" / "2025-04-12" / "clip.mp4"
    footage.parent.mkdir(parents=True)
    ffmpeg.run(
        ["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12:duration=1.2", "-pix_fmt", "yuv420p", footage]
    )
    project = load_project(root)

    story = read_story(build_site(project, tmp_path / "s1").read_text())
    hero, film = story["chapters"]
    types = [s["type"] for s in hero["video"]["sources"]]
    expected = ['video/mp4; codecs="hvc1"', "video/mp4"] if "libx265" in ffmpeg.encoders() else ["video/mp4"]
    assert types == expected
    assert hero["video"]["sources"][-1]["src"] == "assets/video/open.mp4"
    info = ffmpeg.probe(tmp_path / "s1" / "assets/video/open.mp4")
    assert (info.width, info.height, info.codec) == (320, 180, "h264")
    assert film["video"]["poster"]["fallback"].startswith("assets/img/video/film-")
    if "libx265" in ffmpeg.encoders():
        assert (
            ffmpeg.probe(tmp_path / "s1" / "assets/video/open-hevc.mp4").raw["streams"][0]["codec_tag_string"]
            == "hvc1"
        )

    shutil.rmtree(root / "footage")  # cached clips in work/clips are enough
    monkeypatch.setattr(build, "_encode", lambda *a: pytest.fail("re-encoded a cached clip"))
    story = read_story(build_site(project, tmp_path / "s2").read_text())
    assert story["chapters"][0]["video"]["sources"][-1]["src"] == "assets/video/open.mp4"

    shutil.rmtree(root / "work" / "clips")  # nothing left: hero keeps its still, the video chapter goes
    story = read_story(build_site(project, tmp_path / "s3").read_text())
    assert [c["type"] for c in story["chapters"]] == ["hero"]
    assert "video" not in story["chapters"][0] and story["chapters"][0]["capture"] == 4


def test_image_variants_fast_mode_and_never_upscale(tmp_path, monkeypatch):
    src = tmp_path / "m.jpg"
    _master(src, seed=3)
    settings = load_project(make_project(tmp_path / "p")).config.output.images.model_copy(
        update={"widths": [200, 320, 960], "formats": ["avif", "webp"]}
    )
    full = images.encode_images([images.ImageSpec(src, "assets/img/a", "A")], tmp_path / "s1", settings)[
        "assets/img/a"
    ]
    assert [s["type"] for s in full["sources"]] == ["image/avif", "image/webp", "image/jpeg"]  # JPEG always
    assert [w for _, w in full["sources"][0]["srcset"]] == [200, 320, 480]  # 960 capped at the source width
    assert (full["w"], full["h"]) == (480, 300) and full["fallback"] == "assets/img/a-480.jpg"
    monkeypatch.setenv("VANTAGE_FAST", "1")
    fast = images.encode_images([images.ImageSpec(src, "assets/img/a", "A")], tmp_path / "s2", settings)[
        "assets/img/a"
    ]
    assert [(s["type"], [w for _, w in s["srcset"]]) for s in fast["sources"]] == [
        ("image/webp", [200, 320]), ("image/jpeg", [200, 320]),
    ]  # fmt: skip
    assert sorted(p.name for p in (tmp_path / "s2/assets/img").iterdir()) == [
        "a-200.jpg",
        "a-200.webp",
        "a-320.jpg",
        "a-320.webp",
    ]


# --------------------------------------------------------------------------- #
# Hostile YAML: files from outside the project, script links, colliding names
# --------------------------------------------------------------------------- #


def _edit_yaml(path: Path, change: Any) -> None:
    data = yaml.safe_load(path.read_text())
    change(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.mark.parametrize(
    ("file", "edit", "error"),
    [
        ("brand/brand.yaml", lambda b: b["logos"].update(mark="../../secret.svg"), "outside"),
        ("brand/brand.yaml", lambda b: b["partners"][0].update(logo="brand.yaml"), "not an image"),
        ("story.yaml", lambda s: s["chapters"][6]["images"][0].update(file="../secret.jpg"), "outside"),
        (
            "story.yaml",
            lambda s: s["chapters"].append({"type": "video", "clip": {"source": "../../secret.mp4"}}),
            "outside",
        ),
    ],
)
def test_yaml_cannot_publish_files_from_outside(tmp_path, file, edit, error):
    root = make_project(tmp_path / "p")
    _master(tmp_path / "secret.jpg", seed=1)
    (tmp_path / "secret.svg").write_text("<svg/>")
    (tmp_path / "secret.mp4").write_bytes(b"\0" * 64)
    _edit_yaml(root / file, edit)
    with pytest.raises(ValueError, match=error):
        build_site(load_project(root), tmp_path / "site")
    assert not any("secret" in p.name for p in (tmp_path / "site").rglob("*"))


def test_script_links_are_dropped_and_gallery_names_do_not_collide(tmp_path):
    story = json.loads(json.dumps(STORY))
    credits, timeline, gallery = story["chapters"][-1], story["chapters"][5], story["chapters"][6]
    credits["sources"] += [
        {"label": "Evil", "url": " JavaScript:alert(1)"},
        {"label": "Mail", "url": "mailto:a@b.c"},
    ]
    timeline["items"][0]["source"] = "java\tscript:alert(2)"
    gallery["images"].append({"file": "archive/1960/postcard.jpg", "caption": "Later"})
    root = make_project(tmp_path / "p", story=story)
    _master(root / "archive" / "1960" / "postcard.jpg", seed=60)
    _edit_yaml(root / "brand/brand.yaml", lambda b: b.update(url="data:text/html,<script>alert(3)</script>"))
    _edit_yaml(root / "brand/brand.yaml", lambda b: b["partners"][0].update(url="vbscript:msgbox"))
    site = tmp_path / "site"
    built = read_story(build_site(load_project(root), site).read_text())
    html = (site / "index.html").read_text().lower()
    assert "script:" not in html and "data:text/html" not in html
    chapters = {c["id"]: c for c in built["chapters"]}
    assert [s.get("url") for s in chapters["credits"]["sources"]] == [
        "https://example.org/plan",
        None,
        "mailto:a@b.c",
    ]
    assert "source" not in chapters["tl"]["items"][0] and "url" not in built["brand"]
    assert "url" not in built["brand"]["partners"][0]
    first, second = (g["img"]["fallback"] for g in chapters["archive"]["images"])
    assert first != second  # same file name, two folders: two images
    assert (site / first).read_bytes() != (site / second).read_bytes()


def test_service_worker_installs_one_jpeg_per_image(built):
    _, site, story = built
    config = json.loads(re.search(r"const CONFIG = (\{.*?\});", (site / "sw.js").read_text()).group(1))
    images = [c for c in config["core"] if c.startswith("assets/img/")]
    stems = [re.sub(r"-\d+\.jpg$", "", c) for c in images]
    assert len(stems) == len(set(stems)) and all(c.endswith(".jpg") for c in images)
    fallbacks = {c["img"]["fallback"] for v in story["vantages"] for c in v["captures"]}
    assert fallbacks <= set(images)
    js = (site / "sw.js").read_text()
    assert "const CACHE = `${CONFIG.cache} ${SCOPE}`" in js  # stories sharing an origin keep their caches


# --------------------------------------------------------------------------- #
# Brand: facts in brand.yaml text, sanitized SVG logos
# --------------------------------------------------------------------------- #

HOSTILE_SVG = """<?xml version="1.0"?>
<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">
<?xml-stylesheet type="text/xsl" href="evil.xsl"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 120 32" onload="alert(1)">
  <!-- a comment -->
  <title>Tiny Parks</title>
  <script>alert(2)</script>
  <SCRIPT xlink:href="https://evil.example/x.js"/>
  <foreignObject width="10" height="10"><iframe xmlns="http://www.w3.org/1999/xhtml" src="javascript:alert(3)"/></foreignObject>
  <a xlink:href=" java&#x09;script:alert(4)"><rect width="120" height="32" fill="#123" ONCLICK="alert(5)"/></a>
  <a href="#top"><image xlink:href="data:image/png;base64,iVBORw0KGgo=" width="1" height="1"/></a>
  <image href="data:text/html;base64,PHNjcmlwdD4=" width="1" height="1"/>
  <set attributeName="xlink:href" to="javascript:alert(6)"/>
  <animate attributeName="onbegin" values="alert(7)"/>
  <animate attributeName="fill" values="#123;#456" dur="2s"/>
  <x:script xmlns:x="http://www.w3.org/1999/xhtml">alert(8)</x:script>
  <x:div xmlns:x="http://www.w3.org/1999/xhtml">html in svg</x:div>
  <style>rect > title { fill: red }</style>
</svg>
"""


def test_sanitize_svg_strips_everything_that_runs():
    out, removed = build.sanitize_svg(HOSTILE_SVG.encode())
    text = out.decode()
    root = ET.fromstring(out)
    assert root.tag == "{http://www.w3.org/2000/svg}svg" and root.get("viewBox") == "0 0 120 32"
    assert {el.tag.split("}")[1] for el in root.iter()} == {
        "svg",
        "title",
        "a",
        "rect",
        "image",
        "animate",
        "style",
    }
    assert not re.search(r"script|\son\w+=|foreignObject|stylesheet|DOCTYPE|data:text|<!--|<x:", text, re.I)
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg"')
    assert (
        '<a href="#top"><image xlink:href="data:image/png;base64,iVBORw0KGgo=" width="1" height="1"/></a>'
        in text
    )
    assert '<animate attributeName="fill" values="#123;#456" dur="2s"/>' in text
    assert "<style>rect &gt; title { fill: red }</style>" in text
    assert sorted(set(removed)) == [
        "<animate>", "<div>", "<foreignobject>", "<script>", "<set>",
        "href=data:…", "href=javascript:…", "onclick=…", "onload=…",
    ]  # fmt: skip
    assert build.sanitize_svg(out) == (out, [])  # a clean SVG comes through byte for byte


@pytest.mark.parametrize(
    ("svg", "error"),
    [
        ('<!DOCTYPE svg [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><svg xmlns="{ns}">&b;</svg>',
         "it has a DOCTYPE internal subset"),
        ('<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="{ns}">&x;</svg>',
         "it has a DOCTYPE internal subset"),
        ('<svg xmlns="{ns}">&nbsp;</svg>', "not well-formed"),
        ('<svg xmlns="{ns}"><g></svg>', "not well-formed"),
        ('<svg xmlns="{ns}">' + "<g>" * 300 + "</g>" * 300 + "</svg>", "nested more than 200 deep"),
        ('<html xmlns="http://www.w3.org/1999/xhtml"><body onload="alert(1)"/></html>', "not an SVG document"),
        ('<svg viewBox="0 0 1 1"/>', "not an SVG document"),
    ],
)  # fmt: skip
def test_sanitize_svg_refuses_entities_and_non_svg(svg: str, error: str):
    with pytest.raises(ValueError, match=error):
        build.sanitize_svg(svg.replace("{ns}", "http://www.w3.org/2000/svg").encode(), "logo")


def test_unverified_count_includes_facts_only_in_brand_text(tmp_path):
    facts = json.loads(json.dumps(FACTS))
    facts["facts"]["year"] = {"text": "2026", "status": "needs-client"}
    root = make_project(tmp_path / "p", facts=facts)
    _edit_yaml(root / "brand/brand.yaml", lambda b: b.update({"copyright": "© {fact:year} Tiny Parks"}))
    story = read_story(build_site(load_project(root), tmp_path / "site").read_text())
    assert story["meta"]["unverifiedFacts"] == 2  # cost in the story, year only in the copyright line


def test_brand_text_facts_and_hostile_logos(tmp_path, capsys):
    facts = json.loads(json.dumps(FACTS))
    facts["facts"]["year"] = {"text": "2026", "status": "client-approved", "sources": ["Client brief"]}
    brand_text = {
        "credit_line": "Flown for {fact:months} by Tiny Parks",
        "copyright": "© {fact:year} Tiny Parks",
        "disclaimer": "The {fact:cost} figure is an estimate.",
    }
    root = make_project(tmp_path / "p", facts=facts)
    _edit_yaml(root / "brand/brand.yaml", lambda b: b.update(brand_text))
    (root / "brand" / "logo-on-dark.svg").write_text(HOSTILE_SVG)
    site = tmp_path / "site"
    story = read_story(build_site(load_project(root), site).read_text())

    assert [story["brand"][k] for k in ("creditLine", "copyright", "disclaimer")] == [
        "Flown for nine months by Tiny Parks", "© 2026 Tiny Parks", "The $5 million figure is an estimate.",
    ]  # fmt: skip
    assert [(n["factId"], n["releasable"]) for n in story["notes"]] == [
        ("months", True), ("cost", False), ("opened", True), ("acres", True), ("year", True),
    ]  # fmt: skip
    html = (site / "index.html").read_text()
    credits = html[html.index('id="credits"') :]
    assert "{fact:" not in html and '<p class="v-dek">Flown for nine months by Tiny Parks</p>' in credits
    assert "<p>The $5 million figure is an estimate.</p>" in credits and "<p>© 2026 Tiny Parks</p>" in credits
    assert (
        '<li id="v-note-2" data-releasable="false">$5 million'
        ' <span class="v-label v-unverified">Unverified: needs client</span></li>'
    ) in credits
    assert '<li id="v-note-5">2026<span class="v-meta">Client brief</span></li>' in credits

    logo = (site / story["brand"]["logos"]["onDark"]).read_text()
    assert "script" not in logo.lower() and "onload" not in logo and '<rect width="120"' in logo
    assert "logo 'logo-on-dark.svg': removed <animate>, <div>, <foreignobject>" in capsys.readouterr().err

    facts["facts"]["cost"]["status"] = "verified"
    facts["facts"]["year"]["status"] = "needs-client"
    root = make_project(tmp_path / "q", facts=facts, draft=False)
    _edit_yaml(root / "brand/brand.yaml", lambda b: b.update(brand_text))
    with pytest.raises(ReleaseError, match=r"brand\.yaml\.copyright: fact 'year' has status 'needs-client'"):
        build_site(load_project(root), tmp_path / "site2", release=True)
    (root / "brand" / "logo.svg").write_text(
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'
    )
    with pytest.raises(ValueError, match=r"logo 'logo\.svg': refusing to publish this SVG"):
        build_site(load_project(root), tmp_path / "site3")
