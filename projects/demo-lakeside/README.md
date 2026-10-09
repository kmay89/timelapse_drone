# Demo: *Where the Wave Pool Was*

A complete Vantage project about a **fictional** lakeside site: an abandoned waterpark that becomes a
public park over eighteen months. It is the showcase for the whole system until real client footage
exists, and the end-to-end fixture that CI builds on every push.

Nothing here is real. The town, the lake, the Lakeside Parks Conservancy and its partners, the people
quoted and every figure are invented, and the drone footage is computer-generated.

## Run it

```sh
uv run vantage demo            # synthesize full-quality footage (about 2–3 minutes), then process, build, film, package
uv run vantage demo --fast     # CI-sized footage (about 15–40 seconds)
uv run vantage preview demo-lakeside
```

`vantage demo` reuses footage that is already in `footage/`; pass `--regenerate` to render it again.
To produce only the footage:

```sh
uv run python -m vantage.demo.synth projects/demo-lakeside/footage [--fast] [--seed 7]
```

## What the generator makes

`vantage.demo.synth` composes a planar, top-down site plan (520 × 340 m) for each of twelve flights
between 2025-04-12 and 2026-09-20, lights it for the date and time (sun position, season, snow, lake
ice) and photographs it through an exact pinhole drone camera:

| take | files per visit | notes |
|---|---|---|
| overview video | `DJI_0001.MP4` + `DJI_0001.SRT` | oblique, from over the parking lot, hover drift |
| overview still | `DJI_<yyyymmddhhmmss>_0001.JPG` | EXIF date/GPS + DJI XMP gimbal tags |
| shoreline video / still | `DJI_0002.MP4` / `DJI_<…>_0002.JPG` | from over the water, looking back at the beach |
| distractor | `DJI_0003.MP4` (2025-09-05 only) | straight down over the water; must not be picked |

Two visits are stills-only (2025-12-12, 2026-06-27) and two are video-only (2025-05-30,
2026-03-21). Each visit's camera is perturbed (position ±7 m, yaw ±2.5°, roll ±1.5°, altitude ±4 %,
pitch ±2°) and gets its own exposure, white balance and haze, so registration has real work to do.

Because the world is a plane, the world→image homography of every frame is exact. It is written to
`footage/_truth.json` (per file, and per frame for videos) together with the camera pose, sun, season
and the site's points of interest, so alignment can be measured instead of eyeballed:

```python
from vantage.demo.synth import load_truth, relative_homography

truth = load_truth("projects/demo-lakeside/footage")
H = relative_homography(
    truth, "2025-04-12/DJI_20250412154159_0001.JPG", "2026-09-20/DJI_20260920172528_0001.JPG"
)
```

## Files

| file | what |
|---|---|
| `project.yaml` | title, dek, location, processing and output settings (`simulated: true`) |
| `story.yaml` | two vantages and one chapter of every type; hotspots computed from the site plan |
| `facts.yaml` | every figure printed through `{fact:id}` tokens, with sources and status |
| `brand/brand.yaml` | the fictional Lakeside Parks Conservancy: palette, Fraunces + Inter, voice, partners |
| `brand/*.svg`, `brand/partners/*.svg` | original logos built from geometric paths (no fonts) |
| `footage/`, `masters/`, `work/` | generated; never committed |
