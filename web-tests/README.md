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
| `smoke` | chapters render in StoryJSON order; the chapter locators find ids such as `2025` and `phase-1.5`; scrolling the whole story gives no console errors, page errors, HTTP errors or failed requests, and every on-screen image loads; no sideways scroll; stats figures fill their rows evenly and never run past their column (at the device width and at 768 px); checkpoint screenshots |
| `nojs` | JavaScript off (the iOS Quick Look case) for the site, the single file and the lite file: headline, hero, every chapter title and text paragraph visible at full opacity; every `<img>` has `alt`, figure images non-empty; images load; no sideways scroll; screenshots |
| `interactions` | the runtime's controls by role and ARIA (contract below), and that screen readers get every stats figure's real value, with default motion and again with `prefers-reduced-motion: reduce` (set through `contextOptions`), where every control still works, every stop a Tab walk reaches can be seen, nothing errors and the hero has no pause button and never plays its loop; default motion only: the pinned scrub stage, the hero pause button, the explore flight pickers at 16 px or larger (no iOS focus zoom), explore decoding its flights at the size it draws them and sharper only while zoomed in, a compare step with no text (made by editing the StoryJSON in flight) leaving the next step's card visible, and save for offline failing (a stubbed service worker that never activates, is refused or runs out of space); with the real service worker (Chromium): a first visit stores no image the page did not ask for beyond the opening hero's fallback JPEGs, a save whose downloads fail (404) ends in "Try again", and after an update the saved copy keeps its files, drops what the update removed and fetches what changed without a tap |
| `file` | `file://` loads of `site/index.html`, the unzipped offline zip, the single file and the lite file: right `data-edition`, every chapter, images load, zero errors, **zero network requests** |

Interaction tests skip (with a reason) only when the build lacks what they test: a chapter of that kind
(or a blink mode, or two titled chapters), or, for save for offline, a hosted `http(s)` edition in a
browser with service workers. The real-worker tests run on Chromium only (Playwright's handle on a
service worker is Chromium's); WebKit's worker is checked on a device (`docs/DELIVERY.md`).

## Screenshots

Checkpoints are written to `web-tests/.results/test-results/shots/<project>/<name>.jpg` and attached
to the report. Names: `<mode>-<NN>-<chapter-id>[-k].jpg`, where mode is `js`, `nojs-site`,
`nojs-single`, `nojs-lite` or `reduced-motion`, and `k` is 0/1/2 for the top, middle and end of tall
pinned chapters. They are artifacts for people and for Claude to look at, not golden images.
Playwright clears `.results/test-results/` at the start of each run.

## DOM contract assumed by `interactions`

| Feature | Contract |
|---|---|
| chapters | `<section id="<chapter id>" data-type="<type>">` in StoryJSON order (rendered by `site/render.py`); an id may start with a digit or hold a dot (`2025`, `phase-1.5`), so specs find it with `section()` / `byId()` from `support.mjs` (`[id="…"]`), never `#id` |
| hero pause | in each hero with a picture, a `button` named "Pause the background video" ("Pause the background motion" when there is no video, only the still's slow drift); activating it pauses and renames it "Play the background video" / "…motion"; the choice is kept per story for the session (`sessionStorage` `vantage:<slug>:still`), so the loop stays paused after a reload; with reduced motion the button is hidden and the loop never plays |
| video chapter | the browser's control bar is replaced by one round `button` named "Play the video" or "Pause the video" (following the player); a tap on the picture toggles it too |
| scrub stage | a `<canvas>` inside the scrub section that stays pinned (same top within 2 px) while the section scrolls |
| scrub date | the current capture label is exposed as text: in an `aria-live` region, on the `aria-current` rail tick, or as the only on-screen element whose text is a capture label; first capture at the section's start, last at its end |
| curtain | an element with `role="slider"` in each non-blink compare section: `aria-valuemin="0"`, `aria-valuemax="100"`, `aria-valuenow`, `aria-valuetext` naming the dates; Home/End, arrows and PageUp/PageDown move it; a horizontal pointer drag moves it; a single tap on the picture moves it to the tap (WCAG 2.2 SC 2.5.7); scrolling through steps with `split` sweeps it; a step with no text (an empty card) never hides the other steps' cards |
| hotspots | a `button` whose accessible name contains the hotspot label; Enter opens a `role="dialog"` containing the label; Escape closes it |
| keyboard focus | Tab from the top of the page reaches only elements that can be seen (opacity along the ancestor chain above 0.5 once an entrance fade ends): a faded step card's links and a faded pin leave the tab order; on screen in a scrub section, a step card's `link`s are tabbable exactly while the card shows |
| explore blink | the explore chapter's `button` named "Blink" shows a toggle `button` named "Show <before flight label>" with `aria-pressed`; Space and Enter toggle it, and while it is pressed the stage (`role="img"`, named "<vantage name>, <flight label>") shows the before flight; the toggle is hidden in the other modes |
| explore curtain | the explore chapter's `button` named "Curtain" shows a `role="slider"` named "Divider" (`aria-valuenow` 0–100); a single tap on the stage, not zoomed in, moves it to the tap |
| explore pickers | after the explore chapter's `button` "Curtain", two `combobox`es named "Before flight" and "After flight" with a computed `font-size` of at least 16 px (iOS Safari zooms the page into a smaller focused control) |
| explore zoom | a double-click on the stage (`role="img"`, named after the vantage) zooms in and shows a `button` "Reset zoom"; the decoded flights it draws (`ImageBitmap`s) sample at most 1.05 source px per canvas px at rest, are re-decoded while zoomed in to at least the smaller of the widest variant in the capture's srcset and 1.5 × the resting width, and are back to at most 1.05 after Reset zoom |
| chapter index | a `button` named like "Chapters", "Contents" or "Index"; it shows a link per titled chapter; following one brings that chapter into view and moves focus to its heading, so the next Tab carries on from there |
| chrome order | the `navigation` "Story" (Share, the Contents `button`) comes before `<main>` in the DOM, so it follows the skip link in the tab order as it sits at the top of the screen |
| explore from a flight | a timeline thumbnail `button` "View the <flight label> flight" opens a `role="dialog"`; when the explore chapter covers that vantage, its `button` "Explore from this flight" closes it and moves focus to the explore chapter's heading (the section when it has no title) |
| stats figures | in a stats section's last list, each `listitem`'s accessible text starts with its StoryJSON `items[].value`, once: from page load (before the figure is scrolled to and counts up from zero) and after the count-up |
| save for offline | hosted edition (`<html data-sw>` over `http(s)`) with service workers: the chapter index sheet has a `button` "Save for offline", disabled while saving; a worker that never starts (20 s) or stalls (45 s), a refused registration or a full disk hides the `progressbar`, says so in the status text ("Saving didn’t start…", "…stalled…", "This browser can’t save a copy right now…", "Not enough free space…", "Couldn’t save everything…", also announced in a polite live region) and enables a `button` named "Try again"; a finished save shows a disabled `button` "Saved for offline" and status text starting "Saved for offline" |
