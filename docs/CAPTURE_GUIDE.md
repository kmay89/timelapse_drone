# Capture guide: how to fly so every visit lines up

*For the pilot. Goal: the same pictures from the same spots every time, so the story shows the site
changing, not the camera moving.* Disciplined repeat flights matter more than any feature of the
story. `/vantage-flight-plan` turns this guide plus a project's vantages into a one-page brief.

Replace `<SITE>` with a short uppercase code for the site (for example `LKSD` for the demo).

## A. Once, before the next flight

1. Tell us the drone model and app (DJI Fly or another), and whether the pilot holds the required
   remote-pilot certificate (in the US, FAA Part 107 for commissioned work).
2. **Turn on video subtitles/captions** in the flight app (DJI Fly: camera settings → video
   subtitles). This writes an `.SRT` file with GPS, altitude and camera data next to every video.
   Without it we lose the position data that finds each viewpoint.
3. **Pick one take-off spot and mark it** (paint, a survey nail, a described landmark) outside any
   construction fence. Altitude is measured from take-off, so moving the take-off spot shifts every
   shot.
4. **Save one waypoint mission named `<SITE>-STD-v1`** and re-fly it every time. Bump the version
   only when a stop changes, and tell us.
   - Built-in waypoints exist on recent DJI models (Mini 4 Pro, Air 3/3S, Mavic 3 class); confirm for
     your aircraft.
   - Third-party mission apps depend on the manufacturer's mobile SDK; some consumer models are not
     supported, so check before relying on one.

## B. The standard mission (about 15–18 minutes; one or two batteries)

A typical mission for a park or public space. Your project's brief lists its own stops with the
numbers from the first flight.

| Stop | Vantage | Position | Altitude above take-off | Camera tilt | Heading | Capture |
|---|---|---|---|---|---|---|
| 1 | `overview` | the widest view that shows the whole site and its context | 90 m | −30° | fixed | 10 s hover video + 1 photo |
| 2 | `nadir` | straight above the centre of the work area | 115 m (stay under the 400 ft / 121.9 m limit) | −90° | north-up | 10 s hover video + 1 photo |
| 3 | `entrance` | low oblique at the main entrance or signature feature | 30 m | −25° | fixed | 10 s hover video + 1 photo |
| 4 | `feature` | low, looking at a second key feature (waterfront, pavilion) | 15–20 m | −10° to −15° | fixed | 10 s hover video + 1 photo |
| 5 | `orbit` | a slow full circle around the site centre | 90 m | −35° | point of interest | one continuous 360° video |
| 6 | `rise` | behind the entrance, rising straight up | 5 m → 60 m | −15° | fixed | vertical (9:16) video if supported, else 4K landscape |

At each stop: **hover still for 10 seconds**, then take one photo. Do not zoom; a telephoto camera or a
different crop mode counts as a different viewpoint.

## C. Camera settings (identical every visit)

- **Video**: 4K (3840×2160) at 30 fps, colour profile **Normal** (if you prefer a log profile, use it
  every time and tell us). **White balance fixed at 5500 K**, not auto. Same lens every time.
- **Photos**: highest resolution, **JPEG + RAW (DNG)**, exposure compensation 0, the same
  distortion-correction setting every visit.
- **Do not edit, trim, rename or re-export files.** Originals only.

## D. When to fly

- **Monthly**, in the same week of each month, within **±60 minutes of the same local time** (mid
  morning is a good default: shadows short, light even).
- **Also on milestone days**: groundbreaking, first trees, major structures, opening day (wide and
  high only; see G).
- Skip rain, fog and winds above about 15 mph (25 km/h). A bright overcast day is fine, and good for
  the straight-down shot.

## E. Mapping flight (quarterly plus milestones; optional)

- Straight-down grid at 70–90 m with **80 % front and 70 % side overlap**; for 3D, a second pass at
  −60° over the same grid.
- If your aircraft cannot fly a mapping app, ask whether the contractor or survey team already flies
  mapping missions: their orthophotos (with permission) may be the best data available.
- Ground control: if possible, 5–8 permanent painted targets at fixed points, surveyed once.

## F. Two phone photos per visit

Stand on two marked spots (for example the main entrance and the end of a pier or path). Hold the
phone at eye height in landscape, frame the same view each time, and take one photo at each.

## G. Delivering files

- Folder name: `YYYY-MM-DD_<SITE>/`. Put in every `.MP4`, `.SRT`, `.JPG` and `.DNG` exactly as they
  came off the card.
- Upload to the shared folder you were given. **Do not** send through Photos, iMessage, WhatsApp or
  social apps: they compress files and strip the data we need.
- Add a `notes.txt`:
  - date and start time; pilot and aircraft; weather;
  - **what changed since last time** (these notes become the captions);
  - anything we should not show: people, licence plates, backyards, an unannounced feature.

## H. Safety, permissions and privacy

- Fly under the rules that apply where you are (in the US, Part 107 with a Remote ID–compliant
  aircraft). **Check airspace before every flight** (B4UFLY or LAANC in the US).
- Permissions: the site owner, and during construction the general contractor (take off outside the
  fence; follow site rules and the site's safety officer).
- **Do not fly over people**, especially at openings and events: shoot crowds from offset positions.
- Neighbours: do not linger over homes or private yards next to the site.

## Why each rule matters (for the story)

| Rule | Because |
|---|---|
| same take-off spot, waypoint mission | alignment works from matching features; a camera 10 m off adds parallax that no software removes |
| subtitles on (`.SRT`) | the pipeline finds each viewpoint in a 40-minute video by GPS, heading and altitude |
| hover 10 s | gives a sharp frame without motion blur and lets the gimbal settle |
| fixed white balance, Normal profile | visits match in colour without heavy grading, so seasons stay honest |
| same time of day | shadows that move look like change; the reader cannot tell the difference |
| originals only | messaging apps strip GPS and dates and recompress the image |
