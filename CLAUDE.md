# Vantage: notes for Claude

Vantage turns repeat drone flights over a changing site into a cinematic, iPhone-first interactive
progress story (pinned time-lapse scrub, before/after curtain, hotspots, a photo essay that works with
JavaScript off) plus MP4 time-lapse films, themed from the client's brand kit. One build makes a
hosted site that works offline, a full single-file HTML, a lite single file for email, an offline zip
and the films. The engine is generic and this repo is public; client projects live elsewhere.

`docs/ARCHITECTURE.md` is the contract between modules (CLI, artifacts, StoryJSON, output layout).
Read it before changing a format or a cross-module function, and update it in the same change.

## Golden commands

```sh
uv sync                                    # Python deps (the SessionStart hook runs it)
uv run vantage doctor                      # ffmpeg, encoders, Python deps, projects/dist roots
uv run vantage demo --fast --no-film       # fictional demo end to end → dist/demo-lakeside/ (~2–3 min)
uv run vantage preview demo-lakeside       # http://127.0.0.1:8000/ (--host 0.0.0.0 for a phone)

uv run vantage new <slug> --dir "$VANTAGE_PROJECTS"   # scaffold a client project
uv run vantage validate <project>          # YAML + brand files + capture refs + TODO count
uv run vantage process <project>           # ingest → select → align: masters/ + work/review/
uv run vantage all <project> [--release]   # process (if footage) → build → film → package
uv run vantage build|film|package <project>
uv run vantage caption <project>           # optional: Claude drafts → work/llm/suggestions.yaml
```

`<project>` is a slug under the projects root or a path to a project folder.

## Before saying "done"

```sh
uv run ruff check && uv run ruff format --check
uv run pytest -m "not slow"                # slow = full-size video renders
uv run python scripts/budgets.py           # runtime JS ≤ 30 KB gz, CSS ≤ 15 KB gz, lite ≤ 15 MB, files ≤ 25 MiB
npm run test:web:local                     # Playwright, iPhone + desktop Chromium, against dist/<slug>
```

- Web tests need a built story (`vantage demo --fast --no-film`, or `VANTAGE_STORY=<slug>` for another
  build in `dist/`). WebKit projects run in CI only; locally use the `*-chromium` projects.
- Checkpoint screenshots land in `web-tests/.results/test-results/shots/<project>/*.png`. Open them with
  the Read tool and look before claiming anything visual works. No golden-image diffs.
- Visual change? Screenshot iPhone (393×852) and desktop, JS on and JS off (`nojs-lite-*` is the
  Quick Look stand-in).

## Invariants

1. **Client data never enters this public repo.** No client names, sites, footage, masters, brand files,
   facts or screenshots of them, in code, docs, tests, commit messages or fixtures. Client projects live
   in a private repo pointed to by `VANTAGE_PROJECTS`; this repo holds only `projects/demo-lakeside`,
   which is fictional. Examples in docs say "the client", "the site", "the design firm".
2. **YAML is the source of truth.** `project.yaml`, `story.yaml`, `brand/brand.yaml`, `facts.yaml`
   (models: `src/vantage/config.py`). Everything in `work/` and `dist/` is regenerated from YAML +
   footage, or from YAML + committed `masters/`. Manual frame picks and skips go in `story.yaml`
   (`vantages[].picks`, `captures[].exclude`), never only in `work/`.
3. **LLMs suggest, humans accept.** `vantage caption` writes `work/llm/suggestions.yaml` with
   `status: proposed`; nothing is copied into `story.yaml` until a person approves it. A build never
   needs an LLM or network access.
4. **Facts gate.** Every number, date or claim in story text is a `{fact:id}` token backed by
   `facts.yaml` (text, sources, status). Never invent a figure or a source. `--release` fails on
   `draft: true`, unknown facts, and facts that are not `verified`/`client-approved` (or `reported`
   with an `attribution`).
