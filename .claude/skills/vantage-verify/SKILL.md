---
name: vantage-verify
description: >-
  Build and check a Vantage story the way a reader will meet it: build, package, size budgets, the
  Playwright suite (iPhone and desktop, JavaScript on and off, reduced motion, file://), then read the
  checkpoint screenshots, critique them like an art director and iterate until it looks right. Use for
  "verify", "check the build", "how does it look on an iPhone", "screenshot the story", "is it ready",
  before /vantage-publish, and after any change to story.yaml, brand.yaml or the runtime.
argument-hint: "[project] (default demo-lakeside)"
allowed-tools: Bash(uv run vantage *), Bash(uv run python scripts/budgets.py *), Bash(npx playwright test *), Bash(npm run test:web*), Bash(uv run pytest *), Bash(ls *), Read, Glob, Grep
---

# Verify a story

Project: `$ARGUMENTS` (default `demo-lakeside`). Read-only for the project: this skill builds and
looks; fixes go through the owning skill (copy → `/vantage-write-story`, frames →
`/vantage-align-review`) or a code change with tests.

## 1. Build everything

```sh
uv run vantage all <project> --no-film     # films are slow; add them back for a release check
```

Demo: `uv run vantage demo --fast --no-film`. A story builds from committed masters when footage is
absent. Note the slug: outputs are in `dist/<slug>/`.

## 2. Gates and budgets

```sh
uv run python scripts/budgets.py dist/<slug>
uv run vantage build <project> --release   # lists release blockers (draft, unapproved facts); then rebuild without it
```

## 3. Browser tests and screenshots

```sh
VANTAGE_STORY=<slug> npm run test:web:local      # iPhone 15 Pro + desktop on Chromium
```

(CI runs the same specs on iPhone WebKit and iPhone SE WebKit.) Specs: `smoke` (no errors, every
chapter, images load, no sideways scroll), `nojs` (site, single and lite with JS off: the Quick Look
case), `interactions` (scrub, curtain slider, hotspots, chapter index; also with reduced motion),
`file` (file:// editions, no network). Failures print the role/ARIA contract they expect; see
`web-tests/README.md`. `npm run test:web:report` opens the HTML report with traces.

## 4. Look (the important part)

Screenshots: `web-tests/.results/test-results/shots/<playwright-project>/<mode>-<NN>-<chapter>[-k].jpg`
(`js-…`, `nojs-site-…`, `nojs-lite-…`, `reduced-motion-…`). Read them with the Read tool, iPhone
first. For each chapter judge, and write down concrete fixes:

- **First screen**: does the hero fill the phone edge to edge, is the title legible over the image, is
  there a reason to scroll? Logo crisp, not cramped by the notch.
- **Typography**: measure 60–75 characters, no orphans in headlines, dates in tabular figures,
  hierarchy obvious at arm's length.
- **Imagery**: portrait crops centred on the subject (`portrait_focus`), no soft upscaling, no
  misregistered ghosting in scrubs, labels never covering the thing they name.
- **Contrast**: text over images has a scrim; paper/night surfaces alternate with intent.
- **No-JS / lite**: still a complete photo essay; nothing invisible waiting for a script; "Also flown"
  lines instead of broken tiles.
- **Honesty**: simulated or draft disclosures present where required; every figure has a note.
- **Chrome**: nothing overlaps the home indicator; no horizontal scroll; tap targets ≥ 44 pt.

## 5. Iterate

Fix the highest-impact issue, rebuild (`uv run vantage build <project>` takes about a second with the
image cache), rerun the relevant spec (`npx playwright test -c web-tests/playwright.config.mjs
--project=iphone-chromium smoke`), look again. Stop when a pass finds nothing worth a client's time.

## 6. Report

Summarize: tests (passed/failed/skipped and why), budgets table, release blockers, the screenshots
you looked at with your verdict per chapter, and the fixes made or proposed. Include the paths of the
two or three screenshots that best show the current state.

## Failure handling

| Symptom | Fix |
|---|---|
| `Executable doesn't exist` (Playwright) | `npx playwright install chromium` (WebKit only in CI unless installed) |
| web server timeout | another server on 4173: `VANTAGE_PORT=4174 npm run test:web:local` |
| every interaction test fails on a missing role | the runtime JS did not load: check the page for errors (`smoke`) |
| `missing …lite.html` from budgets | run `uv run vantage package <project>` |
| screenshots blank or grey | images still loading: check `smoke` for failed requests; rebuild the site |
