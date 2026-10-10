from __future__ import annotations

import html
import http.client
import shutil
import sys
import threading
import types
import typing
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml
from PIL import Image
from typer.testing import CliRunner

from vantage.cli import app, make_server, parse_byte_range
from vantage.config import FACT_TOKEN_RE, Facts, FactStatus, Project, load_project
from vantage.models import MastersIndex, MastersVantage

runner = CliRunner()

COMMANDS = [
    "new", "validate", "ingest", "select", "align", "process", "build", "film",
    "package", "all", "preview", "demo", "caption", "doctor", "fonts",
]  # fmt: skip


def _invoke(tmp_path: Path, *args: str) -> object:
    return runner.invoke(app, ["--projects-dir", str(tmp_path), "--dist-dir", str(tmp_path / "dist"), *args])


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    result = _invoke(tmp_path, "new", "riverside-park", "--title", "Riverside Park")
    assert result.exit_code == 0, result.output
    return tmp_path / "riverside-park"


def test_root_help_lists_every_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in COMMANDS:
        assert cmd in result.output


@pytest.mark.parametrize("cmd", COMMANDS)
def test_command_help(cmd: str) -> None:
    result = runner.invoke(app, [cmd, "--help"])
    assert result.exit_code == 0, result.output
    assert "Usage" in result.output


def test_new_creates_a_valid_project(tmp_path: Path, project_dir: Path) -> None:
    project = load_project(project_dir)
    assert (project.slug, project.config.title, project.brand.name) == (
        "riverside-park",
        "Riverside Park",
        "Riverside Park",
    )
    assert [v.id for v in project.story.vantages] == ["overview"]
    types_ = [ch.type for ch in project.story.chapters]
    assert types_ == ["hero", "text", "scrub", "compare", "stats", "timeline", "explore", "credits"]
    assert (project_dir / ".gitignore").read_text().split()[-2:] == ["footage/", "work/"]
    assert (project_dir / "footage").is_dir()
    cited = FACT_TOKEN_RE.findall(project.story.model_dump_json())
    assert cited == ["site-area"] and project.facts.facts["site-area"].status == "needs-client"
    assert "{{" not in "".join(p.read_text() for p in project_dir.rglob("*") if p.is_file())

    again = _invoke(tmp_path, "new", "riverside-park")
    assert again.exit_code == 1 and "already exists" in again.output
    bad = _invoke(tmp_path, "new", "Not A Slug")
    assert bad.exit_code == 1 and "invalid slug" in bad.output


