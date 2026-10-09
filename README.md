# Vantage

**Repeat drone flights over a changing site, turned into a story people open on their phone.**

A park replaces a parking lot; a ruin becomes a plaza. A drone returns to the same patch of sky every
few weeks. Vantage aligns every visit to the same frame, colour-matches them, and builds an
iPhone-first interactive story in the client's brand: scroll and the site rebuilds itself under a
pinned camera, drag a curtain between before and after, tap a pin to see what changed. With
JavaScript off it is still a complete photo essay; saved to the Home Screen it works in airplane
mode; and it ships with time-lapse films for everyone else.

The engine is deterministic Python plus a dependency-free web runtime. Claude Code operates it through
repo skills (ingest, alignment review, writing, verification); an LLM never decides a fact and is
never needed for a build.

## Quickstart

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and ffmpeg (`apt-get install ffmpeg` or
`brew install ffmpeg`).

```sh
uv sync
uv run vantage doctor                    # checks ffmpeg, encoders and dependencies
uv run vantage demo --fast               # synthesize the fictional demo and build everything (a few minutes)
uv run vantage preview demo-lakeside     # http://127.0.0.1:8000/  (add --host 0.0.0.0 to open it on a phone)
```

The demo, *Where the Wave Pool Was*, is a fictional lakeside waterpark becoming a public beach over
twelve computer-generated flights (`projects/demo-lakeside/`). Nothing in it is real.

## What you get

`dist/<slug>/` after `vantage all <project>`:

| Deliverable | For | Notes |
|---|---|---|
| `site/` | the hosted link: the full experience on iPhone | service worker, "Add to Home Screen", "Save for offline" |
| `<slug>.html` | AirDrop, desktop, archive | everything inline in one file |
| `<slug>-lite.html` | email (≤ 15 MB) | key images only; reads well in iOS Quick Look with JS off |
| `<slug>-offline.zip` | desktop, long-term archive | the site folder, opens from disk |
| `film/<slug>-16x9.mp4`, `-9x16.mp4` | meetings, social, Photos | branded title and end cards, date rail |
| `HOW-TO-VIEW.txt`, `SHA256SUMS`, `build.json` | recipients, integrity, provenance | |

## How a client project flows

```
interview + brand kit ─► vantage new ─► footage/YYYY-MM-DD/ ─► vantage ingest ─► name vantages
   ─► vantage process (align, colour-match → masters/) ─► review sheets ─► write story.yaml + facts.yaml
   ─► vantage all ─► Playwright screenshots on iPhone ─► client approval ─► vantage all --release ─► publish
```

1. **Set up** (`/vantage-new-project`): client, audience, brand kit and font licences, vantages,
   milestones and confidentiality become `project.yaml`, `story.yaml`, `brand/brand.yaml` and
   `facts.yaml` in a **private** projects repo (`VANTAGE_PROJECTS=/path/to/projects`).
2. **Fly** (`/vantage-flight-plan`, [Capture guide](docs/CAPTURE_GUIDE.md)): the same waypoint
   mission every visit, originals delivered unedited.
3. **Ingest and align** (`/vantage-ingest`, `/vantage-align-review`): contact sheets and review
   sheets are read by a person or by Claude; overrides live in `story.yaml`; aligned masters are
   committed so CI can rebuild without footage.
4. **Write** (`/vantage-write-story`): every figure is a `{fact:id}` with a source; optional Claude
   vision drafts go to `work/llm/suggestions.yaml` for a human to accept.
5. **Verify and publish** (`/vantage-verify`, `/vantage-publish`): budgets, browser tests and
   screenshots; release builds refuse drafts and unapproved facts.

The full cycle, step by step: [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Commands

| Command | Does |
|---|---|
| `vantage new <slug>` | scaffold a project |
| `vantage validate <project>` | check YAML, brand files, capture references, TODOs |
| `vantage ingest / select / align <project>` | catalog footage · pick frames · register and grade masters |
| `vantage process <project>` | ingest → select → align |
| `vantage build / film / package <project>` | site · MP4 films · single files, zip, checksums |
| `vantage all <project> [--release]` | everything; `--release` turns on the gates |
| `vantage preview <project>` | serve the built site (byte ranges, so Safari plays video) |
| `vantage caption <project>` | optional Claude vision drafts (`uv sync --extra llm`) |
| `vantage demo`, `vantage doctor`, `vantage fonts` | demo · environment check · bundled fonts |

## Development

```sh
uv run ruff check && uv run ruff format --check
uv run pytest -m "not slow"
uv run python scripts/budgets.py         # size budgets (runtime JS/CSS, lite file, hosted files)
npm ci && npm run test:web:local         # Playwright on Chromium (iPhone 15 Pro + desktop)
```

CI (`.github/workflows/`) runs lint and tests, builds the demo with budgets, then runs the browser
suite on iPhone WebKit and desktop Chromium; pushes to `main` publish the demo to GitHub Pages; tags
build releases. Working with Claude Code: [CLAUDE.md](CLAUDE.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md): the contract between modules, formats and StoryJSON
- [Blueprint](docs/BLUEPRINT.md): product vision, chapter vocabulary, feature tiers, iOS decisions
- [Capture guide](docs/CAPTURE_GUIDE.md): how to fly so every visit lines up
- [Delivery](docs/DELIVERY.md): editions, iPhone realities, hosting, footage storage, releases
- [Projects](docs/PROJECTS.md): the private projects repo, what to commit, approvals
- [Runbook](docs/RUNBOOK.md): one cycle per flight, with the Claude skills
- [Research](docs/research/): visual state of the art, pipeline techniques, delivery and CI
- [Web tests](web-tests/README.md): the Playwright suite and the DOM contract it assumes

## Licence

Apache-2.0 ([LICENSE](LICENSE)). Bundled fonts are under the SIL Open Font License
(`src/vantage/runtime/fonts/*/OFL.txt`).
