# Delivery

How a finished story reaches people, where it is hosted, where raw footage lives, and how a release
is cut. Sources and details: [`research/delivery-ci.md`](research/delivery-ci.md).

## Editions

One build (`vantage all <project>`) writes every edition into `dist/<slug>/`:

| Edition | File | Best for | Budget / limit |
|---|---|---|---|
| Hosted PWA | `site/` | the full experience on iPhone; sharing by link | each file ≤ 25 MiB (static hosts) |
| Full single file | `<slug>.html` | desktop, AirDrop to a Mac, archive | `output.single_file_max_mb` (default 60 MB); video dropped first, then image quality |
| Lite single file | `<slug>-lite.html` | email attachment, iOS Quick Look | ≤ `output.lite_max_mb` (default 15 MB); key images only, no video |
| Offline zip | `<slug>-offline.zip` | desktop `file://`, long-term archive | release assets < 2 GiB each |
| Films | `film/<slug>-16x9.mp4`, `-9x16.mp4` | everyone: meetings, Photos, social | H.264, plays everywhere |
| Info | `HOW-TO-VIEW.txt`, `SHA256SUMS`, `build.json` | recipients, integrity, provenance | |

`<html data-edition>` is `site`, `offline`, `single` or `lite`. Only the hosted site registers a
service worker; everything else works from disk with no network at all (the browser suite checks it).
Inside a single file, images the no-JS essay shows are `data:` URIs; the rest are base64 blocks the
runtime turns into blob URLs on demand; videos are never `data:` sources.

## iPhone realities

| Path | What happens | Consequence |
|---|---|---|
| tap a link in Messages or Mail | Safari opens the hosted site | the full experience |
| tap an `.html` attachment or a file in Files | **Quick Look** preview, very likely with JavaScript off and no sibling files | the lite and full files must read as a complete photo essay with JS off |
| unzip the offline zip in Files | Quick Look again, relative assets unreliable | the zip is a desktop deliverable |
| Safari and local files | iOS Safari does not open `file://` | |
| Share → Add to Home Screen, then "Save for offline" | the service worker caches every asset with progress | works in airplane mode |

- Storage: since iOS 17, Safari and Home Screen web apps get roughly 60 % of disk per origin.
  Script-written storage can be evicted after 7 days without use in Safari; the runtime calls
  `navigator.storage.persist()`.
- iOS `<video>` makes HTTP Range requests: the service worker answers from cache with `206` slices
  (`runtime/sw.js`), and `vantage preview` serves byte ranges.
- The text that goes with a link: *"Open the link in Safari, tap Share, then Add to Home Screen. Open
  it from the new icon and tap Save for offline. It now works in airplane mode."* (`HOW-TO-VIEW.txt`
  has the full version.)

## Hosting options

| Host | Limits that bind | Use for |
|---|---|---|
| Cloudflare Pages / Workers static assets | 25 MiB per file; 20,000 files (free) | client story sites, one path per project, unlisted + `noindex` until launch |
| Cloudflare R2 behind a custom domain | $0.015/GB-month, **no egress fees**, 10 GB-month free; `r2.dev` is not for production | files over 25 MiB, zip downloads, raw footage |
| Netlify | deploy previews, password protection; free deploys expire after 30 days | client review previews |
| GitHub Pages | 1 GB site; **public even from private repos** on Free/Pro/Team | **only the fictional demo** (`pages.yml`) |
| GitHub Releases | each asset < 2 GiB, no total limit; private-repo assets need a GitHub login | versioned archive of the offline deliverables |

Serve video from the site's own origin (or route `/media/*` to R2 through a Worker): cross-origin
opaque responses cannot be range-sliced by the service worker.

Before launch, keep a client site unlisted: `draft: true` adds `noindex`; use an unguessable path,
Cloudflare Access or a Netlify password for review.

## Raw footage storage

- **Never in git, not even LFS.** One 4K flight is roughly 5–10 GB; a year of monthly flights is
  60–150 GB, far beyond LFS free quotas (10 GiB storage and bandwidth; uploads blocked when exceeded
  on a $0 budget).
- **Bucket**: one R2 bucket, `vantage-footage/<slug>/<YYYY-MM-DD>/DJI_…` (about $2 a month for 150 GB).
  Transfer with [rclone](https://rclone.org/) (S3 backend, `provider = Cloudflare`, `region = auto`):

  ```sh
  rclone copy ./footage/2026-09-12 r2:vantage-footage/<slug>/2026-09-12 --progress
  rclone copy r2:vantage-footage/<slug>/2026-09-12 "$VANTAGE_PROJECTS/<slug>/footage/2026-09-12"
  rclone check ./footage r2:vantage-footage/<slug>      # after every upload
  ```

- Keep a second copy on a local drive. After a project ends, move the bucket to infrequent access.
- Masters (aligned JPEGs) **are** committed in the private projects repo: they are small and let CI
  rebuild the story without footage.
- Cloud Claude Code sessions have limited disk (about 30 GB): pull only the visits being processed,
  or run heavy ingests locally.

## Release process

1. `/vantage-verify` passes; the person has looked at the iPhone and no-JS screenshots.
2. The client approves copy and every fact (recorded as in [PROJECTS.md](PROJECTS.md#approvals)).
3. `draft: false`, then a gated build: `uv run vantage all <project> --release` (fails on drafts and
   on unknown or unapproved facts), `uv run python scripts/budgets.py dist/<slug>`.
4. Tag `<slug>@<edition>` (for example `<slug>@2026-10`) in the repo that holds the project. The
   release workflow builds from committed masters with the gates on, checks budgets and checksums,
   and attaches the zip, both single files, the films, `HOW-TO-VIEW.txt`, `build.json` and
   `SHA256SUMS` to a GitHub Release. Engine tags `v*` release the demo as a sample.
5. Deploy `dist/<slug>/site/` to the host (`/vantage-publish` walks through it). Convention: each
   edition stays reachable at its own path (`/<slug>/v/<edition>/`) and `/<slug>/` serves the latest.
6. Send the link with the share text, and the release assets to whoever needs files.
7. Real-iPhone pass for the edition: Quick Look of the lite file; offline from the Home Screen in
   airplane mode; video through the service worker; Low Power Mode; an older device.

Rollback: redeploy the previous edition's `site/` (hosted platforms keep deployments); released
files are immutable.
