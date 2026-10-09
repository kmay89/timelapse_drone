# Vantage — architecture & contracts

Vantage turns repeat drone flights over a changing site into a cinematic,
iPhone-first interactive progress story (plus a classic MP4 time-lapse film),
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
        └──► film ─► dist/<slug>/film/<slug>-16x9.mp4, <slug>-9x16.mp4
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
  film/render.py         MP4 time-lapse films (16:9 and 9:16) with branded title/end cards
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
| `vantage film <project>` | render MP4 films |
| `vantage package <project>` | single-file HTML + offline zip |
| `vantage all <project>` | process → build → film → package |
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
  assets/fonts/…                 woff2 actually used
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
service worker there) and from `https://` (service worker registers and
precaches every asset so "Add to Home Screen" works offline).

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
`MastersIndex`, each with `.load(path)` / `.save(path)`); config models in
`vantage/config.py` (`load_project(path) -> Project`).

```python
vantage.ingest.catalog.build_catalog(project: Project, *, force: bool = False) -> Catalog
    # writes work/catalog.json + work/candidates/** + work/contact/<date>.jpg
vantage.process.select.select_frames(project: Project, catalog: Catalog | None = None) -> Selection
    # writes work/selection.json (loads catalog.json when not given)
vantage.process.masters.make_masters(project: Project, selection: Selection | None = None) -> MastersIndex
    # writes masters/<vantage>/<date>.jpg + masters/index.json + work/review/**
vantage.site.build.build_site(project: Project, out_dir: Path) -> Path
    # out_dir = dist/<slug>/site ; returns out_dir / "index.html"
vantage.film.render.render_film(project: Project, out_dir: Path) -> list[Path]
    # out_dir = dist/<slug>/film
vantage.site.package.package_project(project: Project, dist_dir: Path) -> dict[str, Path]
    # dist_dir = dist/<slug>; reads dist_dir/site; returns {"single_file": …, "zip": …}
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
