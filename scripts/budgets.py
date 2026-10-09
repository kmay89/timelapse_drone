"""Size budgets for the web runtime and for built stories. CI runs this after `vantage demo`.

    uv run python scripts/budgets.py                     # runtime + dist/demo-lakeside
    uv run python scripts/budgets.py dist/<slug> [...]   # runtime + those builds
    uv run python scripts/budgets.py --runtime-only      # runtime JS/CSS only (needs no build)

Prints a table; exits 1 when anything is over budget, 2 when a build folder is missing.
KB and MB are binary (1024, 1024²), the way `vantage package` counts its edition budgets.
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from vantage.paths import project_dist
from vantage.site.render import runtime_css, runtime_js

KB = 1024
MB = 1024 * KB
HOSTED_FILE_LIMIT = 25 * MB  # Cloudflare Pages / Workers static assets
PAGES_SITE_LIMIT = 1024 * MB  # GitHub Pages
_PAGE_CSS = re.compile(r'<style id="vantage-(?:theme|css)">(.*?)</style>', re.S)


@dataclass(frozen=True)
class Check:
    name: str
    size: int
    budget: int
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.size <= self.budget


def gz(text: str) -> int:
    return len(gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0))


def human(n: int) -> str:
    return f"{n / MB:.1f} MB" if n >= MB else f"{n / KB:.1f} KB"


def runtime_checks(js_kb: float, css_kb: float) -> list[Check]:
    return [
        Check("runtime js (gzip)", gz(runtime_js()), round(js_kb * KB)),
        Check("runtime css (gzip)", gz(runtime_css()), round(css_kb * KB)),
    ]


def dist_checks(dist: Path, *, css_kb: float, lite_mb: float, single_mb: float) -> list[Check]:
    """Budgets for one built story folder (dist/<slug>, holding site/ and the packaged files)."""
    slug, site = dist.name, dist / "site"
    files = [p for p in site.rglob("*") if p.is_file()]
    largest = max(files, key=lambda p: p.stat().st_size)
    page = (site / "index.html").read_text(encoding="utf-8")
    checks = [
        Check(f"{slug} page css (gzip)", gz("".join(_PAGE_CSS.findall(page))), round(css_kb * KB)),
        Check(
            f"{slug} largest hosted file",
            largest.stat().st_size,
            HOSTED_FILE_LIMIT,
            largest.relative_to(site).as_posix(),
        ),
        Check(
            f"{slug} site total",
            sum(p.stat().st_size for p in files),
            PAGES_SITE_LIMIT,
            f"{len(files)} files",
        ),
    ]
    return [
        *checks,
        Check(f"{slug} lite html", (dist / f"{slug}-lite.html").stat().st_size, round(lite_mb * MB)),
        Check(f"{slug} single html", (dist / f"{slug}.html").stat().st_size, round(single_mb * MB)),
    ]


def report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    rows = [f"{'check':<{width}}  {'size':>9}  {'budget':>9}"]
    rows += [
        f"{c.name:<{width}}  {human(c.size):>9}  {human(c.budget):>9}  {'ok' if c.ok else 'OVER'}"
        + (f"  {c.note}" if c.note else "")
        for c in checks
    ]
    return "\n".join(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "dists", nargs="*", type=Path, help="built story folders (default: dist/demo-lakeside)"
    )
    parser.add_argument("--runtime-only", action="store_true", help="check the runtime JS/CSS only")
    parser.add_argument("--js-kb", type=float, default=30, help="runtime JS budget, gzipped (default 30)")
    parser.add_argument("--css-kb", type=float, default=15, help="CSS budget, gzipped (default 15)")
    parser.add_argument("--lite-mb", type=float, default=15, help="lite single file (default 15)")
    parser.add_argument("--single-mb", type=float, default=60, help="full single file (default 60)")
    args = parser.parse_args(argv)

    checks = runtime_checks(args.js_kb, args.css_kb)
    if not args.runtime_only:
        for dist in args.dists or [project_dist("demo-lakeside")]:
            needed = [
                dist / "site" / "index.html",
                dist / f"{dist.name}.html",
                dist / f"{dist.name}-lite.html",
            ]
            missing = [p.as_posix() for p in needed if not p.is_file()]
            if missing:
                print(
                    f"✗ missing {', '.join(missing)}; run `vantage build` and `vantage package`",
                    file=sys.stderr,
                )
                return 2
            checks += dist_checks(dist, css_kb=args.css_kb, lite_mb=args.lite_mb, single_mb=args.single_mb)
    print(report(checks))
    over = [c.name for c in checks if not c.ok]
    if over:
        print(f"✗ over budget: {', '.join(over)}", file=sys.stderr)
        return 1
    print(f"✓ {len(checks)} budgets met")
    return 0


if __name__ == "__main__":
    sys.exit(main())
