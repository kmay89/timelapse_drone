# Vantage blueprint

The product vision and the design decisions behind it. `ARCHITECTURE.md` is the contract for the
code; this file explains what the code is for and where it is going. Background research, with
sources: [`research/`](research/).

## 1. The vision

### The piece in one paragraph

A link arrives by text. You tap it, the screen goes dark for half a beat, and the site fills the
phone edge to edge: the latest flight, the light low, one line of display type settling over it and a
date range underneath in small tabular figures. The first minute is paper and ink: what was here,
why it closed, what the plan is. Then the camera locks onto the site and a calendar rail along the
bottom ticks from the first flight to the last as you scroll. Nothing wobbles: every visit is
registered to the same frame, so only the site changes. Pins stay fixed on the places that matter
and say what changed there. A curtain lets you drag between the first flight and the last. The piece
ends wide and quiet on the finished place, with one line of type. Below sit the credits, the flight
log and every source. It never stutters, works with the sound off, and can be saved to the Home
Screen and watched in airplane mode.

The fictional demo, *Where the Wave Pool Was* (`projects/demo-lakeside`), is this piece in miniature:
an abandoned waterpark handed back to a lake over twelve flights.

### What "beyond the newsroom before/after" means

Newsroom before/after pieces are one-offs, photographed once. A Vantage story is:

- **locked-camera rigorous**: registered, colour-matched and measured (alignment error is reported);
- **living**: it updates after every flight, each edition permanent at its own URL;
- **touch-native**: designed for a thumb on a phone first, with a desktop layout second;
- **honest about method**: flight log, sources for every figure, disclosures for anything simulated;
- **offline-capable**: Home Screen, single file, offline zip, films.

### Minute by minute (a typical eight-minute story)

| Time | Chapter | What the thumb does | What the reader feels |
|---|---|---|---|
| 0:00–0:20 | Cold open (hero) | nothing yet | "this is a film" |
| 0:20–1:30 | History on paper (text, timeline, gallery) | scroll steps through archival images | deep time |
| 1:30–3:00 | The long view (scrub) | scroll moves through every flight | "it's the same ground" |
| 3:00–4:00 | Before and after (compare) | drag the curtain | the payoff, in one gesture |
| 4:00–5:00 | By the numbers, what comes next (stats, timeline) | read | scale, and a reason to come back |
| 5:00+ | Explore, credits | free play | trust |

### The moments that make it, and what each needs

1. **The locked-camera scrub**: repeat waypoint flights, homography + ECC registration, a canvas
   crossfade with dwell on each visit, pins in master coordinates.
2. **The curtain**: two registered epochs, a scroll-linked sweep that hands over to direct drag.
3. **The "same ground" dissolve** (v2): archival or public aerial imagery registered to today's frame.
4. **The time lens** (v2): long-press to see another epoch inside a circle under the finger.
5. **The quiet ending**: a still image, one line of type, no confetti.

## 2. Chapter vocabulary

Every chapter reads well untouched: its default state already tells the story, and interaction is a
bonus. Surfaces alternate with intent: `night` for imagery, `paper` for words and history.

| Type | For | Reader does | Without JavaScript |
|---|---|---|---|
| `hero` | the cold open: title over the latest flight, optional ambient loop | nothing | full-bleed picture and title |
| `text` | history, context, a pull quote | reads | the same |
| `scrub` | every flight of one vantage, pinned, scroll moves time; steps, focus pushes, pins | scrolls; taps pins or rail ticks | a dated sequence of figures with captions |
| `compare` | two visits, curtain (or blink) with steps that sweep the split | scrolls, then drags | the pair stacked, labelled |
| `video` | a moving shot: the last evening of the old place, a flyover | plays | poster image |
| `stats` | the numbers, each a sourced fact | reads | the same |
| `timeline` | dated milestones with status (done, in progress, planned) | reads; taps through to a flight | the same |
| `gallery` | a dated filmstrip of captures or archival images with credits | swipes | a strip of figures |
| `explore` | free play: pick any two flights, scrub any vantage | plays | a grid of every flight |
| `credits` | partners, method, notes and sources, flight log, disclaimers | reads | the same |

Planned: `map` (static SVG layers drawn at build time: shorelines, parcel lines, a county line drawing
itself on scroll) and archival vantages (`kind: archival`) registered in a shared geographic frame.

Editorial ground rules: no fake sepia (archival images appear as they are); every figure is a
`{fact:id}`; "visible by <date>", never "on <date>", for anything inferred from sparse flights; never
interpolate frames between visits (it invents construction).

## 3. Feature tiers

### v1: buildable with no client footage (the engine today)

