"""Where text and the rail sit in a story cut: safe zones and anchors per aspect (film/layout.py).

    story-16x9   safe .06H .05W .06H .05W · date label cap-top at the safe top · lead baseline .78H
                 · rail .88H · tag top-right inside the safe zone
    story-9x16   safe .14H .12W .28H .06W · label cap-top .16H · lead baseline .60H · rail .70H
                 · tag right-aligned, baseline 18 units above the rail

The 9:16 zone keeps every word and the rail between 14 % and 72 % of the height and clear of the
buttons phones overlay on the right edge (Reels, Shorts, TikTok). Sizes in styles are in units of
short edge / 1080, multiplied by `type_scale`. A film's `safe` fractions replace the preset's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

TagAnchor = Literal["top-right", "rail-right"]


@dataclass(frozen=True)
class Layout:
    name: str
    width: int
    height: int
    safe: tuple[int, int, int, int]  # px: top, right, bottom, left
    margin: int  # left text edge (= safe left)
    label_top: int  # cap top of the date label, the kicker and the curtain labels
    lead_baseline: int
    rail_y: int
    tag: TagAnchor
    type_scale: float = 1.0

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

    def record(self) -> dict[str, Any]:
        """plan.json `layout`."""
        return {
            "name": self.name,
            "safe": list(self.safe),
            "margin": self.margin,
            "label_top": self.label_top,
            "lead_baseline": self.lead_baseline,
            "rail_y": self.rail_y,
            "tag": self.tag,
            "type_scale": self.type_scale,
        }

    @classmethod
    def from_record(cls, width: int, height: int, rec: dict[str, Any]) -> Layout:
        safe = rec["safe"]
        return cls(
            rec["name"], width, height, (safe[0], safe[1], safe[2], safe[3]), rec["margin"],
            rec["label_top"], rec["lead_baseline"], rec["rail_y"], rec["tag"], rec["type_scale"],
        )  # fmt: skip


# name → (safe fractions [t, r, b, l] of H, W, H, W), label cap-top (None = safe top), lead baseline,
# rail y (fractions of H), tag anchor
_PRESETS: dict[str, tuple[tuple[float, float, float, float], float | None, float, float, TagAnchor]] = {
    "story-16x9": ((0.06, 0.05, 0.06, 0.05), None, 0.78, 0.88, "top-right"),
    "story-9x16": ((0.14, 0.12, 0.28, 0.06), 0.16, 0.60, 0.70, "rail-right"),
}
PRESETS = tuple(_PRESETS)


def layout(
    name: str,
    width: int,
    height: int,
    *,
    safe: Sequence[float] | None = None,
    type_scale: float = 1.0,
) -> Layout:
    """The named preset at width × height (px values rounded), with optional safe fractions."""
    if name not in _PRESETS:
        raise ValueError(f"unknown film layout {name!r}; known: {', '.join(PRESETS)}")
    fractions, label, lead, rail, tag = _PRESETS[name]
    t, r, b, left = safe if safe is not None else fractions
    px = (round(t * height), round(r * width), round(b * height), round(left * width))
    label_top = px[0] if label is None else max(px[0], round(label * height))
    return Layout(
        name, width, height, px, px[3], label_top,
        min(round(lead * height), height - px[2]), min(round(rail * height), height - px[2]),
        tag, type_scale,
    )  # fmt: skip


__all__ = ["PRESETS", "Layout", "layout"]
