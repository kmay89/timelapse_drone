"""The story-cut renderer: a pure frame(f) over a story FilmPlan (film/cut.py).

`_Cut(project, plan)` typesets every text block of the plan once (brand fonts, sizes in units of
short edge / 1080 × type_scale) and then evaluates each frame from the plan alone:

    keyed camera (film/beat.py) → warp of a two-level pyramid (L0 at the cover-fit scale s0, L1 at
    s0·zmax, blended by log z / log zmax so a push never pops between levels) → linear-light `_mix`
    dissolves → scrims behind each text block's ink → date label → words rising at sub-pixel offsets
    → rail and knob → tag ('Simulated imagery', 'Preview', and ' · plan <hash6>' in drafts).

The rail's ends are inset by its knob (8.5 units), so the knob never reaches past the text edges.
Every typeset layer's ink box (with its rise travel) is kept in `ink` for plan.json `checks`.

Curtains split two warps of the same camera at the rule (before left, after right) under a 3-unit
accent rule and grip; their labels are clipped to their own side and shown only inside the plan's
reveal intervals. A run of stats is set once (`stat_run`, shared with film/story.py): each figure on
a fixed digit advance, the figure and its unit sliding into a mask as one block, over a picture
under one panel in the run's tint whose opacity is measured so the text keeps the contrast WCAG 1.4.3
asks of its size (`panel_contrast`) over each stat's picture along the camera path; the panel rises
over the beat before the first figure lands and stays while a stat on another picture dissolves in
under it, and one stat's text fades out before the next figure lands (never two figures, and never a partial one, on screen). The tag's
and each date label's scrim are measured too, so their small type keeps 4.5:1 over every picture
(`scrims`, for plan.json `checks`). Cards,
plain stat cards and the end card dip to and from their surface in display gamma, like the classic
cut. Every image pixel is a crop, a dissolve, a curtain split or a dip of registered masters; text,
scrims, panels and the rail are overlays.

Loop films rise from pure night (frame 0 is a night fill) onto the title's image while the first
series' rail sweeps its knob back to tick 0 (the rail then stays through the title into the first
capture), and their closing dip eases to that same night fill on frame N, one past the last frame,
so a player looping the file shows no seam: frame(N) == frame(0), byte for byte.
"""

from __future__ import annotations

import bisect
import dataclasses
import datetime as dt
import math
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw

from vantage.config import Project
from vantage.film.beat import Window, keyed, lerp, mix_cam, ramp
from vantage.film.layout import Layout
from vantage.film.plan import ROLES, font_path
from vantage.film.render import _hex, _Layer, _mix, _Style, _Type, balance, ink_box
from vantage.film.safety import SURFACES
from vantage.models import FilmPlan

# Styles, in units (short edge / 1080 × type_scale).
KICKER = _Style("numeric", 38, 600, "accent", tracking=0.22, upper=True)
TITLE = _Style("display", 124, 400, leading=1.08)
DATE = _Style("numeric", 52, 600, tracking=0.08, upper=True)
LEAD = _Style("display", 120, 500)
TAG = _Style("numeric", 22, 600, tracking=0.16, upper=True)
CARD_TITLE = _Style("display", 96, 400, leading=1.08)
BRAND = _Style("display", 92, 400, leading=1.08)
CREDIT = _Style("text", 36, 400, alpha=0.9, leading=1.35)
ROLE = _Style("numeric", 22, 600, alpha=0.6, tracking=0.16, upper=True)
PARTNER = _Style("text", 34, 500)
LINK = _Style("text", 34, 500, "accent")
URL = _Style("text", 64, 500, "accent")  # a loop's end card: the short URL, read across a room
HONESTY = _Style("text", 38, 400, alpha=0.92, leading=1.35)
FINE = _Style("text", 27, 400, alpha=0.62, leading=1.4)
DISCLAIMER = _Style("text", 30, 400, alpha=0.9, leading=1.35)  # read, unlike the fine print
CURTAIN_LABEL = _Style("numeric", 44, 600, tracking=0.06, upper=True)
STAT_VALUE = _Style("numeric", 220, 500, "ink")
STAT_UNIT = _Style("text", 220 * 0.35, 500, "ink")
# A stat's label, its source and the honesty line riding under a stat run, in units, by the film's
# orientation: a portrait film is read on a phone in the hand, so its words are half as large again
# (a 9:16 label's x-height is 44 px at 1080 wide, its source's 30 px). A landscape film is read on a
# phone held sideways too, so its words are at least STAT_LANDSCAPE_PX px at a 1080 px short edge
# whatever its type_scale (a 16:9 label's x-height is 37 px, its source's 25); a loop's larger
# type_scale sets them larger still. All fully opaque: their contrast over the panel is measured,
# not left to an alpha.
STAT_TYPE = {"landscape": (52.0, 34.0, 30.0), "portrait": (86.0, 58.0, 38.0)}
STAT_LANDSCAPE_PX = (72.0, 48.0, 36.0)
STAT_LABEL = _Style("text", 52, 400, "ink", leading=1.22)
STAT_SOURCE = _Style("text", 34, 400, "ink")
RIDE = _Style("text", 30, 400, "ink", leading=1.3)
SMALL_CONTRAST = 4.5  # WCAG 1.4.3 for small text: the tag, the date labels and stat panels keep this
LARGE_CONTRAST = 3.0  # WCAG 1.4.3 for large text: a stat panel whose every text is large keeps this
# WCAG's large text (18 pt, 24 CSS px) in film px at a 1080 px short edge, the frame seen 393 pt
# wide on a phone (scaled with the short edge; type_scale makes text larger, not the threshold).
LARGE_TEXT = 24 * 1080 / 393
TAG_SCRIM, LABEL_SCRIM = 0.3, 0.42  # the least scrim behind the tag and a date label (raised as measured)
TITLE_SCRIM = 0.45  # the least scrim behind the title and its kicker (raised as the kicker needs)
PANEL_PAD, PANEL_SIGMA = 44.0, 16.0  # units: the panel's feather starts 2.75 sigma clear of the ink
# Units between a stat run's ink and the tag's: the panel's padding and feather (3 sigma) end 8 units
# clear of the tag, so a paper panel never lightens the picture under the tag's paper type.
TAG_CLEAR = PANEL_PAD + 3 * PANEL_SIGMA + 8
END_STYLES = {
    "brand": BRAND, "credit": CREDIT, "role": ROLE, "partner": PARTNER, "link": LINK, "url": URL,
    "honesty": HONESTY, "disclaimer": DISCLAIMER, "fine": FINE, "kicker": KICKER, "title": CARD_TITLE,
}  # fmt: skip
END_PORTRAIT = 1.35  # a portrait film's end card is read on a phone in the hand, like its stats
RISE = 14  # units a text block rises while it fades in
KNOB = 8.5  # units: the rail knob's radius with its ring (the rail's ends are inset by it)
LRU = 4  # masters kept decoded (a dissolve needs two)

Items = Sequence[tuple[float, float, str, _Style, str]]  # x, baseline y, text, style, align (l|r|c)


# --------------------------------------------------------------------------- #
# Stat runs: where a run of stats sets its text (shared with film/story.py, which frames its pushes
# clear of it)
# --------------------------------------------------------------------------- #


Box4 = tuple[float, float, float, float]  # x0, y0, x1, y1 in px


@dataclasses.dataclass(frozen=True)
class StatSet:
    """One stat's text as set in a run: styles, baselines (px) and its ink box."""

    value: _Style
    unit: _Style
    label: _Style
    source: _Style
    base: float  # the figure's baseline
    label_lines: tuple[str, ...]
    label_bases: tuple[float, ...]
    source_text: str | None
    source_base: float
    box: Box4


@dataclasses.dataclass(frozen=True)
class StatRun:
    """A run of stats in one place ('low': resting on the safe bottom, or above the tag in 9:16;
    'high': hanging from the label line, or under a top-right tag), with the honesty line under
    the stats when it rides along (`ride`). `box` holds every ink of the run, `blocks` the stats'."""

    place: str
    sets: tuple[StatSet, ...]
    ride: _Style
    ride_lines: tuple[str, ...]
    ride_bases: tuple[float, ...]
    box: Box4
    blocks: Box4


def stat_type(lay: Layout) -> tuple[float, float, float]:
    """(label, source, ride) sizes in units for a layout's orientation (STAT_TYPE, STAT_LANDSCAPE_PX)."""
    if lay.height > lay.width:
        return STAT_TYPE["portrait"]
    ts = lay.type_scale  # a size of S units is S × type_scale px at a 1080 px short edge
    floor = zip(STAT_TYPE["landscape"], STAT_LANDSCAPE_PX, strict=True)
    label, source, ride = (max(size, px / ts) for size, px in floor)
    return label, source, ride


def panel_contrast(lay: Layout, run: StatRun) -> float:
    """The contrast a stat run's panel keeps under its text (WCAG 1.4.3): LARGE_CONTRAST when every
    text on it (figures, units, labels, sources, the riding line) is large, at least LARGE_TEXT px
    at a 1080 px short edge, else SMALL_CONTRAST."""
    styles = [x for st in run.sets for x in (st.value, st.unit, st.label)]
    styles += [st.source for st in run.sets if st.source_text]
    if run.ride_lines:
        styles.append(run.ride)
    large = LARGE_TEXT * min(lay.width, lay.height) / 1080
    return LARGE_CONTRAST if all(s.size * lay.unit >= large for s in styles) else SMALL_CONTRAST


def stat_values(
    t: _Type, lay: Layout, value: str, unit: str | None, scale: float = 1.0
) -> tuple[_Style, _Style]:
    """The figure's and unit's styles, shrunk so figure + unit fit between the margins."""
    value_s = dataclasses.replace(STAT_VALUE, size=STAT_VALUE.size * scale)
    unit_s = dataclasses.replace(STAT_UNIT, size=STAT_UNIT.size * scale)
    total = sum(stat_advances(t, value_s, value))
    if unit:
        total += 0.18 * value_s.size * t.unit + t.width(unit, unit_s)
    if total > lay.text_width:
        k = lay.text_width / total * 0.98
        value_s = dataclasses.replace(value_s, size=value_s.size * k)
        unit_s = dataclasses.replace(unit_s, size=unit_s.size * k)
    return value_s, unit_s


def stat_advances(t: _Type, style: _Style, value: str) -> list[float]:
    """A fixed advance per digit (the widest digit's: tabular figures without raqm)."""
    font = t.font(style)
    digit = max(font.getlength(d) for d in "0123456789")
    return [digit if c.isdigit() else font.getlength(c) for c in value]


