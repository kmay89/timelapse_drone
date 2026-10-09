# Delivery, packaging, CI and the Claude-operated repo: research notes

Condensed research behind `docs/DELIVERY.md`, the workflows and the Claude Code setup. Tags: **[C]**
confirmed from a primary source (October 2026); **[R]** reported or secondary; **[I]** inference;
**[U]** recollection, verify before relying on it.

## Recommendations in brief

1. **Two channels plus fallbacks**: a hosted link (PWA with "Save for offline") is the only full
   experience on iPhone; offline files (full and lite single files, zip) and MP4 films cover the rest.
   Design every page for JavaScript off: iOS Files/Mail Quick Look should be treated as no-JS.
2. **Hosting**: site on Cloudflare (25 MiB per file [C]), large media in R2 behind a custom domain ($0
   egress [C]), GitHub Pages only for the public demo (Pages sites are public even from private repos
   on Free/Pro/Team [C]), Netlify for password previews.
3. **Raw footage never in git, not even LFS**: an R2 bucket via rclone, a committed manifest, a
   private projects repo for YAML, brand and masters.
4. **CI on every PR**: ruff + pytest; the synthetic demo built with apt ffmpeg; budgets; Playwright
   with iPhone WebKit and Chromium, JS-off and reduced-motion variants, `file://` checks; screenshots as
   artifacts. Release tags publish GitHub Releases.
5. **Claude Code operation**: CLAUDE.md under 200 lines, skills per workflow step, a SessionStart hook,
   subagents for review, and an optional Claude API caption step that skips without credentials.

## What happens on an iPhone

