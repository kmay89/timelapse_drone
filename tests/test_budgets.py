from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from vantage.paths import repo_root

SCRIPT = repo_root() / "scripts" / "budgets.py"
MIB = 1024 * 1024


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False)


def _dist(root: Path, slug: str = "demo", *, hosted_file_mb: float = 1) -> Path:
    dist = root / slug
    site = dist / "site"
    (site / "assets").mkdir(parents=True)
    css = ":root{--v-accent:#e07a3f}" + "".join(f".c{i}{{margin:{i}px}}" for i in range(50))
    (site / "index.html").write_text(
        f'<style id="vantage-theme">{css}</style><style id="vantage-css"></style>'
    )
    with (site / "assets" / "clip.mp4").open("wb") as f:
        f.truncate(int(hosted_file_mb * MIB))  # sparse: no real disk use
    (dist / f"{slug}.html").write_text("<p>full</p>")
    (dist / f"{slug}-lite.html").write_text("<p>lite</p>" * 1000)
    return dist


def test_runtime_only_mode_passes_on_the_shipped_runtime() -> None:
    result = _run("--runtime-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "runtime js (gzip)" in result.stdout and "runtime css (gzip)" in result.stdout
    assert "budgets met" in result.stdout


def test_a_built_story_within_budget(tmp_path: Path) -> None:
    result = _run(str(_dist(tmp_path)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "demo largest hosted file" in result.stdout and "assets/clip.mp4" in result.stdout
    assert "7 budgets met" in result.stdout


def test_over_budget_files_fail_and_are_named(tmp_path: Path) -> None:
    dist = _dist(tmp_path, hosted_file_mb=25.5)
    result = _run(str(dist), "--lite-mb", "0.001")
    assert result.returncode == 1
    over = [line for line in result.stdout.splitlines() if "OVER" in line]
    assert [line.split()[1] for line in over] == ["largest", "lite"]
    assert "over budget: demo largest hosted file, demo lite html" in result.stderr


def test_a_missing_build_is_a_usage_error(tmp_path: Path) -> None:
    result = _run(str(tmp_path / "nothing-here"))
    assert result.returncode == 2
    assert "vantage package" in result.stderr