def test_new_keeps_client_projects_out_of_the_public_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 1: `vantage new` never writes a client project where `git add -A` would publish it."""
    from vantage import paths

    checkout = tmp_path / "vantage"  # stands in for the public, editable-installed checkout
    (checkout / "src" / "vantage").mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("")
    monkeypatch.setattr(paths, "PACKAGE_DIR", checkout / "src" / "vantage")
    monkeypatch.delenv("VANTAGE_PROJECTS", raising=False)
    monkeypatch.chdir(checkout)

    bare = runner.invoke(app, ["new", "acme-park"])  # default projects root = <checkout>/projects
    assert bare.exit_code == 1 and "inside the public Vantage checkout" in bare.output, bare.output
    empty = runner.invoke(app, ["new", "acme-park", "--dir", ""])  # `--dir "$VANTAGE_PROJECTS"`, unset
    assert empty.exit_code == 1 and "--dir is empty" in empty.output, empty.output
    here = runner.invoke(app, ["new", "acme-park", "--dir", "."])
    assert here.exit_code == 1 and "inside the public Vantage checkout" in here.output, here.output
    assert sorted(p.name for p in checkout.iterdir()) == ["pyproject.toml", "src"]  # nothing written

    (checkout / "private" / ".git").mkdir(parents=True)  # the private projects repo, cloned inside
    nested = runner.invoke(app, ["new", "acme-park", "--dir", "private"])
    assert nested.exit_code == 0, nested.output
    assert runner.invoke(app, ["validate", "private/acme-park"]).exit_code == 0
    assert "public Vantage checkout" not in runner.invoke(app, ["validate", "private/acme-park"]).output
    demo = checkout / "projects" / "demo-lakeside"
    assert not paths.exposed_in_public_checkout(demo) and paths.exposed_in_public_checkout(demo.parent)

    fictional = runner.invoke(app, ["new", "second-demo", "--public"])
    assert fictional.exit_code == 0, fictional.output
    warned = runner.invoke(app, ["validate", "second-demo"])
    assert warned.exit_code == 0 and "inside the public Vantage checkout" in warned.output, warned.output


def test_new_facts_yaml_documents_one_example_per_status(project_dir: Path) -> None:
    text = (project_dir / "facts.yaml").read_text()
    header = text.split("\nfacts:")[0]
    assert all(f" {status} " in header for status in typing.get_args(FactStatus))  # each one explained
    examples = "\n".join(  # the commented-out examples, uncommented: they must be valid facts too
        line.replace("  # ", "  ", 1)
        for line in text.splitlines()
        if line.startswith(("facts:", "  # ")) and ":" in line
    )
    facts = Facts.model_validate(yaml.safe_load(examples)).facts
    assert sorted(f.status for f in facts.values()) == sorted(typing.get_args(FactStatus))
    assert [f.releasable for f in facts.values()] == [True, True, True, False, False]


def test_validate_passes_on_a_new_project(tmp_path: Path, project_dir: Path) -> None:
    by_slug = _invoke(tmp_path, "validate", "riverside-park")
    assert by_slug.exit_code == 0, by_slug.output
    assert "riverside-park is valid" in by_slug.output and "TODO markers left" in by_slug.output
    assert "facts.yaml 3" in by_slug.output
    assert (
        "1 fact(s) not releasable yet" in by_slug.output
        and "'site-area' has status 'needs-client'" in by_slug.output
    )
    by_path = runner.invoke(app, ["validate", str(project_dir)])
    assert by_path.exit_code == 0, by_path.output


def test_validate_reports_cross_check_errors(tmp_path: Path, project_dir: Path) -> None:
    (project_dir / "brand" / "logo.svg").unlink()
    story_path = project_dir / "story.yaml"
    story = yaml.safe_load(story_path.read_text())
    story["vantages"][0]["reference"] = {"source": "2025-01-01/missing.MP4", "t": 1.0}
    story["chapters"][0]["capture"] = "yesterday"
    story["chapters"][1]["id"] = story["chapters"][0]["id"]
    story["chapters"][5]["items"][1]["vantage"] = "nowhere"
    story["chapters"][1]["body"] = "Opened in {fact:opening-year}."
    story_path.write_text(yaml.safe_dump(story, sort_keys=False))
    brand_path = project_dir / "brand" / "brand.yaml"
    brand = yaml.safe_load(brand_path.read_text())
    brand |= {"name": "Parks {fact:site-area}", "copyright": "© {fact:site-area}"}  # copyright resolves facts
    brand_path.write_text(yaml.safe_dump(brand, sort_keys=False))
    (project_dir / "brand" / "logo-on-dark.svg").write_text(
        '<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>'
    )
    (project_dir / "brand" / "mark.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(2)</script></svg>'
    )
    footage = project_dir / "footage" / "2025-01-01"
    footage.mkdir(parents=True)
    Image.new("RGB", (8, 8)).save(footage / "still.jpg")

    result = _invoke(tmp_path, "validate", "riverside-park")
    assert result.exit_code == 1
    for needle in (
        "logos.primary: missing file",
        "'yesterday'",
        "duplicate chapter id",
        "'nowhere'",
        "missing.MP4",
        "story.yaml.chapters[1].body: unknown fact {fact:opening-year}",
        "brand.yaml name: {fact:…} tokens only resolve in credit_line, disclaimer, copyright",
        "brand.yaml logos.on_dark (",
        "refusing to publish this SVG: it has a DOCTYPE internal subset",
        "brand.yaml logos.mark: the published copy leaves out <script>, onload=…",
    ):
        assert needle in result.output, needle
    assert "brand.yaml copyright" not in result.output


def test_schema_errors_are_friendly(tmp_path: Path, project_dir: Path) -> None:
    config = project_dir / "project.yaml"
    config.write_text(config.read_text().replace("slug: riverside-park", "slug: Riverside Park\nbogus: 1"))
    result = _invoke(tmp_path, "validate", "riverside-park")
    assert result.exit_code == 1
    assert "invalid project.yaml" in result.output and "slug" in result.output and "bogus" in result.output
    assert "Traceback" not in result.output
    config.write_text("slug: [unclosed")
    assert "YAML syntax error" in _invoke(tmp_path, "validate", "riverside-park").output
    missing = _invoke(tmp_path, "validate", "nope")
    assert missing.exit_code == 1 and "no project 'nope'" in missing.output


def test_ingest_command(tmp_path: Path, project_dir: Path) -> None:
    folder = project_dir / "footage" / "2025-06-14"
    folder.mkdir(parents=True)
    Image.effect_noise((64, 48), 40).convert("RGB").save(folder / "DJI_0001.JPG")
    result = _invoke(tmp_path, "ingest", "riverside-park")
    assert result.exit_code == 0, result.output
    assert "1 sources over 1 dates, 1 candidates" in result.output
    assert (project_dir / "work" / "catalog.json").is_file()
    assert (project_dir / "work" / "contact" / "2025-06-14.jpg").is_file()


@pytest.fixture
def fake_outputs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand-ins for the site/film/package modules, recording the order they run in."""
    calls: list[str] = []

    def build_site(project: Project, out_dir: Path) -> Path:
        calls.append("build")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text("<!doctype html><title>x</title>")
        return out_dir / "index.html"

    def render_film(project: Project, out_dir: Path) -> list[Path]:
        calls.append("film")
        out_dir.mkdir(parents=True, exist_ok=True)
        film = out_dir / f"{project.slug}-16x9.mp4"
        film.write_bytes(b"\0" * 2048)
        return [film]

    def package_project(project: Project, dist_dir: Path) -> dict[str, Path]:
        calls.append("package")
        single, archive = dist_dir / f"{project.slug}.html", dist_dir / f"{project.slug}-offline.zip"
        single.write_text("<html>")
        archive.write_bytes(b"PK")
        return {"single_file": single, "zip": archive}

    for name, fn in (
        ("vantage.site.build", build_site),
        ("vantage.film.render", render_film),
        ("vantage.site.package", package_project),
    ):
        module = types.ModuleType(name)
        setattr(module, fn.__name__, fn)
        monkeypatch.setitem(sys.modules, name, module)
    return calls


