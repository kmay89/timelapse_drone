# Vantage — architecture & contracts

Vantage turns repeat drone flights over a changing site into a cinematic,
iPhone-first interactive progress story (plus MP4 time-lapse films cut from the story),
themed from a client's brand kit. This file is the **contract** between the
modules: if you change a format or a function signature here, change it
everywhere.

```
footage/ (raw, never committed)                  brand/ + story.yaml + project.yaml
        │                                                   │
        ▼                                                   │
  ingest ──► catalog.json + candidate frames                │
        │                                                   │
        ▼                                                   │
  select ──► selection.json (best frame per vantage/visit)  │
        │                                                   │
        ▼                                                   │
  align + grade ──► masters/<vantage>/<date>.jpg            │   ◄── committed (small)
                    masters/index.json                      │
        │                                                   │
        ├──────────────► build ◄────────────────────────────┘
        │                  │  story.json (inlined), responsive images, theme CSS,
        │                  │  runtime JS/CSS, share card, icons, sw.js
        │                  ▼
        │           dist/<slug>/site/index.html      (hosted; works offline via SW)
        │           dist/<slug>/<slug>.html          (single self-contained file)
        │           dist/<slug>/<slug>-offline.zip   (site folder, opens from disk)
        └──► film ─► plan_films ─► <slug>-<id>.plan.json + .storyboard.jpg   (the Reel Ledger)
                         └──► render ─► dist/<slug>/film/<slug>-16x9.mp4, <slug>-9x16.mp4 (+ posters)
                                          ▲ package --release re-plans and refuses stale films
```

## Repository layout

```
src/vantage/
  cli.py                 Typer app; every command is a thin wrapper over a module function
  config.py              Pydantic models for project.yaml / story.yaml / brand.yaml  (SOURCE OF TRUTH)
  paths.py               repo/projects/dist resolution, project lookup by slug or path
  log.py                 tiny rich-free console logger (info/ok/warn/fail + step timers)
  media/ffmpeg.py        ffprobe/ffmpeg wrappers (probe, extract frame, iterate frames, encode)
  ingest/
    catalog.py           discover footage, detect dates, build work/catalog.json
    dji_srt.py           DJI .SRT telemetry parser
    exif.py              EXIF/XMP date + GPS + DJI gimbal tags from stills (Pillow, no exiftool dep)
    frames.py            candidate frame sampling (sharpness), contact sheets
  process/
    select.py            pick best frame per (vantage, visit)
    align.py             feature registration (SIFT/AKAZE/ORB + USAC homography) + ECC refine
    grade.py             photometric normalization (Reinhard LAB / histogram) with strength
    masters.py           warp + common crop + grade + write masters/ and review sheets
  site/
    build.py             StoryJSON assembly, capture-ref resolution, markdown → HTML
    images.py            responsive derivatives (AVIF/WebP/JPEG), LQIP, dominant color
    theme.py             brand kit → CSS custom properties + @font-face (bundled/client fonts)
    render.py            StoryJSON → semantic, pre-rendered HTML (works without JS)
    package.py           single-file inliner, offline zip, manifest, icons, share card, sw.js
  film/
    plan.py              FilmPlans: YAML + masters + fonts → one hashed plan per film; the film set; the release gate
    beat.py              the beat grid (frames per beat, dissolves) and ports of the runtime's holdWindows/keyed
    layout.py            safe zones and text/rail anchors of every film layout (story and classic, 16:9 and 9:16)
    story.py             derives a story cut's shots from the chapters: title, captures, cards, curtains, stats,
                         end; degrade for 1–2 captures; subject-aware push targets and zoom clamps
    cut.py               renders story plans: pure frame(f) (pyramid warps, dissolves, words, rail, tag,
                         curtain splits, tabular stat figures, surface dips)
    safety.py            photosensitivity: static plan check and the per-frame luminance (flash) meter
    render.py            renders plans to MP4 + web copy + poster (classic `_Film`, shared drawing code, the
                         encode, ink checks, films side by side in worker processes)
    storyboard.py        one contact-sheet tile per key frame of a plan, with fact status chips
    tracks.py            captions (.vtt, .srt), chapters and the timed transcript from a plan; embeds
                         them into the MP4 by stream copy (mov_text, off by default, and named chapters)
    loop.py              --hours: a loop film repeated by stream copy into film/usb/ (FAT32 size guard)
  llm/claude.py          optional Claude vision helpers (captions, alt text, milestones)
  demo/synth.py          procedural "drone footage" of a fictional site changing over time
  runtime/               the web runtime shipped into every story (no build step, no deps)
    template.html
    css/*.css            concatenated in filename order
    js/*.js              concatenated in filename order inside one IIFE
    fonts/<name>/*.woff2 + fonts.json (bundled OFL fonts)
projects/
  demo-lakeside/         fictional demo used by CI (synthetic footage generated on the fly)
tests/                   pytest (unit + synthetic end-to-end)
web-tests/               Playwright (iPhone WebKit + Chromium) screenshots & interaction tests
.claude/skills/          Claude Code skills that drive a new client project end to end
.github/workflows/       CI, Pages deploy, releases
docs/                    blueprint, research, capture guide, this file
```

Client projects normally live **outside this public repo** (a private repo or
folder), pointed to with `VANTAGE_PROJECTS=/path/to/projects` or by passing a
path instead of a slug. See `docs/PROJECTS.md`.

## Project folder

```
<project>/
  project.yaml        ProjectConfig
  story.yaml          Story
  facts.yaml          Facts: every printed number, date or claim (text, sources, status)
  brand/brand.yaml    BrandKit (+ logo SVGs, optional fonts/*.woff2)
  footage/            raw flights, one sub-folder per visit date is ideal: footage/2026-09-12/...
  masters/            aligned + graded masters (commit these) + index.json
  work/               intermediates (gitignored)
```

`vantage new` writes `project.yaml`, `story.yaml`, `facts.yaml`, `brand/` (brand.yaml and
placeholder logos), a README and an empty `footage/` from `src/vantage/templates/project/`.

## CLI

`<project>` is a slug (looked up under the projects root) or a path.

| command | does |
|---|---|
| `vantage new <slug> [--title T] [--dir D] [--public]` | scaffold a project from the template; refuses an empty `--dir` and a folder inside this public checkout unless `--public` (fictional demos) |
| `vantage validate <project>` | load + cross-check YAML, brand files, capture refs |
| `vantage ingest <project>` | catalog footage + sample candidate frames + contact sheets |
| `vantage select <project>` | choose best frame per vantage per visit |
| `vantage align <project>` | register, crop, grade → masters/ + review sheets |
| `vantage process <project>` | ingest → select → align |
| `vantage build <project> [--out DIR]` | build the interactive site from masters + YAML; `--out` replaces only an earlier Vantage build, and refuses any other non-empty folder or one under version control |
| `vantage film <project> [--film ID]… [--plan-only] [--release] [--hours H]` | plan and render the `auto` films, or the named ones (including on-demand ones such as `screen`). `--plan-only` writes plan.json, the storyboard and the tracks without encoding. `--release` refuses drafts and non-releasable facts before planning. `--hours H` (with `--film` naming a loop film) repeats it into `film/usb/` without re-encoding. |
| `vantage package <project> [--release]` | single-file HTML + offline zip; checks the films against their plans (`--release` refuses any problem, see "Films") |
| `vantage all <project> [--release]` | process → build → film → package; `--release` reaches the film step too |
| `vantage preview <project> [--port 8000]` | serve dist site locally |
| `vantage demo [--fast] [--no-film]` | synthesize demo footage and run `all` on demo-lakeside |
| `vantage caption <project>` | Claude vision drafts captions/alt text → work/llm/suggestions.yaml |
| `vantage doctor` | check ffmpeg/ffprobe/encoders/python deps |
| `vantage fonts` | list bundled fonts |

Global options: `--projects-dir`, `--dist-dir`, `-v/--verbose`.
Exit code non-zero on any failure; every command prints a one-line summary.

## Work-dir artifacts (JSON, written with `indent=2`, stable key order)

`work/catalog.json`
```json
{ "version": 1,
  "sources": [
    { "id": "a1b2c3d4", "path": "2026-09-12/DJI_0042.MP4", "kind": "video",
      "date": "2026-09-12", "date_source": "folder|srt|exif|quicktime|filename|mtime",
      "start": "2026-09-12T14:03:11", "duration_s": 42.1, "width": 3840, "height": 2160,
      "fps": 29.97, "telemetry": "2026-09-12/DJI_0042.SRT", "size_bytes": 123 } ],
  "candidates": [
    { "source": "a1b2c3d4", "t": 12.5, "file": "candidates/a1b2c3d4/0012.500.jpg",
      "sharpness": 812.4, "lat": 12.34, "lon": -45.67, "rel_alt": 60.1 } ] }
```
Source ids are the first 8 hex chars of sha1(relative path + size). Candidate
JPEGs are ≤1600 px wide. Dates are local calendar dates of the flight.

`work/selection.json`
```json
{ "version": 1,
  "vantages": { "overview": {
      "reference": { "source": "…", "t": 12.5, "date": "2026-09-12" },
      "picks": { "2025-06-14": { "source": "…", "t": 3.0, "score": 0.83, "inliers": 412, "manual": false } } } } }
```

`masters/index.json`
```json
{ "version": 1,
  "vantages": { "overview": {
      "name": "Overview from the north", "width": 2560, "height": 1600,
      "captures": [
        { "date": "2025-06-14", "file": "overview/2025-06-14.jpg", "source": "…", "t": 3.0,
          "align": { "method": "homography", "inliers": 412, "rmse_px": 0.8, "ok": true } } ] } } }
```
Vantage ids name folders and pick dates name files that `vantage process` writes and clears, so
`config.py` accepts only ids matching `[a-z0-9][a-z0-9_-]*` and `picks` keys that are real
`YYYY-MM-DD` dates (quoted or not); `make_masters` re-checks both against `selection.json`.
A capture's `file` must be a relative path inside `masters/` (no `..`, no absolute path).