| Path | Behaviour | Status |
|---|---|---|
| tap an `.html` in Files or Mail | Quick Look; [QLPreviewController](https://developer.apple.com/documentation/quicklook/qlpreviewcontroller) previews `public.text` types and does not mention JavaScript | doc [C]; no-JS [U] |
| relative assets after unzipping | probably not loaded: the preview is scoped to one file | [I] |
| local file in Safari | iOS Safari does not open `file://` | [U] |
| apps that run local HTML | any `WKWebView.loadFileURL(_:allowingReadAccessTo:)` app | API [C] |
| email | most providers cap attachments at 20–25 MB, and MIME adds a third | [U]; ≤ 15 MB is email-safe [I] |
| CSS scroll-driven animations | Safari/iOS 26, Chrome 115 ([BCD](https://github.com/mdn/browser-compat-data/blob/main/css/properties/animation-timeline.json)) | [C] |

## Single-file engineering

- Base64 adds exactly a third; recompressing media gains nothing [I].
- `data:` URL limits: WebKit 2048 MB, Chromium and Firefox 512 MB; top-level navigation to `data:` is
  blocked ([MDN](https://developer.mozilla.org/en-US/docs/Web/URI/Reference/Schemes/data)) [C]. Real limits on iPhone are memory [I].
- Structure [I]: the essential set (hero, before/after pairs) as real `<img>` with `data:` sources so
  the no-JS essay works; the heavy set as base64 in `<script type="application/octet-stream">` blocks,
  decoded lazily to blob URLs and revoked when unused; never `<video src="data:…">`.
- Budgets: lite ≤ 15 MB, full ≤ 60–80 MB, zip unbounded (warn above 1 GB).

## PWA and offline

- Since iOS 17 / macOS 14, Safari and Home Screen web apps get about 60 % of disk per origin; other
  WebView apps 15 % ([MDN quotas](https://developer.mozilla.org/en-US/docs/Web/API/Storage_API/Storage_quotas_and_eviction_criteria),
  [WebsiteDataStoreCocoa.mm](https://github.com/WebKit/WebKit/blob/main/Source/WebKit/UIProcess/WebsiteData/Cocoa/WebsiteDataStoreCocoa.mm)) [C].
- With tracking prevention on, script-written data is deleted after 7 days of browser use without
  interaction [C]. `persist()` from Safari 15.2, `estimate()` from 17 ([BCD](https://github.com/mdn/browser-compat-data/blob/main/api/StorageManager.json)) [C].
- Service worker [I]: per-story scope and cache names, old caches deleted on activate; precache the
  shell and one image size on install; "Save for offline" caches the rest with progress, then
  `persist()`. iOS `<video>` sends Range requests: answer with `206` slices from cache (the problem
  [workbox-range-requests](https://github.com/GoogleChrome/workbox/tree/v7/packages/workbox-range-requests) solves). Serve media same-origin.

## Hosting limits (October 2026)

| Host | Limits | Use |
|---|---|---|
| GitHub Pages | site ≤ 1 GB; soft 100 GB/month; artifact ≤ 10 GB (1 GB supported), no symlinks; public even from private repos ([limits](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits), [upload-pages-artifact](https://github.com/actions/upload-pages-artifact)) [C] | public demo |
| Cloudflare Pages / Workers assets | 25 MiB per file; 20,000 files free ([limits](https://developers.cloudflare.com/pages/platform/limits/)) [C] | client sites |
| Cloudflare R2 | $0.015/GB-month (IA $0.01 + retrieval), egress $0, 10 GB-month free; `r2.dev` not for production ([pricing](https://developers.cloudflare.com/r2/pricing/), [limits](https://developers.cloudflare.com/r2/platform/limits/)) [C] | video, zip downloads, footage |
| Netlify | atomic deploys, deploy previews, password protection; free deploys deleted after 30 days [C] | client previews |
| GitHub Releases | assets < 2 GiB each, no total limit ([about releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)) [C]; private assets need a login [I] | versioned offline archive |

## Raw footage

- GitHub warns at 50 MiB and blocks files over 100 MiB; LFS free tiers include 10 GiB storage and 10
  GiB/month bandwidth, overage $0.07/GiB stored and $0.0875/GiB transferred, blocked on a $0 budget
  ([LFS billing](https://docs.github.com/en/billing/concepts/product-billing/git-lfs)) [C]. A 4K flight is 5–10 GB [I].
- R2 bucket `vantage-footage/<slug>/<YYYY-MM-DD>/…`, rclone S3 backend with `provider = Cloudflare`,
  `region = auto` ([rclone s3](https://github.com/rclone/rclone/blob/master/docs/content/s3.md)) [C]; about $2/month for 150 GB.
- Commit a manifest (path, bytes, hash, ETag, duration, codec, capture start, aircraft, visit date).
- Work on proxies; pull full-resolution frames only at chosen times (`ffmpeg -ss T -i <presigned URL>`
  seeks with HTTP ranges). Cloud Claude Code sessions have about 30 GB disk, 4 vCPU, 16 GB RAM
  ([cloud environments](https://code.claude.com/docs/en/cloud-environments)) [C].
- Commit per project: YAML, citations, brand files (licensed fonts only), masters (≈ 1–1.5 MB each).
  Never: `footage/`, `work/`, `dist/`.

## CI with GitHub Actions

- Runners: public repos 4 vCPU / 16 GB; **private 2 vCPU / 8 GB / 14 GB SSD**; macOS arm64 3 vCPU; 6 h
  per job ([runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)) [C].
- Free private plan: 2,000 minutes/month and **500 MB artifact storage**: keep screenshots small and
  retention at 7 days. Cache 10 GB per repo, 7-day eviction ([caching](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)) [C].
- The Ubuntu 24.04 image has **no ffmpeg**; `apt-get install ffmpeg` gives 6.1.1 with libx264, libx265,
  libsvtav1 ([runner image](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)) [C].
- Playwright's WebKit is built from WebKit main, not Safari; codecs vary by OS ([browsers.md](https://github.com/microsoft/playwright/blob/main/docs/src/browsers.md)) [C].
  Playwright 1.56 descriptors go up to iPhone 15 Pro Max [C]. Real Mobile Safari needs a macOS runner
  with the iOS Simulator (`xcrun simctl`), billed at macOS rates on private repos.
- Lighthouse CI runs Chromium only: a performance proxy, not iOS verification ([LHCI](https://github.com/GoogleChrome/lighthouse-ci/blob/main/docs/getting-started.md)).
- Workflows used here: `ci.yml` (python, demo + budgets, web), `pages.yml` (demo only), `release.yml`
  (tags `v*` and `<slug>@<edition>`). Candidates for later: an iOS Simulator smoke test on release,
  PR previews on Netlify/Cloudflare (Pages PR previews are not public), a reusable build workflow for
  the private projects repo, `claude-code-action` for `@claude` mentions.

## Claude Code mechanics

- **CLAUDE.md** under 200 lines; `.claude/rules/*.md` with `paths:` load only for matching files
  ([memory](https://code.claude.com/docs/en/memory)) [C].
- **Skills** in `.claude/skills/<name>/SKILL.md` (under 500 lines, supporting files beside it);
  frontmatter `name`, `description` (the trigger), `argument-hint`, `allowed-tools`,
  `disable-model-invocation` for side-effecting skills; `$ARGUMENTS` and `${CLAUDE_SKILL_DIR}`
  substitutions ([skills](https://code.claude.com/docs/en/skills)) [C].
- **Hooks** in `.claude/settings.json`; `SessionStart` stdout becomes context; `CLAUDE_CODE_REMOTE=true`
  in cloud sessions ([hooks](https://code.claude.com/docs/en/hooks)) [C]. In a multi-repo cloud session
  no repo's hooks run: use the environment setup script instead.
- **Subagents** in `.claude/agents/` are picked up automatically.
- **GitHub Actions**: `anthropics/claude-code-action@v1` takes a `prompt` (which can be a skill) and
  `claude_args` ([usage](https://github.com/anthropics/claude-code-action/blob/main/docs/usage.md)) [C].

## Things to check on a real iPhone before promising "offline"

1. The single and lite files from Files, Mail and AirDrop: does JS run, do `data:` pictures render,
   does a scroll-driven CSS scrub work, does an 80 MB file open?
2. Does an unzipped `index.html` in Files load its relative assets?
3. PWA: Add to Home Screen, Save for offline, airplane mode; video through the worker's `206` replies;
   `persist()` and `estimate()`; does it survive more than 7 days?
4. The email-safe size with the client's own mail provider.
