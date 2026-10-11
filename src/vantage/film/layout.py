"""Where text and the rail sit in a film: safe zones and anchors per cut and aspect (film/layout.py).

    story-16x9    safe .06H .05W .06H .05W · date label cap-top at the safe top · lead baseline .78H
                  · rail .88H · tag top-right inside the safe zone
    story-9x16    safe .14H .12W .28H .06W · label cap-top .16H · lead baseline .60H · rail .70H
                  · tag right-aligned, baseline 18 units above the rail
    classic-16x9  today's numbers, byte for byte: margin .055W, rail H − round(.075H), bottom scrim
                  rows .42H, tag .8·margin from the top-left corner (ink checked against .05 all round)
    classic-9x16  the story-9x16 safe zone: rail .70H, the lower third above it, the tag top-left at
                  the safe top (below .14H), a bottom scrim rising from rail − .30H over .22H

The 9:16 zone keeps every word and the rail between 14 % and 72 % of the height and clear of the
buttons phones overlay on the right edge (Reels, Shorts, TikTok). Sizes in styles are in units of
short edge / 1080, multiplied by `type_scale`. A film's `safe` fractions replace the preset's, and
the anchors move with the edge they hang from: the label with the top, the lead and the rail with
the bottom, the classic margin with the left.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

TagAnchor = Literal["top-right", "rail-right", "top-left", "corner"]


@dataclass(frozen=True)
class Layout:
    name: str
    width: int
    height: int
    safe: tuple[int, int, int, int]  # px: top, right, bottom, left
    margin: int  # left text edge (story: the safe left)
    label_top: int  # cap top of the date label, the kicker and the curtain labels
    lead_baseline: int
    rail_y: int
    tag: TagAnchor
    type_scale: float = 1.0
    scrim: tuple[int, int] | None = None  # classic: bottom scrim (first row, rows of its ramp)
    rail_x: tuple[float, float] | None = None  # classic: rail ends (story: inset by its knob)

    @property
    def unit(self) -> float:
        """One style unit in px: short edge / 1080 × type_scale."""
        return min(self.width, self.height) / 1080 * self.type_scale

    @property
    def right(self) -> int:
        """The right text edge (x)."""
        return self.width - self.safe[1]

    @property
    def text_width(self) -> int:
        return self.right - self.margin

    @property
    def top(self) -> int:
        return self.safe[0]

    @property
    def bottom(self) -> int:
        """The lowest y any ink may reach."""
        return self.height - self.safe[2]

    @property
    def rect(self) -> tuple[int, int, int, int]:
        """The safe rectangle as (x0, y0, x1, y1)."""
        return self.safe[3], self.safe[0], self.right, self.bottom

    def record(self) -> dict[str, Any]:
        """plan.json `layout` (classic layouts add `scrim` and `rail_x`)."""
        rec: dict[str, Any] = {
            "name": self.name,
            "safe": list(self.safe),
            "margin": self.margin,
            "label_top": self.label_top,
            "lead_baseline": self.lead_baseline,
            "rail_y": self.rail_y,
            "tag": self.tag,
            "type_scale": self.type_scale,
        }
        if self.scrim is not None:
            rec["scrim"] = list(self.scrim)
        if self.rail_x is not None:
            rec["rail_x"] = list(self.rail_x)
        return rec

    @classmethod
    def from_record(cls, width: int, height: int, rec: dict[str, Any]) -> Layout:
        safe = rec["safe"]
        scrim, rail_x = rec.get("scrim"), rec.get("rail_x")
        return cls(
            rec["name"], width, height, (safe[0], safe[1], safe[2], safe[3]), rec["margin"],
            rec["label_top"], rec["lead_baseline"], rec["rail_y"], rec["tag"], rec["type_scale"],
            (scrim[0], scrim[1]) if scrim is not None else None,
            (rail_x[0], rail_x[1]) if rail_x is not None else None,
        )  # fmt: skip


Fractions = tuple[float, float, float, float]

# Story presets: name → (safe fractions [t, r, b, l] of H, W, H, W), label cap-top (None = safe top),
# lead baseline, rail y (fractions of H), tag anchor.
_STORY: dict[str, tuple[Fractions, float | None, float, float, TagAnchor]] = {
    "story-16x9": ((0.06, 0.05, 0.06, 0.05), None, 0.78, 0.88, "top-right"),
    "story-9x16": ((0.14, 0.12, 0.28, 0.06), 0.16, 0.60, 0.70, "rail-right"),
}
CLASSIC_16X9: Fractions = (0.05, 0.05, 0.05, 0.05)  # the zone its ink is checked against
CLASSIC_KNOB = 5  # units: the classic rail knob's radius (its rail ends are inset by it in 9:16)
PRESETS = (*_STORY, "classic-16x9", "classic-9x16")


def _px(f: Sequence[float], width: int, height: int) -> tuple[int, int, int, int]:
    return round(f[0] * height), round(f[1] * width), round(f[2] * height), round(f[3] * width)


def _story(name: str, width: int, height: int, safe: Sequence[float] | None, type_scale: float) -> Layout:
    preset, label, lead, rail, tag = _STORY[name]
    t, r, b, left = safe if safe is not None else preset
    px = _px((t, r, b, left), width, height)
    # Anchors move with their edge (+ 0.0 for the preset itself: the same floats as before).
    dt, db = t - preset[0], preset[2] - b
    label_top = px[0] if label is None else max(px[0], round((label + dt) * height))
    return Layout(
        name, width, height, px, px[3], label_top,
        min(round((lead + db) * height), height - px[2]), min(round((rail + db) * height), height - px[2]),
        tag, type_scale,
    )  # fmt: skip


def _classic_16x9(width: int, height: int, safe: Sequence[float] | None, type_scale: float) -> Layout:
    """Today's classic landscape numbers: margin round(.055W), rail H − round(.075H), scrim .42H."""
    t, r, b, left = safe if safe is not None else CLASSIC_16X9
    px = _px((t, r, b, left), width, height)
    margin = round((0.055 + (left - CLASSIC_16X9[3])) * width)
    rail_y = height - round((0.075 + (b - CLASSIC_16X9[2])) * height)
    rows = round(0.42 * height)
    return Layout(
        "classic-16x9", width, height, px, margin, px[0], rail_y, rail_y, "corner", type_scale,
        (height - rows, rows), (margin, width - margin),
    )  # fmt: skip