## StoryJSON (the runtime contract)

Built by `site/build.py`, rendered by `site/render.py`, enhanced by
`runtime/js`. Embedded in the page as
`<script id="vantage-story" type="application/json">…</script>`.
All URLs are relative to the site root. All `html` fields are already-rendered,
trusted markdown output.

```ts
type Img = {
  w: number; h: number;                 // intrinsic px of the largest variant
  alt: string;
  color: string;                        // dominant/average color "#rrggbb" (placeholder bg)
  lqip: string;                         // tiny blurred JPEG data URI (~24-32 px wide)
  sources: { type: "image/avif" | "image/webp" | "image/jpeg";
             srcset: [url: string, width: number][] }[];   // best format first
  fallback: string;                     // JPEG url, mid width
};
type Capture = { date: "YYYY-MM-DD"; label: string; note?: string; img: Img };
type Vantage = { id: string; name: string; aspect: number; captures: Capture[] }; // captures sorted by date
type Hotspot = { id: string; x: number; y: number; label: string; html?: string;
                 from: number; to: number };            // capture indices, inclusive
type Step = { capture: number | null; html: string; focus?: [x: number, y: number, zoom: number];
              split?: number };
type Chapter =
 | { type: "hero"; id: string; kicker?: string; title: string; html?: string;
     vantage?: string; capture?: number; video?: Video }
 | { type: "scrub"; id: string; kicker?: string; title?: string; html?: string;
     vantage: string; from: number; to: number; scrollVh: number; steps: Step[]; hotspots: Hotspot[] }
 | { type: "compare"; id: string; kicker?: string; title?: string; html?: string;
     vantage: string; before: number; after: number; beforeLabel: string; afterLabel: string;
     steps: Step[]; hotspots: Hotspot[] }
 | { type: "video"; id: string; title?: string; html?: string; video: Video; caption?: string; loop: boolean }
 | { type: "text"; id: string; kicker?: string; title?: string; html?: string;
     pullQuote?: string; attribution?: string }
 | { type: "stats"; id: string; kicker?: string; title?: string; html?: string;
     items: { value: string; unit?: string; label: string; source?: string }[] }
 | { type: "timeline"; id: string; kicker?: string; title?: string; html?: string;
     items: { date: string; title: string; html?: string; vantage?: string; capture?: number; source?: string }[] }
 | { type: "explore"; id: string; kicker?: string; title?: string; html?: string; vantages: string[] }
 | { type: "credits"; id: string; title?: string; html?: string;
     sources: { label: string; url?: string }[]; notes: string[] };
type Video = { poster: Img; sources: { src: string; type: string }[] };   // hevc (hvc1) first, then h264
type StoryJSON = {
  version: 1;
  meta: { slug: string; title: string; subtitle?: string; kicker?: string; dek?: string; byline?: string;
          lang: string; location?: { name: string; region?: string };  // never lat/lon: they stay in project.yaml
          draft: boolean; simulated: boolean; generatedAt: string;          // ISO timestamp
          dateRange: { start: string; end: string }; url?: string; shareImage?: string };
  brand: { name: string; url?: string; alt: string; theme: "dark" | "light"; grain: boolean;
           logos: { primary?: string; onDark?: string; mark?: string };
           partners: { name: string; role: string; url?: string; logo?: string; logoOnDark?: string }[];
           creditLine?: string; copyright?: string };
  vantages: Vantage[];
  chapters: Chapter[];
};
```

Chapter ids are the chapter's `id` (default `<type>-<n>`) with each run of
characters outside `[A-Za-z0-9._-]` turned into `-`, so an id may start with a
digit or hold a dot (`2025`, `phase-1.5`): find one with `getElementById` or
`[id="…"]`, never `#id` CSS. Each id, and the `<id>-title` id of its heading, is
unique in the page: a repeat, or an id the page uses itself (`v-main`,
`vantage-story`, `v-note-<n>`, …), takes the next free `-2`, `-3` suffix.

Capture references in YAML (`"2026-09-12"`, `"earliest"`, `"latest"`, `"#3"`)
are resolved to indices into the vantage's date-sorted `captures` at build time.
A date that is not an exact capture resolves to the nearest capture on or
before it (or the earliest). Hotspot `from` defaults to 0 and `to` to the last
index.

## Site output layout

```
dist/<slug>/site/
  index.html                     pre-rendered HTML + inlined CSS/JS/StoryJSON (+ theme)
  assets/img/<vantage>/<date>-<w>.<avif|webp|jpg>
  assets/video/<chapter>.<mp4>   (h264 + hevc)
  assets/brand/…                 logos (SVGs sanitized, rasters re-encoded bare, see below)
  assets/fonts/…                 woff2 actually used (bundled: own name; client: <stem>-<sha256[:8]>.woff2)
  share.jpg (1200×630)  icon-192.png  icon-512.png  apple-touch-icon.png
  manifest.webmanifest  sw.js
```

Brand and partner logos are copied from inside `brand/` (SVG, PNG, JPEG, WebP, AVIF or GIF). An SVG
is published as a sanitized copy (`site/build.py` `sanitize_svg`): pages show it through `<img>`,
where SVG never runs script, but the copy can also be opened on its own, on the story's origin. The
build refuses, and `vantage validate` reports as an error, an SVG with entity declarations, a DOCTYPE
internal subset, external entities, XML that is not well-formed, nesting deeper than 200 elements or
a root other than an SVG `<svg>` (a plain `<!DOCTYPE svg PUBLIC …>` is fine). It strips `<script>`,
`<foreignObject>`, `<handler>`, `<iframe>`, `<embed>`, `<object>` and any XHTML element; `<set>` and
`<animate>` aimed at a link attribute or an event handler; `on*` attributes; `javascript:`,
`vbscript:` and `livescript:` values; `data:` URLs other than `data:image/…` in `href`, `src`,
`action` and `formaction`; comments and processing instructions (`xml-stylesheet`). The build logs
what it removed and `vantage validate` lists it as a warning.

No published image carries metadata. A raster logo is re-encoded from its pixels in its own format
(`site/images.py` `strip_raster`): EXIF (camera, GPS, author), XMP (design-tool history, names, local
paths), IPTC, comments and PNG text chunks go; EXIF orientation is applied to the pixels; transparency
and the colour profile stay. PNG, GIF and WebP are written losslessly, a JPEG with its own
quantization tables, an AVIF at quality 90; an animated logo keeps its first frame. The build logs
what it removed. Photo variants, LQIPs, the share card and single-file re-encodes start from
`images.open_rgb`, which keeps only the colour profile (Pillow would otherwise copy a source JPEG's
comment into every JPEG made from it), and video posters come from a bitexact `ffmpeg.extract_frame`
with no metadata.

The page must work from `file://` (no fetch of JSON, no ES-module imports, no
service worker there) and from `https://`, where `sw.js` registers. Its CONFIG
(`site/build.py` `_service_worker`) is `{cache: "vantage-<slug>", core: [...],
assets: [[path, bytes, sha256[:16]], ...]}`:

* Install stores only `core`: `./`, the manifest and icons, fonts, brand files
  and the opening hero's fallback JPEGs (shown before the worker is in
  control). Every other file is cached the first time the page asks for it, and
  "Save for offline" fetches all of `assets` (any 404 or network error is
  reported as a failure, never as saved).
* The cache name stays the same from build to build and every file is stored
  under `<path>?v=<sha>`, so a redeploy keeps each saved file that did not
  change; activate deletes keys this build no longer lists. Once a save
  completes, the cache holds a `.vantage-saved` marker, and after an update the
  worker fetches the changed files when the page next asks for its status (on
  load and when the new worker takes over).

## Runtime principles

* Progressive enhancement: `render.py` emits complete, readable HTML with real
  `<picture>` elements and text; the JS upgrades chapters into pinned,
  scroll-driven experiences. With JS off (iOS Quick Look), it's an elegant
  photo essay.
* Headings: an opening hero carries the page's `<h1>`, the brand bar, the date
  line, the scroll cue and the standfirst (dek, byline). A later hero is a part
  opener: an `<h2>` over its picture, without any of those. A story that does
  not open with a hero gets a visually hidden `<h1>` with `meta.title` and, when
  it has a dek, a byline or simulated imagery, an intro
  `<section class="v-chapter v-intro">` (no `id`, no `data-type`) before the
  first chapter, on that chapter's surface, holding the standfirst
  (`templates/intro.html`, `standfirst.html`).
* A hotspot pin opens a sheet with a 16:9 detail crop of the picture beneath
  the pin above the hotspot's text (a third of the picture wide, centred on
  the pin and kept inside the frame, the pin ringed in `--v-accent`,
  `aria-hidden`). The crop comes from the capture on screen, on a compare from
  the side of the curtain the pin is on (in blink, the flight showing); there
  is none while that picture is still decoding (`runtime/js/25-stage.js`
  `crop`).
* No dependencies, no build step, ES2020, one IIFE. Target iOS Safari 16.4+.
* Scroll-scrubbed time-lapse = `<canvas>` cross-fading between decoded
  captures (not `<video>` seeking): smooth on iPhone, tiny memory, any
  number of visits. `requestAnimationFrame`, passive listeners, no layout
  thrash, `svh/dvh` units, safe-area insets.
* `prefers-reduced-motion`: no auto camera moves; steps become static figures.
* Theme comes only from CSS custom properties emitted by `theme.py`:
  `--v-accent --v-accent-2 --v-ink --v-paper --v-night --v-muted
   --v-font-display --v-font-text --v-font-numeric`.

## Module API (cross-module entry points)

Artifact models live in `vantage/models.py` (`Catalog`, `Selection`,
`MastersIndex`, `FilmPlan`, each with `.load(path)` / `.save(path)`); config models in
`vantage/config.py` (`load_project(path) -> Project`).

