"""Storyboards: one contact sheet per film plan, so a cut can be approved before anything is encoded.

    <slug>-<id>.storyboard.jpg

One tile per key frame of the plan (classic cuts: the middle of each card, the middle of each
capture's hold, and the poster frame), in film order: 8 columns of 320 px tiles for landscape
films, 10 columns of 192 px for portrait ones. Under each tile a strip carries the timecode
(`bar.beat · m:ss.ff · f<frame>` on a beat grid, without bar.beat otherwise), `<shot> <kind> <date>`
and one chip per printed fact: green releasable, amber needs-client or reported without an
attribution, red do-not-print. The footer names the film and its plan hash, and says whether it is
ready for release. Labels use the bundled Inter; the JPEG is baseline, quality 85, without metadata,
so the same plan gives the same bytes.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from vantage.models import FilmPlan
from vantage.paths import RUNTIME_DIR

LANDSCAPE = (8, 320)  # columns, tile width
PORTRAIT = (10, 192)
STRIP = 54  # px of labels under each tile
FOOTER = 36
QUALITY = 85
_BG, _INK, _DIM = (16, 17, 18), (232, 232, 228), (150, 152, 150)
_CHIPS = {  # fill, text
    "green": ((46, 125, 50), (255, 255, 255)),
    "amber": ((236, 167, 32), (24, 20, 10)),
    "red": ((198, 40, 40), (255, 255, 255)),
}
_INTER = RUNTIME_DIR / "fonts" / "inter" / "inter-latin-opsz-normal.woff2"

FrameFn = Callable[[int], np.ndarray]


@dataclass(frozen=True)
class Tile:
    key: int  # frame index
    shot: str
    kind: str
    date: str | None
    facts: tuple[str, ...]


def tiles(plan: FilmPlan) -> list[Tile]:
    """The plan's key frames, in film order."""
    out: list[Tile] = []
    p = plan.plan
    for shot in p["shots"]:
        if shot["kind"] == "section":
            out += [
                Tile(c["key"], shot["id"], "section", c["date"], tuple(c["facts"])) for c in shot["shots"]
            ]
        else:
            kind = f"card {shot['role']}" if shot["kind"] == "card" else shot["kind"]
            date = shot.get("capture") if isinstance(shot.get("capture"), str) else None
            out.append(Tile(shot["key"], shot["id"], kind, date, tuple(shot["facts"])))
    if p.get("poster_seconds") is not None:
        first = next((s for s in p["shots"] if s["kind"] == "section"), None)
        date = first["shots"][-1]["date"] if first else None
        key = min(round(p["poster_seconds"] * p["fps"]), p["frames"] - 1)
        out.append(Tile(key, "P1", "poster", date, ()))
    return out


def grid(plan: FilmPlan, n: int) -> tuple[int, int, int, int, int]:
    """(columns, tile width, tile height, sheet width, sheet height) for n tiles."""
    w, h = plan.plan["width"], plan.plan["height"]
    cols, tile_w = PORTRAIT if h > w else LANDSCAPE
    tile_h = round(tile_w * h / w)
    rows = max(1, math.ceil(n / cols))
    return cols, tile_w, tile_h, cols * tile_w, rows * (tile_h + STRIP) + FOOTER


def timecode(frame: int, fps: int) -> str:
    seconds, ff = divmod(frame, fps)
    return f"{seconds // 60}:{seconds % 60:02d}.{ff:02d}"


def chip_color(fact: Mapping[str, Any] | None) -> str:
    if fact is None or fact.get("status") == "do-not-print":
        return "red"
    return "green" if fact.get("releasable") else "amber"


@cache
def _font(px: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.truetype(str(_INTER), px) if _INTER.is_file() else ImageFont.load_default(px)


def _fit(draw: ImageDraw.ImageDraw, text: str, font: Any, width: float) -> str:
    """Text cut with an ellipsis to fit `width` px."""
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text + "…"


def write_storyboard(
    plan: FilmPlan, frame_fn: FrameFn, facts: Mapping[str, Mapping[str, Any]], out: Path
) -> Path:
    """Render plan's key frames with frame_fn into one JPEG at `out` (written as JPEG whatever its suffix)."""
    p = plan.plan
    shots = tiles(plan)
    cols, tile_w, tile_h, width, height = grid(plan, len(shots))
    sheet = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(sheet)
    line, small, foot = _font(13), _font(11), _font(14)
    fps, beat = p["fps"], (p.get("grid") or {}).get("fpb")
    for n, tile in enumerate(shots):
        x, y = (n % cols) * tile_w, (n // cols) * (tile_h + STRIP)
        frame = cv2.resize(frame_fn(tile.key), (tile_w, tile_h), interpolation=cv2.INTER_AREA)
        sheet.paste(Image.fromarray(np.ascontiguousarray(frame[..., ::-1])), (x, y))
        draw.line((x, y, x, y + tile_h + STRIP - 1), fill=_BG, width=1)  # a hairline between tiles
        when = f"{timecode(tile.key, fps)} · f{tile.key}"
        if beat:
            when = f"{tile.key // (4 * beat) + 1}.{tile.key // beat % 4 + 1} · {when}"
        what = " ".join(filter(None, (tile.shot, tile.kind, tile.date)))
        draw.text(
            (x + 6, y + tile_h + 16), _fit(draw, when, line, tile_w - 10), font=line, fill=_INK, anchor="ls"
        )
        draw.text(
            (x + 6, y + tile_h + 32), _fit(draw, what, line, tile_w - 10), font=line, fill=_DIM, anchor="ls"
        )
        cx = x + 6
        for i, fact in enumerate(tile.facts):
            label = fact
            w = draw.textlength(label, font=small) + 8
            rest = len(tile.facts) - i
            if cx + w > x + tile_w - 6 and rest:
                more = f"+{rest}"
                draw.text((cx, y + tile_h + 48), more, font=small, fill=_INK, anchor="ls")
                break
            fill, ink = _CHIPS[chip_color(facts.get(fact))]
            draw.rounded_rectangle((cx, y + tile_h + 37, cx + w, y + tile_h + 51), radius=3, fill=fill)
            draw.text((cx + 4, y + tile_h + 48), label, font=small, fill=ink, anchor="ls")
            cx += w + 4
    unreleasable = sum(not f.get("releasable") for f in facts.values())
    status = "DRAFT" if p.get("draft") or unreleasable else "RELEASE-READY"
    if unreleasable:
        status += f" ({unreleasable} unreleasable)"
    footer = " · ".join(
        [
            p["slug"],
            plan.id,
            p["cut"],
            f"{p['width']}×{p['height']}@{fps}",
            timecode(p["frames"], fps),
            f"plan {plan.hash[:12]}",
            status,
        ]
    )
    draw.text((10, height - 13), footer, font=foot, fill=_INK, anchor="ls")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, format="JPEG", quality=QUALITY)
    return out
