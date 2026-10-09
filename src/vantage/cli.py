"""`vantage` command line. Every command is a thin wrapper over a module function.

Pipeline modules are imported inside the commands, so `vantage --help` is fast
and each command only needs the pieces it uses.
"""

from __future__ import annotations

import functools
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, BinaryIO, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer

from vantage import __version__, log, paths

if TYPE_CHECKING:
    from vantage.config import Project

F = TypeVar("F", bound=Callable[..., Any])

app = typer.Typer(
    name="vantage",
    help="Cinematic, iPhone-first progress stories from repeat drone flights.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


@dataclass
class _Options:
    projects_dir: Path | None = None
    dist_dir: Path | None = None
    verbose: bool = False


_opts = _Options()

ProjectArg = Annotated[
    str,
    typer.Argument(
        metavar="PROJECT", help="Project slug (under the projects root) or path to a project folder."
    ),
]
ForceOpt = Annotated[
    bool, typer.Option("--force", help="Re-sample every source instead of reusing candidates.")
]
ReleaseOpt = Annotated[
    bool,
    typer.Option(
        "--release", help="Release build: fail on draft: true, unknown or unapproved {fact:…} tokens."
    ),
]


# --------------------------------------------------------------------------- #
# Plumbing
# --------------------------------------------------------------------------- #

_YAML_FILES = {
    "ProjectConfig": "project.yaml",
    "Story": "story.yaml",
    "BrandKit": "brand/brand.yaml",
    "Facts": "facts.yaml",
}


def _friendly(exc: BaseException) -> str | None:
    """A one-message explanation for expected failures; None for bugs (which keep their traceback)."""
    import yaml
    from pydantic import ValidationError

    from vantage.media.ffmpeg import FFmpegError

    if isinstance(exc, ValidationError):
        where = _YAML_FILES.get(exc.title, exc.title)
        lines = [f"{'.'.join(map(str, e['loc'])) or '(root)'}: {e['msg']}" for e in exc.errors()]
        return f"invalid {where}:\n  " + "\n  ".join(lines)
    if isinstance(exc, yaml.YAMLError):
        return f"YAML syntax error: {exc}"
    if isinstance(exc, OSError | ValueError | FFmpegError):
        return str(exc) or type(exc).__name__
    return None


def _command(name: str) -> Callable[[F], F]:
    """Register a command whose expected failures end in a friendly '✗ …' line and exit code 1."""

    def register(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                message = _friendly(exc)
                if message is None:
                    raise
                if _opts.verbose:
                    traceback.print_exc()
                log.fail(message)
                raise typer.Exit(1) from None

        app.command(name)(wrapper)
        return fn

    return register


@app.callback()
def main(
    projects_dir: Annotated[
        Path | None,
        typer.Option(
            "--projects-dir",
            help="Where project slugs are looked up (default: $VANTAGE_PROJECTS or ./projects).",
        ),
    ] = None,
    dist_dir: Annotated[
        Path | None, typer.Option("--dist-dir", help="Output root (default: $VANTAGE_DIST or ./dist).")
    ] = None,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging and full tracebacks.")
    ] = False,
) -> None:
    """Cinematic, iPhone-first progress stories from repeat drone flights."""
    _opts.projects_dir, _opts.dist_dir, _opts.verbose = projects_dir, dist_dir, verbose
    log.set_verbose(verbose or bool(os.environ.get("VANTAGE_VERBOSE")))


def _load(ref: str | Path) -> Project:
    from vantage.config import load_project

    project = load_project(paths.find_project(ref, _opts.projects_dir))
    if paths.exposed_in_public_checkout(project.root):
        log.warn(
            f"{_show(project.root)} is inside the public Vantage checkout: move client projects to the "
            "private projects repo (VANTAGE_PROJECTS) before anything is committed"
        )
    return project


def _dist(project: Project) -> Path:
    return paths.project_dist(project.slug, _opts.dist_dir)


def _show(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def _has_media(folder: Path) -> bool:
    from vantage.ingest.catalog import has_media

    return has_media(folder)


# --------------------------------------------------------------------------- #
# Pipeline steps (shared by the single-step commands, `process`, `all` and `demo`)
# --------------------------------------------------------------------------- #


def _ingest(project: Project, *, force: bool = False) -> None:
    from vantage.ingest.catalog import CATALOG_NAME, build_catalog

    with log.step("ingest"):
        catalog = build_catalog(project, force=force)
    log.ok(
        f"{len(catalog.sources)} sources over {len(catalog.dates())} dates, {len(catalog.candidates)} candidates"
        f" → {_show(project.work_dir / CATALOG_NAME)}; contact sheets in {_show(project.work_dir / 'contact')}"
    )


def _select(project: Project) -> None:
    from vantage.process.select import select_frames

    with log.step("select"):
        selection = select_frames(project)
    picks = ", ".join(f"{vid} {len(v.picks)}" for vid, v in selection.vantages.items())
    log.ok(f"picked frames per vantage ({picks}) → {_show(project.work_dir / 'selection.json')}")


def _align(project: Project) -> None:
    from vantage.process.masters import make_masters

    with log.step("align"):
        index = make_masters(project)
    captures = [c for v in index.vantages.values() for c in v.captures]
    flagged = sum(not c.align.ok for c in captures)
    log.ok(f"{len(captures)} masters for {len(index.vantages)} vantage(s) → {_show(project.masters_dir)}")
    if flagged:
        log.warn(f"{flagged} capture(s) aligned poorly; check {_show(project.work_dir / 'review')}")


def _process(project: Project, *, force: bool = False) -> None:
    _ingest(project, force=force)
    _select(project)
    _align(project)


def _build(project: Project, out: Path | None = None, *, release: bool = False) -> Path:
    from vantage.site.build import build_site

    out = out or _dist(project) / "site"
    with log.step("build site"):
        index = build_site(project, out, **({"release": True} if release else {}))
    log.ok(f"site → {_show(index)} ({_human(_size(out))})")
    return index


def _film(project: Project) -> list[Path]:
    from vantage.film.render import render_film

    with log.step("render films"):
        films = render_film(project, _dist(project) / "film")
    log.ok("films → " + ", ".join(f"{_show(f)} ({_human(_size(f))})" for f in films))
    return films


def _package(project: Project, *, release: bool = False) -> dict[str, Path]:
    from vantage.site.package import package_project

    with log.step("package"):
        packages = package_project(project, _dist(project), **({"release": True} if release else {}))
    log.ok("packages → " + ", ".join(f"{_show(p)} ({_human(_size(p))})" for p in packages.values()))
    return packages


def _all(project: Project, *, film: bool = True, force: bool = False, release: bool = False) -> None:
    if _has_media(project.footage_dir):
        _process(project, force=force)
    elif (project.masters_dir / "index.json").is_file():
        log.warn(f"no footage in {_show(project.footage_dir)}; building from the committed masters")
    else:
        raise FileNotFoundError(
            f"no footage in {project.footage_dir} and no masters/index.json to build from"
        )
    index = _build(project, release=release)
    outputs: list[tuple[str, Path]] = [("site", index.parent)]
    if film and project.config.output.film.enabled:
        outputs += [("film", f) for f in _film(project)]
    elif film:
        log.info("films disabled in project.yaml (output.film.enabled: false)")
    outputs += [(name.replace("_", " "), p) for name, p in _package(project, release=release).items()]
    width = max(len(_show(p)) for _, p in outputs)
    for label, path in outputs:
        typer.echo(f"  {label:<12} {_show(path):<{width}}  {_human(_size(path)):>9}")
    log.ok(f"{project.slug} ready in {_show(_dist(project))}")


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

_CAPTURE_REF = re.compile(r"^(earliest|latest|#\d+|\d{4}-\d{2}-\d{2})$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _bundled_fonts() -> dict[str, Any] | None:
    manifest = paths.RUNTIME_DIR / "fonts" / "fonts.json"
    return json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else None


def _check_brand(project: Project, errors: list[str], warnings: list[str]) -> None:
    brand = project.brand
    files: list[tuple[str, str | None]] = [
        ("logos.primary", brand.logos.primary),
        ("logos.on_dark", brand.logos.on_dark),
        ("logos.mark", brand.logos.mark),
    ]
    for i, partner in enumerate(brand.partners):
        files += [
            (f"partners[{i}].logo", partner.logo),
            (f"partners[{i}].logo_on_dark", partner.logo_on_dark),
        ]
    fonts = _bundled_fonts()
    for role in ("display", "text", "numeric"):
        spec = getattr(brand.typography, role)
        if spec is None:
            continue
        files += [(f"typography.{role}.files[{j}]", f) for j, f in enumerate(spec.files)]
        if spec.bundled and fonts is not None and spec.bundled not in fonts:
            errors.append(
                f"brand.yaml typography.{role}: unknown bundled font {spec.bundled!r} (see `vantage fonts`)"
            )
        if not spec.bundled and not spec.files:
            warnings.append(
                f"brand.yaml typography.{role}: {spec.family!r} has no files; viewers see the fallback"
            )
        warnings += [
            f"brand.yaml typography.{role}: {f} is not .woff2"
            for f in spec.files
            if not f.lower().endswith(".woff2")
        ]
    for label, rel in files:
        path = brand.resolve(rel)
        if path is not None and not path.is_file():
            errors.append(f"brand.yaml {label}: missing file {_show(path)}")
        elif path is not None and path.suffix.lower() == ".svg":
            _check_svg(path, f"brand.yaml {label}", errors, warnings)
    if not (brand.logos.primary or brand.logos.on_dark):
        warnings.append("brand.yaml has no logo")


def _check_svg(path: Path, label: str, errors: list[str], warnings: list[str]) -> None:
    """The build publishes a sanitized copy of every SVG logo: say now what it will refuse or remove."""
    from vantage.site.build import sanitize_svg

    try:
        _, removed = sanitize_svg(path.read_bytes(), f"{label} ({_show(path)})")
    except ValueError as exc:
        errors.append(str(exc))
        return
    if removed:
        warnings.append(f"{label}: the published copy leaves out {', '.join(sorted(set(removed)))}")


def _check_facts(project: Project, errors: list[str], warnings: list[str]) -> None:
    from vantage.config import FACT_TOKEN_RE
    from vantage.site.facts import BRAND_TEXT, check_facts

    unknown, unreleasable = check_facts(project)
    errors += unknown
    if unreleasable:
        warnings.append(
            f"{len(unreleasable)} fact(s) not releasable yet (draft builds mark them 'Unverified'; "
            "--release refuses them): " + "; ".join(unreleasable)
        )
    brand = project.brand.model_dump(mode="json", exclude={*BRAND_TEXT, "voice"})  # voice is never printed
    errors += [
        f"brand.yaml {key}: {{fact:…}} tokens only resolve in {', '.join(BRAND_TEXT)}"
        for key, value in brand.items()
        if FACT_TOKEN_RE.search(json.dumps(value))
    ]


def _check_story(project: Project, errors: list[str], warnings: list[str]) -> None:
    from vantage.config import (
        CompareChapter,
        GalleryChapter,
        HeroChapter,
        ScrubChapter,
        TimelineChapter,
        VideoChapter,
    )

    story = project.story
    vantage_ids = {v.id for v in story.vantages}
    refs: list[tuple[str, str | None]] = []
    frames: list[tuple[str, str]] = []
    for v in story.vantages:
        if v.reference:
            frames.append((f"vantage {v.id} reference", v.reference.source))
        for date, pick in v.picks.items():
            frames.append((f"vantage {v.id} pick {date}", pick.source))
            if not _ISO_DATE.match(date):
                errors.append(f"story.yaml vantage {v.id!r}: pick key {date!r} is not a YYYY-MM-DD date")
    seen: set[str] = set()
    for i, ch in enumerate(story.chapters):
        name = ch.id or f"#{i} ({ch.type})"
        if ch.id:
            if ch.id in seen:
                errors.append(f"story.yaml: duplicate chapter id {ch.id!r}")
            seen.add(ch.id)
        if isinstance(ch, HeroChapter):
            refs.append((f"{name} capture", ch.capture))
            if ch.video:
                frames.append((f"chapter {name} video", ch.video.source))
        elif isinstance(ch, ScrubChapter | CompareChapter):
            ends = (ch.from_, ch.to) if isinstance(ch, ScrubChapter) else (ch.before, ch.after)
            refs += [(f"{name} range", r) for r in ends]
            refs += [(f"{name} step {j}", s.capture) for j, s in enumerate(ch.steps)]
            for h in ch.hotspots:
                refs += [(f"{name} hotspot {h.id}", h.from_), (f"{name} hotspot {h.id}", h.to)]
        elif isinstance(ch, VideoChapter):
            frames.append((f"chapter {name} clip", ch.clip.source))
        elif isinstance(ch, GalleryChapter):
            refs += [(f"{name} gallery", r) for r in ch.captures]
            errors += [
                f"story.yaml {name}: gallery image {img.file!r} not found in the project folder"
                for img in ch.images
                if not (project.root / img.file).is_file()
            ]
        elif isinstance(ch, TimelineChapter):
            for item in ch.items:
                refs.append((f"{name} item {item.title!r}", item.capture))
                if item.vantage and item.vantage not in vantage_ids:
                    errors.append(
                        f"story.yaml {name}: timeline item references unknown vantage {item.vantage!r}"
                    )
    errors += [
        f"story.yaml {where}: capture reference {ref!r} is not earliest/latest/#N/YYYY-MM-DD"
        for where, ref in refs
        if ref is not None and not _CAPTURE_REF.match(ref)
    ]
    dates = [c.date for c in story.captures]
    if len(dates) != len(set(dates)):
        warnings.append("story.yaml: captures lists the same date more than once")
    if _has_media(project.footage_dir):
        errors += [
            f"story.yaml {where}: {src!r} not found under {_show(project.footage_dir)}"
            for where, src in frames
            if not (project.footage_dir / src).is_file()
        ]
    elif frames:
        warnings.append(f"footage not present; skipped checking {len(frames)} frame reference(s)")


def _check_outputs(project: Project, warnings: list[str]) -> None:
    from vantage.models import MastersIndex

    index_path = project.masters_dir / "index.json"
    if not index_path.is_file():
        warnings.append("no masters yet (run `vantage process` once footage is in place)")
        return
    masters = MastersIndex.load(index_path)
    warnings += [
        f"vantage {v.id!r} has no masters yet"
        for v in project.story.vantages
        if v.id not in masters.vantages or not masters.vantages[v.id].captures
    ]


def check_project(project: Project) -> tuple[list[str], list[str]]:
    """Cross-checks beyond the YAML schema: (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    if project.root.name != project.slug:
        warnings.append(f"folder name {project.root.name!r} differs from slug {project.slug!r}")
    try:
        ZoneInfo(project.config.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(
            f"project.yaml timezone: {project.config.timezone!r} is not an IANA zone like America/New_York"
        )
    _check_brand(project, errors, warnings)
    _check_story(project, errors, warnings)
    _check_facts(project, errors, warnings)
    _check_outputs(project, warnings)
    todos = {
        rel: (project.root / rel).read_text(encoding="utf-8").count("TODO")
        for rel in ("project.yaml", "story.yaml", project.config.brand, "facts.yaml")
        if (project.root / rel).is_file()
    }
    if sum(todos.values()):
        warnings.append("TODO markers left: " + ", ".join(f"{k} {n}" for k, n in todos.items() if n))
    return errors, warnings


# --------------------------------------------------------------------------- #
# Preview server
# --------------------------------------------------------------------------- #

_RANGE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$")


def parse_byte_range(header: str, size: int) -> tuple[int, int] | None:
    """A single 'bytes=a-b' / 'bytes=a-' / 'bytes=-n' range → inclusive (start, end).

    None means "ignore the header and send the whole file" (malformed or multi-range);
    an unsatisfiable range comes back with start >= size.
    """
    m = _RANGE.match(header)
    if not m or not any(m.groups()):
        return None
    first, last = m.groups()
    if not first:
        n = int(last)
        return (max(size - n, 0), size - 1) if n else (size, size - 1)
    start = int(first)
    if last and int(last) < start:
        return None
    return start, min(int(last), size - 1) if last else size - 1


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """Static files with byte ranges (Safari won't play <video> without them) and correct MIME types."""

    extensions_map = {  # noqa: RUF012
        **SimpleHTTPRequestHandler.extensions_map,
        ".avif": "image/avif",
        ".webp": "image/webp",
        ".webmanifest": "application/manifest+json",
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".woff2": "font/woff2",
        ".js": "text/javascript",
        ".json": "application/json",
        ".svg": "image/svg+xml",
    }
    protocol_version = "HTTP/1.1"  # keep-alive: a story page pulls dozens of images
    _span: tuple[int, int] | None = None

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self) -> BinaryIO | None:
        self._span = None
        header = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not header or os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")  # noqa: SIM115 - handed to copyfile, closed by do_GET
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        size = os.fstat(f.fileno()).st_size
        span = parse_byte_range(header, size)
        if span is None:
            f.close()
            return super().send_head()
        start, end = span
        if start >= size:
            f.close()
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        self._span = span
        return f

    def copyfile(self, source: Any, outputfile: Any) -> None:
        if self._span is None:
            return super().copyfile(source, outputfile)
        remaining = self._span[1] - self._span[0] + 1
        while remaining > 0 and (chunk := source.read(min(1 << 16, remaining))):
            outputfile.write(chunk)
            remaining -= len(chunk)
        return None

    def log_message(self, format: str, *args: Any) -> None:
        log.debug(f"{self.address_string()} {format % args}")


class _PreviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        if not isinstance(sys.exc_info()[1], ConnectionError):  # players abort range requests all the time
            super().handle_error(request, client_address)


def make_server(root: Path, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    """A threaded static server for `root` (port 0 picks a free port)."""
    return _PreviewServer((host, port), functools.partial(RangeRequestHandler, directory=str(root)))


def _lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outbound interface
            return str(s.getsockname()[0])
        except OSError:
            return "127.0.0.1"


# --------------------------------------------------------------------------- #
# Doctor
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _tool_version(binary: str, flag: str = "-version") -> str:
    try:
        out = subprocess.run([binary, flag], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return "?"
    first = out.splitlines()[0] if out else ""
    m = re.search(r"version\s+(\S+)", first)
    return m.group(1) if m else first or "?"


def _module_check(module: str, dist: str, *, required: bool = True, why: str = "") -> _Check:
    try:
        importlib.import_module(module)
    except ImportError:
        return _Check(dist, False, f"not installed{' — ' + why if why else ''}", required)
    try:
        version = importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        version = "?"
    return _Check(dist, True, version, required)


def doctor_checks() -> list[_Check]:
    """Environment checks: hard requirements first, then optional extras."""
    checks = [_Check("python", sys.version_info >= (3, 11), f"{platform.python_version()} (needs ≥ 3.11)")]
    for tool in ("ffmpeg", "ffprobe"):
        where = shutil.which(tool)
        checks.append(
            _Check(tool, bool(where), f"{_tool_version(where)}  {where}" if where else "not on PATH")
        )
    if shutil.which("ffmpeg"):
        from vantage.media.ffmpeg import encoders

        try:
            available = encoders()
        except (OSError, subprocess.SubprocessError):
            available = frozenset()
        for enc, required, why in (
            ("libx264", True, "H.264 films and web video"),
            ("libx265", False, "HEVC video for iPhone (smaller files)"),
            ("libsvtav1", False, "AV1 video"),
        ):
            checks.append(
                _Check(enc, enc in available, why if enc in available else f"missing — {why}", required)
            )
    for module, dist in (
        ("numpy", "numpy"),
        ("cv2", "opencv-python-headless"),
        ("PIL", "pillow"),
        ("pydantic", "pydantic"),
        ("yaml", "pyyaml"),
        ("jinja2", "jinja2"),
        ("markdown_it", "markdown-it-py"),
    ):
        checks.append(_module_check(module, dist))
    if importlib.util.find_spec("PIL"):
        from PIL import features

        for fmt in ("avif", "webp"):
            ok = bool(features.check(fmt))
            checks.append(
                _Check(f"pillow {fmt}", ok, "supported" if ok else "missing — images fall back", False)
            )
    checks.append(
        _module_check("anthropic", "anthropic", required=False, why="`uv sync --extra llm` for captions")
    )
    node = shutil.which("node")
    checks.append(
        _Check(
            "node",
            bool(node),
            _tool_version(node, "--version") if node else "not found — web tests only",
            False,
        )
    )
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    checks.append(
        _Check("ANTHROPIC_API_KEY", has_key, "set" if has_key else "not set — captions disabled", False)
    )
    return checks


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@_command("new")
def new_cmd(
    slug: Annotated[
        str, typer.Argument(help="Project id: lowercase words joined by hyphens, e.g. riverside-park.")
    ],
    title: Annotated[
        str | None, typer.Option("--title", "-t", help="Display title (default: from slug).")
    ] = None,
    parent: Annotated[
        str | None,
        typer.Option("--dir", "-d", help="Folder to create <slug>/ in (default: the projects root)."),
    ] = None,
    public: Annotated[
        bool,
        typer.Option("--public", help="Allow a folder inside this public checkout (fictional demos only)."),
    ] = False,
) -> None:
    """Scaffold a new project from the template (outside this public checkout)."""
    from vantage.scaffold import create_project

    if parent is not None and not parent.strip():  # `--dir "$VANTAGE_PROJECTS"` with the variable unset
        raise ValueError("--dir is empty: set VANTAGE_PROJECTS or name the folder to create the project in")
    root = Path(parent).expanduser().resolve() if parent else paths.projects_root(_opts.projects_dir)
    dest = create_project(root / slug, slug, title, public=public)
    ref = slug if root == paths.projects_root(_opts.projects_dir) else _show(dest)
    log.ok(f"created {_show(dest)}")
    typer.echo(
        f"next: replace the placeholder logos in {_show(dest / 'brand')}, copy flights into "
        f"{_show(dest / 'footage')}/YYYY-MM-DD/, then run `vantage ingest {ref}` (see README.md)"
    )


@_command("validate")
def validate_cmd(project: ProjectArg) -> None:
    """Load and cross-check YAML, brand files and capture references."""
    proj = _load(project)
    errors, warnings = check_project(proj)
    for w in warnings:
        log.warn(w)
    for e in errors:
        log.fail(e)
    if errors:
        log.fail(f"{proj.slug}: {len(errors)} error(s), {len(warnings)} warning(s)")
        raise typer.Exit(1)
    log.ok(
        f"{proj.slug} is valid: {len(proj.story.vantages)} vantage(s), {len(proj.story.chapters)} chapters, "
        f"{len(warnings)} warning(s)"
    )


@_command("ingest")
def ingest_cmd(project: ProjectArg, force: ForceOpt = False) -> None:
    """Catalog footage, sample candidate frames and write contact sheets."""
    _ingest(_load(project), force=force)


@_command("select")
def select_cmd(project: ProjectArg) -> None:
    """Choose the best frame per vantage for every visit."""
    _select(_load(project))


@_command("align")
def align_cmd(project: ProjectArg) -> None:
    """Register, crop and color-match picks into masters/ (+ review sheets)."""
    _align(_load(project))


@_command("process")
def process_cmd(project: ProjectArg, force: ForceOpt = False) -> None:
    """ingest → select → align."""
    _process(_load(project), force=force)


@_command("build")
def build_cmd(
    project: ProjectArg,
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Output folder (default: dist/<slug>/site).")
    ] = None,
    release: ReleaseOpt = False,
) -> None:
    """Build the interactive site from masters + YAML."""
    _build(_load(project), out.expanduser().resolve() if out else None, release=release)


@_command("film")
def film_cmd(project: ProjectArg) -> None:
    """Render the MP4 time-lapse films (16:9, plus 9:16 when enabled)."""
    _film(_load(project))


@_command("package")
def package_cmd(project: ProjectArg, release: ReleaseOpt = False) -> None:
    """Single-file HTML + offline zip from the built site."""
    _package(_load(project), release=release)


@_command("all")
def all_cmd(
    project: ProjectArg,
    no_film: Annotated[bool, typer.Option("--no-film", help="Skip rendering the MP4 films.")] = False,
    force: ForceOpt = False,
    release: ReleaseOpt = False,
) -> None:
    """process → build → film → package."""
    _all(_load(project), film=not no_film, force=force, release=release)


@_command("preview")
def preview_cmd(
    project: ProjectArg,
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on.")] = 8000,
    host: Annotated[
        str, typer.Option("--host", help="Interface; 0.0.0.0 to open it from a phone.")
    ] = "127.0.0.1",
) -> None:
    """Serve the built site locally (with byte ranges, so Safari plays video)."""
    proj = _load(project)
    root = _dist(proj) / "site"
    if not (root / "index.html").is_file():
        raise FileNotFoundError(f"no built site at {_show(root)}; run `vantage build {project}` first")
    server = make_server(root, host, port)
    shown = "127.0.0.1" if host in ("", "0.0.0.0") else host
    log.ok(f"serving {_show(root)} at http://{shown}:{server.server_port}/  (Ctrl-C to stop)")
    if host in ("", "0.0.0.0"):
        log.info(f"on a phone on the same network: http://{_lan_ip()}:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        server.server_close()


@_command("demo")
def demo_cmd(
    fast: Annotated[bool, typer.Option("--fast", help="Smaller, quicker synthetic footage.")] = False,
    no_film: Annotated[bool, typer.Option("--no-film", help="Skip rendering the MP4 films.")] = False,
    regenerate: Annotated[
        bool, typer.Option("--regenerate", help="Re-render the synthetic footage.")
    ] = False,
    project: Annotated[
        Path | None,
        typer.Option("--project", help="Demo project folder (default: <projects>/demo-lakeside)."),
    ] = None,
) -> None:
    """Synthesize demo footage and run `all` on the demo-lakeside project."""
    from vantage.demo.synth import TRUTH_NAME, generate_demo_footage

    proj = _load(project or paths.projects_root(_opts.projects_dir) / "demo-lakeside")
    footage = proj.footage_dir
    if regenerate or not _has_media(footage):
        (footage / TRUTH_NAME).unlink(missing_ok=True)
        generate_demo_footage(footage, fast=fast)
    else:
        log.info(f"using existing demo footage in {_show(footage)} (--regenerate to re-render)")
    _all(proj, film=not no_film)


@_command("caption")
def caption_cmd(project: ProjectArg) -> None:
    """Draft captions and alt text with Claude vision → work/llm/suggestions.yaml."""
    from vantage.llm.claude import draft_captions

    proj = _load(project)
    out = draft_captions(proj)
    if out is None:
        log.fail("no suggestions drafted")
        raise typer.Exit(1)
    log.ok(f"caption suggestions → {_show(out)} (review, then copy what you like into story.yaml)")


@_command("doctor")
def doctor_cmd() -> None:
    """Check ffmpeg/ffprobe, encoders and Python dependencies."""
    checks = doctor_checks()
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = "✓" if c.ok else ("✗" if c.required else "!")
        typer.echo(f"{mark}  {c.name:<{width}}  {c.detail}")
    typer.echo(f"   {'projects':<{width}}  {paths.projects_root(_opts.projects_dir)}")
    typer.echo(f"   {'dist':<{width}}  {paths.dist_root(_opts.dist_dir)}")
    missing = [c.name for c in checks if c.required and not c.ok]
    if missing:
        log.fail(f"missing requirements: {', '.join(missing)}")
        raise typer.Exit(1)
    optional = sum(not c.ok for c in checks)
    log.ok(f"vantage {__version__}: all requirements present ({optional} optional extra(s) missing)")


@_command("fonts")
def fonts_cmd() -> None:
    """List the open-licensed fonts bundled with Vantage."""
    fonts = _bundled_fonts()
    if not fonts:
        log.warn(f"no bundled fonts manifest at {_show(paths.RUNTIME_DIR / 'fonts' / 'fonts.json')}")
        return
    width = max(map(len, fonts))
    for key, spec in fonts.items():
        styles = "+".join(
            sorted({f.get("style", "normal") for f in spec.get("faces", [])}, key="normal".__ne__)
        )
        typer.echo(
            f"{key:<{width}}  {spec.get('family', '?'):<18} {spec.get('category', ''):<11} "
            f"weight {spec.get('weight', '?'):<8} {styles:<14} {spec.get('license', '')}"
        )
    log.ok(f"{len(fonts)} bundled fonts; use `bundled: <key>` under typography in brand.yaml")
