"""Deliverables from a built site: single-file HTML (full + lite), offline zip, checksums, build info.

dist/<slug>/<slug>.html          everything inline: fonts, one ~1600 px JPEG per image, videos if
                                 they fit output.single_file_max_mb (see ARCHITECTURE.md
                                 "Single-file assets")
dist/<slug>/<slug>-lite.html     email-sized: ~1080 px JPEGs for the hero, compare pairs, scrub steps
                                 and video posters; other images keep only their LQIP; no video
dist/<slug>/<slug>-offline.zip   the site folder for file:// (no service worker / manifest)
dist/<slug>/HOW-TO-VIEW.txt  SHA256SUMS  build.json
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import io
import json
import mimetypes
import os
import platform
import re
import zipfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import PIL
from PIL import Image

from vantage import __version__, log
from vantage.config import Project
from vantage.site.build import ReleaseError, build_site, inside
from vantage.site.facts import check_release
from vantage.site.images import data_uri, encode, jpeg_bytes, resize
from vantage.site.render import Edition, render_page

MIB = 1024 * 1024
HOSTED_FILE_LIMIT = 25 * MIB  # Cloudflare Pages / Workers static assets
_STORY = re.compile(r'<script id="vantage-story" type="application/json">(.*?)</script>', re.S)
_THEME = re.compile(r'<style id="vantage-theme">(.*?)</style>', re.S)
_ASSET_ATTR = re.compile(r'<section class="v-chapter v-(\w+)|\b(src|poster|href)="(assets/[^"]+)"')
_CSS_URL = re.compile(r'url\("(assets/[^"]+)"\)')
_STORED = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".mp4", ".m4v", ".woff2", ".zip"}
_MIME = {".woff2": "font/woff2", ".avif": "image/avif", ".webp": "image/webp", ".svg": "image/svg+xml",
         ".webmanifest": "application/manifest+json", ".mp4": "video/mp4", ".js": "text/javascript"}  # fmt: skip
# (width, JPEG quality) steps tried until an edition fits its budget; None = the site's own ~1600 px JPEG.
_FULL_LADDER: tuple[tuple[int, int] | None, ...] = (None, (1280, 78), (1080, 72), (900, 66))
_LITE_LADDER: tuple[tuple[int, int], ...] = ((1080, 70), (960, 64), (800, 58), (640, 52))
_REPEAT = (720, 62)  # (width, quality) cap for a thumbnail of a picture the page already carries
# Chapters whose pictures are thumbnails (the runtime never enlarges them): a repeat there gets a
# small copy. Elsewhere a repeat keeps the full bytes: the runtime moves those pictures into stages.
_THUMBNAILS = {"explore", "timeline"}


def mime_type(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    return _MIME.get(suffix) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _site_file(site: Path, path: str) -> Path:
    """A file of the built site; a path that leads outside it (a tampered page or theme) is refused."""
    return inside(site / path, site, "packaged asset")


def read_story(html: str) -> dict[str, Any]:
    """The StoryJSON embedded in a rendered page."""
    m = _STORY.search(html)
    if not m:
        raise ValueError('no <script id="vantage-story"> in the page; rebuild the site')
    return json.loads(m.group(1))


def _imgs(story: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for v in story["vantages"]:
        for c in v["captures"]:
            yield c["img"]
    for ch in story["chapters"]:
        if "video" in ch:
            yield ch["video"]["poster"]
        for g in ch.get("images", []):
            yield g["img"]


def _essential(story: dict[str, Any]) -> set[int]:
    """ids of the Img dicts the lite edition keeps sharp: hero, compare pairs, one per scrub step, posters."""
    vantages = {v["id"]: v for v in story["vantages"]}
    keep: list[dict[str, Any]] = []
    for ch in story["chapters"]:
        v = vantages.get(ch.get("vantage", ""))
        if ch["type"] == "hero" and (v or "video" in ch):
            keep.append(v["captures"][ch["capture"]]["img"] if v else ch["video"]["poster"])
        elif ch["type"] == "compare" and v:
            keep += [v["captures"][ch["before"]]["img"], v["captures"][ch["after"]]["img"]]
        elif ch["type"] == "video":
            keep.append(ch["video"]["poster"])
        elif ch["type"] == "scrub" and v:
            caps = [s["capture"] for s in ch["steps"] if s.get("capture") is not None]
            keep += [v["captures"][i]["img"] for i in caps or (ch["from"], ch["to"])]
    return {id(img) for img in keep}


class _Assets:
    """Bytes for every site-relative path an edition references, re-encoded on demand."""

    def __init__(self, site: Path, step: tuple[int, int] | None) -> None:
        self.site = site
        self.files: dict[str, bytes] = {}
        self._uris: dict[str, str] = {}
        # repeats follow the edition down its ladder, never above the cap
        self._repeat = _REPEAT if step is None else (min(_REPEAT[0], step[0]), min(_REPEAT[1], step[1]))

    def add_file(self, path: str) -> str:
        if path not in self.files:
            self.files[path] = _site_file(self.site, path).read_bytes()
        return path

    def add_jpeg(self, img: dict[str, Any], step: tuple[int, int] | None) -> None:
        """Point `img` at a single JPEG: the site's fallback, or a re-encode at (width, quality)."""
        jpeg = next(s for s in img["sources"] if s["type"] == "image/jpeg")["srcset"]
        if step is None:
            path = img["fallback"]
            width = next((w for url, w in jpeg if url == path), img["w"])
            self.add_file(path)
        else:
            width, quality = step
            largest = max(jpeg, key=lambda e: e[1])[0]
            width = min(width, max(w for _, w in jpeg))
            path = re.sub(r"-\d+\.jpg$", f"-{width}q{quality}.jpg", largest)
            if path not in self.files:
                self.files[path] = jpeg_bytes(_site_file(self.site, largest), width, quality)
        img["h"] = round(img["h"] * width / img["w"])
        img["w"] = width
        img["sources"] = [{"type": "image/jpeg", "srcset": [[path, width]]}]
        img["fallback"] = path

    def repeat(self, path: str) -> str:
        """A smaller copy of a JPEG the page already carries, for a thumbnail (never the asset itself)."""
        width, quality = self._repeat
        again = path.removesuffix(".jpg") + f"-again{width}q{quality}.jpg"
        if again not in self.files:
            with Image.open(io.BytesIO(self.files[path])) as im:
                self.files[again] = encode(resize(im.convert("RGB"), width), "jpeg", quality)
        return again

    def uri(self, path: str) -> str:
        if path not in self._uris:
            self._uris[path] = data_uri(self.files[path], mime_type(path))
        return self._uris[path]