```python
vantage.ingest.catalog.build_catalog(project: Project, *, force: bool = False) -> Catalog
    # writes work/catalog.json + work/candidates/** + work/contact/<date>.jpg
vantage.process.select.select_frames(project: Project, catalog: Catalog | None = None) -> Selection
    # writes work/selection.json (loads catalog.json when not given)
vantage.process.masters.make_masters(project: Project, selection: Selection | None = None) -> MastersIndex
    # writes masters/<vantage>/<date>.jpg + masters/index.json + work/review/**; raises ValueError,
    # touching nothing, if a master's visit is neither in the catalog nor excluded in story.yaml
vantage.site.build.build_site(project: Project, out_dir: Path) -> Path
    # out_dir = dist/<slug>/site ; returns out_dir / "index.html"
vantage.film.render.render_film(project: Project, out_dir: Path, *, only: Sequence[str] | None = None,
                                release: bool = False, plan_only: bool = False) -> list[Path]
    # out_dir = dist/<slug>/film; returns the MP4s (the plan.json files with plan_only). See "Films".
vantage.site.package.package_project(project: Project, dist_dir: Path, *, release: bool = False) -> dict[str, Path]
    # dist_dir = dist/<slug>; reads dist_dir/site (builds it when missing); returns {"single_file": …, "zip": …}
    # release=True runs check_release, then always rebuilds dist_dir/site with release=True, so an
    # earlier draft build or a site older than the YAML never ships under a release label
vantage.demo.synth.generate_demo_footage(footage_dir: Path, *, fast: bool = False, seed: int = 7) -> Path
    # writes footage/<date>/… and footage/_truth.json; returns the truth path
vantage.llm.claude.draft_captions(project: Project) -> Path | None
    # writes work/llm/suggestions.yaml; None (with a warning) when ANTHROPIC_API_KEY is unset
```

Shared helpers: `vantage.log` (info/ok/warn/fail/step), `vantage.paths`
(find_project, projects_root, dist_root, project_dist, RUNTIME_DIR), and
`vantage.media.ffmpeg` (probe, run, extract_frame, iter_frames, FrameWriter,
encoders).

### Site rendering API

```python
vantage.site.theme.theme_css(brand: BrandKit, *, font_prefix: str = "assets/fonts/") -> Theme
    # Theme(css: str, fonts: list[FontAsset(src: Path, dest: str)])  — dest relative to site root
    # css = :root custom properties (see Runtime principles) + @font-face rules for every face used
vantage.site.render.render_page(story: dict, *, theme_css: str, base_href: str | None = None) -> str
    # full index.html: pre-rendered semantic HTML for every chapter, inlined runtime CSS/JS
    # (runtime/css/*.css, runtime/js/*.js in filename order), theme CSS, and the StoryJSON
    # <script id="vantage-story" type="application/json">. Pure function: no file IO except
    # reading the runtime directory.
```

Bundled fonts: `runtime/fonts/fonts.json` maps a key (`fraunces`, `inter`,
`newsreader`, `libre-franklin`, `public-sans`, `instrument-serif`) to
`{family, category, axes, weight, license, unicode_range, faces: [{file, style, weight}]}`
with files relative to `runtime/fonts/`. A brand `FontSpec` uses either
`bundled: <key>` or its own `files` (client-licensed woff2).
Its `fallback`, `weight` and `features` go into the theme CSS, so `config.py` accepts only
values of that kind (a list of font names, a weight or range, `font-feature-settings` tags) and
a `family` without control characters: a brand kit cannot add CSS rules or `url()`s.

### StoryJSON v1 additions (blueprint round 1)

* Every chapter carries `surface: "night" | "paper"` (default: `night` for hero/scrub/compare/video/explore/gallery-of-captures, `paper` for text/stats/timeline/credits/gallery-of-images).
* `Vantage` gains `kind` and optional `portraitFocus: [x, y]` (normalized) — the crop centre on portrait phones.
* `scrub` gains `hold` (0–1, share of each capture's scroll segment spent still); `scrollVh` is always resolved (default `min(900, 75 × captures)`).
* `compare` gains `mode: "curtain" | "blink"`.
* Timeline items gain `status?: "done" | "in-progress" | "planned"`.
* New chapter:
  `{ type: "gallery"; id; kicker?; title?; html?; surface; vantage?: string; captures: number[];
     images: { img: Img; caption?: string; date?: string; credit?: string }[] }`
* `brand.disclaimer?: string`.
* Facts: markdown may contain `{fact:id}` tokens resolved from the project's optional
  `facts.yaml` (`config.Facts`). The build renders the fact text followed by a numbered
  source marker linking to the credits (or, with no credits chapter, to a Notes section at the
  end); StoryJSON gains
  `notes: { n: number; factId: string; text: string; sources: string[]; status: string; releasable: boolean }[]`
  and `meta.unverifiedFacts: number`. Draft builds style non-releasable facts visibly
  (`.v-fact[data-releasable="false"]`, titled "Unverified: <status>"), and the notes list labels
  them "Unverified: needs client", "Unverified: needs attribution" (`reported` without an
  `attribution`) or "Unverified: do not print" (`templates/refs.html`). A `do-not-print` fact is
  withheld from every build, draft or release: wherever it is used the build prints
  `facts.WITHHELD` ("[withheld: do-not-print]") inside the usual marker, and its note carries that
  placeholder as `text` with empty `sources`. `vantage build --release`
  fails if any fact used in the story is not releasable (`Fact.releasable`), if a token references
  an unknown fact, or if the project is `draft: true`.
* Brand text: `brand.yaml`'s `credit_line`, `disclaimer` and `copyright` (`facts.BRAND_TEXT`)
  resolve `{fact:…}` tokens to the fact's text without a marker (as titles do; the film's end card
  prints the same text). Each such fact is still numbered in the notes, after those the story uses.
  `check_release` scans brand.yaml along with project.yaml and story.yaml, so these fields are part
  of the `--release` gate (`build`, `package`, `all`). `vantage validate` errors on a token in any
  other brand field (`voice` is never printed and is not checked).

### Single-file assets

`vantage package` writes two self-contained editions from the built site: `<slug>.html` (full)
and `<slug>-lite.html` (email). `<html data-edition>` is `site` (hosted), `offline` (the zip),
`single` or `lite`; only `site` carries `data-sw="sw.js"` and the manifest link.

* StoryJSON URLs stay site-relative paths (`assets/…`). Each `Img` is collapsed to one JPEG:
  `sources: [{type: "image/jpeg", srcset: [[path, w]]}]`, `fallback: path`. An `Img` with
  `sources: []` and `fallback === lqip` is a placeholder only (lite: everything except the hero,
  compare pairs, one image per scrub step and video posters).
* The bytes of every path the StoryJSON references are carried by exactly one element, either
  as the `src` data: URI of the first `<img>` showing it, which gets `data-asset="<path>"` (images
  the static essay shows; a later full-size `<img>` of the same picture repeats that data: URI,
  because the runtime moves such pictures into its stages, while explore-grid and timeline
  thumbnails get a smaller JPEG copy of their own, ≤ 720 px, with no `data-asset`), or as base64 inside
  `<script type="application/octet-stream" data-asset="<path>" data-type="<mime>">`, placed
  before the runtime script. The runtime resolves a path by looking up `[data-asset="<path>"]`:
  a `<script>` becomes `URL.createObjectURL(new Blob([bytes], {type: data-type}))` (create it
  lazily, revoke it when its last user releases it); an element with `src` lends its data: URI.
  The base64 is decoded by `fetch()` of a `data:<type>;base64,…` URL, off the main thread; if that
  fails, `atob()` in 1 MiB chunks that yield between chunks (`runtime/js/10-assets.js`).
* Videos are never `data:` sources. `render.py` emits no `<video>` in these editions (the poster
  is a plain picture); the runtime builds the player from `chapter.video.sources` (H.264 only
  here) and decodes the video only when it is needed: a hero loop once the hero is on screen (and
  motion is allowed and not paused), a video chapter once it is within 1.5 viewports. Videos are
  kept only while the file fits
  `output.single_file_max_mb`; otherwise hero `video` is removed and video chapters keep
  `sources: []`. Lite never carries video.
* Fonts are inlined as `data:font/woff2` in the theme CSS; `meta.shareImage` is dropped unless
  it is an absolute URL. Packaging reads only files inside `site/`; a path that leads outside it is an error.

## Films

Films are made in two stages: plan, then render. `plan_films` turns YAML, `masters/index.json`, the
master and font files and the settings into one **FilmPlan** per film. That step is pure and renders
nothing. A renderer then evaluates a pure frame function over the plan and pipes the frames to
ffmpeg. The same plan, with the same inputs, gives the same bytes with the same ffmpeg build, on any
machine: x264's thread count, which changes its output, is pinned to 4 (`-threads 4`,
`media.ffmpeg.ENCODER_THREADS`, recorded as `encode.threads`) rather than left to the CPU count.

A film has one of two cuts:

- **story** (the default; `film/story.py`, rendered by `film/cut.py`): shots derived from the
  chapters on a beat grid: the step's lead words landing on downbeats, pushes toward what changed, a
  curtain through the compare chapter and the stats as figures (see "Derived story cut").
- **classic** (`output.film.cut: classic` or a FilmSpec `cut: classic`): a title card, one section per vantage (the main vantage first;
  the 9:16 film keeps only the main one), a card between vantages and an end card. Inside a section
  each capture holds `hold_s` and dissolves `fade_s` into the next (linear light) under an eased 6 %
  push. `hold_s`, `fade_s`, `title_card_s` and `end_card_s` in `output.film` time it.

