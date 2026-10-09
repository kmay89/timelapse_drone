"""Responsive image derivatives (AVIF/WebP/JPEG), LQIP + dominant color, share card and app icons.

`encode_images` turns master JPEGs into the StoryJSON `Img` shape (docs/ARCHITECTURE.md), writing
`<dest>-<width>.<ext>` under the site folder. Widths never exceed the source. Encoded variants are
cached by content hash (source bytes + settings) so rebuilds only copy files; AVIF is slow, so
images are encoded in parallel threads (Pillow releases the GIL). `VANTAGE_FAST=1` limits output to
JPEG + WebP at two widths for quick dev/CI builds.

Published images carry pixels and a colour profile, nothing else: `open_rgb` drops EXIF, XMP and
comments before anything is encoded, and `strip_raster` re-encodes raster logos the same way.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps, JpegImagePlugin

from vantage.config import ImageSettings

_VERSION = 2  # 2: variants no longer inherit a source's JPEG comment
_ORDER = ("avif", "webp", "jpeg")
_EXT = {"avif": "avif", "webp": "webp", "jpeg": "jpg"}
_MIME = {"avif": "image/avif", "webp": "image/webp", "jpeg": "image/jpeg"}
_DEFAULT_QUALITY = {"avif": 52, "webp": 80, "jpeg": 82}
_FALLBACK_WIDTH = 1600  # the plain <img src> / single-file size
LQIP_WIDTH = 32


@dataclass(frozen=True)
class ImageSpec:
    src: Path
    dest: str  # URL stem relative to the site root, e.g. "assets/img/overview/2025-06-14"
    alt: str


def fast_mode() -> bool:
    return os.environ.get("VANTAGE_FAST", "") not in ("", "0")


def _plan(settings: ImageSettings) -> tuple[list[str], list[int], dict[str, int]]:
    formats = [f for f in _ORDER if f in settings.formats or f == "jpeg"]  # JPEG is always the fallback
    widths = sorted(set(settings.widths))
    if fast_mode():
        formats, widths = ["webp", "jpeg"], widths[:2]
    return formats, widths, {f: settings.quality.get(f, _DEFAULT_QUALITY[f]) for f in formats}


def _bare(im: Image.Image) -> Image.Image:
    """`im` with `info` cut to its colour profile. Pillow's writers fall back to `info` (a JPEG
    source's COM comment is written into every JPEG made from it), so EXIF, XMP, IPTC and comments
    from a camera, a scanner or a design tool stop here."""
    im.info = {"icc_profile": im.info["icc_profile"]} if im.info.get("icc_profile") else {}
    return im


def open_rgb(path: Path) -> Image.Image:
    """`path` upright (EXIF orientation applied) as RGB, with no metadata but its colour profile."""
    with Image.open(path) as im:
        return _bare(ImageOps.exif_transpose(im).convert("RGB"))


def resize(im: Image.Image, width: int) -> Image.Image:
    if width >= im.width:
        return im
    height = max(1, round(im.height * width / im.width))
    return im.resize((width, height), Image.Resampling.LANCZOS, reducing_gap=3.0)


def encode(im: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt == "avif":
        im.save(buf, "AVIF", quality=quality, speed=6, max_threads=1)
    elif fmt == "webp":
        im.save(buf, "WEBP", quality=quality, method=5)
    else:
        im.save(buf, "JPEG", quality=quality, progressive=True, optimize=True, subsampling="4:2:0")
    return buf.getvalue()


def jpeg_bytes(path: Path, width: int, quality: int) -> bytes:
    """A progressive JPEG of `path` at most `width` px wide (single-file editions)."""
    return encode(resize(open_rgb(path), width), "jpeg", quality)


def data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def lqip(im: Image.Image) -> str:
    """A ~32 px wide JPEG data URI: the blurred placeholder shown while the real image loads."""
    return data_uri(encode(resize(im, LQIP_WIDTH), "jpeg", 50), "image/jpeg")


def mean_color(im: Image.Image) -> str:
    small = resize(im, 64)
    r, g, b = np.asarray(small, dtype=np.float64).reshape(-1, 3).mean(axis=0).round().astype(int)
    return f"#{r:02x}{g:02x}{b:02x}"


def _widths(src_width: int, widths: Sequence[int]) -> list[int]:
    return sorted({min(w, src_width) for w in widths})


def _cache_key(data: bytes, formats: list[str], widths: list[int], quality: dict[str, int]) -> str:
    h = hashlib.sha256(data)
    h.update(json.dumps([_VERSION, formats, widths, quality], sort_keys=True).encode())
    return h.hexdigest()[:24]


def _variants(
    src: Path, formats: list[str], widths: list[int], quality: dict[str, int]
) -> tuple[dict[str, Any], dict[str, bytes]]:
    im = open_rgb(src)
    ws = _widths(im.width, widths)
    blobs = {}
    for w in ws:
        scaled = resize(im, w)
        blobs |= {f"{w}.{_EXT[f]}": encode(scaled, f, quality[f]) for f in formats}
    meta = {
        "w": ws[-1],
        "h": max(1, round(im.height * ws[-1] / im.width)),
        "widths": ws,
        "color": mean_color(im),
        "lqip": lqip(im),
    }
    return meta, blobs


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _cached(entry: Path | None, formats: list[str]) -> dict[str, Any] | None:
    if entry is None or not (entry / "meta.json").is_file():
        return None
    meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
    complete = all((entry / f"{w}.{_EXT[f]}").is_file() for w in meta["widths"] for f in formats)
    return meta if complete else None


def _encode_one(spec: ImageSpec, site_dir: Path, settings: ImageSettings, cache_dir: Path | None) -> dict:
    formats, widths, quality = _plan(settings)
    entry = cache_dir / _cache_key(spec.src.read_bytes(), formats, widths, quality) if cache_dir else None
    meta = _cached(entry, formats)
    if meta is None:
        meta, blobs = _variants(spec.src, formats, widths, quality)
        for name, blob in blobs.items():
            w, ext = name.split(".")
            _write(site_dir / f"{spec.dest}-{w}.{ext}", blob)
            if entry:
                _write(entry / name, blob)
        if entry:
            (entry / "meta.json").write_text(
                json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    else:
        assert entry is not None
        for w in meta["widths"]:
            for f in formats:
                target = site_dir / f"{spec.dest}-{w}.{_EXT[f]}"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry / f"{w}.{_EXT[f]}", target)
    ws = meta["widths"]
    fallback = min(ws, key=lambda w: (abs(w - _FALLBACK_WIDTH), -w))
    return {
        "w": meta["w"],
        "h": meta["h"],
        "alt": spec.alt,
        "color": meta["color"],
        "lqip": meta["lqip"],
        "sources": [
            {"type": _MIME[f], "srcset": [[f"{spec.dest}-{w}.{_EXT[f]}", w] for w in ws]} for f in formats
        ],
        "fallback": f"{spec.dest}-{fallback}.jpg",
    }


def encode_images(
    specs: Sequence[ImageSpec],
    site_dir: Path,
    settings: ImageSettings,
    *,
    cache_dir: Path | None = None,
    workers: int | None = None,
) -> dict[str, dict]:
    """Encode every spec (in parallel) → {dest: Img}. Specs sharing a dest are encoded once."""
    unique = list({s.dest: s for s in specs}.values())
    with ThreadPoolExecutor(max_workers=workers or min(4, os.cpu_count() or 1)) as pool:
        imgs = pool.map(lambda s: _encode_one(s, site_dir, settings, cache_dir), unique)
        return {s.dest: img for s, img in zip(unique, imgs, strict=True)}


# --------------------------------------------------------------------------- #
# Share card and app icons (Pillow, using the brand's own WOFF2 faces)
# --------------------------------------------------------------------------- #


def load_font(
    path: Path, size: int, *, weight: int | None = None, opsz: int | None = None
) -> ImageFont.FreeTypeFont:
    """A FreeType font (WOFF2 works) with variable axes set by name when the font has them."""
    font = ImageFont.truetype(str(path), size)
    try:
        axes = font.get_variation_axes()
    except OSError:  # not a variable font
        return font
    values = []
    for axis in axes:
        name = axis["name"].decode() if isinstance(axis["name"], bytes) else str(axis["name"])
        wanted = {"weight": weight, "optical size": opsz}.get(name.lower())
        value = axis["default"] if wanted is None else wanted
        values.append(min(max(value, axis["minimum"]), axis["maximum"]))
    font.set_variation_by_axes(values)
    return font


def _rgb(color: str) -> tuple[int, int, int]:
    h = color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _cover(im: Image.Image, size: tuple[int, int], focus: tuple[float, float] = (0.5, 0.5)) -> Image.Image:
    return ImageOps.fit(im, size, Image.Resampling.LANCZOS, centering=focus)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        trial = f"{lines[-1]} {word}" if lines else word
        if lines and draw.textlength(trial, font=font) <= width:
            lines[-1] = trial
        else:
            lines.append(word)
    return lines


@dataclass(frozen=True)
class CardText:
    title: str
    kicker: str | None
    wordmark: str
    detail: str | None


def share_card(
    photo: Path,
    out: Path,
    text: CardText,
    *,
    colors: dict[str, str],
    display_font: Path,
    text_font: Path,
    size: tuple[int, int] = (1200, 630),
) -> Path:
    """The 1200×630 Open Graph card: photo, bottom scrim, kicker, title, wordmark."""
    w, h = size
    im = _cover(open_rgb(photo), size).convert("RGBA")
    night = _rgb(colors["night"])
    rows = np.arange(h) / h  # bottom scrim for the title, a lighter one at the top for the wordmark
    ys = (np.clip((rows - 0.25) / 0.75, 0, 1) ** 1.6 * 0.92 + np.clip(1 - rows / 0.3, 0, 1) ** 2 * 0.55)[
        :, None
    ]
    xs = np.clip(0.42 - np.arange(w) / w, 0, None)[None, :] * 0.5
    alpha = (np.clip(ys + xs, 0, 1) * 255).round().astype(np.uint8)
    scrim = Image.new("RGBA", size, (*night, 0))
    scrim.putalpha(Image.fromarray(alpha, "L"))
    im = Image.alpha_composite(im, scrim)
    draw = ImageDraw.Draw(im)
    pad = 64
    title_font = load_font(display_font, 76, weight=500, opsz=144)
    lines = _wrap(draw, text.title, title_font, w - 2 * pad - 80)
    while len(lines) > 2 and title_font.size > 48:
        title_font = load_font(display_font, title_font.size - 6, weight=500, opsz=144)
        lines = _wrap(draw, text.title, title_font, w - 2 * pad - 80)
    lines = lines[:3]
    leading = round(title_font.size * 1.04)
    y = h - pad - leading * len(lines)
    label = load_font(text_font, 21, weight=600)
    if text.kicker:
        draw.text((pad, y - 46), text.kicker.upper(), font=label, fill=_rgb(colors["accent-on-night"]))
    snow = _rgb(colors["text-on-night"])
    for i, line in enumerate(lines):
        draw.text((pad, y + i * leading), line, font=title_font, fill=snow)
    small = load_font(text_font, 20, weight=500)
    draw.text((pad, pad - 6), text.wordmark.upper(), font=small, fill=snow)
    draw.line([(pad, pad + 30), (pad + 40, pad + 30)], fill=_rgb(colors["accent-on-night"]), width=3)
    if text.detail:
        tw = draw.textlength(text.detail, font=small)
        draw.text((w - pad - tw, pad - 6), text.detail, font=small, fill=_rgb(colors["muted-on-night"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    im.convert("RGB").save(out, "JPEG", quality=86, progressive=True, optimize=True)
    return out


# --------------------------------------------------------------------------- #
# Raster logos: pixels only
# --------------------------------------------------------------------------- #

_RASTER_WRITERS: dict[str, tuple[str, dict[str, Any]]] = {  # Pillow format → (writer, lossless-ish options)
    "PNG": ("PNG", {"optimize": True}),
    "GIF": ("GIF", {"optimize": True}),
    "WEBP": ("WEBP", {"lossless": True, "exact": True, "quality": 100}),
    "JPEG": ("JPEG", {"optimize": True}),
    "MPO": ("JPEG", {"optimize": True}),  # a JPEG with more pictures after it (phone thumbnails, depth)
    "AVIF": ("AVIF", {"quality": 90, "subsampling": "4:4:4", "speed": 6, "max_threads": 1}),
}
_METADATA = {
    "exif": "EXIF",
    "xmp": "XMP",
    "XML:com.adobe.xmp": "XMP",
    "photoshop": "IPTC",
    "comment": "comment",
}
EXTRA_FRAMES = "frames after the first"


def strip_raster(path: Path) -> tuple[bytes, list[str]]:
    """A PNG, JPEG, WebP, AVIF or GIF re-encoded from its pixels in its own format, and what went.

    Only pixels, transparency and the colour profile are kept. EXIF (camera, GPS, author), XMP
    (design-tool history with names and local file paths), IPTC, comments and PNG text chunks are
    not; EXIF orientation is applied to the pixels first. PNG, GIF and WebP are written losslessly
    (the decoded pixels exactly; a lossy WebP grows), a JPEG with its own quantization tables and
    subsampling, an AVIF at quality 90. Of an animated image only the first frame is kept: a logo
    does not move (prefers-reduced-motion).
    """
    with Image.open(path) as src:
        if src.format not in _RASTER_WRITERS:
            raise ValueError(f"{path.name} is {src.format or 'unknown'}, not PNG, JPEG, WebP, AVIF or GIF")
        writer, options = _RASTER_WRITERS[src.format]
        options = dict(options)
        removed = {name for key, name in _METADATA.items() if src.info.get(key)}
        if set(getattr(src, "text", None) or ()) - set(_METADATA):
            removed.add("text chunks")
        if getattr(src, "n_frames", 1) > 1:
            removed.add(EXTRA_FRAMES)
        if writer == "JPEG":
            options |= {
                "qtables": src.quantization,
                "subsampling": JpegImagePlugin.get_sampling(src),
                "progressive": bool(src.info.get("progressive")),
            }
        icc = src.info.get("icc_profile")
        im = ImageOps.exif_transpose(src)  # the first frame, upright
        transparency = im.info.get("transparency")
        im.info = {}
        if icc and writer != "GIF":
            options["icc_profile"] = icc
        if transparency is not None and writer in ("PNG", "GIF"):
            options["transparency"] = transparency
        if writer == "JPEG" and im.mode not in ("L", "RGB", "CMYK"):
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, writer, **options)
    return buf.getvalue(), sorted(removed)


def initials(name: str) -> str:
    words = [w for w in name.replace("-", " ").split() if w[:1].isalnum()]
    return "".join(w[0] for w in words[:2]).upper() or "V"


def app_icon(out: Path, size: int, letters: str, *, colors: dict[str, str], display_font: Path) -> Path:
    """A typographic monogram icon (maskable: everything inside the central 80% safe zone)."""
    scale = 4
    s = size * scale
    im = Image.new("RGB", (s, s), _rgb(colors["night"]))
    draw = ImageDraw.Draw(im)
    r = s * 0.34
    c = s / 2
    draw.ellipse([c - r, c - r, c + r, c + r], outline=_rgb(colors["accent-on-night"]), width=max(2, s // 64))
    font = load_font(display_font, round(s * (0.3 if len(letters) > 1 else 0.36)), weight=480, opsz=144)
    draw.text((c, c), letters, font=font, fill=_rgb(colors["text-on-night"]), anchor="mm")
    out.parent.mkdir(parents=True, exist_ok=True)
    im.resize((size, size), Image.Resampling.LANCZOS).save(out, "PNG", optimize=True)
    return out