def _classic_9x16(width: int, height: int, safe: Sequence[float] | None, type_scale: float) -> Layout:
    """The story 9:16 safe zone for the classic cut: lower third over a rail at .70H, tag top-left."""
    s = _story("story-9x16", width, height, safe, type_scale)
    knob = CLASSIC_KNOB * s.unit
    return Layout(
        "classic-9x16", width, height, s.safe, s.margin, s.top, s.lead_baseline, s.rail_y, "top-left",
        type_scale, (s.rail_y - round(0.30 * height), round(0.22 * height)),
        (s.margin + knob, s.right - knob),
    )  # fmt: skip


def layout(
    name: str,
    width: int,
    height: int,
    *,
    safe: Sequence[float] | None = None,
    type_scale: float = 1.0,
) -> Layout:
    """The named preset at width × height (px values rounded), with optional safe fractions."""
    if name in _STORY:
        return _story(name, width, height, safe, type_scale)
    if name == "classic-16x9":
        return _classic_16x9(width, height, safe, type_scale)
    if name == "classic-9x16":
        return _classic_9x16(width, height, safe, type_scale)
    raise ValueError(f"unknown film layout {name!r}; known: {', '.join(PRESETS)}")


def outside(box: Sequence[float], rect: Sequence[float], tolerance: float = 0.0) -> bool:
    """Whether an ink box [x, y, w, h] leaves the rectangle (x0, y0, x1, y1)."""
    x, y, w, h = box
    x0, y0, x1, y1 = rect
    return x < x0 - tolerance or y < y0 - tolerance or x + w > x1 + tolerance or y + h > y1 + tolerance


__all__ = ["PRESETS", "Layout", "layout", "outside"]