A **loop film** (`preset: screen`, or any story FilmSpec with `loop: true`) is a seamless cycle for an
event screen: it rises from pure night and dips back to it, so frame N equals frame 0 (see "Loop
films").

Every film except a loop film also gets **tracks** made from its plan: captions, chapters and a
timed transcript as sidecar files, and the captions and chapters embedded in its MP4s (see "Tracks").

Rendering: when more than one film is rendered and `os.cpu_count()` ≥ 4, each film renders in its
own worker process (`ProcessPoolExecutor`, spawn context, `cv2.setNumThreads(1)` inside), one per
film. `VANTAGE_FILM_JOBS` overrides the worker count; 1 renders sequentially. Workers write `*.part`
files only; they are renamed once every worker has succeeded, and every film's parts are deleted
when one fails. Parallel and sequential renders give the same bytes.

### Output layout (dist/<slug>/film/)

```
<slug>-<id>.mp4              H.264 High, yuv420p, BT.709 tags, +faststart, bitexact, no audio stream;
                             mov_text captions (off by default) and named chapters (see "Tracks");
                             crf 18 slow (VANTAGE_FAST: crf 20 veryfast), -threads 4
<slug>-<id>-web.mp4          web copy ≤ 25 MiB: the same frames, crf 23 under a VBV cap (see "Web encode");
                             not for loop films, nor with output.film.web: false
<slug>-<id>.jpg              poster (classic: the main vantage's final hold; story: the last frame of the last capture)
<slug>-<id>.plan.json        the FilmPlan plus its render record
<slug>-<id>.storyboard.jpg   one tile per key frame
<slug>-<id>.vtt  .srt        captions (WebVTT; SubRip for social uploads)
<slug>-<id>.chapters.vtt     chapters
<slug>-<id>.txt              timed text transcript (the film's text alternative, WCAG 1.2.1)
usb/<slug>-<id>-<H>h.mp4     --hours: a loop film repeated H hours by stream copy (never packaged, hosted or released)
```

A loop film's MP4 has closed GOPs of 2 beats (`encode.gop`), no web copy and no tracks.

- Film ids match `[a-z0-9][a-z0-9-]{0,31}` and never end in `-web`. The film set is `16x9`, plus
  `9x16` when `output.film.vertical`; those ids keep the file names films had before plans.
- Every output of a film is written to `*.part` and renamed only once all of them exist (the plan
  last). Packaging ships only the files the release gate accepted for a current film (build.json
  `films.<id>.files`), never a `.part`.
- A full `vantage film` run (no `--film`) deletes `<slug>-*` files whose id is no longer in the film
  set, and stray `*.part` files.
- `--plan-only` writes plan.json (with `render: null`), the storyboard and the track sidecars, but no
  MP4 or poster, so a cut can be approved in seconds. A plan that has not changed keeps the render record of the MP4
  next to it.

`output.film.require_approval` (default false): `package --release` needs each film's plan hash in
`approvals.yaml` (see PROJECTS.md).

### Settings and the films: spec

project.yaml `output.film` (FilmSettings). Existing fields are unchanged; `hold_s`, `fade_s`,
`title_card_s` and `end_card_s` time the classic cut. New fields:

```yaml
cut: story            # story | classic: the cut of the derived films
bpm: 100              # 60–160; the beat grid of story cuts
max_upscale: 1.6      # 1.0–3.0; output px per master px never exceeds this in a push
web: true             # write <name>-web.mp4
tracks: true          # captions, chapters and transcript (sidecars, and embedded in the MP4s)
require_approval: false
```

story.yaml:

```yaml
films:                       # optional; id → FilmSpec; merged over the derived set
  16x9: {}                   # derived: aspect 16x9, chapters all
  9x16: {chapters: main}     # derived when output.film.vertical (aspect from the id)
  screen: {preset: screen}   # the event loop (auto: false) → render it with --film screen
# FilmSpec fields (all optional): preset (screen) · cut · aspect (16x9|9x16) · bpm · chapters (all | main | [chapter ids])
#   · stats (0–6, default 3) · title_beats (4; at least 3) · end_beats (6; at least 2) · safe [top, right, bottom, left] (fractions)
#   · type_scale · max_upscale · loop · sound (none; anything else: "not supported yet") · auto (true)
#   · enabled (true) · hours (loop films)
steps[].film: {line: str, focus: [x, y, zoom], skip: bool}   # scrub and compare steps
```

- Film ids match `[a-z0-9][a-z0-9-]{0,31}`, do not end in `-web` and are unique as typed (`1080` and
  `"1080"` collide). `films.<id>` with `enabled: false` removes a film.
- Precedence: explicit FilmSpec field > preset > output.film (`cut`, `bpm`, `max_upscale`) > built-in
  default. A new id's aspect is 16x9 unless the id is `9x16`.
- The `screen` preset sets cut story, aspect 16x9, loop true, bpm 75, type_scale 1.6, safe 0.05 on
  every side (the `screen` layout), auto false, title_beats 4, end_beats 4, and capture beats 2 (no
  step), 4 (a step) and 5 (a push) where other films have 1, 3 and 4 (`FilmDef.beats`, preset only).
  Loop films have no web encode and no embedded tracks. A loop film needs the story cut.
- `vantage film` (no `--film`) plans and renders the `auto` films; `--film ID` names any enabled film.
- The 16:9 size is `output.film.width × height`; the 9:16 size is `short × short·16/9`. `VANTAGE_FAST=1`
  caps the long edge at 960 px, which changes the size and so the plan hash. `max_upscale` (and the
  soft-push warning) is measured in output px of the full-size film, so a VANTAGE_FAST plan clamps
  the same pushes and has the same beats, captions, chapters and poster frame as the full-size film;
  only its pixel sizes differ.
- `chapters: main` keeps the chapters whose vantage is the main vantage (the first scrub's with
  masters, else the first compare's), plus stats. Unknown ids in a list are ignored with a warning.
- `film.line` sets a step's words on screen; `film.focus` its push target (used as given, only
  clamped); `film.skip` leaves the step out of films (its capture still plays as a quiet beat).
- `vantage validate` warns about words over 40 characters, unknown chapter ids in
  `films.<id>.chapters`, "bpm too fast for fps", (with masters) clamped or soft pushes, and numbers
  outside `{fact:}` tokens in the text films print (`cli.bare_numbers`: digits and number words
  other than "one", in project.yaml title, subtitle and kicker, step text and `film.line`, capture
  notes, stat labels and units, and hero, scrub, compare and stats titles and kickers; the flights'
  own years are not counted). It is a warning, not a release error: the facts gate decides what is
  releasable, and a reworded sentence is often the better fix.

### Beat grid (film/beat.py)

- B (frames per beat) = round(60·fps / bpm). The effective bpm (60·fps / B) is the plan's `grid.bpm`.
- D (dissolve frames) = max(ceil(0.34·fps), round(2B/3)). B must be ≥ D, otherwise planning fails
  with "bpm too fast for fps". At 30 fps and 100 bpm: B = 18, D = 12.
- Every shot start, dissolve start, push start and lead-word landing falls on a frame index divisible
  by B. Sub-beat staggers (title word groups at B/3, text fades over B/3) stay inside their beat.
- `ramp(f, start, n)` = smoothstep((f − start + 1) / n): the first changed frame is `start`, the last
  is start + n − 1. Dissolves, dips, rises and fades all use it.
- `hold_windows(keys, hold)` and `keyed(windows, values, v, mix)` are line-for-line ports of
  runtime/js/25-stage.js (smoothstep, without the reduced-motion branch); the camera is `keyed` over
  the plan's `camera` windows with `mix_cam` (componentwise linear, as the runtime's `mixCam`).

### Derived story cut (film/story.py derive_cut)

Shots come from the chapters in scope, in story order: the first hero → title; each scrub chapter →
a series of captures; each compare chapter → a curtain; the first stats chapter → stat shots; then
the end card. Gallery, explore, text, timeline and video chapters are not used. A film with no scrub
or compare chapter in scope plays the main vantage's captures as one quiet series. For the demo
the 16:9 cut is 62 beats (37.2 s at 100 bpm) and the 9:16 cut 60 beats (its narrower crop clamps
two pushes), with or without VANTAGE_FAST.

- **title** (`title_beats`, 4): the first image (the first series capture, else the first curtain's
  before image) at rest under a 30 % night
  scrim. The kicker (hero, else project.yaml, else "<place> · <years>") tracks out 0.10 → 0.22 em
  across beat 0; the title's word groups (up to three, split by word count) rise on frames B, B + B/3
  and B + 2B/3 (`groups`). Text and scrim fade out over the last B/3 frames. Frame 0 is the image.
- **capture** (one per capture of each series: `from`..`to` minus `exclude`d captures): 1 beat with
  no step, 3 with a step, 4 with a push (at least 3 when the film has a single image).
  - Every capture except the film's first image starts with a dissolve on its first frame: `_mix` in
    linear light from the previous image (a capture, or a curtain as it ended) over D frames, or a
    display-gamma dip from a surface (`from: night` after a card, `from: <surface>` after a stat).
  - The date label swaps at the dissolve's midpoint; the rail knob eases (smoothstep) to the new
    tick over the dissolve. Ticks sit at the real dates of the series.
  - Words: `film.line`, else the step's leading `**bold**` run, resolved through the facts (a
    do-not-print fact drops the words with a warning). They land at beat 1 (beat 0 on the first
    image, `step.land`), rise 14 units while fading in over B/3 frames, and fade out with the next
    dissolve.
  - A push starts at beat 1 (`push_f0`) and reaches its target at beat 2 + B/3 (`push_f1`) and holds
    to the end of the shot. It eases back as slowly as it went in: over B + B/3 frames from the next
    shot's first frame (through its dissolve), or, when the next shot pushes too, the camera glides
    from one target to the next. Before a curtain it eases back over B + B/3 from the curtain's first
    frame; before a card, stat or the end card it holds while the image dips out. `check_plan`
    refuses any camera move shorter than B + B/3 frames.
  - Several steps on one capture: films use the first (warning). Skipped steps play as quiet beats.
