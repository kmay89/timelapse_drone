"""`vantage new`: create a project folder from the bundled template.

Template files (src/vantage/templates/project/) use ``{{slug}}``, ``{{title}}``
and ``{{initial}}`` placeholders, escaped for the file they land in (YAML
double-quoted strings, SVG/XML text, or verbatim). The rendered YAML is
validated against the config models before anything is written.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import yaml

from vantage.config import SLUG_RE, BrandKit, ProjectConfig, Story
from vantage.paths import TEMPLATE_PROJECT_DIR

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_XML_SUFFIXES = frozenset({".svg", ".xml", ".html"})


def _escape(value: str, suffix: str) -> str:
    if suffix in {".yaml", ".yml"}:  # placeholders sit inside "double quotes" in the YAML templates
        return value.replace("\\", "\\\\").replace('"', '\\"')
    if suffix in _XML_SUFFIXES:
        return html.escape(value)
    return value


def render_template(text: str, values: dict[str, str], suffix: str = "") -> str:
    """Substitute ``{{name}}`` placeholders; unknown names are left untouched."""
    return _PLACEHOLDER.sub(
        lambda m: _escape(values[m.group(1)], suffix) if m.group(1) in values else m.group(0), text
    )


def default_title(slug: str) -> str:
    return " ".join(word.capitalize() for word in slug.split("-"))


def create_project(dest: Path, slug: str, title: str | None = None) -> Path:
    """Write a new project into `dest` (created; must be empty or absent) and return its path."""
    if not SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r}: use lowercase letters/digits separated by single hyphens")
    dest = dest.expanduser().resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise FileExistsError(f"{dest} already exists and is not empty")
    title = " ".join((title or default_title(slug)).split())
    initial = next((ch.upper() for ch in title if ch.isalnum()), "V")
    values = {"slug": slug, "title": title, "initial": initial}

    files: dict[Path, str] = {}
    for src in sorted(TEMPLATE_PROJECT_DIR.rglob("*")):
        if src.is_file() and "__pycache__" not in src.parts:
            rel = src.relative_to(TEMPLATE_PROJECT_DIR)
            files[rel] = render_template(src.read_text(encoding="utf-8"), values, src.suffix.lower())

    config = ProjectConfig.model_validate(yaml.safe_load(files[Path("project.yaml")]))
    Story.model_validate(yaml.safe_load(files[Path("story.yaml")]))
    BrandKit.model_validate(yaml.safe_load(files[Path(config.brand)]))

    for rel, text in files.items():
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    (dest / "footage").mkdir(exist_ok=True)
    return dest
