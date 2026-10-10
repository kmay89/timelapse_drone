"""Where things live: repo root, projects root, dist root, and project lookup."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = PACKAGE_DIR / "runtime"
TEMPLATE_PROJECT_DIR = PACKAGE_DIR / "templates" / "project"


PUBLIC_PROJECTS = ("demo-lakeside",)  # the only projects this public repo holds (fictional)


def public_checkout() -> Path | None:
    """The public Vantage source checkout this package runs from (editable install), else None."""
    candidate = PACKAGE_DIR.parent.parent
    return candidate if (candidate / "pyproject.toml").exists() else None


def repo_root() -> Path:
    """The checkout this package was installed from (editable install), else CWD."""
    return public_checkout() or Path.cwd()


def exposed_in_public_checkout(path: Path) -> bool:
    """True if `path` lies in the public checkout's own git work tree, where one `git add -A` would
    publish it (CLAUDE.md invariant 1). The fictional demo projects are exempt, and so is a separate
    repository cloned inside the checkout (such as the private projects repo): it has its own `.git`."""
    root = public_checkout()
    if root is None:
        return False
    root, path = root.resolve(), path.expanduser().resolve()
    if not path.is_relative_to(root):
        return False
    parts = path.relative_to(root).parts
    if parts[:1] == ("projects",) and parts[1:2] and parts[1] in PUBLIC_PROJECTS:
        return False
    return not any((root.joinpath(*parts[:i]) / ".git").exists() for i in range(1, len(parts) + 1))


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
