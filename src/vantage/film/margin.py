"""The flash margin of a story film: the planner's estimate of its flashes, and what it lengthens or
softens to keep every region at most `flash_limit` transitions in any second (film/margin.py).

WCAG 2.3.1 allows 3 flashes a second; the render meter (film/safety.py `LumaMeter`) refuses a film
with more than LIMIT (3) transitions in one region within a second, and only measures what is
already rendered. The planner keeps a margin below that (output.film.flash_limit, default 2):
`derive_cut` plans the film again at a proxy size (`proxy_size`: 480 px on the long edge, a quarter
of the full-size film, metered at the 1/8 size the meter measures a full-size frame at; both from
the full size, so a VANTAGE_FAST preview gets the same margin and beats as the film it previews),
renders that proxy with the real renderer (film/cut.py `_Cut`:
the masters, the camera, the dissolves, the text and its scrims) and meters every frame, at the full
film's 1/8 and at the VANTAGE_FAST film's 1/8, counting luminance runs from PROXY_THRESHOLD (0.09,
below the meter's 0.10: a proxy misjudges a near run by a few per cent). For each
run of more than the limit in one region within `fps` frames, earliest first (`fixes`):

    soften  when they all come from one push (a capture's push in and its ease back, a curtain
            step's move, a stat's push to its hotspot), that push keeps less of its move: its log
            zoom scaled by limit / count × 0.9, never below SOFTEST, or for a pan (a push that zooms
            under PAN_ZOOM) that share of its way, never below PAN_SOFTEST (`Fixes.caps`: key →
            share; the step records it as `gentled`)
    hold    else a beat more at a slot that splits them, on the beat grid: a quiet capture plays a
            beat longer, a step's picture plays a beat before its words land and its push starts, a
            montage picture holds, a curtain holds after its entry ("<id>.0") or after step k's
            sweep ("<id>.<k>"), the first stat holds before its figure lands ("<id>.0") and any
            stat before the next (`Fixes.holds`, at most MAX_HOLD beats a slot). Quiet captures go
            first, then steps and montage pictures, then curtains and stats; among them the slot
            that splits the transitions most evenly, the later of two. An ease back under way at
            the slot runs from its own first frame: a hold moves none of its transitions, so a slot
            inside one splits only a run that also has transitions after it.
    slow    else a move holding two or more of them is softened or, once at SOFTEST, slowed a beat
            (`Fixes.slows`, at most MAX_SLOW): a capture's push then eases back as slowly and the
            capture lasts until it lands B/3 before its end, a curtain step's sweep waits for its
            move, and a stat's push is slowed by holding the stat. When two or more of them fall in
            a capture's ease back to rest (from the next shot's first frame) and the camera then
            rests a beat or more on that vantage, that ease back alone is slowed ("<id>.back"): it
            plays over the shots after the push, so the film gets no longer.

Each fix shifts the measured transitions as it would (a hold moves what follows it a beat later but
not an ease back under way, a slow stretches its move; a softened push is assumed to keep the limit
until measured), so one render finds most fixes; the film is then planned and metered again, at
most PASSES times, each pass rendering only from the shot before the first change
(`Measured.snaps`). The plan records `flash` ({limit, held, slowed, softened}: what decides frames)
and checks `flash` ({estimate, frame, proxy, measures, passes}: not hashed); when the estimate stays
above the limit (nothing left to lengthen) a warning says so. The render meter remains the final
check: it refuses a film above LIMIT, warns above the limit, and a release refuses that too. A
proxy's transitions are memoised by its plan's hash within a process and on disk (work/film/flash/),
and proxy masters decoded once (at a JPEG reduction) within a process.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from vantage.film.safety import CELLS, LumaMeter

PROXY_EDGE = 480  # px: the long edge of the proxy (text set smaller misjudges where its ink falls)
# A proxy counts a luminance run from this size (the meter: 0.10): its frames are smaller, its type
# hinted at other sizes, so a run measured at 0.098 on the proxy can be 0.102 in the film.
PROXY_THRESHOLD = 0.09
PASSES = 6  # proxy renders per film at most
MAX_HOLD = 2  # beats one slot may be held
MAX_SLOW = 4  # beats one push may be slowed
SOFTEST = 1.25  # the margin never softens a push below this zoom
PAN_SOFTEST = 0.34  # nor a pan (a push under PAN_ZOOM) below this share of its way
PAN_ZOOM = 1.08  # a push zooming less than this is a pan (story.MIN_ZOOM)
_MEMO: dict[Any, Any] = {}  # → Measured
_MEMO_SIZE = 64

Event = tuple[int, str, int]  # (frame, kind, region) of a transition


def proxy_size(width: int, height: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """((w, h) a full-size film of width × height is proxied at, (w, h) the proxy is metered at). The
    meter measures a frame downscaled to 1/8 (`safety.regions`): the proxy is metered at 1/8 of the
    full-size film. It is rendered at PROXY_EDGE on its long edge (no larger than the film: smaller,
    its type sets at whole pixel sizes far enough off to move a line's ink into the next region)."""
    n = 2 * CELLS
    measure = (max(n, width // 8), max(n, height // 8))
    s = min(1.0, PROXY_EDGE / max(width, height))
    return (max(n, round(width * s)), max(n, round(height * s))), measure


_DECODED: dict[tuple[str, int, int, int], np.ndarray] = {}  # (path, mtime, size, reduction) → BGR


def _decoded(path: str, scale: float) -> np.ndarray:
    """A master decoded at the coarsest JPEG reduction (1/2, 1/4, 1/8) still at least `scale` of
    its size, cached across proxies (a proxy's pyramid levels are a tenth of a master's size)."""
    factor = next((f for f in (8, 4, 2) if scale <= 1 / f), 1)
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns, stat.st_size, factor)
    if key not in _DECODED:
        flag = {8: cv2.IMREAD_REDUCED_COLOR_8, 4: cv2.IMREAD_REDUCED_COLOR_4, 2: cv2.IMREAD_REDUCED_COLOR_2}
        img = cv2.imread(path, flag.get(factor, cv2.IMREAD_COLOR))
        if img is None:
            raise FileNotFoundError(f"cannot read master {path}")
        if len(_DECODED) >= _MEMO_SIZE:
            del _DECODED[next(iter(_DECODED))]
        _DECODED[key] = img
    return _DECODED[key]


@dataclass
class Measured:
    """A proxy's transitions, and the meters' state at each shot's first frame (`snaps`: frame →
    (meters, transitions so far of each)), so the next pass meters only from where its plan changed
    (none when the transitions came from the disk cache: the next pass then meters from frame 0)."""

    plan: dict[str, Any]
    events: list[Event]
    snaps: dict[int, tuple[list[LumaMeter], list[int]]] = field(default_factory=dict)
    logs: list[list[Event]] = field(default_factory=list)  # each measure's own transitions


_FRAMELESS = frozenset(
    {"shots", "camera", "frames", "grid", "captions", "chapters", "transcript", "poster", "flash"}
)


def first_change(a: dict[str, Any], b: dict[str, Any]) -> int:
    """The first frame on which two story plans may differ: a frame before the start of the shot
    before the first differing shot (its fade may depend on what follows), and before the first
    differing camera move; 0 when anything else that frames depend on differs. (A softer push may
    lower the film's largest zoom and with it the scale of the renderer's second pyramid level: an
    estimate tolerates that.)"""
    if any(a.get(k) != b.get(k) for k in (set(a) | set(b)) - _FRAMELESS):
        return 0
    if (a["grid"]["fpb"], a["grid"]["dissolve"]) != (b["grid"]["fpb"], b["grid"]["dissolve"]):
        return 0
    i = next((k for k, (x, y) in enumerate(zip(a["shots"], b["shots"], strict=False)) if x != y), None)
    if i is None:
        i = min(len(a["shots"]), len(b["shots"]))
    d = a["shots"][max(0, i - 1)]["f0"] if a["shots"] else 0
    j = next((k for k, (x, y) in enumerate(zip(a["camera"], b["camera"], strict=False)) if x != y), None)
    if j is None and len(a["camera"]) != len(b["camera"]):
        j = min(len(a["camera"]), len(b["camera"]))
    if j is not None:
        d = min(d, a["camera"][j - 1]["b"] if j > 0 else 0)
    return max(0, d - 1)


def _code_digest() -> str:
    """sha256 of the renderer's and meter's sources: a cached estimate is only reused by the code that
    made it."""
    global _CODE
    if _CODE is None:
        import hashlib
        from pathlib import Path

        h = hashlib.sha256()
        here = Path(__file__).parent
        for name in ("beat.py", "cut.py", "layout.py", "margin.py", "plan.py", "render.py", "safety.py"):
            h.update(name.encode() + b"\0" + (here / name).read_bytes())
        _CODE = h.hexdigest()
    return _CODE


_CODE: str | None = None


def _cache_path(project: Any, plan: Any, measures: Sequence[tuple[int, int]]) -> Any:
    import hashlib
    import json

    p = plan.plan
    key = json.dumps([plan.hash, p["width"], p["height"], [list(m) for m in measures], _code_digest()])
    return project.work_dir / "film" / "flash" / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def _cached(project: Any, plan: Any, measures: Sequence[tuple[int, int]]) -> list[Event] | None:
    import json

    path = _cache_path(project, plan, measures)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [(int(f), str(kind), int(r)) for f, kind, r in data["events"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _store(project: Any, plan: Any, measures: Sequence[tuple[int, int]], events: list[Event]) -> None:
    """Keep a proxy's transitions in work/film/flash/ (a cache: deleting it only costs time)."""
    import json

    path = _cache_path(project, plan, measures)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + f".{os.getpid()}.part")
        part.write_text(json.dumps({"events": [list(e) for e in events]}), encoding="utf-8")
        part.replace(path)
    except OSError:
        pass  # a read-only project still plans; it only meters again next time


def meter_proxy(
    project: Any,
    plan: Any,
    measures: Sequence[tuple[int, int]],
    before: Measured | None = None,
) -> Measured:
    """Every flash transition of a (proxy) story plan as rendered by film/cut.py, metered at each
    size in `measures` (the full-size film's 1/8, and a VANTAGE_FAST film's 1/8: a transition
    metered at measures[k], k > 0, is logged with kind "<kind>@<k>", so each size is judged on its
    own). Memoised by the plan's hash within a process and on disk in work/film/flash/ (keyed by the
    plan hash, the sizes and the renderer's code); with `before`, the previous pass, only the frames
    from where the plans differ are rendered again. The proxy's masters are decoded once at a JPEG
    reduction and shared (`_decoded`); everything else is the renderer's own."""
    import copy

    from vantage.film.cut import _Cut

    class _Proxy(_Cut):
        def _pyramid(self, file: str) -> tuple[np.ndarray, np.ndarray | None]:
            if file in self._levels:
                return self._levels[file]
            mw, mh = (self.masters[file][k] for k in ("w", "h"))
            s0 = max(self.width / mw, self.height / mh)
            img = _decoded(str(self.project.masters_dir / file), s0 * self.zmax)

            def level(s: float) -> np.ndarray:
                size = (max(1, round(mw * s)), max(1, round(mh * s)))
                return cv2.resize(img, size, interpolation=cv2.INTER_AREA)

            levels = (level(s0), level(s0 * self.zmax) if self.zmax > 1 + 1e-9 else None)
            self._levels[file] = levels  # a proxy is small: keep every master's levels
            return levels

    p = plan.plan
    measures = [tuple(m) for m in measures]
    key = (plan.hash, (p["width"], p["height"]), tuple(measures))
    if key in _MEMO:
        return _MEMO[key]
    stored = _cached(project, plan, measures)
    if stored is not None:
        out = Measured(p, stored)
        _remember(key, out)
        return out
    log: list[Event] = []
    logs: list[list[Event]] = [[] for _ in measures]
    meters = [LumaMeter(p["fps"], size=m, threshold=PROXY_THRESHOLD) for m in measures]
    start = 0
    snaps: dict[int, tuple[list[LumaMeter], list[int]]] = {}
    if before is not None and (before.plan["width"], before.plan["height"]) == (p["width"], p["height"]):
        d = first_change(before.plan, p)
        k = max((f for f in before.snaps if f <= d), default=None)
        if k is not None:
            state, counts = before.snaps[k]
            meters, start = copy.deepcopy(state), k
            logs = [list(old[:n]) for old, n in zip(before.logs, counts, strict=True)]
            snaps = {f: s for f, s in before.snaps.items() if f <= k}
    starts = {s["f0"] for s in p["shots"]}
    cut = _Proxy(project, plan)
    for f in range(start, p["frames"]):
        if f in starts and f not in snaps:
            snaps[f] = (copy.deepcopy(meters), [len(x) for x in logs])
        frame = cut.frame_at(f)
        for meter, out_log in zip(meters, logs, strict=True):
            meter.log = out_log
            meter.add(frame)
            meter.log = None
    for k, out_log in enumerate(logs):
        log += out_log if k == 0 else [(f, f"{kind}@{k}", r) for f, kind, r in out_log]
    log.sort()
    out = Measured(p, log, snaps, logs)
    _store(project, plan, measures, log)
    _remember(key, out)
    return out


def _remember(key: Any, out: Measured) -> None:
    if len(_MEMO) >= _MEMO_SIZE:
        del _MEMO[next(iter(_MEMO))]
    _MEMO[key] = out


def violation(events: Iterable[Event], fps: int, limit: int, *, region: bool = False) -> Any:
    """The first run of limit + 1 transitions of one region within `fps` frames (their frames, ending
    earliest; with `region`, (frames, (kind, region))), or None."""
    by: dict[tuple[str, int], list[int]] = {}
    for f, kind, r in events:
        by.setdefault((kind, r), []).append(f)
    best: tuple[list[int], tuple[str, int]] | None = None
    for key, frames in sorted(by.items()):
        frames.sort()
        for i in range(len(frames) - limit):
            run = frames[i : i + limit + 1]
            if run[-1] - run[0] < fps:
                if best is None or (run[-1], run[0]) < (best[0][-1], best[0][0]):
                    best = (run, key)
                break
    if best is None:
        return None
    return best if region else best[0]


def worst(events: Iterable[Event], fps: int) -> tuple[int, int]:
    """(most transitions of one region within fps frames, the frame that window ends on)."""
    by: dict[tuple[str, int], list[int]] = {}
    for f, kind, r in events:
        by.setdefault((kind, r), []).append(f)
    top, at = 0, 0
    for frames in by.values():
        frames.sort()
        i = 0
        for j, f in enumerate(frames):
            while f - frames[i] >= fps:
                i += 1
            if j - i + 1 > top:
                top, at = j - i + 1, f
    return top, at


@dataclass
class Fixes:
    """What the margin lengthens or softens, by slot or push key (see the module docstring)."""

    holds: dict[str, int] = field(default_factory=dict)  # beats held at a slot
    slows: dict[str, int] = field(default_factory=dict)  # beats added to a camera move
    caps: dict[str, float] = field(default_factory=dict)  # the share of its move a push keeps

    def copy(self) -> Fixes:
        return Fixes(dict(self.holds), dict(self.slows), dict(self.caps))

    def record(self) -> dict[str, Any]:
        return {
            "held": dict(sorted(self.holds.items())),
            "slowed": dict(sorted(self.slows.items())),
            "softened": dict(sorted(self.caps.items())),
        }


@dataclass
class _Slot:
    key: str
    at: int  # frames at or after this move a beat later when held
    rank: int  # 0 a quiet capture, 1 a step or montage picture, 2 a curtain or stat


@dataclass
class _Move:
    spans: list[list[int]]  # [a, b): the move, and the ease back that mirrors it
    full: float  # the zoom of the push it makes or eases back from, unsoftened
    cap: str | None  # the key softening its push (`Fixes.caps`)
    slow: str | None  # the key slowing it (`Fixes.slows`; "hold:<id>": holding that shot slows it)
    back: str | None = None  # the key slowing its ease back to rest alone ("<id>.back")
    room: int = 0  # frames the camera rests after that ease back on its vantage: what it may grow into


def _slots(plan: dict[str, Any]) -> list[_Slot]:
    B = plan["grid"]["fpb"]
    out = []
    for s in plan["shots"]:
        if s["kind"] == "capture":
            step = s.get("step") or {}
            land = step.get("land")
            at = land if land is not None else s["f0"] + B
            quiet = not step and not s.get("montage")
            out.append(_Slot(s["id"], at, 0 if quiet else 1))
        elif s["kind"] == "curtain":
            out += [_Slot(f"{s['id']}.{k}", key["b"], 2) for k, key in enumerate(s["keys"])]
        elif s["kind"] == "stat":
            if s.get("dissolve") or s.get("dip"):  # the first of a run: before its figure lands
                out.append(_Slot(f"{s['id']}.0", s["land"], 2))
            out.append(_Slot(s["id"], s["f1"], 2))
    return out


def _moves(plan: dict[str, Any]) -> list[_Move]:
    """Each capture's push (in, and its ease back or glide to the next), each curtain step's camera
    move and each stat's push to a hotspot, as frame spans."""
    cams = plan["camera"]
    out = []
    for j, s in enumerate(plan["shots"]):
        step = s.get("step") or {}
        if s["kind"] == "capture" and step.get("push_f0") is not None:
            k = next((i for i, w in enumerate(cams) if w["a"] == step["push_f1"]), None)
            spans = [[step["push_f0"], step["push_f1"]]]
            back, room = None, 0
            if k is not None and k + 1 < len(cams):
                spans.append([cams[k]["b"], cams[k + 1]["a"]])
                if cams[k]["b"] == s["f1"]:  # an ease back to rest from the next shot's first frame
                    # It may run on while the camera rests, over this vantage's next captures only.
                    end = s["f1"]
                    for t in plan["shots"][j + 1 :]:
                        if t["kind"] != "capture" or t.get("montage") or t.get("vantage") != s.get("vantage"):
                            break
                        end = t["f1"]
                    back, room = f"{s['id']}.back", min(cams[k + 1]["b"], end) - cams[k + 1]["a"]
            zoom = cams[k]["zoom"] if k is not None else step["focus"][2]
            out.append(_Move(spans, _full(zoom, step), s["id"], s["id"], back, room))
        elif s["kind"] == "curtain":  # each step's move (to its push, or back from it): "<id>.<k>"
            pushed: tuple[str, float] | None = None
            for k, st in enumerate(s["steps"], 1):
                key = f"{s['id']}.{k}"
                if st.get("move"):
                    if st.get("push"):
                        out.append(_Move([list(st["move"])], _full(st["focus"][2], st), key, key))
                    elif pushed is not None:
                        out.append(_Move([list(st["move"])], pushed[1], pushed[0], key))
                pushed = (key, _full(st["focus"][2], st)) if st.get("push") else None
        elif s["kind"] == "stat" and (s.get("move") or {}).get("subjects"):
            move = s["move"]
            # A stat's push runs to its end: holding the stat longer slows it.
            out.append(
                _Move([[move["f0"], move["f1"]]], _full(move["focus"][2], move), s["id"], f"hold:{s['id']}")
            )
    return out


def _full(zoom: float, record: dict[str, Any]) -> float:
    """The zoom a push softened to `record["gentled"]` of its log zoom would make unsoftened."""
    return math.exp(math.log(max(zoom, 1.0)) / record.get("gentled", 1.0))


def fixes(events: list[Event], plan: dict[str, Any], limit: int, given: Fixes) -> tuple[Fixes, bool]:
    """`given` plus the fixes that bring the measured transitions of `plan` to at most `limit` per
    region in any second, found by shifting the transitions as each fix would. For each run of too
    many transitions, earliest first: when they all come from one push (or its ease back), that push
    is softened (`caps`: its log zoom scaled by limit / count, not below SOFTEST); else a slot that
    splits them is held a beat (`holds`; a hold does not move an ease back already under way,
    `under_way`); else a move holding two or more of them is softened or, once at SOFTEST, slowed a
    beat (`slows`): when two or more fall in a capture's ease back to rest and the camera rests a
    beat or more after it before the picture changes vantage, that ease back alone ("<id>.back": it
    plays over the shots after the push, so the film is no longer), else the move. Returns (the
    fixes, whether any was added)."""
    fps, B = plan["fps"], plan["grid"]["fpb"]
    out = given.copy()
    frames = [list(e) for e in events]
    slots, moves = _slots(plan), _moves(plan)
    added = False

    def warp(fn: Callable[[int], int], at: int | None = None) -> None:
        """Shift every transition, slot and move as `fn` does; with `at` (a hold there), an ease
        back already under way at `at` keeps its frames and the transitions in them (`under_way`)."""
        keep = [] if at is None else under_way(at)
        for e in frames:
            if not any(a <= e[0] < b for a, b in keep):
                e[0] = fn(e[0])
        for slot in slots:
            slot.at = fn(slot.at)
        for move in moves:
            move.spans = [span if span in keep else [fn(span[0]), fn(span[1])] for span in move.spans]

    def under_way(at: int) -> list[list[int]]:
        """The ease backs (every span of a move after its first) that started before `at` and run on
        past it. One runs from its shot's first frame for as long as its push went in: holding a
        slot inside it lengthens a shot it plays over, not the ease back, so its transitions stay."""
        return [span for m in moves for span in m.spans[1:] if span[0] < at < span[1]]

    def stays(at: int, f: int) -> bool:
        """Whether a transition on frame f stays put when the slot at `at` is held."""
        return f < at or any(a <= f < b for a, b in under_way(at))

    def ease(span: list[int]) -> None:
        """Stretch the transitions in `span` (an ease back) over a beat more; nothing after it moves."""
        c, d = span
        m = max(1, d - c)
        for e in frames:
            if c <= e[0] < d:
                e[0] = c + round((e[0] - c) * (m + B) / m)
        span[1] = d + B

    def inside(move: _Move, run: list[int]) -> int:
        return max(sum(a <= f < b for f in run) for a, b in move.spans)

    def soften(move: _Move, run: list[int], region: tuple[str, int], allowed: int) -> bool:
        """Ask `move.cap`'s push for less of its move: its share (`caps`) times allowed / n × 0.9 of
        its log zoom, never below SOFTEST (a pan, under SOFTEST: never below PAN_SOFTEST of its way)."""
        assert move.cap is not None
        n = inside(move, run)
        share = out.caps.get(move.cap, 1.0)
        full = move.full
        if full > SOFTEST + 1e-6:
            least = math.log(SOFTEST) / math.log(full)
        else:  # a pan (no zoom to speak of) may move less; a gentle push already is as soft as allowed
            least = PAN_SOFTEST if full < PAN_ZOOM else 1.0
        soft = max(least, share * allowed / n * 0.9)
        if allowed < 1 or share - soft < 0.05:
            return False
        out.caps[move.cap] = round(soft, 3)
        # Assume the softer push keeps at most `allowed` of its transitions in any region (the next
        # pass measures it), so other regions of the same push are not fixed twice.
        mine: dict[tuple[str, int], list[list[Any]]] = {}
        for e in frames:
            if any(a <= e[0] < b for a, b in move.spans):
                mine.setdefault((e[1], e[2]), []).append(e)
        drop = {id(e) for group in mine.values() for e in group[allowed:]}
        frames[:] = [e for e in frames if id(e) not in drop]
        return True

    for _ in range(64):
        found = violation(((e[0], e[1], e[2]) for e in frames), fps, limit, region=True)
        if found is None:
            break
        run, region = found
        own = [m for m in moves if m.cap is not None and inside(m, run) == len(run)]
        if own and soften(own[0], run, region, limit):
            added = True
            continue
        # A slot splits the run when holding it moves some of its transitions and not others.
        staying = {id(s): sum(stays(s.at, f) for f in run) for s in slots}
        open_slots = [
            s for s in slots if 0 < staying[id(s)] < len(run) and out.holds.get(s.key, 0) < MAX_HOLD
        ]
        if open_slots:
            slot = min(open_slots, key=lambda s: (s.rank, abs(2 * staying[id(s)] - len(run)), -s.at))
            out.holds[slot.key] = out.holds.get(slot.key, 0) + 1
            at = slot.at
            warp(lambda t, at=at: t + B if t >= at else t, at)
            added = True
            continue
        busy = sorted((m for m in moves if inside(m, run) >= 2), key=lambda m: -inside(m, run))
        if any(
            m.cap is not None and soften(m, run, region, limit - (len(run) - inside(m, run))) for m in busy
        ):
            added = True
            continue
        # Two or more in a capture's ease back to rest: slowing it alone adds no length.
        back = next(
            (
                m for m in moves
                if m.back is not None and m.room >= B and out.slows.get(m.back, 0) < MAX_SLOW
                and sum(m.spans[1][0] <= f < m.spans[1][1] for f in run) >= 2
            ),
            None,
        )  # fmt: skip
        if back is not None and back.back is not None:
            out.slows[back.back] = out.slows.get(back.back, 0) + 1
            ease(back.spans[1])
            back.room -= B
            added = True
            continue

        def slowed(m: _Move) -> dict[str, int]:
            return out.holds if (m.slow or "").startswith("hold:") else out.slows

        def name(m: _Move) -> str:
            return (m.slow or "").removeprefix("hold:")

        move = next((m for m in busy if m.slow is not None and slowed(m).get(name(m), 0) < MAX_SLOW), None)
        if move is None:
            break  # nothing left to lengthen between them: the estimate stays (a warning says so)
        slowed(move)[name(move)] = slowed(move).get(name(move), 0) + 1
        (a, b), *rest = move.spans
        n = b - a

        def stretch(t: int, a: int = a, b: int = b, n: int = n) -> int:
            if a <= t < b:
                return a + round((t - a) * (n + B) / n)
            return t + B if t >= b else t

        warp(stretch)
        for span in rest:  # its ease back, as slow (nothing after it moves)
            ease(span)
        move.room -= B  # its ease back starts a beat later and lasts a beat longer
        added = True
    return out, added


__all__ = [
    "MAX_HOLD",
    "MAX_SLOW",
    "PASSES",
    "PROXY_EDGE",
    "PROXY_THRESHOLD",
    "SOFTEST",
    "Fixes",
    "fixes",
    "meter_proxy",
    "proxy_size",
    "violation",
    "worst",
]
