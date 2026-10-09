# Premium before/after and scrollytelling visuals (2015–2026), applied to drone progress stories

Condensed research notes. Tags: **[V]** verified at the cited source when researched (October 2026);
**[M]** from memory, URL not re-fetched; **[I]** inference or recommendation. Library sizes were
measured from npm tarballs (min + gzip -9). Exemplar URLs tagged [M] should be re-checked before
quoting.

## 1. Exemplars and what to take

| Exemplar | Take |
|---|---|
| NYT "Snow Fall" (2012) [M] | chaptered structure: each chapter opens on a hero visual; text and media take turns |
| NYT "What China Has Been Building in the South China Sea" (2015) [M] | dated sequences of the **same footprint**; captions name what changed; scale cues |
| NYT "Greenland Is Melting Away" (2015) [M] | aerials treated as photography: big type, negative space |
| NYT "What the Tulsa Race Massacre Destroyed" (2021) [M] | the "ghost of what was" device for a place that no longer exists |
| NYT "Why Notre-Dame Was About to Collapse" (2019) [M] | camera moves only while text explains the move; anchored labels |
| ProPublica / Texas Tribune "Hell and High Water" (2016) [M] | sticky map with stepping text |
| [Reuters graphics-components](https://github.com/reuters-graphics/graphics-components) [V] | the best public reference: `BeforeAfter`, `ScrollerVideo`, `Scroller`, `SimpleTimeline`, `PhotoPack` |
| [The Pudding svelte-starter](https://github.com/the-pudding/svelte-starter) [V] | copy kept apart from code (ArchieML): suits an LLM-in-the-loop workflow |
| [NYT ai2html](https://github.com/newsdev/ai2html) [V] | never bake text into images; annotations stay live HTML |
| Apple product pages [M] | scroll-scrubbed image sequences on `<canvas>` beat video seeking; one object, black stage |
| Google Earth Timelapse / [CMU Time Machine](https://github.com/CMU-CREATE-Lab/timemachine-viewer) [V] | zoom *and* scrub time; annotated tours |
| DroneDeploy, Pix4D, OpenSpace [M] | the industry baseline is utilitarian: no narrative, no typography. That is the opening |
| Gaussian splatting: [3DGS](https://github.com/graphdeco-inria/gaussian-splatting) (non-commercial licence), [Spark](https://github.com/sparkjsdev/spark) (MIT), [splat-transform](https://github.com/playcanvas/splat-transform) (MIT) [V] | a per-epoch "walk the site" chapter, using only the MIT tooling |

Reuters details [V]: `BeforeAfter` clips with `clip`/`clip-path: inset()`, handle `role="slider"`
with `aria-valuenow` and arrow keys, but no `aria-valuemin/max/valuetext` (add date text).
`ScrollerVideo` docs recommend 24 fps, 720p H.264, short keyframe intervals, `+faststart`, separate
phone and desktop videos, and **`svh`/`lvh` instead of `vh`** for sticky stages. On WebKit it scrubs by
seeking; its decoded-frame cache is never `close()`d, a plausible cause of the mobile crashes the
docs warn about [I].

## 2. Interaction patterns

| Pattern | Best for | iPhone verdict |
|---|---|---|
| Curtain slider | two-epoch hero comparisons | works; `touch-action: pan-y`, start at 50 %, one affordance nudge. [img-comparison-slider](https://github.com/sneas/img-comparison-slider) (MIT, 3.6 KB gz) as reference; avoid Juxtapose (MPL-2.0, unmaintained) [V] |
| Blink comparator | subtle change | needs sub-pixel registration or it flickers; no auto-blink under reduced motion |
| Opacity crossfade | "dissolve through time" | ghosting shows misregistration |
| Scroll-scrubbed time-lapse | the signature mechanic | sparse epochs → registered stills + crossfade, not 30 fps video [I] |
| Date rail | uneven capture dates | ticks at real calendar positions; 44 pt targets; safe-area bottom bar |
| Sticky graphic + steps | narrative chapters | [Scrollama](https://github.com/russellsamora/scrollama) (MIT, 2.1 KB gz) or ~60 lines of IntersectionObserver; `svh` stages; text backplates |
| Pins persistent across time | "this corner, every month" | store in master coordinates; transform-only positioning |
| Time loupe | another epoch in a circle | `clip-path: circle()`; long-press; offset above the finger |
| Deep zoom | 100+ MP orthomosaics | [OpenSeadragon](https://github.com/openseadragon/openseadragon) (BSD-3, 87 KB gz) uses its canvas drawer on iOS [V] |
| 3D flythrough | "walk the site" | heavy (Spark 922 KB gz); lazy behind a tap, MP4 fallback |
| Haptics | tactile date ticks | iOS has no Vibration API ([BCD](https://github.com/mdn/browser-compat-data/blob/main/api/Navigator.json)); [ios-haptics](https://github.com/tijnjh/ios-haptics) switch trick works on trusted taps only |
| Fullscreen | immersion | no element fullscreen on iPhone ([BCD](https://github.com/mdn/browser-compat-data/blob/main/api/Element.json)); pseudo-fullscreen + Home Screen |

## 3. What makes it feel expensive

1. **Zero-wobble registration**, with the residual error reported in the method notes.
2. **Colour continuity** across visits (match to a reference look; keep seasons honest).
3. **Time made legible**: a date chip on every image, proportional rails, named milestones. Never
   interpolate between epochs with optical flow; it invents construction.
4. **Editorial typography**: a display serif (or the brand face, licence permitting) with a quiet
   sans; tabular numerals for dates; 60–75 character measures; live HTML text over images.
5. **Pacing and restraint**: one idea per screen, dwell holds at reveals, one gesture per graphic, a
   default state that already tells the story.
6. **Motion grammar**: time is scroll-*linked*, text reveals scroll-*triggered*; ease-out, no bounce,
   200–450 ms, transform and opacity only; no scroll-jacking on touch ([Lenis](https://github.com/darkroomengineering/lenis)
   notes Safari capped at 60 fps, 30 in Low Power Mode) [V].
7. **Loading choreography**: inline placeholders ([ThumbHash](https://github.com/evanw/thumbhash) or
   LQIP), explicit aspect ratios, `fetchpriority=high` hero, lazy elsewhere, `content-visibility`.
8. **Quiet chrome**: custom handles, one accent, near-black or paper stage; the brand in masthead,
   credits and accents, not on every surface.
9. **Accessibility as craft**: a designed reduced-motion version; `aria-valuetext` dates.
10. **Editorial credibility**: credits, flight log, sources, "how we made this".

## 4. iOS Safari field guide

| Topic | Fact | Response |
|---|---|---|
| Autoplay | needs `muted` + `playsinline` [V] | always, plus a poster |
| Low Power Mode | WebKit refuses video autoplay in Low Power Mode and under thermal mitigation ([MediaElementSession.cpp](https://github.com/WebKit/WebKit/blob/main/Source/WebCore/html/MediaElementSession.cpp)) [V] | posters and a graceful still; handle `play()` rejection |
| Seeking | precise seeks decode from the previous keyframe | short GOP for scrubbed clips; image sequences for sparse epochs |
| WebCodecs | Safari/iOS 16.4+ [V] | ring buffer of bitmaps, `close()` evicted frames |
| Canvas | max area 8192² device pixels on iOS ([CanvasBase.cpp](https://github.com/WebKit/WebKit/blob/main/Source/WebCore/html/CanvasBase.cpp)) [V] | DPR ≤ 2–3; 3–6 decoded frames at once |
| Device adaptation | no `deviceMemory`/`connection` in Safari [V] | viewport × DPR + a decode benchmark |
| Viewport units | `svh`/`lvh`/`dvh` in Safari 15.4+ [V] | `svh` for sticky stages; not `dvh` for scroll-linked layout |
| Scroll-driven CSS | `animation-timeline` in Safari/iOS 26 and Chrome 115 ([BCD](https://github.com/mdn/browser-compat-data/blob/main/css/properties/animation-timeline.json)) [V] | progressive enhancement behind `@supports` |
| View Transitions | same-document Safari 18, cross-document 18.2 [V] | hosted edition only |
| Motion sensors | `requestPermission()` needed [V] | only after a tap |
| Share, Wake Lock | `navigator.share` 12.1+; Wake Lock fully in 18.4+ [V] | share cards; kiosk mode |

Offline packages on iPhone [M/I, verify on a device]: Safari cannot open `file://`; an `.html` file
tapped in Files opens in Quick Look, believed to run no JavaScript and not to resolve sibling assets.
The downloadable iPhone form is therefore a hosted URL saved to the Home Screen; the zip is for
desktops. Desktop `file://` rules: classic scripts (Chromium blocks module scripts from `file://`), no
`fetch` of local JSON, 2D canvas rather than WebGL for local images.

## 5. Library facts (npm, October 2026)

| Library | Licence | Size (gz) | Use |
|---|---|---|---|
| svelte 5 | MIT | small runtime | used by newsrooms; not needed for ~10 components [I] |
| scrollama 3.2.0 | MIT | 2.1 KB | or custom IntersectionObserver |
| gsap 3.15 | GreenSock standard licence (not OSI) | 28 + 18 KB | keep out of a reusable engine |
| lenis 1.3 | MIT | 5.4 KB | desktop-only, opt-in |
| openseadragon 6.1.1 | BSD-3 | 87 KB | deep zoom (v2) |
| thumbhash 0.1.1 | MIT | tiny | placeholders |
| ios-haptics 3.2.0 | MIT | 0.7 KB | tap-to-step ticks (v2) |
| maplibre-gl 6.13 | BSD-3 | 285 KB + worker | hosted only; needs range requests |
| @sparkjsdev/spark 2.3.1 | MIT | 922 KB | 3D chapter, lazy |

## 6. Ideas beyond the newsroom standard

1. **Same spot, every month**: identical waypoint missions, auto-registered and colour-matched, scrub
   ticks at real dates, labelled milestones.
2. **Pins that persist through time**, each opening a spotlight time-lapse of its crop.
3. **The time loupe**: long-press for another epoch inside a circle; tap to step with a haptic tick.
4. **Plan → built**: the design firm's plan (with permission) registered over the latest flight.
5. **Earth moved**: DSM differencing between mapping flights, shown with uncertainty and a method note.
6. **The ghost layer**: public-domain historical aerials registered to today, revealing what stood
   there before. Keep it factual; former operators' marks are references, never branding.
7. **A 4D splat "walk the site"** per epoch, hosted and lazy, with an MP4 fallback.
8. **Two-axis scrub**: scroll moves along the flight path, swipe changes the month.
9. **On-site "you are here, then"** via a QR sign and geolocation.
10. **"What changed this month"**: change masks + field notes → Claude drafts → a human approves.
11. **Sun-honest pairs** and a raw/normalized toggle as a trust signal.
12. **Share-card postcards** per pin with the Web Share API.
13. **Kiosk / council mode**: auto-tour, idle reset, Wake Lock; optional soundscape on tap.

Plus a **living colophon**: flight log (date, aircraft, altitude, weather), processing steps,
registration error, credits.

## Open items (need a device or more research)

Quick Look behaviour of `.html` packages; scrubbing a paused video in Low Power Mode; ImageBitmap
ring-buffer memory headroom on older iPhones; whether haptics fire during drags; the exact GSAP
licence clauses.