def _ink_y(t: _Type, style: _Style, text: str) -> tuple[float, float]:
    """(top, bottom) of a line's ink relative to its baseline."""
    box = t.font(style).getbbox(text.upper() if style.upper else text, anchor="ls")
    return float(box[1]), float(box[3])


def _cap_of(t: _Type, style: _Style) -> float:
    return -t.font(style).getbbox("H", anchor="ls")[1]


def stat_run(
    t: _Type,
    lay: Layout,
    items: Sequence[dict[str, Any]],
    honesty: str | None,
    place: str,
    tagged: bool,
) -> StatRun:
    """Where each stat of a run (items with value, unit, label, source) and the riding honesty line
    sit. Every stat's block is aligned on the run's edge ('low': their bottoms on the line above the
    honesty line; 'high': their figures' cap tops on one line), so one panel holds them all. Shrunk
    (all sizes together) until the run fits between the safe top and bottom."""
    u = t.unit
    size_l, size_s, size_r = stat_type(lay)
    top_y, bottom_y = float(lay.top), float(lay.bottom)
    tag_cap = _cap_of(t, TAG)
    if tagged and lay.tag == "rail-right":  # the 9:16 tag sits right above the rail: stay above it
        bottom_y = min(bottom_y, lay.rail_y - 18 * u - tag_cap - TAG_CLEAR * u)
    if tagged and lay.tag == "top-right":  # under the tag, the panel clear of it
        top_y = max(top_y, lay.top + tag_cap + TAG_CLEAR * u)
    elif place == "high":
        top_y = max(top_y, float(lay.label_top))
    scale = 1.0
    for _attempt in range(5):
        label_s = dataclasses.replace(STAT_LABEL, size=size_l * scale)
        source_s = dataclasses.replace(STAT_SOURCE, size=size_s * scale)
        ride_s = dataclasses.replace(RIDE, size=size_r * scale)
        rel: list[tuple[_Style, _Style, float, list[str], list[float], str | None, float, float, float]] = []
        for item in items:
            value_s, unit_s = stat_values(t, lay, item["value"], item.get("unit"), scale)
            cap_v = _cap_of(t, value_s)
            lines = t.wrap(item["label"], label_s, lay.text_width, balanced=True)
            step = label_s.size * u * label_s.leading
            first = 0.26 * value_s.size * u + _cap_of(t, label_s)
            bases = [first + k * step for k in range(len(lines))]
            bottom = bases[-1] + _ink_y(t, label_s, lines[-1])[1]
            source = f"Source: {item['source']}" if item.get("source") else None
            sbase = bases[-1]
            if source:
                sbase = bases[-1] + 0.8 * source_s.size * u + _cap_of(t, source_s)
                bottom = sbase + _ink_y(t, source_s, source)[1]
            rel.append((value_s, unit_s, cap_v, lines, bases, source, sbase, -cap_v, bottom))
        ride_lines = t.wrap(honesty, ride_s, lay.text_width, balanced=True) if honesty else []
        ride_step = ride_s.size * u * ride_s.leading
        ride_cap = _cap_of(t, ride_s)
        gap = max(30 * u, 1.1 * ride_s.size * u)
        ride_h = (
            (ride_cap + (len(ride_lines) - 1) * ride_step + _ink_y(t, ride_s, ride_lines[-1])[1])
            if ride_lines
            else 0.0
        )
        shifts: list[float] = []
        if place == "low":
            ride_top = bottom_y - ride_h
            edge = ride_top - gap if ride_lines else bottom_y
            shifts = [edge - r[8] for r in rel]
        else:
            shifts = [top_y + r[2] for r in rel]
            ride_top = max(s + r[8] for s, r in zip(shifts, rel, strict=True)) + gap
        tops = [s + r[7] for s, r in zip(shifts, rel, strict=True)]
        bottoms = [s + r[8] for s, r in zip(shifts, rel, strict=True)]
        run_top, run_bottom = min(tops), max(bottoms)
        if ride_lines:
            run_bottom = max(run_bottom, ride_top + ride_h)
        over = max(0.0, top_y - run_top) + max(0.0, run_bottom - bottom_y)
        if over <= 0.5 or _attempt == 4:
            break
        scale *= (run_bottom - run_top - over) / (run_bottom - run_top) * 0.98
    sets = []
    x0 = float(lay.margin)
    right = x0
    for (value_s, unit_s, _cap_v, lines, bases, source, sbase, rtop, rbottom), shift, item in zip(
        rel, shifts, items, strict=True
    ):
        widths = [t.width(line, label_s) for line in lines]
        fig = sum(stat_advances(t, value_s, item["value"]))
        if item.get("unit"):
            fig += 0.18 * value_s.size * u + t.width(item["unit"], unit_s)
        if source:
            widths.append(t.width(source, source_s))
        x1 = x0 + max([fig, *widths])
        right = max(right, x1)
        box = (x0, shift + rtop, x1, shift + rbottom)
        lines_at = tuple(b + shift for b in bases)
        sets.append(
            StatSet(
                value_s, unit_s, label_s, source_s, shift, tuple(lines), lines_at, source, sbase + shift, box
            )
        )
    ride_bases = tuple(ride_top + ride_cap + k * ride_step for k in range(len(ride_lines)))
    blocks = (x0, min(s.box[1] for s in sets), right, max(s.box[3] for s in sets))
    box = blocks
    if ride_lines:
        rw = max(t.width(line, ride_s) for line in ride_lines)
        box = (x0, min(blocks[1], ride_top), max(right, x0 + rw), max(blocks[3], ride_top + ride_h))
    return StatRun(place, tuple(sets), ride_s, tuple(ride_lines), ride_bases, box, blocks)


# --------------------------------------------------------------------------- #
# Step words: where a step's lead words sit (shared with film/story.py, which chooses the zone)
# --------------------------------------------------------------------------- #

# The zones a step's words may take, by the film's orientation; the first is the default. `low`: on
# the lead baseline from the left margin; `low-right`: on the lead baseline, the ink ending on the
# right text edge; `high`: from the left margin, the ink top WORDS_GAP units under the label line.
WORDS_ZONES = {"portrait": ("low", "high"), "landscape": ("low", "low-right", "high")}
WORDS_GAP = 44.0  # units between the label line (the date label's or curtain labels' baseline) and the words
WORDS_SCRIM = 0.5  # the words' scrim at its peak
WORDS_PAD = (26.0, 18.0)  # units: the words' scrim padding (x, y)


def words_zones(lay: Layout) -> tuple[str, ...]:
    """The zones a step's words may take in this layout (the first is the default)."""
    return WORDS_ZONES["portrait" if lay.height > lay.width else "landscape"]


@dataclasses.dataclass(frozen=True)
class WordsSet:
    """A step's words as set in a zone: style, the text's origin (x, the first line's baseline), its
    ink box and, for words on more than one line (a curtain's), each line and its baseline."""

    zone: str
    style: _Style
    x: float
    baseline: float
    box: Box4  # x0, y0, x1, y1 of the ink, at rest (it rises RISE units into place from below)
    lines: tuple[str, ...] = ()
    baselines: tuple[float, ...] = ()


WORDS_LEADING = 1.06  # a curtain's words on two lines: display type, set close


def words_lines(t: _Type, lay: Layout, words: str, scale: float) -> list[str]:
    """`words` at LEAD × scale broken into balanced lines between the margins."""
    return t.wrap(words, LEAD, lay.text_width, scale, balanced=True)


def words_scale(t: _Type, lay: Layout, texts: Sequence[str], lines: int | None = None) -> float:
    """The largest scale (at most 1) of LEAD at which each of `texts` breaks into at most `lines`
    balanced lines whose ink fits between the margins: one size for every line of a curtain. By
    default two lines in a portrait frame and one in a landscape one, whose wide lines would
    otherwise reach down over the middle of the picture, where a curtain's push puts its subject."""
    lines = lines or (2 if lay.height > lay.width else 1)
    scale = 1.0
    for _ in range(40):
        ok = True
        for text in texts:
            rows = words_lines(t, lay, text, scale)
            style = dataclasses.replace(LEAD, size=LEAD.size * scale)
            ink = max(t.font(style).getbbox(row, anchor="ls")[2] for row in rows)
            ok = ok and len(rows) <= lines and ink <= lay.text_width
        if ok:
            break
        scale *= 0.97
    return round(scale, 4)


def words_set(
    t: _Type, lay: Layout, words: str, zone: str, label_base: float, scale: float | None = None
) -> WordsSet:
    """Lead words in `zone` (shrunk to fit between the margins, overhangs included). `label_base`:
    the baseline of the label line a `high` zone hangs under. With `scale` (a curtain's one size,
    `words_scale`), the words are set at LEAD × scale on balanced lines instead: the last line on
    the low baselines, the first under the label line when high."""
    if scale is not None:
        return _words_lines(t, lay, words, zone, label_base, scale)
    size = 1.0
    w = t.width(words, LEAD)
    if w > lay.text_width:
        size = lay.text_width / w
    style = _Style(LEAD.role, LEAD.size * size, LEAD.weight)
    for _ in range(4):  # the ink (overhangs included) fits too, at the font's whole px size
        ink = t.font(style).getbbox(words, anchor="ls")[2]
        if ink <= lay.text_width:
            break
        style = _Style(LEAD.role, style.size * lay.text_width / ink * 0.995, LEAD.weight)
    font = t.font(style)
    left, top, right, bottom = font.getbbox(words, anchor="ls")
    x = float(lay.margin)
    if zone == "low-right":  # the ink, not the advance, ends on the right text edge
        x = lay.right - right
    if zone == "high":  # every high line on one baseline: the ascender top of 'Hdl' under the label
        baseline = label_base + WORDS_GAP * t.unit - font.getbbox("Hdl", anchor="ls")[1]
    elif zone in ("low", "low-right"):
        baseline = float(lay.lead_baseline)
    else:
        raise ValueError(f"unknown words zone {zone!r}")
    return WordsSet(zone, style, x, baseline, (x + left, baseline + top, x + right, baseline + bottom))