| # | Feature | Why it earns v1 |
|---|---|---|
| 1 | Pinned scroll scrub: two-layer canvas crossfade, dwell easing, calendar rail with real-date ticks, persistent pins | the signature mechanic |
| 2 | Curtain compare: scroll-linked sweep, then hand-off to drag; `aria-valuetext` dates; pins on both sides | highest wow per line of code |
| 3 | Editorial type system, brand theming, paper/night rhythm, LQIP load choreography | most of what reads as "expensive" |
| 4 | Pre-rendered no-JS photo essay; CSS scroll-driven enhancement where supported | the downloaded file must still impress |
| 5 | Editions: hosted PWA, full single file, lite single file, offline zip, 16:9 and 9:16 films, share card | the "downloadable" promise |
| 6 | Living colophon (flight log, method, sources), facts gate, release gates | credibility, repeatability |
| 7 | Pseudo-fullscreen stages (`100svh`), safe-area insets | iPhone has no element fullscreen |
| 8 | Optional Claude vision drafts for captions, alt text and pins, accepted by a human | faster writing per flight |

### v2: after two or three disciplined flights

| Feature | Notes |
|---|---|
| Time lens: long-press loupe onto another epoch, tap-to-step with haptic ticks | iOS has no Vibration API; the `<input type=checkbox switch>` trick fires only on trusted taps |
| Blink comparator | only for captures with sub-1.5 px registration |
| Plan-to-built overlay | the design firm's drawing registered over the latest flight; needs their permission |
| Archival "same ground" dissolve and satellite-decade scrub | public-domain aerials (e.g. USDA NAIP, USGS historical, state orthophoto programs), co-registered |
| Spotlight time-lapse per pin | a full-resolution crop of one spot across every visit |
| Deep-zoom orthomosaic with a time slider | OpenSeadragon (BSD-3) with its canvas drawer on iOS |
| "What changed" chapter per flight | Claude drafts from change masks and field notes; a human approves |
| Share cards per pin, Web Share | before/after diptych images |
| Kiosk / council mode | auto-tour, idle reset, Wake Lock (iOS 18.4+) |
| Cut/fill heat map | DSM differencing from mapping flights (OpenDroneMap run as a separate tool) |

### v3: once the mission has a year of repeats

| Feature | Notes |
|---|---|
| 4D flythrough: same route each visit, frames matched by distance along it; scroll moves the camera while the date advances | needs strict waypoint missions; pre-rendered MP4 offline |
| Two-axis scrub: vertical scroll through space, horizontal swipe through months | depends on the 4D data |
| Gaussian splat per epoch, "walk the site" | MIT renderers (Spark, PlayCanvas); never the non-commercial reference code; hosted only, lazy |
| On-site QR "you are here, then" | geolocation places the visitor on past epochs |
| AR Quick Look of the site model, ambient soundscape | optional, off by default |

### Explicitly not doing

- Optical-flow or AI frame interpolation between visits (it invents construction).
- Scroll-jacking on touch (smooth-scroll libraries cap Safari at 60 fps, 30 in Low Power Mode).
- Autoplaying sound. WebGL or GSAP in the core. Commercial map imagery with display restrictions.
- Logos of businesses that used to operate on a site; history is told in words and public imagery.

## 4. Operating model

- **The repo does everything deterministic** and builds with no LLM and no network.
- **Claude Code is the operator** for judgment calls, through `.claude/skills/` (ingest, alignment
  review, writing, verification, publishing, flight plans) and subagents (frame reviewer, fact checker,
  rights auditor).
- **The optional Claude API step drafts text only** (`vantage caption`); a human accepts it.
- **Gates block release builds**: drafts, unknown or unapproved facts, budget overruns.
- Claude never decides: facts without a source, frame interpolation, unaccepted text, logo use, rights.

## 5. Front-end decisions

### Stack

- **Vanilla ES2020, no runtime dependencies**, one IIFE with inlined CSS and StoryJSON. HTML is
  pre-rendered by Python (Jinja2 + markdown-it), so the page is complete before any script runs.
- **Why**: Python-only builds (no npm supply chain at build time; Node is test-only); a chapter
  vocabulary of about ten components does not repay a framework; classic scripts work from `file://`
  where module scripts do not; maps become static SVG because tile servers need HTTP range requests.
- **Optional islands** later, loaded only when needed: OpenSeadragon for deep zoom, a splat renderer
  for 3D (hosted edition only, behind a tap).

### Budgets (CI: `scripts/budgets.py`)