def _inline(html: str, assets: _Assets) -> tuple[str, set[str]]:
    """Every assets/… attribute → data: URI. The first <img> of a path carries it (data-asset); a
    thumbnail of a picture shown before (explore grid, timeline) gets a smaller copy instead."""
    carried: set[str] = set()
    chapter = ""

    def sub(m: re.Match[str]) -> str:
        nonlocal chapter
        if m[1]:
            chapter = m[1]
            return m[0]
        attr, path = m[2], m[3]
        if attr == "src" and path not in carried:
            carried.add(path)
            return f'src="{assets.uri(path)}" data-asset="{path}"'
        if attr == "src" and chapter in _THUMBNAILS and path.endswith(".jpg"):
            path = assets.repeat(path)
        return f'{attr}="{assets.uri(path)}"'

    return _ASSET_ATTR.sub(sub, html), carried


def _referenced(story: dict[str, Any]) -> list[str]:
    return sorted(set(re.findall(r'"(assets/[^"]+)"', json.dumps(story))))


def _embed(html: str, assets: _Assets, paths: list[str]) -> str:
    """Append <script type="application/octet-stream" data-asset> blocks before the runtime script."""
    blocks = "".join(
        f'<script type="application/octet-stream" data-asset="{p}" data-type="{mime_type(p)}">'
        f"{base64.b64encode(assets.files[p]).decode('ascii')}</script>\n"
        for p in paths
    )
    marker = '<script id="vantage-runtime">' if '<script id="vantage-runtime">' in html else "</body>"
    return html.replace(marker, blocks + marker, 1)


