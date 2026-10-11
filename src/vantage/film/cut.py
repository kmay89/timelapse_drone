"""The story-cut renderer: a pure frame(f) over a story FilmPlan (film/cut.py).

`_Cut(project, plan)` typesets every text block of the plan once (brand fonts, sizes in units of
short edge / 1080 × type_scale) and then evaluates each frame from the plan alone:

    keyed camera (film/beat.py) → warp of a two-level pyramid (L0 at the cover-fit scale s0, L1 at
    s0·zmax, blended by log z / log zmax so a push never pops between levels) → linear-light `_mix`
    dissolves → scrims behind each text block's ink → date label → words rising at sub-pixel offsets
    → rail and knob → tag ('Simulated imagery', 'Preview', and ' · plan <hash6>' in drafts).

Cards and the end card dip to and from night in display gamma, like the classic cut. Every image
pixel is a crop, a dissolve or a dip of registered masters; text, scrims and the rail are overlays.
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw

from vantage.config import Project
from vantage.film.beat import Window, keyed, mix_cam, ramp
from vantage.film.layout import Layout
from vantage.film.plan import ROLES, font_path
from vantage.film.render import _hex, _Layer, _mix, _Style, _Type
from vantage.models import FilmPlan

# Styles, in units (short edge / 1080 × type_scale).
KICKER = _Style("numeric", 38, 600, "accent", tracking=0.22, upper=True)
TITLE = _Style("display", 124, 400, leading=1.08)
DATE = _Style("numeric", 52, 600, tracking=0.08, upper=True)
LEAD = _Style("display", 120, 500)
TAG = _Style("numeric", 22, 600, alpha=0.85, tracking=0.16, upper=True)
CARD_TITLE = _Style("display", 96, 400, leading=1.08)
BRAND = _Style("display", 92, 400, leading=1.08)
CREDIT = _Style("text", 36, 400, alpha=0.9, leading=1.35)
ROLE = _Style("numeric", 22, 600, alpha=0.6, tracking=0.16, upper=True)
PARTNER = _Style("text", 34, 500)
LINK = _Style("text", 34, 500, "accent")
HONESTY = _Style("text", 38, 400, alpha=0.92, leading=1.35)
FINE = _Style("text", 27, 400, alpha=0.62, leading=1.4)
END_STYLES = {
    "brand": BRAND, "credit": CREDIT, "role": ROLE, "partner": PARTNER, "link": LINK,
    "honesty": HONESTY, "fine": FINE, "kicker": KICKER, "title": CARD_TITLE,
}  # fmt: skip
RISE = 14  # units a text block rises while it fades in
LRU = 4  # masters kept decoded (a dissolve needs two)

Items = Sequence[tuple[float, float, str, _Style, str]]  # x, baseline y, text, style, align (l|r|c)


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


def _put(frame: np.ndarray, layer: _Layer | None, opacity: float, dy: float = 0.0) -> None:
    """Composite a layer at a (sub-pixel) vertical offset, clipped to the frame."""
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
    y0, y1 = max(iy, 0), min(iy + h, fh)
    x0, x1 = max(layer.x, 0), min(layer.x + w, fw)
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
        inputs = p["inputs"]
        files = {role: font_path(inputs["fonts"][role]["file"], project.brand) for role in ROLES}
        colors = inputs["colors"]
        self.type = _Type(files, colors, u)
        self.night = np.array(_hex(colors["night"])[::-1], np.float32)
        self.night_bgr = tuple(int(v) for v in self.night)
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
        self.shots: list[dict[str, Any]] = p["shots"]
        self.starts = [s["f0"] for s in self.shots]
        cams = p["camera"]
        self.windows = [Window(c["a"], c["b"]) for c in cams]
        self.cams = [(c["x"], c["y"], c["zoom"]) for c in cams]
        self.zmax = max([1.0, *(c["zoom"] for c in cams)])
        self.masters = inputs["masters"]
        self._levels: dict[str, tuple[np.ndarray, np.ndarray | None]] = {}
        self.tag_scrim: _Layer | None = None
        words = list(p["tag"])
        if p.get("draft"):
            words.append(f"plan {plan.hash[:6]}")
        self.tag = self._tag(words)
        self._typeset()

    # ------------------------------------------------------------- type --- #

    def _cap(self, style: _Style) -> float:
        return -self.type.font(style).getbbox("H", anchor="ls")[1]

    def _ink(self, items: Items, *, shadow: float = 0.6, blur: float = 4.0) -> tuple[_Layer, tuple[int, ...]]:
        """A layer of text items (x, baseline, text, style, align) and its ink box."""
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        for x, y, text, style, align in items:
            w = self.type.width(text, style)
            x = x - w if align == "r" else x - w / 2 if align == "c" else x
            self.type.draw(draw, x, y, text, style)
        bbox = canvas.getbbox() or (0, 0, 1, 1)
        return _Layer.of(canvas, shadow=shadow, blur=blur * self.u), bbox

    def _scrim(self, bbox: Sequence[float], alpha: float, rise: float = 0.0) -> _Layer:
        """A feathered night box behind an ink box (grown by `rise` px downward for rising text)."""
        u = self.u
        sigma = 14 * u
        pad_x, pad_y = 26 * u, 18 * u
        x0, y0 = bbox[0] - pad_x, bbox[1] - pad_y
        x1, y1 = bbox[2] + pad_x, bbox[3] + pad_y + rise
        edge = math.ceil(3 * sigma)
        X0, Y0 = max(math.floor(x0) - edge, 0), max(math.floor(y0) - edge, 0)
        X1, Y1 = min(math.ceil(x1) + edge, self.width), min(math.ceil(y1) + edge, self.height)
        mask = np.zeros((Y1 - Y0, X1 - X0), np.float32)
        mask[round(y0) - Y0 : round(y1) - Y0, round(x0) - X0 : round(x1) - X0] = 1.0
        mask = cv2.GaussianBlur(mask, (0, 0), sigma) * alpha
        a = np.ascontiguousarray(mask[..., None])
        return _Layer(X0, Y0, np.ascontiguousarray(self.night * a), a)

    def _tag(self, words: Sequence[str]) -> _Layer | None:
        if not words:
            return None
        lay, text = self.lay, " · ".join(words)
        top = lay.tag == "top-right"
        base = lay.top + self._cap(TAG) if top else lay.rail_y - 18 * self.u
        layer, box = self._ink([(lay.right, base, text, TAG, "r")], shadow=0.6, blur=3)
        self.tag_scrim = self._scrim(box, 0.3)
        return layer

    def _put_tag(self, frame: np.ndarray) -> None:
        if self.tag is not None:
            _put(frame, self.tag_scrim, 1.0)
            _put(frame, self.tag, 1.0)

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
        return scale, lines

    def _typeset(self) -> None:
        lay, u, t = self.lay, self.u, self.type
        self.layers: dict[str, Any] = {}
        for shot in self.shots:
            sid, kind = shot["id"], shot["kind"]
            text = {item["role"]: item["text"] for item in shot["text"]}
            if kind == "title":
                self._typeset_title(shot, text)
            elif kind == "capture":
                cap_top = lay.label_top + self._cap(DATE)
                label, box = self._ink([(lay.margin, cap_top, shot["label"], DATE, "l")])
                entry: dict[str, Any] = {"label": label, "label_scrim": self._scrim(box, 0.42)}
                words = text.get("words")
                if words:
                    size = 1.0
                    w = t.width(words, LEAD)
                    if w > lay.text_width:
                        size = lay.text_width / w
                    style = _Style(LEAD.role, LEAD.size * size, LEAD.weight)
                    layer, box = self._ink(
                        [(lay.margin, lay.lead_baseline, words, style, "l")], shadow=0.8, blur=7
                    )
                    entry["words"] = layer
                    entry["words_scrim"] = self._scrim(box, 0.5, RISE * u)
                self.layers[sid] = entry
            elif kind in ("card", "end"):
                self.layers[sid] = {"stack": self._stack(shot["text"])}
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
            boxes.append(box)
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
                        [(x, base + row * step, groups[g], style, "l")], shadow=0.5, blur=5
                    )
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
            scrim = self._scrim(bbox, 0.45, RISE * u)
        self.layers[shot["id"]] = {"kicker": kick_layers, "groups": group_layers, "scrim": scrim}

    def _stack(self, items: Sequence[dict[str, str]]) -> _Layer:
        """Centred lines of a card or the end card, wrapped and shrunk to fit the safe zone."""
        lay, u, t = self.lay, self.u, self.type
        blocks: list[tuple[str | None, _Style, float]] = []
        prev = None
        for item in items:
            role, text = item["role"], item["text"]
            style = END_STYLES[role]
            gap = 0.0 if prev is None else 8.0
            if prev is not None and role != prev and not (prev == "role" and role == "partner"):
                gap = 36.0 if role in ("role", "link", "honesty", "fine") and prev != "fine" else 22.0
            if role == "credit" or (role == "title" and prev == "kicker"):
                gap = 22.0
            if prev == "brand":
                blocks.append((None, style, 26.0))  # the accent rule
                gap = 26.0
            blocks.append((text, style, gap))
            prev = role
        max_w = lay.text_width
        room = lay.bottom - lay.top
        scale = 1.0
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
                for i, line in enumerate(t.wrap(text, style, max_w, scale)):
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
        return _Layer.of(canvas)

    def _rail_layer(self, days: list[int]) -> _Layer:
        canvas = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(canvas)
        u, y, paper = self.u, self.lay.rail_y, self.type.colors["paper"]
        draw.rectangle((self.lay.margin, y - u, self.lay.right, y + u), fill=(*paper, 110))
        for i in range(len(days)):
            x = self._rail_x(days, i)
            draw.rectangle((x - u, y - 6 * u, x + u, y + 6 * u), fill=(*paper, 170))
        return _Layer.of(canvas, shadow=0.35, blur=2 * u)

    def _rail_x(self, days: list[int], position: float) -> float:
        i = min(int(position), len(days) - 1)
        day = days[i] + (days[min(i + 1, len(days) - 1)] - days[i]) * (position - i)
        x0, x1, span = self.lay.margin, self.lay.right, days[-1] - days[0]
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
        return self._card(i, f)

    def _title(self, i: int, f: int) -> np.ndarray:
        shot, L = self.shots[i], self.layers[self.shots[i]["id"]]
        third, u = self.third, self.u
        frame = self._warp(shot["file"], self.camera(f))
        out = 1 - ramp(f, shot["f1"] - third, third)
        a = 0.30 * out
        if a > 0.002:
            frame[:] = cv2.addWeighted(frame, 1 - a, self.card_bg, a, 0)
        groups = shot["groups"]
        seen = [ramp(f, groups[g], third) for g in range(len(L["groups"]))]
        kick = ramp(f, shot["f0"], third) if L["kicker"] else 0.0
        _put(frame, L["scrim"], max([kick, *seen]) * out)
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
        within = d is not None and d["from"] != "night" and prev is not None and prev["kind"] == "capture"
        if within and w < 1:
            assert prev is not None
            frame = _mix(self._warp(prev["file"], cam), frame, w)
        self._overlays(frame, i, f, w if within else 1.0)
        if d is not None and d["from"] == "night" and w < 1:
            frame = cv2.addWeighted(self.card_bg, 1 - w, frame, w, 0)
        return frame

    def _overlays(self, frame: np.ndarray, i: int, f: int, w: float) -> None:
        """Scrims, date label, words, rail and tag of capture i at frame f (w: its dissolve so far)."""
        shot, L, u = self.shots[i], self.layers[self.shots[i]["id"]], self.u
        prev = self.shots[i - 1] if i else None
        within = w < 1 and prev is not None and prev["kind"] == "capture"
        P = self.layers[prev["id"]] if within and prev is not None else None
        intro = ramp(f, shot["f0"], self.third) if prev is not None and prev["kind"] == "title" else 1.0
        # Scrims, then text.
        _put(frame, self.rail_scrim, intro)
        if P is not None:
            _put(frame, P["label_scrim"], 1 - w)
            _put(frame, L["label_scrim"], w)
        else:
            _put(frame, L["label_scrim"], intro)
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
        pos = (
            shot["rail"] - 1 + w
            if within and prev is not None and prev["series"] == shot["series"]
            else shot["rail"]
        )
        _put(frame, self.rail_layers[shot["series"]], intro)
        if intro > 0.002:
            self._progress(frame, days, pos, intro)
        self._put_tag(frame)

    def _progress(self, frame: np.ndarray, days: list[int], pos: float, opacity: float) -> None:
        u, y = self.u, self.lay.rail_y
        x = self._rail_x(days, pos)
        band = None
        if opacity < 0.998:
            r0, r1 = max(int(y - 12 * u) - 2, 0), min(math.ceil(y + 12 * u) + 2, self.height)
            band = (r0, r1, frame[r0:r1].copy())
        _bar(frame, self.lay.margin, y - 1.5 * u, x, y + 1.5 * u, self.accent)
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
        if shot["kind"] == "card":
            opacity *= 1 - ramp(f, shot["f1"] - third, third)
        _put(frame, L["stack"], opacity)
        prev = self.shots[i - 1] if i else None
        if prev is not None and prev["kind"] in ("title", "capture") and f < shot["f0"] + dip:
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