- **card** (4 beats): inserted when consecutive image shots (captures, curtains) change vantage. The
  chapter kicker and title (else the vantage name) on night. A dip (`dip`) from the image in display
  gamma; the next image dips in from night.
- **curtain** (`C1…`, one per compare chapter; mode `blink` is rendered as a curtain too): before is
  left of the rule, after right of it; split 0 shows all of the after image.
  - Entry: 1 beat at split 0. If the previous image is not `after` (same vantage and capture), a
    D-frame dissolve to `after`; otherwise no dissolve (`from_after: true`). Either way the camera
    returns to rest over the entry beat, and the previous shot's label, words and rail leave over the
    first half of the entry while the curtain's own overlays arrive over the second.
  - Then per step that has a split (steps with `film.skip` are left out; no split at all: one step at
    0.5): 2 beats of smoothstep sweep to the split, then 2 beats of hold. `keys` holds the hold
    windows in frames, eased by `keyed` exactly like the camera. A step's focus is clamped like a push
    (no subject recentring) and eases in over the sweep.
  - Words: only `film.line` goes on screen (landing on the sweep's downbeat, out at the next sweep);
    the full step text is for the tracks.
  - Labels: `before_label` / `after_label` (else the capture labels) at the top-left and top-right,
    44 units, shrunk (`label_scale`) so each fits between its text edge and a rule at the centre, and
    set under the tag when the tag is top-right (`label_top`). `label_ink` holds the inner ink edges
    and `reveal` the frame intervals in which each label shows: only while the rule (3 units wide)
    is at least 8 px clear of its ink. A label fades in over B/3, out over the last B/3 frames of its
    interval, and is clipped to its own side of the rule.
  - The rule is 3 units wide in the accent colour; the grip is a 44-unit disc with chevrons at 0.56H.
- **stat** (`N1…`, at least 4 beats each, at most `stats`, default 3): items of the first stats chapter in
  scope, in order, whose value contains a `{fact:}` token. Any other item, and any item whose value,
  unit, label or source uses a do-not-print fact, is left out with a plan warning.
  - Background: the chapter's `surface` (default paper), entered by a dip (`dip`).
  - Value: the numeric face at 220 units, every digit on a fixed advance equal to the widest digit
    (tabular figures without raqm; other glyphs keep their own advance), shifted so the first
    glyph's ink starts on the text column with the label. Each glyph slides up into a mask over B/2
    frames, staggered by 2 frames, from `land` (the end of the shot's dip: the figure lands on a clean
    surface). The mask ends at the figure's own ink bottom (its baseline, or a comma's tail), so a
    rising glyph never shows above the label. The value is the plan's printed fact text: it never
    counts.
  - Unit at 0.35× the value size after it; label at 44 units and "Source: <source>" at 26 units
    below. They fade in at `text_in`, the first beat at or after the figure has landed (land + B/2).
    Ink on paper, paper on night; no scrims (a loop film adds its tag).
  - Length: max(4, ceil((text_in − f0 + 2·B/3 + cue_frames(label + source)) / B)) beats, so label
    and source are fully on screen for as long as they take to read at 17 characters a second (the
    figure itself is read at a glance).
  - Its text fades out over its last B/3 frames before any next shot, so a stat followed by a stat
    shows one figure at a time.
  - Followed by an image, the text fades out over the last B/3 frames and the image dips in from the
    bare surface.
- **Dips**: every dip and dissolve is D frames, except one that crosses between paper and another
  surface or an image: it lasts `surface_dip` frames (film/safety.py, 17 for the demo colours at
  30 fps), the shortest display-gamma dip from night to paper in which no frame pair changes
  luminance by 0.10 or more.
- **end** (`end_beats`, 6): a D-frame dip to night, then brand name, credit line, partners, the short
  `base_url`, copyright, disclaimer, the honesty line and SIMULATED_NOTE when the project is simulated.
  The honesty line comes from `provenance`:
  - `photo`: "Every frame is made from real aerial photographs. No in-between images are invented."
  - `simulated`: "Every frame is made from the simulated flights. No in-between images are invented."
- **Loop films** (`loop: true`; the `screen` preset):
  - A `rise` (`R1`, 2 beats) opens the film: frame 0 is a pure night fill, easing in display gamma
    (`ramp(f, f0 + 1, n)`, 0 on its first frame) onto the title's image at rest under the title's
    30 % scrim, with the tag. When a series follows the title, its rail is shown with the knob
    sweeping back from the last tick to tick 0; the rail then stays (knob at 0) through the title into
    the first capture instead of fading in.
  - The title starts at 2B (its `groups` and `key` are relative to its own `f0`).
  - Every text needs releasable facts unless the project is a draft: a stat item, step words,
    capture label or note, card or title text that prints any other fact is left out with a plan
    warning ("left out of the loop"), since the loop plays unattended (a label falls back to its
    date). `--hours` refuses a loop plan of a project that is not a draft if it still prints a fact
    that is not releasable.
  - The end card (`end_beats`, 4) prints only the brand name and, when `output.base_url` is set, the
    short URL in large type (text role `url`, 64 units × type_scale); no credits, partners, fine
    print or honesty line (the `honesty` field is still recorded). No QR code in v1.
  - With no disclaimer on its end card and no tracks, a loop keeps its `tag` ("Simulated imagery",
    "Preview") on every shot: on cards, stats and the end card too, in ink without a scrim on paper.
  - The end card is lengthened by a beat when needed so the film's length is a whole number of GOPs
    (2B frames): N % 2B == 0.
  - A `dip` (`D1`, 2 beats) closes the film: the end card eases in display gamma to the night fill,
    reaching it on frame N (`ramp(f, f0, n + 1)`), one past the last frame. So frame(N) == frame(0),
    byte for byte, and a player looping the file shows no seam. The renderer checks it before
    encoding (`render.check_seam`).
  - For the demo the screen cycle is 76 beats at 75 bpm (1824 frames, 60.8 s at 30 fps).
- **Push target**: `film.focus` when given (`target: film.focus`, used as given); otherwise the
  step's `focus`, recentred to the crop nearest it that puts the step's **subject hotspots** inside
  the layout's subject box (`focus+subject:<ids>`; `story.subject_box`: the safe zone 1 % in from
  its sides, below the date label (label cap-top + 64 units) and above the lead words (lead baseline
  − 110 units), so in 9:16 a subject sits above the words, never under them), or `focus` when there
  are none. Subjects are the chapter's hotspots that appear since the previous step (`from` index
  in (prev step, this]) or disappear since it (`to` index in [prev step, this − 1]); the first step's
  "previous" is the chapter's `from`. If they do not fit, the zoom is lowered toward 1 in 32 steps.
  **Reach**: the crop centre never moves more than `MAX_TRAVEL` (0.5) of the rest crop's width
  (height) from the rest view. When the subjects cannot be framed within that reach (in 9:16 a
  subject far outside the portrait crop at rest), the push aims at the step's focus, capped to the
  reach (`focus, subject off-frame:<ids>`, with a plan warning) rather than whipping across the
  master to a subject the viewer never saw.
