"""Theme: WCAG contrast solving in OKLCH, surface tokens, and @font-face for the faces actually used."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vantage.config import BrandKit
from vantage.site.theme import contrast, ensure_contrast, mix, oklch, palette, theme_css

TEXT_TOKENS = {
    "text-on-night": ("night", "night-2"),
    "muted-on-night": ("night", "night-2"),
    "accent-on-night": ("night", "night-2"),
    "accent-2-on-night": ("night", "night-2"),
    "text-on-paper": ("paper", "paper-2"),
    "muted-on-paper": ("paper", "paper-2"),
    "accent-on-paper": ("paper", "paper-2"),
    "accent-2-on-paper": ("paper", "paper-2"),
}
BRANDS = [
    {},  # engine defaults: gold on near-black
    {"accent": "#324A84", "night": "#101820", "paper": "#ffffff", "ink": "#222222"},  # navy: fails on night
    {"accent": "#ffe14d", "accent_2": "#00a0a0", "paper": "#fafafa"},  # yellow: fails on paper
    {"accent": "#777777", "night": "#3a3a3a", "paper": "#c8c8c8", "ink": "#5a5a5a"},  # everything muddy
    {"accent": "#e07a3f", "muted": "#cccccc"},  # a brand muted that is too light for paper
]


def _brand(colors: dict[str, str], **kw: object) -> BrandKit:
    return BrandKit.model_validate({"name": "Test", "colors": colors, **kw})


def test_contrast_reference_values():
    assert contrast("#000000", "#ffffff") == pytest.approx(21.0)
    assert contrast("#777777", "#777777") == pytest.approx(1.0)
    assert contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)  # the classic AA grey


def test_ensure_contrast_moves_lightness_only_as_needed():
    assert ensure_contrast("#ffffff", "#000000") == "#ffffff"  # already fine: untouched
    fixed = ensure_contrast("#324a84", "#101820")
    assert contrast(fixed, "#101820") >= 4.5
    assert abs(oklch(fixed)[2] - oklch("#324a84")[2]) < 6  # same hue, just lighter
    assert oklch(fixed)[0] > oklch("#324a84")[0]
    darker = ensure_contrast("#ffe14d", "#fafafa")
    assert contrast(darker, "#fafafa") >= 4.5 and oklch(darker)[0] < oklch("#ffe14d")[0]
    assert contrast(ensure_contrast("#808080", "#7a7a7a", "#ffffff", target=3), "#7a7a7a") >= 3


@pytest.mark.parametrize("colors", BRANDS)
def test_every_text_token_meets_aa(colors):
    tokens = palette(_brand(colors))
    for token, surfaces in TEXT_TOKENS.items():
        for surface in surfaces:
            assert contrast(tokens[token], tokens[surface]) >= 4.5, (token, surface, tokens[token])
    assert contrast(tokens["on-accent"], tokens["accent"]) >= 3


def test_supplied_colors_are_kept_verbatim():
    tokens = palette(_brand({"accent": "#E07A3F", "night": "#0B1414", "extra": {"lake": "#1F5F63"}}))
    assert (
        tokens["accent"] == "#e07a3f" and tokens["night"] == "#0b1414" and tokens["extra-lake"] == "#1f5f63"
    )
    assert tokens["accent-2"] == tokens["accent"]  # no accent_2: the accent stands in
    assert mix("#000000", "#ffffff", 0) == "#000000" and mix("#000000", "#ffffff", 1) == "#ffffff"


def test_css_tokens_and_bundled_fonts():
    theme = theme_css(BrandKit(name="Test"))
    root = re.search(r":root\{(.*?)\}", theme.css).group(1)
    for name in (
        "accent",
        "accent-2",
        "ink",
        "paper",
        "night",
        "muted",
        "font-display",
        "font-text",
        "font-numeric",
    ):
        assert f"--v-{name}:" in root
    assert "--v-accent-on-night:" in root and "--v-night-rgb:11 12 11" in root and "color-scheme:dark" in root
    assert '--v-font-display:"Fraunces", Georgia' in root  # serif fallback for a serif face
    assert "--v-font-numeric:var(--v-font-text)" in root
    faces = re.findall(r"@font-face\{(.*?)\}", theme.css)
    assert len(faces) == 3  # Fraunces upright, Inter upright + italic
    assert all(
        "font-display:swap" in f and 'format("woff2")' in f and "unicode-range:U+0000-00FF" in f
        for f in faces
    )
    assert 'font-family:"Inter";src:url("assets/fonts/inter-latin-opsz-italic.woff2")' in theme.css
    assert "font-weight:100 900" in theme.css
    assert [f.dest for f in theme.fonts] == [
        "assets/fonts/fraunces-latin-opsz-normal.woff2",
        "assets/fonts/inter-latin-opsz-normal.woff2",
        "assets/fonts/inter-latin-opsz-italic.woff2",
    ]
    assert all(f.src.is_file() for f in theme.fonts)
    upright = theme_css(BrandKit(name="Test"), italic=False, font_prefix="fonts/")
    assert len(upright.fonts) == 2 and "italic" not in upright.css and 'url("fonts/' in upright.css


def test_client_fonts_numeric_face_and_light_theme(tmp_path: Path):
    (tmp_path / "fonts").mkdir()
    for name in ("Brand-Regular.woff2", "Brand-Italic.woff2"):
        (tmp_path / "fonts" / name).write_bytes(b"wOF2")
    brand = BrandKit.model_validate({
        "name": "Client",
        "theme": "light",
        "typography": {
            "display": {"family": "Brand Serif", "files": ["fonts/Brand-Regular.woff2", "fonts/Brand-Italic.woff2"],
                        "weight": "300 700", "fallback": "Georgia, serif", "features": "'ss01' 1"},
            "text": {"family": "Inter", "bundled": "inter"},
            "numeric": {"family": "Public Sans", "bundled": "public-sans"},
        },
    })  # fmt: skip
    brand.root = tmp_path
    theme = theme_css(brand)
    assert "color-scheme:light" in theme.css
    assert '--v-font-display:"Brand Serif", Georgia, serif' in theme.css
    assert "--v-display-features:'ss01' 1" in theme.css
    assert '--v-font-numeric:"Public Sans"' in theme.css
    assert (
        'font-family:"Brand Serif";src:url("assets/fonts/Brand-Regular.woff2") format("woff2");font-weight:300 700'
        in theme.css
    )
    assert "Brand-Italic" not in theme.css  # the display face is only used upright
    assert [f.dest.rsplit("/", 1)[1] for f in theme.fonts] == [
        "Brand-Regular.woff2", "inter-latin-opsz-normal.woff2", "inter-latin-opsz-italic.woff2",
        "public-sans-latin-wght-normal.woff2",
    ]  # fmt: skip


def test_unknown_bundled_font_is_an_error():
    brand = BrandKit.model_validate(
        {"name": "X", "typography": {"text": {"family": "Nope", "bundled": "nope"}}}
    )
    with pytest.raises(ValueError, match="unknown bundled font 'nope'"):
        theme_css(brand)
