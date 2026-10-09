# {{title}}

A Vantage aerial progress story. Everything here is plain YAML plus the
client's brand files; the pipeline rebuilds the site and films from them at any time.

```
project.yaml        title, location, time zone, processing and output settings
story.yaml          vantages (viewpoints), visit labels, chapters and copy
facts.yaml          every figure and claim the copy cites as {fact:id}, with sources and status
brand/              brand.yaml + logos (+ licensed web fonts, if any)
footage/            raw flights, one folder per visit date   (not committed)
masters/            aligned, color-matched frames             (commit these)
work/               candidates, contact sheets, review sheets (not committed)
```

## Next steps

1. **Brand.** Replace `brand/logo.svg`, `brand/logo-on-dark.svg` and `brand/mark.svg` with the
   client's files, then set colors and fonts in `brand/brand.yaml` (`vantage fonts` lists the
   bundled open-licensed faces).
2. **Footage.** Copy each visit into its own dated folder, keeping DJI `.SRT` files next to their
   videos: `footage/2026-09-12/DJI_0042.MP4`, `footage/2026-09-12/DJI_0042.SRT`, photos alongside.
3. **Ingest.** `vantage ingest {{slug}}` catalogs every file, dates it and samples candidate
   frames. Open `work/contact/<date>.jpg` and choose the frame each vantage should align to; set it
   as `reference` in `story.yaml` (`source` is the path under `footage/`, `t` the second shown on
   the contact sheet).
4. **Process.** `vantage process {{slug}}` picks the best frame of every visit, aligns and
   color-matches them into `masters/`. Check the review sheets in `work/review/`; pin any wrong
   pick under the vantage's `picks:`.
5. **Write.** Replace every `TODO` in `story.yaml`, `project.yaml` and `facts.yaml`
   (`vantage validate {{slug}}` counts what is left). Every number, date or claim goes in
   `facts.yaml` with its sources and is cited as `{fact:id}`; a release build refuses facts that are
   not verified or client-approved. Optional: `vantage caption {{slug}}` drafts captions and alt
   text with Claude into `work/llm/suggestions.yaml`.
6. **Build and review.** `vantage all {{slug}}` builds the site, the films and the packages;
   `vantage preview {{slug}} --host 0.0.0.0` serves it so you can open it on an iPhone on the same
   Wi-Fi.
7. **Deliver.** `dist/{{slug}}/` holds the hosted site folder, a single self-contained
   `{{slug}}.html`, an offline zip and the MP4 films. Set `draft: false` in `project.yaml` first.

## Capture tips

Fly the same viewpoint every visit: same take-off spot, altitude, heading and gimbal pitch (save
it as a waypoint or note the numbers from the first flight's `.SRT`). Shoot a 10–20 s steady
hover plus a few stills; mid-morning light or a bright overcast day keeps visits comparable.
