# COLMAP for drone ingestion: is it the wow?

Short answer: **COLMAP is usable, but it is not the wow by itself.** It solves the hardest part of
alignment (one 3D model across visits, seasons and construction), but the picture that makes people
say "wow", walking around the site in 3D and scrubbing through time, also needs a GPU, orbit
footage and a lazy 3D viewer outside the runtime budget. Below is what was measured, what COLMAP
would buy Vantage now, and what the wow version costs.

## What was checked

- **Binary**: `pycolmap` 4.2.1 from PyPI (`uv pip install pycolmap`, about 126 MB, BSD-3 like COLMAP).
  The default Linux wheel is **CPU only** (`pycolmap.has_cuda == False`). SIFT extraction, matching,
  incremental and global mapping, bundle adjustment, GPS alignment (`align_reconstruction_to_locations`)
  and `estimate_sim3d` all work on CPU. **Dense stereo (`patch_match_stereo`) needs CUDA**, so dense
  point clouds and meshes are not available from this wheel; `pycolmap-cuda12` or a source build is
  required.
- **REA** (Reverse Engineer Anything) was pointed at the wheel's native core. `rea inspect-artifact`
  inventoried it (ELF, digest recorded). The deeper probes (`inspect-binary-layout`, symbol and
  function analysis) refuse inputs over 32 MiB and need Ghidra, Hopper or IDA, so the API was read
  from the COLMAP sources and the module itself instead. REA adds little for an open-source library;
  its value is for closed binaries.
- **Probe on the fictional demo**: overview vantage, all 10 visits, each as one still (1600 px) plus
  4 frames from the hover video (960 px), 50 images, 4 CPU threads, SIFT 8000 features, exhaustive
  matching, incremental mapping, seed 0. Camera centres compared with `footage/_truth.json` after a
  similarity fit on the stills.

| Run | Time (extract / match / map) | Models | Stills registered | Centre error | Ground height |
|---|---|---|---|---|---|
| one camera per image | 23 s / 43 s / 152 s | 1 (50 images, 0.51 px) | 10 / 10 | 3–8 m | wrong (median z −54 m) |
| one camera per lens (folder) | 22 s / 48 s / 165 s | 5 (largest 35 images) | 8 / 10 | **0.01–0.06 m** | right (z 5/50/95 %: −1.6 / 0.1 / 2.1 m) |

Readings:

1. **Cross-season registration works.** Snow, demolition, bare earth and finished summer visits
   matched into one model with sub-pixel reprojection error.
2. **Intrinsics must be shared.** One camera per image lets the near-planar site fold into a wrong
   depth (the classic planar ambiguity). One camera per lens gives centimetre camera centres, but the
   model fragments, so a real integration needs a focal prior from EXIF (`FocalLengthIn35mmFilm`),
   GPS pose priors from the SRT/XMP telemetry Vantage already parses, and a merge step.
3. **The demo cannot show 3D.** `vantage.demo.synth` photographs a textured plane through a homography,
   so any reconstruction of it is flat (heights only exist in its shading). A 3D feature needs real
   footage or a new synthetic renderer that draws true geometry.
4. **CPU cost is fine for a hover vantage** (about 4 minutes for 50 images) and grows with image count;
   an orbit of 150–300 frames per visit is 10–30 minutes per visit on CPU without dense stereo.

## What COLMAP buys Vantage now (CPU, no new deliverable)

- **Parallax-correct alignment.** Today `process/align.py` fits one homography, exact only for a
  plane (see `pipeline-tech.md`, "Parallax"). With a joint model, fit the ground plane to the sparse
  points on stable areas and warp each visit with the **plane-induced homography** from the two
  camera poses. Rooftops, trees and the work zone stop dragging the fit; ghosting is confined to tall
  new structures, which is honest.
- **Pilot feedback in metres.** After GPS alignment, each visit's camera centre and heading relative to
  the reference, printed on the review sheet and in `/vantage-flight-plan`: "flown 6 m east and 4 m
  low; expect parallax on the pavilion roof."
- **Chaining for free.** Visits too different from the reference join through their neighbours inside
  the model, which `select.py` does by hand today.

These improve quality and review, which readers notice as a steadier time-lapse rather than as a
new effect.

## What the wow costs

The effect worth building is a **"walk the site" chapter**: an orbitable 3D model per epoch with a
time scrub, pins on real 3D positions, and a fly-in from the hero frame. It needs:

| Piece | Requirement |
|---|---|
| Capture | an orbit or the −60° mapping pass on milestone visits (`CAPTURE_GUIDE.md` E); the hover shot alone has too little baseline |
| Poses | COLMAP (CPU is fine) with EXIF focal priors and GPS priors, one model across epochs |
| Geometry | Gaussian splats trained with gsplat or Brush (Apache-2.0) on a **GPU**; or COLMAP dense stereo with `pycolmap-cuda12` |
| Delivery | SPZ-compressed splats per epoch (a few MB each), the Spark viewer (MIT, about 922 KB gz) loaded lazily behind a tap on the hosted site only; poster image and MP4 fly-around as the no-JS, lite and film fallbacks |
| Budgets | the viewer stays outside the 30 KB runtime budget as an optional island (`BLUEPRINT.md` §5); the lite file never carries it |
| Determinism | splat training is not bitexact; commit trained splats as masters (like `masters/`) so builds stay reproducible |

## Recommendation

1. Add COLMAP as an **optional extra** (`vantage[sfm]`, pycolmap CPU) behind a `vantage sfm` stage:
   joint model per vantage with EXIF/GPS priors, plane-induced alignment as `align.method: sfm`, and
   the metres-off report on the review sheet. Measurable against the demo truth today.
2. Treat the 3D chapter as a separate project gated on (a) one real orbit capture from a milestone
   visit, (b) access to a CUDA machine for training, and (c) a decision to ship a lazy viewer island on
   the hosted edition. Prototype it on that one capture before touching the runtime.
