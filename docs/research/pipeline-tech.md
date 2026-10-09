# Footage ingest and processing: technical notes

Condensed research notes behind `ingest/`, `process/`, `film/` and `llm/`. Tags: **[V]** checked
against a primary source or run locally (ffmpeg 6.1.1, OpenCV 5.0.0, Pillow 12.3); **[K]** prior
knowledge, confirm with a sample from the client's drone; **[I]** inference or recommendation.

## Summary

1. The core path is classical, CPU-only and deterministic: telemetry from DJI `.SRT` and photo XMP →
   candidate frames narrowed by position, altitude and heading → SIFT + USAC_MAGSAC homography + ECC
   refinement on stable areas → stable-area colour matching → common crop → committed masters. On a
   synthetic pair with a new building and an exposure/white-balance shift, corner error was 0.10–0.26
   px in about 1 s on CPU [V].
2. OpenCV 5 main wheels have no AKAZE/KAZE/BRISK (moved to contrib `xfeatures2d`); SIFT and ORB stay
   [V]. SIFT is the default.
3. Manual overrides belong in committed YAML (`story.yaml` picks and excludes), never only in `work/`.
4. Never invent frames between visits: interpolators are for consecutive video frames [I].
5. Claude suggests (tags, tie-breaks, captions, alt text, a second alignment opinion); a human decides.

## Telemetry and dates

- **Modern DJI `.SRT`** (Mavic 3 / Air / Mini era): bracketed key/values per frame —
  `[latitude: …] [longitude: …] [rel_alt: … abs_alt: …]`, ISO, shutter, `ct`, `color_md` [V from parser
  regexes].