- **Zoom clamp**: s0 = max(W/mw, H/mh); z_eff = clamp(z, 1, max(1, max_upscale/s0)). Warnings when
  z_eff < z ("push clamped"), s0·z_eff > 1.25 ("soft push") and s0 > max_upscale ("master upscaled
  even at rest"). A push with z_eff < 1.08 whose crop centre moves < 0.03 (normalized) becomes a
  3-beat step without a push (`push: false, reason: clamped`).
- **Rest camera**: the image centre at zoom 1 in 16:9; the vantage's `portrait_focus` in 9:16.
- **Degrade** (counted on the main vantage's captures after exclusions): 0 captures is an error;
  1 capture plays title → capture (at least 3 beats, words if it is a step) → stats → end; 2 captures
  play title → the first capture (3 beats, no push) → a curtain between the two (the first compare
  chapter on the main vantage in scope gives labels and splits, else `[0.5]`) → stats → end; 3 or
  more play the full cut.
- Pushes sample a two-level pyramid per master (L0 at s0, L1 at s0·zmax, zmax the largest camera
  zoom in the plan) and blend the two warps with weight log z / log zmax, so there is no pop between
  levels. The classic cut keeps its single level at s0·1.06.

### Layout presets (film/layout.py)

`Layout(name, width, height, safe px [t, r, b, l], margin, label_top, lead_baseline, rail_y, tag,
type_scale, scrim, rail_x)`; px values are rounded. Styles are in units of short edge / 1080 ×
type_scale. Every cut reads its layout from the plan (`plan.layout`, `Layout.record()`); the classic
renderer no longer has constants of its own.

| layout | safe t, r, b, l | label cap-top | lead baseline | rail | tag |
|---|---|---|---|---|---|
| story-16x9 | .06H .05W .06H .05W | safe top | .78H | .88H | top-right in safe |
| story-9x16 | .14H .12W .28H .06W | .16H | .60H | .70H | right, baseline rail − 18u |
| screen | .05 on every side (TV overscan) | safe top | .76H | .88H | top-right in safe |
| classic-16x9 | .05 all round (ink check only) | margin round(.055W) | | H − round(.075H) | `corner`: .8·margin from the top-left |
| classic-9x16 | as story-9x16 | lower third above the rail | | .70H | `top-left`: ascent top at the safe top |

- At 1080 × 1920 the story-9x16 safe zone is px [269, 130, 538, 65], the rectangle (65, 269)–(950,
  1382): between 14 % and 72 % of the height and clear of the right 12 %.
- `films.<id>.safe` ([top, right, bottom, left] fractions) and `type_scale` apply to every layout,
  story and classic. The anchors move with the edge they hang from: label cap-top with the top
  (story-9x16 `.16 + (t − .14)`), lead baseline and rail with the bottom (`.70 + (.28 − b)`), the
  classic margin with the left (`.055 + (l − .05)`), the classic-16x9 rail `H − round((.075 + b −
  .05)·H)`. With the preset's own fractions the numbers are exactly the preset's.
- Classic layouts also record `scrim` [first row, ramp rows] of the bottom scrim (16:9: the bottom
  round(.42H) rows ramping over all of them, as before; 9:16: from rail − round(.30H), ramping over
  round(.22H), then held to the bottom) and `rail_x` [x0, x1] (16:9: margin, W − margin; 9:16 inset
  by the 5-unit knob). Cards in classic-9x16 wrap to min(.8W, text width) and shrink to fit the safe
  height, centred in the safe zone; classic-16x9 keeps .8W × .86H centred in the frame. The classic
  16:9 film is byte-identical to the one before layouts.
- Story rails are inset by the knob (8.5 units) at both ends, so the knob never reaches past the text
  edges. The story tag and lead words shrink until their ink (overhangs included) fits between the
  margins.
- **Ink checks** (`render.check_ink`): both renderers record the ink box of every typeset layer
  ([x, y, w, h]; rising text includes its rise travel; the rail includes its knob) and write plan.json
  `checks` (not hashed): `{"safe": [x0, y0, x1, y1], "ink": [{"shot": id | null, "layer": name,
  "box": [x, y, w, h]}], "outside": [indices]}`. A box more than 1 px (antialiasing) outside the safe
  rectangle is a plan warning; `vantage film --release` refuses it.

Story styles (units): kicker 38, title 124, date label 52 (caps, tracking .08), lead 120 (shrinks to
fit between the margins), curtain labels 44, stat value 220 / unit 77 / label 44 / source 26, card title 96, end brand 92, honesty 38, fine print 27, tag 22. Text rises
are composited at sub-pixel offsets by bilinear warping of the pre-rendered layer patch. Scrims are
feathered night boxes behind each text block's ink (and the rail and tag), never full-width bands.
In 9:16 (story and classic) every word, the end card and the rail stay between 0.14H and 0.72H, clear
of the right edge.

### Safety (film/safety.py)

- **Static** (`check_plan`, at plan time and again before rendering a story plan; `PlanError`):
  every change between two images is a dissolve of at least ceil(0.34·fps) frames that starts with
  its shot, there are no hard cuts between images (a curtain may open without a dissolve only on the
  image already on screen, `from_after`), an image after a surface always dips in, every card, stat
  or end dip is as long, any dip to or from paper lasts at least `surface_dip` frames, and every
  curtain sweep lasts at least 2 beats. The title's last word group is fully in before its fade
  starts, and a card, stat or end card's dip leaves at least one beat of the shot for its text (a
  long dip from paper can need more `end_beats` than the minimum). A loop plan opens with a `rise` and closes with a `dip`, each
  at least ceil(0.34·fps) frames, and its frame count is a multiple of 2B. Classic plans keep their
  own timing and are not checked.
- **Flashes** (`LumaMeter`, render time, every frame of every cut, classic cuts included: they have
  no static check, so the meter is their guard): each frame is downscaled to 1/8 (INTER_AREA),
  converted from sRGB to linear light and averaged over 113 regions, an 8×8 grid of cells plus the
  7×7 cells offset by half a cell (`safety.regions`), so a flash across a cell corner is seen whole.
  Following WCAG 2.3.1's general and red flash definitions, a **transition** is a monotonic run of a
  region's Rec.709 relative luminance, however many frames it is ramped over, that changes it by
  ≥ 0.10 while its darker end is < 0.80; or a run of its red value max(0, (R − G − B)·320) of more
  than 20 with saturated red (R / (R + G + B) ≥ 0.8, linear) at one end. A run ends only when the
  value comes back 0.10 (red: 20) from its extreme, so noise does not split a dissolve into several.
  The film fails if any region holds more than 3 transitions within `fps` frames (WCAG allows 3
  flashes, 6 transitions, a second; and a region, 1/64 of the frame, is smaller than WCAG's 25 % of
  a 10° field, about 2.8 % of a screen, so a flash of that size changes some region in full): the renderer deletes the `.part`
  outputs and raises `FlashError`. The meter only reads frames, so it never changes bytes; its
  result is `render.flash` (`{pass, max_transitions_per_s, max_delta, frame}`; `max_delta` is the
  largest one-frame change of any region). For a loop film it also reads frame N (= frame 0), so the
  seam is measured like any other transition. The fast demo films measure at most 3 transitions a
  second.

### plan.json

Canonical JSON is `json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
allow_nan=False)` encoded as UTF-8 (`models.canonical_json`). Floats are never rounded: Python's
shortest round-trip repr means a plan read back renders the same bytes. The file is written with
`indent=2` and has no timestamps. `FilmPlan.load` refuses a file whose `plan` no longer matches its
`hash` (edited by hand).

```jsonc
{ "version": 1,
  "id": "16x9",
  "hash": "<sha256 hex of canonical(plan)>",           // people quote the first 12 characters
  "plan": {                                            // hashed: everything that decides frames and tracks
    "slug": "demo-lakeside", "cut": "classic", "preset": null, "renderer": 1,   // renderer: bumped when frames change for an equal plan
                                                                                // (classic 1; story 2: rails inset by their knob;
                                                                                //  3: stat land/text_in, aligned and clipped figures)
    "width": 1920, "height": 1080, "fps": 30, "frames": 1410, "loop": false, "sound": "none",
    "grid": null | {"bpm": 100.0, "fpb": 18, "dissolve": 12, "beats": 37},   // story: effective bpm; classic: null
    "layout": {"name": "classic-16x9", "safe": [54, 96, 54, 96], "margin": 106, "label_top": 54,
               "lead_baseline": 999, "rail_y": 999, "tag": "corner", "type_scale": 1.0,
               "scrim": [626, 454], "rail_x": [106, 1814]},
    // story: {"name": "story-16x9", "safe": [65, 96, 65, 96], "margin": 96, "label_top": 65,
    //         "lead_baseline": 842, "rail_y": 950, "tag": "top-right", "type_scale": 1.0}
    "draft": false, "simulated": true, "tag": ["Simulated imagery"],          // + "Preview" in drafts
    "provenance": ["simulated"],                       // photo | simulated; reserved: ortho, analysis, reconstruction
    "inputs": {                                        // every file the frames read, by role
      "masters": {"overview/2025-04-12.jpg": {"sha256": "…", "w": 2560, "h": 1600}},
      "fonts": {"display": {"file": "runtime:fraunces/….woff2" | "brand:fonts/….woff2", "sha256": "…"},
                "text": {…}, "numeric": {…}},          // "pillow:default" (sha256 null) when a brand has no font file
      "colors": {"night": "#…", "paper": "#…", "ink": "#…", "accent": "#…"} },
    "camera": [],                                      // story: [{"a": 0, "b": 162, "x": 0.5, "y": 0.5, "zoom": 1.0}, …]
    "captions": [{"f0": 36, "f1": 90, "text": "April 2025. Before.", "facts": []}],   // see "Tracks"; [] for loop films
    "chapters": [{"f0": 0, "title": "Every flight, one frame", "facts": []}],
    "transcript": {"title": "…", "subtitle": "…", "honesty": "…", "facts": [],
                   "lines": [{"f0": 0, "text": "Title over the April 2025 view: …", "facts": []}]} | null,
    "shots": [ /* see below */ ],
    "hold_s": 1.0, "fade_s": 0.8, "seconds_total": 47.0, "poster_seconds": 23.65 },   // classic timing
    // story instead: "poster": <frame> (the last frame of the last capture shot, else of the last curtain)
  "facts": {"stalls-to-meadow": {"status": "verified", "releasable": true}},  // snapshot at plan time, NOT hashed;
                                                       // every fact the shots and the tracks print
  "warnings": ["captures 2025-06-14 note: left out of the films (it uses a do-not-print fact)"],  // NOT hashed
  "checks": {"safe": [96, 54, 1824, 1026], "ink": [{"shot": "T1", "layer": "stack", "box": [598, 330, 724, 420]}],
             "outside": []},                           // NOT hashed: written by the renderer (see Layout presets)
  "encode": {"crf": 18, "preset": "slow", "threads": 4, "web": {"crf": 23, "preset": "slow", "maxrate": 4015000,
             "bufsize": 8030000, "threads": 4} | null, "gop": null | 48, "tracks": true, "lang": "eng"},  // NOT hashed: bytes, not frames
                                                       // tracks: embed captions and chapters; lang: ISO 639-2
                                                       // gop: a loop film's closed GOP, 2B frames
  "render": null | {"mp4": {"bytes": 0, "sha256": ""}, "web": {"bytes": 0, "sha256": ""} | null,
             "flash": {"pass": true, "max_transitions_per_s": 0, "max_delta": 0.0467, "frame": 384},
             "ffmpeg": "6.1.1-3ubuntu5"} }             // written after the encode; null with --plan-only
```

`inputs` is a generic map of hashed files by role: later inputs (orthophotos, analysis layers,
committed reconstruction clips) join it as new roles, and `provenance` will say so. The engine never
reconstructs anything itself.

Until each kind has its own honesty line and on-image tag, films show only vantages of `kind: drone`
(`plan.FILM_KINDS`): `plan_films` leaves every `ortho`, `ground`, `archival` and `plan` vantage out
of the masters it plans from, with a plan warning per vantage (`vantage validate` warns too), so
`provenance` is only ever `photo` or `simulated`. `honesty_line` raises on any other provenance
rather than describing it as photographs.

Shots (in film order; `f0`, `f1` are frames, half-open; `key` is the storyboard frame):

- Every shot: `id`, `kind`, `f0`, `f1`, `key`, `chapter` (null in classic cuts), `text`
  (`[{role, text}]`, printed text with facts already resolved), `facts` (ids the shot prints).
- `card` (`T1` title, `K1…` between vantages, `E1` end): `role` (title | section | end), `prev` and
  `next` (section ids or null), `seconds: {start, duration, dissolve}`, `blocks: [{text | null, style,
  gap}]` (null text is the accent rule; `style` names a style in render.py, such as KICKER or TITLE;
  `gap` is the space above in units of short edge / 1080).
- `section` (`S01…`): `vantage`, `name`, `focus` [x, y], `seconds: {start, length, lead, tail}`,
  `shots: [{date, label, note, file, key, facts}]`. The classic renderer evaluates `frame(k / fps)`
  in seconds.

Story shots (`chapter` is the chapter id, or `<type>-<n>` for a chapter without one):

- `title` (`T1`): `vantage`, `capture` (date), `file`, `groups` (the frames the title's word groups
  start rising). Text roles `kicker`, `title`.
- `capture` (`S01…`): `vantage`, `capture` (date), `label`, `file`, `dissolve` (`{from: <date> |
  <curtain id> | "night" | "paper", f0, f1}` or null on the film's first image), `series` (ordinal), `rail` (index on the
  series' rail), `step` (null or `{index, words, words_from: film.line | lead | null,
  focus_requested, focus [x, y, z_eff], target, subjects, z_eff, upscale, push, reason, land,
  push_f0, push_f1}`). Text roles `label`, `words`.
- `card` (`K1…`): `vantage`, `dip` (frames). Text roles `kicker`, `title`.
- `curtain` (`C1…`): `vantage`, `before`, `after` (dates), `before_file`, `after_file`, `labels`
  [before, after], `label_scale`, `label_top` (cap-top px), `label_ink` [right edge of the before
  label, left edge of the after label] (px), `reveal` [[[f0, f1], …] per label], `dissolve` (as a
  capture's, or null), `from_after`, `keys` [{a, b, split}], `steps` [{index, split, f0 (sweep start),
  words, words_from: film.line | null, land, focus_requested, and when focused: focus, target,
  subjects, z_eff, upscale, push, reason}]. Text roles `before`, `after`, `words`.
- `stat` (`N1…`): `value`, `unit`, `label`, `source`, `surface` (night | paper), `dip`, `land` (the
  frame the figure starts to slide in), `text_in` (the frame unit, label and source fade in). Text roles
  `value`, `unit`, `label`, `source`.
- `end` (`E1`): `dip`, `honesty`. Text roles `brand`, `credit`, `role`, `partner`, `link`, `fine`,
  `honesty`; a loop's end card has only `brand` and `url`.
- `rise` (`R1`), `dip` (`D1`): loop films only, no extra fields and no text.

Shot kinds `ortho`, `heatmap` and `insert` are reserved and refused. A text that uses a
`do-not-print` fact is never printed in a film: it is left out (a capture label falls back to its
date, a vantage name to the masters' name) and a warning is recorded.

### Storyboard (film/storyboard.py)

- Tiles show the key frames in film order. Classic: the middle of each card, the middle of each
  capture's hold, then the poster frame (`P1`). Story: one per shot `key` (the title once its words
  are in, a capture at its words or push hold, else the end of its hold, a card's middle, a curtain
  holding the split nearest 0.5, a stat once its figure has landed, the end card once its text is
  in). A curtain's date reads `<before>|<after>`. 8 columns of 320 px tiles for landscape films, 10 columns of
  192 px for portrait ones.
- Under each tile: `m:ss.ff · f<frame>` (prefixed with `bar.beat` on a beat grid), then
  `<shot id> <kind> <date>`, then one chip per fact: green releasable, amber needs-client or
  reported without an attribution, red do-not-print.
- Footer: `<slug> · <id> · <cut> · W×H@fps · <duration> · plan <hash12> · DRAFT|RELEASE-READY`
  (with `(<n> unreleasable)` when facts are not releasable).
- Labels use the bundled Inter. JPEG quality 85, baseline, no metadata; the same plan gives the
  same bytes.

### Web encode and budgets

- Every film whose plan has `encode.web` (not loop films; not with `output.film.web: false`) gets
  `<slug>-<id>-web.mp4`, encoded from the same frame pipe as the main film (`FrameWriter`
  `extra_outputs`: one rawvideo input, one independent encoder per output, so the main film's bytes
  equal a single-output encode).
- Args: `-c:v libx264 -profile:v high -crf 23 -preset slow (VANTAGE_FAST: veryfast) -threads 4
  -maxrate R -bufsize 2R -pix_fmt yuv420p -movflags +faststart` plus the main film's BT.709 and bitexact args.
  R = min(6,000,000, floor(0.9 · 25 MiB · 8 / duration_s)) bit/s, floored to a whole kbit/s
  (`plan.web_maxrate`; 37.2 s → 5,073,000).
- `render.web` records its bytes and sha256; the release gate checks it like the MP4.
- `scripts/budgets.py` (`film_checks`): when `dist/<slug>/film/` exists, every `*.plan.json` whose
  `encode.web` is not null needs `film/<stem>-web.mp4` (missing: exit 2), and each must be ≤ 25 MiB
  (check `<slug> film <stem>-web.mp4`). Main MP4s and `film/usb/` are not budgeted; a dist without
  `film/` (`--no-film`) has no film checks.

### Tracks (film/tracks.py)

Captions, chapters and the transcript are filled at plan time into the hashed `captions`,
`chapters` and `transcript` (story cuts: film/story.py; classic: film/plan.py `_classic_tracks`),
with every fact already printed through facts.yaml. So they cannot drift from the picture, and every
fact they print is in the plan's `facts` and goes through the release gate, even one that appears only
in a caption or the transcript (a capture's note, a step's full text). A text that uses a
do-not-print fact is left out of the tracks (the cue keeps its shorter form) with a warning. Loop films and
`output.film.tracks: false` have no tracks (`captions: []`, `chapters: []`, `transcript: null`,
`encode.tracks: false`) and no sidecars; a stale sidecar is deleted when its film is re-rendered.

- **Captions**: one cue per text-bearing shot, from the midpoint of its dissolve or dip (its first frame
  without one) to the next cue's start, the last to the film's end; sorted, never overlapping;
  consecutive cues with the same text merge. Every cue then gets `tracks.cue_frames`: at least
  1 s (`MIN_CUE_S`) and its length at 17 characters a second. A run of short cues that are only a
  quiet capture's date label becomes one "<first> – <last>" cue ("June 2026 – August 2026"); any
  other short cue is held into the next cue, which starts later but ends where it did. Only the
  last cue can stay short (the film ends).
  - title → the title (else the kicker); card → the chapter title;
  - capture → "<label>. <words>" ("April 2025. Before."); its note is appended only when the whole
    cue stays at or under 17 characters per second of the cue's length (`tracks.CPS`);
  - curtain → one cue per step, from the entry (first step) or the sweep start: "<before> / <after>:
    <step text, plain>" under the same 17 characters/s rule, else "<before> / <after>";
  - stat → "<value> <unit> <label>"; end → the credit line (else the brand name).
  - Classic cuts: the title, each capture's label (+ note under the same rule) from the middle of
    its incoming crossfade, each section card's name and the credit line.
- **Chapters**: each series (the scrub title, else the vantage name), each curtain (the compare
  title, else "<before> / <after>"), the stats (the stats title, else its kicker, else "Figures") and
  "Credits"; a chapter starts with the card that introduces it. The title shot belongs to the first
  chapter, which starts at frame 0. Classic: one chapter per section, then "Credits".
- **Transcript** (`.txt`): the project title and subtitle, then "<m:ss> film, no sound. <honesty
  line>" (from `provenance`, for classic cuts too), "On screen throughout: <tag words joined by ·>."
  when the plan has a `tag`, "Preview, not for release." for a draft or a plan that prints a fact
  that is not releasable (adding "Lines marked [unverified] use figures that are not confirmed
  yet." for the latter), a blank line, then one `[m:ss] …` line per shot (a line never starts after
  the last frame; "[unverified] " after the time when the line prints such a fact, from the plan's
  fact snapshot):
  - title: "Title over the <label> view: <kicker>. <title>";
  - capture: "Aerial view (<vantage name>), <label>: <step text, plain, else the note>" for a series'
    first capture, "Same view, <label>: …" after it; a pushed step adds "The camera moves in on
    <subject hotspot labels joined by ' and '>." ("The camera moves in." without subjects);
  - card: "Chapter card: <kicker>. <title>";
  - curtain: "Split screen (<vantage name>), <before> left and <after> right. The line moves to
    <split>%: <step text>. Then to <split>%: …";
  - stat: "Figure: <value> <unit> <label> (source: <source>).";
  - end: "End card: " and the end card's lines joined by " / ".
  It reads only the plan (`to_transcript(plan)`), so it never prints a fact the gate has not seen.
- **Formats**: WebVTT `WEBVTT` with `hh:mm:ss.mmm` (cue text escapes `&`, `<`, `>`); SRT numbered
  with `hh:mm:ss,mmm`; times are frames converted to whole milliseconds, rounded half up (frame 36
  at 30 fps → 00:00:01.200). UTF-8, LF line ends.
- **Embedding** (`tracks.embed`, before the `.part` rename, for the MP4 and its -web.mp4 when
  `encode.tracks`): a stream-copy remux
  `-i film -f ffmetadata -i chapters -i captions.srt -map 0:v -map 2:s -map_chapters 1
  -map_metadata 1 -c:v copy -c:s mov_text -metadata:s:s:0 language=<encode.lang> -disposition:s:0 0
  -fflags +bitexact -flags:v +bitexact -movflags +faststart`. The ffmetadata file carries the film's
  title and the chapters in milliseconds. `encode.lang` is project.yaml `lang` as ISO 639-2 (en →
  eng, fr → fra, es → spa, de → deu, else und).
  - **Never `-map_metadata -1` here**: chapter titles are metadata, and it blanks them
    (`test_embedded_tracks` guards it).
  - The mp4 muxer always enables the first subtitle track, so `embed` then clears the `enabled` bit
    of that track's `tkhd` (`disable_subtitles`, one bit): players offer the captions but do not
    show them until asked (ffprobe: disposition default 0).
  - The H.264 elementary stream is copied bit for bit (the classic 16:9 film's video stream is the
    step-3 one); the same plan gives the same bytes. The render record hashes the files as shipped.

### Loop encode and --hours (film/render.py, film/loop.py)

- Loop films are encoded with `-g 2B -keyint_min 2B -sc_threshold 0 -x264-params open-gop=0
  -level:v 4.1` (`render.loop_args`) on top of the usual args: an IDR frame on every frame divisible
  by 2B and none elsewhere, so each cycle of a repeated file starts on one. No audio stream, no
  subtitle track, no chapters, no web copy.
- `vantage film <slug> --film <id> --hours H` (0 < H ≤ 12; or a loop FilmSpec's `hours` when its
  film is rendered) writes `film/usb/<slug>-<id>-<H>h.mp4` (`H` as `%g`: `1h`, `0.25h`): the cycle k
  = ceil(H·3600 / cycle_s) times through the concat demuxer, `-map 0:v -c copy -fflags +bitexact
  -flags:v +bitexact -map_metadata -1 -movflags +faststart`, via `.part`. The same cycle gives the
  same bytes. cycle_s is the plan's frames / fps.
- A loop film already in `film/` whose plan.json has the same hash and encode and whose MP4 matches
  its render record is reused, not re-rendered, so `--hours` takes seconds.
- When k × the cycle's bytes would exceed 3.9 GiB (FAT32 holds 4 GiB per file), it refuses and names
  the largest H that fits.
- `--hours` without `--film`, on a film that is not a loop, outside (0, 12] or with `--plan-only` is
  an error before anything is planned.
- `film/usb/` is never packaged, listed in `files`/SHA256SUMS/build.json, budgeted, hosted or
  uploaded as a release asset (every film glob is non-recursive).

### Release gate (film/plan.py check_films; package and film --release)

`package --release` runs `check_release`, rebuilds the site, then re-plans every film that has a file
in `film/` (any output `vantage film` writes: MP4, -web.mp4, poster, plan.json, storyboard, tracks)
and refuses with one line per problem. The remedy names `vantage film <slug> --film <id>` for a film
that is not `auto` (such as `screen`).

1. A film file has no `<name>.plan.json` (rendered by an older engine, or left over): remove it, or
   re-render.
2. Its id is not in the current film set (an old id, an orphaned web copy or sidecar): remove it, or
   run a full `vantage film`.
3. It is stale: the plan hash differs from the re-planned hash. The message names the differing
   top-level plan keys, and hints at VANTAGE_FAST when the size differs. With an equal hash, a
   different `encode` (web, lang, tracks, crf, preset, gop) is a problem too.
4. `plan.draft` is true.
5. The MP4 (or its -web.mp4) sha256 differs from the `render` record, `render` is null, or a plan
   of an `auto` film has no MP4. A `-web.mp4` that is not the current film's web copy (`render.web`
   or the re-planned `encode.web` is null: the web copy was turned off) is a problem; `vantage film`
   deletes it when `encode.web` is null.
6. A fact it prints (as re-planned, on screen or in its tracks) is not releasable under the current
   facts.yaml. Every film text comes from YAML that `check_release` already scans, so in
   `package --release` this is a second line behind it; `check_films` and build.json
   `films.<id>.unreleasable` report it on their own.
7. `render.flash.pass` is false (every cut is metered; a failing film is never written).
8. `require_approval` is on and neither the hash nor its first 12 characters is listed in any
   `approvals.yaml` `approvals[].films`.
9. Text outside the safe zone: `checks.outside` is not empty, or the plan has no `checks`. (`vantage
   film --release` refuses the same before encoding.)
10. A track sidecar (`.vtt`, `.srt`, `.chapters.vtt`, `.txt`) differs from `track_texts(plan)`
    (edited by hand), is missing, or exists when the plan has no tracks.

A plan of a film that is not `auto` with no MP4 (`--film screen --plan-only`, the approval step) is
skipped: it is not a problem and none of its files ship. With `output.film.enabled: false` nothing
in `film/` is checked or shipped (a warning says so). No films at all is allowed (with a warning). A
non-release `package` logs the same problems as warnings and ships only the files of each film that
passed their own check (`files`). `vantage film --release` runs `check_release` and refuses a draft
before planning, and refuses a film with text outside its safe zone (`checks.outside`).

### build.json additions

```jsonc
"tools": {"vantage": …, "python": …, "pillow": …, "ffmpeg": "6.1.1-3ubuntu5"},
"films": {"16x9": {"mp4": "film/demo-lakeside-16x9.mp4", "plan": "film/demo-lakeside-16x9.plan.json",
          "hash": "…", "cut": "classic", "preset": null, "loop": false, "width": 1920, "height": 1080,
          "fps": 30, "frames": 1410, "fresh": true, "draft": false, "facts": [], "unreleasable": [],
          "problems": [],
          "files": ["film/demo-lakeside-16x9-web.mp4", "film/demo-lakeside-16x9.mp4", "…"]}}
```

`files` and SHA256SUMS list every top-level `film/` deliverable the gate accepted (each film's
`files`): its MP4 and -web.mp4, plan.json, storyboard and tracks (`.vtt`, `.srt`, `.chapters.vtt`,
`.txt`), never a poster or `film/usb/`. release.yml uploads `film/*.mp4 film/*.vtt film/*.srt
film/*.txt` from the files SHA256SUMS lists. HOW-TO-VIEW.txt adds "Captions (.srt, .vtt) and a timed
text transcript (.txt) come with each film" when there are tracks (naming the films that have them
when a loop film, which has none, is also shipped). It describes `film/<name>-web.mp4` as "a
smaller copy for websites and uploads (under 25 MB)" and a loop film as "silent loop for an event
screen; set the player to repeat (ask for a long copy for a TV)": `film/usb/` is never shipped, so
it is never pointed to.

### Module API

```python
vantage.models.FilmPlan                     # .load(path) / .save(path) / .hash; models.canonical_json
vantage.film.plan.plan_films(project: Project, *, only: Sequence[str] | None = None,
                             fast: bool | None = None) -> dict[str, FilmPlan]   # pure; hashes inputs
vantage.film.plan.check_films(project: Project, film_dir: Path) -> list[str]    # release problems
vantage.film.plan.film_records(project: Project, film_dir: Path) -> tuple[dict, list[str]]  # build.json films + problems
vantage.film.render.render_film(project, out_dir, *, only=None, release=False, plan_only=False,
                                hours: float | None = None) -> list[Path]   # main MP4s; --hours → out_dir/usb/
vantage.film.loop.concat_hours(mp4: Path, hours: float, out_dir: Path) -> Path   # stream copy; FAT32 guard
vantage.film.render.check_seam(plan, film) -> None                          # loop: frame(N) == frame(0)
vantage.film.render.film_jobs(n: int) -> int                                # worker processes for n films (VANTAGE_FILM_JOBS)
vantage.film.tracks.write_tracks(plan: FilmPlan, stem: Path) -> list[Path]   # <stem>.vtt .srt .chapters.vtt .txt
vantage.film.tracks.to_vtt(plan) / to_srt(plan) / to_chapters_vtt(plan) / to_ffmetadata(plan) / to_transcript(plan) -> str
vantage.film.tracks.embed(mp4: Path, plan: FilmPlan) -> None                # stream-copy remux in place (mov_text + chapters)
vantage.film.tracks.Tracks(fps, enabled=True)                               # plan time: .cue / .chapter / .line → .record(frames, …)
vantage.film.tracks.iso639_2(lang) -> str                                   # 'en' → 'eng', else 'und'
vantage.film.storyboard.write_storyboard(plan: FilmPlan, frame_fn: Callable[[int], ndarray],
                                         facts: Mapping[str, Mapping], out: Path) -> Path
vantage.film.plan.resolve_films(project: Project) -> dict[str, FilmDef]     # enabled films, settings merged
vantage.film.beat.beat_grid(fps, bpm) -> Grid                               # ValueError "bpm too fast for fps"
vantage.film.beat.frames_per_beat(fps, bpm) / dissolve_frames(fps, B) / bar_beat(f, B)
vantage.film.beat.hold_windows(keys, hold) / keyed(windows, values, v, mix)  # ports of runtime/js/25-stage.js
vantage.film.layout.layout(name, width, height, *, safe=None, type_scale=1.0) -> Layout   # story-*/classic-*
vantage.film.render.check_ink(plan: FilmPlan, film) -> list[str]            # plan.checks; text outside the safe zone
vantage.film.plan.encode_settings(film, settings, frames, fps, *, fast, fpb=None) -> dict   # plan.json encode (web, gop)
vantage.film.plan.web_maxrate(duration_s: float) -> int                     # the web encode's VBV cap, bit/s
vantage.film.story.derive_cut(project, film, index, fonts, hashes, warnings, *, fast) -> FilmPlan
vantage.film.safety.check_plan(plan: dict) -> None                          # PlanError
vantage.film.safety.surface_dip(fps, dissolve, night, paper) -> int          # frames of a dip to or from paper
vantage.film.safety.LumaMeter(fps).add(frame) / .result() / .check(name)    # FlashError
vantage.film.render.encode_frames(film, part, *, crf, preset, meter=None, web=None, gop=None)  # the one encode
                                                                            # path; web=(path, encode.web) adds the web copy;
                                                                            # gop=encode.gop closes a loop's GOPs
vantage.media.ffmpeg.FrameWriter(..., extra_outputs: Sequence[tuple[Path, Sequence[str]]] = ())  # one pipe, several encodes
vantage.media.ffmpeg.ffmpeg_version() -> str
```
