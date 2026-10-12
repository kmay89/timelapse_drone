"""Hours of an event-screen loop for a TV's USB player, without re-encoding (film/loop.py).

A loop film (the `screen` preset) is one seamless cycle: closed GOPs of 2 beats, no audio, and frame
N equal to frame 0. `concat_hours` repeats that cycle k = ceil(H·3600 / cycle_s) times with ffmpeg's
concat demuxer and a bitexact stream copy into film/usb/<slug>-<id>-<H>h.mp4 (written as .part,
then renamed), so H hours cost seconds and the same cycle gives the same bytes. TV USB players
usually read FAT32 sticks, whose largest file is 4 GiB: a file that would pass 3.9 GiB is refused
with the largest H that fits. film/usb/ is never packaged, hosted or uploaded as a release asset.
"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

from vantage import log
from vantage.media.ffmpeg import probe, run

FAT32_LIMIT = int(3.9 * 1024**3)  # bytes: under FAT32's 4 GiB file limit, with room for the moov box
MAX_HOURS = 12.0
USB_DIR = "usb"


def hours_name(hours: float) -> str:
    """'1h', '0.25h', '12h'."""
    return f"{hours:g}h"


def cycle_seconds(mp4: Path) -> float:
    """One cycle's length: frames / fps from the plan next to the film, else ffprobe's duration."""
    plan = mp4.with_name(mp4.name.removesuffix(".mp4") + ".plan.json")
    if plan.is_file():
        p = json.loads(plan.read_text(encoding="utf-8"))["plan"]
        return p["frames"] / p["fps"]
    return probe(mp4).duration_s


def copies(hours: float, cycle_s: float) -> int:
    """k = ceil(H·3600 / cycle_s), the cycles that fill H hours."""
    if cycle_s <= 0:
        raise ValueError("a loop film needs a positive length")
    return max(1, math.ceil(round(hours * 3600 / cycle_s, 9)))


def _quote(path: Path) -> str:
    """A path as the concat demuxer reads it (single quotes, a quote escaped as '\\'')."""
    return "'" + path.as_posix().replace("'", "'\\''") + "'"


def concat_hours(mp4: Path, hours: float, out_dir: Path) -> Path:
    """Repeat a loop film for `hours` (0 < H ≤ 12) by stream copy into out_dir (film/usb/) as
    <mp4 stem>-<H>h.mp4. ValueError when H is out of range or the file would pass 3.9 GiB."""
    if not 0 < hours <= MAX_HOURS:
        raise ValueError(f"--hours must be more than 0 and at most {MAX_HOURS:g} (got {hours:g})")
    if not mp4.is_file():
        raise FileNotFoundError(f"no {mp4}; render the loop film first")
    cycle = cycle_seconds(mp4)
    k = copies(hours, cycle)
    size = mp4.stat().st_size
    if k * size > FAT32_LIMIT:
        fit = math.floor(FAT32_LIMIT / size) * cycle / 3600
        raise ValueError(
            f"{hours:g} h of {mp4.name} is {k} cycles, about {k * size / 1024**3:.1f} GiB: over the "
            f"3.9 GiB a FAT32 USB stick holds in one file. Use --hours {math.floor(fit * 100) / 100:g} "
            f"or less (or a smaller film)"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{mp4.name.removesuffix('.mp4')}-{hours_name(hours)}.mp4"
    part = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory(prefix="vantage-concat-") as tmp:
        listing = Path(tmp) / "cycles.txt"
        listing.write_text("ffconcat version 1.0\n" + f"file {_quote(mp4.resolve())}\n" * k, encoding="utf-8")
        args: list[str | Path] = [
            "-f", "concat", "-safe", "0", "-i", listing, "-map", "0:v", "-c", "copy",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
            "-movflags", "+faststart", "-f", "mp4", part,
        ]  # fmt: skip
        try:
            run(args)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    part.replace(out)
    log.info(f"{out.name}: {k} × {cycle:.2f} s cycles ({k * cycle / 3600:.2f} h), stream copy")
    return out


__all__ = ["FAT32_LIMIT", "USB_DIR", "concat_hours", "copies", "cycle_seconds", "hours_name"]
