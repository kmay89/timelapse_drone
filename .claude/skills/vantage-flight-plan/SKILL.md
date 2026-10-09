---
name: vantage-flight-plan
description: >-
  Produce a printable pilot brief for a Vantage project: the standard repeat mission (one stop per
  vantage with position, altitude, heading, gimbal tilt and what to capture), camera settings, timing,
  delivery and safety, from the vantage hints in story.yaml and docs/CAPTURE_GUIDE.md. Use for "flight
  plan", "pilot brief", "capture guide for the pilot", "how should they fly", or before a new
  client's first or next flight.
argument-hint: "<project>"
allowed-tools: Bash(uv run python *), Bash(ls *), Read, Write, Glob, Grep
---

# Pilot brief

Project: `$ARGUMENTS`. Output: `<project>/work/flight-plan/<slug>-flight-plan.html`, one or two
printed pages (Letter/A4) the pilot can carry, built from `${CLAUDE_SKILL_DIR}/template.html`.
Disciplined repeat flights matter more than any feature of the story: this brief is how we get them.

## 1. Gather

- `docs/CAPTURE_GUIDE.md`: the generic guide (mission, camera settings, timing, delivery, safety).
- The project's vantages and hints:

  ```sh
  uv run python - <project> <<'EOF'
  import sys
  from vantage.config import load_project
  from vantage.paths import find_project
  p = load_project(find_project(sys.argv[1]))
  print(p.slug, "|", p.config.title, "|", p.config.location, "|", p.config.timezone)
  for v in p.story.vantages:
      print(v.id, "|", v.name, "|", v.kind, "|", v.hint, "|", v.reference)
  EOF
  ```

- Previous visits (`masters/index.json`, `work/catalog.json` if present): dates flown, take-off times,
  and the measured telemetry of each vantage's reference frame (`lat`, `lon`, `rel_alt`,
  `heading_deg`, `gimbal_pitch_deg`). Prefer measured numbers over hints.

## 2. Fill the brief

Copy `${CLAUDE_SKILL_DIR}/template.html` to the output path and replace every `{{…}}`:

- **Mission name**: `<SITE>-STD-v<N>` (short site code, uppercase; bump `v` only when a stop changes).
- **Stops table**: one row per drone vantage, in flying order (take-off → farthest → back): id, what
  it shows (the vantage name), position (lat, lon to 6 decimals or "set on first flight"), altitude
  above take-off, heading, gimbal tilt, capture ("10 s hover video + 1 photo"). Ground vantages (`kind:
  ground`) become the "phone photos" section with the marked spot described in words.
- **Timing**: the project's usual time of day (from previous take-off times, ±60 min), cadence
  (monthly, plus milestone days the client named), weather limits.
- **Delivery**: folder name `YYYY-MM-DD_<SITE>/`, originals only (MP4 + SRT + JPG/DNG), `notes.txt`
  contents, where to upload.
- **Safety and privacy**: from the capture guide; add site-specific notes (construction fence,
  people at events, neighbours) only from what the client told you.

Never invent coordinates, altitudes or legal requirements: unknown values read "set on first flight
together" and airspace is always "check B4UFLY/LAANC before each flight".

## 3. Check and hand over

Open the HTML and read it top to bottom as the pilot would. Two printed pages at most; tables fit
the page width. **Checkpoint:** show the person the stops table and ask them to confirm it with the
pilot before the next flight. The brief is a project file (private repo); never commit it here.

## Failure handling

| Symptom | Fix |
|---|---|
| vantages have no hints and no footage yet | brief lists the stops by description; numbers "set on first flight together" |
| a vantage is archival or a plan (`kind: archival/plan/ortho`) | leave it out of the mission |
| the pilot's drone has no waypoint support | keep the table; add "fly manually to the logged numbers, compare with the reference photo on screen" |
