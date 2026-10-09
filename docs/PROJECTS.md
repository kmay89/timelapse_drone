# Projects

The engine (this repository) is public and generic. **Client projects live in a separate, private
repository** and are pointed to with `VANTAGE_PROJECTS`. Only the fictional demo,
`projects/demo-lakeside`, lives here.

## Layout of the private projects repo

```
vantage-projects/                     private
  README.md                           who owns what, how to get footage access
  .github/workflows/release.yml       copy of this repo's release workflow (see "CI")
  <slug>/
    project.yaml                      settings, title, location, outputs
    story.yaml                        vantages, picks and excludes, captures, chapters
    facts.yaml                        every printed figure: text, sources, status
    approvals.yaml                    who approved what, when (see "Approvals")
    brand/brand.yaml                  the client's kit: colours, fonts, voice, partners
    brand/*.svg, brand/partners/*.svg logos as supplied (never traced or downloaded)
    brand/fonts/*.woff2               only when licensed for web embedding
    masters/<vantage>/<date>.jpg      aligned, colour-matched frames + masters/index.json
    images/                           cleared gallery and archival images (with their licences)
    footage/                          NOT committed (bucket; see DELIVERY.md)
    work/                             NOT committed (candidates, contact and review sheets)
```

## Working with it

```sh
git clone <private-projects-remote> ~/vantage-projects
export VANTAGE_PROJECTS=~/vantage-projects        # shell profile, or .claude/settings.local.json "env"
uv run vantage validate <slug>                     # slugs now resolve under $VANTAGE_PROJECTS
uv run vantage all <slug>                          # outputs still go to ./dist/<slug> (or $VANTAGE_DIST)
```

A path works anywhere a slug does (`uv run vantage build ~/vantage-projects/<slug>`). Claude Code
sessions that open both repos should set `VANTAGE_PROJECTS` in the environment setup, since a
multi-repo cloud session does not run either repo's hooks.

## What to commit, and what never

| Commit (private repo) | Never commit anywhere |
|---|---|
| `project.yaml`, `story.yaml`, `facts.yaml`, `approvals.yaml` | raw footage (`footage/`) |
| `brand/` files the client supplied, licensed fonts | `work/` (regenerated) |
| `masters/` (about 1 MB per frame; CI rebuilds from them) | `dist/` (regenerated; releases carry the files) |
| cleared gallery images and their licence notes | credentials, presigned URLs, `.env` |
| footage manifest (paths, sizes, hashes, bucket location) | screenshots of client work in the public repo |

In **this public repo**, never commit client names, places, figures, brand files, masters or
screenshots, including in tests, fixtures, docs and commit messages. Examples say "the client", "the
site", "the design firm". The `rights-auditor` agent checks this before a release.

Manual decisions are YAML, so they survive deleting `work/`: frame overrides are
`vantages[].picks`, unusable visits are `captures[].exclude: true`.

## Approvals

Releases need recorded approval of the copy and every fact. Keep `approvals.yaml` next to the
project (it is not read by the build; it is the audit trail the publish skill checks):

```yaml
approvals:
  - edition: "2026-10"                 # the release tag suffix
    date: 2026-10-14
    by: "Name, role, organisation"
    via: "email of 2026-10-14, subject …"
    build: "sha256 of <slug>.html from SHA256SUMS"
    copy: approved                     # story text as built
    facts: [opening-day, trees-planted] # facts.yaml ids confirmed (set them to client-approved)
    notes: "Hold the playground photo until the press release."
```

When the client confirms a fact, set its `status: client-approved` in `facts.yaml` in the same
commit. `vantage build --release` fails while any printed fact is still `needs-client`.

## CI for private projects

The engine's workflows build the demo. For client projects, copy `.github/workflows/release.yml` into
the private repo and add a step that checks out the engine (pinned to a tag) and runs
`uv sync --frozen` there with `VANTAGE_PROJECTS=$GITHUB_WORKSPACE`. Builds use the committed masters;
no footage is needed. Never deploy a client story with GitHub Pages: Pages sites are public even
from private repositories on most plans.