def test_all_builds_from_committed_masters(
    tmp_path: Path, project_dir: Path, fake_outputs: list[str]
) -> None:
    no_inputs = _invoke(tmp_path, "all", "riverside-park")
    assert no_inputs.exit_code == 1 and "no masters/index.json" in no_inputs.output
    MastersIndex(vantages={"overview": MastersVantage(name="Overview", width=16, height=9)}).save(
        project_dir / "masters" / "index.json"
    )
    result = _invoke(tmp_path, "all", "riverside-park")
    assert result.exit_code == 0, result.output
    assert fake_outputs == ["build", "film", "package"]
    dist = tmp_path / "dist" / "riverside-park"
    for needle in ("building from the committed masters", "single file", "riverside-park-16x9.mp4", "2.0 KB"):
        assert needle in result.output
    assert (dist / "site" / "index.html").is_file()

    fake_outputs.clear()
    assert _invoke(tmp_path, "all", "riverside-park", "--no-film").exit_code == 0
    assert fake_outputs == ["build", "package"]


def test_preview_needs_a_built_site(tmp_path: Path, project_dir: Path) -> None:
    result = _invoke(tmp_path, "preview", "riverside-park")
    assert result.exit_code == 1 and "no built site" in result.output


def test_doctor_runs() -> None:
    result = runner.invoke(app, ["doctor"])
    required_ok = shutil.which("ffmpeg") and shutil.which("ffprobe")
    assert result.exit_code == (0 if required_ok else 1), result.output
    for needle in ("ffmpeg", "libx264", "opencv-python-headless", "pillow avif", "ANTHROPIC_API_KEY"):
        assert needle in result.output