def _edition(
    story: dict[str, Any],
    site: Path,
    theme: str,
    edition: Edition,
    step: tuple[int, int] | None,
    *,
    videos: bool,
) -> str:
    story = copy.deepcopy(story)
    if not story["meta"].get("shareImage", "").startswith(("http://", "https://")):
        story["meta"].pop("shareImage", None)  # share.jpg doesn't travel with a lone file
    assets = _Assets(site, step)
    keep = _essential(story) if edition == "lite" else None
    for img in _imgs(story):
        if keep is not None and id(img) not in keep:
            img["sources"], img["fallback"] = [], img["lqip"]
        else:
            assets.add_jpeg(img, step)
    for ch in story["chapters"]:
        video = ch.get("video")
        if video and not videos:
            if ch["type"] == "hero":
                del ch["video"]
            else:
                video["sources"] = []
        if "video" in ch:  # one file needs one codec: H.264 plays everywhere
            ch["video"]["sources"] = [s for s in ch["video"]["sources"] if s["type"] == "video/mp4"]
            for source in ch["video"]["sources"]:
                assets.add_file(source["src"])
    logos = story["brand"]["logos"]
    for path in [
        *logos.values(),
        *(p[k] for p in story["brand"]["partners"] for k in ("logo", "logoOnDark") if k in p),
    ]:
        assets.add_file(path)
    css = _CSS_URL.sub(
        lambda m: f'url("{data_uri(_site_file(site, m[1]).read_bytes(), mime_type(m[1]))}")', theme
    )
    html, inlined = _inline(render_page(story, theme_css=css, edition=edition), assets)
    missing = [p for p in _referenced(story) if p not in inlined]
    return _embed(html, assets, missing)


def _fit(name: str, render: Callable[[Any], str], ladder: Sequence[Any], budget_mb: float) -> str:
    """The first rendering along `ladder` that fits the budget (the last one, with a warning, if none do)."""
    html = ""
    for step in ladder:
        html = render(step)
        size = len(html.encode("utf-8"))
        if size <= budget_mb * MIB:
            return html
        log.info(f"{name}: {size / MIB:.1f} MB is over its {budget_mb:g} MB budget; trying a lighter edition")
    log.warn(f"{name} is still {len(html.encode()) / MIB:.1f} MB, over its {budget_mb:g} MB budget")
    return html


def _single_files(
    project: Project, site: Path, story: dict[str, Any], theme: str, dist: Path
) -> dict[str, Path]:
    out = project.config.output
    full, lite = dist / f"{project.slug}.html", dist / f"{project.slug}-lite.html"
    has_video = any(ch.get("video", {}).get("sources") for ch in story["chapters"])
    ladder = ([(None, True)] if has_video else []) + [(step, False) for step in _FULL_LADDER]
    full.write_text(
        _fit(
            full.name,
            lambda s: _edition(story, site, theme, "single", s[0], videos=s[1]),
            ladder,
            out.single_file_max_mb,
        ),
        encoding="utf-8",
    )
    lite.write_text(
        _fit(
            lite.name,
            lambda s: _edition(story, site, theme, "lite", s, videos=False),
            _LITE_LADDER,
            out.lite_max_mb,
        ),
        encoding="utf-8",
    )
    return {"single_file": full, "lite": lite}


def _zip_time() -> tuple[int, int, int, int, int, int]:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    when = dt.datetime.fromtimestamp(int(epoch), dt.UTC) if epoch else dt.datetime(1980, 1, 1, tzinfo=dt.UTC)
    when = max(when, dt.datetime(1980, 1, 1, tzinfo=dt.UTC))
    return when.year, when.month, when.day, when.hour, when.minute, when.second


def _offline_zip(project: Project, site: Path, story: dict[str, Any], theme: str, out: Path) -> Path:
    """The site folder for file:// use: same files, no service worker or manifest; deterministic bytes."""
    when = _zip_time()
    entries = {"index.html": render_page(story, theme_css=theme, edition="offline").encode("utf-8")}
    for path in sorted(p for p in site.rglob("*") if p.is_file()):
        rel = path.relative_to(site).as_posix()
        if rel not in ("index.html", "sw.js", "manifest.webmanifest"):
            entries[rel] = path.read_bytes()
    tmp = out.with_name(out.name + ".part")
    with zipfile.ZipFile(tmp, "w") as zf:
        for rel in sorted(entries):
            info = zipfile.ZipInfo(f"{project.slug}/{rel}", date_time=when)
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            stored = Path(rel).suffix.lower() in _STORED
            info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            zf.writestr(info, entries[rel], compresslevel=None if stored else 9)
    tmp.replace(out)
    return out