- **Legacy SRT**: `GPS(lon,lat,alt)` — **longitude first** [V]. Quirks: the key `longtitude`; a Mavic Air
  2 bug writing radians; Autel `GPS(W:…,N:…,…m)`; no time zone in SRT dates ([DJI_SRT_Parser](https://github.com/JuanIrache/DJI_SRT_Parser/blob/master/index.js)) [V].
- An SRT is only written when video subtitles are on in the flight app [K]: it is in the capture guide.
- **Embedded MP4 telemetry**: ExifTool decodes DJI protobuf (`djmd`/`dbgi`) for recent models, the only
  video source of gimbal yaw/pitch ([ExifTool DJI.pm](https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/DJI.pm)) [V]. Optional subprocess: `exiftool -ee -n -G3 -json`.
- **Stills**: XMP `drone-dji` has altitudes, gimbal and flight angles, `GpsLatitude` and the misspelled
  `GpsLongtitude` [V]; Pillow exposes XMP as `im.info["xmp"]`.
- **Date precedence** [I]: folder `YYYY-MM-DD` → SRT local time → EXIF `DateTimeOriginal` → DJI filename
  timestamp → QuickTime `creation_time` → mtime. QuickTime times are nominally UTC but many drones
  write local time; evening flights in US time zones cross UTC midnight, so mixing them shifts the
  visit date. Store local dates with the project time zone and record `date_source`.

## Sampling commands [V]

```sh
ffprobe -v error -show_entries format_tags=creation_time:stream=codec_name,codec_tag_string,width,height,r_frame_rate,pix_fmt,color_transfer -of json IN.MP4
ffmpeg -skip_frame nokey -i IN.MP4 -fps_mode passthrough -frame_pts 1 -q:v 2 kf/%06d.jpg      # keyframes only
ffmpeg -i IN.MP4 -vf "fps=2,scale=1600:-2:flags=lanczos" -q:v 3 cand/%05d.jpg                 # 2 fps candidates
ffmpeg -ss 12.500 -i IN.MP4 -frames:v 1 -vf "lut3d=file=dlogm.cube,format=rgb48be" master16.png  # 16-bit master
```

Masters are extracted from the full-resolution source at the chosen time, never from candidates.

## Capture discipline

- Repeat viewpoints with saved waypoint missions. Third-party mission apps need the manufacturer's
  mobile SDK; DJI MSDK v5 lists Mini 3/3 Pro/4 Pro and enterprise models but not Air 3 or consumer
  Mavic 3 ([MSDK v5](https://github.com/dji-sdk/Mobile-SDK-Android-V5)) [V].
- Hover 5–10 s plus a still at each stop; same lens, zoom, resolution, profile and distortion
  correction every visit [I]; the same marked take-off pad (relative altitude depends on it).
- Similar time of day; warn when sun azimuth differs by more than about 15° [I].

## Colour

- Detect log/HLG profiles from SRT `color_md`, ffprobe `color_transfer` (HLG: `arib-std-b67`) or a
  per-visit override; D-Log M usually carries BT.709 tags, so it cannot be detected from metadata [K].
- Convert to Rec.709 before registration (`lut3d` with the vendor `.cube`; `zscale` + `tonemap` for
  HLG) [V options]. Run feature detection on CLAHE-equalized grayscale [I].

## Selection

Per vantage and visit [I]: filter candidates by distance, altitude and heading to the reference pose
(hover only); register the best few against the reference at low resolution inside a stable mask;
reject on low inliers, low overlap, a non-convex warped quad, extreme scale or an ill-conditioned
homography; score on inliers, overlap, sharpness and scale. Visits too different from the reference
(before construction vs after) chain through the nearest already-registered visit.

## Registration

```python
sift = cv2.SIFT_create(nfeatures=8000)
k1, d1 = sift.detectAndCompute(clahe(ref_gray), stable_mask)
k2, d2 = sift.detectAndCompute(clahe(mov_gray), None)
good = [a for a, b in cv2.BFMatcher(cv2.NORM_L2).knnMatch(d2, d1, k=2) if a.distance < 0.75 * b.distance]
cv2.setRNGSeed(1234)
H, inl = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, 2.0, maxIters=10000, confidence=0.9999)
# ECC refine at half size: ECC maps template → input, so pass inv(H_scaled) and invert the result
```

- MAGSAC gave identical results on repeated runs; BFMatcher is reproducible where FLANN may not be [V/I].
  ([OpenCV USAC](https://github.com/opencv/opencv/blob/5.x/doc/tutorials/calib3d/usac.markdown))
- **Parallax**: a homography is exact only for a plane. Shift ≈ f·b·h / (Z·(Z−h)); at 4K (f ≈ 2200 px),
  60 m altitude, 10 m object, 2 m position error ≈ 15 px. Mitigate with capture discipline, a
  ground-plane fit on stable areas, accepted ghosting on tall new structures, orthomosaics for true
  pixel alignment. **Never** dense optical-flow warping across visits [I].
- Mask water (reflections), seasonal trees, parking lots and the active work zone.
- Robust matchers for seasonal extremes (optional): LightGlue code (Apache-2.0) with DISK/ALIKED
  weights; XFeat (Apache-2.0); RoMa (MIT). **Avoid** SuperPoint/SuperGlue weights, MASt3R (CC BY-NC-SA)
  and research-only releases for client work [V licences].
- Store inliers, inlier ratio, RMSE, ECC, overlap and stable-area NCC per capture; review with a
  checkerboard, a stable-area difference and an A/B flicker [I]. Rough "ok": inliers ≥ 150, ratio ≥ 0.3,
  RMSE ≤ 1.5 px.

## Grading and stabilization

- Reinhard mean/std matching in LAB on the stable area: match lightness, shift chroma means, blend by
  `strength`; keep snow and leaf-off authentic [V prototype / I]. Histogram matching is too aggressive
  across seasons.
- Deflicker across visits with a rolling median of stable-area luminance; within a clip, ffmpeg
  `deflicker` [V].
- Gimbal footage rarely needs stabilization; `vidstab` (LGPL) for flyovers [V]. Per-frame registration
  already stabilizes the still time-lapse.

## Films

- Pacing about 1.2 s hold and 0.6–0.9 s crossfade per visit, longer on milestones; title, section and
  end cards drawn with the brand fonts in Pillow; camera moves with sub-pixel warps in Python (ffmpeg
  `zoompan` jitters) [I/K].
- Crossfades in approximately linear light avoid muddy midtones [I].

## Output encoding

| Asset | Recommendation |
|---|---|
| masters | JPEG q≈92, long edge 2560 (more for sharper portrait crops) |
| responsive stills | AVIF (iOS 16+), WebP, progressive JPEG fallback; Pillow 12 encodes AVIF [V] |
| H.264 | `-profile:v high -pix_fmt yuv420p -movflags +faststart -an` |
| HEVC | **add `-tag:v hvc1`** (ffmpeg defaults to `hev1`, which Apple players reject) [V] |
| AV1 | Safari decodes only with hardware support; always keep an H.264 fallback [V] |
| scrubbed video | all-intra or GOP 6–15, ≤ 1920 px (all-intra ≈ 2.2× the size of long-GOP) [V] |
| reproducible encodes | `-fflags +bitexact -flags:v +bitexact -map_metadata -1`, fixed threads [V] |

## Orthomosaics and 3D (later)

[OpenDroneMap](https://github.com/OpenDroneMap/ODM) (AGPL-3.0: run as a container, never link) makes
orthophotos and DSMs, and reads video plus SRT [V]. Co-register orthos across dates with
[AROSICS](https://github.com/GFZ/arosics) (Apache-2.0) [V]. Deep zoom: `vips dzsave` + OpenSeadragon.
Splats: gsplat or Brush (Apache-2.0) to train, SPZ to compress, Spark (MIT) to render; never the
non-commercial reference code [V].

## Claude in the pipeline

- Default model `claude-opus-5-5`; on it thinking cannot be disabled (control with `effort`), sampling
  parameters are rejected, forced `tool_choice` returns 400: use structured outputs and get
  determinism by caching on (model, prompt version, request, image hashes) [V].
- Vision limits: JPEG/PNG/GIF/WebP, ≤ 8000 px, ≤ 2000 px each above 20 images per request; tokens ≈
  ⌈w/28⌉·⌈h/28⌉, capped near 4,784 per image ([vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)) [V].
  Under $1 per flight at list prices; Batches halve it.
- Check for refusals before reading output; skip gracefully without credentials (the SDK also reads
  auth tokens and CLI profiles, so catch authentication errors rather than testing one variable).
- Uses: change descriptions between consecutive visits, hero tie-breaks, milestone candidates ("visible
  by <date>"), captions and alt text (≤ 150 characters), a second alignment opinion from a
  checkerboard. Output is `status: proposed`; a human accepts.

## Pitfalls checklist

AKAZE missing in OpenCV 5 · manual picks only in `work/` · legacy lon-first GPS · `longtitude` ·
radians bug · timezone-less SRT/EXIF and UTC midnight rollover · no SRT without subtitles · gimbal yaw
only in protobuf or stills · relative altitude depends on take-off · mixed colour profiles · mixed
lenses or crop modes · reflections and seasonal trees · parallax ghosting · one bad capture shrinking
the common crop · masters from candidates · `hev1` HEVC · AV1 without fallback · range-request formats
from `file://` · pixel-exact test goldens · sampling parameters on models that reject them.

## Licensing summary

Safe dependencies: OpenCV (Apache-2.0), PyAV and scikit-image (BSD-3), LightGlue/XFeat/RoMa/Kornia,
gsplat, Brush, Spark, SPZ, OpenSeadragon, AROSICS. External tools only: Ubuntu ffmpeg (GPL build),
vid.stab and libvips (LGPL), exiftool, OpenDroneMap (AGPL). Avoid for client work: SuperPoint/SuperGlue
weights, MASt3R/DUSt3R, the 3DGS reference code, research-only releases. Codec patents are a separate
question for commercial distribution.

## Sources

ExifTool [DJI.pm](https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/DJI.pm),
[QuickTimeStream.pl](https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/QuickTimeStream.pl),
[QuickTime.pm](https://github.com/exiftool/exiftool/blob/master/lib/Image/ExifTool/QuickTime.pm) ·
[DJI_SRT_Parser](https://github.com/JuanIrache/DJI_SRT_Parser) · [DJI MSDK v5](https://github.com/dji-sdk/Mobile-SDK-Android-V5) ·
[Gyroflow](https://github.com/gyroflow/gyroflow) · [OpenDroneMap](https://github.com/OpenDroneMap/ODM) ·
[OpenCV USAC](https://github.com/opencv/opencv/blob/5.x/doc/tutorials/calib3d/usac.markdown),
[OpenCV 5 features](https://github.com/opencv/opencv/blob/5.x/modules/features/include/opencv2/features.hpp),
[xfeatures2d](https://github.com/opencv/opencv_contrib/blob/5.x/modules/xfeatures2d/include/opencv2/xfeatures2d.hpp),
[opencv-python](https://github.com/opencv/opencv-python) · [LightGlue](https://github.com/cvg/LightGlue) ·
[SuperPoint licence](https://github.com/magicleap/SuperPointPretrainedNetwork/blob/master/LICENSE) ·
[LoFTR](https://github.com/zju3dv/LoFTR) · [RoMa](https://github.com/Parskatt/RoMa) · [MASt3R](https://github.com/naver/mast3r) ·
[XFeat](https://github.com/verlab/accelerated_features) · [Kornia](https://github.com/kornia/kornia) ·
[FILM](https://github.com/google-research/frame-interpolation) · [Practical-RIFE](https://github.com/hzwer/Practical-RIFE) ·
[vid.stab](https://github.com/georgmartius/vid.stab) · [AROSICS](https://github.com/GFZ/arosics) ·
[libvips pyramids](https://github.com/libvips/libvips/blob/master/doc/making-image-pyramids.md) ·
[PMTiles](https://github.com/protomaps/PMTiles) · [gsplat](https://github.com/nerfstudio-project/gsplat) ·
[3DGS licence](https://github.com/graphdeco-inria/gaussian-splatting/blob/main/LICENSE.md) ·
[Brush](https://github.com/ArthurBrussee/brush) · [Spark](https://github.com/sparkjsdev/spark) ·
[SPZ](https://github.com/nianticlabs/spz) · [ThumbHash](https://github.com/evanw/thumbhash) ·
[PyAV](https://github.com/PyAV-Org/PyAV) · [caniuse data](https://github.com/Fyrd/caniuse/tree/main/features-json) ·
[MDN video codecs](https://github.com/mdn/content/blob/main/files/en-us/web/media/guides/formats/video_codecs/index.md) ·
[Claude vision](https://platform.claude.com/docs/en/build-with-claude/vision)
