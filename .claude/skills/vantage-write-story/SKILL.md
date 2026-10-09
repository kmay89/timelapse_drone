---
name: vantage-write-story
description: >-
  Write or update a Vantage story in story.yaml: chapters, scrub steps, compare steps, hotspots,
  captions, alt text and the "what changed" copy for a new flight, with every number or date as a
  {fact:id} token sourced in facts.yaml. Optionally drafts with `vantage caption` (Claude vision) for a
  human to accept. Use for "write the story", "draft captions", "what changed this month", "add a
  chapter", "update the copy", or after /vantage-align-review.
argument-hint: "<project> [what to write]"
allowed-tools: Bash(uv run vantage *), Bash(ls *), Read, Edit, Write, Glob, Grep
---

# Write the story

Request: `$ARGUMENTS`. Goal: story.yaml copy that a client can approve, built only from what the
imagery shows and what a source says.

## Ground rules (non-negotiable)

- **Never invent.** Every number, date, quantity, name or claim goes through `{fact:id}`; the fact
  in `facts.yaml` has `text`, `sources` and a `status`. No source → status `needs-client` and say so.
  Do not upgrade a fact to `verified` unless you read the primary source yourself.
- **Describe only what is visible** in the masters. Sparse flights mean "visible by <date>", never
  "on <date>", unless a source dates it.
- **Brand voice** from `brand/brand.yaml` (`voice.tone`, `voice.avoid`, `voice.examples`).
  Short sentences. No boosterism, no exclamation points.
- **Never ship unaccepted LLM text.** Drafts are proposals until the person approves them.
- Alt text describes the image for someone who cannot see it (≤ 150 characters, no "image of").

## 1. Gather

- `story.yaml`, `facts.yaml`, `brand/brand.yaml`, `project.yaml`; `footage/<date>/notes.txt` for each
  visit; `masters/index.json` for the dates per vantage.
- Look at the masters you write about (Read `masters/<vantage>/<date>.jpg`); compare consecutive
  dates for the "what changed" step.
- Optional drafts from Claude vision (needs credentials; costs well under $1 a flight, cached):

  ```sh
  uv run vantage caption <project>     # → work/llm/suggestions.yaml (status: proposed)
  ```

  Each entry has an analysis, a draft step and hotspot ideas. Treat them as a junior's notes: verify
  against the image, rewrite in the brand voice, and move every figure into a fact.

## 2. Write (models: `src/vantage/config.py`; vocabulary: `docs/BLUEPRINT.md`)

- **Chapters**: `hero`, `text`, `scrub`, `compare` (`mode: curtain|blink`), `video`, `stats`,
  `timeline` (`status: done|in-progress|planned`), `gallery`, `explore`, `credits`. Alternate
  `surface: paper` (history, text) and `night` (imagery). Every chapter must read well untouched: the
  default state tells the story; interaction is a bonus.
- **Scrub steps**: one per moment worth a sentence (`capture: "YYYY-MM-DD"`), optional
  `focus: [x, y, zoom]` (0–1, zoom ≥ 1) to push in on the detail the text names.
- **Compare steps**: `split` 0–1 per step, so scrolling sweeps the curtain from before to after.
- **Hotspots**: `x, y` in the aligned frame (0–1; measure on the master, e.g. from a suggestion's
  box centre), `label` (≤ 4 words), `body` (one sentence), `from`/`to` capture refs for things that
  appear or disappear.
- **Captures**: `label` ("June 2025") and `note` (one line on what changed) for every visit.
- **Facts**: add entries to `facts.yaml` as you go:

  ```yaml
  facts:
    trees-planted:
      text: "78 trees"
      sources: ["Planting plan, sheet L-401 (rev. 2026-03-02)"]
      status: needs-client
  ```

## 3. Check

```sh
uv run vantage validate <project>
uv run vantage build <project>          # draft build: unapproved facts are underlined in red
```

Count what still blocks a release: `uv run vantage build <project> --release` lists every unknown
or unapproved fact and `draft: true`. (A failed release build changes nothing; rebuild without
`--release` afterwards if you need the preview.)

## 4. Checkpoint

Give the person: the diff of story.yaml/facts.yaml, a list of facts with status ≠ verified/approved
and who must confirm each, and anything you were unsure of in the imagery. They approve copy; the
client approves facts (record approvals as the project's `docs/PROJECTS.md` describes). Then run
`/vantage-verify` to see it on an iPhone.

## Failure handling

| Symptom | Fix |
|---|---|
| `unknown fact {fact:x}` | add `x` to facts.yaml or fix the token (lowercase, digits, `-`, `_`) |
| capture reference errors | use `earliest`, `latest`, `#N` (0-based) or a flight date in quotes |
| `vantage caption` returns nothing | no `anthropic` extra (`uv sync --extra llm`) or no credentials; write by hand |
| a suggestion was refused or failed | it is marked `refused`/`failed` and retried next run; never paste it |
| hotspot sits in the wrong place | x/y are fractions of the aligned master, origin top-left |