5. **Budgets** (enforced in CI by `scripts/budgets.py`): runtime JS ≤ 30 KB gzip, CSS ≤ 15 KB gzip,
   lite single file ≤ 15 MB, no hosted file over 25 MiB.
6. **The runtime has no dependencies and no build step**: ES2020 in one IIFE, classic scripts, inline
   StoryJSON. It must work from `file://`, with JavaScript off (pre-rendered photo essay), and under
   `prefers-reduced-motion`. iOS Safari 16.4+ is the target; Playwright WebKit is not iOS Safari.
7. **Deterministic outputs**: sorted inputs, seeded RNG, stable JSON, bitexact ffmpeg; timestamps
   honour `SOURCE_DATE_EPOCH`. Same inputs, same bytes.
8. **Rights**: only client-supplied logos and fonts licensed for web embedding; bundled fonts are OFL.
   Never scrape a logo. Archival images need a recorded licence before they ship.
9. Raw footage is never committed (not even with LFS): `footage/`, `work/`, `dist/` are gitignored.

## Where things live

| Path | What |
|---|---|
| `src/vantage/` | engine: `cli.py`, `config.py` (YAML models), `ingest/`, `process/`, `site/`, `film/`, `llm/` |
| `src/vantage/runtime/` | web runtime shipped in every story: `template.html`, `css/`, `js/`, `fonts/`, `sw.js` |
| `projects/demo-lakeside/` | the fictional demo (synthetic footage generated on demand) |
| `$VANTAGE_PROJECTS/<slug>/` | client projects (private repo): YAML, brand, facts, committed `masters/` |
| `dist/<slug>/` | outputs: `site/`, `<slug>.html`, `<slug>-lite.html`, `<slug>-offline.zip`, `film/` |
| `tests/` · `web-tests/` | pytest · Playwright (`playwright.config.mjs`, `*.spec.mjs`) |
| `scripts/` | `budgets.py`, `session-start.sh` |
| `docs/` | ARCHITECTURE (contract), BLUEPRINT, CAPTURE_GUIDE, DELIVERY, PROJECTS, RUNBOOK, research/ |

Environment: `VANTAGE_PROJECTS` (projects root), `VANTAGE_DIST` (output root), `VANTAGE_FAST=1`
(smaller, quicker films), `ANTHROPIC_API_KEY` or an `ant` login (only for `vantage caption`).

## Skills and agents

One cycle per flight is in `docs/RUNBOOK.md`. Skills in `.claude/skills/`:

- `/vantage-new-project`: interview, scaffold, brand, facts, validate
- `/vantage-ingest`: catalog footage, read contact sheets, name vantages, flag bad visits
- `/vantage-align-review`: align, read review sheets, write picks/excludes, re-run
- `/vantage-write-story`: chapters, steps, hotspots, captions, alt text, all facts sourced
- `/vantage-verify`: build, package, budgets, Playwright screenshots, critique, iterate
- `/vantage-publish`: release build and delivery (runs only when you invoke it)
- `/vantage-flight-plan`: printable pilot brief from the vantages

Subagents in `.claude/agents/`: `frame-reviewer` (alignment sheets), `fact-checker` (facts.yaml
sources), `rights-auditor` (logos, fonts, archival images, credits).

## Conventions

- Python 3.11+, typed, ruff (line length 110). Module docstrings say what a file makes and why.
- The CLI is a thin wrapper: logic lives in modules with plain function APIs (see ARCHITECTURE).
- Tests use synthetic inputs (tiny generated masters, `vantage.demo.synth`); assert with tolerances,
  not pixel-exact goldens. Mark full-size video renders `@pytest.mark.slow`.
- Web tests select by role and ARIA (`getByRole("slider")`), never by styling classes, and read
  expectations from the page's StoryJSON so they run against any story.
- Never commit `dist/`, `work/`, `footage/` or `web-tests/.results/`.
