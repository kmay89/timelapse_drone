---
name: vantage-align-review
description: >-
  Align every visit to its vantage and review the result: run `vantage select` and `vantage align`,
  read the review sheets in work/review (checkerboards, red/cyan overlays, review.json metrics), fix
  wrong picks and unusable visits in story.yaml (picks / captures.exclude), re-run until every master
  is locked, then hand the masters to the person to commit. Use for "align", "process the flights",
  "the time-lapse wobbles/ghosts", "check the masters", or after /vantage-ingest.
argument-hint: "<project>"
allowed-tools: Bash(uv run vantage *), Bash(uv run python *), Bash(ls *), Read, Edit, Glob, Grep
---

# Align and review the masters

Project: `$ARGUMENTS`. Goal: one registered, color-matched master per vantage per visit in
`masters/`, each one looked at, every override recorded in `story.yaml` (never only in `work/`).

## 1. Run

```sh
uv run vantage select <project>     # best frame per vantage per visit → work/selection.json
uv run vantage align <project>      # → masters/<vantage>/<date>.jpg, masters/index.json, work/review/**
```

(`uv run vantage process <project>` runs ingest + select + align in one go.)

## 2. Review each vantage

For every `work/review/<vantage>/`:

1. Read `review.json`: per capture `align.ok`, `inliers`, `inlier_ratio`, `rmse_px`, `overlap`, and the
   common `crop.retained`. Rough bar: ok when inliers ≥ 150, rmse ≤ 1.5 px, overlap ≥ 0.6; crop
   retained ≥ 0.7.
2. Open `contact.jpg` (every master side by side): same framing in every tile? A tile that is a
   different viewpoint, upside down or mostly sky is a wrong pick.
3. Open `<date>.jpg` for every capture flagged by the metrics, and a sample of the rest. Top half is a
   checkerboard of reference and capture: roads, kerbs and roof lines must continue straight across
   tile edges. Bottom half is an anaglyph (red = reference, cyan = capture): stable ground should be
   grey; red/cyan fringes on stable edges mean misregistration. New construction is supposed to
   differ: judge only things that did not change (roads, shorelines, old buildings).

With more than a handful of dates, send each vantage's folder to the `frame-reviewer` agent and
merge its verdict tables.

## 3. Fix in story.yaml (then re-run step 1)

| Verdict | Fix |
|---|---|
| wrong frame picked | pin one: `vantages[<id>].picks: {"YYYY-MM-DD": {source: "<path under footage/>", t: <s>}}` chosen from `work/contact/<date>.jpg` |
| visit unusable (fog, blur, wrong lens, nothing visible) | `captures: [{date: YYYY-MM-DD, exclude: true, note: "why"}]` |
| one visit shrinks the crop for all | exclude it, or give that viewpoint its own vantage id |
| poor registration across seasons | lower `align.min_inliers` a little, or try `align.method: affine` (project.yaml); check again |
| colors jump between visits | `grade.strength` (0 keeps each flight's light, 1 fully matches; 0.5–0.6 keeps seasons) |
| reference itself is poor | change `vantages[<id>].reference` to a sharper, well-lit frame |

Never "fix" a visit by inventing frames or interpolating between visits.

## 4. Checkpoint with the person

Show a table per vantage: date · inliers · rmse · ok · your verdict · fix applied. Attach the paths of
the worst two overlays so they can look. Only when they agree:

```sh
uv run vantage validate <project>
```

and tell them to commit `masters/` and `story.yaml` in the private projects repo (masters are small
JPEGs and let CI rebuild the story without footage). Next: `/vantage-write-story`.

## Failure handling

| Symptom | Fix |
|---|---|
| `nothing on <date> registers to the reference` | that visit missed the viewpoint: pin a pick by hand or exclude it |
| `common crop keeps only N%` warning | one capture is badly offset: find it in `contact.jpg`; exclude or re-pick |
| `align` very slow or killed | `align.detect_width` lower (1280), close other jobs; masters stream one frame at a time |
| alignment flagged but looks right | small inlier count on snow or water is normal: record "checked by eye" in the capture note |
| no `work/` (fresh clone) | `vantage ingest` first; footage must be present (masters alone are enough only for build) |
