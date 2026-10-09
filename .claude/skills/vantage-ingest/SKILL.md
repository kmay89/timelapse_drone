---
name: vantage-ingest
description: >-
  Catalog a project's drone footage and look at it: run `vantage ingest`, read every contact sheet with
  vision, name the vantages, pin each vantage's reference frame and telemetry hint in story.yaml, and
  flag blurred, weather-hit, mis-dated or missing visits. Use when new flights arrive, when someone says
  "ingest", "new footage", "we flew again", "look at the flights", or after /vantage-new-project.
argument-hint: "<project>"
allowed-tools: Bash(uv run vantage *), Bash(uv run python *), Bash(ls *), Bash(ffprobe *), Read, Edit, Glob, Grep
---

# Ingest and look at the footage

Project: `$ARGUMENTS` (a slug under `$VANTAGE_PROJECTS` or a path). Goal: every visit cataloged and
seen, vantages named with a reference frame each, problems reported before anything is aligned.

## 1. Footage in place

```sh
ls "<project>/footage"            # one folder per visit: footage/YYYY-MM-DD/...
```

- Each visit folder holds the files exactly as they came off the card: `.MP4` with its `.SRT`, `.JPG`
  (and `.DNG`), and the pilot's `notes.txt`. Never rename or re-encode originals.
- Footage in a bucket: `rclone copy r2:<bucket>/<slug>/<date> "<project>/footage/<date>"` (see
  `docs/DELIVERY.md`). Footage is never committed.
- Read each `footage/<date>/notes.txt`; they become captions later.

## 2. Catalog and sample

```sh
uv run vantage ingest <project>          # --force re-samples every source
```

Writes `work/catalog.json`, `work/candidates/**` and one contact sheet per date in
`work/contact/<date>.jpg`. Summarize the catalog:

```sh
uv run python - <project> <<'EOF'
import sys
from collections import defaultdict
from vantage.config import load_project
from vantage.models import Catalog
from vantage.paths import find_project
p = load_project(find_project(sys.argv[1]))
cat = Catalog.load(p.work_dir / "catalog.json")
by = defaultdict(list)
for s in cat.sources:
    by[s.date].append(s)
for date, srcs in sorted(by.items()):
    kinds = ", ".join(f"{s.kind}:{s.path.split('/')[-1]}({s.date_source}{'' if s.telemetry or s.lat else ', no GPS'})" for s in srcs)
    print(date, len(srcs), kinds)
EOF
```

## 3. Look at every contact sheet (vision)

Open each `work/contact/<date>.jpg` with the Read tool. Tiles are labelled `<file> t=<seconds>s`.
For every date record:

- which vantages appear, and which tile shows each best (file + `t`);
- quality: motion blur, haze or fog, rain or glare on the lens, very low sun or hard shadows, snow,
  a tilted horizon, a different lens or zoom (that is a different vantage);
- privacy: identifiable people, licence plates, private yards (note them for the story, never crop
  them out silently);
- off-plan shots (straight down over water, take-off and landing frames) that must not be picked.

With many dates, delegate batches of sheets to the `frame-reviewer` agent and merge its tables.

## 4. Name the vantages and pin references (story.yaml)

For each repeat viewpoint:

```yaml
vantages:
  - id: overview                      # short, stable, kebab-case; never rename once masters exist
    name: Over the lake, looking at the gatehouses
    kind: drone                       # drone | ground | ortho | archival | plan
    reference: { source: "2026-09-12/DJI_0042.MP4", t: 12.5 }   # clearest frame, ideally latest visit
    hint: { lat: 12.3456, lon: -45.6789, alt_m: 90, heading_deg: 150, gimbal_pitch_deg: -30 }
    portrait_focus: [0.5, 0.5]        # centre of the phone crop, 0–1
```

Take `hint` values from the reference source in `work/catalog.json` (`lat`, `lon`, `rel_alt`,
`heading_deg`, `gimbal_pitch_deg`). Add visit labels and the pilot's notes under `captures:`
(`{date, label, note}`). A visit that is unusable for every vantage gets `exclude: true` there.

**Checkpoint:** show the person a table (date · sources · vantages seen · issues) and your proposed
vantage ids/names/references. Wait for confirmation before `/vantage-align-review`.

## 5. Validate

```sh
uv run vantage validate <project>
```

## Failure handling

| Symptom | Likely cause and fix |
|---|---|
| a visit lands on the wrong date | folder not named `YYYY-MM-DD`; `date_source` is `mtime` or `quicktime` (UTC). Move files into a dated folder |
| two visits on one date | expected if flown twice; labels then show the day ("June 14, 2025") |
| no GPS on videos | DJI subtitles were off; ask the pilot to turn on video captions (`docs/CAPTURE_GUIDE.md`) |
| `ffprobe` errors on a file | truncated copy; re-copy from the card |
| a month missing | ask the client whether a flight happened; note it in the report, never invent a date |
| contact sheet shows only take-off frames | sampling too sparse: `select.sample_every_s` in project.yaml, then `--force` |