def test_fonts_lists_bundled_faces() -> None:
    result = runner.invoke(app, ["fonts"])
    assert result.exit_code == 0
    assert "fraunces" in result.output or "no bundled fonts manifest" in result.output


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("bytes=0-1", (0, 1)),
        ("bytes=990-", (990, 999)),
        ("bytes=-5", (995, 999)),
        ("bytes=-5000", (0, 999)),
        ("bytes=900-5000", (900, 999)),
        ("bytes=5000-", (5000, 999)),
        ("bytes=-0", (1000, 999)),
        ("bytes=5-1", None),
        ("bytes=0-1,4-5", None),
        ("items=0-1", None),
        ("bytes=-", None),
    ],
)
def test_parse_byte_range(header: str, expected: tuple[int, int] | None) -> None:
    assert parse_byte_range(header, 1000) == expected


@pytest.fixture(scope="module")
def server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    site = tmp_path_factory.mktemp("site")
    (site / "index.html").write_text("<!doctype html><title>t</title>")
    (site / "clip.mp4").write_bytes(bytes(range(256)) * 4)
    for name in ("a.avif", "b.webp", "manifest.webmanifest", "f.woff2", "app.js"):
        (site / name).write_bytes(b"x")
    srv = make_server(site, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield srv.server_port
    srv.shutdown()
    srv.server_close()


def _get(port: int, path: str, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body


def test_preview_server_serves_byte_ranges(server: int) -> None:
    status, headers, body = _get(server, "/clip.mp4", {"Range": "bytes=0-1"})
    assert (status, headers["content-range"], headers["content-length"], body) == (
        206,
        "bytes 0-1/1024",
        "2",
        b"\0\1",
    )
    assert headers["content-type"] == "video/mp4"
    status, headers, body = _get(server, "/clip.mp4", {"Range": "bytes=-3"})
    assert (status, body) == (206, bytes([253, 254, 255]))
    status, headers, _ = _get(server, "/clip.mp4", {"Range": "bytes=4096-"})
    assert (status, headers["content-range"]) == (416, "bytes */1024")
    status, headers, body = _get(server, "/clip.mp4")
    assert (status, len(body), headers["accept-ranges"]) == (200, 1024, "bytes")
    assert _get(server, "/missing.mp4", {"Range": "bytes=0-1"})[0] == 404
    assert _get(server, "/")[0] == 200


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("a.avif", "image/avif"),
        ("b.webp", "image/webp"),
        ("manifest.webmanifest", "application/manifest+json"),
        ("f.woff2", "font/woff2"),
        ("app.js", "text/javascript"),
    ],
)
def test_preview_server_mime_types(server: int, name: str, mime: str) -> None:
    assert _get(server, f"/{name}")[1]["content-type"].startswith(mime)


def test_new_escapes_hostile_titles_and_validate_checks_timezone_and_gallery(tmp_path: Path) -> None:
    title = 'Say "Hi" & <Co> \\ back: # not-a-comment {{slug}}'
    assert _invoke(tmp_path, "new", "odd-one", "--title", title).exit_code == 0
    root = tmp_path / "odd-one"
    project = load_project(root)
    assert (project.config.title, project.brand.name, project.story.chapters[0].title) == (title,) * 3
    for svg in (root / "brand").glob("*.svg"):
        ElementTree.parse(svg)  # well-formed XML despite the title
    assert f">{html.escape(title)}</text>" in (root / "brand" / "logo.svg").read_text()

    config = root / "project.yaml"
    config.write_text(config.read_text().replace("America/New_York", "Mars/Olympus"))
    story = yaml.safe_load((root / "story.yaml").read_text())
    story["chapters"].append(
        {"type": "gallery", "id": "archive", "captures": ["first"], "images": [{"file": "archive/gone.jpg"}]}
    )
    (root / "story.yaml").write_text(yaml.safe_dump(story, sort_keys=False))
    result = _invoke(tmp_path, "validate", "odd-one")
    assert result.exit_code == 1
    for needle in ("'Mars/Olympus'", "archive/gone.jpg", "'first'"):
        assert needle in result.output
