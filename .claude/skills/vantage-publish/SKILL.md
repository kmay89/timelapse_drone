---
name: vantage-publish
description: >-
  Release a finished Vantage story: release build with the gates on, packaging, checksums, then
  delivery (hosted site, GitHub Release, single files, films) with HOW-TO-VIEW text and a share
  message. Run only when the person explicitly asks to publish or release.
argument-hint: "<project> [edition tag, e.g. 2026-10]"
disable-model-invocation: true
allowed-tools: Bash(uv run vantage *), Bash(uv run python scripts/budgets.py *), Bash(npm run test:web*), Bash(ls *), Read, Glob, Grep
---

# Publish a story

Request: `$ARGUMENTS`. Publishing puts a client's story in front of people: every step that leaves
the machine needs the person's explicit go-ahead. Read `docs/DELIVERY.md` first.

## 1. Preconditions (stop if any fails)

- `/vantage-verify` passed on this commit and the person looked at the screenshots.
- Client approval of the copy and every fact is recorded (see `docs/PROJECTS.md`, approvals).
- `project.yaml` has `draft: false`; `simulated: true` only for demos.
- The project is in the private projects repo, committed, with its masters.

## 2. Release build (gates on)

```sh
uv run vantage all <project> --release      # fails on draft, unknown or unapproved {fact:…}
uv run python scripts/budgets.py dist/<slug>
(cd dist/<slug> && sha256sum --check SHA256SUMS)
```

Then run the browser suite once more against the release build (`VANTAGE_STORY=<slug> npm run
test:web:local`). Fix nothing here: a failure sends the work back to `/vantage-verify`.

## 3. Choose where it goes (ask; never assume)

| Target | When | How |
|---|---|---|
| Hosted site (Cloudflare Pages / Workers static assets) | the main iPhone path; unlisted until launch | upload `dist/<slug>/site/` under `/<slug>/`; no file over 25 MiB (budgets check it) |
| Password preview (Netlify) | client review before launch | deploy `dist/<slug>/site/` as a preview with password protection |
| GitHub Release | versioned archive of the offline deliverables | tag `<slug>@<edition>` in the repo that holds the project; `release.yml` builds and attaches the zip, single files, films, SHA256SUMS |
| Large media / zip download | files over 25 MiB, client download links | R2 bucket behind a custom domain |
| GitHub Pages | **only the fictional demo** | `pages.yml` on main; Pages sites are public even from private repos |

**Checkpoint:** state the exact target, URL path and visibility (public, unlisted, password), and
wait for "yes". Never deploy a client story to GitHub Pages or any public URL without that.

## 4. Deliver

- Hosted: after deploy, open the URL on a phone (or run the web suite with
  `VANTAGE_URL=https://…/<slug>/ npm run test:web:local`) and confirm the share card and icon.
- Files: `dist/<slug>/HOW-TO-VIEW.txt` explains each file to the recipient; send it with them.
- Films: `dist/<slug>/film/<slug>-16x9.mp4` (meetings, web) and `-9x16.mp4` (phones, social).

## 5. Share text (draft for the person to send)

> **<Title>**: <one-line dek>. Open on your iPhone: <URL>
> To keep it: Share → Add to Home Screen, then open it from the icon and tap "Save for offline".
> Files (zip, single page, films): <release or download link>

## 6. Record

Note in the project (private repo): edition tag, date, URL, build.json `generatedAt`, the SHA256SUMS,
and who approved. Real-device checks for this edition (`docs/DELIVERY.md`, "iPhone checklist"):
Quick Look of the lite file, offline from the Home Screen, video playback, Low Power Mode.

## Failure handling

| Symptom | Fix |
|---|---|
| `release build blocked: …` | each line names the fact or setting; back to `/vantage-write-story` or the client |
| budgets fail | lite: fewer essential images or lower quality; hosted file > 25 MiB: move it to R2 |
| SHA256SUMS mismatch | something edited dist after packaging: run `vantage package` again |
| tag exists already | `release.yml` re-uploads assets to the existing release (`--clobber`) |
