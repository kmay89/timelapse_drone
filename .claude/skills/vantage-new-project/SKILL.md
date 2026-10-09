---
name: vantage-new-project
description: >-
  Start a new Vantage client project: interview the person about the client, site, audience, brand
  kit, footage and confidentiality, then scaffold it with `vantage new`, fill brand.yaml, project.yaml
  and facts.yaml, and validate. Use when someone says "new project", "set up a client", "start a
  story for <site>", "onboard <client>" or hands over a brand kit for a site that has no project yet.
argument-hint: "[slug]"
allowed-tools: Bash(uv run vantage *), Bash(ls *), Read, Write, Edit, Glob, Grep
---

# New Vantage project

Goal: a validated project folder in the **private** projects root with brand, settings, a facts file
and named vantages, ready for `/vantage-ingest`. Requested slug: `$ARGUMENTS` (ask if empty).

## 0. Where the project lives (decide first)

Client projects never go in this public repo. Ask where the private projects checkout is and use it:

```sh
echo "${VANTAGE_PROJECTS:-unset}"            # must point at the private projects repo
git -C "$VANTAGE_PROJECTS" remote -v         # confirm it is the private one
```

If it is unset, ask for the path and tell the person to add `export VANTAGE_PROJECTS=…` to their
shell profile (or `.claude/settings.local.json` → `env`). Only the fictional demo lives in
`projects/` here. See `docs/PROJECTS.md`.

## 1. Interview (one message, grouped questions; accept "unknown")

1. **Client and publisher**: who commissions it, whose name is on it (byline, credit line), partners
   and their roles (owner, landscape architect, contractor, aerial imagery), the approver of text.
2. **Audience and channel**: who reads it (residents, council, donors, staff), how it reaches them
   (text link, email attachment, kiosk, social), language, launch or embargo date.
3. **Site**: name, town and region, latitude/longitude, time zone, what is being built, what was there
   before, phases and milestone dates (groundbreaking, openings).
4. **Brand assets**: logo SVGs (full on light, full on dark, square mark), colors as hex, fonts and
   **their web-embedding licence** (no licence → a bundled face from `uv run vantage fonts`), voice
   (tone, words to avoid, example sentences), required disclaimer.
5. **Footage**: where it is (local folder, shared drive, bucket), drone model and app, dates flown so
   far, whether DJI video subtitles (`.SRT`) were on, stills or video, pilot's notes.
6. **Vantages**: the repeat viewpoints the pilot flies (names, and GPS/altitude/heading/tilt if known).
7. **Facts**: figures and claims the client wants printed, each with its source and who confirms it.
8. **Confidentiality**: public or unlisted, things never to show (people, plates, backyards, an
   unannounced feature), whether simulated or placeholder imagery must be disclosed.

**Checkpoint:** summarize the answers as a short table and get a "yes" before writing files.

## 2. Scaffold

```sh
uv run vantage new <slug> --title "<Title>" --dir "$VANTAGE_PROJECTS"
```

Slugs are lowercase words joined by hyphens. This writes `project.yaml`, `story.yaml`,
`brand/brand.yaml`, placeholder logos, a project README and `footage/`.

## 3. Fill the YAML (models: `src/vantage/config.py`)

- `project.yaml`: title, subtitle, kicker, dek, byline, `location`, `timezone`, `draft: true`,
  `simulated: false`, `output.base_url` if the hosting URL is known.
- `brand/brand.yaml`: name, short_name, url, colors (`accent`, `accent_2`, `ink`, `paper`, `night`),
  typography (`bundled: <key>` or client `files: [fonts/….woff2]` — add a YAML comment naming the
  licence), voice, partners (copy their logo files into `brand/partners/`), credit_line, copyright,
  disclaimer, `theme: dark|light`. Copy only files the client supplied: **never** download or trace a
  logo from a website.
- `facts.yaml` (create it): one entry per figure from the interview.

  ```yaml
  facts:
    opening-day:
      text: "Oct. 23, 2026"          # exactly what the story prints
      sources: ["<URL or document title>"]
      status: needs-client           # verified | reported | needs-client | client-approved | do-not-print
      attribution: null              # required for `reported` facts in a release
      note: "who confirms it, and how"
  ```

  Status is `verified` only with a primary source you have actually read; otherwise `needs-client`.
- `story.yaml`: one entry per vantage (`id`, `name`, `kind`, `hint` if known). Leave chapters as the
  template's TODOs; `/vantage-write-story` fills them once masters exist.

## 4. Validate

```sh
uv run vantage validate <slug>
```

Errors must be fixed now (missing logo files, unknown bundled font, bad time zone, bad slug).
Warnings about TODO markers and "no masters yet" are expected at this stage.

## 5. Hand off

Tell the person: what was created, every open question (unknown facts, missing logo variants, font
licence), and the next step: copy flights into `footage/YYYY-MM-DD/` (see `docs/CAPTURE_GUIDE.md`;
`/vantage-flight-plan` makes the pilot brief), then `/vantage-ingest`. They commit the project in the
private repo; do not commit it here.

## Failure handling

| Symptom | Fix |
|---|---|
| `invalid slug` | lowercase letters/digits joined by single hyphens |
| `already exists and is not empty` | pick another slug or work in the existing folder |
| `unknown bundled font` | `uv run vantage fonts` lists the keys |
| `missing file` for a logo | copy the client file into `brand/` or remove the key |
| client only has PNG/JPG logos | ask for SVG; meanwhile leave the key unset (the brand name prints instead) |