| Item | Budget |
|---|---|
| Runtime JS | ≤ 30 KB gzip |
| CSS | ≤ 15 KB gzip |
| Lite single file | ≤ 15 MB (email) |
| Any hosted file | ≤ 25 MiB (static hosts' per-file limit) |
| Full single file | ≤ `output.single_file_max_mb` (video dropped first) |

### Scroll engine

- One `requestAnimationFrame` loop for the page; IntersectionObserver activates chapters; pinned
  progress from `getBoundingClientRect()` only while active. Passive listeners only.
- Sticky stages are `height: 100svh` (never `vh`/`dvh` for scroll-linked layout: the toolbar collapse
  resizes them), `viewport-fit=cover` plus `env(safe-area-inset-*)` for the bottom rail.
- Off-screen chapters get `content-visibility: auto`; the hero is `fetchpriority=high`, everything
  else lazy with idle prefetch of the next chapter.
- Where `@supports (animation-timeline: view())` holds (Safari/iOS 26), step reveals run in CSS.

### The scroll-scrubbed time-lapse

| Content | Technique |
|---|---|
| 2–60 registered captures (every v1 chapter) | canvas, two layers, crossfade between decoded stills |
| 60–240 frames (orbit, flyover; v2) | image sequence on canvas, ring buffer |
| more than 240 frames (4D flythrough; v3) | short-GOP H.264 seek, WebCodecs as an enhancement |

```js
// p: 0..1 progress through the pinned section; n: captures; hold: share of each segment spent still
function scrubState(p, n, hold = 0.55) {
  const u = Math.min(Math.max(p, 0), 1) * (n - 1);
  const i = Math.min(Math.floor(u), n - 2), f = u - i;
  const t = f <= hold ? 0 : (f - hold) / (1 - hold);
  return { i, j: i + 1, a: t * t * (3 - 2 * t) };   // smoothstep weight of capture j
}
```

- Scroll is uniform per capture, so long gaps never feel dead; the rail places ticks by real calendar
  date, so time stays honest. Tapping a tick scrolls to that capture's hold.
- Memory: backing store = CSS size × min(DPR, 2), capped near 2.2 MP; at most five decoded bitmaps;
  `createImageBitmap` at exactly the backing size and `close()` on eviction. iOS caps canvas area at
  8192² device pixels.
- Portrait art direction: a 16:9 aerial full-width on a portrait phone is 220 pt tall and looks
  cheap, so stages fill `100svh` with a cover crop centred on `portrait_focus`, and steps pan with
  `focus: [x, y, zoom]`. Higher-resolution masters keep the portrait crop sharp.
- Accessibility: the canvas has `role="img"` and a label naming vantage and date, announced through a
  polite live region at each hold. Reduced motion: stepped crossfades, focus pushes become cuts.

### The curtain (and blink, and lens)

- Two layers in the sticky stage; the top ("after") is clipped with `clip-path: inset(…)`.
- Handle: a 1 px rule and a 44 pt grip; dates on each side in the numeric face; `role="slider"`,
  `aria-valuemin=0`, `aria-valuemax=100`, `aria-valuenow`, `aria-valuetext` naming both dates; arrows
  ±2 %, PageUp/PageDown ±10 %, Home/End.
- Scroll-linked first (steps set `split`), then direct manipulation: `touch-action: pan-y`; a drag
  that is more than 8 px and 1.7× more horizontal than vertical captures the pointer, and a single tap
  moves the curtain to the tap (WCAG 2.2 SC 2.5.7: no drag-only control); after the first drag or tap,
  scroll stops driving the curtain until the chapter is left.
- One affordance nudge on first entry (skipped under reduced motion).
- Hotspots in normalized master coordinates, transformed with the image; a tap opens a bottom sheet
  inside the safe area, never a hover tooltip.

## 6. iOS checklist (built into the runtime, re-checked on a device per release)

- **Video**: `muted playsinline` and a poster on every `<video>`; handle `play()` rejection (Low Power
  Mode and thermal limits refuse autoplay). HEVC tagged `hvc1` first, then H.264; no AV1-only paths.
- **Viewport**: `svh` stages, safe-area insets, no orientation lock, pseudo-fullscreen (iPhone has no
  element fullscreen).
- **Memory**: no `navigator.deviceMemory` on iOS; adapt from viewport × DPR and a one-frame decode
  timing; bounded bitmap caches.
- **Sensors**: DeviceOrientation needs `requestPermission()` on a tap (v3 only).
- **Downloaded files**: an `.html` opened from Files or Mail is shown by Quick Look, most likely with
  JavaScript off: the lite file must read as a photo essay with nothing hidden behind a script.
- **Offline**: Home Screen web apps get roughly 60 % of disk per origin (iOS 17+); script storage can
  be evicted after 7 days without use in Safari; call `navigator.storage.persist()`.
- **Real-device pass per release** (Playwright WebKit is not iOS Safari): Quick Look of the lite and
  full files; unzipped folder in Files; PWA offline in airplane mode; video through the service worker;
  Low Power Mode; memory on an older iPhone.
