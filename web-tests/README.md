# Web tests

Playwright tests for a **built** story. Every expectation is read from the StoryJSON embedded in the
page (`<script id="vantage-story">`), so the suite runs unchanged against the demo or a client build.

```sh
uv run vantage demo --fast --no-film                 # or any `vantage all <project>`
npm ci
npm run test:web:local                               # iPhone 15 Pro + desktop, on Chromium
npm run test:web:report                              # HTML report with traces of failures
npx playwright test -c web-tests/playwright.config.mjs --project=iphone-chromium nojs   # one spec
```

The config starts `uv run vantage preview <slug> --port 4173` (or reuses a server already there).

| Variable | Default | Meaning |
|---|---|---|
| `VANTAGE_STORY` | `demo-lakeside` | slug of the build under test |
| `VANTAGE_DIST` | `dist` | output root holding `<slug>/` |
| `VANTAGE_PORT` | `4173` | preview server port |
| `VANTAGE_URL` | — | test an already-hosted copy (no local server); file:// specs still use `dist/` |

## Projects

| Project | Device | Where |
|---|---|---|
| `iphone-webkit` | iPhone 15 Pro (393×659 viewport, DPR 3), WebKit | CI |
| `iphone-se-webkit` | iPhone SE (320×568, DPR 2), WebKit | CI |
| `desktop-chromium` | 1440×900, Chromium | CI and local |
| `iphone-chromium` | iPhone 15 Pro metrics on Chromium | local stand-in when WebKit is not installed |

Playwright's WebKit is not iOS Safari: real-device checks per release are listed in
`docs/DELIVERY.md`.

## Specs

| Spec | Checks |
|---|---|
| `smoke` | chapters render in StoryJSON order; scrolling the whole story gives no console errors, page errors, HTTP errors or failed requests, and every on-screen image loads; no sideways scroll; stats figures fill their rows evenly and never run past their column (at the device width and at 768 px); checkpoint screenshots |
| `nojs` | JavaScript off (the iOS Quick Look case) for the site, the single file and the lite file: headline, hero, every chapter title and text paragraph visible at full opacity; every `<img>` has `alt`, figure images non-empty; images load; no sideways scroll; screenshots |
| `interactions` | the runtime's controls by role and ARIA (contract below), with default motion and again with `prefers-reduced-motion: reduce` (set through `contextOptions`), where every control still works, nothing errors and the hero has no pause button and never plays its loop; default motion only: the pinned scrub stage, the hero pause button, and save for offline failing (a stubbed service worker that never activates, is refused or runs out of space) |
| `file` | `file://` loads of `site/index.html`, the unzipped offline zip, the single file and the lite file: right `data-edition`, every chapter, images load, zero errors, **zero network requests** |

Interaction tests skip (with a reason) only when the build lacks what they test: a chapter of that kind
(or a blink mode, or two titled chapters), or, for save for offline, a hosted `http(s)` edition in a
browser with service workers.

## Screenshots

Checkpoints are written to `web-tests/.results/test-results/shots/<project>/<name>.jpg` and attached
to the report. Names: `<mode>-<NN>-<chapter-id>[-k].jpg`, where mode is `js`, `nojs-site`,
`nojs-single`, `nojs-lite` or `reduced-motion`, and `k` is 0/1/2 for the top, middle and end of tall
pinned chapters. They are artifacts for people and for Claude to look at, not golden images.
Playwright clears `.results/test-results/` at the start of each run.

## DOM contract assumed by `interactions`

| Feature | Contract |
|---|---|
| chapters | `<section id="<chapter id>" data-type="<type>">` in StoryJSON order (rendered by `site/render.py`) |
| hero pause | in each hero with a picture, a `button` named "Pause the background video" ("Pause the background motion" when there is no video, only the still's slow drift); activating it pauses and renames it "Play the background video" / "…motion"; the choice is kept per story for the session (`sessionStorage` `vantage:<slug>:still`), so the loop stays paused after a reload; with reduced motion the button is hidden and the loop never plays |
| video chapter | the browser's control bar is replaced by one round `button` named "Play the video" or "Pause the video" (following the player); a tap on the picture toggles it too |
| scrub stage | a `<canvas>` inside the scrub section that stays pinned (same top within 2 px) while the section scrolls |
| scrub date | the current capture label is exposed as text: in an `aria-live` region, on the `aria-current` rail tick, or as the only on-screen element whose text is a capture label; first capture at the section's start, last at its end |
| curtain | an element with `role="slider"` in each non-blink compare section: `aria-valuemin="0"`, `aria-valuemax="100"`, `aria-valuenow`, `aria-valuetext` naming the dates; Home/End, arrows and PageUp/PageDown move it; a horizontal pointer drag moves it; scrolling through steps with `split` sweeps it |
| hotspots | a `button` whose accessible name contains the hotspot label; Enter opens a `role="dialog"` containing the label; Escape closes it |
| explore blink | the explore chapter's `button` named "Blink" shows a toggle `button` named "Show <before flight label>" with `aria-pressed`; Space and Enter toggle it, and while it is pressed the stage (`role="img"`, named "<vantage name>, <flight label>") shows the before flight; the toggle is hidden in the other modes |
| chapter index | a `button` named like "Chapters", "Contents" or "Index"; it shows a link per titled chapter; following one brings that chapter into view |
| save for offline | hosted edition (`<html data-sw>` over `http(s)`) with service workers: the chapter index sheet has a `button` "Save for offline", disabled while saving; a worker that never starts (20 s) or stalls (45 s), a refused registration or a full disk hides the `progressbar`, says so in the status text ("Saving didn’t start…", "…stalled…", "This browser can’t save a copy right now…", "Not enough free space…", "Couldn’t save everything…", also announced in a polite live region) and enables a `button` named "Try again" |
