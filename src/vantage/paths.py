"""Where things live: repo root, projects root, dist root, and project lookup."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = PACKAGE_DIR / "runtime"
TEMPLATE_PROJECT_DIR = PACKAGE_DIR / "templates" / "project"


def repo_root() -> Path:
    """The checkout this package was installed from (editable install), else CWD."""
    candidate = PACKAGE_DIR.parent.parent
    if (candidate / "pyproject.toml").exists():
        return candidate
    return Path.cwd()


def projects_root(override: Path | None = None) -> Path:
    if override:
        return override.expanduser().resolve()
    env = os.environ.get("VANTAGE_PROJECTS")
    if env:
        return Path(env).expanduser().resolve()
    return repo_root() / "projects"


def dist_root(override: Path | None = None) -> Path:
    if override:
        return override.expanduser().resolve()
    env = os.environ.get("VANTAGE_DIST")
    if env:
        return Path(env).expanduser().resolve()
    return repo_root() / "dist"


def find_project(ref: str | Path, projects_dir: Path | None = None) -> Path:
    """Resolve a project reference: an existing path, or a slug under the projects root."""
    p = Path(ref).expanduser()
    if (p / "project.yaml").exists():
        return p.resolve()
    candidate = projects_root(projects_dir) / str(ref)
    if (candidate / "project.yaml").exists():
        return candidate.resolve()
    raise FileNotFoundError(
        f"no project {str(ref)!r}: expected {p}/project.yaml or {candidate}/project.yaml "
        "(set VANTAGE_PROJECTS or --projects-dir for projects outside this repo)"
    )


def project_dist(slug: str, dist_dir: Path | None = None) -> Path:
    return dist_root(dist_dir) / slug
