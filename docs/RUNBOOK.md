# Runbook: one cycle per flight

What happens between "the pilot landed" and "the new edition is live", with the Claude Code skill
for each step. Every step can also be done by hand with the commands shown. Times are for a typical
monthly flight with three to six vantages.

```
fly ─► upload ─► /vantage-ingest ─► /vantage-align-review ─► /vantage-write-story
    ─► /vantage-verify ─► client review ─► approvals ─► /vantage-publish
```

## 0. Once per project

| Step | Skill / command | Result |
|---|---|---|
| Interview, scaffold, brand, facts | `/vantage-new-project` | `$VANTAGE_PROJECTS/<slug>/` validated |
| Pilot brief | `/vantage-flight-plan <slug>` | `work/flight-plan/<slug>-flight-plan.html` to print |
| Hosting decided | [DELIVERY.md](DELIVERY.md) | hosted path, preview method, bucket for footage |

## 1. After the flight (day 0–2)

1. The pilot uploads `YYYY-MM-DD_<SITE>/` with originals and `notes.txt` (see
   [CAPTURE_GUIDE.md](CAPTURE_GUIDE.md)).
2. Copy it into the bucket and the project:

   ```sh
   rclone copy "<shared folder>/2026-10-12_<SITE>" r2:vantage-footage/<slug>/2026-10-12
   rclone copy r2:vantage-footage/<slug>/2026-10-12 "$VANTAGE_PROJECTS/<slug>/footage/2026-10-12"
   ```

   Folder names in `footage/` are plain dates; the date folder wins over every timestamp in the files.

## 2. Ingest (≈ 10–20 min) — `/vantage-ingest <slug>`

```sh
uv run vantage ingest <slug>
```

Claude reads every `work/contact/<date>.jpg`, checks the new visit covers every vantage, and flags
blur, weather, a different lens, privacy problems and missing files. New vantages get an id, a name,
a reference frame and a telemetry hint in `story.yaml`. **You confirm** the table before aligning.

## 3. Align and review (≈ 10–30 min) — `/vantage-align-review <slug>`

```sh
uv run vantage select <slug> && uv run vantage align <slug>
```

Claude reads `work/review/<vantage>/review.json`, `contact.jpg` and the checkerboard/anaglyph sheets
(the `frame-reviewer` agent takes vantages in parallel), then writes fixes into `story.yaml`: a pinned
frame (`picks`) or an excluded visit (`captures[].exclude`), and re-runs until every master is clean.
**You look** at the worst overlays it names, then commit `masters/` and `story.yaml`.

## 4. Write what changed (≈ 30–60 min) — `/vantage-write-story <slug> "what changed in <month>"`

- Optional drafts: `uv run vantage caption <slug>` → `work/llm/suggestions.yaml` (`status: proposed`).
- Claude adds the visit to `captures:` (label, one-line note from `notes.txt` and the imagery), a scrub
  step and any new hotspots, updates the timeline, and puts every new figure in `facts.yaml` with its
  source and `needs-client` unless verified.
- The `fact-checker` agent re-opens sources for anything new.
- **You edit and approve** the copy. Nothing from the LLM ships until you do.

## 5. Verify (≈ 15 min) — `/vantage-verify <slug>`

```sh
uv run vantage all <slug> --no-film
uv run python scripts/budgets.py dist/<slug>
VANTAGE_STORY=<slug> npm run test:web:local
```

Claude reads the iPhone, desktop, no-JS and reduced-motion screenshots in
`web-tests/.results/test-results/shots/`, critiques each chapter and iterates on copy, crops
(`portrait_focus`, `focus`) and pins. **You look** at the two or three screenshots it picks.

## 6. Client review (1–5 days)

- Deploy a password-protected or unlisted preview of `dist/<slug>/site/` (or send the lite file).
- Send the list of facts that need the client's confirmation (from `vantage build --release`).
- Record their answer in `approvals.yaml` and set confirmed facts to `client-approved`
  ([PROJECTS.md](PROJECTS.md#approvals)).

## 7. Publish (≈ 20 min, films included) — `/vantage-publish <slug> <edition>`

Runs only when you invoke it. Release build with the gates on, budgets, checksums, the browser suite
once more, then deploy and the release:

```sh
uv run vantage all <slug> --release
git tag <slug>@2026-10 && git push origin <slug>@2026-10    # in the projects repo: release workflow
```

Claude drafts the share message and you send it with `HOW-TO-VIEW.txt`. Do the real-iPhone checks in
[DELIVERY.md](DELIVERY.md#release-process).

## When something goes wrong

| Problem | Where to look |
|---|---|
| a visit is on the wrong date | `work/catalog.json` `date_source`; put the files in a `YYYY-MM-DD` folder |
| the time-lapse wobbles or ghosts | `/vantage-align-review`; the anaglyph shows which visit |
| colours jump between months | `grade.strength` in `project.yaml` |
| release build refuses | each line names the fact or setting (`draft: true`, unknown or unapproved fact) |
| a file is over budget | `scripts/budgets.py` names it; lower image widths or move video to R2 |
| browser tests fail | `npm run test:web:report`; `web-tests/README.md` lists the DOM contract |