def _words_lines(t: _Type, lay: Layout, words: str, zone: str, label_base: float, scale: float) -> WordsSet:
    style = _Style(LEAD.role, LEAD.size * scale, LEAD.weight, leading=WORDS_LEADING)
    font = t.font(style)
    rows = words_lines(t, lay, words, scale)
    step = style.size * t.unit * style.leading
    boxes = [font.getbbox(row, anchor="ls") for row in rows]
    if zone == "high":
        first = label_base + WORDS_GAP * t.unit - font.getbbox("Hdl", anchor="ls")[1]
    elif zone in ("low", "low-right"):
        first = float(lay.lead_baseline) - step * (len(rows) - 1)
    else:
        raise ValueError(f"unknown words zone {zone!r}")
    bases = tuple(first + k * step for k in range(len(rows)))
    x = float(lay.margin)
    if zone == "low-right":
        x = lay.right - max(bx[2] for bx in boxes)
    box = (
        x + min(bx[0] for bx in boxes), bases[0] + boxes[0][1],
        x + max(bx[2] for bx in boxes), bases[-1] + boxes[-1][3],
    )  # fmt: skip
    return WordsSet(zone, style, x, first, box, tuple(rows), bases)


def panel_from(shot: dict[str, Any], fpb: int) -> int:
    """The frame a stat run's panel (and its riding line) starts to come in, from the run's first
    stat: a beat before its figure lands, so the panel rises with the picture rather than popping in,
    but not before its shot starts or its dissolve is half way (the image before it is leaving), and
    at least B/3 before the figure lands."""
    land = shot.get("land", shot["f0"])
    floor = shot["f0"]
    d = shot.get("dissolve")
    if d is not None:
        floor = max(floor, d["f0"] + (d["f1"] - d["f0"]) // 2)
    return min(land - max(1, round(fpb / 3)), max(land - fpb, floor))


def stat_subject_box(lay: Layout, run: StatRun) -> tuple[float, float, float, float]:
    """Where a stat's push may put its hotspot (u0, u1, v0, v1 as frame fractions): inside the safe
    zone and clear of the run's panel (above it when the run is low, below it when high)."""
    W, H, u = lay.width, lay.height, lay.unit
    pad = (PANEL_PAD + 24) * u
    u0, u1 = lay.margin / W + 0.01, lay.right / W - 0.01
    if run.place == "low":
        return u0, u1, lay.top / H + 0.01, (run.box[1] - pad) / H
    return u0, u1, (run.box[3] + pad) / H, lay.bottom / H - 0.01


def stat_panel_box(lay: Layout, run: StatRun) -> tuple[float, float, float, float]:
    """The run's panel at full opacity (its ink box padded by PANEL_PAD), as frame fractions."""
    pad = PANEL_PAD * lay.unit
    x0, y0, x1, y1 = run.box
    return (
        (x0 - pad) / lay.width, (y0 - pad) / lay.height, (x1 + pad) / lay.width, (y1 + pad) / lay.height
    )  # fmt: skip


def _bar(frame: np.ndarray, x0: float, y0: float, x1: float, y1: float, color: Sequence[int], opacity=1.0):
    """Anti-aliased, sub-pixel rectangle."""
    if x1 - x0 < 0.25 or opacity <= 0.002:
        return
    h, w = frame.shape[:2]
    r0, r1 = max(int(y0) - 1, 0), min(math.ceil(y1) + 1, h)
    c0, c1 = max(int(x0) - 1, 0), min(math.ceil(x1) + 1, w)
    if r1 <= r0 or c1 <= c0:
        return
    region = frame[r0:r1, c0:c1]
    ink = region.copy()
    pts = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]) - (c0, r0)
    cv2.fillConvexPoly(ink, np.round(pts * 16).astype(np.int32), tuple(color), cv2.LINE_AA, 4)
    region[:] = ink if opacity >= 0.998 else cv2.addWeighted(ink, opacity, region, 1 - opacity, 0)


def _disc(
    frame: np.ndarray, x: float, y: float, r: float, color: Sequence[int], ring: Sequence[int], rw: float
):
    h, w = frame.shape[:2]
    r0, r1 = max(int(y - r - rw) - 2, 0), min(math.ceil(y + r + rw) + 2, h)
    c0, c1 = max(int(x - r - rw) - 2, 0), min(math.ceil(x + r + rw) + 2, w)
    if r1 <= r0 or c1 <= c0:
        return
    region = frame[r0:r1, c0:c1]
    ctr = (round((x - c0) * 16), round((y - r0) * 16))
    cv2.circle(region, ctr, round((r + rw) * 16), tuple(ring), -1, cv2.LINE_AA, 4)
    cv2.circle(region, ctr, round(r * 16), tuple(color), -1, cv2.LINE_AA, 4)


def _put(
    frame: np.ndarray,
    layer: _Layer | None,
    opacity: float,
    dy: float = 0.0,
    clip: tuple[int, int, int, int] | None = None,
) -> None:
    """Composite a layer at a (sub-pixel) vertical offset, clipped to the frame (and to `clip`,
    x0, y0, x1, y1, when given)."""
    if layer is None or opacity <= 0.002:
        return
    rgb, alpha, y = layer.rgb, layer.alpha, float(layer.y) + dy
    iy = math.floor(y)
    fy = y - iy
    if fy > 1e-4:  # bilinear shift of the premultiplied patch by fy px down
        h, w = alpha.shape[:2]
        m = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, fy]])
        pad = ((0, 1), (0, 0), (0, 0))
        rgb = cv2.warpAffine(np.pad(rgb, pad), m, (w, h + 1), flags=cv2.INTER_LINEAR)
        alpha = cv2.warpAffine(np.pad(alpha, pad), m, (w, h + 1), flags=cv2.INTER_LINEAR)[..., None]
    h, w = alpha.shape[:2]
    fh, fw = frame.shape[:2]
    cx0, cy0, cx1, cy1 = clip if clip is not None else (0, 0, fw, fh)
    y0, y1 = max(iy, 0, cy0), min(iy + h, fh, cy1)
    x0, x1 = max(layer.x, 0, cx0), min(layer.x + w, fw, cx1)
    if y1 <= y0 or x1 <= x0:
        return
    a = alpha[y0 - iy : y1 - iy, x0 - layer.x : x1 - layer.x]
    c = rgb[y0 - iy : y1 - iy, x0 - layer.x : x1 - layer.x]
    region = frame[y0:y1, x0:x1]
    region[:] = region * (1 - a * opacity) + c * opacity + 0.5