def _how_to_view(story: dict[str, Any], files: dict[str, Path], films: list[Path], out: Path) -> Path:
    """Plain-language instructions that travel with the files."""
    meta = story["meta"]

    def size(p: Path) -> str:
        return f"{p.stat().st_size / MIB:.1f} MB"

    rows: list[tuple[str, str, str]] = []  # (section, file, what it is)
    if "single_file" in files:
        rows.append(
            ("On a computer", files["single_file"].name, "the whole story in one file; double-click to open")
        )
    rows.append(("On a computer", files["zip"].name, "full resolution; unzip it, then open index.html"))
    if "lite" in files:
        rows.append(("By email or message", files["lite"].name, "a small edition with the key images"))
    for film in films:
        use = (
            "for phones: save to Photos or send in a message"
            if "9x16" in film.name
            else "for screens and talks"
        )
        rows.append(("The films", f"film/{film.name}", use))
    width = max(len(name) for _, name, _ in rows)
    sizes = {name: size(out.parent / name) for _, name, _ in rows}
    lines = [
        meta["title"],
        *([meta["subtitle"]] if meta.get("subtitle") else []),
        "",
        "HOW TO VIEW THIS STORY",
        "",
    ]
    lines += [
        "On an iPhone or iPad (best)",
        f"  1. Open {meta.get('url') or 'the link you were sent'} in Safari.",
        '  2. Tap the Share button, then "Add to Home Screen".',
        '  3. Open the story from its new icon and tap "Save for offline".',
        "     It now works anywhere, even in airplane mode, with full-resolution images and video.",
    ]
    section = ""
    for heading, name, what in rows:
        if heading != section:
            lines += ["", heading]
            section = heading
        lines.append(f"  {name:<{width}}  {sizes[name]:>8}  {what}")
    if "lite" in files:
        lines.append(
            "  (On an iPhone the file opens as a preview: scroll to read; the link above is the full story.)"
        )
    lines += [
        "",
        "Checking the files",
        "  SHA256SUMS lists a checksum per file; build.json records how this edition was made.",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def package_project(project: Project, dist_dir: Path, *, release: bool = False) -> dict[str, Path]:
    """Package dist_dir/site into the offline deliverables. The site is built first when missing, and
    always rebuilt for a release: one already in dist may be an earlier draft build (Preview badge,
    unverified facts) or predate the YAML that just passed the gates, and must not ship as a release."""
    if release:
        problems = check_release(project)
        if problems:
            raise ReleaseError("release package blocked:\n  " + "\n  ".join(problems))
    site = dist_dir / "site"
    if release:
        log.info(f"release: rebuilding {site} from the current YAML")
        build_site(project, site, release=True)
    elif not (site / "index.html").is_file():
        log.info(f"no site in {site}; building it first")
        build_site(project, site)
    page = (site / "index.html").read_text(encoding="utf-8")
    story = read_story(page)
    theme_match = _THEME.search(page)
    theme = theme_match.group(1) if theme_match else ""
    files: dict[str, Path] = {}
    if project.config.output.single_file:
        files |= _single_files(project, site, story, theme, dist_dir)
    files["zip"] = _offline_zip(project, site, story, theme, dist_dir / f"{project.slug}-offline.zip")
    films = sorted((dist_dir / "film").glob("*.mp4"))
    files["how_to_view"] = _how_to_view(story, files, films, dist_dir / "HOW-TO-VIEW.txt")
    site_files = [p for p in site.rglob("*") if p.is_file()]
    for p in site_files:
        if p.stat().st_size > HOSTED_FILE_LIMIT:
            log.warn(
                f"{p.relative_to(dist_dir)} is {p.stat().st_size / MIB:.1f} MiB; hosts like Cloudflare Pages cap files at 25 MiB"
            )
    deliverables = sorted([*files.values(), *films], key=lambda p: p.relative_to(dist_dir).as_posix())
    info = {
        "version": 1,
        "slug": project.slug,
        "title": story["meta"]["title"],
        "generatedAt": story["meta"]["generatedAt"],
        "release": release,
        "draft": story["meta"]["draft"],
        "simulated": story["meta"]["simulated"],
        "unverifiedFacts": story["meta"].get("unverifiedFacts", 0),
        "tools": {"vantage": __version__, "python": platform.python_version(), "pillow": PIL.__version__},
        "site": {"files": len(site_files), "bytes": sum(p.stat().st_size for p in site_files)},
        "files": {
            p.relative_to(dist_dir).as_posix(): {"bytes": p.stat().st_size, "sha256": _sha256(p)}
            for p in deliverables
        },
    }
    files["build_json"] = dist_dir / "build.json"
    files["build_json"].write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    sums = [*deliverables, files["build_json"]]
    files["checksums"] = dist_dir / "SHA256SUMS"
    files["checksums"].write_text(
        "".join(
            f"{_sha256(p)}  {p.relative_to(dist_dir).as_posix()}\n"
            for p in sorted(sums, key=lambda p: p.relative_to(dist_dir).as_posix())
        ),
        encoding="utf-8",
    )
    log.info(
        ", ".join(
            f"{p.name} {p.stat().st_size / MIB:.1f} MB"
            for k, p in files.items()
            if k in ("single_file", "lite", "zip")
        )
    )
    return files
