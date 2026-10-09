"""Brand kit → CSS custom properties + @font-face rules (docs/ARCHITECTURE.md "Site rendering API").

Contract tokens (`--v-accent --v-accent-2 --v-ink --v-paper --v-night --v-muted --v-font-*`) are
emitted as supplied; everything that is ever used as *text* gets a surface-specific variant whose
WCAG contrast is solved to ≥ 4.5:1 by moving its OKLCH lightness (hue kept, chroma clipped to the
sRGB gamut), e.g. `--v-accent-on-night` / `--v-accent-on-paper`. Chapters switch world with the
`v-surface-night` / `v-surface-paper` classes, which map these onto `--v-bg`, `--v-fg`, `--v-acc`….
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from vantage.config import BrandKit, FontSpec
from vantage.paths import RUNTIME_DIR

TEXT_CONTRAST = 4.5
_FONTS_DIR = RUNTIME_DIR / "fonts"
_DEFAULT_FALLBACK = FontSpec.model_fields["fallback"].default
_SERIF_FALLBACK = "Georgia, 'Times New Roman', serif"

RGB = tuple[float, float, float]


# --------------------------------------------------------------------------- #
# Color math: sRGB ↔ OKLab/OKLCH, WCAG 2.x relative luminance and contrast
# --------------------------------------------------------------------------- #


def hex_to_rgb(value: str) -> RGB:
    h = value.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))
    return r, g, b


def rgb_to_hex(rgb: RGB) -> str:
    return "#" + "".join(f"{round(min(max(c, 0.0), 1.0) * 255):02x}" for c in rgb)


def _linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gamma(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def luminance(color: str) -> float:
    r, g, b = (_linear(c) for c in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _oklab(rgb: RGB) -> RGB:
    r, g, b = (_linear(c) for c in rgb)
    l_ = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m_ = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s_ = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
        1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
        0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_,
    )


def _from_oklab(lab: RGB) -> RGB:
    L, a, b = lab
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    lin = (
        4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_,
        -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_,
        -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_,
    )
    return tuple(_gamma(c) if c >= 0 else -_gamma(-c) for c in lin)  # type: ignore[return-value]


def oklch(color: str) -> RGB:
    L, a, b = _oklab(hex_to_rgb(color))
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def from_oklch(L: float, C: float, h: float) -> str:
    """OKLCH → hex, reducing chroma until the color fits in sRGB (hue and lightness kept)."""

    def rgb(c: float) -> RGB:
        return _from_oklab((L, c * math.cos(math.radians(h)), c * math.sin(math.radians(h))))

    def fits(c: float) -> bool:
        return all(-1e-4 <= v <= 1 + 1e-4 for v in rgb(c))

    if not fits(C):
        lo, hi = 0.0, C
        for _ in range(24):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if fits(mid) else (lo, mid)
        C = lo
    return rgb_to_hex(rgb(C))


def mix(a: str, b: str, t: float) -> str:
    """`t` of the way from a to b, interpolated in OKLab (like CSS `color-mix(in oklab, …)`)."""
    la, lb = _oklab(hex_to_rgb(a)), _oklab(hex_to_rgb(b))
    return rgb_to_hex(_from_oklab(tuple(x + (y - x) * t for x, y in zip(la, lb, strict=True))))  # type: ignore[arg-type]


def ensure_contrast(fg: str, *backgrounds: str, target: float = TEXT_CONTRAST) -> str:
    """`fg` itself when it reaches `target` on every background, else the nearest OKLCH lightness that does."""
    fg = rgb_to_hex(hex_to_rgb(fg))

    def worst(color: str) -> float:
        return min(contrast(color, bg) for bg in backgrounds)

    if worst(fg) >= target:
        return fg
    L, C, h = oklch(fg)
    dark = max(luminance(bg) for bg in backgrounds) < 0.18
    bound = 1.0 if dark else 0.0
    if worst(from_oklch(bound, C, h)) < target:  # brand hue can't get there: fall back to white/black
        return "#ffffff" if dark else "#000000"
    lo, hi = L, bound  # lo fails, hi passes
    for _ in range(32):
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if worst(from_oklch(mid, C, h)) >= target else (mid, hi)
    return from_oklch(hi, C, h)


def _rgb_triplet(color: str) -> str:
    return " ".join(str(round(c * 255)) for c in hex_to_rgb(color))


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FontAsset:
    src: Path
    dest: str  # relative to the site root


@dataclass(frozen=True)
class _Face:
    family: str
    src: Path
    style: str
    weight: str
    unicode_range: str | None


@dataclass(frozen=True)
class Theme:
    css: str
    fonts: list[FontAsset]
    colors: dict[str, str] = field(default_factory=dict)  # resolved tokens (no "--v-" prefix)


@cache
def bundled_fonts() -> dict[str, Any]:
    return json.loads((_FONTS_DIR / "fonts.json").read_text(encoding="utf-8"))


def _faces(spec: FontSpec, styles: set[str], brand: BrandKit) -> list[_Face]:
    if spec.bundled:
        info = bundled_fonts().get(spec.bundled)
        if info is None:
            raise ValueError(f"brand.yaml: unknown bundled font {spec.bundled!r} (see `vantage fonts`)")
        return [
            _Face(spec.family, _FONTS_DIR / f["file"], f["style"], f["weight"], info.get("unicode_range"))
            for f in info["faces"]
            if f["style"] in styles
        ]
    faces = []
    for rel in spec.files:
        path = brand.resolve(rel)
        assert path is not None
        if brand.root is not None and not path.resolve().is_relative_to(brand.root.resolve()):
            raise ValueError(f"brand.yaml: font {rel!r} is outside the brand folder {brand.root}")
        if path.suffix.lower() != ".woff2":
            raise ValueError(f"brand.yaml: font {rel!r} is not a .woff2 file")
        if re.search(r'["\\\x00-\x1f\x7f]', path.name):  # it goes into url("…") in the CSS
            raise ValueError(f"brand.yaml: font {rel!r} has a quote or control character; rename the file")
        style = "italic" if "italic" in path.stem.lower() else spec.style
        if style in styles:
            faces.append(_Face(spec.family, path, style, spec.weight, None))
    return faces


def font_file(brand: BrandKit, role: str) -> Path:
    """The upright file of a typography role ("display" | "text" | "numeric"), for raster text."""
    spec = getattr(brand.typography, role) or brand.typography.text
    faces = _faces(spec, {"normal", "italic"}, brand)
    face = next((f for f in faces if f.style == "normal"), faces[0] if faces else None)
    if face is None or not face.src.is_file():  # no usable files: the bundled default for the role
        key = "fraunces" if role == "display" else "inter"
        return _FONTS_DIR / bundled_fonts()[key]["faces"][0]["file"]
    return face.src


def _family(name: str) -> str:
    """A family name as a CSS string: quotes, backslashes and control characters (\\n → \\a) escaped."""
    text = name.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + re.sub(r"[\x00-\x1f\x7f]", lambda m: f"\\{ord(m[0]):x} ", text) + '"'


def _stack(spec: FontSpec) -> str:
    fallback = spec.fallback
    info = bundled_fonts().get(spec.bundled or "", {})
    if fallback == _DEFAULT_FALLBACK and info.get("category") == "serif":
        fallback = _SERIF_FALLBACK
    return f"{_family(spec.family)}, {fallback}"


def _font_face(face: _Face, url: str) -> str:
    rules = [
        f"font-family:{_family(face.family)}",
        f'src:url("{url}") format("woff2")',
        f"font-weight:{face.weight}",
        f"font-style:{face.style}",
        "font-display:swap",
    ]
    if face.unicode_range:
        rules.append(f"unicode-range:{face.unicode_range}")
    return "@font-face{" + ";".join(rules) + "}"


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #


def palette(brand: BrandKit) -> dict[str, str]:
    """Every color token, solved for contrast on the surface it is used on."""
    c = brand.colors
    night, paper = rgb_to_hex(hex_to_rgb(c.night)), rgb_to_hex(hex_to_rgb(c.paper))
    accent = rgb_to_hex(hex_to_rgb(c.accent))
    accent_2 = rgb_to_hex(hex_to_rgb(c.accent_2 or c.accent))
    snow = ensure_contrast(paper, night, target=7.0)  # warm off-white text on night
    ink = ensure_contrast(c.ink, paper, target=7.0)
    night_2, paper_2 = mix(night, snow, 0.07), mix(paper, ink, 0.05)
    muted = ensure_contrast(c.muted or mix(ink, paper, 0.4), paper, paper_2)
    tokens = {
        "accent": accent,
        "accent-2": accent_2,
        "ink": rgb_to_hex(hex_to_rgb(c.ink)),
        "paper": paper,
        "night": night,
        "muted": muted,
        "night-2": night_2,
        "paper-2": paper_2,
        "text-on-night": snow,
        "text-on-paper": ink,
        "muted-on-night": ensure_contrast(mix(snow, night, 0.36), night, night_2),
        "muted-on-paper": muted,
        "accent-on-night": ensure_contrast(accent, night, night_2),
        "accent-on-paper": ensure_contrast(accent, paper, paper_2),
        "accent-2-on-night": ensure_contrast(accent_2, night, night_2),
        "accent-2-on-paper": ensure_contrast(accent_2, paper, paper_2),
        "rule-on-night": mix(night, snow, 0.2),
        "rule-on-paper": mix(paper, ink, 0.16),
        "on-accent": max((ink, "#ffffff", night), key=lambda t: contrast(t, accent)),
    }
    for name, value in c.extra.items():
        tokens[f"extra-{name}"] = rgb_to_hex(hex_to_rgb(value))
    return tokens


def theme_css(brand: BrandKit, *, font_prefix: str = "assets/fonts/", italic: bool = True) -> Theme:
    """CSS for a brand: `:root` tokens + @font-face for the faces the page uses.

    Faces: the display and numeric fonts in their own style, the text font plus its italic
    (for `<em>`; pass `italic=False` when the story has none, so the face isn't shipped).
    """
    colors = palette(brand)
    typo = brand.typography
    numeric = typo.numeric or typo.text
    roles: list[tuple[FontSpec, set[str]]] = [
        (typo.display, {typo.display.style}),
        (typo.text, {typo.text.style} | ({"italic"} if italic else set())),
        (numeric, {numeric.style}),
    ]
    faces: dict[tuple[str, Path], _Face] = {}  # roles may share a file under different family names
    for spec, styles in roles:
        for face in _faces(spec, styles, brand):
            faces.setdefault((face.family, face.src), face)
    fonts = list({f.src: FontAsset(f.src, font_prefix + f.src.name) for f in faces.values()}.values())
    decls = {f"--v-{name}": value for name, value in colors.items()}
    decls |= {
        "--v-night-rgb": _rgb_triplet(colors["night"]),
        "--v-paper-rgb": _rgb_triplet(colors["paper"]),
        "--v-font-display": _stack(typo.display),
        "--v-font-text": _stack(typo.text),
        "--v-font-numeric": _stack(numeric) if typo.numeric else "var(--v-font-text)",
        "--v-display-style": typo.display.style,
        "--v-display-features": typo.display.features or "normal",
        "--v-text-features": typo.text.features or "normal",
        "--v-numeric-features": numeric.features or "normal",
        "color-scheme": "light" if brand.theme == "light" else "dark",
    }
    root = ":root{" + ";".join(f"{k}:{v}" for k, v in decls.items()) + "}"
    css = "\n".join([root, *(_font_face(face, font_prefix + face.src.name) for face in faces.values())])
    return Theme(css=css + "\n", fonts=fonts, colors=colors)