class _Cut:
    """One story cut evaluated from its plan: typeset once, then a pure frame(f)."""

    def __init__(self, project: Project, plan: FilmPlan):
        p = plan.plan
        if p["cut"] != "story":
            raise ValueError(f"film {plan.id}: _Cut renders story plans, not {p['cut']!r}")
        self.project, self.plan, self.p = project, plan, p
        self.width, self.height, self.fps, self.frames = p["width"], p["height"], p["fps"], p["frames"]
        self.fpb, self.d = p["grid"]["fpb"], p["grid"]["dissolve"]
        self.third = max(1, round(self.fpb / 3))
        self.lay = Layout.from_record(self.width, self.height, p["layout"])
        self.u = u = self.lay.unit
        self.rail_ends = (self.lay.margin + KNOB * u, self.lay.right - KNOB * u)
        self.ink: list[dict[str, Any]] = []
        inputs = p["inputs"]
        files = {role: font_path(inputs["fonts"][role]["file"], project.brand) for role in ROLES}
        colors = inputs["colors"]
        self.type = _Type(files, colors, u)
        self.night = np.array(_hex(colors["night"])[::-1], np.float32)
        self.night_bgr = tuple(int(v) for v in self.night)
        self.night_fill = np.full((self.height, self.width, 3), self.night_bgr, np.uint8)  # loops: frame 0
        self.paper = _hex(colors["paper"])[::-1]
        self.accent = _hex(colors["accent"])[::-1]
        yy, xx = np.mgrid[0 : self.height, 0 : self.width].astype(np.float32)
        glow = np.exp(
            -(
                ((xx - self.width / 2) / (0.6 * self.width)) ** 2
                + ((yy - self.height / 2) / (0.6 * self.height)) ** 2
            )
        )
        paper = np.array(self.paper, np.float32)
        self.card_bg = (self.night + (paper - self.night) * 0.05 * glow[..., None] + 0.5).astype(np.uint8)
        self.surfaces = {
            "night": self.card_bg,
            "paper": np.ascontiguousarray(
                np.broadcast_to(np.array(self.paper, np.uint8), self.card_bg.shape)
            ),
        }
        self.shots: list[dict[str, Any]] = p["shots"]
        self.starts = [s["f0"] for s in self.shots]
        cams = p["camera"]
        self.windows = [Window(c["a"], c["b"]) for c in cams]
        self.cams = [(c["x"], c["y"], c["zoom"]) for c in cams]
        self.zmax = max([1.0, *(c["zoom"] for c in cams)])
        self.masters = inputs["masters"]
        self._levels: dict[str, tuple[np.ndarray, np.ndarray | None]] = {}
        self.tag_scrim: _Layer | None = None  # its unit mask: drawn at the measured `levels["tag"]`
        self.levels: dict[str, float] = {}  # measured scrim opacities: "tag", "<shot id> label", …
        self.measured: list[dict[str, Any]] = []  # their records, for plan.json checks
        self._bare_text = False  # measuring: frames without the tag and labels being measured
        words = list(p["tag"])
        if p.get("draft"):
            words.append(f"plan {plan.hash[:6]}")
        self.tag = self._tag(words)
        # A loop plays unattended with no end-card disclaimer: its tag stays on cards, stats and the
        # end card too, in ink on paper (no scrim) and as usual on night.
        self.tag_on_paper: _Layer | None = None
        if p.get("loop") and self.tag is not None:
            self.tag_on_paper = self._tag(words, dataclasses.replace(TAG, color="ink"), scrim=False)
        self._typeset()
        # A loop's rise shows the rail of the series that follows its title (kept through the title).
        nxt = self.shots[2] if len(self.shots) > 2 and self.shots[0]["kind"] == "rise" else None
        self.rise_series: int | None = nxt["series"] if nxt is not None and nxt["kind"] == "capture" else None

    # ------------------------------------------------------------- type --- #

    def _cap(self, style: _Style) -> float:
        return -self.type.font(style).getbbox("H", anchor="ls")[1]

    def _note(self, sid: str | None, layer: str, bbox: Sequence[float], rise: float = 0.0) -> None:
        """Record an ink box (x0, y0, x1, y1), grown `rise` px down for text that rises into place."""
        self.ink.append(
            {"shot": sid, "layer": layer, "box": ink_box(bbox[0], bbox[1], bbox[2], bbox[3] + rise)}
        )

    def _ink(
        self,
        items: Items,
        *,
        shadow: float = 0.6,
        blur: float = 4.0,
        note: tuple[str | None, str] | None = None,
        rise: float = 0.0,
    ) -> tuple[_Layer, tuple[int, ...]]:
        """A layer of text items (x, baseline, text, style, align) and its ink box (recorded under
        `note` = (shot id, layer name) with its rise travel)."""
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        for x, y, text, style, align in items:
            w = self.type.width(text, style)
            x = x - w if align == "r" else x - w / 2 if align == "c" else x
            self.type.draw(draw, x, y, text, style)
        bbox = canvas.getbbox() or (0, 0, 1, 1)
        if note is not None and canvas.getbbox():
            self._note(note[0], note[1], bbox, rise)
        return _Layer.of(canvas, shadow=shadow, blur=blur * self.u), bbox

    def _scrim(
        self,
        bbox: Sequence[float],
        alpha: float,
        rise: float = 0.0,
        *,
        color: np.ndarray | None = None,
        pad: tuple[float, float] = (26.0, 18.0),
        sigma: float = 14.0,
    ) -> _Layer:
        """A feathered box (night, or `color`) behind an ink box (grown by `rise` px downward for
        rising text); `pad` and `sigma` in units."""
        u = self.u
        sigma = sigma * u
        pad_x, pad_y = pad[0] * u, pad[1] * u
        x0, y0 = bbox[0] - pad_x, bbox[1] - pad_y
        x1, y1 = bbox[2] + pad_x, bbox[3] + pad_y + rise
        edge = math.ceil(3 * sigma)
        X0, Y0 = max(math.floor(x0) - edge, 0), max(math.floor(y0) - edge, 0)
        X1, Y1 = min(math.ceil(x1) + edge, self.width), min(math.ceil(y1) + edge, self.height)
        mask = np.zeros((Y1 - Y0, X1 - X0), np.float32)
        mask[round(y0) - Y0 : round(y1) - Y0, round(x0) - X0 : round(x1) - X0] = 1.0
        mask = cv2.GaussianBlur(mask, (0, 0), sigma) * alpha
        a = np.ascontiguousarray(mask[..., None])
        tint = self.night if color is None else color
        return _Layer(X0, Y0, np.ascontiguousarray(tint * a), a)

    def _tag(self, words: Sequence[str], tag: _Style = TAG, *, scrim: bool = True) -> _Layer | None:
        if not words:
            return None
        lay, text = self.lay, " · ".join(words)
        style = tag
        if (w := self.type.width(text, tag)) > lay.text_width:  # shrink to fit between the margins
            style = dataclasses.replace(tag, size=tag.size * lay.text_width / w * 0.98)
        top = lay.tag == "top-right"
        base = lay.top + self._cap(style) if top else lay.rail_y - 18 * self.u
        if not scrim:  # the paper variant: same place and box as the tag, recorded once
            return self._ink([(lay.right, base, text, style, "r")], shadow=0.0)[0]
        layer, box = self._ink([(lay.right, base, text, style, "r")], shadow=0.6, blur=3, note=(None, "tag"))
        self.tag_scrim = self._scrim(box, 1.0)
        return layer

    def _put_tag(self, frame: np.ndarray) -> None:
        if self.tag is not None and not self._bare_text:
            _put(frame, self.tag_scrim, self._tag_level())
            _put(frame, self.tag, 1.0)

    # ------------------------------------------------------- contrast --- #

    def _least_scrim(
        self, layer: _Layer, scrim: _Layer, backgrounds: Sequence[np.ndarray], floor: float
    ) -> tuple[float, float]:
        """(alpha, contrast): the least opacity (floor to 0.95) of a night `scrim` (a unit mask) at
        which a text `layer` keeps SMALL_CONTRAST in every one of `backgrounds` (frames without that
        text), and the worst contrast it gives. Contrast as a reader meets it: the text composited
        over the scrimmed picture, the median luminance of its glyph cores against the 95th-percentile
        brightest pixel 1 to 4 px around the glyphs (where small type's edges are read)."""
        from vantage.film.safety import _BGR_WEIGHTS, _SRGB_TO_LINEAR

        weights = _BGR_WEIGHTS.astype(np.float64)
        lh, lw = layer.alpha.shape[:2]
        y0, x0 = max(layer.y, 0), max(layer.x, 0)
        y1, x1 = min(layer.y + lh, self.height), min(layer.x + lw, self.width)
        a_text = layer.alpha[y0 - layer.y : y1 - layer.y, x0 - layer.x : x1 - layer.x]
        rgb = layer.rgb[y0 - layer.y : y1 - layer.y, x0 - layer.x : x1 - layer.x]
        m = np.zeros((y1 - y0, x1 - x0, 1), np.float32)  # the scrim's unit mask over the text's box
        sy0, sx0 = max(y0, scrim.y), max(x0, scrim.x)
        sy1, sx1 = min(y1, scrim.y + scrim.alpha.shape[0]), min(x1, scrim.x + scrim.alpha.shape[1])
        if sy1 > sy0 and sx1 > sx0:
            m[sy0 - y0 : sy1 - y0, sx0 - x0 : sx1 - x0] = scrim.alpha[
                sy0 - scrim.y : sy1 - scrim.y, sx0 - scrim.x : sx1 - scrim.x
            ]
        ink = a_text[..., 0]  # coverage, its shadow's halo included
        core = ink >= 0.9 * max(float(ink.max()), 1e-6)
        near = (ink > 0.02).astype(np.uint8)
        ring = (
            cv2.dilate(near, np.ones((9, 9), np.uint8)) & (1 - cv2.dilate(near, np.ones((3, 3), np.uint8)))
        ) > 0
        crops = [bg[y0:y1, x0:x1].astype(np.float32) for bg in backgrounds]

        def ratio(alpha: float) -> float:
            worst = 21.0
            for bg in crops:
                mixed = bg * (1 - alpha * m) + self.night * (alpha * m)
                out = np.clip(mixed * (1 - a_text) + rgb + 0.5, 0, 255).astype(np.uint8)
                lum = _SRGB_TO_LINEAR[out] @ weights
                lt, lb = float(np.median(lum[core])), float(np.quantile(lum[ring], 0.95))
                worst = min(worst, (max(lt, lb) + 0.05) / (min(lt, lb) + 0.05))
            return worst

        lo, hi = round(floor * 100), 95
        if not crops or not core.any() or not ring.any() or ratio(lo / 100) >= SMALL_CONTRAST:
            return lo / 100, ratio(lo / 100) if crops and core.any() and ring.any() else 21.0
        if ratio(hi / 100) < SMALL_CONTRAST:
            return hi / 100, ratio(hi / 100)
        while hi - lo > 1:  # the contrast only grows with the scrim
            mid = (lo + hi) // 2
            if ratio(mid / 100) >= SMALL_CONTRAST:
                hi = mid
            else:
                lo = mid
        return hi / 100, ratio(hi / 100)

    def _sampled(self, f0: int, f1: int) -> list[int]:
        """The frames a scrim is measured on: every half beat from f0, and f1 − 1."""
        return sorted({*range(f0, f1, max(1, self.fpb // 2)), f1 - 1}) if f1 > f0 else []

    def _tag_level(self) -> float:
        """The tag's scrim opacity: measured once over every half beat of the film (frames rendered
        without the tag: pictures, dissolves, panels and all), so its small type keeps 4.5:1."""
        if "tag" not in self.levels:
            assert self.tag is not None and self.tag_scrim is not None
            self._bare_text = True
            try:
                frames = [self.frame_at(f) for f in self._sampled(0, self.frames)]
            finally:
                self._bare_text = False
            alpha, contrast = self._least_scrim(self.tag, self.tag_scrim, frames, TAG_SCRIM)
            self.levels["tag"] = alpha
            self.measured.append({"text": "tag", "alpha": alpha, "contrast": round(contrast, 2)})
        return self.levels["tag"]

    def _label_level(self, i: int) -> float:
        """A capture's date label scrim: measured over its own picture (every half beat of the shot,
        along the camera) so the label keeps 4.5:1."""
        shot = self.shots[i]
        key = f"{shot['id']} label"
        if key not in self.levels:
            L = self.layers[shot["id"]]
            frames = [self._bare(i, f) for f in self._sampled(shot["f0"], shot["f1"])]
            alpha, contrast = self._least_scrim(L["label"], L["label_scrim"], frames, LABEL_SCRIM)
            self.levels[key] = alpha
            self.measured.append({"text": key, "alpha": alpha, "contrast": round(contrast, 2)})
        return self.levels[key]

    def _title_level(self, i: int) -> float:
        """A title's scrim: TITLE_SCRIM, raised over its own picture (every half beat while its text
        is up, under the title's wash) until its kicker, small accent type, keeps 4.5:1."""
        shot = self.shots[i]
        key = f"{shot['id']} kicker"
        if key not in self.levels:
            L = self.layers[shot["id"]]
            if not L["kicker"] or L["scrim"] is None:
                return TITLE_SCRIM
            frames = []
            for f in self._sampled(shot["f0"], max(shot["f0"] + 1, shot["f1"] - self.fpb)):
                frame = self._warp(shot["file"], self.camera(f))
                frames.append(cv2.addWeighted(frame, 0.7, self.card_bg, 0.3, 0))  # `_title`'s wash
            alpha, contrast = self._least_scrim(L["kicker"][-1], L["scrim"], frames, TITLE_SCRIM)
            self.levels[key] = alpha
            self.measured.append({"text": key, "alpha": alpha, "contrast": round(contrast, 2)})
        return self.levels[key]

    @property
    def scrims(self) -> list[dict[str, Any]]:
        """Every measured scrim, for plan.json checks: the stat panels, then the tag's, each title
        kicker's and each date label's (measured here if no frame has needed them yet)."""
        if self.tag is not None and self.tag_scrim is not None:
            self._tag_level()
        for i, shot in enumerate(self.shots):
            if shot["kind"] == "capture":
                self._label_level(i)
            elif shot["kind"] == "title":
                self._title_level(i)
        order = {
            "tag": -1,
            **{f"{s['id']} {what}": k for k, s in enumerate(self.shots) for what in ("label", "kicker")},
        }
        return [*self.panels, *sorted(self.measured, key=lambda r: order.get(r["text"], 0))]

    def _surface_tag(self, frame: np.ndarray, surface: str) -> None:
        """A loop film's tag on a card, stat or end card (night or paper); other films carry their
        disclaimer on the end card instead."""
        if surface == "paper" and self.tag_on_paper is not None:
            _put(frame, self.tag_on_paper, 1.0)
        elif surface != "paper" and self.tag_on_paper is not None:
            self._put_tag(frame)

    def _wrap_groups(
        self, groups: Sequence[str], style: _Style, width: float
    ) -> tuple[float, list[list[int]]]:
        """Shrink the title until every group fits; flow groups onto lines. (scale, lines of group idx)."""
        t = self.type
        scale = 1.0
        widest = max(t.width(g, style) for g in groups)
        if widest > width:
            scale = width / widest * 0.98
        space = t.width(" ", style, scale)
        lines: list[list[int]] = [[]]
        x = 0.0
        for g, text in enumerate(groups):
            w = t.width(text, style, scale)
            if lines[-1] and x + space + w > width:
                lines.append([])
                x = 0.0
            x += (space if lines[-1] else 0) + w
            lines[-1].append(g)
        if len(lines) > 1:  # the same number of lines, evened out: never a lone short last group
            rows = balance(
                groups, len(lines), lambda a, b: t.width(" ".join(groups[a:b]), style, scale), width
            )
            if rows:
                lines = [list(range(a, b)) for a, b in rows]
        return scale, lines

    def _typeset(self) -> None:
        lay, u = self.lay, self.u
        self.layers: dict[str, Any] = {}
        self.curtain_words: dict[str, dict[int, int]] = {}
        self.curtain_keys: dict[str, tuple[list[Window], list[float]]] = {}
        self.stat_mask: dict[str, tuple[int, int]] = {}
        self.stat_runs: dict[str, dict[str, Any]] = {}  # shot id → its run's panel and honesty line
        self.panels: list[dict[str, Any]] = []  # measured stat panels, for plan.json checks
        for i, shot in enumerate(self.shots):
            sid, kind = shot["id"], shot["kind"]
            text = {item["role"]: item["text"] for item in shot["text"]}
            if kind == "title":
                self._typeset_title(shot, text)
            elif kind == "capture":
                cap_top = lay.label_top + self._cap(DATE)
                label, box = self._ink([(lay.margin, cap_top, shot["label"], DATE, "l")], note=(sid, "label"))
                entry: dict[str, Any] = {"label": label, "label_scrim": self._scrim(box, 1.0)}
                words = text.get("words")
                if words:
                    zone = (shot.get("step") or {}).get("words_zone")
                    entry["words"], entry["words_scrim"] = self._words(words, sid, zone, cap_top)
                self.layers[sid] = entry
            elif kind in ("card", "end"):
                self.layers[sid] = {"stack": self._stack(shot["text"], sid)}
            elif kind == "curtain":
                self._typeset_curtain(shot)
            elif kind == "stat" and (i == 0 or self.shots[i - 1]["kind"] != "stat"):
                run = [shot]
                for nxt in self.shots[i + 1 :]:
                    if nxt["kind"] != "stat":
                        break
                    run.append(nxt)
                self._typeset_stats(run)
        # Rails: one per series, ticks at the real dates.
        self.rails: dict[int, list[int]] = {}
        for shot in self.shots:
            if shot["kind"] == "capture":
                self.rails.setdefault(shot["series"], []).append(
                    dt.date.fromisoformat(shot["capture"]).toordinal()
                )
        self.rail_layers = {n: self._rail_layer(days) for n, days in self.rails.items()}
        x0, x1, y = lay.margin, lay.right, lay.rail_y
        self.rail_scrim = self._scrim((x0, y - 8 * u, x1, y + 8 * u), 0.32)
        if self.rails:  # track, ticks and the knob at either end
            k = KNOB * u
            self._note(None, "rail", (self.rail_ends[0] - k, y - k, self.rail_ends[1] + k, y + k))

    def _words(
        self, words: str, sid: str | None, zone: str | None, label_base: float, scale: float | None = None
    ) -> tuple[_Layer, _Layer]:
        """A step's words in their zone (`words_set`; plans before zones: low) and their scrim;
        with `scale`, a curtain's words at its one size on balanced lines."""
        u = self.u
        ws = words_set(self.type, self.lay, words, zone or "low", label_base, scale)
        items = (
            [(ws.x, y, row, ws.style, "l") for row, y in zip(ws.lines, ws.baselines, strict=True)]
            if ws.lines
            else [(ws.x, ws.baseline, words, ws.style, "l")]
        )
        layer, box = self._ink(
            items,
            shadow=0.8,
            blur=7,
            note=(sid, "words"),
            rise=RISE * u,
        )
        return layer, self._scrim(box, WORDS_SCRIM, RISE * u, pad=WORDS_PAD)

    def _typeset_curtain(self, shot: dict[str, Any]) -> None:
        lay = self.lay
        style = dataclasses.replace(CURTAIN_LABEL, size=CURTAIN_LABEL.size * shot["label_scale"])
        cap_top = shot["label_top"] + self._cap(style)
        before, after = shot["labels"]
        labels, scrims = [], []
        sid = shot["id"]
        for x, text, align, side in ((lay.margin, before, "l", "before"), (lay.right, after, "r", "after")):
            layer, box = self._ink([(x, cap_top, text, style, align)], note=(sid, side))
            labels.append(layer)
            scrims.append(self._scrim(box, 0.42))
        words, word_scrims, index = [], [], {}
        for k, step in enumerate(shot["steps"]):
            if step.get("words"):
                layer, scrim = self._words(
                    step["words"], sid, step.get("words_zone"), cap_top, shot.get("words_scale")
                )
                index[k] = len(words)
                words.append(layer)
                word_scrims.append(scrim)
        self.layers[shot["id"]] = {
            "labels": labels,
            "label_scrims": scrims,
            "words": words,
            "words_scrims": word_scrims,
        }
        self.curtain_words[shot["id"]] = index
        self.curtain_keys[shot["id"]] = (
            [Window(k["a"], k["b"]) for k in shot["keys"]],
            [k["split"] for k in shot["keys"]],
        )

    def _typeset_stats(self, run: list[dict[str, Any]]) -> None:
        """A run of stats (film/story.py places it: `place`): each figure on a fixed advance per
        digit, one layer per glyph for the slide into its mask, the unit after it, label and source
        below; the riding honesty line under the run; and, over a picture, one panel in the run's tint
        behind all of it, its opacity measured so every text keeps the contrast its size asks
        (`panel_contrast`) over the picture."""
        lay, u, t = self.lay, self.u, self.type
        first = run[0]
        surface = first["surface"]
        color = "ink" if surface == "paper" else "paper"
        tagged = bool(self.p.get("tag"))
        honesty = first.get("honesty")
        geo = stat_run(t, lay, run, honesty, first.get("place", "low"), tagged)
        for shot, st in zip(run, geo.sets, strict=True):
            sid, value, unit = shot["id"], shot["value"], shot.get("unit")
            value_s, unit_s, label_s, source_s = (
                dataclasses.replace(x, color=color) for x in (st.value, st.unit, st.label, st.source)
            )
            base = st.base
            # Digits sit centred on a tabular advance; the first glyph's ink starts on the text column.
            adv = stat_advances(t, value_s, value)
            lsb = t.font(value_s).getbbox(value[0], anchor="ls")[0] if value.strip() else 0
            x = float(lay.margin) - (adv[0] - t.width(value[0], value_s)) / 2 - lsb
            glyphs = []
            ink_bottom = base
            for c, a in zip(value, adv, strict=True):
                if not c.isspace():
                    w = t.width(c, value_s)
                    layer, box = self._ink(
                        [(x + (a - w) / 2, base, c, value_s, "l")], shadow=0.0, note=(sid, f"value {c}")
                    )  # its slide stays inside the mask, so the box is where it lands
                    glyphs.append(layer)
                    ink_bottom = max(ink_bottom, box[3])
                x += a
            rest = []
            if unit:  # it slides in with the figure, one block: never a figure without its unit
                layer, box = self._ink(
                    [(x + 0.18 * value_s.size * u, base, unit, unit_s, "l")], shadow=0.0, note=(sid, "unit")
                )
                glyphs.append(layer)
                ink_bottom = max(ink_bottom, box[3])
            for line, y in zip(st.label_lines, st.label_bases, strict=True):
                layer, _ = self._ink([(lay.margin, y, line, label_s, "l")], shadow=0.0, note=(sid, "label"))
                rest.append(layer)
            if st.source_text:
                layer, _ = self._ink(
                    [(lay.margin, st.source_base, st.source_text, source_s, "l")], shadow=0.0,
                    note=(sid, "source"),
                )  # fmt: skip
                rest.append(layer)
            # The slide is clipped at the figure's own ink (its baseline, or a comma's tail), never
            # lower, so rising glyphs never show in the band above the label.
            top = math.floor(base - self._cap(value_s) - 0.12 * value_s.size * u)
            self.layers[sid] = {"glyphs": glyphs, "rest": rest}
            self.stat_mask[sid] = (top, math.ceil(ink_bottom) + 1)
        ride = None
        if geo.ride_lines:
            ride_s = dataclasses.replace(geo.ride, color=color)
            items = [
                (lay.margin, y, line, ride_s, "l")
                for line, y in zip(geo.ride_lines, geo.ride_bases, strict=True)
            ]
            ride, _ = self._ink(items, shadow=0.0, note=(first["id"], "honesty"))
        panel = None
        if first.get("file"):
            tint = np.array(self.paper if surface == "paper" else self.night, np.float32)
            target = panel_contrast(lay, geo)
            alpha, contrast = self._measure_panel(run, geo.box, tint, color, target)
            panel = self._scrim(geo.box, alpha, color=tint, pad=(PANEL_PAD, PANEL_PAD), sigma=PANEL_SIGMA)
            self.panels.append(
                {"shots": [s["id"] for s in run], "tint": surface, "alpha": round(alpha, 3),
                 "contrast": round(contrast, 2), "target": target, "box": ink_box(*geo.box)}
            )  # fmt: skip
        entry = {"panel": panel, "ride": ride, "first": first["id"]}
        for shot in run:
            self.stat_runs[shot["id"]] = entry

    def _measure_panel(
        self, run: Sequence[dict[str, Any]], box: Sequence[float], tint: np.ndarray, color: str, target: float
    ) -> tuple[float, float]:
        """(alpha, contrast): the least panel opacity (0.30 to 0.95) at which the text colour keeps
        `target` over the 99.5th-worst percentile of the picture under the run's ink, sampled on
        every beat of each stat over its own picture along the camera path (the luminance of what
        the panel leaves showing, in display gamma as `_put` mixes it); and the contrast it gives."""
        from vantage.film.safety import _BGR_WEIGHTS, _SRGB_TO_LINEAR

        x0, y0 = max(0, math.floor(box[0])), max(0, math.floor(box[1]))
        x1, y1 = min(self.width, math.ceil(box[2])), min(self.height, math.ceil(box[3]))
        samples = [
            self._warp(shot["file"], self.camera(f))[y0:y1:2, x0:x1:2].reshape(-1, 3)
            for shot in run
            for f in [*range(shot["f0"], shot["f1"], self.fpb), shot["f1"] - 1]
        ]
        px = np.concatenate(samples).astype(np.float32)
        weights = _BGR_WEIGHTS.astype(np.float64)
        text = np.array(_hex(self.p["inputs"]["colors"][color])[::-1], np.uint8)
        lt = float(_SRGB_TO_LINEAR[text] @ weights)
        dark_text = color == "ink"

        def ratio(alpha: float) -> float:
            mixed = np.clip(px * (1 - alpha) + tint * alpha + 0.5, 0, 255).astype(np.uint8)
            lum = _SRGB_TO_LINEAR[mixed] @ weights
            worst = float(np.quantile(lum, 0.005 if dark_text else 0.995))
            hi, lo = max(lt, worst), min(lt, worst)
            return (hi + 0.05) / (lo + 0.05)

        for k in range(30, 96):
            if (c := ratio(k / 100)) >= target:
                return k / 100, c
        return 0.95, ratio(0.95)

    def _typeset_title(self, shot: dict[str, Any], text: dict[str, str]) -> None:
        lay, u, t = self.lay, self.u, self.type
        B = self.fpb
        kicker = text.get("kicker")
        kick_layers = []
        boxes: list[Sequence[int]] = []
        base_k = lay.label_top + self._cap(KICKER)
        if kicker:
            for k in range(B):
                track = 0.10 + 0.12 * ramp(k, 0, B)
                style = _Style(
                    KICKER.role, KICKER.size, KICKER.weight, KICKER.color, tracking=track, upper=True
                )
                layer, box = self._ink([(lay.margin, base_k, kicker, style, "l")], blur=3)
                kick_layers.append(layer)
            boxes.append(box)  # the widest tracking, where it ends
            self._note(shot["id"], "kicker", box)
        words = (text.get("title") or "").split()
        n = len(words)
        sizes = [n // 3 + (1 if g < n % 3 else 0) for g in range(min(3, n))]
        groups, k = [], 0
        for size in sizes:
            groups.append(" ".join(words[k : k + size]))
            k += size
        group_layers = []
        if groups:
            scale, lines = self._wrap_groups(groups, TITLE, lay.text_width)
            style = _Style(TITLE.role, TITLE.size * scale, TITLE.weight, leading=TITLE.leading)
            space = t.width(" ", style)
            base = (base_k if kicker else lay.label_top) + 30 * u + self._cap(style)
            step = style.size * u * style.leading
            for row, line in enumerate(lines):
                x = float(lay.margin)
                for g in line:
                    layer, box = self._ink(
                        [(x, base + row * step, groups[g], style, "l")], shadow=0.5, blur=5,
                        note=(shot["id"], f"title {g + 1}"), rise=RISE * u,
                    )  # fmt: skip
                    group_layers.append(layer)
                    boxes.append(box)
                    x += t.width(groups[g], style) + space
        scrim = None
        if boxes:
            bbox = (
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            )
            scrim = self._scrim(bbox, 1.0, RISE * u)  # its unit mask: drawn at `_title_level`
        self.layers[shot["id"]] = {"kicker": kick_layers, "groups": group_layers, "scrim": scrim}

    def _stack(self, items: Sequence[dict[str, str]], sid: str | None = None) -> _Layer:
        """Centred lines of a card or the end card (END_PORTRAIT larger in a portrait film), wrapped and
        shrunk to fit the safe zone."""
        lay, u, t = self.lay, self.u, self.type
        blocks: list[tuple[str | None, _Style, float]] = []
        prev = None
        for item in items:
            role, text = item["role"], item["text"]
            style = END_STYLES[role]
            gap = 0.0 if prev is None else 8.0
            if prev is not None and role != prev and not (prev == "role" and role == "partner"):
                gap = 36.0 if role in ("role", "link", "honesty", "fine") and prev != "fine" else 22.0
                gap = 14.0 if (prev, role) == ("honesty", "disclaimer") else gap
            if role == "credit" or (role == "title" and prev == "kicker"):
                gap = 22.0
            if prev == "brand":
                blocks.append((None, style, 26.0))  # the accent rule
                gap = 26.0
            blocks.append((text, style, gap))
            prev = role
        max_w = lay.text_width
        room = lay.bottom - lay.top
        scale = END_PORTRAIT if lay.height > lay.width else 1.0
        for attempt in range(4):
            lines: list[tuple[str | None, _Style, float]] = []
            y = 0.0
            for text, style, gap in blocks:
                y += gap * u * scale
                if text is None:
                    lines.append((None, style, y))
                    y += 3 * u * scale
                    continue
                ascent, descent = t.font(style, scale).getmetrics()
                for i, line in enumerate(t.wrap(text, style, max_w, scale, balanced=True)):
                    y += ascent if i == 0 else style.size * u * scale * style.leading
                    lines.append((line, style, y))
                y += descent
            if y <= room or attempt == 3:
                break
            scale *= room / y * 0.98
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        top = lay.top + (room - y) / 2
        cx = (lay.margin + lay.right) / 2
        for text, style, base in lines:
            if text is None:
                x0, y0 = cx - 22 * u * scale, top + base
                draw.rectangle((x0, y0, x0 + 44 * u * scale, y0 + 3 * u * scale), fill=t.colors["accent"])
            else:
                t.draw(draw, cx - t.width(text, style, scale) / 2, top + base, text, style, scale=scale)
        if bbox := canvas.getbbox():
            self._note(sid, "stack", bbox)
        return _Layer.of(canvas)

    def _rail_layer(self, days: list[int]) -> _Layer:
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        u, y, paper = self.u, self.lay.rail_y, self.type.colors["paper"]
        draw.rectangle((self.rail_ends[0], y - u, self.rail_ends[1], y + u), fill=(*paper, 110))
        for i in range(len(days)):
            x = self._rail_x(days, i)
            draw.rectangle((x - u, y - 6 * u, x + u, y + 6 * u), fill=(*paper, 170))
        return _Layer.of(canvas, shadow=0.35, blur=2 * u)

    def _rail_x(self, days: list[int], position: float) -> float:
        i = min(int(position), len(days) - 1)
        day = days[i] + (days[min(i + 1, len(days) - 1)] - days[i]) * (position - i)
        (x0, x1), span = self.rail_ends, days[-1] - days[0]
        return x1 if span == 0 else x0 + (x1 - x0) * (day - days[0]) / span

    # ----------------------------------------------------------- images --- #

    def _pyramid(self, file: str) -> tuple[np.ndarray, np.ndarray | None]:
        """L0 at the cover-fit scale s0 and, when the film pushes, L1 at s0·zmax (LRU of four)."""
        if file in self._levels:
            return self._levels[file]
        path = self.project.masters_dir / file
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is None:
            raise FileNotFoundError(f"cannot read master {path}")
        mh, mw = img.shape[:2]
        s0 = max(self.width / mw, self.height / mh)

        def level(s: float) -> np.ndarray:
            size = (max(1, round(mw * s)), max(1, round(mh * s)))
            return cv2.resize(img, size, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)

        levels = (level(s0), level(s0 * self.zmax) if self.zmax > 1 + 1e-9 else None)
        if len(self._levels) >= LRU:
            del self._levels[next(iter(self._levels))]
        self._levels[file] = levels
        return levels

    def _warp_level(self, base: np.ndarray, zoom: float, cx: float, cy: float) -> np.ndarray:
        bh, bw = base.shape[:2]
        k = max(self.width / bw, self.height / bh) * zoom
        hw, hh = self.width / (2 * k), self.height / (2 * k)
        x, y = min(max(cx * bw, hw), bw - hw), min(max(cy * bh, hh), bh - hh)
        m = np.array(
            [[k, 0, self.width / 2 - 0.5 + k * (0.5 - x)], [0, k, self.height / 2 - 0.5 + k * (0.5 - y)]]
        )
        return cv2.warpAffine(
            base, m, (self.width, self.height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
        )

    def _warp(self, file: str, cam: tuple[float, ...]) -> np.ndarray:
        x, y, z = cam
        l0, l1 = self._pyramid(file)
        if l1 is None or z <= 1 + 1e-9:
            return self._warp_level(l0, z, x, y)
        if z >= self.zmax - 1e-9:
            return self._warp_level(l1, z, x, y)
        w = math.log(z) / math.log(self.zmax)
        return cv2.addWeighted(self._warp_level(l0, z, x, y), 1 - w, self._warp_level(l1, z, x, y), w, 0)

    def camera(self, f: int) -> tuple[float, ...]:
        return keyed(self.windows, self.cams, f, mix_cam)

    # ----------------------------------------------------------- frames --- #

    def frame_at(self, f: int) -> np.ndarray:
        """Frame f of the film (pure)."""
        i = max(0, bisect.bisect_right(self.starts, f) - 1)
        return self._shot(i, f)

    def _shot(self, i: int, f: int) -> np.ndarray:
        kind = self.shots[i]["kind"]
        if kind == "title":
            return self._title(i, f)
        if kind == "capture":
            return self._capture(i, f)
        if kind == "curtain":
            return self._curtain(i, f)
        if kind == "stat":
            return self._stat(i, f)
        if kind == "rise":
            return self._rise(i, f)
        if kind == "dip":
            return self._dip(i, f)
        return self._card(i, f)

    def _split_at(self, shot: dict[str, Any], f: int) -> float:
        windows, splits = self.curtain_keys[shot["id"]]
        return keyed(windows, splits, f, lerp)

    def _composite(self, shot: dict[str, Any], cam: tuple[float, ...], split: float) -> np.ndarray:
        """Before left of the rule, after right of it (split 0 shows all of the after image)."""
        x = min(max(round(split * self.width), 0), self.width)
        if x >= self.width:
            return self._warp(shot["before_file"], cam)
        frame = self._warp(shot["after_file"], cam)
        if x > 0:
            frame[:, :x] = self._warp(shot["before_file"], cam)[:, :x]
        return frame

    def _bare(self, i: int, f: int, cam: Sequence[float] | None = None) -> np.ndarray:
        """Image shot i at frame f without its overlays or its own dissolve (what a dissolve leaves),
        through `cam` when given (a dissolve to another vantage keeps the outgoing framing)."""
        shot = self.shots[i]
        cam = self.camera(f) if cam is None else tuple(cam)
        if shot["kind"] == "curtain":
            return self._composite(shot, cam, self._split_at(shot, f))
        if shot["kind"] == "stat" and not shot.get("file"):
            return self.surfaces[shot["surface"]].copy()
        return self._warp(shot["file"], cam)

    def _reveal(self, shot: dict[str, Any], side: int, f: int) -> float:
        """Opacity of a curtain label: on only inside its reveal intervals (the rule clear of its ink),
        fading in over B/3 and out over the last B/3 frames before the rule comes back."""
        third = self.third
        for a, b in shot["reveal"][side]:
            if a <= f < b:
                op = ramp(f, a, third) if a > shot["f0"] else 1.0
                if b < shot["f1"]:
                    op *= 1 - ramp(f, b - third, third)
                return op
        return 0.0

    def _ghost(self, frame: np.ndarray, j: int, opacity: float) -> None:
        """The overlays of image shot j as they were on its last frame, at `opacity` (fading out)."""
        if opacity <= 0.002:
            return
        shot, L = self.shots[j], self.layers[self.shots[j]["id"]]
        last = shot["f1"] - 1
        if shot["kind"] == "capture":
            _put(frame, self.rail_scrim, opacity)
            _put(frame, L["label_scrim"], opacity * self._label_level(j))
            _put(frame, L["label"], opacity)
            if "words" in L and (shot.get("step") or {}).get("land") is not None:
                _put(frame, L["words_scrim"], opacity)
                _put(frame, L["words"], opacity)
            _put(frame, self.rail_layers[shot["series"]], opacity)
            self._progress(frame, self.rails[shot["series"]], shot["rail"], opacity)
        elif shot["kind"] == "curtain":
            x = self._split_at(shot, last) * self.width
            self._curtain_overlays(frame, j, last, x, opacity)
        elif shot["kind"] == "stat":
            run = self.stat_runs[shot["id"]]
            _put(frame, run["panel"], opacity)
            self._stat_text(frame, j, last, opacity, landed=True)
            _put(frame, run["ride"], opacity)

    def _grip(self, frame: np.ndarray, x: float, opacity: float) -> None:
        """The curtain rule (3 units, accent) and its grip (44 units at 0.56H) at x."""
        if opacity <= 0.002:
            return
        u, h = self.u, self.height
        y = 0.56 * h
        r = 22 * u
        before = frame.copy() if opacity < 0.998 else None
        _bar(frame, x - 1.5 * u, 0, x + 1.5 * u, h, self.accent)
        _disc(frame, x, y, r, self.accent, self.night_bgr, 1.5 * u)
        for d in (-1, 1):  # chevrons
            pts = np.array([[x + d * 5 * u, y - 7 * u], [x + d * 11 * u, y], [x + d * 5 * u, y + 7 * u]])
            cv2.polylines(
                frame, [np.round(pts * 16).astype(np.int32)], False, tuple(int(c) for c in self.paper),
                max(1, round(2 * u)), cv2.LINE_AA, 4,
            )  # fmt: skip
        if before is not None:
            frame[:] = cv2.addWeighted(frame, opacity, before, 1 - opacity, 0)

    def _curtain_overlays(self, frame: np.ndarray, i: int, f: int, x: float, opacity: float) -> None:
        """Labels (each clipped to its side of the rule) and the steps' film.line words."""
        shot, L, u = self.shots[i], self.layers[self.shots[i]["id"]], self.u
        xi = round(x)
        for side, clip in enumerate(((0, 0, xi, self.height), (xi, 0, self.width, self.height))):
            vis = self._reveal(shot, side, f) * opacity
            _put(frame, L["label_scrims"][side], vis, clip=clip)
            _put(frame, L["labels"][side], vis, clip=clip)
        index = self.curtain_words[shot["id"]]
        for k in index:
            e, out = self._line(shot, k, f)
            _put(frame, L["words_scrims"][index[k]], e * out * opacity)
            _put(frame, L["words"][index[k]], e * out * opacity, RISE * u * (1 - e))

    def _line(self, shot: dict[str, Any], k: int, f: int) -> tuple[float, float]:
        """(fade in, fade out) of curtain step k's line at frame f: in over B/3 from its landing (at
        its sweep's midpoint), out over B/3 from the next sweep's downbeat, or, when the next step
        has a line too, over the B/3 before that line lands, so it lands on a clear zone (never two
        lines over each other)."""
        steps, index = shot["steps"], self.curtain_words[shot["id"]]
        land = steps[k].get("land")
        if land is None or f < land:
            return 0.0, 0.0
        out = 1.0
        if k + 1 < len(steps):
            nxt = steps[k + 1]
            out = 1 - ramp(f, nxt["land"] - self.third if k + 1 in index else nxt["f0"], self.third)
        return ramp(f, land, self.third), out

    def _curtain(self, i: int, f: int) -> np.ndarray:
        shot, prev = self.shots[i], self.shots[i - 1] if i else None
        cam = self.camera(f)
        split = self._split_at(shot, f)
        frame = self._composite(shot, cam, split)
        d = shot["dissolve"]
        w = ramp(f, d["f0"], d["f1"] - d["f0"]) if d else 1.0
        mixed = d is not None and d["from"] not in SURFACES
        if mixed and w < 1:
            frame = _mix(self._bare(i - 1, f), frame, w)
        # The previous shot's overlays leave over the first half of the entry, ours come in over
        # the second, so two labels never share a corner.
        n = d["f1"] - d["f0"] if mixed and d is not None else 2 * self.third
        half = max(1, n // 2)
        fade = ramp(f, shot["f0"] + half, n - half) if mixed or d is None else 1.0
        if prev is not None and prev["kind"] in ("capture", "curtain", "stat") and (mixed or d is None):
            self._ghost(frame, i - 1, 1 - ramp(f, shot["f0"], half))
        x = split * self.width
        edge = min(1.0, max(0.0, min(x, self.width - x) / (24 * self.u)))
        self._grip(frame, x, fade * edge)
        self._curtain_overlays(frame, i, f, x, fade)
        self._put_tag(frame)
        if d is not None and not mixed and w < 1:
            frame = cv2.addWeighted(self.surfaces[d["from"]], 1 - w, frame, w, 0)
        return frame

    def _stat_text(
        self, frame: np.ndarray, j: int, f: int, opacity: float = 1.0, *, landed: bool = False
    ) -> None:
        """Stat j's figure and words at frame f: the figure and its unit slide up into their mask as
        one block over half a beat from `land` (never a partial figure), already the true figure;
        label and source fade in over B/3 from `text_in`, once the figure is whole. Before another
        stat of the run, all of it fades out over the shot's last B/3 frames, so the next figure lands
        on a clear panel. `landed` draws it all in place (a stat being dissolved out)."""
        if opacity <= 0.002:
            return
        shot, L = self.shots[j], self.layers[self.shots[j]["id"]]
        top, bottom = self.stat_mask[shot["id"]]
        dist = bottom - top
        half = max(1, round(self.fpb / 2))
        land = shot.get("land", shot["f0"])
        nxt = self.shots[j + 1] if j + 1 < len(self.shots) else None
        if not landed and nxt is not None and nxt["kind"] == "stat":
            opacity *= 1 - ramp(f, shot["f1"] - self.third, self.third)
            if opacity <= 0.002:
                return
        e = 1.0 if landed else ramp(f, land, half)
        for glyph in L["glyphs"]:
            _put(frame, glyph, opacity, dist * (1 - e), clip=(0, top, self.width, bottom))
        e = 1.0 if landed else ramp(f, shot.get("text_in", land + half), self.third)
        for layer in L["rest"]:
            _put(frame, layer, e * opacity)

    def _stat_overlays(
        self, frame: np.ndarray, i: int, f: int, intro: float = 1.0, panel: float | None = None
    ) -> None:
        """A stat's panel (over a picture), its text, and the riding honesty line, which comes in
        with the panel (the B/3 before the first figure lands) and stays on through the run."""
        shot = self.shots[i]
        run = self.stat_runs[shot["id"]]
        prev = self.shots[i - 1] if i else None
        within = prev is not None and prev["kind"] == "stat"
        _put(frame, run["panel"], 1.0 if within else intro if panel is None else panel)
        self._stat_text(frame, i, f, intro)
        if run["ride"] is not None:
            a, land = panel_from(shot, self.fpb), shot.get("land", shot["f0"])
            seen = 1.0 if within else ramp(f, a, land - a)
            _put(frame, run["ride"], seen * intro)

    def _stat(self, i: int, f: int) -> np.ndarray:
        """A stat over its picture (`file`: the camera keeps moving through the run) or on a plain
        surface. The first of a run dissolves in from the image before it (whose overlays leave over
        the first half; the panel arrives over the B/3 before the figure lands) or dips from a
        surface; the next stat of the run starts on the beat with no dip, after this one's text has
        faded out over its last B/3. The last stat's text holds to the shot's last frame; what
        follows the run dissolves or dips from it as it is."""
        shot = self.shots[i]
        f0, f1 = shot["f0"], shot["f1"]
        prev = self.shots[i - 1] if i else None
        nxt = self.shots[i + 1] if i + 1 < len(self.shots) else None
        if not shot.get("file"):
            frame = self.surfaces[shot["surface"]].copy()
            # Before an image, a plain stat's text fades out over its last B/3 frames: the image
            # dips in from the bare surface.
            out = 1.0
            if nxt is not None and nxt["kind"] in ("title", "capture", "curtain"):
                out = 1 - ramp(f, f1 - self.third, self.third)
            self._stat_overlays(frame, i, f, out)
            self._surface_tag(frame, shot["surface"])
            dip = shot.get("dip", 0)
            if prev is not None and dip and f < f0 + dip:
                w = ramp(f, f0, dip)
                frame = cv2.addWeighted(self._shot(i - 1, f), 1 - w, frame, w, 0)
            return frame
        frame = self._warp(shot["file"], self.camera(f))
        d = shot.get("dissolve")
        w = ramp(f, d["f0"], d["f1"] - d["f0"]) if d else 1.0
        mixed = d is not None and d["from"] not in SURFACES
        within = prev is not None and prev["kind"] == "stat"  # a stat of the run on another picture
        intro = 1.0
        if mixed and d is not None:
            if w < 1:
                frame = _mix(self._bare(i - 1, f, d.get("cam")), frame, w)
            if not within:  # into the run: the image before leaves with its overlays
                half = max(1, (d["f1"] - d["f0"]) // 2)
                if prev is not None and prev["kind"] in ("capture", "curtain"):
                    self._ghost(frame, i - 1, 1 - ramp(f, d["f0"], half))
                intro = ramp(f, d["f0"] + half, d["f1"] - d["f0"] - half)
        elif d is None and prev is not None and prev["kind"] == "capture":  # on the picture as it was
            self._ghost(frame, i - 1, 1 - ramp(f, f0, max(1, self.fpb // 2)))  # its overlays leave
        # The panel rises over the beat before the first figure lands (`panel_from`), whole as it
        # lands (a held entry, "N1.0", shows the bare picture until then); within the run it stays.
        land = shot.get("land", f0)
        a = panel_from(shot, self.fpb)
        panel = ramp(f, a, land - a)
        self._stat_overlays(frame, i, f, intro, panel)
        self._put_tag(frame)
        if d is not None and not mixed and w < 1:
            frame = cv2.addWeighted(self.surfaces[d["from"]], 1 - w, frame, w, 0)
        return frame

    def _held_rail(self, frame: np.ndarray, pos: float) -> None:
        """A loop's rail from its rise to its first capture: scrim, track and ticks, knob at `pos`."""
        s = self.rise_series
        if s is None:
            return
        _put(frame, self.rail_scrim, 1.0)
        _put(frame, self.rail_layers[s], 1.0)
        self._progress(frame, self.rails[s], pos, 1.0)

    def _rise(self, i: int, f: int) -> np.ndarray:
        """A loop's opening: pure night on its first frame, easing (display gamma) onto the title's
        image at rest under the title's scrim, the rail's knob sweeping back to its first tick."""
        shot, title = self.shots[i], self.shots[i + 1]
        n = shot["f1"] - shot["f0"]
        w = ramp(f, shot["f0"] + 1, n)  # 0 on the first frame, 1 one past the last
        if w <= 0:
            return self.night_fill.copy()
        frame = cv2.addWeighted(self._warp(title["file"], self.camera(f)), 0.70, self.card_bg, 0.30, 0)
        if self.rise_series is not None:
            self._held_rail(frame, (len(self.rails[self.rise_series]) - 1) * (1 - w))
        self._put_tag(frame)
        return cv2.addWeighted(self.night_fill, 1 - w, frame, w, 0)

    def _dip(self, i: int, f: int) -> np.ndarray:
        """A loop's close: the end card eases (display gamma) to pure night, reached on frame N."""
        shot = self.shots[i]
        w = ramp(f, shot["f0"], shot["f1"] - shot["f0"] + 1)  # 1 on f1 = N, one past the last frame
        if w >= 1:
            return self.night_fill.copy()
        return cv2.addWeighted(self._shot(i - 1, f), 1 - w, self.night_fill, w, 0)

    def _title(self, i: int, f: int) -> np.ndarray:
        shot, L = self.shots[i], self.layers[self.shots[i]["id"]]
        third, u = self.third, self.u
        frame = self._warp(shot["file"], self.camera(f))
        out = 1 - ramp(f, shot["f1"] - self.fpb, self.fpb)  # text and scrim leave over a whole beat
        a = 0.30 * out
        if a > 0.002:
            frame[:] = cv2.addWeighted(frame, 1 - a, self.card_bg, a, 0)
        if i and self.shots[i - 1]["kind"] == "rise":
            self._held_rail(frame, 0.0)
        groups = shot["groups"]
        seen = [ramp(f, groups[g], third) for g in range(len(L["groups"]))]
        kick = ramp(f, shot["f0"], third) if L["kicker"] else 0.0
        # The scrim comes in over the kicker's whole beat (a raised one would otherwise darken the
        # picture in a third of a beat), and is full before the title's first group rises.
        under = ramp(f, shot["f0"], self.fpb) if L["kicker"] else 0.0
        _put(frame, L["scrim"], max([under, *seen]) * out * self._title_level(i))
        if L["kicker"]:
            _put(frame, L["kicker"][min(f - shot["f0"], len(L["kicker"]) - 1)], kick * out)
        for g, layer in enumerate(L["groups"]):
            _put(frame, layer, seen[g] * out, RISE * u * (1 - seen[g]))
        self._put_tag(frame)
        return frame

    def _capture(self, i: int, f: int) -> np.ndarray:
        shot = self.shots[i]
        prev = self.shots[i - 1] if i else None
        cam = self.camera(f)
        frame = self._warp(shot["file"], cam)
        d = shot["dissolve"]
        w = ramp(f, d["f0"], d["f1"] - d["f0"]) if d else 1.0
        mixed = d is not None and d["from"] not in SURFACES
        if mixed and w < 1:
            frame = _mix(self._bare(i - 1, f, d.get("cam")), frame, w)
        within = mixed and prev is not None and prev["kind"] == "capture"
        intro = None
        if mixed and d is not None and prev is not None and prev["kind"] in ("curtain", "stat"):
            half = max(1, (d["f1"] - d["f0"]) // 2)
            self._ghost(frame, i - 1, 1 - ramp(f, d["f0"], half))
            intro = ramp(f, d["f0"] + half, d["f1"] - d["f0"] - half)
        self._overlays(frame, i, f, w if within else 1.0, intro)
        if d is not None and not mixed and w < 1:
            frame = cv2.addWeighted(self.surfaces[d["from"]], 1 - w, frame, w, 0)
        return frame

    def _overlays(self, frame: np.ndarray, i: int, f: int, w: float, intro: float | None = None) -> None:
        """Scrims, date label, words, rail and tag of capture i at frame f (w: its dissolve so far)."""
        shot, L, u = self.shots[i], self.layers[self.shots[i]["id"]], self.u
        prev = self.shots[i - 1] if i else None
        within = w < 1 and prev is not None and prev["kind"] == "capture"
        P = self.layers[prev["id"]] if within and prev is not None else None
        if intro is None:
            intro = ramp(f, shot["f0"], self.third) if prev is not None and prev["kind"] == "title" else 1.0
        # A loop's rail is already on screen when its first capture follows the title.
        held = prev is not None and prev["kind"] == "title" and self.rise_series == shot["series"] and i == 2
        rail_in = 1.0 if held else intro
        # Scrims, then text.
        _put(frame, self.rail_scrim, rail_in)
        if P is not None:
            _put(frame, P["label_scrim"], (1 - w) * self._label_level(i - 1))
            _put(frame, L["label_scrim"], w * self._label_level(i))
        else:
            _put(frame, L["label_scrim"], intro * self._label_level(i))
        d = shot["dissolve"]
        swap = d is None or f >= d["f0"] + (d["f1"] - d["f0"]) // 2
        _put(frame, L["label"] if swap or P is None else P["label"], intro)
        if P is not None and "words" in P:
            _put(frame, P["words_scrim"], 1 - w)
            _put(frame, P["words"], 1 - w)
        step = shot.get("step") or {}
        if "words" in L and "land" in step:
            e = ramp(f, step["land"], self.third)
            _put(frame, L["words_scrim"], e)
            _put(frame, L["words"], e, RISE * u * (1 - e))
        # Rail: track and ticks, accent up to the knob, lit ticks passed, the knob.
        days = self.rails[shot["series"]]
        same = within and prev is not None and prev["series"] == shot["series"]
        pos = shot["rail"] - 1 + w if same else shot["rail"]
        if within and not same and prev is not None:  # another series' rail crossfades out
            _put(frame, self.rail_layers[prev["series"]], rail_in * (1 - w))
            self._progress(frame, self.rails[prev["series"]], prev["rail"], rail_in * (1 - w))
            rail_in *= w
        _put(frame, self.rail_layers[shot["series"]], rail_in)
        if rail_in > 0.002:
            self._progress(frame, days, pos, rail_in)
        self._put_tag(frame)

    def _progress(self, frame: np.ndarray, days: list[int], pos: float, opacity: float) -> None:
        u, y = self.u, self.lay.rail_y
        x = self._rail_x(days, pos)
        band = None
        if opacity < 0.998:
            r0, r1 = max(int(y - 12 * u) - 2, 0), min(math.ceil(y + 12 * u) + 2, self.height)
            band = (r0, r1, frame[r0:r1].copy())
        _bar(frame, self.rail_ends[0], y - 1.5 * u, x, y + 1.5 * u, self.accent)
        for k in range(min(int(pos + 1e-6), len(days) - 1) + 1):
            xk = self._rail_x(days, k)
            _bar(frame, xk - u, y - 6 * u, xk + u, y + 6 * u, self.accent)
        _disc(frame, x, y, 7 * u, self.accent, self.night_bgr, 1.5 * u)
        if band is not None:
            r0, r1, before = band
            frame[r0:r1] = cv2.addWeighted(frame[r0:r1], opacity, before, 1 - opacity, 0)

    def _card(self, i: int, f: int) -> np.ndarray:
        """A card or the end card: dips from the image before it, text fades in once on night."""
        shot = self.shots[i]
        L = self.layers[shot["id"]]
        third, dip = self.third, shot.get("dip", self.d)
        frame = self.card_bg.copy()
        opacity = ramp(f, shot["f0"] + dip, third)
        nxt = self.shots[i + 1] if i + 1 < len(self.shots) else None
        if shot["kind"] == "card" or (nxt is not None and nxt["kind"] == "end"):
            opacity *= 1 - ramp(f, shot["f1"] - third, third)
        elif nxt is None:  # the last shot of a film that does not loop: its text fades to night
            opacity *= 1 - ramp(f, shot["f1"] - self.fpb, self.fpb)
        _put(frame, L["stack"], opacity)
        self._surface_tag(frame, "night")
        prev = self.shots[i - 1] if i else None
        if prev is not None and f < shot["f0"] + dip:
            w = ramp(f, shot["f0"], dip)
            frame = cv2.addWeighted(self._shot(i - 1, f), 1 - w, frame, w, 0)
        return frame

    # ------------------------------------------------------------ output --- #

    def poster(self) -> bytes:
        """The poster JPEG: the last frame of the last series shot."""
        from vantage.film.render import POSTER_QUALITY

        params = [cv2.IMWRITE_JPEG_QUALITY, POSTER_QUALITY, cv2.IMWRITE_JPEG_PROGRESSIVE, 1]
        ok, data = cv2.imencode(".jpg", self.frame_at(self.p["poster"]), params)
        if not ok:
            raise ValueError(f"could not encode the poster of film {self.plan.id}")
        return data.tobytes()


__all__ = ["_Cut"]
